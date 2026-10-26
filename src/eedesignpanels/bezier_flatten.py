"""KiCad's own flattening of a cubic Bezier into a polyline - pure geometry.

A curve on `Edge.Cuts` is flattened the way KiCad flattens
it, not by some other subdivision that happens to look smooth. KiCad does not
keep a `gr_curve` smooth either - every consumer of one calls
`EDA_SHAPE::RebuildBezierToSegmentsPointsList` first
(`common/eda_shape.cpp:1043-1067`), which hands the four control points to
`BEZIER_POLY` and takes back a list of points. The outline extractor is one of
those consumers (`pcbnew/convert_shape_list_to_polygon.cpp:336`).

So this is a port of `libs/kimath/src/bezier_curves.cpp` (KiCad 10.0.3), the
cubic half of it: `isFlat`, `subdivide`, `eval`,
`thirdControlPointDeviation`, `numberOfInflectionPoints`,
`findInflectionPoints`, `recursiveSegmentation`, `cubicParabolicApprox` and
`getCubicPoly`. The algorithm is Hain et al., "Fast, Precise Flattening of
Cubic Bezier segments offset Curves"; the quadratic half (`getQuadPoly`) is
not ported because a `gr_curve` always carries exactly four control points
(`pcb_io_kicad_sexpr_parser.cpp:3322-3352`).

**Everything here works in integer nanometres, as KiCad does.** KiCad's
`BEZIER_POLY` holds `VECTOR2D` doubles built from `VECTOR2I` nanometres and
rounds the result back with `KiROUND` (`bezier_curves.cpp:162-173`). Feeding
it millimetres instead would change the max-error comparison by six orders of
magnitude and produce a different point count. The caller converts.

This module imports nothing but `math`.
"""

import math

# The flatness budget KiCad uses for a board shape, in millimetres: the board
# design setting `rules.max_error`, whose default is `ARC_HIGH_DEF`
# (`pcbnew/board_design_settings.cpp:214`, `:1025-1027`), and `ARC_HIGH_DEF_MM`
# is 0.005 (`include/base_units.h:119`). It is the distance between the middle
# of a flattened segment and the true curve. The value lives in the PROJECT
# file rather than the board file, so a board read on its own carries the
# default - which is what every stock board has.
MAX_ERROR_MM = 0.005

# KiCad's internal unit: one nanometre. `pcbIUScale.mmToIU`.
IU_PER_MM = 1_000_000.0

# `BEZIER_POLY::GetPoly` substitutes this for a non-positive budget
# (`bezier_curves.cpp:438-439`), in internal units.
_FALLBACK_MAX_ERROR_IU = 10.0

# `thirdControlPointDeviation` calls a control leg this short degenerate and
# returns zero deviation (`bezier_curves.cpp:280-281`). Internal units squared.
_DEGENERATE_LEG_SQ_IU = 1e-6

# Two inflection points closer together than this in parameter space are one
# (`bezier_curves.cpp:404`).
_INFLECTION_MERGE_T = 0.00001


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scale(a, k):
    return (a[0] * k, a[1] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _norm_sq(a):
    return a[0] * a[0] + a[1] * a[1]


def _lerp(a, b, t):
    return _add(a, _scale(_sub(b, a), t))


def _eval(pts, t):
    """`BEZIER_POLY::eval`, cubic branch (`bezier_curves.cpp:196-206`)."""
    omt = 1.0 - t
    omt2 = omt * omt
    omt3 = omt * omt2
    t2 = t * t
    t3 = t * t2
    return (
        omt3 * pts[0][0] + 3.0 * t * omt2 * pts[1][0]
        + 3.0 * t2 * omt * pts[2][0] + t3 * pts[3][0],
        omt3 * pts[0][1] + 3.0 * t * omt2 * pts[1][1]
        + 3.0 * t2 * omt * pts[2][1] + t3 * pts[3][1],
    )


def _is_flat(pts, max_error):
    """`BEZIER_POLY::isFlat`, cubic branch (`bezier_curves.cpp:133-155`)."""
    delta = _sub(pts[3], pts[0])
    delta_sq = _norm_sq(delta)
    # C++ divides by this without a guard and yields infinities; a closed
    # sub-curve is already the zero-length case the callers pop, so call it
    # flat rather than propagate a NaN.
    if delta_sq == 0.0:
        return True
    d21 = _sub(pts[1], pts[0])
    d31 = _sub(pts[2], pts[0])
    cross1 = _cross(delta, d21)
    cross2 = _cross(delta, d31)
    inv_delta_sq = 1.0 / delta_sq
    d1 = (cross1 * cross1) * inv_delta_sq
    d2 = (cross2 * cross2) * inv_delta_sq
    factor = 0.75 if cross1 * cross2 > 0.0 else 4.0 / 9.0
    f2 = factor * factor
    tol = max_error * max_error
    return d1 * f2 <= tol and d2 * f2 <= tol


def _subdivide(pts, t):
    """`BEZIER_POLY::subdivide`, cubic branch (`bezier_curves.cpp:304-322`)."""
    left_ctrl1 = _lerp(pts[0], pts[1], t)
    tmp = _lerp(pts[1], pts[2], t)
    left_ctrl2 = _lerp(left_ctrl1, tmp, t)
    right_ctrl2 = _lerp(pts[2], pts[3], t)
    right_ctrl1 = _lerp(tmp, right_ctrl2, t)
    shared = _lerp(left_ctrl2, right_ctrl1, t)
    left = (pts[0], left_ctrl1, left_ctrl2, shared)
    right = (shared, right_ctrl1, right_ctrl2, pts[3])
    return left, right


def _third_control_point_deviation(pts):
    """`BEZIER_POLY::thirdControlPointDeviation` (`bezier_curves.cpp:274-288`)."""
    delta = _sub(pts[1], pts[0])
    len_sq = _norm_sq(delta)
    if len_sq < _DEGENERATE_LEG_SQ_IU:
        return 0.0
    length = math.sqrt(len_sq)
    r = (pts[1][1] - pts[0][1]) / length
    s = (pts[0][0] - pts[1][0]) / length
    u = (pts[1][0] * pts[0][1] - pts[0][0] * pts[1][1]) / length
    return abs(r * pts[2][0] + s * pts[2][1] + u)


def _number_of_inflection_points(pts):
    """`BEZIER_POLY::numberOfInflectionPoints` (`bezier_curves.cpp:249-272`).

    Returns -1 for the rare case where the sign test cannot tell 0 from 2,
    exactly as the source does; the caller then measures.
    """
    d21 = _sub(pts[1], pts[0])
    d32 = _sub(pts[2], pts[1])
    d43 = _sub(pts[3], pts[2])
    cross1 = _cross(d21, d32) * _cross(d32, d43)
    cross2 = _cross(d21, d32) * _cross(d21, d43)
    if cross1 < 0.0:
        return 1
    if cross2 > 0.0:
        return 0
    b1 = _dot(d21, d32) > 0.0
    b2 = _dot(d32, d43) > 0.0
    if b1 != b2:
        return 0
    return -1


def _find_inflection_points(pts):
    """`BEZIER_POLY::findInflectionPoints` (`bezier_curves.cpp:367-432`).

    Returns `(count, t1, t2)` instead of the source's out-parameters.
    """
    ax = -pts[0][0] + 3 * pts[1][0] - 3 * pts[2][0] + pts[3][0]
    ay = -pts[0][1] + 3 * pts[1][1] - 3 * pts[2][1] + pts[3][1]
    bx = 3 * pts[0][0] - 6 * pts[1][0] + 3 * pts[2][0]
    by = 3 * pts[0][1] - 6 * pts[1][1] + 3 * pts[2][1]
    cx = -3 * pts[0][0] + 3 * pts[1][0]
    cy = -3 * pts[0][1] + 3 * pts[1][1]
    a = 3 * _cross((ax, ay), (bx, by))
    b = 3 * _cross((ax, ay), (cx, cy))
    c = _cross((bx, by), (cx, cy))
    r2 = b * b - 4 * a * c
    if r2 < 0.0 or a == 0.0:
        return 0, 0.0, 0.0
    r = math.sqrt(r2)
    t1 = (-b + r) / (2 * a)
    t2 = (-b - r) / (2 * a)
    if (0.0 < t1 < 1.0) and (0.0 < t2 < 1.0):
        if t1 > t2:
            t1, t2 = t2, t1
        if t2 - t1 > _INFLECTION_MERGE_T:
            return 2, t1, t2
        return 1, t1, t2
    if 0.0 < t1 < 1.0:
        return 1, t1, 0.0
    if 0.0 < t2 < 1.0:
        return 1, t2, 0.0
    return 0, 0.0, 0.0


def _recursive_segmentation(pts, out, threshold):
    """`BEZIER_POLY::recursiveSegmentation` (`bezier_curves.cpp:328-364`).

    The source replaces the stack's back with the RIGHT half and pushes the
    left on top, so the left half is emitted first. Kept exactly, because the
    point order is the polyline.
    """
    stack = [pts]
    while stack:
        curve = stack[-1]
        if curve[3] == curve[0]:
            stack.pop()
        elif _is_flat(curve, threshold):
            out.append(curve[3])
            stack.pop()
        else:
            left, right = _subdivide(curve, 0.5)
            stack[-1] = right
            stack.append(left)


def _cubic_parabolic_approx(pts, out, max_error):
    """`BEZIER_POLY::cubicParabolicApprox` (`bezier_curves.cpp:435-509`)."""
    curve = pts
    while True:
        if any(math.isnan(value) for point in curve for value in point):
            break
        if _is_flat(curve, max_error):
            out.append(curve[3])
            break
        deviation = _third_control_point_deviation(curve)
        # The source computes t from the deviation and falls back to recursive
        # halving when t exceeds 1. A zero deviation makes that division
        # undefined in C++ and raises here, so it takes the same fallback.
        if deviation <= 0.0:
            _recursive_segmentation(curve, out, max_error)
            break
        t = 2 * math.sqrt(max_error / (3.0 * deviation))
        if t > 1.0:
            _recursive_segmentation(curve, out, max_error)
            break
        first, rest = _subdivide(curve, t)
        if _is_flat(first, max_error):
            out.append(first[3])
        else:
            _recursive_segmentation(first, out, max_error)
        curve = rest


def _get_cubic_poly(pts, max_error):
    """`BEZIER_POLY::getCubicPoly` (`bezier_curves.cpp:511-577`)."""
    out = [pts[0]]
    if _number_of_inflection_points(pts) == 0:
        _cubic_parabolic_approx(pts, out, max_error)
        return out
    count, t1, _t2 = _find_inflection_points(pts)
    if count == 2:
        first, rest = _subdivide(pts, t1)
        _cubic_parabolic_approx(first, out, max_error)
        count, t1, _t2 = _find_inflection_points(rest)
        if count == 0:
            out.append(rest[3])
            return out
        middle, last = _subdivide(rest, t1)
        _recursive_segmentation(middle, out, max_error)
        _cubic_parabolic_approx(last, out, max_error)
    elif count == 1:
        first, rest = _subdivide(pts, t1)
        _cubic_parabolic_approx(first, out, max_error)
        _cubic_parabolic_approx(rest, out, max_error)
    else:
        _cubic_parabolic_approx(pts, out, max_error)
    return out


def kiround(value: float) -> int:
    """Round half away from zero, as KiCad `KiROUND` does."""
    return math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)


def flatten_cubic_bezier(control_points: list,
                         max_error_mm: float = MAX_ERROR_MM) -> list:
    """The four control points as the polyline KiCad would draw, in millimetres.

    Start and end are the first and last point, so the result is an OPEN run:
    a `gr_curve` is one of the three shapes KiCad chains to its neighbours
    (`convert_shape_list_to_polygon.cpp:321-367`), never a loop on its own.

    The round trip through nanometres is not incidental. KiCad's own call
    `GetPoly( vector<VECTOR2I>&, int )` rounds every output point to the
    nanometre grid (`bezier_curves.cpp:162-173`), so a point that comes back
    here is a coordinate the file could hold.
    """
    if len(control_points) != 4:
        raise ValueError(
            f"a cubic Bezier needs 4 control points, got {len(control_points)}"
        )
    pts = tuple(
        (float(kiround(x * IU_PER_MM)), float(kiround(y * IU_PER_MM)))
        for x, y in control_points
    )
    max_error = max_error_mm * IU_PER_MM
    if max_error <= 0.0:
        max_error = _FALLBACK_MAX_ERROR_IU
    return [
        (kiround(x) / IU_PER_MM, kiround(y) / IU_PER_MM)
        for x, y in _get_cubic_poly(pts, max_error)
    ]
