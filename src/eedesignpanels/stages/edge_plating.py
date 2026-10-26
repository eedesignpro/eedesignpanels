"""Mark the routed board contour with native KiCad lines and arcs."""

import copy
import math
import uuid

from eedesigncore import PcbArc, PcbGroup, PcbLine, PtsArc, Stroke
from eedesigncore.common.layers import LAYER_NAME_TO_ORDINAL
from eedesigncore.pcb.models import BoardLayer

from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import (
    NODE_TOL_MM, Arc, Line, arc_from_three_points, bounded_intersections, contact_curve,
    midpoint_of, on_primitive,
    point_inside_loop, split_primitive, sub_primitive,
)

LAYER = "User.39"
LAYER_NAME = "User.39-EdgePlating"
STROKE_MM = 0.4
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "eedesignpro/eedesignpanels/edge-plating")
MASK_GROUP_ID = str(uuid.uuid5(NAMESPACE, "mask-graphics"))
MASK_LAYERS = ("F.Mask", "B.Mask")


def sync_board_setting(board):
    """Set KiCad's plating flag from actual plating lines and arcs."""
    present = any(
        isinstance(item, (PcbLine, PcbArc))
        and item.layer == LAYER
        and item.stroke is not None and item.stroke.width > 0
        and math.dist(item.start, item.end) > 0
        for item in (*board.drawings, *board.trailing_drawings)
    )
    if board.setup is None or board.setup.stackup is None:
        if present:
            raise ValueError(
                "Cannot save edge plating without a board stackup. "
                "Save the board in KiCad with a defined stackup, then reload it."
            )
        return
    board.setup.stackup.edge_plating = present


def copper_keepouts(board):
    """Read copper-pour exclusions on either outer copper layer."""
    zones = list(board.zones)
    for footprint in board.footprints:
        # KiCad stores footprint zone vertices in board coordinates.
        zones.extend(footprint.zones)
    for zone in zones:
        if zone.keepout is None or zone.keepout.copper_pour:
            continue
        layers = set(zone.layers or ())
        if zone.layer:
            layers.add(zone.layer)
        if not layers.intersection({"F.Cu", "B.Cu", "*.Cu", "F&B.Cu"}):
            continue
        loops = []
        for polygon in zone.polygons:
            elements = list(polygon.pts)
            loop = []
            for element, following in zip(elements, elements[1:] + elements[:1]):
                if isinstance(element, PtsArc):
                    loop.append(arc_from_three_points(element.start, element.mid, element.end))
                    end = element.end
                else:
                    end = tuple(element)
                start = following.start if isinstance(following, PtsArc) else tuple(following)
                if math.dist(end, start) > 1e-7:
                    loop.append(Line(end, start))
            if loop:
                loops.append(loop)
        if loops:
            yield loops


def outside_keepouts(edges, regions):
    """Split the contour at rule-area boundaries and retain the outside parts."""
    boundaries = [edge for loops in regions for loop in loops for edge in loop]
    for edge in edges:
        cuts = [point for boundary in boundaries
                for point in bounded_intersections(edge, boundary)]
        cuts.extend(point for boundary in boundaries
                    for point in (boundary.start, boundary.end) if on_primitive(edge, point))
        for part in split_primitive(edge, cuts):
            middle = midpoint_of(part)
            on_boundary = any(on_primitive(boundary, middle) for boundary in boundaries)
            inside = any(sum(point_inside_loop(loop, middle) for loop in loops) % 2
                         for loops in regions)
            if not on_boundary and not inside:
                yield part


def retract_path_ends(edges, distance):
    """Retract open paths by arc length. Keep internal joins and closed loops intact."""
    paths = []
    for edge in edges:
        if not paths or math.dist(paths[-1][-1].end, edge.start) > NODE_TOL_MM:
            paths.append([])
        paths[-1].append(edge)
    if len(paths) > 1 and math.dist(paths[-1][-1].end, paths[0][0].start) <= NODE_TOL_MM:
        paths[0] = paths.pop() + paths[0]
    for path in paths:
        if math.dist(path[0].start, path[-1].end) <= NODE_TOL_MM:
            yield from path
            continue
        length = sum(piece.length for piece in path)
        if length <= 2.0 * distance:
            continue
        cursor = 0.0
        for piece in path:
            low = max(distance, cursor)
            high = min(length - distance, cursor + piece.length)
            if high > low + 1e-7:
                yield sub_primitive(piece, (low - cursor) / piece.length,
                                    (high - cursor) / piece.length)
            cursor += piece.length


def contact_edges(runs, chain):
    """Keep actual contour contacts and exclude the tab-mouth chords."""
    for run in runs:
        for piece in run:
            if not 0 <= piece.origin < len(chain):
                continue
            source = chain[piece.origin]
            if source.role == "mouth" or type(source) is not type(piece):
                continue
            contact = contact_curve(piece, chain)
            if contact.length <= 1e-7:
                continue
            # Offset extensions outside the original contour are not board edges.
            points = [point for point in (source.start, source.end)
                      if on_primitive(contact, point)]
            for part in split_primitive(contact, points):
                if part.length > 1e-7 and on_primitive(source, midpoint_of(part)):
                    yield part


def clear(board):
    """Remove plating and owned mask graphics. Preserve other mask artwork."""
    members = {member for group in board.groups if group.id == MASK_GROUP_ID
               for member in group.members}
    for drawings in (board.drawings, board.trailing_drawings):
        drawings[:] = [item for item in drawings
                       if getattr(item, "layer", None) != LAYER
                       and not (getattr(item, "layer", None) in MASK_LAYERS
                                and item.id in members)]
    board.groups[:] = [group for group in board.groups if group.id != MASK_GROUP_ID]


def generate(board, config, result=None):
    """Replace board-level plating graphics. All edits remain in memory."""
    if result is None:
        source = copy.deepcopy(board)
        result = route_toolpath.build(
            source, config.prep.cutter_diameter, config.prep.vscore, config.min_tab_mm,
            heal_gap_mm=config.outline_heal_mm, pocket_cutouts=config.pocket_cutouts,
        )
    edges = list(contact_edges(result.boundary_runs, result.boundary_chain))
    for job in result.jobs:
        if not job.is_boundary and job.made and job.path:
            edges.extend(contact_edges((job.path,), job.loop))
    edges = list(outside_keepouts(edges, list(copper_keepouts(board))))
    layers = [(LAYER, STROKE_MM)]
    for enabled, layer in ((config.edge_plating_front_mask, "F.Mask"),
                           (config.edge_plating_back_mask, "B.Mask")):
        if enabled:
            layers.append((layer, config.edge_plating_mask_width_mm))
    items = []
    for layer, width in layers:
        for index, piece in enumerate(retract_path_ends(edges, width / 2.0)):
            key = f"{index}|{piece}" if layer == LAYER else f"{layer}|{index}|{piece}"
            identity = str(uuid.uuid5(NAMESPACE, key))
            if isinstance(piece, Line):
                item = PcbLine(start=piece.start, end=piece.end, layer=layer,
                               stroke=Stroke(width=width), id=identity)
            elif isinstance(piece, Arc):
                item = PcbArc(start=piece.start, mid=piece.mid, end=piece.end,
                              layer=layer, stroke=Stroke(width=width), id=identity)
            else:
                continue
            items.append(item)
    if LAYER not in {layer.canonical_name for layer in board.layers}:
        board.layers.append(BoardLayer(
            ordinal=LAYER_NAME_TO_ORDINAL[LAYER], canonical_name=LAYER,
            layer_type="user", user_name=LAYER_NAME,
        ))
    else:
        for layer in board.layers:
            if layer.canonical_name == LAYER:
                layer.user_name = LAYER_NAME
    clear(board)
    declared = {layer.canonical_name for layer in board.layers}
    for layer, _ in layers:
        if layer not in declared:
            board.layers.append(BoardLayer(
                ordinal=LAYER_NAME_TO_ORDINAL[layer], canonical_name=layer,
                layer_type="user",
            ))
    board.drawings.extend(items)
    members = [item.id for item in items if item.layer in MASK_LAYERS]
    if members:
        board.groups.append(PcbGroup(
            name="eedesignpanels edge plating mask", id=MASK_GROUP_ID, members=members,
        ))
    return len(items)
