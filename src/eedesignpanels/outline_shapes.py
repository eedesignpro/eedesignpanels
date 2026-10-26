"""Every shape KiCad accepts on `Edge.Cuts`, read as offsettable primitives.

The panel builder handles ANY board outline KiCad accepts as valid. Six shapes
reach a
board file on `Edge.Cuts` - `gr_line`, `gr_arc`, `gr_rect`, `gr_circle`,
`gr_poly` and `gr_curve` - and only those six
(`pcb_io_kicad_sexpr_parser.cpp:1220-1226`). This module turns any of them
into the lines and true arcs the offset is defined for. It is the one place
that knows the shape catalogue; `stages/route_toolpath.py` and
`stages/panelize.py` both read through it, because a gap fixed in one and not
the other simply moves.

KiCad splits the six in two (`convert_shape_list_to_polygon.cpp:796-800`):

* **closed on their own** - rectangle, circle, polygon. Each is a whole
  contour, and `processClosedShape` (`:178-266`) calls `SetClosed(true)`
  unconditionally, so a polygon whose point list stops short of its first
  point is closed anyway. No winding has to be guessed: the corner order of a
  rectangle is fixed by `GetRectCorners` (`common/eda_shape.cpp:1766-1778`)
  and a polygon's comes from the file. Inside and outside are settled later,
  by nesting parity, never by direction of travel.
* **open, chained to neighbours** - segment, arc, Bezier. Handled by
  `processShapeSegment` (`:268-371`).

Two things this module does NOT do the way KiCad does, both deliberate:

* KiCad turns a rounded rectangle and a circle into many-sided polygons at the
  board's flattening budget. We emit TRUE ARCS instead, so the cut path stays
  analytic. The shape is the same shape; our version is the one KiCad's
  polygon approximates, so the two differ by at most that budget
  (`MAX_ERROR_MM`, 0.005 mm by default).
* KiCad ignores anything on `Edge.Cuts` that is not a `PCB_SHAPE` - text,
  dimensions, tables, images, zones - because its collector asks only for
  `PCB_SHAPE_T` (`:1038`, `:1252`). So do we: an unrecognised item kind
  contributes nothing rather than raising, or we would refuse boards KiCad
  builds.

Coordinates are millimetre floats throughout, matching the file and
`eedesigncore`. A footprint-hosted shape is mapped into board coordinates by
the caller's *at* callable, which is a rigid rotate-then-translate - so a
rectangle stays a rectangle and a three-point arc stays exact, whatever angle
the footprint sits at.
"""

import math

from eedesignpanels.bezier_flatten import MAX_ERROR_MM, flatten_cubic_bezier
from eedesignpanels.toolpath import TOL_MM, Line, arc_from_three_points

# An arc whose three stored points are this close to collinear is a straight
# line as far as any cutter is concerned. The toolpath writes a few of them -
# micrometre-long slivers left where two offset curves touched - and they round
# to collinear in the file's own six decimal places. Reading them as lines is
# exact to picometres and keeps a degenerate arc out of the boolean.
COLLINEAR_ARC_TOL = 1e-12

# A run shorter than the file's own coordinate grid is not a run. A rounded
# rectangle whose radius reaches half its shorter side has two edges of exactly
# zero length, and a polygon may repeat a point; either would collapse under
# `toolpath.weld_chain` and be reported as a broken outline.
MIN_RUN_MM = TOL_MM

# The six shape classes `eedesigncore` models, by class name. Duck-typed on
# purpose: the same classes serve `gr_*` and `fp_*`, and naming them here keeps
# this module free of a library import it does not otherwise need.
SHAPE_KINDS = frozenset(
    {"PcbLine", "PcbArc", "PcbRect", "PcbCircle", "PcbPoly", "PcbCurve"}
)

__all__ = [
    "COLLINEAR_ARC_TOL",
    "MAX_ERROR_MM",
    "MIN_RUN_MM",
    "SHAPE_KINDS",
    "shape_primitives",
]


def _identity(point: tuple) -> tuple:
    return point


def _line(p0: tuple, p1: tuple, source: str) -> list:
    """One straight run, or nothing when its two ends are the same point."""
    if math.dist(p0, p1) < MIN_RUN_MM:
        return []
    return [Line(p0=p0, p1=p1, source=source)]


def _arc(start: tuple, mid: tuple, end: tuple, source: str) -> list:
    """One circular run from KiCad's three-point storage.

    A three-point arc whose points are collinear is the line between its ends:
    it is what the file says, and a circumcircle through three collinear points
    is not.
    """
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    twice_area = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(twice_area) < COLLINEAR_ARC_TOL:
        return _line(start, end, source)
    return [arc_from_three_points(start, mid, end, source=source)]


def _rect_corners(start: tuple, end: tuple) -> list:
    """The four corners in KiCad's own order, `GetRectCorners`.

    `common/eda_shape.cpp:1766-1778`: `start`, `(end.x, start.y)`, `end`,
    `(start.x, end.y)`. The order is fixed by the source, so nothing here
    guesses which way round the rectangle goes.
    """
    return [start, (end[0], start[1]), end, (start[0], end[1])]


def _rectangle(item, source: str, at) -> list:
    """A `gr_rect` with no corner radius: four lines, closed back to `start`."""
    corners = [at(corner) for corner in _rect_corners(item.start, item.end)]
    runs = []
    for index, corner in enumerate(corners):
        runs.extend(_line(corner, corners[(index + 1) % 4], source))
    if len(runs) < 3:
        raise ValueError(
            f"the {source or 'rectangle'} on the board outline has no area - "
            f"its two corners are ({item.start[0]:.4f}, {item.start[1]:.4f}) "
            f"and ({item.end[0]:.4f}, {item.end[1]:.4f}) mm. KiCad rejects a "
            f"degenerate rectangle too; give it a width and a height, or draw "
            f"the edge as a line"
        )
    return runs


def _rounded_rectangle(item, source: str, at, radius: float) -> list:
    """A `gr_rect` with a corner radius: four lines and four quarter-arcs.

    Its own case, never the square-cornered path with a note. KiCad builds it
    through `ROUNDRECT( SHAPE_RECT, radius, normalize=true ).TransformToPolygon`
    (`convert_shape_list_to_polygon.cpp:226-242`), which flattens the corners
    into many short segments; we keep them as true arcs of the same radius in
    the same places, so the SHAPE matches and the deviation is the flattening
    budget KiCad would have spent (`MAX_ERROR_MM`).

    `normalize=true` is what clamps the radius to half the shorter side, so a
    radius drawn larger than the rectangle can hold becomes a stadium rather
    than an error.
    """
    (sx, sy), (ex, ey) = item.start, item.end
    x0, x1 = min(sx, ex), max(sx, ex)
    y0, y1 = min(sy, ey), max(sy, ey)
    radius = min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    if radius < MIN_RUN_MM:
        return _rectangle(item, source, at)
    # Walking the boundary: an edge, then the corner arc that ends it. Y points
    # down in the file, so this order runs clockwise on screen - which does not
    # matter to anything, per the module docstring.
    stations = [
        ((x0 + radius, y0), (x1 - radius, y0), (x1 - radius, y0 + radius),
         (x1, y0 + radius)),
        ((x1, y0 + radius), (x1, y1 - radius), (x1 - radius, y1 - radius),
         (x1 - radius, y1)),
        ((x1 - radius, y1), (x0 + radius, y1), (x0 + radius, y1 - radius),
         (x0, y1 - radius)),
        ((x0, y1 - radius), (x0, y0 + radius), (x0 + radius, y0 + radius),
         (x0 + radius, y0)),
    ]
    runs = []
    for edge_start, edge_end, center, arc_end in stations:
        runs.extend(_line(at(edge_start), at(edge_end), source))
        runs.extend(_arc(
            at(edge_end), at(_corner_mid(center, edge_end, arc_end)), at(arc_end),
            source,
        ))
    return runs


def _corner_mid(center: tuple, start: tuple, end: tuple) -> tuple:
    """The point halfway round a quarter-turn, for three-point arc storage."""
    bisector = (
        (start[0] - center[0]) + (end[0] - center[0]),
        (start[1] - center[1]) + (end[1] - center[1]),
    )
    length = math.hypot(*bisector)
    radius = math.dist(center, start)
    return (
        center[0] + radius * bisector[0] / length,
        center[1] + radius * bisector[1] / length,
    )


def _circle(item, source: str, at) -> list:
    """A `gr_circle`: two half-arcs, because three points cannot span 360.

    KiCad builds `SHAPE_ARC( center, center + (r, 0), ANGLE_360, 0 )` and
    flattens it (`convert_shape_list_to_polygon.cpp:205-223`); the radius is
    the distance from the centre to the stored perimeter point,
    `EDA_SHAPE::GetRadius`. Splitting at that perimeter point and its antipode
    keeps both halves exact and keeps the stored point on the curve.
    """
    (cx, cy), (ex, ey) = item.center, item.end
    radius = math.dist(item.center, item.end)
    if radius < MIN_RUN_MM:
        raise ValueError(
            f"the {source or 'circle'} on the board outline has no radius - "
            f"its centre and its perimeter point are the same. KiCad rejects a "
            f"zero-radius circle too; give it a radius, or delete it"
        )
    dx, dy = ex - cx, ey - cy
    near, far = (ex, ey), (cx - dx, cy - dy)
    quarter_one, quarter_two = (cx - dy, cy + dx), (cx + dy, cy - dx)
    return [
        *_arc(at(near), at(quarter_one), at(far), source),
        *_arc(at(far), at(quarter_two), at(near), source),
    ]


def _polygon(item, source: str, at) -> list:
    """A `gr_poly`: its point list walked in order, closed unconditionally.

    A `(pts …)` chain interleaves plain `(xy …)` points with `(arc …)`
    elements (`parseOutlinePoints`, `pcb_io_kicad_sexpr_parser.cpp:389-471`),
    so a rounded corner arrives as a real arc rather than as a fan of
    vertices. KiCad closes the contour whether or not the list comes back to
    its first point (`processClosedShape`, `:184-204`), so this does too.
    """
    runs = []
    first = None
    cursor = None
    for element in item.pts:
        if isinstance(element, tuple):
            point = at(element)
            if cursor is None:
                first = point
            else:
                runs.extend(_line(cursor, point, source))
            cursor = point
            continue
        start, mid, end = at(element.start), at(element.mid), at(element.end)
        if cursor is None:
            first = start
        else:
            runs.extend(_line(cursor, start, source))
        runs.extend(_arc(start, mid, end, source))
        cursor = end
    if first is None or cursor is None:
        raise ValueError(
            f"the {source or 'polygon'} on the board outline holds no points, "
            f"so there is no edge to cut. Give it corners or delete it"
        )
    runs.extend(_line(cursor, first, source))
    if len(runs) < 2:
        raise ValueError(
            f"the {source or 'polygon'} on the board outline has no area - its "
            f"points collapse to one place. Give it corners that differ"
        )
    return runs


def _curve(item, source: str, at, max_error_mm: float) -> list:
    """A `gr_curve`: flattened to lines the way KiCad flattens it.

    Not a loop - a Bezier is one of the three shapes KiCad chains to its
    neighbours (`convert_shape_list_to_polygon.cpp:321-367`), and it gets there
    through `RebuildBezierToSegmentsPointsList` at the board's max error. The
    port lives in `eedesignpanels.bezier_flatten`.
    """
    # The control points are placed FIRST and the curve flattened after, so a
    # footprint-hosted curve is chopped in board coordinates - the same points
    # KiCad's parser would have, which rotates and moves the shape at load
    # (`pcb_io_kicad_sexpr_parser.cpp:3609-3613`) and flattens later.
    points = flatten_cubic_bezier(
        [at(control) for control in item.pts], max_error_mm
    )
    runs = []
    for index in range(len(points) - 1):
        runs.extend(_line(points[index], points[index + 1], source))
    if not runs:
        raise ValueError(
            f"the {source or 'curve'} on the board outline flattens to nothing - "
            f"its four control points are the same place. Give it a shape or "
            f"delete it"
        )
    return runs


def shape_primitives(item, source: str = "", at=None,
                     max_error_mm: float = MAX_ERROR_MM) -> list:
    """One `Edge.Cuts` drawing as a list of offsettable primitives.

    *at* maps a point into board coordinates - a footprint's rotate-then-
    translate, or None for a board-level item. *source* is carried on every
    primitive the item produces, so the offset can still say which drawing a
    stretch of cut came from.

    An item that is not one of the six drawing shapes returns an empty list,
    because KiCad's outline collector asks only for `PCB_SHAPE_T` and a text
    box on `Edge.Cuts` contributes nothing to its outline either.
    """
    at = _identity if at is None else at
    kind = type(item).__name__
    if kind == "PcbLine":
        return _line(at(item.start), at(item.end), source)
    if kind == "PcbArc":
        return _arc(at(item.start), at(item.mid), at(item.end), source)
    if kind == "PcbRect":
        radius = item.corner_radius or 0.0
        if radius > 0.0:
            return _rounded_rectangle(item, source, at, radius)
        return _rectangle(item, source, at)
    if kind == "PcbCircle":
        return _circle(item, source, at)
    if kind == "PcbPoly":
        return _polygon(item, source, at)
    if kind == "PcbCurve":
        return _curve(item, source, at, max_error_mm)
    return []
