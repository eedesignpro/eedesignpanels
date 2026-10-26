"""Panel fiducials: the optical targets a placement machine finds the panel by.

A pick-and-place machine does not trust the panel's edge; it
photographs two or three copper dots and computes where everything else is from
them. This stage puts those dots on the RAILS - never on a board - and says
where each one went.

**A FIDUCIAL IS A REAL FOOTPRINT FROM A REAL LIBRARY.** Not a drawn circle and
not a footprint this stage invents: the
engineer picks one of the library's own fiducial footprints and the panel gets a
placed instance of exactly that definition, so it reaches the position files,
the paste layer and the fabrication drawing the way every other footprint does,
and DRC checks its placement like every other footprint. Which footprints are
offered is the LIBRARY's answer, read through KiCad's own settings
(:mod:`eedesignpanels.fplib`).

**The library's own attributes are left alone.** The definition arrives from the
`.kicad_mod` file with whatever `(attr ...)` the library author wrote - for
example `smd exclude_from_bom` - and this stage does not touch it. A
fiducial the assembler cannot see in the position file is a fiducial that does
not do its job, which is the opposite of a tooling hole
(:mod:`eedesignpanels.stages.tooling_holes` excludes its holes from everything
deliberately).

**THE TOOLING HOLES ARE THE DATUM**: each target is an offset from a tooling
hole. Not the blank's corner and not a typed coordinate: the holes are what
the fixture locates the panel by, so the optical targets are placed a stated
distance from them and move with them. A corner whose hole was declined - a
zero rail, a hole that would have landed on a board - gets no fiducial either,
by name: there is nothing there to measure from.

The offset always runs INWARD, towards the middle of the panel. Outward from a
corner hole is off the panel, and a signed offset would be a second way of
saying the same thing with one wrong answer in it.

**Three corners is the orientation pattern.** Four fiducials in a rectangle look
the same rotated 180 degrees; three do not, which is how the machine knows the
panel is not in backwards. The config says which corners, so the user can
ask for the pattern by unticking one box.

**A BACK-SIDE FIDUCIAL IS THE LIBRARY'S OWN FLIP.** Turning a placed footprint
over is `FOOTPRINT::Flip`
(`pcbnew/footprint.cpp:2931-3000`): the position mirrors about the centre, the
layer flips through the BOARD's own opposite-layer table, the orientation is
negated and normalised, every field, pad, zone, graphic and point flips itself
in the footprint-local frame, and a `left_right` flip then turns the whole
footprint a half circle. `eedesigncore.pcb.transform.flip_footprint` is that
operation, measured against four footprints flipped by hand in KiCad 10.0.4;
nothing here reimplements any part of it, because the board library owns
the geometry.

The centre is the footprint's OWN placement, which is what KiCad uses for a
single selected item (`edit_tool.cpp:2843-2849`), so a back-side fiducial sits
at the same panel coordinate its front-side twin would - which is what a
placement machine reading the panel from either side expects. The direction is
`left_right`, KiCad's own shipped default (`pcbnew_settings.cpp:151-153`), so a
panel fiducial is the footprint an engineer would get by selecting the front-side
one and pressing F.
"""

import uuid
from typing import NamedTuple, Union

from eedesigncore import Board, Footprint, inject_footprint
from eedesigncore.pcb.transform import flip_footprint

from eedesignpanels import fplib
from eedesignpanels.stages.tooling_holes import CORNERS, on_rail_material

DESIGNATOR_PREFIX = "PNLFD"

# The two ways the offset may run, and they are the two rail pairs. "Along the
# top and bottom rails" moves the fiducial sideways out of the corner, which is
# the direction that keeps it in a horizontal rail; the other moves it down or
# up, which keeps it in a vertical one.
DIRECTIONS = ("along_top_and_bottom_rails", "along_left_and_right_rails")

# WHICH COPPER LAYER CARRIES THE TARGETS, IN KICAD'S OWN NAMES: `F.Cu`, `B.Cu`
# or `both`, the same strings the board file carries, and the GUI shows those
# strings as its labels. So the config, the page and the board say the same
# thing about one layer.
SIDES = ("F.Cu", "B.Cu", "both")

# The layers one setting asks for, in the order they are placed.
SIDES_OF = {"F.Cu": ("F.Cu",), "B.Cu": ("B.Cu",), "both": ("F.Cu", "B.Cu")}

# KiCad's own shipped flip direction, and the one this stage turns a back-side
# fiducial over with. `pcbnew.json` holds `editing.flip_left_right: false`, and
# that value IS `LEFT_RIGHT`: the enum orders it first (`include/core/mirror.h`)
# and the setting binds a bool straight onto it, defaulted to
# `static_cast<bool>(FLIP_DIRECTION::LEFT_RIGHT)` (`pcbnew_settings.cpp:151`).
FLIP_DIRECTION = "left_right"

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so the same panel built twice
# on two machines gives its fiducials the same identities.
FIDUCIAL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-fiducial"
)

# The child collections a placed footprint has identities for, in the order the
# file writes them. Named rather than discovered: an item this stage does not
# name would carry the random uuid `inject_footprint` minted, and one random
# value is enough to break "same input, same output".
CHILD_LISTS = ("properties", "drawings", "points", "pads", "zones", "groups")


class Settings(NamedTuple):
    """What a fiducial run needs, in millimetres and plain values.

    The stage takes plain values rather than a pydantic model, which is what
    makes it testable without a config file. :meth:`of` accepts either.

    `footprint` and `library` name the DEFINITION; resolving the library to a
    directory is :mod:`eedesignpanels.fplib`'s job and happens before the stage
    runs. `library_path` pins the directory for a machine with no KiCad
    settings.
    """

    footprint: str = ""
    library: str = fplib.DEFAULT_FIDUCIAL_LIBRARY
    library_path: Union[str, None] = None
    offset_mm: float = 5.0
    direction: str = DIRECTIONS[0]
    side: str = "F.Cu"
    # One boolean per entry of CORNERS, in that order.
    corners: tuple = (True, True, True, False)

    @classmethod
    def of(cls, value) -> "Settings":
        """Settings from a config section, a mapping, or another Settings.

        None is an ERROR here and not the defaults, unlike the tooling holes:
        this stage is opt-in, so "no section" is answered by not calling it.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                footprint=str(value.get("footprint", "")),
                library=str(value.get("library",
                                      fplib.DEFAULT_FIDUCIAL_LIBRARY)),
                library_path=value.get("library_path"),
                offset_mm=float(value.get("offset_mm", 5.0)),
                direction=str(value.get("direction", DIRECTIONS[0])),
                side=str(value.get("side", "F.Cu")),
                corners=_corner_flags(value.get("corners", {})),
            )
        if value is None:
            raise ValueError(
                "panel fiducials are opt-in, so there are no default settings "
                "to fall back on - a caller with no section to hand over is a "
                "caller that should not be running this stage"
            )
        return cls(
            footprint=str(value.footprint),
            library=str(value.library),
            library_path=(
                None if value.library_path is None else str(value.library_path)
            ),
            offset_mm=float(value.offset_mm),
            direction=str(value.direction),
            side=str(value.side),
            corners=_corner_flags(value.corners),
        )

    @property
    def enabled_corners(self) -> tuple:
        """The corners that get a fiducial, top-left first and then clockwise."""
        return tuple(
            name for name, on in zip(CORNERS, self.corners) if on
        )

    @property
    def sides(self) -> tuple:
        """The board sides that carry targets, in the order they are placed."""
        if self.side not in SIDES_OF:
            raise ValueError(
                f"fiducial copper layer {self.side!r} is not one this stage "
                f"knows: it is {', '.join(SIDES)}"
            )
        return SIDES_OF[self.side]

    @property
    def orientation_pattern(self) -> bool:
        """Do these corners break the panel's 180-degree symmetry?

        Three of four corners do; four do not, and so do not tell a machine
        which way round the panel is. Reported, never enforced: a panel with
        four fiducials and a keyed fixture is a legitimate thing to ask for.
        """
        return len(self.enabled_corners) == 3


def _corner_flags(corners) -> tuple:
    """Four booleans in CORNERS order, from whatever shape the caller has."""
    if isinstance(corners, dict):
        return tuple(
            bool(corners.get(name, name != "bottom_left")) for name in CORNERS
        )
    if isinstance(corners, (tuple, list)):
        if len(corners) != len(CORNERS):
            raise ValueError(
                f"fiducial corners are {len(CORNERS)} booleans - "
                f"{', '.join(CORNERS)} - and this is {len(corners)}"
            )
        return tuple(bool(flag) for flag in corners)
    return tuple(bool(getattr(corners, name)) for name in CORNERS)


class Fiducial(NamedTuple):
    """One placed fiducial: what it is called, where it is, and on which side."""

    designator: str
    corner: str
    side: str
    at: tuple
    footprint: str
    # The tooling hole it was measured from, so a report can say "5 mm from
    # PNLTH1" rather than only where it ended up.
    datum: str
    datum_at: tuple


class Declined(NamedTuple):
    """A corner or a side that was asked for and could not be given a fiducial."""

    corner: str
    side: str
    reason: str


class Fiducials(NamedTuple):
    """What the stage put on the panel, counted and named."""

    placed: tuple
    footprint: str
    library: str
    offset_mm: float
    direction: str
    side: str
    declined: tuple = ()

    @property
    def count(self) -> int:
        return len(self.placed)

    @property
    def orientation_pattern(self) -> bool:
        """Do the placed targets break the panel's 180-degree symmetry?

        Judged on what was PLACED and not on what was asked for: a fourth
        corner the rails could not host turns a four-fiducial request into the
        orientation pattern, and the report should say so.
        """
        return len({f.corner for f in self.placed}) == 3


def offset_vector(corner: str, direction: str, offset_mm: float) -> tuple:
    """Which way and how far the fiducial sits from its hole, in millimetres.

    INWARD, always: `+x` out of a left corner, `-x` out of a right one, and the
    same in `y` for the other direction. Outward from a corner hole is off the
    panel, so a sign here would only ever be a way of asking for that.

    Raises:
        ValueError: for a direction this stage does not know, or a corner that
            is not one of the four. The config refuses both first; this is the
            guard for a caller passing plain values.
    """
    if corner not in CORNERS:
        raise ValueError(
            f"fiducial corner {corner!r} is not one of {', '.join(CORNERS)}"
        )
    vertical, horizontal = corner.split("_")
    if direction == "along_top_and_bottom_rails":
        return (offset_mm if horizontal == "left" else -offset_mm, 0.0)
    if direction == "along_left_and_right_rails":
        return (0.0, offset_mm if vertical == "top" else -offset_mm)
    raise ValueError(
        f"fiducial direction {direction!r} is not one this stage knows: it is "
        f"{' or '.join(DIRECTIONS)}"
    )


def plan(holes, blank, instance_bbox: tuple, settings) -> Fiducials:
    """Where every fiducial goes and what it is called. Reads and writes nothing.

    *holes* is the :class:`eedesignpanels.stages.tooling_holes.ToolingHoles` the
    holes stage produced - the datum. Separate from :func:`apply` because the
    panel page draws these positions before anything is built, and because the
    arithmetic is worth testing without a footprint library in reach.

    A corner is DECLINED, by name and with the reason, when the holes stage did
    not put a hole there, or when the offset would carry the fiducial onto the
    boards rather than along the rail. Both are the config asking for something
    the panel cannot hold, and neither is worth failing a build over: the panel
    is placed on the targets it has and the run says how many that is.

    Raises:
        ValueError: when a fiducial would land off the blank altogether - the
            config describing something that is not a panel - or when the
            settings name a side or a direction this stage does not know.
    """
    settings = Settings.of(settings)
    sides = settings.sides
    by_corner = {hole.corner: hole for hole in (holes.holes if holes else ())}
    placed, declined = [], []
    for side in sides:
        for corner in settings.enabled_corners:
            hole = by_corner.get(corner)
            if hole is None:
                declined.append(Declined(
                    corner=corner, side=side,
                    reason=(
                        f"the {corner.replace('_', ' ')} corner has no tooling "
                        f"hole to measure from, so there is no datum for a "
                        f"fiducial there"
                    ),
                ))
                continue
            dx, dy = offset_vector(corner, settings.direction,
                                   settings.offset_mm)
            at = (hole.at[0] + dx, hole.at[1] + dy)
            if not blank.contains(at):
                raise ValueError(
                    f"the {corner.replace('_', ' ')} fiducial would sit at "
                    f"({at[0]:.4f}, {at[1]:.4f}) mm, {settings.offset_mm:g} mm "
                    f"from tooling hole {hole.designator} - which is off the "
                    f"panel blank (x {blank.min_x:.4f}..{blank.max_x:.4f}, y "
                    f"{blank.min_y:.4f}..{blank.max_y:.4f} mm)"
                )
            if not on_rail_material(at, blank, instance_bbox):
                declined.append(Declined(
                    corner=corner, side=side,
                    reason=(
                        f"the {corner.replace('_', ' ')} fiducial would sit at "
                        f"({at[0]:.4f}, {at[1]:.4f}) mm, {settings.offset_mm:g}"
                        f" mm from tooling hole {hole.designator} - which is on "
                        f"the boards rather than on rail material. Shorten the "
                        f"offset, or run it along the other pair of rails"
                    ),
                ))
                continue
            placed.append(Fiducial(
                designator=f"{DESIGNATOR_PREFIX}{len(placed) + 1}",
                corner=corner,
                side=side,
                at=at,
                footprint=settings.footprint,
                datum=hole.designator,
                datum_at=hole.at,
            ))
    return Fiducials(
        placed=tuple(placed),
        footprint=settings.footprint,
        library=settings.library,
        offset_mm=settings.offset_mm,
        direction=settings.direction,
        side=settings.side,
        declined=tuple(declined),
    )


def _identify(placed: Footprint, key: str) -> None:
    """Give *placed* and every child it owns a uuid derived from *key*.

    `inject_footprint` mints a fresh random uuid for the footprint and for each
    child, which is KiCad's own fresh-add behaviour and is exactly wrong for a
    generated panel: two runs over the same config would differ in every
    identity. So the identities are replaced with `uuid5` values
    keyed by the fiducial's corner and side - the same grid-cell-style key the
    step-and-repeat uses - and the position never enters the key, so
    moving the offset renames nothing.

    The child lists are named in :data:`CHILD_LISTS` rather than discovered, so
    a footprint holding an item kind this stage has never seen fails the
    determinism fence loudly instead of shipping one random uuid.
    """
    placed.uuid = str(uuid.uuid5(FIDUCIAL_NAMESPACE, key))
    for field in CHILD_LISTS:
        for index, item in enumerate(getattr(placed, field, ()) or ()):
            if "id" not in type(item).model_fields:
                continue
            item.id = str(
                uuid.uuid5(FIDUCIAL_NAMESPACE, f"{key}|{field}|{index}")
            )


def place(board: Board, definition: Footprint, fiducial: Fiducial,
          library: str) -> Footprint:
    """Put one fiducial on *board* as a placed instance of *definition*.

    `inject_footprint` is the library's own `.kicad_mod`-to-board transform, so
    the result is the footprint form KiCad writes for a footprint an engineer
    dropped on the panel - library nickname prepended to the FPID, the
    reference set, the placement at the fiducial's own position. Nothing about
    the definition's layers, attributes or geometry is edited here.

    A BACK-SIDE fiducial is that same placed footprint turned over by
    `flip_footprint`, KiCad's `FOOTPRINT::Flip`. The centre is its own
    placement, so the coordinate does not move; the layer, the pads, the fields
    and the graphics all cross to the other side, and the definition's own
    front-side library file is never touched.
    """
    placed = inject_footprint(
        board, definition,
        at=fiducial.at,
        reference=fiducial.designator,
        lib_nickname=library,
    )
    if fiducial.side == "B.Cu":
        flip_footprint(placed, direction=FLIP_DIRECTION, board=board)
    # LAST, so the identities are a function of the config and not of anything
    # the flip happened to touch.
    _identify(placed, f"{fiducial.corner}|{fiducial.side}")
    return placed


def apply(board: Board, blank, instance_bbox: tuple, settings, holes,
          warnings=None) -> Fiducials:
    """Put the panel fiducials on *board*, in place. Returns what it put there.

    The definition is read ONCE, from the library KiCad's settings resolve
    (:mod:`eedesignpanels.fplib`), and placed at each position :func:`plan`
    found. Footprints are appended in designator order, so the file reads the
    way the panel does.

    Every corner or side that was asked for and refused goes into *warnings* by
    name: a panel placed on two targets instead of three is a thing the
    user has to be told and never a thing to guess at.

    Raises:
        ValueError: for a side or direction this stage does not know, or for a
            fiducial off the blank.
        fplib.LibraryError: when the library did not resolve, when the config
            names no footprint, or when the library holds no footprint of that
            name. The stage writes NOTHING in that case - a panel missing its
            optical targets silently is a panel the assembler cannot place.
    """
    settings = Settings.of(settings)
    if not settings.footprint:
        raise fplib.LibraryError(
            f"panel fiducials are switched on and no footprint is named - set "
            f"panel.fiducials.footprint to one of {settings.library}'s "
            f"fiducial footprints"
        )
    made = plan(holes, blank, instance_bbox, settings)
    if made.placed:
        resolution = fplib.resolve_cached(settings.library,
                                          settings.library_path)
        definition = fplib.load(resolution, settings.footprint)
        for fiducial in made.placed:
            place(board, definition, fiducial, resolution.library)
    if warnings is not None:
        for refused in made.declined:
            warnings.append(
                f"{refused.reason}, so no fiducial was put there - this panel "
                f"is placed on {made.count} optical target(s) instead of "
                f"{made.count + len(made.declined)}"
            )
        if not made.placed:
            warnings.append(
                "panel fiducials are switched on and not one could be placed - "
                "every corner asked for was refused, so the panel carries no "
                "optical targets at all"
            )
    return made


__all__ = [
    "CHILD_LISTS",
    "DESIGNATOR_PREFIX",
    "DIRECTIONS",
    "FIDUCIAL_NAMESPACE",
    "FLIP_DIRECTION",
    "SIDES",
    "SIDES_OF",
    "Declined",
    "Fiducial",
    "Fiducials",
    "Settings",
    "apply",
    "offset_vector",
    "place",
    "plan",
]
