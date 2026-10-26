"""Rail copper: ONE no-net pour over the whole carrier, held off by rule areas.

A bare laminate rail warps in a reflow oven and etches
unevenly in the bath, so a panel's rails carry copper on every layer. It
belongs to nobody electrically - it is a no-net pour - and it is held off
everything else by one clearance the fabricator quotes.

**ONE ZONE, FOR ANY COMBINATION OF SETTINGS**, because KiCad does not handle
overlapping zones of the same net well. Separate rectangles for each rail and
gap would meet at the corners and at the gap crossings, which is same-net zones
overlapping. So the pour is one zone whose outline is the whole carrier, and
rule areas keep it off what must stay clear.

**ONE ZONE OBJECT NAMING EVERY COPPER LAYER, never one object per layer.** The
same rule holds
for every rule area this stage makes. KiCad's format takes a `(layers ...)`
list on a zone and on a rule area alike, so nothing here has to be repeated per
layer.

**WHERE THE POUR IS HELD OFF, and why that is safe.** Each board's
own zones are flattened to plain copper polygons before the panel is built
(:mod:`eedesignpanels.stages.zones`), so nothing on a board is a zone any more
and there is no same-net zone overlap left to go wrong. What that also means is
that a board's copper is GRAPHICS, and a zone clearance does not hold a pour off
a graphic. The rule area is what holds the pour off that copper, and the outset
is what gives it its clearance:

- `pour_between_boards` ON: the SOURCE board's `Edge.Cuts` outline - the full
  outline, board-level drawings plus every footprint's - outset by the panel
  zone clearance, as a rule area on every copper layer, stepped and repeated
  with the boards. One per board instance, so copper reaches into the gaps and
  onto the tabs but never onto a board.
- `pour_between_boards` OFF: one rectangle over the whole board array, outset
  by the same clearance. Copper stays on the rails and out of the gaps.

**THE OUTSET IS THE ROUTER'S OWN OFFSET.** A cutter path is the
board outline offset outward by half the cutter diameter, and
:func:`eedesignpanels.toolpath.resolve_offset` is what computes it, exactly, over
all six shapes KiCad accepts on `Edge.Cuts`. The keep-out asks that same
function for the same kind of offset at a different distance. There is no
second offset implementation in this stage.

**A RULE AREA HERE STOPS THE POUR AND NOTHING ELSE.** `copper_pour` is the only
thing set `not_allowed`; tracks, vias, pads and footprints stay allowed. A rule
area laid over a whole board that forbade pads would put every pad on the panel
in violation, which is the opposite of what this is for. Contrast
:mod:`eedesignpanels.stages.panel_scores`, whose rule area guards a saw line and
therefore forbids everything.

**Nothing here computes copper.** The zone is created, KiCad fills it in a
sandbox, and the fill records are transplanted back
(:mod:`eedesignpanels.stages.refill`). The instances' copper is already flat
graphics by the time this runs, so no refill can touch it, and the transplant
proves it did not.

The one KiCad setting this stage cannot leave alone is island removal. Set it
to KiCad's default and a rail pour comes back nearly empty, because a no-net
pour is all islands - measured, with numbers, in `refill`'s own docstring. The
pour this stage makes says "never remove islands", and `refill` refuses a zone
that does not.
"""

import math
import uuid
from typing import NamedTuple

from eedesigncore import (
    Board,
    PtsArc,
    Zone,
    ZoneFill,
    ZoneKeepout,
    ZoneOutlinePolygon,
)

from eedesignpanels.stages import refill as refill_module
from eedesignpanels.stages import route_toolpath as route_stage
from eedesignpanels.toolpath import (
    Line,
    OffsetError,
    chain_loop,
    resolve_offset,
    weld_chain,
)

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so a panel built twice on two
# machines gives its rail pour the same identities.
RAIL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/rail-copper"
)
# The rule areas are a second family with a second namespace, so resizing the
# carrier never renames a keep-out and moving a board never renames the pour.
KEEPOUT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/rail-copper-keepout"
)

# The name the generated rail pour carries. KiCad puts a zone's name in its DRC
# messages, so this is what the designer reads when something fouls it.
# `pour_zones` filters on the prefix, so the keep-out name below must not start
# with it.
ZONE_NAME_PREFIX = "PanelRail"
POUR_NAME = f"{ZONE_NAME_PREFIX}_carrier"
KEEPOUT_NAME = "Panel pour keep-out"

# KiCad's own defaults for a pour, so a panel zone opened in the editor reads
# like a zone rather than like something a script invented. The clearance is
# the config's and is the only one of them a panel changes.
DEFAULT_MIN_THICKNESS_MM = 0.2
DEFAULT_THERMAL_GAP_MM = 0.5
DEFAULT_THERMAL_BRIDGE_MM = 0.5

# A rectangle thinner than this is not a shape. Below it there is nothing to
# pour into and nothing to keep anything out of.
MIN_STRIP_MM = 1e-6

# What a pour that came back with nothing in it means, said once so the two
# places that can see it say the same thing.
EMPTY_POUR_WARNING = (
    "the rail pour came back from the fill with no copper at all - the "
    "clearance or the keep-outs may be covering the whole carrier"
)

# An arc goes into a `(pts ...)` chain as start, mid and end, and those three
# points name one arc only while it turns less than a whole circle. Half a turn
# is the split point: it is well clear of the degenerate case, and it keeps a
# round board's offset - one arc of a whole turn - expressible.
MAX_ARC_SWEEP = math.pi


class Settings(NamedTuple):
    """What a rail pour needs, in millimetres and plain values."""

    clearance_mm: float = 0.635
    pour_between_boards: bool = False

    @classmethod
    def of(cls, value) -> "Settings":
        """Settings from a config section, a mapping, or another Settings."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                clearance_mm=float(value.get("clearance_mm", 0.635)),
                pour_between_boards=bool(
                    value.get("pour_between_boards", False)
                ),
            )
        return cls(
            clearance_mm=float(value.clearance_mm),
            pour_between_boards=bool(value.pour_between_boards),
        )


class RailZone(NamedTuple):
    """The pour, named for the part of the panel it covers. One of these now."""

    name: str
    rect: tuple

    @property
    def width_mm(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height_mm(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm


class RailKeepout(NamedTuple):
    """One rule area the pour is held off by, measured rather than described.

    `shaped` says whether it followed a board's own outline or fell back to that
    board's rectangle, which is the one thing a reader of a report needs to know
    about it.
    """

    name: str
    key: str
    bbox: tuple
    points: int
    shaped: bool


class RailCopper(NamedTuple):
    """What the stage put on the panel, and what the refill made of it."""

    zones: tuple
    layers: tuple
    clearance_mm: float
    pour_between_boards: bool
    # The rule areas that hold the pour off the boards, in the order they were
    # made. Empty when there was no board rectangle to keep anything off.
    keepouts: tuple = ()
    # None when the caller asked for the zones without the fill - which the
    # panel page does, because a fill costs a KiCad process.
    refill: object = None

    @property
    def count(self) -> int:
        return len(self.zones)

    @property
    def area_mm2(self) -> float:
        return sum(zone.area_mm2 for zone in self.zones)

    @property
    def keepout_count(self) -> int:
        return len(self.keepouts)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def carrier_rect(blank) -> RailZone:
    """The one pour: the whole carrier blank.

    **THE OUTLINE IS THE BLANK, NOT THE DERIVED PANEL EDGE.** KiCad
    clips a fill to the board outline less the copper-to-edge clearance whatever
    the zone outline says, so a pour drawn to the blank fills exactly the
    material that is there - the rails, the tabs, and nothing inside a routed
    channel. Drawing the derived edge instead would mean handing KiCad a zone
    with several outlines and depending on how it reads them, which is a thing
    nobody should have to know.
    """
    return RailZone(name="carrier", rect=blank.bbox)


def outset_bbox(bbox: tuple, distance: float) -> tuple:
    """A rectangle grown outward on every side by *distance*."""
    return (bbox[0] - distance, bbox[1] - distance,
            bbox[2] + distance, bbox[3] + distance)


def rect_pts(rect: tuple) -> list:
    """A rectangle as a zone outline's four corners, clockwise from top left."""
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _arc_elements(arc) -> list:
    """One offset arc as `(arc ...)` elements of a zone outline's `pts` chain.

    Split in half until each piece turns less than :data:`MAX_ARC_SWEEP`, so the
    three stored points always name the piece unambiguously.
    """
    if arc.sweep <= MAX_ARC_SWEEP + 1e-12:
        return [PtsArc(start=arc.start, mid=arc.mid, end=arc.end)]
    half = arc.sweep / 2.0
    middle = arc.a0 + (half if arc.ccw else -half)
    return (_arc_elements(arc._replace(a1=middle))
            + _arc_elements(arc._replace(a0=middle)))


def loop_pts(loop) -> list:
    """An offset loop as a zone outline's `pts` chain - lines and TRUE arcs.

    A line contributes its start point and an arc contributes an `(arc ...)`
    element, because KiCad's own outline parser takes the two interleaved
    (`parseOutlinePoints`, `pcb_io_kicad_sexpr_parser.cpp:389-461`, 10.0.3) and
    `SHAPE_LINE_CHAIN::Append` drops a point that repeats the last one
    (`shape_line_chain.cpp:1576`) - so the junction between two pieces is
    written once however the pieces meet. No arc is ever flattened into
    segments: the keep-out is the board's own shape.
    """
    pts = []
    for piece in loop:
        if isinstance(piece, Line):
            pts.append((piece.p0[0], piece.p0[1]))
        else:
            pts.extend(_arc_elements(piece))
    return pts


def outset_outline(primitives, distance: float) -> list:
    """The outline grown outward by *distance*, as the router's own offset.

    Straight to :func:`eedesignpanels.toolpath.resolve_offset` - the function
    that turns a board outline into a cutter path - at the clearance instead of
    the cutter radius. Lines offset to parallel lines, arcs to concentric arcs,
    and a convex corner grows a fillet of exactly *distance*. There is no second
    offset implementation in this stage.

    Raises:
        toolpath.OffsetError: for an outline that is not a loop, or an offset
            that cannot be walked closed.
    """
    return resolve_offset(weld_chain(chain_loop(list(primitives))), distance).loop


def placed_pts(pts, placement) -> list:
    """A `pts` chain turned and moved onto one board instance.

    The transform is the one `inject_board` applies and the one
    `placement.Placement` documents: a turn about the origin, then the offset.
    Every turn in an arrangement is a quarter turn, so `rotate_kicad` swaps and
    negates and nothing rounds - which is what keeps two builds of one config
    byte-identical.
    """
    angle = placement.rotation_deg
    dx, dy = placement.dx_mm, placement.dy_mm

    def at(point: tuple) -> tuple:
        turned = route_stage.rotate_kicad(point, angle)
        return (turned[0] + dx, turned[1] + dy)

    moved = []
    for element in pts:
        if isinstance(element, PtsArc):
            moved.append(PtsArc(
                start=at(element.start), mid=at(element.mid), end=at(element.end)
            ))
        else:
            moved.append(at(element))
    return moved


def _pts_bbox(pts) -> tuple:
    """The extent of a `pts` chain, measured over an arc's three stored points."""
    points = []
    for element in pts:
        if isinstance(element, PtsArc):
            points.extend((element.start, element.mid, element.end))
        else:
            points.append(element)
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def keepout_plan(instance_bbox: tuple, instance_boxes, settings,
                 source_outline=(), placements=(), warnings=None) -> list:
    """Every rule area the pour must be held off by, and the `pts` for each.

    Returns `(RailKeepout, pts)` pairs in a fixed order - the array rectangle
    alone when `pour_between_boards` is off, one per board instance when it is
    on. The ORDER is the arrangement's own row-major order, so a panel built
    twice makes them in the same order with the same identities.

    **THE SHAPED KEEP-OUT NEEDS TWO THINGS THE PANEL NO LONGER HOLDS**: the
    source board's outline and where every copy of it sits. Both are carried
    from the step-and-repeat measurement. Without them, or when the offset
    cannot be walked closed, each board falls back to its own RECTANGLE outset
    by the same clearance - pessimistic, never wrong, and said out loud in a
    warning rather than left to be noticed in a render.
    """
    warnings = warnings if warnings is not None else []
    clearance = settings.clearance_mm

    if not settings.pour_between_boards:
        # ONE RECTANGLE OVER THE WHOLE ARRAY. With
        # the pour kept out of the boards AND of the gaps between them, copper
        # lands on the rails only, which is what the setting being off means.
        rect = outset_bbox(instance_bbox, clearance)
        if rect[2] - rect[0] <= MIN_STRIP_MM or rect[3] - rect[1] <= MIN_STRIP_MM:
            return []
        pts = rect_pts(rect)
        return [(RailKeepout(name=KEEPOUT_NAME, key="array", bbox=rect,
                             points=len(pts), shaped=False), pts)]

    boxes = list(instance_boxes)
    places = list(placements)
    shaped = None
    if source_outline and places:
        try:
            shaped = loop_pts(outset_outline(source_outline, clearance))
        except (OffsetError, ValueError) as failure:
            warnings.append(
                f"the boards' own outline could not be grown by the "
                f"{clearance:g} mm pour clearance ({failure}), so each board is "
                f"kept clear of the pour by its RECTANGLE instead - copper will "
                f"stop short of a shaped board's concave edges"
            )
    elif boxes:
        warnings.append(
            "this panel was built without the source board's outline, so each "
            "board is kept clear of the pour by its RECTANGLE instead of by its "
            "own shape"
        )

    if shaped is not None:
        plans = []
        for place in places:
            pts = placed_pts(shaped, place)
            plans.append((
                RailKeepout(
                    name=KEEPOUT_NAME,
                    key=f"board|{place.column}|{place.row}",
                    bbox=_pts_bbox(pts), points=len(pts), shaped=True,
                ),
                pts,
            ))
        return plans

    plans = []
    for index, box in enumerate(boxes):
        rect = outset_bbox(box, clearance)
        if rect[2] - rect[0] <= MIN_STRIP_MM or rect[3] - rect[1] <= MIN_STRIP_MM:
            continue
        pts = rect_pts(rect)
        plans.append((
            RailKeepout(name=KEEPOUT_NAME, key=f"box|{index}", bbox=rect,
                        points=len(pts), shaped=False),
            pts,
        ))
    return plans


# ---------------------------------------------------------------------------
# the board items
# ---------------------------------------------------------------------------


def copper_layers(board: Board) -> tuple:
    """Every copper layer the board declares, in the board file's own order."""
    return tuple(
        layer.canonical_name
        for layer in board.layers
        if layer.canonical_name.endswith(".Cu")
    )


def zone_for(strip: RailZone, layers, clearance_mm: float) -> Zone:
    """The carrier pour as ONE no-net copper zone naming every copper layer.

    One object, not one per layer: KiCad's zone
    takes a `(layers ...)` list, and a pour repeated per layer is the same net
    poured several times over the same material.

    The uuid is derived from the rectangle, so the same panel built twice names
    the same pour and resizing the carrier gives it a new identity rather than
    silently reusing the old one.
    """
    key = f"{strip.name}|" + "|".join(f"{round(v, 6) + 0.0:.6f}" for v in strip.rect)
    return Zone(
        net=0,
        net_name="",
        layers=list(layers),
        id=str(uuid.uuid5(RAIL_NAMESPACE, key)),
        name=(POUR_NAME if strip.name == "carrier"
              else f"{ZONE_NAME_PREFIX}_{strip.name}"),
        hatch_style="edge",
        hatch_pitch=0.508,
        connect_pads_clearance=clearance_mm,
        min_thickness=DEFAULT_MIN_THICKNESS_MM,
        fill=ZoneFill(
            filled=True,
            thermal_gap=DEFAULT_THERMAL_GAP_MM,
            thermal_bridge_width=DEFAULT_THERMAL_BRIDGE_MM,
            island_removal_mode=refill_module.ISLAND_REMOVAL_NEVER,
        ),
        polygons=[ZoneOutlinePolygon(pts=rect_pts(strip.rect))],
    )


def keepout_for(area: RailKeepout, pts, layers) -> Zone:
    """One rule area that stops the pour, on every copper layer, as ONE object.

    **ONLY `copper_pour` IS FORBIDDEN.** This rule area lies over a
    whole board, and a board is covered in tracks, pads and footprints that are
    all exactly where they belong. Forbidding them would put the entire panel in
    DRC violation and say nothing true. What must not happen here is a pour, and
    `ZoneKeepout`'s fields read "is this ALLOWED", so `copper_pour=False` is the
    whole of the rule.

    The identity comes from the board's GRID CELL, never from its offset in
    millimetres, so a panel built twice names the same rule areas.
    """
    return Zone(
        layers=list(layers),
        id=str(uuid.uuid5(KEEPOUT_NAMESPACE, area.key)),
        name=area.name,
        hatch_style="full",
        hatch_pitch=0.508,
        keepout=ZoneKeepout(
            tracks=True, vias=True, pads=True, copper_pour=False,
            footprints=True,
        ),
        polygons=[ZoneOutlinePolygon(pts=list(pts))],
    )


def pour_zones(board: Board) -> list:
    """Every pour THIS STAGE put on *board*, in the board's own order.

    The name is the handle, and it is a handle on purpose: a pour is filled by
    KiCad in a sandbox, and whoever asks for that fill later - the save, the live
    preview's trailing phase - has a finished panel and no memory of how it was
    made. A rule area is a zone too and is not one of these, so the filter is by
    the prefix this stage owns rather than by "is a zone" - and the keep-out is
    deliberately named so that it does not carry that prefix.
    """
    return [
        zone for zone in board.zones
        if (zone.name or "").startswith(f"{ZONE_NAME_PREFIX}_")
    ]


def fill_placed(board: Board, poured, warnings=None, cli_path=None,
                work_dir=None):
    """Fill the pour :func:`apply` already put on *board*. Returns it, filled.

    **THE FILL IS THE LAST THING A PANEL DOES.** KiCad fills a
    board, not a zone: whatever copper is on the panel when the sandbox is
    written is what the filler holds off. So the pour's OUTLINE is made where
    the carrier chain wants it - before the fiducials, whose positions nothing
    depends on - and the FILL is taken after every stage that can put copper on
    the panel has run. Filling in `apply` instead floods anything added later,
    which is the defect this function exists to retire: the panel's fiducials
    came back with no clearance ring because the pads were not on the board
    KiCad was asked about.

    *poured* is what :func:`apply` returned, and what comes back is that same
    report carrying the fill's own numbers. None, or a report with no pour in
    it, costs no process.
    """
    if poured is None or not poured.count:
        return poured
    result = fill_pours(board, cli_path=cli_path, work_dir=work_dir)
    if warnings is not None and result.zones_empty:
        warnings.append(EMPTY_POUR_WARNING)
    return poured._replace(refill=result)


def fill_pours(board: Board, cli_path=None, work_dir=None):
    """Fill the pours already on *board*, and transplant the answer back.

    For a panel that was built WITHOUT its fill - which is every panel the live
    preview draws, and the cheap build a caller asks for when it wants geometry
    in a second rather than copper in ten. MEASURED on the test boards:
    filling a finished panel gives byte-identical output to filling it inside
    the build, so this is not a second way to make a panel - it is the same
    panel, paid for later.

    A board with no pour on it costs no process and reports zeros.
    """
    kwargs = {} if cli_path is None else {"cli_path": cli_path}
    return refill_module.refill_zones(
        board, pour_zones(board), work_dir=work_dir, **kwargs
    )


def plan(blank, instance_bbox: tuple, instance_boxes, settings) -> list:
    """The pour's rectangles. One: the whole carrier.

    Kept as a function because callers ask "where does the copper go?" without
    wanting a board built. `instance_bbox` and `instance_boxes` do not shape
    the pour - they shape the RULE AREAS (:func:`keepout_plan`) - and are taken
    so the question can still be asked the same way.
    """
    Settings.of(settings)
    rect = carrier_rect(blank)
    if rect.width_mm <= MIN_STRIP_MM or rect.height_mm <= MIN_STRIP_MM:
        return []
    return [rect]


def apply(board: Board, blank, instance_bbox: tuple, settings,
          instance_boxes=(), warnings=None, fill: bool = True,
          cli_path=None, work_dir=None,
          source_outline=(), placements=()) -> RailCopper:
    """Put the rail pour and its rule areas on *board*, in place. Reports both.

    `fill=False` creates the zones and stops, which is what a caller that only
    wants to see where the copper goes should ask for - the fill costs a KiCad
    process and several seconds. The zones are correct either way; they are
    simply empty of copper until something fills them.

    *source_outline* is the SOURCE board's `Edge.Cuts` primitives in its own
    frame and *placements* is where every copy of it sits
    (`step_and_repeat.PanelGeometry`). They are what makes a board-shaped
    keep-out possible; without them the keep-outs are rectangles and a warning
    says so.

    Raises:
        refill.RefillError: when the fill was asked for and could not be done or
            could not be trusted. A panel whose rails are silently bare is worse
            than a run that stops.
    """
    settings = Settings.of(settings)
    warnings = warnings if warnings is not None else []
    layers = copper_layers(board)
    strips = plan(blank, instance_bbox, instance_boxes, settings)

    if not layers:
        warnings.append(
            "the panel declares no copper layer, so there is nothing for the "
            "rail pour to be poured on and no rail copper was made"
        )
        return RailCopper(
            zones=(), layers=(), clearance_mm=settings.clearance_mm,
            pour_between_boards=settings.pour_between_boards,
        )
    if not strips:
        warnings.append(
            "this panel's carrier has no area to pour into, so the rail copper "
            "stage made nothing - check panel.rails"
        )
        return RailCopper(
            zones=(), layers=layers, clearance_mm=settings.clearance_mm,
            pour_between_boards=settings.pour_between_boards,
        )

    # THE RULE AREAS BEFORE THE POUR, so a panel read top to bottom states what
    # is forbidden before it states what is poured. Nothing depends on the order
    # - KiCad reads the whole board before it fills - but a file a person has to
    # read is worth ordering.
    areas = keepout_plan(
        instance_bbox, instance_boxes, settings,
        source_outline=source_outline, placements=placements,
        warnings=warnings,
    )
    board.zones.extend(keepout_for(area, pts, layers) for area, pts in areas)

    zones = [zone_for(strip, layers, settings.clearance_mm) for strip in strips]
    board.zones.extend(zones)

    result = None
    if fill:
        kwargs = {} if cli_path is None else {"cli_path": cli_path}
        result = refill_module.refill_zones(
            board, zones, work_dir=work_dir, **kwargs
        )
        if result.zones_empty:
            warnings.append(EMPTY_POUR_WARNING)

    return RailCopper(
        zones=tuple(strips),
        layers=layers,
        clearance_mm=settings.clearance_mm,
        pour_between_boards=settings.pour_between_boards,
        keepouts=tuple(area for area, _ in areas),
        refill=result,
    )


__all__ = [
    "DEFAULT_MIN_THICKNESS_MM",
    "EMPTY_POUR_WARNING",
    "KEEPOUT_NAME",
    "KEEPOUT_NAMESPACE",
    "MAX_ARC_SWEEP",
    "MIN_STRIP_MM",
    "POUR_NAME",
    "RAIL_NAMESPACE",
    "ZONE_NAME_PREFIX",
    "RailCopper",
    "RailKeepout",
    "RailZone",
    "Settings",
    "apply",
    "carrier_rect",
    "copper_layers",
    "fill_placed",
    "fill_pours",
    "keepout_for",
    "keepout_plan",
    "loop_pts",
    "outset_bbox",
    "outset_outline",
    "placed_pts",
    "plan",
    "pour_zones",
    "rect_pts",
    "zone_for",
]
