"""Independent routing operations for the flex core and rigid islands."""

import re
import uuid

from eedesigncore import BoardLayer, ProjectFile, Property
from eedesigncore.common.layers import LAYER_NAME_TO_ORDINAL

from eedesignpanels import material, toolpath

RIGID_REGION_LAYER = "User.24"
FLEX_ROUTE_LAYER = "User.37"
CONSTRUCTION_FIELDS = ("PCB_CONSTRUCTION_TYPE", "CONSTRUCTION_TYPE")


def is_rigid_flex(board, project_path=None):
    """Read the construction type from the source project or board properties."""
    values = {prop.name: prop.value for prop in board.properties}
    if project_path is not None and project_path.is_file():
        variables = ProjectFile.from_file(project_path).root.get_object("text_variables")
        if variables is not None:
            for field in CONSTRUCTION_FIELDS:
                value = variables.get(field)
                if value is not None:
                    values[field] = value
    return any(re.sub(r"[\s_-]+", "", str(values.get(field, ""))).upper() == "RIGIDFLEX"
               for field in CONSTRUCTION_FIELDS)


def route_layer(board):
    """Only the flex routing pass cuts through the assembled rigid-flex panel."""
    return FLEX_ROUTE_LAYER if is_rigid_flex(board) else "User.36"


def mark_construction(board):
    """Carry the construction type into the generated panel board."""
    for prop in board.properties:
        if prop.name == CONSTRUCTION_FIELDS[0]:
            prop.value = "RIGID FLEX"
            return
    board.properties.append(Property(name=CONSTRUCTION_FIELDS[0], value="RIGID FLEX"))


def region_loops(board, tolerance, tabs=(), warnings=None):
    """Close rigid tab mouths before collecting the rigid-region outlines."""
    from eedesignpanels.stages.route_toolpath import bridge_mouths, outline_primitives

    view = board.model_copy(deep=True)
    for items in (view.drawings, view.trailing_drawings,
                  *(footprint.drawings for footprint in view.footprints)):
        for item in items:
            layer = getattr(item, "layer", None)
            if layer == "Edge.Cuts":
                item.layer = "User.25"
            elif layer == RIGID_REGION_LAYER:
                item.layer = "Edge.Cuts"
            layers = getattr(item, "layers", None)
            if layers:
                item.layers = [
                    "User.25" if name == "Edge.Cuts" else
                    "Edge.Cuts" if name == RIGID_REGION_LAYER else name
                    for name in layers
                ]
    try:
        primitives = outline_primitives(view)
    except ValueError as exc:
        raise ValueError("Rigid-flex routing needs closed outlines on User.24-RigidRegion.") from exc
    applicable = [tab for tab in tabs if tab_on_outline(tab, primitives, tolerance)]
    primitives, _ = bridge_mouths(
        primitives, applicable, warnings if warnings is not None else [],
    )
    return [toolpath.weld_chain(loop) for loop in toolpath.chain_loops(primitives, tolerance)]


def tab_on_outline(tab, outline, tolerance):
    """Match a tab by its base or both declared mouth points."""
    def on_outline(point):
        return any(toolpath.distance_to_primitive(point, piece)
                   <= tolerance + 0.000002 for piece in outline)

    return on_outline(tab.position) or (
        bool(tab.mouth) and all(on_outline(point.position) for point in tab.mouth)
    )


def build(board, config):
    """Route the complete flex outline and each independent rigid island."""
    from eedesignpanels.stages import route_toolpath

    mark_construction(board)
    if not any(layer.canonical_name == FLEX_ROUTE_LAYER for layer in board.layers):
        board.layers.append(BoardLayer(
            ordinal=LAYER_NAME_TO_ORDINAL[FLEX_ROUTE_LAYER],
            canonical_name=FLEX_ROUTE_LAYER, layer_type="user",
            user_name="User.37-RouteToolPathFlex"))
    options = dict(heal_gap_mm=config.outline_heal_mm, warn_unmatched=False,
                   pocket_cutouts=config.pocket_cutouts)
    flex = route_toolpath.build(
        board, config.prep.cutter_diameter, config.prep.vscore, config.min_tab_mm,
        toolpath_layer=FLEX_ROUTE_LAYER, **options)
    rigid_items, rigid_warnings, results = [], [], []
    loops = region_loops(board, config.outline_heal_mm, flex.tabs, rigid_warnings)
    for index, loop in enumerate(loops):
        if material.loop_is_hole(loops, index, config.prep.cutter_diameter):
            continue
        holes = [hole for other, hole in enumerate(loops)
                 if material.loop_is_hole(loops, other, config.prep.cutter_diameter)
                 and toolpath.point_inside_loop(loop, toolpath.midpoint_of(hole[0]))]
        outline = [piece for contour in [loop, *holes] for piece in contour]
        tabs = [tab for tab in flex.tabs
                if tab_on_outline(tab, outline, config.outline_heal_mm)]
        view = board.model_copy(deep=True)
        result = route_toolpath.build(
            view, config.prep.cutter_diameter, config.prep.vscore, config.min_tab_mm,
            outline_override=outline, tabs_override=tabs, **options)
        for item in result.items:
            item.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rigid-island:{index}:{item.id}"))
        results.append(result)
        rigid_items.extend(result.items)
        rigid_warnings.extend(f"Rigid island {index + 1}: {warning}" for warning in result.warnings)
    route_toolpath.clear_toolpath(board)
    board.drawings.extend(rigid_items)
    warnings = list(flex.warnings) + rigid_warnings
    used = {end.end_id for result in (flex, *results) for end in result.junctions}
    for key in sorted(set(config.prep.vscore.route_end_overrides) - used) if config.prep.vscore.custom_route_ends else ():
        warnings.append(f"Route end override {key} no longer matches this outline. Reassign or remove it.")
    warnings.append("Rigid and flex routing are separate operations. Only the flex route defines the assembled panel outline.")
    warnings.append(f"Rigid routing: {len(results)} islands, {len(rigid_items)} items on User.36. Flex routing: {len(flex.items)} items on User.37.")
    return flex._replace(warnings=warnings, rigid_results=tuple(results))
