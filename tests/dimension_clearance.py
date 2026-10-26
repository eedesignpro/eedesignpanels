"""Measure the gap between a dimension's lettering and everything near it.

On the per-board dimensions, the dimension text is set out from the dimension
arrows to help prevent overlap. This measures whether any dimension text
overlaps another or its own line work, so the test and the corpus run use one
answer and not two.

Nothing here draws or guesses. Both halves come from eedesigncore, which ports
KiCad's own code:

* the LINE WORK - crossbar, arrowheads, extension lines, with the knock-out
  where the text sits - from :func:`eedesigncore.render.dimensions
  .dimension_segments_mm`, a port of `PCB_DIM_ALIGNED::updateGeometry`;
* the INK - the pen-down runs of the value itself - from KiCad's stroke font
  (`GetTextAsGlyphs`), placed at the text position and angle the item carries.

A clearance is measured between PAINTED EDGES: the distance between two
centrelines less half of each one's pen. So zero is two strokes touching and a
negative number is ink printed through ink, which is the thing being refused.
"""

import math

from eedesigncore.common.font import default_font, effective_pen_width
from eedesigncore.common.transform import point_to_iu, to_mm
from eedesigncore.render.dimensions import dimension_segments_mm

from eedesignpanels.stages.panel_dimensions import crossbar_nm

# What KiCad draws a dimension's text with when the item's effects block names
# no size: `_text_polygon` in the same library, and KiCad's own default.
DEFAULT_TEXT_MM = 1.27


def text_pen_mm(dimension) -> float:
    """The pen the value is really stroked with, in millimetres.

    `EDA_TEXT::GetEffectiveTextPenWidth`, through the library's own port, so the
    clamp on a thickness typed past a quarter of the height is applied here too.
    """
    size, thickness, bold = _text_style(dimension)
    return to_mm(effective_pen_width(
        point_to_iu((thickness, 0.0))[0], point_to_iu(size), bold=bold,
    ))


def text_ink_mm(dimension) -> list:
    """The value's pen-down runs, in millimetre polylines.

    The anchor is the text position the item carries and the justification is
    centre/centre, which is what `PCB_DIMENSION_BASE::updateText` sets on every
    dimension's text.
    """
    text = dimension.text
    if text is None or not text.content:
        return []
    size, thickness, bold = _text_style(dimension)
    runs = default_font().text_strokes(
        text.content,
        point_to_iu(size),
        point_to_iu(text.position),
        point_to_iu((thickness, 0.0))[0],
        angle=float(text.angle or 0.0),
        italic=bool(getattr(text.attributes, "italic", False)),
    )
    del bold
    return [[(to_mm(x), to_mm(y)) for x, y in run] for run in runs]


def line_work_mm(dimension) -> list:
    """The dimension's own line work as millimetre segments. Never None."""
    return list(dimension_segments_mm(dimension) or ())


def line_pen_mm(dimension) -> float:
    """The pen the line work is stroked with, in millimetres."""
    return float(dimension.style.thickness or 0.0)


def parts_of(dimension) -> dict:
    """The line work split into `crossbar`, `arrows` and `extensions`.

    An outset value moves the text clear of the crossbar and the arrowheads
    but not of the extension lines, so a measurement that lumps the three
    together cannot tell whether the outset worked.

    Split by geometry rather than by order, because the writer's order is the
    library's business: the crossbar's own two ends are computed from the
    measured points and the height (`panel_dimensions.crossbar_nm`, the tested
    port of `updateGeometry`). A segment lying along that line is the crossbar
    or a knocked-out piece of it; a segment STARTING at one of its ends and
    leaving the line is an arrowhead; everything else is an extension line.
    """
    points = [tuple(point) for point in dimension.pts[:2]]
    ends = [
        (to_mm(x), to_mm(y))
        for x, y in crossbar_nm(points[0], points[1], float(dimension.height))
    ]
    out = {"crossbar": [], "arrows": [], "extensions": []}
    for segment in line_work_mm(dimension):
        on_line = all(_on_line(point, ends) for point in segment)
        if on_line:
            out["crossbar"].append(list(segment))
        elif any(_same(point, end) for point in segment for end in ends):
            out["arrows"].append(list(segment))
        else:
            out["extensions"].append(list(segment))
    return out


def _on_line(point, ends, tol: float = 1e-6) -> bool:
    """Does *point* lie on the infinite line through *ends*?"""
    (x0, y0), (x1, y1) = ends
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0.0:
        return _same(point, ends[0])
    cross = (point[0] - x0) * dy - (point[1] - y0) * dx
    return abs(cross) / length <= tol


def _same(first, second, tol: float = 1e-6) -> bool:
    return math.dist(first, second) <= tol


def clearance_mm(first_runs, first_pen: float,
                 second_runs, second_pen: float) -> float:
    """The smallest gap between two sets of strokes, painted edge to edge.

    Positive is clear space, zero is touching, negative is overlap. Every pair
    of segments is measured, because the smallest gap is not always between the
    obvious pair - a rotated value beside a crossbar is the case that proves it.
    """
    if not first_runs or not second_runs:
        return math.inf
    closest = math.inf
    for first in _segments(first_runs):
        for second in _segments(second_runs):
            closest = min(closest, _segment_distance(first, second))
    return closest - first_pen / 2.0 - second_pen / 2.0


def _segments(runs) -> list:
    """Every polyline as its segments. A one-point run is a dot, kept as one."""
    out = []
    for run in runs:
        if len(run) == 1:
            out.append((run[0], run[0]))
            continue
        out.extend(
            (run[index], run[index + 1]) for index in range(len(run) - 1)
        )
    return out


def _segment_distance(first, second) -> float:
    """The distance between two segments, zero when they cross.

    The four end-to-segment distances are the whole answer EXCEPT for a proper
    crossing - two segments that cut through each other have no endpoint near
    the other one - so that one case is tested for and everything else, touching
    and collinear and degenerate included, is measured. A degenerate segment is
    a dot, which a stroke font really does produce (the full stop in "8.5 mm"),
    and it must not be treated as crossing everything it is collinear with.
    """
    if _properly_crosses(first, second):
        return 0.0
    return min(
        _point_to_segment(first[0], second),
        _point_to_segment(first[1], second),
        _point_to_segment(second[0], first),
        _point_to_segment(second[1], first),
    )


def _point_to_segment(point, segment) -> float:
    """The distance from *point* to the nearest place on *segment*."""
    (x0, y0), (x1, y1) = segment
    dx, dy = x1 - x0, y1 - y0
    length = dx * dx + dy * dy
    if length == 0.0:
        return math.dist(point, segment[0])
    along = ((point[0] - x0) * dx + (point[1] - y0) * dy) / length
    along = max(0.0, min(1.0, along))
    return math.dist(point, (x0 + along * dx, y0 + along * dy))


def _properly_crosses(first, second) -> bool:
    """Do two segments cut THROUGH each other?

    Strictly: each segment's ends must fall on opposite sides of the other's
    line. A shared endpoint, a collinear overlap and a dot on a line are all
    false here, and all three are measured correctly by the end-to-segment
    distances instead.
    """
    if first[0] == first[1] or second[0] == second[1]:
        return False

    def side(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0]))

    d1 = side(second[0], second[1], first[0])
    d2 = side(second[0], second[1], first[1])
    d3 = side(first[0], first[1], second[0])
    d4 = side(first[0], first[1], second[1])
    return (d1 > 0.0) != (d2 > 0.0) and (d3 > 0.0) != (d4 > 0.0) and \
        d1 != 0.0 and d2 != 0.0 and d3 != 0.0 and d4 != 0.0


def _text_style(dimension) -> tuple:
    """`(size_mm, thickness_mm, bold)` for a dimension's value text."""
    attributes = getattr(dimension.text, "attributes", None)
    size = (DEFAULT_TEXT_MM, DEFAULT_TEXT_MM)
    thickness = 0.0
    bold = False
    if attributes is not None:
        if attributes.font_size:
            size = tuple(attributes.font_size)
        thickness = float(attributes.font_thickness or 0.0)
        bold = bool(attributes.bold)
    return size, thickness, bold


def measured(dimensions) -> dict:
    """Every clearance that matters, keyed by what was measured against what.

    Three questions, and each is asked separately because the answers differ:

    * `own crossbar` and `own arrows` - each value against the two parts of its
      own line work the outset is meant to clear;
    * `own extensions` - the same value against its own extension lines, which
      an outset cannot help: a value LONGER than the feature it measures runs
      past both ends of it whatever side of the crossbar it sits on;
    * `text` and `line` - each value against every other dimension's value and
      line work, which is the overlap the row pitch exists to prevent.

    Every entry is a millimetre gap between painted edges.
    """
    items = list(dimensions)
    ink = [text_ink_mm(item) for item in items]
    lines = [line_work_mm(item) for item in items]
    pens = [(text_pen_mm(item), line_pen_mm(item)) for item in items]
    out = {}
    for index, item in enumerate(items):
        for part, segments in parts_of(item).items():
            out[(f"own {part}", _key(item, index))] = clearance_mm(
                ink[index], pens[index][0], segments, pens[index][1],
            )
    for index, item in enumerate(items):
        for other, against in enumerate(items):
            if other == index:
                continue
            key = (_key(item, index), _key(against, other))
            out[("text", *key)] = clearance_mm(
                ink[index], pens[index][0], ink[other], pens[other][0],
            )
            out[("line", *key)] = clearance_mm(
                ink[index], pens[index][0], lines[other], pens[other][1],
            )
            del against
    return out


def _key(dimension, index: int) -> str:
    """A name a reader recognises: the value the dimension states."""
    content = getattr(dimension.text, "content", "") or ""
    return f"{index}:{content}"


__all__ = [
    "DEFAULT_TEXT_MM",
    "clearance_mm",
    "parts_of",
    "line_pen_mm",
    "line_work_mm",
    "measured",
    "text_ink_mm",
    "text_pen_mm",
]
