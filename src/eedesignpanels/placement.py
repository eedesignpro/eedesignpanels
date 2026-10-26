"""Where each board instance sits in the panel, and how it is turned.

Millimetre floats throughout, matching the file format and `eedesigncore`.
This module imports nothing from the library: it is the part that stays
trivially testable and is unaffected by anything KiCad does.

**A copy is a turn about the board origin, then a move** - which is exactly the
order `eedesigncore.inject_board(rotation=..., offset=...)` applies, so what
this module computes is what the injection performs and nothing re-derives it.
The turn centre is the ORIGIN and never a measured midpoint: a centre that is
not a whole number of internal units pushes every rotated coordinate off the
nanometre lattice, and the origin is the one point that cannot.

Only quarter turns exist here. KiCad's `RotatePoint` takes an exact integer
branch at 0, 90, 180 and 270 and a trigonometric one everywhere else, and a
rectangle turned by anything else is tessellated into a polygon
(`eda_shape.cpp:957-972`), so a non-cardinal panel would be neither exact nor
byte-stable. The config refuses one by name.

The four placement controls, in one arrangement:

* **rotation** - the base angle every copy takes;
* **alternation** - a further 180 degrees on every other row or column, so a
  shaped board interlocks with its neighbour;
* **nest shift** - a signed millimetre offset applied to the ALTERNATED copies
  alone, pulling them towards the copies they interlock with. Nothing is
  alternated when alternation is off, so nest shift then moves nothing;
* **clearance warning** - :func:`clearance_marks`, a measurement drawn over the
  rectangles a build already produced. It moves no coordinate.
"""

import math
from typing import NamedTuple

# The only turns a panel copy may take. See the module docstring.
ROTATIONS = (0.0, 90.0, 180.0, 270.0)

# Which cells take the extra half turn. `none` is the panel built before this
# setting existed, to the byte.
ALTERNATIONS = ("none", "rows", "columns")

# How far apart two millimetre values may be and still count as the same one.
# A hundredth of a micrometre: far under anything a fabricator holds, far over
# the float dust a chain of additions leaves.
SAME_MM_TOL = 1e-5

# How many clearance marks one page draws. A nested arrangement can produce a
# mark for every pair of boards, and a picture with four hundred labels on it
# is a picture nobody reads - so the worst ones are drawn and the count is
# stated. Page-side only; no board data depends on it.
MAX_CLEARANCE_MARKS = 64


class Placement(NamedTuple):
    """One board instance: its turn about the origin, then its offset.

    The order is load bearing and it is the order `inject_board` applies:
    `rotation_deg` about `(0, 0)`, and then `(dx_mm, dy_mm)`. `alternated` says
    whether this copy is one of the ones alternation turned a further half
    circle, which is what nest shift moves and what a report counts.
    """

    column: int
    row: int
    dx_mm: float
    dy_mm: float
    rotation_deg: float
    mirror: bool
    alternated: bool = False

    @property
    def turns(self) -> bool:
        """Does this copy actually take a turn? A whole circle is no turn."""
        return self.rotation_deg % 360.0 != 0.0


class GridExtent(NamedTuple):
    """The pitch of a grid and the extent the whole array occupies."""

    step_x_mm: float
    step_y_mm: float
    width_mm: float
    height_mm: float


class Arrangement(NamedTuple):
    """Every copy of one panel: where it sits, how it is turned, and its box.

    `boxes` are the copies' own outline rectangles in board millimetres, in the
    same order as `placements`, so a caller that needs to know where a board is
    never has to re-apply the transform. `extent` measures the union of them,
    which is the extent of the field of boards the carrier is grown from - and
    with a nest shift or a placement file that is NOT the grid arithmetic.
    """

    placements: tuple
    boxes: tuple
    extent: GridExtent
    instance_width_mm: float
    instance_height_mm: float
    origin_mm: tuple
    warnings: tuple = ()


def normalized_angle(angle: float) -> float:
    """An angle in `[0, 360)`, as `EDA_ANGLE::Normalize` gives it."""
    value = float(angle) % 360.0
    return 0.0 if value == 0.0 else value


def is_cardinal(angle: float) -> bool:
    """True for a quarter turn - the only turn a panel copy may take."""
    return float(angle) % 90.0 == 0.0


def rotated_extent(width_mm: float, height_mm: float,
                   rotation_deg: float) -> tuple:
    """How much room one instance takes once it is turned. `(width, height)`.

    A quarter turn swaps the two; a half turn leaves them. Anything else is not
    a turn this panel makes.

    Raises:
        ValueError: for a turn that is not a multiple of 90 degrees.
    """
    if not is_cardinal(rotation_deg):
        raise ValueError(
            f"a panel copy takes a quarter turn - 0, 90, 180 or 270 degrees - "
            f"not {rotation_deg:g}"
        )
    return (
        (height_mm, width_mm) if normalized_angle(rotation_deg) in (90.0, 270.0)
        else (width_mm, height_mm)
    )


def rotated_box(box: tuple, rotation_deg: float) -> tuple:
    """`(min_x, min_y, max_x, max_y)` turned about the ORIGIN by a quarter turn.

    KiCad's own mapping, with y growing downward: 90 degrees takes `(x, y)` to
    `(y, -x)`, 180 to `(-x, -y)`, 270 to `(-y, x)`
    (`libs/kimath/src/trigo.cpp:229-264`). Every step is a swap and a negation,
    so nothing rounds.

    Raises:
        ValueError: for a turn that is not a multiple of 90 degrees.
    """
    if not is_cardinal(rotation_deg):
        raise ValueError(
            f"a panel copy takes a quarter turn - 0, 90, 180 or 270 degrees - "
            f"not {rotation_deg:g}"
        )
    min_x, min_y, max_x, max_y = box
    turn = normalized_angle(rotation_deg)
    if turn == 90.0:
        return (min_y, -max_x, max_y, -min_x)
    if turn == 180.0:
        return (-max_x, -max_y, -min_x, -min_y)
    if turn == 270.0:
        return (-max_y, min_x, -min_y, max_x)
    return (min_x, min_y, max_x, max_y)


def alternates(alternate: str, column: int, row: int) -> bool:
    """Is this cell one of the ones alternation turns a further half circle?

    Every other ROW or every other COLUMN, counting the first as unturned - so a
    single-row panel with row alternation turns nothing, which is what it looks
    like on screen.

    Raises:
        ValueError: for an alternation nobody declared.
    """
    if alternate not in ALTERNATIONS:
        raise ValueError(
            f"alternation is one of {', '.join(ALTERNATIONS)} - not "
            f"{alternate!r}"
        )
    if alternate == "rows":
        return row % 2 == 1
    if alternate == "columns":
        return column % 2 == 1
    return False


def copy_angle(rotation_deg: float, alternate: str, column: int,
               row: int) -> float:
    """The whole turn one cell's copy takes: the base angle, plus alternation."""
    extra = 180.0 if alternates(alternate, column, row) else 0.0
    return normalized_angle(rotation_deg + extra)


def grid_extent(
    instance_width_mm: float,
    instance_height_mm: float,
    columns: int,
    rows: int,
    gap_mm: float,
    gap_y_mm: float = None,
) -> GridExtent:
    """Pitch and overall extent for a columns x rows array of one instance.

    The gap is the clear space between neighbouring instances, so the pitch is
    the instance extent plus the gap, and the array spans one instance plus a
    pitch per additional column or row. Which extent the caller measured -
    outline centreline or stroked outline - decides what the gap means; the
    arithmetic is the same either way.

    *gap_mm* is the ACROSS gap and *gap_y_mm* the DOWN one; None means the two
    are the same number, which is what one gap always meant.
    """
    if columns < 1 or rows < 1:
        raise ValueError(f"grid must be at least 1x1, got {columns}x{rows}")
    if instance_width_mm < 0 or instance_height_mm < 0:
        raise ValueError(
            f"instance extent must not be negative: "
            f"{instance_width_mm} x {instance_height_mm}"
        )
    gap_down = gap_mm if gap_y_mm is None else gap_y_mm
    if gap_mm < 0 or gap_down < 0:
        raise ValueError(f"gap must not be negative: {gap_mm}, {gap_down}")

    step_x = instance_width_mm + gap_mm
    step_y = instance_height_mm + gap_down
    return GridExtent(
        step_x_mm=step_x,
        step_y_mm=step_y,
        width_mm=(columns - 1) * step_x + instance_width_mm,
        height_mm=(rows - 1) * step_y + instance_height_mm,
    )


def grid_placements(
    columns: int,
    rows: int,
    step_x_mm: float,
    step_y_mm: float,
) -> list[Placement]:
    """Row-major placements for a columns x rows step-and-repeat array.

    The step is centre-to-centre pitch, so the caller adds the gap to the
    board extent before calling. Nothing here turns or mirrors anything: this
    is the plain grid, and :func:`arrangement` is the one that places a turned
    copy.
    """
    if columns < 1 or rows < 1:
        raise ValueError(f"grid must be at least 1x1, got {columns}x{rows}")
    if step_x_mm < 0 or step_y_mm < 0:
        raise ValueError(f"step must not be negative: {step_x_mm}, {step_y_mm}")

    return [
        Placement(
            column=column,
            row=row,
            dx_mm=column * step_x_mm,
            dy_mm=row * step_y_mm,
            rotation_deg=0.0,
            mirror=False,
        )
        for row in range(rows)
        for column in range(columns)
    ]


def _placed(box: tuple, column: int, row: int, angle: float,
            target: tuple, alternated: bool) -> tuple:
    """One copy: its :class:`Placement`, and the rectangle it lands in.

    *target* is where the copy's own rectangle must have its top-left corner.
    The offset is that corner less the corner the turn alone would have put it
    at, which is the whole of why nothing here needs to know about the grid.
    """
    turned = rotated_box(box, angle)
    placement = Placement(
        column=column,
        row=row,
        dx_mm=target[0] - turned[0],
        dy_mm=target[1] - turned[1],
        rotation_deg=angle,
        mirror=False,
        alternated=alternated,
    )
    landed = (
        target[0], target[1],
        target[0] + (turned[2] - turned[0]),
        target[1] + (turned[3] - turned[1]),
    )
    return placement, landed


def _union(boxes) -> tuple:
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def arrangement(
    box: tuple,
    columns: int,
    rows: int,
    gap_x_mm: float,
    gap_y_mm: float = None,
    rotation_deg: float = 0.0,
    alternate: str = "none",
    nest_across_mm: float = 0.0,
    nest_down_mm: float = 0.0,
) -> Arrangement:
    """Every copy of the panel, from all four placement controls.

    *box* is the source outline's own rectangle, `(min_x, min_y, max_x, max_y)`
    in board millimetres. **Cell (0, 0) lands where the source board's
    rectangle is**, whatever the turn, so a one-up panel with a rotation sits
    where the design sat and the panel is never re-origined.

    The pitch is the TURNED instance plus the gap - a 90 degree panel steps by
    the board's height across and its width down - and alternation adds a half
    turn, which changes no extent. A nest shift moves the alternated copies
    alone, so an arrangement with nothing alternated is unmoved by it, which is
    what "no effect when nothing is rotated" means.

    Raises:
        ValueError: for an empty grid, a negative gap, a turn that is not a
            quarter turn, or an alternation nobody declared.
    """
    if columns < 1 or rows < 1:
        raise ValueError(f"grid must be at least 1x1, got {columns}x{rows}")
    gap_down = gap_x_mm if gap_y_mm is None else gap_y_mm
    if gap_x_mm < 0 or gap_down < 0:
        raise ValueError(f"gap must not be negative: {gap_x_mm}, {gap_down}")

    width, height = box[2] - box[0], box[3] - box[1]
    instance_w, instance_h = rotated_extent(width, height, rotation_deg)
    step_x, step_y = instance_w + gap_x_mm, instance_h + gap_down

    nest = (float(nest_across_mm), float(nest_down_mm))
    placements, boxes = [], []
    for row in range(rows):
        for column in range(columns):
            alternated = alternates(alternate, column, row)
            angle = copy_angle(rotation_deg, alternate, column, row)
            target = (
                box[0] + column * step_x + (nest[0] if alternated else 0.0),
                box[1] + row * step_y + (nest[1] if alternated else 0.0),
            )
            placement, landed = _placed(box, column, row, angle, target,
                                        alternated)
            placements.append(placement)
            boxes.append(landed)

    span = _union(boxes)
    warnings = []
    if any(nest) and alternate == "none":
        warnings.append(
            f"a nest shift of {nest[0]:g} x {nest[1]:g} mm is set and nothing "
            f"is alternated, so it moves nothing - it pulls the copies "
            f"alternation turned, and this arrangement turns them all the "
            f"same way"
        )
    return Arrangement(
        placements=tuple(placements),
        boxes=tuple(boxes),
        extent=GridExtent(
            step_x_mm=step_x, step_y_mm=step_y,
            width_mm=span[2] - span[0], height_mm=span[3] - span[1],
        ),
        instance_width_mm=instance_w,
        instance_height_mm=instance_h,
        origin_mm=(span[0], span[1]),
        warnings=tuple(warnings),
    )


def placed_arrangement(box: tuple, columns: int, rows: int, rows_data) -> Arrangement:
    """The arrangement a PLACEMENT FILE states: one row per copy.

    *rows_data* is a sequence of `(across_mm, down_mm, angle_deg)` triples in
    ROW-MAJOR cell order, so the copy in cell `(column, row)` is entry
    `row * columns + column`. The cell is still the identity - it is what every
    copy's uuids are derived from - which is why the file has to carry
    exactly `columns x rows` of them and why columns and rows stay live controls
    while a file is active.

    Each offset is measured the same way a grid offset is: from the source
    board's own rectangle. So `0, 0, 0` is the design exactly where it sits, and
    a file of one such line builds the one-up panel.

    Raises:
        ValueError: for an empty grid, a row count that is not the cell count,
            or an angle that is not a quarter turn.
    """
    if columns < 1 or rows < 1:
        raise ValueError(f"grid must be at least 1x1, got {columns}x{rows}")
    entries = list(rows_data)
    if len(entries) != columns * rows:
        raise ValueError(
            f"this placement file carries {len(entries)} placement(s) and the "
            f"grid asks for {columns} x {rows} = {columns * rows}. One line per "
            f"copy - change the columns and rows, or the file."
        )

    placements, boxes = [], []
    for row in range(rows):
        for column in range(columns):
            across, down, angle = entries[row * columns + column]
            if not is_cardinal(angle):
                raise ValueError(
                    f"placement {row * columns + column + 1} turns by "
                    f"{angle:g} degrees. A panel copy takes a quarter turn - "
                    f"0, 90, 180 or 270 - because only those are exact."
                )
            angle = normalized_angle(angle)
            placement, landed = _placed(
                box, column, row, angle,
                (box[0] + float(across), box[1] + float(down)),
                alternated=False,
            )
            placements.append(placement)
            boxes.append(landed)

    span = _union(boxes)
    width, height = box[2] - box[0], box[3] - box[1]
    return Arrangement(
        placements=tuple(placements),
        boxes=tuple(boxes),
        extent=GridExtent(
            # A placed arrangement has no pitch: the file said where every copy
            # goes, so a step between two of them is not a property of the
            # panel. Reported as the extent divided by the cells it spans would
            # be arithmetic nobody can check, so it is the extent itself.
            step_x_mm=span[2] - span[0], step_y_mm=span[3] - span[1],
            width_mm=span[2] - span[0], height_mm=span[3] - span[1],
        ),
        instance_width_mm=width,
        instance_height_mm=height,
        origin_mm=(span[0], span[1]),
    )


# -- the clearance warning -------------------------------
#
# PAGE-SIDE ONLY. Nothing here reaches board data, nothing here is written to a
# file, and a change to the distance moves no coordinate: it is a measurement
# over the rectangles a build already produced, drawn over the picture.


class ClearanceMark(NamedTuple):
    """Two boards closer than the warning distance, and where they are closest."""

    first: int
    second: int
    gap_mm: float
    at: tuple
    to: tuple


def _closest_span(a: tuple, b: tuple) -> tuple:
    """The shortest span between two axis-aligned rectangles. `(gap, at, to)`.

    Zero when they touch or overlap, with the span collapsed onto the overlap,
    so an arrangement that has already collided says so rather than reporting a
    negative distance nobody can read.
    """
    left = b[0] - a[2]
    right = a[0] - b[2]
    above = b[1] - a[3]
    below = a[1] - b[3]

    if left > 0.0:
        x_gap, ax, bx = left, a[2], b[0]
    elif right > 0.0:
        x_gap, ax, bx = right, a[0], b[2]
    else:
        overlap = (max(a[0], b[0]) + min(a[2], b[2])) / 2.0
        x_gap, ax, bx = 0.0, overlap, overlap

    if above > 0.0:
        y_gap, ay, by = above, a[3], b[1]
    elif below > 0.0:
        y_gap, ay, by = below, a[1], b[3]
    else:
        overlap = (max(a[1], b[1]) + min(a[3], b[3])) / 2.0
        y_gap, ay, by = 0.0, overlap, overlap

    return (math.hypot(x_gap, y_gap), (ax, ay), (bx, by))


def clearance_marks(boxes, distance_mm: float,
                    limit: int = MAX_CLEARANCE_MARKS) -> tuple:
    """Every pair of board rectangles closer than *distance_mm*. `(marks, found)`.

    Zero disables it and returns nothing, which is the setting's own off state.
    The distance is compared STRICTLY - "closer than this distance" - so a gap
    exactly at the setting is not a warning.

    **It measures the boards' RECTANGLES**, not their outlines. The instances'
    own outlines are gone by the time a panel exists - the panel has one derived
    edge and nothing records which piece of it was which board - so the
    rectangle each copy landed in is what there is to measure, and it is what
    the page says it measured. For a rectangular board it is exact; for a shaped
    one it is the pessimistic answer, which is the safe direction for a warning.

    *found* is how many pairs qualified, which may exceed the marks returned:
    the worst *limit* are drawn and the rest are counted.
    """
    if not distance_mm or distance_mm <= 0.0:
        return (), 0
    marks = []
    for first in range(len(boxes)):
        for second in range(first + 1, len(boxes)):
            gap, at, to = _closest_span(boxes[first], boxes[second])
            if gap < distance_mm - SAME_MM_TOL:
                marks.append(ClearanceMark(first, second, gap, at, to))
    marks.sort(key=lambda mark: (mark.gap_mm, mark.first, mark.second))
    return tuple(marks[:limit]), len(marks)
