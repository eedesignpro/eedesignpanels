"""Native panel dimensions arranged around four corners.

Board and overall dimensions use the top and right edges. Rail dimensions use
the bottom-right corner. One tooling-hole leader and its corner offsets use
the bottom-left corner. Unique board gaps use the top and left edges.
"""

import math
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from eedesigncore import (
    ArrowDirection,
    Board,
    Dimension,
    DimensionFormat,
    DimensionStyle,
    DimensionText,
    DimensionType,
    TextAttributes,
    PcbCircle,
    PcbGroup,
)

# The layer the dimensions land on. `User.16` is the
# CANONICAL name; a board may give it the purpose-named display name the
# Appearance panel shows, as the corpus board's own entry does:
# `(69 "User.16" user "User.16-Dimensions")`. A purpose-named layer beats
# `Dwgs.User`, which is where a design's own drawing frame lives - putting the
# panel's dimensions there would mix them with the artwork.
#
# A board that does not DECLARE the layer still gets its dimensions, and
# :func:`apply` warns by name: KiCad parses an item on an undeclared user layer
# without complaint but never enables the layer, so nobody sees it (see the
# module docstring). Config key `panel.dimensions.layer` moves them.
DIMENSION_LAYER = "User.16"

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so the same panel built twice
# on two machines gives its dimensions the same identities.
DIMENSION_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-dimension"
)

# KiCad's `DIM_UNITS_MODE::MM` and `DIM_UNITS_FORMAT::BARE_SUFFIX`
# (pcb_dimension.h:39-77). Millimetres are DECLARED rather than left on
# `AUTOMATIC`, so what the panel says does not depend on the reader's unit
# preference.
UNITS_MM = 2
UNITS_FORMAT_BARE_SUFFIX = 1

# `DIM_TEXT_POSITION::INLINE` - the value sits on the crossbar, knocked out of
# it, which is how KiCad's own dimensions read.
TEXT_POSITION_INLINE = 1

# `DIM_TEXT_POSITION::OUTSIDE` - the value sits BESIDE the crossbar, on the side
# KiCad's own quarter turn picks (`pcb_dimension.h:59-66`; the file stores the
# ordinal, and OUTSIDE is 0). NOT WHAT THIS STAGE WRITES ANY MORE, and the
# reason is in the module docstring: on a vertical dimension that side is `-x`,
# which on the right-hand row is the panel side. Kept as the name of the mode
# the port :func:`outside_placement` reproduces.
TEXT_POSITION_OUTSIDE = 0

# `DIM_TEXT_POSITION::MANUAL` - the stored text position STANDS
# (`pcb_dimension.h:59-66`, ordinal 2; `PCB_DIM_ALIGNED::updateText`,
# `pcb_dimension.cpp:1026-1050`, moves the text under OUTSIDE and INLINE and
# leaves it alone under MANUAL). The side has to be OURS, away
# from the panel on both rows, and this is the only mode whose position survives
# the `Update()` the parser ends on (`pcb_io_kicad_sexpr_parser.cpp:4924`).
# KiCad's parser accepts the ordinal: `text_position_mode` is clamped to 0..3
# (`pcb_io_kicad_sexpr_parser.cpp:4738-4745`).
TEXT_POSITION_MANUAL = 2

# The angle KiCad's arrowheads are drawn at (pcb_dimension.cpp:54). Here for one
# reason: `extension_height` is derived from it.
ARROW_ANGLE_DEG = 27.5

# The widest a glyph's pen may be, as a fraction of the text size
# (`ClampTextPenSize`, `common/gr_text.cpp:73-96`, non-strict). Here because the
# OUTSIDE outset is KiCad's EFFECTIVE pen width plus the text height, and the
# effective width is the stated one clamped by this: a thickness the user
# typed past the clamp would move the text further than KiCad moves it.
MAX_PEN_RATIO = 0.25

# THE LETTERING RULE: the text height is the panel's longest
# extent over 50, rounded to the nearest 0.5 mm and never under 2 mm. One
# divisor, because a panel is read at panel scale: the number holds the lettering
# at a constant 2% of the picture, so a 47.5 mm panel gets 2 mm text and a
# 400 mm one - the stated envelope maximum - gets 8 mm.
TEXT_HEIGHT_DIVISOR = 50.0
TEXT_HEIGHT_STEP_MM = 0.5
TEXT_HEIGHT_MIN_MM = 2.0

# How far out the FIRST row sits, as a multiple of the text height. The offsets
# scale with the lettering or they stop being offsets: measured on a 400 mm panel
# with 8 mm text and a fixed 5 mm pitch, the overall extents printed THROUGH the
# hole spacing. 2.5 gives the 5 mm this stage always used at the 2 mm floor.
ROW_OFFSET_RATIO = 2.5

# The step from one row to the next, as a multiple of the text height. BIGGER
# THAN THE FIRST OFFSET, ON PURPOSE, so the single-board and panel dimensions
# stand clearly apart. A multiple rather than a millimetre figure, so it still
# scales when the lettering does. Measured at 2.0 mm lettering on three test
# boards: at a ratio of 2.5 the board value's ink stopped 0.40 mm short of the
# panel value's on the tightest of them, and at 3.5 - 7.0 mm between crossbars
# - it stops 2.40 mm short.
ROW_PITCH_RATIO = 3.5

# The stroke a glyph is drawn with, as a fraction of its height. KiCad's own
# ratio for a user layer - `DEFAULT_TEXT_WIDTH` over `DEFAULT_TEXT_SIZE`,
# 0.15 mm on 1.0 mm text (`include/board_design_settings.h:42-50`) - so the
# lettering keeps its weight as it scales instead of thinning into a hairline.
# At the 2 mm floor it gives 0.3 mm, which is what this stage always wrote.
TEXT_STROKE_RATIO = 0.15

# The highest fixed-decimal precision this stage writes: `DIM_PRECISION::X_XXXXX`
# (pcb_dimension.h:46-58). Values 6 and up are the value-DEPENDENT family
# (`V_VV`...), where the decimals shown change with the number; a fabrication
# drawing that changes its own precision is refused rather than written.
MAX_FIXED_PRECISION = 5

_NM_PER_MM = 1_000_000


class Settings(NamedTuple):
    """What a dimensioning run needs, in millimetres and plain values.

    Plain values rather than a pydantic model, so the stage is testable without
    a config file; :meth:`of` accepts either.

    `first_offset_mm` is how far the INNERMOST crossbar sits outside the blank's
    edge, and `row_pitch_mm` is the gap to the next one out. Two numbers cover
    any number of rows, and no dimension needs its own offset typed. Both are
    None by default, meaning a multiple of the text height -
    :data:`ROW_OFFSET_RATIO` for the first offset and the wider
    :data:`ROW_PITCH_RATIO` for the step between rows. They have to scale with
    the lettering, or an 8 mm number is printed through the row outside it.

    `text_height_mm` is None by default, which means DERIVED FROM THE PANEL
    (:func:`text_height_for`): the lettering is the longest extent over 50,
    rounded to the nearest 0.5 mm and floored at 2 mm, so one number reads the
    same on a 47.5 mm panel and on a 400 mm one. A height in the config wins and
    turns the scaling off - the user asked for that size. `text_thickness_mm`
    follows the height at KiCad's own 15% stroke ratio unless it too is stated.

    The text is LARGER than KiCad's stock 1.0 mm for a user layer, because a
    panel is read at panel scale: 1 mm lettering on a 180 mm panel is about
    seven pixels in a full-panel render. `line_thickness_mm` and
    `arrow_length_mm` stay at KiCad's own figures at every panel size, so the
    panel's dimensions are drawn with the same pen as its boards'.
    """

    layer: str = DIMENSION_LAYER
    # None on all four means "derive it from the panel". See `resolved`.
    first_offset_mm: float = None
    row_pitch_mm: float = None
    text_height_mm: float = None
    text_thickness_mm: float = None
    line_thickness_mm: float = 0.1
    arrow_length_mm: float = 1.27
    extension_offset_mm: float = 0.5
    # DIM_PRECISION::X_XXX - a micrometre of resolution, with the trailing
    # zeroes suppressed so an exact 180 mm panel reads "180 mm" and not
    # "180.000 mm".
    precision: int = 3
    suppress_zeroes: bool = True

    @classmethod
    def of(cls, value) -> "Settings":
        """Settings from a config section, a mapping, or another Settings."""
        if isinstance(value, cls):
            return checked(value)
        if value is None:
            return checked(cls())
        if isinstance(value, dict):
            return checked(cls(**{
                field: value[field] for field in cls._fields if field in value
            }))
        return checked(cls(**{
            field: getattr(value, field)
            for field in cls._fields
            if hasattr(value, field)
        }))

    def resolved(self, longest_extent_mm: float = None) -> "Settings":
        """These settings with the text sized for a panel this big.

        Idempotent: settings that already carry a height come back unchanged, so
        a caller may resolve as often as it likes. With no extent to measure -
        one dimension asked for on its own, no panel in sight - the floor is
        used, because guessing a scale from nothing is worse than the minimum.
        """
        height = self.text_height_mm
        if height is None:
            height = (
                TEXT_HEIGHT_MIN_MM if longest_extent_mm is None
                else text_height_for(longest_extent_mm)
            )
        height = quantized(height)
        thickness = self.text_thickness_mm
        if thickness is None:
            thickness = quantized(height * TEXT_STROKE_RATIO)
        first = quantized(height * ROW_OFFSET_RATIO)
        # THE PITCH IS THE WIDER OF THE TWO: the first offset is
        # clear space over bare material, while the pitch has to hold one row's
        # outset value AND the next row's inline one.
        pitch = quantized(height * ROW_PITCH_RATIO)
        return self._replace(
            text_height_mm=height,
            text_thickness_mm=thickness,
            first_offset_mm=(first if self.first_offset_mm is None
                             else self.first_offset_mm),
            row_pitch_mm=(pitch if self.row_pitch_mm is None
                          else self.row_pitch_mm),
        )


def text_height_for(longest_extent_mm: float) -> float:
    """The lettering height a panel this long gets, in millimetres.

    The panel's longest extent over :data:`TEXT_HEIGHT_DIVISOR`, snapped to the
    nearest :data:`TEXT_HEIGHT_STEP_MM` and never below
    :data:`TEXT_HEIGHT_MIN_MM`. Decimal rather than float arithmetic, so the
    step a value lands on is the same one on every machine and a panel cannot
    change its lettering between two builds.
    """
    if not longest_extent_mm > 0.0:
        raise ValueError(
            f"a panel's longest extent is a positive length, and this is "
            f"{longest_extent_mm!r} - there is nothing to size lettering "
            f"against"
        )
    step = Decimal(str(TEXT_HEIGHT_STEP_MM))
    raw = Decimal(repr(float(longest_extent_mm))) / Decimal(
        str(TEXT_HEIGHT_DIVISOR)
    )
    stepped = (raw / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
    return max(TEXT_HEIGHT_MIN_MM, float(stepped))


def longest_extent_of(blank) -> float:
    """The panel's longest side in millimetres - what the lettering scales to."""
    return max(blank.width_mm, blank.height_mm)


def checked(settings: Settings) -> Settings:
    """*settings*, or a ValueError naming the field that cannot be honoured.

    Raises:
        ValueError: for a precision outside the fixed-decimal family, or for a
            size that is not positive. A dimension drawn with no line or no
            lettering is not a dimension, so this refuses rather than writing
            something invisible.
    """
    if not 0 <= settings.precision <= MAX_FIXED_PRECISION:
        raise ValueError(
            f"panel dimension precision is a number of decimal places, 0 to "
            f"{MAX_FIXED_PRECISION}, and this is {settings.precision}. KiCad's "
            f"higher values are its value-dependent precisions, where the "
            f"decimals shown change with the number - not something a "
            f"fabrication drawing should do on its own"
        )
    for field in ("text_height_mm", "text_thickness_mm", "line_thickness_mm",
                  "arrow_length_mm"):
        # None is the two text sizes saying "derive me from the panel"
        # (`Settings.resolved`), which is not a size at all yet.
        if getattr(settings, field) is None:
            continue
        if getattr(settings, field) <= 0.0:
            raise ValueError(
                f"panel dimension {field} must be greater than zero, and it is "
                f"{getattr(settings, field)!r} - a dimension drawn at zero size "
                f"is a dimension nobody can read"
            )
    for field in ("first_offset_mm", "row_pitch_mm", "extension_offset_mm"):
        # None is an offset saying "derive me from the lettering".
        if getattr(settings, field) is None:
            continue
        if getattr(settings, field) < 0.0:
            raise ValueError(
                f"panel dimension {field} is a distance and must not be "
                f"negative: {getattr(settings, field)!r}"
            )
    if not settings.layer:
        raise ValueError("panel dimensions need a layer to be drawn on")
    return settings


# ---------------------------------------------------------------------------
# nanometres - the unit KiCad measures in
# ---------------------------------------------------------------------------


def kiround(value: float) -> int:
    """KiCad's `KiROUND`: halfway cases away from zero (`math/util.h:102-126`).

    Python's own `round` breaks ties to even, which would disagree with the
    file format at every exact half-nanometre.
    """
    return -math.floor(-value + 0.5) if value < 0 else math.floor(value + 0.5)


def to_nm(mm: float) -> int:
    """Millimetres as the integer nanometres KiCad's parser would store.

    `parseBoardUnits` is `KiROUND( value * IU_PER_MM )`, so this is the one
    place millimetres become integers: rounding happens at one documented
    boundary.
    """
    return kiround(mm * _NM_PER_MM)


def to_mm(nm: int) -> float:
    """Integer nanometres back to the millimetre float the file carries."""
    return nm / _NM_PER_MM


def quantized(mm: float) -> float:
    """*mm* snapped to whole nanometres - what the file can actually hold."""
    return to_mm(to_nm(mm))


def measured_nm(start: tuple, end: tuple) -> int:
    """What KiCad will measure between these two points, in nanometres.

    Both roundings, in KiCad's order: the coordinates become nanometres when
    the file is parsed, and the length is `KiROUND` of their Euclidean norm
    (`pcb_dimension.cpp:962`).
    """
    dx = to_nm(end[0]) - to_nm(start[0])
    dy = to_nm(end[1]) - to_nm(start[1])
    return kiround(math.hypot(dx, dy))


def value_text(nm: int, settings: Settings) -> str:
    """The string KiCad shows for a measurement of *nm* nanometres.

    `PCB_DIMENSION_BASE::GetValueText` then `updateText`
    (`pcb_dimension.cpp:376-463`): the value in the dimension's units at its
    precision, trailing zeroes optionally stripped, then the unit suffix -
    `" mm"` for `units 2` under `units_format 1`
    (`eda_units.cpp:144-151`, `EDA_UNIT_UTILS::GetText`).
    """
    places = Decimal(1).scaleb(-settings.precision)
    text = str(Decimal(nm).scaleb(-6).quantize(places, rounding=ROUND_HALF_UP))
    if settings.suppress_zeroes and "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} mm"


def extension_height_mm(arrow_length_mm: float) -> float:
    """The stub of extension line past the crossbar, KiCad's own derivation.

    `PCB_DIM_ALIGNED`'s constructor (`pcb_dimension.cpp:853`) computes
    `int( m_arrowLength * sin(27.5 deg) )` in nanometres and TRUNCATES. The
    value is written to the file and read back verbatim - never recomputed - so
    a panel this stage writes has to derive it the same way or its dimensions
    would not look like KiCad's.
    """
    return to_mm(int(to_nm(arrow_length_mm) * math.sin(math.radians(
        ARROW_ANGLE_DEG
    ))))


# ---------------------------------------------------------------------------
# which way the crossbar goes
# ---------------------------------------------------------------------------


def signed_height(start: tuple, end: tuple, outward: tuple,
                  distance_mm: float) -> float:
    """The `height` that puts the crossbar *distance_mm* along *outward*.

    `PCB_DIM_ALIGNED::updateGeometry` (`pcb_dimension.cpp:960-987`) offsets the
    crossbar along `(-d.y, d.x)` for a positive height and `(d.y, -d.x)` for a
    negative one, where `d = end - start`. So the sign is not a style choice -
    it is which side of the measured line the dimension is drawn on - and this
    derives it from the side asked for rather than assuming an orientation.

    Raises:
        ValueError: when *outward* is not perpendicular to the measured line,
            which would mean the caller asked for a crossbar somewhere an
            aligned dimension cannot put one.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dx * outward[0] + dy * outward[1]) > 1e-9 * max(
        1.0, abs(dx) + abs(dy)
    ):
        raise ValueError(
            f"an aligned dimension's crossbar sits perpendicular to what it "
            f"measures, and {outward} is not perpendicular to the line from "
            f"{start} to {end}"
        )
    # The direction a POSITIVE height would offset along.
    positive = (-dy, dx)
    towards = positive[0] * outward[0] + positive[1] * outward[1]
    return distance_mm if towards > 0 else -distance_mm


def crossbar_nm(start: tuple, end: tuple, height_mm: float) -> tuple:
    """Where the crossbar's two ends land, in nanometres.

    The offset is `unit(extension) * |height|` - `sign(height)` applied twice
    over `VECTOR2::Resize`, which is signed (`vector2d.h:385-419`), leaves the
    magnitude and the extension direction's own sense.
    """
    sx, sy = to_nm(start[0]), to_nm(start[1])
    ex, ey = to_nm(end[0]), to_nm(end[1])
    dx, dy = ex - sx, ey - sy
    height = to_nm(height_mm)
    extension = (-dy, dx) if height > 0 else (dy, -dx)
    length = math.hypot(*extension)
    if length == 0.0:
        raise ValueError(
            "a dimension needs two distinct feature points, and this one "
            f"measures from {start} to the same place"
        )
    scale = abs(height) / length
    offset = (kiround(extension[0] * scale), kiround(extension[1] * scale))
    return ((sx + offset[0], sy + offset[1]), (ex + offset[0], ey + offset[1]))


def _half(value: int) -> int:
    """C++ integer division by two: truncation towards zero."""
    return -((-value) // 2) if value < 0 else value // 2


def text_placement(start: tuple, end: tuple, height_mm: float) -> tuple:
    """`((x_mm, y_mm), angle_deg)` for the inline value on the crossbar.

    `PCB_DIM_ALIGNED::updateText` (`pcb_dimension.cpp:1026-1065`): under
    `DIM_TEXT_POSITION::INLINE` the text sits at the crossbar's midpoint, and
    with `keep_text_aligned` it turns to run along the crossbar - reading
    left-to-right or bottom-to-top, never upside down.
    """
    (sx, sy), (ex, ey) = crossbar_nm(start, end, height_mm)
    centre = (_half(ex - sx), _half(ey - sy))
    position = (to_mm(sx + centre[0]), to_mm(sy + centre[1]))
    angle = (360.0 - _vector_angle(centre)) % 360.0
    if 90.0 < angle <= 270.0:
        angle -= 180.0
    return (position, angle)


def effective_pen_mm(settings: Settings) -> float:
    """The pen a glyph is really drawn with: the stated one, clamped.

    `EDA_TEXT::GetEffectiveTextPenWidth` (`common/eda_text.cpp:449-467`) ends in
    `ClampTextPenSize`, which caps the pen at :data:`MAX_PEN_RATIO` of the
    smaller text dimension (`common/gr_text.cpp:73-96`). Derived here rather
    than assumed, because the OUTSIDE outset is built on this number and a
    thickness typed past the clamp would move the text further than KiCad does.
    """
    return min(settings.text_thickness_mm,
               MAX_PEN_RATIO * settings.text_height_mm)


def text_outset_mm(settings: Settings) -> float:
    """How far OUTSIDE mode pushes the value off the crossbar, in millimetres.

    KiCad's own figure, `GetEffectiveTextPenWidth() + GetTextHeight()`
    (`pcb_dimension.cpp:1031`). DERIVED, never chosen: KiCad
    recomputes an OUTSIDE text's position every time the file is read - the
    parser calls `Update()` on each dimension it builds
    (`pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp:4925`) - so a distance of
    our own would be overwritten the moment the panel is opened, and the preview
    would show a panel KiCad does not.
    """
    return effective_pen_mm(settings) + settings.text_height_mm


def outside_placement(start: tuple, end: tuple, height_mm: float,
                      outset_mm: float) -> tuple:
    """`((x_mm, y_mm), angle_deg)` for the value set BESIDE the crossbar.

    `PCB_DIM_ALIGNED::updateText` under `DIM_TEXT_POSITION::OUTSIDE`
    (`pcb_dimension.cpp:1029-1046`): the crossbar's half-vector is turned a
    quarter turn - `+90` when it points right or straight up, `-90` when it
    points left or straight down - resized to *outset_mm*, and added to the
    inline position. `RotatePoint` turns by `(y*sin + x*cos, y*cos - x*sin)`
    (`libs/kimath/src/trigo.cpp:229-252`), which is why the quarter turn is
    written out here rather than taken from a rotation matrix of our own.

    The text ANGLE is the inline angle: `keep_text_aligned` runs the value along
    the crossbar whatever side of it the text sits on (`:1053-1061`).
    """
    (sx, sy), (ex, ey) = crossbar_nm(start, end, height_mm)
    centre = (_half(ex - sx), _half(ey - sy))
    if centre[0] == 0:
        quarter = 90.0 if -centre[1] >= 0 else -90.0
    elif centre[0] < 0:
        quarter = -90.0
    else:
        quarter = 90.0
    turned = _quarter_turn(centre, quarter)
    offset = _resized(turned, to_nm(outset_mm))
    position = (to_mm(sx + centre[0] + offset[0]),
                to_mm(sy + centre[1] + offset[1]))
    angle = (360.0 - _vector_angle(centre)) % 360.0
    if 90.0 < angle <= 270.0:
        angle -= 180.0
    return (position, angle)


def manual_placement(start: tuple, end: tuple, height_mm: float,
                     outward: tuple, outset_mm: float) -> tuple:
    """`((x_mm, y_mm), angle_deg)` for the value set beside the crossbar, OUTBOARD.

    THE SIDE IS DECIDED BY *outward* AND BY NOTHING ELSE. *outward*
    is the row's own "away from the panel" direction - :data:`SIDES` - so the
    value sits outboard of its crossbar on the top row and on the right row
    alike. KiCad's `OUTSIDE` mode cannot do that: its quarter turn sends a
    vertical dimension's value to `-x` whichever way the measured line runs
    (`pcb_dimension.cpp:1035-1046`), which on the right-hand row is the panel
    side. See :func:`outside_placement`, which is that rule, ported.

    The distance is still KiCad's own figure (:func:`text_outset_mm`), so the
    drawing reads as a KiCad outset; only the direction is ours. This position
    SURVIVES a load because the item is written `MANUAL`
    (:data:`TEXT_POSITION_MANUAL`): the parser's closing `Update()`
    (`pcb_io_kicad_sexpr_parser.cpp:4924`) recomputes an `OUTSIDE` position and
    leaves a `MANUAL` one alone.

    The text ANGLE is the inline angle: `keep_text_aligned` runs the value along
    the crossbar whatever side of it the text sits on
    (`pcb_dimension.cpp:1053-1061`), so it is computed, not chosen.
    """
    (sx, sy), (ex, ey) = crossbar_nm(start, end, height_mm)
    centre = (_half(ex - sx), _half(ey - sy))
    if abs(centre[0] * outward[0] + centre[1] * outward[1]) > 0:
        raise ValueError(
            f"a dimension's value is set beside its crossbar, not along it, "
            f"and {outward} is not perpendicular to the crossbar from {start} "
            f"to {end}"
        )
    offset = _resized(
        (kiround(outward[0]), kiround(outward[1])), to_nm(outset_mm)
    )
    position = (to_mm(sx + centre[0] + offset[0]),
                to_mm(sy + centre[1] + offset[1]))
    angle = (360.0 - _vector_angle(centre)) % 360.0
    if 90.0 < angle <= 270.0:
        angle -= 180.0
    return (position, angle)


def _quarter_turn(vector: tuple, degrees: float) -> tuple:
    """`RotatePoint` for the only two angles this stage turns by.

    `libs/kimath/src/trigo.cpp:241-251` answers the quarter turns exactly rather
    than through `sin`/`cos`: `+90` gives `(y, -x)` and `270` - which is how a
    normalised `-90` arrives - gives `(-y, x)`.
    """
    if degrees == 90.0:
        return (vector[1], -vector[0])
    if degrees == -90.0:
        return (-vector[1], vector[0])
    raise ValueError(
        f"this stage turns a crossbar by a quarter turn, not by {degrees} "
        f"degrees"
    )


def _resized(vector: tuple, length_nm: int) -> tuple:
    """`VECTOR2I::Resize` for an axis-aligned vector (`vector2d.h:385-419`).

    The general case splits the length between the axes by the squares of the
    components and rounds each with `KiROUND`, keeping each component's own
    sign. An axis-aligned vector - which is every crossbar this stage draws -
    puts the whole length on its own axis, and that is asserted rather than
    assumed: a diagonal crossbar would land on KiCad's `rescale` arithmetic and
    this would quietly disagree with it.
    """
    x, y = vector
    if x != 0 and y != 0:
        raise ValueError(
            f"the panel's dimensions run along an axis, and {vector} does not "
            f"- an outset for a diagonal crossbar is not derived here"
        )
    if x == 0 and y == 0:
        return (0, 0)
    if x:
        return (kiround(math.copysign(length_nm, x)), 0)
    return (0, kiround(math.copysign(length_nm, y)))


def _vector_angle(vector: tuple) -> float:
    """`EDA_ANGLE( VECTOR2D )` in degrees (`eda_angle.h:72-110`).

    The axis and diagonal cases are answered exactly rather than through
    `atan2`, which is what KiCad does and what keeps an axis-aligned dimension's
    text angle a whole number.
    """
    x, y = vector
    if x == 0.0 and y == 0.0:
        return 0.0
    if y == 0.0:
        return 0.0 if x >= 0 else -180.0
    if x == 0.0:
        return 90.0 if y >= 0 else -90.0
    if x == y:
        return 45.0 if x >= 0 else -135.0
    if x == -y:
        return -45.0 if x >= 0 else 135.0
    return math.degrees(math.atan2(y, x))


# ---------------------------------------------------------------------------
# what gets measured
# ---------------------------------------------------------------------------

# The two sides the dimensions are drawn on, and the direction "away from the
# panel" points on each: top and right. Every
# side-dependent decision in this module reads THIS table or the overall
# measures themselves - the board that gets dimensioned is found from where
# the overall dimensions are, never from a named corner - so
# moving the dimensions to the bottom and the left is an edit here plus the two
# blank edges `rows_of` reaches for.
SIDES = {
    "top": (0.0, -1.0),
    "right": (1.0, 0.0),
    "bottom": (0.0, 1.0),
    "left": (-1.0, 0.0),
}

# The two measurements that bound the panel itself. The inner pair is nested
# INSIDE these, and the corner these two share picks the board that is measured.
OVERALL_NAMES = ("panel_width", "panel_length")


class Measure(NamedTuple):
    """One dimension: what it measures, where it is drawn, and what it says."""

    name: str
    # A sentence for the run report, so a reader never has to decode `name`.
    label: str
    side: str
    start: tuple
    end: tuple
    # How far outside the blank's edge this crossbar sits. Assigned by
    # `rows_of`, never typed.
    offset_mm: float
    # The distance from the feature points out to the crossbar - which is the
    # offset PLUS however far inside the panel the measured line is.
    reach_mm: float
    value_nm: int
    text: str
    # WHERE THIS ROW'S VALUE SITS: on the crossbar
    # for the panel's own extents, beside it for the inner pair, whose text was
    # overprinting its own crossbar and arrowheads. A per-measure answer and not
    # a setting, because it is a property of which dimension this is.
    text_position: int = TEXT_POSITION_INLINE
    text_suffix: str = ""

    @property
    def value_mm(self) -> float:
        """What KiCad will measure, in millimetres."""
        return to_mm(self.value_nm)

    @property
    def height_mm(self) -> float:
        """The signed `height` token: magnitude and side in one number."""
        return signed_height(self.start, self.end, SIDES[self.side],
                             self.reach_mm)


class PanelDimensions(NamedTuple):
    """What the stage measured, the layer it drew on, and how big it lettered."""

    measures: tuple
    layer: str
    # The height the lettering was actually drawn at - derived from the panel
    # unless the config named one. Reported because it is a decision the run
    # made, and a decision nobody can see is a decision nobody can overrule.
    text_height_mm: float = TEXT_HEIGHT_MIN_MM
    # WHICH OF THE TWO THAT WAS. The height is a setting on the panel page, and
    # the field is empty on the page when the panel sizes
    # its own lettering, so the run has to be able to say which answer it used -
    # a number on its own does not tell a reader whether anybody chose it.
    text_height_derived: bool = True
    # WHICH BOARD THE INNER PAIR MEASURES, and the point that chose it.
    # Reported for the same reason the text height is: the run
    # picked one instance out of many and a reader has to be able to see which,
    # and why. `board_index` indexes the boxes the caller handed in.
    board_box: tuple = None
    board_index: int = None
    board_corner: tuple = None
    annotation_bounds: tuple = None

    @property
    def count(self) -> int:
        return len(self.measures)

    @property
    def by_name(self) -> dict:
        return {measure.name: measure for measure in self.measures}

    def value_of(self, name: str) -> float:
        """One measurement in millimetres, or None when it was not drawn."""
        measure = self.by_name.get(name)
        return None if measure is None else measure.value_mm


def instance_rects(instance_boxes) -> tuple:
    """The board rectangles, each `(min_x, min_y, max_x, max_y)` in board mm.

    Takes what `step_and_repeat` hands the carrier - a sequence of four-number
    boxes - or anything with a `boxes` sequence, and normalises each so the two
    minima really are the minima. A box handed in corner-swapped would measure
    the same size and choose a different instance, which is the kind of quiet
    difference this stage exists to prevent.
    """
    if instance_boxes is None:
        return ()
    items = getattr(instance_boxes, "boxes", instance_boxes)
    rects = []
    for box in items:
        values = tuple(float(value) for value in box)
        if len(values) != 4:
            raise ValueError(
                f"a board rectangle is four numbers, min_x, min_y, max_x, "
                f"max_y, and this one is {box!r}"
            )
        rects.append((
            min(values[0], values[2]), min(values[1], values[3]),
            max(values[0], values[2]), max(values[1], values[3]),
        ))
    return tuple(rects)


def shared_corner_of(measures) -> tuple:
    """The one point the overall dimensions both touch, or None.

    The board that gets dimensioned is the one closest to the corner the outer
    dimension items share. This finds that corner by
    intersecting the two overall measures' endpoint sets - so it is read off
    where those dimensions ACTUALLY are, and a change that draws them on
    the bottom and the left moves the corner and the chosen board with it. No
    corner is named anywhere.

    Compared on the nanometre, which is the resolution the endpoints are written
    at: two coordinates KiCad would store identically are the same point here.
    """
    overall = [
        measure for measure in measures if measure.name in OVERALL_NAMES
    ]
    if len(overall) != 2:
        return None
    first, second = overall
    keyed = {
        (to_nm(point[0]), to_nm(point[1])): point
        for point in (first.start, first.end)
    }
    shared = [
        keyed[(to_nm(point[0]), to_nm(point[1]))]
        for point in (second.start, second.end)
        if (to_nm(point[0]), to_nm(point[1])) in keyed
    ]
    # Exactly one, or the two lines are not two sides meeting at a corner.
    return shared[0] if len(shared) == 1 else None


def nearest_instance(rects: tuple, corner: tuple) -> int:
    """The index of the board rectangle closest to *corner*, or None.

    Distance is measured to the rectangle ITSELF - zero when the corner sits on
    it - so a rail of any width, and a panel whose boards are turned, still pick
    the copy in that corner of the grid. Ties break on the rectangle's own
    coordinates, so a panel with two boards equally close always dimensions the
    same one: the same input gives the same output.
    """
    if not rects or corner is None:
        return None
    def key(item):
        index, rect = item
        dx = max(rect[0] - corner[0], 0.0, corner[0] - rect[2])
        dy = max(rect[1] - corner[1], 0.0, corner[1] - rect[3])
        return (to_nm(math.hypot(dx, dy)), to_nm(rect[0]), to_nm(rect[1]),
                index)
    return min(enumerate(rects), key=key)[0]


def board_drafts(rect: tuple, corner: tuple, overall: tuple,
                 settings: Settings) -> list:
    """One board's own width and length, drawn on the overall pair's own sides.

    Each inner dimension copies the side and the reading direction of the
    overall dimension it nests inside, and is measured along the rectangle's
    edge NEAREST the shared corner - the edge closest to where the drawing is
    read. Nothing here decides a side; both come from the
    overall measures.
    """
    drafts = []
    for measure in overall:
        outward = SIDES[measure.side]
        # Which axis this dimension measures: the one the crossbar does NOT
        # travel along. `outward` is a unit vector on one axis, so the measured
        # axis is the other one.
        axis = 1 if abs(outward[0]) > abs(outward[1]) else 0
        other = 1 - axis
        # The rectangle edge on the crossbar's axis that sits nearest the shared
        # corner, so the extension lines run the short way to the crossbar.
        edges = (rect[other], rect[other + 2])
        held = min(edges, key=lambda value: abs(value - corner[other]))
        low, high = rect[axis], rect[axis + 2]
        first, second = ((low, high) if measure.end[axis] >= measure.start[axis]
                         else (high, low))
        points = [[0.0, 0.0], [0.0, 0.0]]
        points[0][axis], points[1][axis] = first, second
        points[0][other] = points[1][other] = held
        name = "board_width" if axis == 0 else "board_length"
        label = ("one board's own width" if axis == 0
                 else "one board's own length")
        drafts.append(_draft(
            name, label, measure.side,
            tuple(points[0]), tuple(points[1]), settings,
            # THE INNER PAIR'S VALUE IS OUTSET, AWAY FROM THE PANEL, so the
            # text does not overlap the dimension arrows. MANUAL, because KiCad's
            # own OUTSIDE mode sends a vertical value to the panel side and
            # cannot be talked out of it - see `manual_placement`. Set here, on
            # the two inner dimensions only, so the panel's own extents keep
            # their stock look.
            text_position=TEXT_POSITION_MANUAL,
        ))
    return drafts


def rows_of(measures: list, blank, settings: Settings) -> tuple:
    """*measures* with an offset each: smallest measurement innermost.

    A drafter puts the short dimensions next to the part and the overall
    extents outside them, so nothing crosses and the outermost line is the one
    that says how big the thing is. Ordered by measured value with the name as
    tie-break, so the same panel always numbers its rows the same way.

    THIS IS WHAT KEEPS THE NESTED PAIR CLEAR. A board is
    strictly smaller than the panel holding it, so a board dimension sorts
    inside the panel dimension on its side every time, and the two crossbars
    are a full `row_pitch_mm` apart - which scales with the lettering, so the
    numbers cannot print through each other at any panel size.
    """
    placed = []
    for side in SIDES:
        on_side = sorted(
            (measure for measure in measures if measure.side == side),
            key=lambda measure: (measure.value_nm, measure.name),
        )
        for row, measure in enumerate(on_side):
            offset = settings.first_offset_mm + row * settings.row_pitch_mm
            axis = 1 if side in ("top", "bottom") else 0
            edge = {"top": blank.min_y, "bottom": blank.max_y,
                    "left": blank.min_x, "right": blank.max_x}[side]
            inside = abs(measure.start[axis] - edge)
            placed.append(measure._replace(
                offset_mm=quantized(offset),
                reach_mm=quantized(offset + inside),
            ))
    # Back into a stable, side-then-row order: the file reads the way the panel
    # does, and two builds cannot order it differently.
    return tuple(sorted(
        placed, key=lambda measure: (measure.side, measure.offset_mm,
                                     measure.name)
    ))


def plan(blank, instance_boxes=(), settings=None,
         warnings=None) -> PanelDimensions:
    """Every dimension this panel gets: what, where, and what it will say.

    Reads nothing and writes nothing - :func:`apply` puts them on a board.
    Separate so the panel page can show these numbers before anything is built.

    *blank* is the panel blank (:class:`channels.Rect`); *instance_boxes* are
    the board rectangles `step_and_repeat` measured, which the panel itself can
    no longer answer for - the instances' own `Edge.Cuts` are gone by the time
    the carrier features run.

    The overall pair is planned first because the inner pair is derived FROM it:
    the corner the two overall dimensions share picks the board that is measured
    (:func:`shared_corner_of`).

    The lettering is sized HERE, from the blank, so the page can say what the
    text height will be before a board is written (:func:`text_height_for`).
    """
    settings = Settings.of(settings)
    if blank.width_mm <= 0.0 or blank.height_mm <= 0.0:
        raise ValueError(
            f"a panel with no extent cannot be dimensioned: the blank measures "
            f"{blank.width_mm:.4f} x {blank.height_mm:.4f} mm"
        )
    # WHO CHOSE THE LETTERING SIZE, read before it is resolved: after
    # `resolved` every height is a number and the answer is
    # gone. `apply` hands the unresolved settings in for exactly this reason.
    derived = settings.text_height_mm is None
    settings = settings.resolved(longest_extent_of(blank))

    overall = (
        _draft("panel_width", "panel overall width", "top",
               (blank.min_x, blank.min_y), (blank.max_x, blank.min_y),
               settings),
        _draft("panel_length", "panel overall length", "right",
               (blank.max_x, blank.min_y), (blank.max_x, blank.max_y),
               settings),
    )
    drafts = list(overall)

    rects = instance_rects(instance_boxes)
    corner = shared_corner_of(overall)
    chosen = nearest_instance(rects, corner)
    rect = None
    if chosen is not None:
        rect = rects[chosen]
        drafts += board_drafts(rect, corner, overall, settings)
    elif warnings is not None:
        _report_missing_board(rects, corner, warnings)

    return PanelDimensions(
        measures=rows_of(drafts, blank, settings),
        layer=settings.layer,
        text_height_mm=settings.text_height_mm,
        text_height_derived=derived,
        board_box=rect,
        board_index=chosen,
        board_corner=None if rect is None else corner,
    )


def _report_missing_board(rects: tuple, corner, warnings: list) -> None:
    """Say why one board's own size could not be drawn.

    A missing dimension is easy to miss on a drawing, so the reason is stated
    because a check reports, it does not block. Two ways it happens: nobody
    handed this stage the board rectangles, or the two overall dimensions do not
    meet at a corner and so name no board to measure.
    """
    if not rects:
        warnings.append(
            "no board rectangle reached the dimension stage, so the panel "
            "states its own size but not one board's - the panel's overall "
            "dimensions are drawn and the inner pair is not"
        )
    elif corner is None:
        warnings.append(
            f"the panel's two overall dimensions do not meet at a shared "
            f"corner, so there is no corner to pick a board by and none of the "
            f"{len(rects)} board(s) is dimensioned"
        )


def _draft(name: str, label: str, side: str, start: tuple, end: tuple,
           settings: Settings,
           text_position: int = TEXT_POSITION_INLINE) -> Measure:
    """One measurement, measured and worded but not yet placed in a row."""
    value = measured_nm(start, end)
    return Measure(
        name=name,
        label=label,
        side=side,
        start=(quantized(start[0]), quantized(start[1])),
        end=(quantized(end[0]), quantized(end[1])),
        offset_mm=0.0,
        reach_mm=0.0,
        value_nm=value,
        text=value_text(value, settings),
        text_position=text_position,
    )


# ---------------------------------------------------------------------------
# writing it
# ---------------------------------------------------------------------------


def dimension_for(measure: Measure, settings=None) -> Dimension:
    """One measurement as a native KiCad aligned dimension item.

    The uuid is derived from what the dimension measures and where, so the same
    panel built twice names the same dimension and a panel that changed size
    gets new ones rather than silently reusing the old identities.

    The inner `gr_text` carries the SAME uuid as the dimension, which is what
    KiCad writes: a dimension IS a `PCB_TEXT` (`pcb_dimension.h:118`) and the
    two share one `m_Uuid`.

    Settings that still ask for a derived text size are resolved against no
    panel, which is the floor: this writes ONE dimension and has no blank to
    measure. :func:`apply` resolves against the real panel first, so a panel's
    dimensions are lettered at the panel's own scale.
    """
    settings = Settings.of(settings).resolved()
    height = measure.height_mm
    if measure.text_position == TEXT_POSITION_MANUAL:
        # THE SIDE COMES FROM THE ROW, not from KiCad's quarter turn (see
        # `manual_placement`). `SIDES` is the one table that says which way
        # is away from the panel, so a change that moves the rows moves the
        # values with them.
        position, angle = manual_placement(
            measure.start, measure.end, height, SIDES[measure.side],
            text_outset_mm(settings),
        )
    elif measure.text_position == TEXT_POSITION_OUTSIDE:
        position, angle = outside_placement(
            measure.start, measure.end, height, text_outset_mm(settings),
        )
    else:
        position, angle = text_placement(measure.start, measure.end, height)
    key = "|".join((
        measure.name,
        _coordinate(measure.start[0]), _coordinate(measure.start[1]),
        _coordinate(measure.end[0]), _coordinate(measure.end[1]),
        _coordinate(height),
    ))
    identity = str(uuid.uuid5(DIMENSION_NAMESPACE, key))
    return Dimension(
        dim_type=DimensionType.aligned,
        layer=settings.layer,
        id=identity,
        pts=[measure.start, measure.end],
        height=height,
        dim_format=DimensionFormat(
            prefix="",
            suffix=measure.text_suffix,
            units=UNITS_MM,
            units_format=UNITS_FORMAT_BARE_SUFFIX,
            precision=settings.precision,
            suppress_zeroes=settings.suppress_zeroes,
        ),
        style=DimensionStyle(
            thickness=quantized(settings.line_thickness_mm),
            arrow_length=quantized(settings.arrow_length_mm),
            text_position_mode=measure.text_position,
            arrow_direction=ArrowDirection.outward,
            extension_height=extension_height_mm(settings.arrow_length_mm),
            extension_offset=quantized(settings.extension_offset_mm),
            keep_text_aligned=True,
        ),
        text=DimensionText(
            content=measure.text,
            position=position,
            angle=angle,
            layer=settings.layer,
            id=identity,
            attributes=TextAttributes(
                font_size=(quantized(settings.text_height_mm),
                           quantized(settings.text_height_mm)),
                font_thickness=quantized(settings.text_thickness_mm),
            ),
        ),
    )


def _coordinate(value: float) -> str:
    """One millimetre value at the file's own precision, sign-normalised.

    The same rule the tooling-hole stage keys its uuids by, so `-0.0` and `0.0`
    are one identity and a coordinate cannot derive two.
    """
    rounded = round(value, 6)
    return f"{rounded + 0.0:.6f}" if rounded else "0.000000"


def annotation_box(item):
    """Conservative text bounds using maximum glyph width, including stroke."""
    text = item.text
    size = text.attributes.font_size[0]
    width = (len(text.content) + 2) * size
    height = size * 2
    angle = math.radians(text.angle or 0)
    dx = (abs(math.cos(angle)) * width + abs(math.sin(angle)) * height) / 2
    dy = (abs(math.sin(angle)) * width + abs(math.cos(angle)) * height) / 2
    x, y = text.position
    return (x - dx, y - dy, x + dx, y + dy)


def _boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _segment_hits_box(start, end, box):
    """Clip a segment against the interior of a padded text rectangle."""
    low, high = 0.0, 1.0
    for axis in (0, 1):
        delta = end[axis] - start[axis]
        if abs(delta) < 1e-12:
            if not box[axis] < start[axis] < box[axis + 2]:
                return False
        else:
            a = (box[axis] - start[axis]) / delta
            b = (box[axis + 2] - start[axis]) / delta
            low, high = max(low, min(a, b)), min(high, max(a, b))
            if low >= high:
                return False
    return True


def _annotation_item(measure, settings, blank):
    item = dimension_for(measure, settings)
    if measure.name.startswith("hole_diameter_"):
        # KiCad PCB_DIM_LEADER uses two points and the stored text position.
        x = (measure.start[0] + measure.end[0]) / 2
        y = measure.start[1]
        radius = measure.value_mm / 2
        elbow_y = blank.max_y + measure.offset_mm
        elbow = (x + elbow_y - y, elbow_y)
        item.dim_type = DimensionType.leader
        item.height = None
        # KiCad accepts extension_height only on aligned dimensions.
        item.style.extension_height = None
        item.pts = [(x + radius / math.sqrt(2), y + radius / math.sqrt(2)), elbow]
        item.dim_format.override_value = measure.text
        # The override already includes units for each diameter in the list.
        item.dim_format.units_format = 0
        item.style.text_position_mode = TEXT_POSITION_MANUAL
        item.style.keep_text_aligned = False
        item.style.extension_offset = 0
        item.style.text_frame = 0
        item.text.angle = 0
        item.text.position = (elbow[0] + (len(measure.text) + 3) * settings.text_height_mm / 2,
                              elbow[1])
    else:
        # Orthogonal dimensions measure one axis while each endpoint stays on
        # its own feature. KiCad height is the signed coordinate displacement
        # from the first endpoint to the crossbar, independent of point order.
        horizontal = measure.side in ("top", "bottom")
        start, end = measure.start, measure.end
        if measure.name == "rail_right":
            end = (blank.max_x, blank.max_y)
        elif measure.name == "rail_left":
            start = (blank.min_x, blank.max_y)
        elif measure.name == "rail_bottom":
            end = (blank.max_x, blank.max_y)
        elif measure.name == "rail_top":
            start = (blank.max_x, blank.min_y)
        elif measure.name == "hole_offset_x":
            start = (blank.min_x, blank.max_y)
        elif measure.name == "hole_offset_y":
            end = (blank.min_x, blank.max_y)
        item.dim_type = DimensionType.orthogonal
        item.orientation = 0 if horizontal else 1
        item.pts = [start, end]
        edge = {"top": blank.min_y, "bottom": blank.max_y,
                "left": blank.min_x, "right": blank.max_x}[measure.side]
        direction = -1 if measure.side in ("top", "left") else 1
        item.height = quantized(edge + direction * measure.offset_mm
                                - start[1 if horizontal else 0])
    return item


def arrange_annotations(made, blank, settings, warnings=None):
    """Use shared detail and overall offsets, then clear the label positions."""
    from eedesigncore.render.dimensions import dimension_segments_mm

    common_outset = quantized(text_outset_mm(settings) * 1.5)
    sizes = [settings.text_height_mm for _ in made.measures]
    for _ in range(128):
        pitch = max(settings.row_pitch_mm, common_outset + settings.text_height_mm * 2.5)
        measures, items = [], []
        for index, measure in enumerate(made.measures):
            offset = settings.first_offset_mm + (pitch if measure.name in OVERALL_NAMES else 0)
            measure = measure._replace(offset_mm=quantized(offset),
                        reach_mm=quantized(measure.reach_mm - measure.offset_mm + offset))
            lettering = settings._replace(text_height_mm=quantized(sizes[index]),
                       text_thickness_mm=(settings.text_thickness_mm if sizes[index] == settings.text_height_mm
                                          else quantized(sizes[index] * TEXT_STROKE_RATIO)))
            item = _annotation_item(measure, lettering, blank)
            if item.dim_type != DimensionType.leader and measure.text_position == TEXT_POSITION_MANUAL:
                item.text.position, item.text.angle = manual_placement(
                    measure.start, measure.end, measure.height_mm,
                    SIDES[measure.side], common_outset)
            measures.append(measure)
            items.append(item)
        boxes = [annotation_box(item) for item in items]
        lines = [list(dimension_segments_mm(item) or ()) for item in items]
        conflicts = set()
        for index, box in enumerate(boxes):
            for other in range(len(items)):
                if index == other:
                    continue
                if (_boxes_overlap(box, boxes[other])
                        or any(_segment_hits_box(a, b, box) for a, b in lines[other])):
                    conflicts.add(index)
        if not conflicts:
            break
        changed = False
        if made.text_height_derived:
            for index in sorted(conflicts):
                smaller = max(0.9, sizes[index] * 0.85)
                if smaller < sizes[index] - 1e-6:
                    sizes[index] = smaller
                    changed = True
        if not changed:
            # Increase the same text-to-span distance for every outset label.
            # Keep overall crossbars outside those labels on every attempt.
            common_outset = quantized(common_outset + max(settings.text_height_mm, 0.5))
    else:
        if warnings is not None:
            warnings.append("Some dimension annotations could not be separated with the common text offset.")
    points = [(blank.min_x, blank.min_y), (blank.max_x, blank.max_y)]
    for item in items:
        box = annotation_box(item)
        points.extend(((box[0], box[1]), (box[2], box[3])))
        for a, b in dimension_segments_mm(item) or ():
            points.extend((a, b))
    margin = settings.text_height_mm
    bounds = (min(p[0] for p in points) - margin, min(p[1] for p in points) - margin,
              max(p[0] for p in points) + margin, max(p[1] for p in points) + margin)
    return made._replace(measures=tuple(measures), annotation_bounds=bounds), items


def preview_bounds(made, blank):
    """Include annotation rows and conservative text extents in the preview."""
    if made.annotation_bounds is not None:
        return made.annotation_bounds
    points = [(blank.min_x, blank.min_y), (blank.max_x, blank.max_y)]
    height = made.text_height_mm
    for measure in made.measures:
        points.extend((measure.start, measure.end))
        half_text = (len(measure.text) + 2) * height / 2
        if measure.side == "top":
            middle = (measure.start[0] + measure.end[0]) / 2
            y = blank.min_y - measure.offset_mm - height * 3
            points.extend(((middle - half_text, y), (middle + half_text, y)))
        else:
            middle = (measure.start[1] + measure.end[1]) / 2
            x = blank.max_x + measure.offset_mm + height * 3
            points.extend(((x, middle - half_text), (x, middle + half_text)))
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def apply(board: Board, blank, instance_boxes=(), settings=None,
          warnings=None, holes=None, source_outline=(), placements=()) -> PanelDimensions:
    """Draw the panel's dimensions on *board*, in place. Reports what it drew.

    Appended in the order :func:`plan` settled - side, then row - so the file
    reads the way the panel does and two builds cannot order it differently.

    The layer is NOT added to the board's table: a dimension on a layer the
    panel does not declare is a dimension KiCad accepts, never enables and
    nobody sees, so an absent layer is a warning naming it - a check
    reports, it does not block. Many designs do not declare `User.16`, which
    is exactly why the warning names the layer and the config key that moves it.
    """
    # TWO FORMS OF THE SAME SETTINGS, deliberately. The
    # plan is given the settings AS ASKED, so it can say whether the lettering
    # size was derived or stated; the items are drawn from the resolved form,
    # which is the same numbers with every derivation carried out. `resolved` is
    # idempotent, so the two cannot disagree about a single figure.
    asked = Settings.of(settings)
    rects = instance_rects(instance_boxes)
    if asked.text_height_mm is None:
        shortest_board = min((min(r[2] - r[0], r[3] - r[1]) for r in rects), default=30)
        height = quantized(1.5 * max(0.8, min(2.0, longest_extent_of(blank) / 150, shortest_board / 18)))
        settings = asked._replace(text_height_mm=height).resolved()
    else:
        settings = asked.resolved(longest_extent_of(blank))
    made = plan(blank, instance_boxes, settings, warnings)._replace(
        text_height_derived=asked.text_height_mm is None)
    group_id = str(uuid.uuid5(DIMENSION_NAMESPACE, "generated-dimensions"))
    owned = {member for group in board.groups if group.id == group_id for member in group.members}
    board.drawings[:] = [item for item in board.drawings if getattr(item, "id", None) not in owned]
    board.groups[:] = [group for group in board.groups if group.id != group_id]
    first_generated = len(board.drawings)
    extra = []
    rects = instance_rects(instance_boxes)
    if rects:
        left = min(r[0] for r in rects)
        top = min(r[1] for r in rects)
        right = max(r[2] for r in rects)
        bottom = max(r[3] for r in rects)
        # Prefer right and bottom when opposite rails have the same width.
        borders = [
            ("rail_right", "right rail plus setback", "bottom", (right, bottom), (blank.max_x, bottom)),
            ("rail_left", "left rail plus setback", "bottom", (blank.min_x, bottom), (left, bottom)),
            ("rail_bottom", "bottom rail plus setback", "right", (right, bottom), (right, blank.max_y)),
            ("rail_top", "top rail plus setback", "right", (right, blank.min_y), (right, top)),
        ]
        rail_counts = {}
        for _, _, _, start, end in borders:
            key = value_text(measured_nm(start, end), settings)
            rail_counts[key] = rail_counts.get(key, 0) + 1
        seen_rails = set()
        for name, label, side, start, end in borders:
            value = measured_nm(start, end)
            key = value_text(value, settings)
            if value > 0 and key not in seen_rails:
                seen_rails.add(key)
                measure = _draft(name, label, side, start, end, settings, TEXT_POSITION_MANUAL)
                if rail_counts[key] > 1:
                    measure = measure._replace(text=measure.text + " TYP", text_suffix=" TYP")
                extra.append(measure)
        # Across and down gaps measure the placed board extents, as the grid
        # controls do. Curved or segmented outlines do not make that gap ambiguous.
        # A complete grid with equal board extents is still required.
        xs = sorted({r[0] for r in rects})
        ys = sorted({r[1] for r in rects})
        regular = len(xs) * len(ys) == len(rects) and len({
            (round(r[2] - r[0], 6), round(r[3] - r[1], 6)) for r in rects}) == 1
        if regular:
            for axis, side in ((0, "top"), (1, "left")):
                row = sorted((r for r in rects if (r[1] == top if axis == 0 else r[0] == left)),
                             key=lambda r: r[axis])
                seen = set()
                for before, after in zip(row, row[1:]):
                    gap = to_nm(after[axis] - before[axis + 2])
                    if gap <= 0 or gap in seen:
                        continue
                    seen.add(gap)
                    start, end = ((before[2], top), (after[0], top)) if axis == 0 else ((left, before[3]), (left, after[1]))
                    extra.append(_draft(f"board_gap_{axis}_{gap}", "board extent spacing", side,
                                        start, end, settings, TEXT_POSITION_MANUAL))
        elif len(rects) > 1 and warnings is not None:
            warnings.append("Board spacing dimensions were omitted because the placed boards do not form an unambiguous regular grid.")
    circles = []
    tooling = sorted((h for h in getattr(holes, "holes", ()) if h.diameter_mm > 0),
                     key=lambda h: ((h.at[0] - blank.min_x) ** 2 + (h.at[1] - blank.max_y) ** 2,
                                    h.designator))
    for hole in tooling:
        x, y = hole.at
        radius = hole.diameter_mm / 2
        circles.append(PcbCircle(center=(x, y), end=(x + radius, y), layer=settings.layer,
                                 stroke={"width": settings.line_thickness_mm, "type": "default"},
                                 id=str(uuid.uuid5(DIMENSION_NAMESPACE, "hole-circle:" + hole.designator))))
    if tooling:
        hole = tooling[0]
        x, y = hole.at
        for name, side, start, end in (
            ("hole_offset_x", "bottom", (blank.min_x, y), (x, y)),
            ("hole_offset_y", "left", (x, y), (x, blank.max_y)),
        ):
            if measured_nm(start, end):
                extra.append(_draft(name, "tooling hole offset from panel corner", side,
                                    start, end, settings, TEXT_POSITION_MANUAL))
        radius = hole.diameter_mm / 2
        diameter = _draft("hole_diameter_" + hole.designator, "tooling hole diameter", "bottom",
                          (x - radius, y), (x + radius, y), settings, TEXT_POSITION_MANUAL)
        counts = {}
        for item in tooling:
            value = value_text(to_nm(item.diameter_mm), settings)
            counts[value] = counts.get(value, 0) + 1
        text = ", ".join(f"{count}x ⌀{value}" for value, count in sorted(counts.items()))
        extra.append(diameter._replace(text=text))
    made = made._replace(measures=rows_of(list(made.measures) + extra, blank, settings))
    made, dimensions = arrange_annotations(made, blank, settings, warnings)
    if warnings is not None:
        declared = {layer.canonical_name for layer in board.layers}
        if made.layer not in declared:
            warnings.append(
                f"the panel does not declare layer {made.layer}, so its "
                f"{made.count} dimension(s) are on a layer KiCad keeps disabled "
                f"- it opens the file without complaint and shows nobody. Add "
                f"the layer to the design, or set panel.dimensions.layer to one "
                f"the board has"
            )
    board.drawings.extend(dimensions)
    board.drawings.extend(circles)
    board.groups.append(PcbGroup(name="eedesignpanels dimensions", id=group_id,
                                 members=[item.id for item in board.drawings[first_generated:]]))
    return made


__all__ = [
    "ARROW_ANGLE_DEG",
    "DIMENSION_LAYER",
    "DIMENSION_NAMESPACE",
    "MAX_FIXED_PRECISION",
    "MAX_PEN_RATIO",
    "OVERALL_NAMES",
    "ROW_OFFSET_RATIO",
    "ROW_PITCH_RATIO",
    "SIDES",
    "TEXT_HEIGHT_DIVISOR",
    "TEXT_HEIGHT_MIN_MM",
    "TEXT_HEIGHT_STEP_MM",
    "TEXT_POSITION_INLINE",
    "TEXT_POSITION_MANUAL",
    "TEXT_POSITION_OUTSIDE",
    "TEXT_STROKE_RATIO",
    "UNITS_FORMAT_BARE_SUFFIX",
    "UNITS_MM",
    "Measure",
    "PanelDimensions",
    "Settings",
    "apply",
    "board_drafts",
    "checked",
    "crossbar_nm",
    "dimension_for",
    "effective_pen_mm",
    "extension_height_mm",
    "instance_rects",
    "kiround",
    "longest_extent_of",
    "manual_placement",
    "measured_nm",
    "nearest_instance",
    "outside_placement",
    "plan",
    "quantized",
    "rows_of",
    "shared_corner_of",
    "signed_height",
    "text_height_for",
    "text_outset_mm",
    "text_placement",
    "to_mm",
    "to_nm",
    "value_text",
]
