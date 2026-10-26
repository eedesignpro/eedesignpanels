"""Tooling holes: the non-plated holes a panel is fixtured by.

A fabricator pins the panel down to route it and an
assembler pins it down to print and place on it, and both do that through
holes that carry no copper and no net. This stage puts one generated footprint
per hole in the RAILS - never in a board - and says where each one sits and how
close it came to something it should not have.

**ON WHEN NOBODY ASKED**, the same as the panel's dimensions. A panel that
cannot be pinned down is a
panel the shop cannot hold, so a config with a `panel` section gets all four
`PNLTH` holes without naming them. The `panel.tooling_holes` section is there to
TUNE them - diameter, which corners, how they are placed - or to decline them
with `"enabled": false`; it is never needed to switch them on.

**A hole is a footprint, not a drawing.** The drill file is generated from
pads, so a hole drawn as a circle on `Edge.Cuts` or on a user layer is a hole
the fabricator has to notice and interpret; a footprint holding one `np_thru_hole`
pad is a hole that reaches the drill file, the position files' exclusion, and
DRC, with nothing for anyone to interpret. The pad is KiCad's own mounting-hole
shape, verified against `MountingHole_3.2mm_M3.kicad_mod` shipped with 10.0.4
and against `parsePAD` in the 10.0.3 source: `np_thru_hole`, `circle`, size
EQUAL to the drill so there is no annular ring, and `(layers "*.Cu" "*.Mask")`.
Measured on a gerber export, that pad puts NOTHING on the copper layers and one
aperture of the hole's own diameter on each mask layer - which is the mask
pulled back off the hole edge, and is what the KiCad library does.

**Where the holes go is derived, never typed.** Two rules, and the config picks
one (`config.ToolingHolesConfig`): the middle of the block of material standing
in a corner of the panel, or a fixed inset from the blank's corner. The first
follows the rails when they change and is the default; the second is what a
fixture with a fixed pin pattern needs.

**A ZERO RAIL MEANS A CORNER WITH NOTHING IN IT, AND THAT CORNER IS DECLINED.**
A zero rail removes that side, so the block of material standing
in either corner along it has no width and its middle is the boards' own edge -
a hole there would go straight through a board's tab region.
There is no sensible other position: sliding the hole along the one rail that
survives would put it somewhere no rule in the config names. So the corner is
refused BY NAME, the run says which rail is missing, and the panel is fixtured
on the corners that have material (:func:`hosts_a_hole`).

**Order is physical, and so are the designators.** Top-left, then clockwise:
`PNLTH1`, `PNLTH2`, `PNLTH3`, `PNLTH4`, numbered over the corners the config
turned on. Reading them off the panel and reading them out of this stage give
the same answer, so a hole can be talked about.

The advisory: a hole whose edge comes
within :data:`CLEARANCE_WARN_MM` of a board or of the blank's own edge is
called out by designator, with the distance measured. It WARNS. A hole a
fraction too close to an edge is a judgement about the material and the
fixture, not an arithmetic error, and the user makes it looking at the
panel. What is an ERROR is a hole whose centre is not on the panel at all: that
is the config asking for a hole in mid-air, and the stage says so with the
coordinates and writes nothing.
"""

import uuid
from typing import NamedTuple

from eedesigncore import (
    Board,
    DrillInfo,
    Footprint,
    FootprintAttrs,
    FootprintPlacement,
    Pad,
    PadShape,
    PadType,
    Property,
)

from eedesignpanels.stages import panel_scores
from eedesignpanels.toolpath import distance_to_primitive

# The four corners, TOP-LEFT FIRST AND THEN CLOCKWISE. This tuple is the order
# the designators are handed out in and the order the report lists them in;
# nothing else decides either.
CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")

DESIGNATOR_PREFIX = "PNLTH"

# The footprint library name and the footprint name the generated holes carry.
# A generated footprint has no library on disk - it exists only in the panel -
# so the nickname says who made it rather than pointing anywhere.
LIBRARY_NICKNAME = "eedesignpanels"

# How close a hole may come to a board edge or the blank's edge before the run
# says so.
CLEARANCE_WARN_MM = 0.5

# The pad's copper and mask layers. KiCad's own NPTH mounting holes use exactly
# this pair, and with size equal to the drill it means "no annular ring, mask
# pulled back off the hole".
PAD_LAYERS = ("*.Cu", "*.Mask")

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so the same panel built twice
# on two machines gives its tooling holes the same identities.
HOLE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/tooling-hole"
)


class Settings(NamedTuple):
    """What a tooling-hole run needs, in millimetres and plain values.

    The stage is pure geometry plus board writing, so it takes plain values and
    not a pydantic model - which is what makes it testable without a config
    file. :meth:`of` accepts either.
    """

    diameter_mm: float = 3.175
    # One boolean per entry of CORNERS, in that order.
    corners: tuple = (True, True, True, True)
    placement: str = "rail_corner_center"
    inset_across_mm: float = 5.0
    inset_down_mm: float = 5.0

    @classmethod
    def of(cls, value) -> "Settings":
        """Settings from a config section, a mapping, or another Settings.

        None is THE DEFAULTS and not "no holes": the stage
        runs when nobody named it, so a caller with no section to hand over is
        asking for the shipped holes. Whether the stage runs at all is the
        config's answer (`config.PanelizeConfig.tooling_hole_settings`).
        """
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, dict):
            corners = value.get("corners", {})
            return cls(
                diameter_mm=float(value.get("diameter_mm", 3.175)),
                corners=_corner_flags(corners),
                placement=str(value.get("placement", "rail_corner_center")),
                inset_across_mm=float(value.get("inset_across_mm", 5.0)),
                inset_down_mm=float(value.get("inset_down_mm", 5.0)),
            )
        return cls(
            diameter_mm=float(value.diameter_mm),
            corners=_corner_flags(value.corners),
            placement=str(value.placement),
            inset_across_mm=float(value.inset_across_mm),
            inset_down_mm=float(value.inset_down_mm),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def enabled_corners(self) -> tuple:
        """The corners that get a hole, top-left first and then clockwise."""
        return tuple(
            name for name, on in zip(CORNERS, self.corners) if on
        )


def _corner_flags(corners) -> tuple:
    """Four booleans in CORNERS order, from whatever shape the caller has."""
    if isinstance(corners, dict):
        return tuple(bool(corners.get(name, True)) for name in CORNERS)
    if isinstance(corners, (tuple, list)):
        if len(corners) != len(CORNERS):
            raise ValueError(
                f"tooling hole corners are {len(CORNERS)} booleans - "
                f"{', '.join(CORNERS)} - and this is {len(corners)}"
            )
        return tuple(bool(flag) for flag in corners)
    return tuple(bool(getattr(corners, name)) for name in CORNERS)


class Hole(NamedTuple):
    """One tooling hole: where it is, what it is called, and what it cleared."""

    designator: str
    corner: str
    at: tuple
    diameter_mm: float
    # Distance from the HOLE'S EDGE to the nearest piece of the panel outline -
    # a board edge or the blank's own edge, whichever is closer. None when the
    # caller handed the stage no outline to measure against.
    clearance_mm: float = None

    @property
    def tight(self) -> bool:
        """Is this hole closer to an edge than the advisory allows?"""
        return self.clearance_mm is not None and self.clearance_mm < CLEARANCE_WARN_MM


class NoMaterial(NamedTuple):
    """A corner the config asked for that has no rail material to host a hole.

    A zero rail REMOVES that side (`channels.Rails`), and a corner with no
    material standing in it cannot carry a hole: the middle of a block of
    nothing is the boards' own edge, and a hole drilled there is drilled through
    somebody's board. So the corner is declined, by name, with the reason.
    """

    corner: str
    reason: str


class ToolingHoles(NamedTuple):
    """What the stage put on the panel, counted and measured."""

    holes: tuple
    diameter_mm: float
    placement: str
    # The corners that were asked for and could not be given a hole, each with
    # the reason. Reported rather than counted, so a run says WHICH corner.
    skipped: tuple = ()

    @property
    def count(self) -> int:
        return len(self.holes)

    @property
    def tight(self) -> tuple:
        """Every hole the advisory named."""
        return tuple(hole for hole in self.holes if hole.tight)


def _coordinate(value: float) -> str:
    """One millimetre coordinate at the file's own precision, sign-normalised."""
    rounded = round(value, 6)
    return f"{rounded + 0.0:.6f}" if rounded else "0.000000"


def corner_block(corner: str, blank, instance_bbox: tuple,
                 score_setbacks=None) -> tuple:
    """The block of RAIL material standing in one corner of the panel.

    `(min_x, min_y, max_x, max_y)` in board millimetres: the rectangle between
    the blank's corner and the rail score line, which is as wide as that side's
    rail and as tall as the other's. **It is the rails said in coordinates** -
    the user typed those two numbers - so a hole placed in the middle of
    this block and a rail width in the config cannot disagree.

    **THE RAIL IS MEASURED FROM THE PANEL EDGE, NOT FROM THE BOARD ARRAY.**
    A rail score setback is plain material added between the boards and that
    rail's score line: the PANEL GROWS by twice it and the rail keeps the width
    that was typed, panel edge to score line. The whole distance from the
    blank's corner to the board grid's is the rail PLUS the setback, and a hole
    centred on that would slide half a setback outward every time the setback
    changed. The array-side edge is therefore pulled back by that side's
    setback, which puts the block exactly on the rail band.

    *score_setbacks* is :class:`panel_scores.Setbacks` or anything it accepts -
    the GOVERNED figures, so a setback whose rail score is off is already zero.
    None is no setback.

    A block of no width or no height is a corner with NOTHING standing in it,
    which is what a zero rail means. Read it with :func:`hosts_a_hole`. That
    now stays true under a setback: a zero rail with a 3 mm setback is still a
    corner with no rail in it, and the 3 mm of plain material beside the boards
    is not something to fixture the panel by.
    """
    left, top, right, bottom = instance_bbox
    back = panel_scores.Setbacks.of(score_setbacks)
    # WHICH SETBACK PULLS WHICH WAY, said plainly because it is easy to swap:
    # the HORIZONTAL setback belongs to the top and bottom rail passes and grows
    # the panel VERTICALLY, so it moves the block's `y` edge; the VERTICAL one
    # grows it ACROSS and moves the `x` edge (`panelize.blank_rails`).
    inner_x, inner_y = back.vertical_mm, back.horizontal_mm
    across = {
        "top_left": (blank.min_x, left - inner_x),
        "bottom_left": (blank.min_x, left - inner_x),
        "top_right": (right + inner_x, blank.max_x),
        "bottom_right": (right + inner_x, blank.max_x),
    }[corner]
    down = {
        "top_left": (blank.min_y, top - inner_y),
        "top_right": (blank.min_y, top - inner_y),
        "bottom_left": (bottom + inner_y, blank.max_y),
        "bottom_right": (bottom + inner_y, blank.max_y),
    }[corner]
    return (across[0], down[0], across[1], down[1])


def hosts_a_hole(corner: str, blank, instance_bbox: tuple,
                 score_setbacks=None) -> str:
    """"" when this corner has material to drill, else why it has none.

    The two sides that meet at a corner are the two rails that make it, so the
    reason names the one that is missing - which is the number the user can
    change.

    *score_setbacks* reaches :func:`corner_block`, so the width this judges is
    the TYPED rail and never the rail plus a setback: the setback is plain
    material beside the boards, not rail to
    fixture the panel by.
    """
    min_x, min_y, max_x, max_y = corner_block(
        corner, blank, instance_bbox, score_setbacks
    )
    across, down = max_x - min_x, max_y - min_y
    vertical, horizontal = corner.split("_")
    missing = []
    if across <= 0.0:
        missing.append(f"the {horizontal} rail is {max(across, 0.0):g} mm wide")
    if down <= 0.0:
        missing.append(f"the {vertical} rail is {max(down, 0.0):g} mm wide")
    if not missing:
        return ""
    return (
        f"no rail material stands in the {corner.replace('_', ' ')} corner: "
        + " and ".join(missing)
    )


def on_rail_material(at: tuple, blank, instance_bbox: tuple) -> bool:
    """Is this point on the carrier - on the blank, and off the board field?

    The board field is the whole grid rectangle, gaps included: a hole down a gap
    between two boards is not a hole in a rail, and the fixture pin that goes
    through it lands where the router is about to cut. Edges count as being ON
    the boards, because a hole centred on a board's edge is half in the board.
    """
    left, top, right, bottom = instance_bbox
    return (
        blank.contains(at)
        and not (left <= at[0] <= right and top <= at[1] <= bottom)
    )


def hole_centre(corner: str, blank, instance_bbox: tuple,
                settings: Settings, score_setbacks=None) -> tuple:
    """Where one corner's hole sits, in board millimetres.

    *blank* is the panel blank (:class:`channels.Rect`); *instance_bbox* is the
    extent of the board grid inside it. In `rail_corner_center` mode the hole
    goes in the middle of the block of material standing in that corner
    (:func:`corner_block`), so a rail that changes width takes its hole with it;
    in `fixed_inset` mode it goes at the config's inset from the blank's corner.

    **EVERY POSITION HERE IS MEASURED FROM THE FINISHED PANEL EDGE.** The blank
    is the panel the fabricator gets,
    setback included, so a `fixed_inset` hole is already at its inset from that
    edge and needs no correction; `rail_corner_center` takes *score_setbacks* so
    its block is the rail band and not the rail plus the setback. The distance
    from a hole to the nearest panel edge therefore does not change when a
    setback does.

    Says where the hole WOULD go, whether or not there is material to drill:
    :func:`plan` is what declines a corner that has none.

    Raises:
        ValueError: for a placement mode this stage does not know. The config
            rejects an unknown mode first; this is the guard for a caller
            passing plain values.
    """
    if settings.placement == "rail_corner_center":
        min_x, min_y, max_x, max_y = corner_block(
            corner, blank, instance_bbox, score_setbacks
        )
        return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    if settings.placement != "fixed_inset":
        raise ValueError(
            f"tooling hole placement {settings.placement!r} is not a rule this "
            f"stage knows: it is 'rail_corner_center' or 'fixed_inset'"
        )
    west = blank.min_x + settings.inset_across_mm
    east = blank.max_x - settings.inset_across_mm
    north = blank.min_y + settings.inset_down_mm
    south = blank.max_y - settings.inset_down_mm
    return {
        "top_left": (west, north),
        "top_right": (east, north),
        "bottom_right": (east, south),
        "bottom_left": (west, south),
    }[corner]


def clearance_of(at: tuple, radius_mm: float, outline) -> float:
    """From the hole's EDGE to the nearest piece of *outline*, in millimetres.

    *outline* is every boundary primitive of the panel - the blank's own edge
    and every routed channel wall, which is where the boards are. One number
    covers both, because a hole does not care which kind of edge it is about to
    break into.

    Negative when the hole overlaps an edge, which is the honest answer and is
    what makes the advisory readable: "-0.8 mm" says the hole is cut open.
    """
    nearest = min(
        (distance_to_primitive(at, piece) for piece in outline),
        default=None,
    )
    return None if nearest is None else nearest - radius_mm


def footprint_for(hole: Hole) -> Footprint:
    """One tooling hole as a board footprint holding a single NPTH pad.

    The uuids are derived from the hole's corner and its position, so the same
    panel built twice names the same hole and moving a hole gives it a new
    identity rather than silently reusing the old one.

    The reference designator is HIDDEN and sits on `F.Fab`: the panel's
    silkscreen is the boards' silkscreen, and a fabrication artifact's own
    label is not something an assembler should find printed on the product.
    """
    key = f"{hole.corner}|{_coordinate(hole.at[0])}|{_coordinate(hole.at[1])}"
    diameter = hole.diameter_mm
    return Footprint(
        fp_id=f"{LIBRARY_NICKNAME}:PanelToolingHole_{diameter:g}mm",
        layer="F.Cu",
        uuid=str(uuid.uuid5(HOLE_NAMESPACE, key)),
        at=FootprintPlacement(position=hole.at, angle=0.0),
        description=(
            f"Panel tooling hole, {diameter:g} mm non-plated. Generated by "
            f"eedesignpanels; it exists in this panel only."
        ),
        keywords="panel tooling hole npth",
        attributes=FootprintAttrs(
            through_hole=False,
            board_only=True,
            exclude_from_pos_files=True,
            exclude_from_bom=True,
            allow_missing_courtyard=True,
        ),
        properties=[
            Property(
                name="Reference", value=hole.designator,
                layer="F.Fab", visible=False,
                id=str(uuid.uuid5(HOLE_NAMESPACE, f"{key}|reference")),
            ),
            Property(
                name="Value", value=f"PanelToolingHole_{diameter:g}mm",
                layer="F.Fab", visible=False,
                id=str(uuid.uuid5(HOLE_NAMESPACE, f"{key}|value")),
            ),
        ],
        pads=[Pad(
            number="",
            pad_type=PadType.np_thru_hole,
            shape=PadShape.circle,
            position=(0.0, 0.0),
            size=(diameter, diameter),
            drill=DrillInfo(width=diameter),
            layers=list(PAD_LAYERS),
            id=str(uuid.uuid5(HOLE_NAMESPACE, f"{key}|pad")),
        )],
    )


def plan(blank, instance_bbox: tuple, settings, outline=(),
         score_setbacks=None) -> ToolingHoles:
    """Where every hole goes, what it is called, and how close it came.

    Reads nothing and writes nothing - :func:`apply` is what puts the holes on a
    board. Separate because the panel page draws this before anything is built.

    **A CORNER WITH NO RAIL MATERIAL IS DECLINED.** A hole is drilled through
    the carrier, so a corner that
    has no carrier standing in it gets none: the designators are handed out over
    the corners that DO, top-left first and then clockwise, and every corner
    that was asked for and refused comes back in `skipped` with its reason. In
    `fixed_inset` mode the same rule reads the typed position instead of the
    rails: an inset that lands the hole on the board field is refused, because
    that is a pin through somebody's board.

    **THE SETBACK MOVES THE PANEL EDGE, NOT THE HOLE.** *score_setbacks* is
    what the rail scores grew the blank
    by, and it is here so a `rail_corner_center` hole stays in the middle of the
    rail band measured from the finished panel edge. Without it the hole would
    track the board array while the panel grew round it.

    Nothing here judges how much material there is - only whether there is any.
    A hole in a rail narrower than the hole is placed and the advisory says by
    how much it is cut open, which is the judgement about the material and the
    fixture that the user makes looking at the panel.

    Raises:
        ValueError: when a hole's centre is not on the blank. A hole in mid-air
            is the config asking for something that is not a panel, and the
            message carries the corner and the coordinates.
    """
    settings = Settings.of(settings)
    outline = tuple(outline)
    holes, skipped = [], []
    for corner in settings.enabled_corners:
        at = hole_centre(
            corner, blank, instance_bbox, settings, score_setbacks
        )
        # THE CORNER'S OWN MATERIAL decides first, and only where the position
        # is derived from it: a typed inset is judged by where it lands.
        reason = (
            hosts_a_hole(corner, blank, instance_bbox, score_setbacks)
            if settings.placement == "rail_corner_center" else ""
        )
        # OFF THE PANEL IS STILL AN ERROR: a position that is not on the blank
        # at all is not a corner short of material, it is a config describing
        # something that is not a panel.
        if not reason and not blank.contains(at):
            raise ValueError(
                f"the {corner.replace('_', ' ')} tooling hole would be centred "
                f"at ({at[0]:.4f}, {at[1]:.4f}) mm, which is off the panel "
                f"blank (x {blank.min_x:.4f}..{blank.max_x:.4f}, y "
                f"{blank.min_y:.4f}..{blank.max_y:.4f} mm)"
            )
        if not reason and not on_rail_material(at, blank, instance_bbox):
            reason = (
                f"the {corner.replace('_', ' ')} tooling hole would sit at "
                f"({at[0]:.4f}, {at[1]:.4f}) mm, which is on the boards rather "
                f"than on rail material"
            )
        if reason:
            skipped.append(NoMaterial(corner=corner, reason=reason))
            continue
        holes.append(Hole(
            designator=f"{DESIGNATOR_PREFIX}{len(holes) + 1}",
            corner=corner,
            at=at,
            diameter_mm=settings.diameter_mm,
            clearance_mm=clearance_of(at, settings.radius_mm, outline),
        ))
    return ToolingHoles(
        holes=tuple(holes),
        diameter_mm=settings.diameter_mm,
        placement=settings.placement,
        skipped=tuple(skipped),
    )


def apply(board: Board, blank, instance_bbox: tuple, settings,
          outline=(), warnings=None, score_setbacks=None) -> ToolingHoles:
    """Put the tooling holes on *board*, in place. Returns what it put there.

    Footprints are appended in designator order, so the file reads the way the
    panel does. Anything the advisory catches goes into *warnings* by
    designator, with the measured distance, and the holes are placed anyway
    - a check reports, it does not block.

    A corner that was declined for want of material is named in *warnings* too,
    with the rail that is missing: a panel fixtured on three pins instead of
    four is a thing the user has to be told, and never a thing to guess at.
    """
    made = plan(blank, instance_bbox, settings, outline, score_setbacks)
    for hole in made.holes:
        board.footprints.append(footprint_for(hole))
    if warnings is not None:
        for corner in made.skipped:
            warnings.append(
                f"{corner.reason}, so no tooling hole was put there - this "
                f"panel is fixtured on {made.count} hole(s) instead of "
                f"{made.count + len(made.skipped)}"
            )
        for hole in made.tight:
            warnings.append(
                f"tooling hole {hole.designator} at ({hole.at[0]:.4f}, "
                f"{hole.at[1]:.4f}) mm clears the nearest board or blank edge "
                f"by {hole.clearance_mm:.4f} mm, under the {CLEARANCE_WARN_MM:g} "
                f"mm this panel is checked against - move it, or widen the rail "
                f"it sits in"
            )
    return made


__all__ = [
    "CLEARANCE_WARN_MM",
    "CORNERS",
    "DESIGNATOR_PREFIX",
    "HOLE_NAMESPACE",
    "LIBRARY_NICKNAME",
    "PAD_LAYERS",
    "Hole",
    "NoMaterial",
    "Settings",
    "ToolingHoles",
    "apply",
    "clearance_of",
    "corner_block",
    "footprint_for",
    "hole_centre",
    "hosts_a_hole",
    "on_rail_material",
    "plan",
]
