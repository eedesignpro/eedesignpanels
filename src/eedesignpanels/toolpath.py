"""Analytic outward offset of a closed line/arc loop - pure geometry, no I/O.

No discretization anywhere: a line offsets to a parallel line, an arc to a
concentric arc, and a corner grows a circle of the offset radius centred on the
original vertex. Everything that comes out is still a line or a true arc.

This is the cutter-CENTRE path. Offsetting the board outline outward by half the
cutter diameter puts the middle of the bit exactly on this curve, so the inside
edge of the cut lands on the outline.

**The offset is what a mill leaves, never a refusal.** A recess narrower or
tighter than the bit is a thing designers draw on purpose - a mousebite contour
is nothing else - and the machine's answer to it is not to stop but to get as
close as the bit allows and leave a fillet of exactly the bit's radius. So the
result here is the boundary of the outline's Minkowski dilation by the cutter
disc, which is the same curve Clipper and GEOS `buffer` produce, and which this
module reaches by construction rather than by approximation:

* every point of that boundary sits at distance exactly *r* from the outline,
  and every point at distance *r* from the outline lies on the offset of some
  primitive or on a circle of radius *r* around some vertex;
* so the candidate curves are exactly those, cut at every mutual crossing;
* a cut piece is on the boundary when its middle is no CLOSER than *r* to the
  outline - a piece that dips inside is material the bit cannot reach;
* what survives is walked into the outermost closed loop.

The pieces that never survive are reported rather than discarded silently:
`OffsetResult.unreachable` names each contiguous stretch of outline the cutter
cannot follow, which is what lets a caller distinguish an expected break contour
from material left standing on an edge that was supposed to be cut.

It also answers the other half of the question a router asks: where the cutting
EDGE reaches a line. A centre sitting on a tab's stop line has already cut a
radius past it, so a run ends where the centre is one radius from the line, and
:func:`stadium_intersections` is where that point comes from.

It also answers a fourth question, for anything that has to know whether two
cuts are about to meet: HOW CLOSE two curves come.
:func:`closest_approach` is the exact nearest pair of points on two lines or
arcs, and :class:`Grid` is the coarse index that keeps asking it about every
pair from costing the square of the panel.

It also answers a third question, for the saw rather than the router: which
LINES could be V-SCORED. :func:`score_candidates` is a plausible PROPOSER - a
stretch of outline qualifies when its own line, produced to the outline's
extents, meets board material nowhere except along outline collinear with it,
which is the geometric half of "a blade cannot stop inside the board". The unit
it proposes is the blade line, not the stretch: every collinear span one pass of
the saw would take comes back as one candidate. Which candidates a panel
actually scores is a decision, and it is not made here.

This module imports nothing but `math`. It knows about millimetres and plane
geometry and not about boards, which is what lets every claim in
`tests/test_toolpath.py` be checked against a closed form or against GEOS.

`OffsetError` survives for the two things that really are errors: an outline
that does not close, and an offset that cannot be walked back into a loop. It
carries the millimetre location of whatever went wrong.

Coordinates are millimetre floats in the board's own frame, which has Y pointing
DOWN. None of the maths here cares: it is plane geometry on the stored numbers,
and the orientation of the loop is measured rather than assumed.
"""

import math
from typing import Callable, NamedTuple

# A board coordinate is written to 1e-6 mm, so anything below a nanometre is
# the same point. Chaining and point identity use this; it is not a fudge
# factor for geometry that does not close.
TOL_MM = 1e-6
# Endpoints of two outline items that should meet. Looser than TOL_MM because
# a designer's outline closes to within a rounding step, not exactly.
JOIN_TOL_MM = 1e-4

# How far apart two BOARD OUTLINE ends may be and still be the same end.
# KiCad's own figure, quoted from its source; using any other one means either
# refusing a board KiCad accepts or joining two edges a designer drew apart:
#
#   // The default value for m_outlinesChainingEpsilon to convert a board
#   // outlines to polygons
#   // It is the max dist between 2 end points to see them connected
#   #define DEFAULT_CHAINING_EPSILON_MM 0.01
#                                    -- pcbnew/board.h:86-88, KiCad 10.0.3
#
# Set in the constructor at `pcbnew/board.cpp:110` and handed to every
# extraction call; the test is inclusive squared distance (`close_enough`,
# `pcbnew/convert_shape_list_to_polygon.cpp:84-87`). Nothing in the shipped
# code changes it. The gap is then HEALED rather than merely tolerated - KiCad
# moves the chain's last point onto its first (`:901-935`), which is exactly
# what :func:`weld_chain` does here.
#
# This is a DEFAULT, not a constant. The heal threshold is a setting on the
# page, and every use of it is logged. The number stays KiCad's, so the tool
# keeps accepting every board KiCad accepts; the config may lower it (zero
# refuses any gap at all) or raise it, and every heal it permits is announced
# by coordinate and distance (:class:`Heal`). The tool helps detect why a bad
# outline is bad rather than working around it, so a repair kept for KiCad
# compatibility is never a silent one.
CHAINING_EPSILON_MM = 0.01

# A corner whose round join would be shorter than this is not a corner. A
# designer's arc meets its neighbouring line tangentially, but the arc's
# endpoints are stored to a nanometre, so the two tangents disagree by a few
# microradians and their offsets separate by a fraction of a micrometre. A real
# corner separates them by a good fraction of the cutter radius - the two
# populations are three orders of magnitude apart on the corpus board - and a
# join shorter than a micrometre could not be written to the file anyway.
SNAP_TOL_MM = 1e-3

# How far a candidate may fall short of the offset distance and still count as
# reachable. Every candidate is CONSTRUCTED at exactly that distance from its
# own generator, so the only spread is floating-point noise on coordinates of a
# couple of hundred millimetres - parts in 1e13. Anything the cutter genuinely
# cannot reach falls short by micrometres or more.
REACH_TOL_MM = 1e-9

# No curve is cut within this of its own end or of another cut. Two curves that
# meet at a shallow angle cross twice, a fraction of a nanometre apart, on
# either side of the point where they touch; without this floor each such
# meeting sheds a pair of splinters that belong to no shape. It is the file's
# own coordinate grid, a thousand times finer than the shortest piece the corpus
# keeps.
SPLIT_MARGIN_MM = 1e-6

# A piece shorter than the coordinate grid cannot be written down, so it is
# dropped rather than emitted.
MIN_PIECE_MM = 1e-6

# Two ends this close are the same point in the assembled offset - one grid step
# either side of a shared end, so a piece dropped for being shorter than the
# grid still leaves its neighbours joined.
NODE_TOL_MM = 2e-6

# Two outline lines are on the same straight run when neither end of the second
# is further off the first's line than this. Straight edges in a real file are
# not exactly straight: an item drawn up to a footprint's declared point picks
# up the nanometre of rounding that point carries, which tilts it by a few
# microradians, and produced over the next item that tilt reaches ten
# nanometres. A micrometre is three orders above that dust and four below the
# thinnest feature a designer draws at an angle on purpose - measured on the
# corpus board, whose bottom edge is three items with 10 nm of drift between
# them and whose sharpest real corner is 90 degrees.
COLLINEAR_TOL_MM = 1e-3

# How far a probe has to be from the outline before "inside or outside" is a
# real answer rather than rounding. A board's coordinates sit on a 1 nm grid, so
# a line drawn along an edge can fall a nanometre inside an arc it is meant to
# be tangent to - which reads as a crossing unless contact is allowed for. Also
# the shortest stretch of probe worth asking about at all.
CONTACT_TOL_MM = 1e-3


class OffsetError(Exception):
    """The offset cannot be produced. Carries where, in board millimetres."""

    def __init__(self, message: str, at: tuple):
        super().__init__(f"{message} at ({at[0]:.4f}, {at[1]:.4f}) mm")
        self.at = at


class DoesNotFit(OffsetError):
    """The disc is too big for this chain: the offset set is empty.

    Named separately so a caller can tell "the declared
    cutter will not go in there" - which is a report about a FEATURE, such as
    1.3 mm holes against a 2 mm bit - from an offset that exists and
    cannot be walked, which is a report about this code.
    """


class Heal(NamedTuple):
    """One gap in an outline that was closed rather than met.

    Every heal is stated with where it is and how
    far apart the two ends were, because a repair nobody is told about is how a
    defective outline reaches a fab. `at` is the end the chain arrived at, in
    board millimetres; `distance_mm` is how far the next item's end sat from it.
    """

    at: tuple
    distance_mm: float

    @property
    def sentence(self) -> str:
        """The heal as the one line the page, the report and the record carry."""
        return (
            f"OUTLINE HEALED: the outline does not meet at "
            f"({self.at[0]:.4f}, {self.at[1]:.4f}) mm - its two ends are "
            f"{self.distance_mm * 1000.0:.3f} um ({self.distance_mm:.6f} mm) "
            f"apart and were closed to build it"
        )


class Line(NamedTuple):
    """A straight run, travelled p0 -> p1.

    `origin` is the index of the outline primitive this came from, or -1 for a
    corner arc that belongs to no single primitive. It is what lets the offset
    say which part of the outline the cutter never reached.
    """

    p0: tuple
    p1: tuple
    source: str = ""
    role: str = "line"
    origin: int = -1

    @property
    def start(self):
        return self.p0

    @property
    def end(self):
        return self.p1

    @property
    def length(self) -> float:
        return math.dist(self.p0, self.p1)

    def tangent_at_start(self) -> tuple:
        return _unit(_sub(self.p1, self.p0))

    def tangent_at_end(self) -> tuple:
        return self.tangent_at_start()


class Arc(NamedTuple):
    """A circular run, travelled from `a0` to `a1` about `center`.

    `ccw` is the direction of travel in the stored coordinate numbers. Angles
    are radians, unnormalised only in the sense that `a1` may be anywhere; the
    sweep is always derived, never assumed.
    """

    center: tuple
    radius: float
    a0: float
    a1: float
    ccw: bool
    source: str = ""
    role: str = "arc"
    origin: int = -1

    @property
    def sweep(self) -> float:
        """How far the arc turns, always positive."""
        delta = (self.a1 - self.a0) if self.ccw else (self.a0 - self.a1)
        return delta % (2.0 * math.pi)

    def point_at_angle(self, angle: float) -> tuple:
        return (
            self.center[0] + self.radius * math.cos(angle),
            self.center[1] + self.radius * math.sin(angle),
        )

    @property
    def start(self):
        return self.point_at_angle(self.a0)

    @property
    def end(self):
        return self.point_at_angle(self.a1)

    @property
    def mid(self):
        half = self.sweep / 2.0
        return self.point_at_angle(self.a0 + (half if self.ccw else -half))

    @property
    def length(self) -> float:
        return self.radius * self.sweep

    def tangent_at_angle(self, angle: float) -> tuple:
        sin_a, cos_a = math.sin(angle), math.cos(angle)
        return (-sin_a, cos_a) if self.ccw else (sin_a, -cos_a)

    def tangent_at_start(self) -> tuple:
        return self.tangent_at_angle(self.a0)

    def tangent_at_end(self) -> tuple:
        return self.tangent_at_angle(self.a1)

    def contains_angle(self, angle: float, slack: float = 1e-9) -> bool:
        """True when *angle* lies within the swept span."""
        delta = (angle - self.a0) if self.ccw else (self.a0 - angle)
        return (delta % (2.0 * math.pi)) <= self.sweep + slack


# ---------------------------------------------------------------------------
# vector helpers
# ---------------------------------------------------------------------------


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


def _unit(a):
    length = math.hypot(*a)
    if length == 0.0:
        raise ValueError("zero-length vector has no direction")
    return (a[0] / length, a[1] / length)


def _same(a, b, tol=JOIN_TOL_MM) -> bool:
    return math.dist(a, b) <= tol


def circumcenter(a: tuple, b: tuple, c: tuple) -> tuple:
    """Centre of the circle through three points."""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        raise OffsetError("three collinear points do not define an arc", b)
    sa, sb, sc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    return (
        (sa * (by - cy) + sb * (cy - ay) + sc * (ay - by)) / d,
        (sa * (cx - bx) + sb * (ax - cx) + sc * (bx - ax)) / d,
    )


def arc_from_three_points(start, mid, end, source="") -> Arc:
    """Build an `Arc` from KiCad's start/mid/end storage."""
    center = circumcenter(start, mid, end)
    radius = math.dist(start, center)
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    am = math.atan2(mid[1] - center[1], mid[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    two_pi = 2.0 * math.pi
    # The mid point is what says which way round: travelling counter-clockwise,
    # a mid that arrives before the end means counter-clockwise is the way.
    ccw = ((am - a0) % two_pi) < ((a1 - a0) % two_pi)
    return Arc(center=center, radius=radius, a0=a0, a1=a1, ccw=ccw, source=source)


# ---------------------------------------------------------------------------
# chaining and orientation
# ---------------------------------------------------------------------------


def chain_loops(primitives: list, tol: float = CHAINING_EPSILON_MM,
                heals: list = None) -> list:
    """Order *primitives* into closed chains, one per separate loop.

    An `Edge.Cuts` layer holds a SET of closed loops, not
    one loop, so the reader that turns items into chains says so. KiCad builds
    every closed contour it finds and then sorts outline from hole by how many
    contours enclose it - even parents is an outline, odd is a hole
    (`pcbnew/convert_shape_list_to_polygon.cpp:402-437`, 10.0.3) - and
    `BuildBoardPolygonOutlines` passes `aAllowDisjoint=true` (`:1318`), so a
    board with several separate closed shapes on `Edge.Cuts` is a board KiCad
    accepts. A reader that assumed one loop would count every other loop's
    items as leftovers and refuse the board at the point where the first loop
    CLOSED, which measures perfect and reads as a defect that is not there.

    The tolerance DEFAULTS to KiCad's, and the config may move it: a gap up to
    *tol* is a joint, because
    :data:`CHAINING_EPSILON_MM` is what KiCad calls a joint and a tighter rule
    by default would refuse boards it builds. Zero refuses any gap at all.
    `weld_chain` then closes the gap the same way KiCad does.

    Pass a list as *heals* to be told which joints were closed rather than met:
    each one comes back as a :class:`Heal`, with where it is and how far apart
    the two ends were, for the caller to state on the page, in the panel report
    and in the session record. A joint inside :data:`JOIN_TOL_MM` is the
    rounding step of a stored coordinate and is not reported.

    Loops come back in the order their first item appears in *primitives*.

    Raises:
        OffsetError: when a run of items branches, has a gap, or does not come
            back to where it started. An open run is not a loop at any
            tolerance, and no number of other loops makes it one.
    """
    remaining = list(primitives)
    loops = []
    while remaining:
        chain = [remaining.pop(0)]
        while True:
            tail = chain[-1].end
            for index, candidate in enumerate(remaining):
                if _same(candidate.start, tail, tol):
                    _record_heal(heals, tail, math.dist(candidate.start, tail))
                    chain.append(remaining.pop(index))
                    break
                if _same(candidate.end, tail, tol):
                    _record_heal(heals, tail, math.dist(candidate.end, tail))
                    chain.append(reverse(remaining.pop(index)))
                    break
            else:
                break
        closing = math.dist(chain[-1].end, chain[0].start)
        if not _same(chain[-1].end, chain[0].start, tol):
            raise OffsetError(
                f"the outline does not continue - nothing joins here, and "
                f"{len(remaining)} item(s) are left over. The nearest end is "
                f"{_span(_nearest_end(chain[-1].end, remaining, closing))} away "
                f"and the largest gap this tool will close is {_span(tol)}",
                chain[-1].end,
            )
        _record_heal(heals, chain[-1].end, closing)
        loops.append(chain)
    return loops


def _span(distance_mm: float) -> str:
    """A gap said in the two units a reader needs to judge it.

    A refusal names how far apart the ends are AND what the
    setting is, so the reader knows whether to fix the board or raise the
    number. Micrometres first because an outline defect is micrometres wide.
    """
    return f"{distance_mm * 1000.0:.3f} um ({distance_mm:.6f} mm)"


def _nearest_end(tail: tuple, remaining: list, closing: float) -> float:
    """How far the closest other outline end sits from *tail*.

    The refusal names the distance, not only the coordinate.
    Every end still in play counts, including the loop's own start - a run that
    nearly comes back on itself is the common case, and its own gap is the one
    the reader has to measure against the setting.
    """
    distances = [closing]
    for candidate in remaining:
        distances.append(math.dist(candidate.start, tail))
        distances.append(math.dist(candidate.end, tail))
    return min(distances)


def _record_heal(heals, at: tuple, distance_mm: float) -> None:
    """Note a joint that was closed rather than met, if it is worth saying.

    A heal is announced. Below :data:`JOIN_TOL_MM` it is not
    a heal - it is the rounding step a stored coordinate carries, tens of
    nanometres wide, which every real file has at every joint and which no
    designer can act on. Above it the two ends were drawn apart.
    """
    if heals is None or distance_mm <= JOIN_TOL_MM:
        return
    heals.append(Heal(at, distance_mm))


def chain_loop(primitives: list, tol: float = CHAINING_EPSILON_MM,
               heals: list = None) -> list:
    """Order *primitives* into ONE closed chain, reversing where needed.

    For a caller whose input is one loop by construction - a rail rectangle, a
    derived boundary. A caller reading a whole `Edge.Cuts` layer wants
    :func:`chain_loops` and has to say which loop it means.

    Raises:
        OffsetError: when the outline branches, has a gap, does not close, or
            is more than one loop - all of which mean the input is not the
            single closed loop this offset is defined for.
    """
    loops = chain_loops(primitives, tol, heals)
    if len(loops) > 1:
        extra = loops[1]
        raise OffsetError(
            f"this outline is {len(loops)} separate closed loops, and an "
            f"offset is defined for one",
            extra[0].start,
        )
    return loops[0]


def reverse(primitive):
    """The same geometry travelled the other way."""
    if isinstance(primitive, Line):
        return primitive._replace(p0=primitive.p1, p1=primitive.p0)
    return primitive._replace(a0=primitive.a1, a1=primitive.a0, ccw=not primitive.ccw)


def weld_chain(chain: list) -> list:
    """Close the sub-micrometre gaps at a chained loop's joints, exactly.

    Two outline items that a designer drew as meeting can still be tens of
    nanometres apart in the file - a footprint's contour is stored relative to a
    placement that was itself rounded, so the board-level line drawn up to it
    lands on the nearest nanometre rather than on the exact point. The chain
    tolerates that; the offset cannot, because it assembles by asking whether
    two ends are the SAME point.

    So each joint is welded onto one of the two ends already there. A line is
    what moves: its endpoint is a stored coordinate and moving it changes
    nothing else about it. An arc keeps its circle - it is the rigid one, and it
    is usually the footprint's authored geometry - and only gives ground when
    both sides of a joint are arcs, where the later arc's start slides along its
    own circle to the nearest point.
    """
    welded = list(chain)
    count = len(welded)
    for index in range(count):
        following = (index + 1) % count
        current, after = welded[index], welded[following]
        if current.end == after.start:
            continue
        if isinstance(after, Line):
            welded[following] = after._replace(p0=current.end)
        elif isinstance(current, Line):
            welded[index] = current._replace(p1=after.start)
        else:
            angle = math.atan2(
                current.end[1] - after.center[1], current.end[0] - after.center[0]
            )
            welded[following] = after._replace(a0=angle)
    for primitive in welded:
        if primitive.length <= 0.0:
            raise OffsetError(
                "an outline item collapsed to nothing when its ends were welded "
                "to its neighbours, so this outline is not a loop",
                primitive.start,
            )
    return welded


def signed_area(chain: list) -> float:
    """Signed area of the loop, positive when travel is counter-clockwise.

    Exact: each arc contributes its circular segment on top of the chord, so a
    loop whose shape is carried mostly by arcs still gets the right sign.
    """
    total = 0.0
    for primitive in chain:
        start, end = primitive.start, primitive.end
        total += _cross(start, end)
        if isinstance(primitive, Arc):
            sweep = primitive.sweep
            segment = primitive.radius ** 2 * (sweep - math.sin(sweep))
            total += segment if primitive.ccw else -segment
    return total / 2.0


def outward_normal(tangent: tuple, orientation: int) -> tuple:
    """The unit normal pointing OUT of the loop, given travel and orientation.

    Counter-clockwise travel (positive area) keeps the interior on the left, so
    outward is the right-hand normal; clockwise travel is the mirror.
    """
    if orientation > 0:
        return (tangent[1], -tangent[0])
    return (-tangent[1], tangent[0])


# ---------------------------------------------------------------------------
# intersections
# ---------------------------------------------------------------------------


def line_line_points(a: Line, b: Line) -> list:
    """Intersection of two INFINITE lines. Empty when parallel."""
    d1, d2 = _sub(a.p1, a.p0), _sub(b.p1, b.p0)
    denominator = _cross(d1, d2)
    if abs(denominator) < 1e-12:
        return []
    t = _cross(_sub(b.p0, a.p0), d2) / denominator
    return [_add(a.p0, _scale(d1, t))]


def line_circle_points(line: Line, center: tuple, radius: float) -> list:
    """Intersections of an INFINITE line with a full circle."""
    direction = _sub(line.p1, line.p0)
    offset = _sub(line.p0, center)
    a = _dot(direction, direction)
    b = 2.0 * _dot(offset, direction)
    c = _dot(offset, offset) - radius * radius
    discriminant = b * b - 4.0 * a * c
    if discriminant < -1e-12:
        return []
    root = math.sqrt(max(discriminant, 0.0))
    return [
        _add(line.p0, _scale(direction, t))
        for t in ((-b - root) / (2.0 * a), (-b + root) / (2.0 * a))
    ]


def circle_circle_points(c0, r0, c1, r1) -> list:
    """Intersections of two full circles."""
    separation = math.dist(c0, c1)
    if separation < 1e-12 or separation > r0 + r1 + 1e-9:
        return []
    if separation < abs(r0 - r1) - 1e-9:
        return []
    a = (r0 * r0 - r1 * r1 + separation * separation) / (2.0 * separation)
    height_squared = r0 * r0 - a * a
    height = math.sqrt(max(height_squared, 0.0))
    base = _add(c0, _scale(_sub(c1, c0), a / separation))
    if height <= 1e-12:
        return [base]
    normal = _scale(_sub(c1, c0), height / separation)
    return [
        (base[0] + normal[1], base[1] - normal[0]),
        (base[0] - normal[1], base[1] + normal[0]),
    ]


def full_intersections(one, other) -> list:
    """Every intersection of the two primitives' UNBOUNDED carriers."""
    if isinstance(one, Line) and isinstance(other, Line):
        return line_line_points(one, other)
    if isinstance(one, Line) and isinstance(other, Arc):
        return line_circle_points(one, other.center, other.radius)
    if isinstance(one, Arc) and isinstance(other, Line):
        return line_circle_points(other, one.center, one.radius)
    return circle_circle_points(one.center, one.radius, other.center, other.radius)


def on_primitive(primitive, point: tuple, tol: float = 1e-7) -> bool:
    """True when *point* lies on the primitive's own bounded span."""
    if isinstance(primitive, Line):
        direction = _sub(primitive.p1, primitive.p0)
        length_squared = _dot(direction, direction)
        if length_squared < 1e-18:
            return False
        t = _dot(_sub(point, primitive.p0), direction) / length_squared
        if t < -tol or t > 1.0 + tol:
            return False
        closest = _add(primitive.p0, _scale(direction, t))
        return math.dist(closest, point) <= tol
    if abs(math.dist(point, primitive.center) - primitive.radius) > tol:
        return False
    angle = math.atan2(point[1] - primitive.center[1], point[0] - primitive.center[0])
    return primitive.contains_angle(angle)


def bounded_intersections(one, other, tol: float = 1e-7) -> list:
    """Intersections that lie on BOTH primitives' actual spans."""
    return [
        point
        for point in full_intersections(one, other)
        if on_primitive(one, point, tol) and on_primitive(other, point, tol)
    ]


# ---------------------------------------------------------------------------
# where the cutter's EDGE reaches a line
# ---------------------------------------------------------------------------


def point_segment_distance(point: tuple, seg_a: tuple, seg_b: tuple) -> float:
    """Shortest distance from *point* to the segment, ends included."""
    direction = _sub(seg_b, seg_a)
    length_squared = _dot(direction, direction)
    if length_squared < 1e-18:
        return math.dist(point, seg_a)
    t = _dot(_sub(point, seg_a), direction) / length_squared
    t = max(0.0, min(1.0, t))
    return math.dist(point, _add(seg_a, _scale(direction, t)))


def distance_to_primitive(point: tuple, primitive) -> float:
    """Shortest distance from *point* to one line or arc, ends included."""
    if isinstance(primitive, Line):
        return point_segment_distance(point, primitive.p0, primitive.p1)
    angle = math.atan2(point[1] - primitive.center[1], point[0] - primitive.center[0])
    if primitive.contains_angle(angle):
        return abs(math.dist(point, primitive.center) - primitive.radius)
    return min(math.dist(point, primitive.start), math.dist(point, primitive.end))


def nearest_on_path(point: tuple, primitives) -> tuple:
    """The primitive index and distance of whatever in *primitives* is closest."""
    best_index, best = -1, float("inf")
    for index, primitive in enumerate(primitives):
        distance = distance_to_primitive(point, primitive)
        if distance < best:
            best_index, best = index, distance
    return best_index, best


def closest_feature(point: tuple, seg_a: tuple, seg_b: tuple) -> tuple:
    """Which part of the segment is nearest *point*, and by what margin.

    Returns `(kind, t, margin_mm)`. `kind` is "foot" when the perpendicular
    drops inside the segment and "endpoint" when it falls off an end; `t` is
    where along the segment the nearest point sits; `margin_mm` is how far that
    nearest point is from the closer END of the segment - the distance the case
    has left before it flips.

    This is reported rather than branched on. The distance itself is continuous
    across the flip, which is the whole reason
    :func:`stadium_intersections` intersects the caps and the flanks together
    and lets the measured distance decide.
    """
    direction = _sub(seg_b, seg_a)
    length_squared = _dot(direction, direction)
    if length_squared < 1e-18:
        return ("endpoint", 0.0, 0.0)
    raw = _dot(_sub(point, seg_a), direction) / length_squared
    t = max(0.0, min(1.0, raw))
    length = math.sqrt(length_squared)
    kind = "foot" if 0.0 < t < 1.0 else "endpoint"
    return (kind, t, min(t, 1.0 - t) * length)


def stadium_intersections(primitive, seg_a: tuple, seg_b: tuple,
                          radius: float, tol: float = 1e-9) -> list:
    """Points of *primitive* whose distance to the segment is exactly *radius*.

    The cutter is a disc, so the moment its edge touches a stop line is the
    moment its CENTRE is one radius away from that line - and "away from a
    segment" is not "away from the infinite line it lies on". The locus is the
    boundary of the segment's Minkowski sum with the disc: a stadium, made of
    the two parallel offsets of the segment and a circular cap at each end.

    All four pieces are intersected as unbounded carriers and every candidate is
    then checked against the real distance, which is what makes the caps and the
    flanks hand over correctly at the segment's ends without any case analysis.
    """
    unit = _unit(_sub(seg_b, seg_a))
    normal = (-unit[1], unit[0])

    candidates = []
    for side in (radius, -radius):
        shift = _scale(normal, side)
        flank = Line(p0=_add(seg_a, shift), p1=_add(seg_b, shift))
        candidates.extend(full_intersections(primitive, flank))
    for cap in (seg_a, seg_b):
        circle = Arc(center=cap, radius=radius, a0=0.0, a1=0.0, ccw=True)
        candidates.extend(full_intersections(primitive, circle))

    found = []
    for point in candidates:
        if not on_primitive(primitive, point, 1e-7):
            continue
        if abs(point_segment_distance(point, seg_a, seg_b) - radius) > tol:
            continue
        if not any(math.dist(point, kept) <= tol for kept in found):
            found.append(point)
    return found


# ---------------------------------------------------------------------------
# the offset itself
# ---------------------------------------------------------------------------


def parameter_on(primitive, point: tuple) -> float:
    """Where *point* sits along *primitive*, as a fraction from 0 to 1."""
    if isinstance(primitive, Line):
        span = math.dist(primitive.p0, primitive.p1)
        return math.dist(primitive.p0, point) / span if span else 0.0
    angle = math.atan2(point[1] - primitive.center[1], point[0] - primitive.center[0])
    delta = (angle - primitive.a0) if primitive.ccw else (primitive.a0 - angle)
    sweep = primitive.sweep
    return (delta % (2.0 * math.pi)) / sweep if sweep else 0.0


def sub_primitive(primitive, start_u: float, end_u: float):
    """The piece of *primitive* between two fractional parameters."""
    if isinstance(primitive, Line):
        dx = primitive.p1[0] - primitive.p0[0]
        dy = primitive.p1[1] - primitive.p0[1]
        return primitive._replace(
            p0=(primitive.p0[0] + dx * start_u, primitive.p0[1] + dy * start_u),
            p1=(primitive.p0[0] + dx * end_u, primitive.p0[1] + dy * end_u),
        )
    sign = 1.0 if primitive.ccw else -1.0
    sweep = primitive.sweep
    return primitive._replace(
        a0=primitive.a0 + sign * sweep * start_u,
        a1=primitive.a0 + sign * sweep * end_u,
    )


def midpoint_of(primitive) -> tuple:
    """The point halfway along *primitive*, by its own parameter."""
    if isinstance(primitive, Line):
        return (
            (primitive.p0[0] + primitive.p1[0]) / 2.0,
            (primitive.p0[1] + primitive.p1[1]) / 2.0,
        )
    return primitive.mid


class Unreachable(NamedTuple):
    """One contiguous stretch of outline the cutter's edge never touches.

    `at` is a point on that stretch, in board millimetres. `fillet_mm` is the
    radius of the fillet the bit leaves instead - always the cutter radius,
    because that is the tightest inside corner a round tool can cut.
    """

    at: tuple
    items: int
    span_mm: float
    fillet_mm: float
    sources: tuple


class OffsetResult(NamedTuple):
    """The offset loop, plus what the cutter could not reach getting there."""

    loop: list
    unreachable: list


def offset_primitive(primitive, distance: float, orientation: int, inward=False):
    """One primitive moved *distance* off its own line, outward unless told.

    Returns None when a concave arc is tighter than the offset: the whole of
    that arc is then inside the dilated region and contributes no boundary,
    which is precisely the bit not fitting into the recess.
    """
    sign = -1.0 if inward else 1.0
    if isinstance(primitive, Line):
        normal = outward_normal(primitive.tangent_at_start(), orientation)
        shift = _scale(normal, distance * sign)
        return primitive._replace(
            p0=_add(primitive.p0, shift), p1=_add(primitive.p1, shift)
        )

    normal = outward_normal(primitive.tangent_at_start(), orientation)
    radial = _unit(_sub(primitive.start, primitive.center))
    grows = (_dot(normal, radial) > 0.0) == (not inward)
    radius = primitive.radius + distance if grows else primitive.radius - distance
    if radius <= TOL_MM:
        return None
    return primitive._replace(radius=radius)


def corner_arc(chain: list, index: int, distance: float,
               orientation: int):
    """The circle of radius *distance* about one vertex, cut at its two normals.

    Every point at *distance* from a vertex is a candidate for the offset, and
    the two ends of that circle which matter are where the neighbouring
    primitives' own offsets arrive. Cutting there rather than trusting an
    intersection keeps the tangency exact - an offset line meets a vertex circle
    at exactly one point, which is the numerically worst way to ask.

    The arc returned runs the way the cutter walks round a convex corner; the
    REST of that circle is generated with it in :func:`candidate_curves`.
    Which of the two is real is not decided here: the distance test decides, and
    it is the definition rather than a rule of thumb.

    It turns the way the LOOP turns, which is what makes a corner that doubles
    back on itself come out right. At a feather edge the two tangents are
    exactly opposite and "the shorter way round" is a coin toss, but the
    direction of travel never is: the cutter walks round the tip, not back over
    the material it just left.

    None means there is no corner: two items that meet closely enough to
    tangential that walking round would be a journey of under a micrometre are a
    continuation, and their offsets are welded instead.
    """
    count = len(chain)
    current, following = chain[index], chain[(index + 1) % count]
    vertex = current.end
    before = outward_normal(current.tangent_at_end(), orientation)
    after = outward_normal(following.tangent_at_start(), orientation)
    a0 = math.atan2(before[1], before[0])
    a1 = math.atan2(after[1], after[0])
    delta = (a1 - a0 + math.pi) % (2.0 * math.pi) - math.pi
    if abs(delta) * distance <= SNAP_TOL_MM:
        return None
    return Arc(
        center=vertex, radius=distance, a0=a0, a1=a1, ccw=orientation > 0,
        source=f"{current.source}>{following.source}", role="join", origin=-1,
    )


def split_primitive(primitive, points: list) -> list:
    """*primitive* cut at every one of *points* that lies inside its span."""
    span = primitive.length
    if span <= MIN_PIECE_MM:
        return []
    margin = SPLIT_MARGIN_MM / span
    cuts = sorted(
        u for u in (parameter_on(primitive, point) for point in points)
        if margin < u < 1.0 - margin
    )
    if not cuts:
        return [primitive]
    pieces, previous = [], 0.0
    for cut in (*cuts, 1.0):
        if cut - previous > margin:
            pieces.append(sub_primitive(primitive, previous, cut))
            previous = cut
    return pieces


def weld_start(primitive, point: tuple):
    """*primitive* with its start moved onto *point*.

    A line simply takes the new endpoint, which is exact. An arc slides its
    start along its own circle: that absorbs the whole of a gap running ALONG
    the curve and leaves only the part across it, a second-order remainder. The
    radius is never touched - an offset arc is at the offset distance from its
    outline arc by construction, and nudging its radius to close a gap would
    make that false, which the reachability test would then read as the cutter
    not fitting.
    """
    if isinstance(primitive, Line):
        return primitive._replace(p0=point)
    return primitive._replace(a0=math.atan2(
        point[1] - primitive.center[1], point[0] - primitive.center[0]
    ))


def weld_end(primitive, point: tuple):
    """*primitive* with its END moved onto *point*. See :func:`weld_start`."""
    if isinstance(primitive, Line):
        return primitive._replace(p1=point)
    return primitive._replace(a1=math.atan2(
        point[1] - primitive.center[1], point[0] - primitive.center[0]
    ))


def weld_gap(before, other) -> tuple:
    """Close the gap a REMOVED piece left between two curves. Returns the pair.

    The same discipline as :func:`_weld_pair`, which is where the reasoning is
    written out: an arc gives ground first, because sliding its end along its
    own circle moves the end without moving the circle, and then a LINE closes
    whatever radial remainder is left - a remainder no amount of sliding can
    reach. Two arcs keep it, and it is second order in the gap.

    The difference is the ceiling. `_weld_pair` welds two curves that were
    always meant to meet and refuses anything wider than a tangency error;
    this one is handed a gap its caller created by dropping a piece of dust,
    and leaving it open would break the run where the piece was taken out.

    Exactness matters more here than the distance does: two ends a few
    nanometres apart are two nodes to anything that dilates this path, and a
    boundary cannot pass through a node of degree four.
    """
    if math.dist(before.end, other.start) == 0.0:
        return before, other
    if isinstance(other, Arc):
        slid = weld_start(other, before.end)
        if isinstance(before, Arc):
            return before, slid
        return weld_end(before, slid.start), slid
    return before, weld_start(other, before.end)


def _weld_pair(before, other) -> tuple:
    """Close the gap between two curves meant to meet.

    Returns `(before, other, moved_before, moved_other)`, where each `moved` is
    how far that curve's end had to travel - what the reachability test has to
    forgive it.

    An ARC gives ground first, always: sliding an arc's end along its own circle
    moves the end without moving the circle, so the curve is still exactly the
    offset distance from the outline it came from. But sliding is only free
    ALONG the circle. Where the two outline items meet a shade off tangential -
    a designer's fillet whose stored endpoints leave it a few milliradians out -
    the gap has a radial component too, and no amount of sliding closes that.
    What is left is second order in the gap and lands in the nanometres: 3.9 nm
    on the corpus board's 0.4985 mm fillet at a 0.3 mm cutter radius. Small, and
    still fatal - the assembly asks whether two ends are the SAME point, and a
    node three nanometres wide is two nodes. That was the whole of the 0.5 and
    0.6 mm deadlock measured on that fillet.

    So the LINE closes what is left. Moving a line's endpoint costs real
    clearance, which is exactly why it is done second and reported: the arc
    absorbs everything it can for free, and the line pays only the remainder.
    Two arcs have no line to give ground, so they keep the residue - and it is
    below the node tolerance, because neither end moved far enough to matter.
    """
    gap = math.dist(before.end, other.start)
    if gap == 0.0 or gap > SNAP_TOL_MM * 2.0:
        return before, other, 0.0, 0.0
    if isinstance(other, Arc) and isinstance(before, Arc):
        return before, weld_start(other, before.end), 0.0, 0.0
    if isinstance(other, Arc):
        slid = weld_start(other, before.end)
        residue = math.dist(before.end, slid.start)
        return weld_end(before, slid.start), slid, residue, 0.0
    if isinstance(before, Arc):
        slid = weld_end(before, other.start)
        residue = math.dist(slid.end, other.start)
        return slid, weld_start(other, slid.end), 0.0, residue
    return before, weld_start(other, before.end), 0.0, gap


def candidate_curves(chain: list, distance: float, orientation: int) -> tuple:
    """Every curve the offset boundary can be made of, and every curve that cuts it.

    The boundary lives at distance *r* from the outline, so it is a subset of
    the outward offsets and the corner circles. The INWARD offsets are on that
    same level set without ever being boundary, and they are the only other
    thing that can end a boundary piece, so they are carried purely as knives.

    Where two outline items meet TANGENTIALLY there is no corner arc between
    their offsets, and the two offsets miss each other: both are the vertex plus
    the offset distance along a normal, and the two normals agree only to within
    the tangency the file's nanometre grid allows. That gap is a fraction of a
    micrometre and it is not geometry, so it is welded shut here, exactly as the
    outline's own joints were. Two passes, because the last joint welds onto the
    first curve, whose end the first joint had already used.

    Returns `(keepable, slack, knives)`: `slack[i]` is how far `keepable[i]` was
    displaced by welding, which is what the reachability test must forgive it.
    """
    count = len(chain)
    offsets, knives = [], []
    for index, item in enumerate(chain):
        outward = offset_primitive(item, distance, orientation)
        offsets.append(None if outward is None else outward._replace(origin=index))
        inward = offset_primitive(item, distance, orientation, inward=True)
        if inward is not None:
            knives.append(inward._replace(origin=index, role="inward"))
    joins = [corner_arc(chain, index, distance, orientation) for index in range(count)]

    # The offsets and the corner arcs alternate round the loop; where an item's
    # offset collapsed or a corner is a continuation, that entry is simply absent
    # and its neighbours are left to the arrangement rather than welded.
    order = []
    for index in range(count):
        if offsets[index] is not None:
            order.append(("offset", index))
        if joins[index] is not None:
            order.append(("join", index))
    if not order:
        return [], [], knives

    def read(tag):
        return offsets[tag[1]] if tag[0] == "offset" else joins[tag[1]]

    def write(tag, value):
        if tag[0] == "offset":
            offsets[tag[1]] = value
        else:
            joins[tag[1]] = value

    slack = dict.fromkeys(order, 0.0)
    for _ in range(2):
        for position, tag in enumerate(order):
            following = order[(position + 1) % len(order)]
            if following == tag:
                continue
            before, other, moved_before, moved_other = _weld_pair(
                read(tag), read(following)
            )
            write(tag, before)
            write(following, other)
            slack[tag] = max(slack[tag], moved_before)
            slack[following] = max(slack[following], moved_other)

    keepable, slacks = [], []
    for tag in order:
        curve = read(tag)
        keepable.append(curve)
        slacks.append(slack[tag])
        if tag[0] == "join":
            keepable.append(curve._replace(
                a0=curve.a1, a1=curve.a0, role="joinback"
            ))
            slacks.append(0.0)
    return keepable, slacks, knives


def _reachable_pieces(chain: list, distance: float, orientation: int,
                      inside_only: bool = False) -> list:
    """Cut every candidate at every crossing and keep what the cutter reaches.

    *inside_only* additionally drops every piece that is not INSIDE the chain,
    and it is what an eroded offset needs, because every closed loop is a
    routing job. The distance test alone is blind to the side: the circle
    about a vertex is at exactly *distance* from the outline all the way round,
    so the far half of it - the half outside the loop - passes a test that only
    asks how far away it is. A dilation never had to care, because the pieces on
    the other side of a vertex are inside the material and the walk closes
    before it reaches them. An erosion does. The dilation path is left exactly
    as it was rather than made symmetrical: it is what every board this tool has
    cut was cut by, and the corpus is byte-identical only while it is untouched.
    """
    keepable, slacks, knives = candidate_curves(chain, distance, orientation)
    every = keepable + knives

    crossings = [[] for _ in keepable]
    for index, curve in enumerate(keepable):
        for other_index, other in enumerate(every):
            if other is curve:
                continue
            if other_index < len(keepable) and other_index < index:
                continue  # the pair was done from the other side
            for point in bounded_intersections(curve, other, tol=1e-7):
                crossings[index].append(point)
                if other_index < len(keepable):
                    crossings[other_index].append(point)

    reachable = []
    for curve, points, give in zip(keepable, crossings, slacks):
        for piece in split_primitive(curve, points):
            if piece.length <= MIN_PIECE_MM:
                continue
            middle = midpoint_of(piece)
            _, clearance = nearest_on_path(middle, chain)
            if clearance < distance - REACH_TOL_MM - give:
                continue
            if inside_only and not point_inside_loop(chain, middle):
                continue
            reachable.append(piece)
    return reachable


def _extreme_piece(pieces: list) -> int:
    """The index of the piece reaching furthest left - certainly on the outside.

    A leftmost point of the assembled offset cannot belong to an enclosed loop,
    so starting the walk there starts it on the outer boundary.
    """
    def leftmost(piece) -> tuple:
        if isinstance(piece, Line):
            x = min(piece.p0[0], piece.p1[0])
            y = min(piece.p0[1], piece.p1[1])
        else:
            x = min(piece.start[0], piece.end[0])
            y = min(piece.start[1], piece.end[1])
            if piece.contains_angle(math.pi):
                x = piece.center[0] - piece.radius
        return (x, y)

    return min(
        range(len(pieces)),
        key=lambda index: (*leftmost(pieces[index]), pieces[index].source,
                           pieces[index].role, index),
    )


def node_of(nodes: list, point: tuple, tol: float = NODE_TOL_MM) -> int:
    """The index of *point* among the assembly's shared ends, adding it if new.

    The first coordinate seen for a node is the one every later end welds onto,
    so the answer depends on the order of the input and on nothing else.
    """
    for index, existing in enumerate(nodes):
        if math.dist(existing, point) <= tol:
            return index
    nodes.append(point)
    return len(nodes) - 1


def _walk_outer_loop(pieces: list, orientation: int) -> list:
    """Chain the reachable pieces into the outermost closed loop.

    Every junction is normally a meeting of exactly two pieces - the offset
    boundary is a curve, not a graph. Where four pieces meet, the region has
    pinched to a point, and the way through is the turn that keeps the material
    on the same side it has been on all along: the sharpest turn towards the
    inside.
    """
    nodes = []
    starts = [node_of(nodes, piece.start) for piece in pieces]
    ends = [node_of(nodes, piece.end) for piece in pieces]
    leaving = {}
    for index, node in enumerate(starts):
        leaving.setdefault(node, []).append(index)

    begin = _extreme_piece(pieces)
    order, seen, current = [], set(), begin
    while True:
        order.append(current)
        seen.add(current)
        options = [
            index for index in leaving.get(ends[current], []) if index != current
        ]
        if not options:
            raise OffsetError(
                "the offset path stops without closing, so this outline cannot "
                "be walked round",
                pieces[current].end,
            )
        if len(options) == 1:
            following = options[0]
        else:
            incoming = pieces[current].tangent_at_end()
            following = max(options, key=lambda index: orientation * math.atan2(
                _cross(incoming, pieces[index].tangent_at_start()),
                _dot(incoming, pieces[index].tangent_at_start()),
            ))
        if following == begin:
            break
        if following in seen:
            raise OffsetError(
                "the offset path rejoins itself instead of closing",
                pieces[following].start,
            )
        current = following
    return [pieces[index] for index in order]


def merge_same_curve(loop: list) -> list:
    """Put back together any curve the arrangement cut and then kept whole.

    Two curves that touch without crossing - an offset line meeting the corner
    circle it is tangent to, say - cross twice a picometre apart in floating
    point, so the cut happens and both sides survive it. Nothing about the
    geometry changed, and a splinter of no length has no business in a board
    file, so consecutive pieces of the SAME curve are welded back into one. This
    moves no endpoint: it deletes a join that was never there.
    """
    if len(loop) < 2:
        return loop

    def same_curve(one, other) -> bool:
        if type(one) is not type(other):
            return False
        if (one.source, one.role, one.origin) != (
            other.source, other.role, other.origin
        ):
            return False
        if isinstance(one, Line):
            return (
                math.dist(one.p1, other.p0) <= NODE_TOL_MM
                and abs(_cross(one.tangent_at_end(), other.tangent_at_start()))
                <= 1e-9
                and _dot(one.tangent_at_end(), other.tangent_at_start()) > 0.0
            )
        return (
            one.ccw == other.ccw
            and math.dist(one.center, other.center) <= NODE_TOL_MM
            and abs(one.radius - other.radius) <= NODE_TOL_MM
            and math.dist(one.end, other.start) <= NODE_TOL_MM
        )

    merged = [loop[0]]
    for piece in loop[1:]:
        previous = merged[-1]
        if same_curve(previous, piece):
            merged[-1] = (
                previous._replace(p1=piece.p1) if isinstance(piece, Line)
                else previous._replace(a1=piece.a1)
            )
        else:
            merged.append(piece)
    # The loop is cyclic, so the last piece may continue the first.
    if len(merged) > 2 and same_curve(merged[-1], merged[0]):
        merged[0] = (
            merged[0]._replace(p0=merged[-1].p0) if isinstance(merged[0], Line)
            else merged[0]._replace(a0=merged[-1].a0)
        )
        merged.pop()
    return merged


def _start_at_first_outline_item(loop: list, chain: list) -> list:
    """Rotate *loop* so it begins where the outline itself begins.

    The walk starts wherever the geometry is leftmost, which is not a stable
    place to number items from. Beginning at the earliest surviving piece of the
    earliest surviving outline item is, and it keeps a board whose offset needs
    no resolution numbering its output exactly as it always did.
    """
    for index in range(len(chain)):
        candidates = [
            position for position, piece in enumerate(loop)
            if piece.origin == index and piece.role != "join"
        ]
        if candidates:
            item = chain[index]
            first = min(candidates, key=lambda position: _along(item, loop[position]))
            return loop[first:] + loop[:first]
    return loop


def _along(item, piece) -> float:
    """How far along *item* the offset *piece* starts - the numbering order."""
    if isinstance(item, Line):
        return _dot(_sub(piece.start, item.p0), _unit(_sub(item.p1, item.p0)))
    angle = math.atan2(piece.start[1] - item.center[1], piece.start[0] - item.center[0])
    delta = (angle - item.a0) if item.ccw else (item.a0 - angle)
    return delta % (2.0 * math.pi)


def _unreachable_runs(chain: list, loop: list, distance: float) -> list:
    """Every contiguous stretch of outline no piece of *loop* was offset from."""
    reached = {piece.origin for piece in loop if piece.role != "join"}
    count = len(chain)
    missing = [index for index in range(count) if index not in reached]
    if not missing or len(missing) == count:
        groups = [missing] if missing else []
    else:
        # Cyclic grouping: rotate so the list starts after a reached item, then
        # a group is simply a run of consecutive indices.
        first = next(index for index in range(count) if index in reached)
        ordered = [(first + step) % count for step in range(count)]
        groups, run = [], []
        for index in ordered:
            if index in reached:
                if run:
                    groups.append(run)
                    run = []
            else:
                run.append(index)
        if run:
            groups.append(run)

    runs = []
    for group in groups:
        span = sum(chain[index].length for index in group)
        middle = chain[group[len(group) // 2]]
        runs.append(Unreachable(
            at=midpoint_of(middle),
            items=len(group),
            span_mm=span,
            fillet_mm=distance,
            sources=tuple(chain[index].source for index in group),
        ))
    return runs


class Corner(NamedTuple):
    """One vertex the tool rounds off instead of cutting square.

    `radius_mm` is always the cutter radius, because that is the tightest inside
    corner a round tool can leave. `turn_deg` is how far the outline turns at
    that vertex, signed the way the loop travels.
    """

    at: tuple
    turn_deg: float
    radius_mm: float
    sources: tuple


def fillet_corners(chain: list, distance: float, orientation: int) -> list:
    """Every inside corner of *chain* the tool cannot reach into, so it comes out round.

    An inside corner tighter than the cutter's radius cannot
    be reached, so the corner comes out rounded to the tool - and that is a
    thing to report, not a refusal.

    **AN INSIDE CORNER IS NOT ALWAYS A VERTEX.** A concave ARC tighter than the
    tool is the same fact. A test that asks only what the outline does AT a
    vertex misses it: a designer who draws a 0.25 mm inside radius instead of a
    sharp corner joins it tangentially, so the turn at both its ends is
    microradian dust. MEASURED on a camera board with a stepped edge at a
    1.2 mm cutter: 16 boundary items, no vertex turning more than 0.010
    degrees, four concave arcs of 0.25 and 0.5838 mm radius - every one of them
    a corner the bit rounds off, and a vertex-only test finds none. The test is
    :func:`offset_primitive` answering None, which is that function's own
    statement that the whole arc lies inside the dilation and contributes no
    boundary; asking it there rather than comparing radii here keeps the corner
    test and the offset from disagreeing about one arc.

    *orientation* is the RIDE SIDE, the same number
    :func:`resolve_offset` hands its machinery: the loop's own orientation when
    the tool rides outside what the loop encloses, and the negative of it when
    the tool rides inside. A corner is reached when the tool wraps round the
    OUTSIDE of the turn and rounded when it is on the inside of it, which is one
    sign test: the tool is on the inside of the turn when the outline turns away
    from the side the tool rides.

    The threshold is the one :func:`corner_arc` already uses to decide that two
    items are a continuation rather than a corner - a turn that moves the tool
    less than a micrometre is not a corner anybody drew.
    """
    corners = []
    count = len(chain)
    for index in range(count):
        current, following = chain[index], chain[(index + 1) % count]
        # THE ARC ITSELF FIRST, then the vertex at its end, so the corners come
        # out in the order the outline is walked (a reader matching
        # a red mark on the picture to a line in the list reads them in that
        # order). `at` is the arc's MIDDLE because the whole arc is the corner,
        # not one of its ends, and `turn_deg` is its own sweep signed the way
        # the loop travels - the same sign convention the vertex case uses.
        if (isinstance(current, Arc)
                and offset_primitive(current, distance, orientation) is None):
            corners.append(Corner(
                at=midpoint_of(current),
                turn_deg=(math.degrees(current.length / current.radius)
                          * (1.0 if current.ccw else -1.0)),
                radius_mm=distance,
                sources=(current.source,),
            ))
        before = current.tangent_at_end()
        after = following.tangent_at_start()
        turn = math.atan2(_cross(before, after), _dot(before, after))
        if abs(turn) * distance <= SNAP_TOL_MM:
            continue
        if orientation * turn >= 0.0:
            continue
        corners.append(Corner(
            at=current.end,
            turn_deg=math.degrees(turn),
            radius_mm=distance,
            sources=(current.source, following.source),
        ))
    return corners


def resolve_offset(chain: list, distance: float,
                   inward: bool = False) -> OffsetResult:
    """Offset a closed chain by *distance*, the way a mill would.

    The result is the boundary of the chain's Minkowski dilation by a disc of
    that radius: true lines and true arcs, no discretization, and a fillet of
    exactly *distance* wherever the disc could not follow the outline in.

    *inward* offsets into what the chain encloses instead - the EROSION by the
    same disc, which is the path a cutter takes round the inside of a cutout.

    EVERY CLOSED LOOP IS A ROUTING JOB, and which side the
    tool rides on is decided by where the material is
    (:mod:`eedesignpanels.material`), so this has to be able to go either way.
    One sign does it, and nothing else here changes: every curve this builds is
    placed by `outward_normal(tangent, orientation)`, so handing the machinery
    the OPPOSITE orientation puts the same level set on the other side of the
    same chain. The reachability test is a distance from the chain and is blind
    to the side, the corner arcs come out spanning the other pair of normals -
    which is exactly the fillet an eroded region wants at a corner the disc
    cannot enter - and every arc turns the way the walk then reads. What was
    the dilation's boundary is the erosion's.

    Raises:
        OffsetError: only for an outline that is not a loop, or an offset that
            cannot be walked closed. Geometry the cutter cannot reach is
            reported, not refused.
    """
    welded = weld_chain(chain)
    orientation = 1 if signed_area(welded) > 0.0 else -1
    if inward:
        orientation = -orientation
    pieces = _reachable_pieces(
        welded, distance, orientation, inside_only=inward)
    if not pieces:
        raise DoesNotFit(
            f"nothing of this outline is more than {distance:.4f} mm from "
            f"itself, so a cutter that size has nowhere to go",
            welded[0].start,
        )
    loop = _start_at_first_outline_item(
        merge_same_curve(_walk_outer_loop(pieces, orientation)), welded
    )
    return OffsetResult(
        loop=loop, unreachable=_unreachable_runs(welded, loop, distance)
    )


def offset_loop(chain: list, distance: float, inward: bool = False) -> list:
    """Just the offset loop - :func:`resolve_offset` without its report."""
    return resolve_offset(chain, distance, inward=inward).loop


def foot_on_line(line: Line, point: tuple) -> tuple:
    """Where the perpendicular from *point* meets the line's own INFINITE line."""
    direction = _sub(line.p1, line.p0)
    length_squared = _dot(direction, direction)
    if length_squared < 1e-18:
        return line.p0
    along = _dot(_sub(point, line.p0), direction) / length_squared
    return _add(line.p0, _scale(direction, along))


def contact_curve(piece, chain: list):
    """The curve the cutter's CONTACT POINT traces while its centre walks *piece*.

    The bit is a disc rolling along the outline, and the point where it touches
    that outline is where the cut takes the board edge to full depth. That point
    is NOT the centreline: it is one radius off it, along the normal into the
    material - so the two agree about how far past a plane the cut reaches only
    where the run drives square at that plane.

    Every piece of a resolved offset is the parallel of one outline item or the
    circle about one vertex, and `origin` says which, so the contact curve is
    exact and carries the SAME parameterisation as the piece: a fraction along
    the piece and the same fraction along its contact curve are the centre and
    the contact point of one position of the bit.

    * an offset LINE contacts the line it came from - its own points dropped
      perpendicularly onto that item's infinite line;
    * an offset ARC contacts the arc it came from, concentric with it, so only
      the radius changes and every angle means the same position;
    * a CORNER arc pivots about one outline vertex and touches the board only
      there, so its contact curve is that vertex - the circle of radius zero.
      Rolling round a corner cuts air; it takes no more board edge to depth.

    Anything whose origin names no item of *chain*, or disagrees in kind with
    it, is handed back unchanged: the centreline is then the honest answer this
    can give.
    """
    if piece.origin < 0 or piece.origin >= len(chain):
        return piece._replace(radius=0.0) if isinstance(piece, Arc) else piece
    source = chain[piece.origin]
    if isinstance(piece, Arc) and isinstance(source, Arc):
        return piece._replace(radius=source.radius)
    if isinstance(piece, Line) and isinstance(source, Line):
        return piece._replace(
            p0=foot_on_line(source, piece.p0), p1=foot_on_line(source, piece.p1)
        )
    return piece


# ---------------------------------------------------------------------------
# v-score candidates: which BLADE LINES a saw could cut across this outline
# ---------------------------------------------------------------------------

# Two spans belong to one blade line when their directions agree to this and
# neither end strays further than COLLINEAR_TOL_MM off the other's line. Unit
# vectors, so it is the sine of the angle between them: 1e-3 is 0.06 degrees,
# three orders above the microradian dust a stored coordinate carries and far
# below any angle a designer draws on purpose.
BLADE_PARALLEL_TOL = 1e-3


class ScoreSpan(NamedTuple):
    """One straight stretch of outline lying on a candidate's blade line.

    `items` are the chain indices it merges - adjacent collinear lines are one
    span, because a saw cutting the second is cutting the first.

    `straightness_mm` is how far the merged outline wanders from the straight
    span that replaces it - the blade cuts one line, so this says what that
    costs. It is nanometres on a real board and it is measured rather than
    assumed.
    """

    items: tuple
    start: tuple
    end: tuple
    length_mm: float
    sources: tuple
    straightness_mm: float = 0.0

    @property
    def midpoint(self) -> tuple:
        return ((self.start[0] + self.end[0]) / 2.0,
                (self.start[1] + self.end[1]) / 2.0)


class ScoreCandidate(NamedTuple):
    """One BLADE LINE a saw could cut, and every stretch of outline on it.

    **The unit a designer counts is the line, not the span.** A v-score is one
    pass of a straight blade across the whole panel, so every collinear stretch
    of outline that pass would take belongs to ONE candidate - however many
    items the outline was drawn as, and however many separate spans a notch or
    an unbridged tab mouth breaks it into. `spans` holds them in order along the
    line; `items`, `sources` and `length_mm` are their union, and `start`/`end`
    are the extreme ends of the run, so `extent_mm` is how far apart the outermost
    scored points are while `length_mm` is how much outline actually lies on the
    line.

    `normal` is the unit vector pointing from the score plane INTO the board, so
    a point's signed distance along it is positive on the material side and
    negative out in the air the route has to reach.
    """

    spans: tuple
    start: tuple
    end: tuple
    normal: tuple
    length_mm: float
    extent_mm: float
    items: tuple
    sources: tuple
    straightness_mm: float = 0.0

    @property
    def midpoint(self) -> tuple:
        return ((self.start[0] + self.end[0]) / 2.0,
                (self.start[1] + self.end[1]) / 2.0)

    @property
    def unit(self) -> tuple:
        """Direction of travel along the blade line."""
        return _unit(_sub(self.end, self.start))

    def plane_distance(self, point: tuple) -> float:
        """Signed distance from the score plane: positive into the board."""
        return _dot(_sub(point, self.start), self.normal)


def chain_bbox(chain: list) -> tuple:
    """`(min_x, min_y, max_x, max_y)` of a chain, with arcs measured exactly."""
    xs, ys = [], []
    for primitive in chain:
        for point in (primitive.start, primitive.end):
            xs.append(point[0])
            ys.append(point[1])
        if isinstance(primitive, Arc):
            for quarter in range(4):
                angle = quarter * math.pi / 2.0
                if primitive.contains_angle(angle):
                    point = primitive.point_at_angle(angle)
                    xs.append(point[0])
                    ys.append(point[1])
    return (min(xs), min(ys), max(xs), max(ys))


def _crosses_the_ray(y0: float, y1: float, py: float) -> bool:
    """Does a span running from *y0* to *y1* straddle the line `y = py`?

    KiCad's own half-open test, `( p1.y >= aPt.y ) != ( p2.y >= aPt.y )`
    (`libs/kimath/src/geometry/shape_line_chain.cpp:2027`, KiCad 10.0.3).
    Half-open is what makes a vertex sitting exactly on the ray honest: an
    outline that passes THROUGH the vertex straddles on one side of it only and
    counts once, and one that merely touches the ray there straddles on both and
    counts twice. The parity is right in both cases with no case for either.
    """
    return (y0 >= py) != (y1 >= py)


def _y_monotone_spans(arc: Arc) -> list:
    """The arc split where it turns round in y: `(angle0, angle1)` in travel order.

    A span that never reverses in y is what the crossing test above needs, and
    an arc reverses exactly where its tangent runs horizontal - the top and the
    bottom of its own circle. Such a span also lies wholly on one side of the
    vertical through the centre, which is why the crossing's x coordinate below
    is a choice of sign rather than a search.
    """
    sign = 1.0 if arc.ccw else -1.0
    turns = []
    for stationary in (math.pi / 2.0, 3.0 * math.pi / 2.0):
        along = ((stationary - arc.a0) * sign) % (2.0 * math.pi)
        if 0.0 < along < arc.sweep:
            turns.append(along)
    cuts = [0.0, *sorted(turns), arc.sweep]
    return [
        (arc.a0 + sign * before, arc.a0 + sign * after)
        for before, after in zip(cuts, cuts[1:])
    ]


class _MonotonePiece(NamedTuple):
    """A stretch of outline that does not reverse in y, for the crossing count.

    `crossing_x` is where the piece meets the horizontal line through a probe,
    exact for both a line and an arc, and asked for only once the piece is known
    to straddle that line.
    """

    y0: float
    y1: float
    crossing_x: Callable


def _monotone_pieces(primitive) -> list:
    """*primitive* split where it turns round in y, in travel order.

    An arc is measured AS AN ARC here - the crossing's x is the circle's own
    root - because a chord walk answers for a polygon rather than for the
    outline. See :func:`point_inside_loop`.
    """
    if isinstance(primitive, Line):
        (x0, y0), (x1, y1) = primitive.p0, primitive.p1

        def on_the_line(py: float) -> float:
            return x0 + (x1 - x0) * (py - y0) / (y1 - y0)

        return [_MonotonePiece(y0, y1, on_the_line)]

    center_x, center_y = primitive.center
    radius = primitive.radius
    pieces = []
    for angle0, angle1 in _y_monotone_spans(primitive):
        # The piece holds no horizontal tangent, so it lies wholly on one side
        # of the vertical through the centre, and the cosine of its mid-angle
        # says which side. That is what makes the root below a choice of sign.
        side = 1.0 if math.cos((angle0 + angle1) / 2.0) >= 0.0 else -1.0

        def on_the_circle(py: float, side: float = side) -> float:
            reach = radius ** 2 - (py - center_y) ** 2
            return center_x + side * math.sqrt(max(0.0, reach))

        pieces.append(_MonotonePiece(
            center_y + radius * math.sin(angle0),
            center_y + radius * math.sin(angle1),
            on_the_circle,
        ))
    return pieces


def _loop_pieces(loop: list) -> list:
    """Every y-monotone piece of the loop, with its joins made to agree exactly.

    A corner where two primitives meet has ONE y as far as a crossing count is
    concerned. KiCad gets that for nothing - its outline is a list of points, so
    a shared corner IS one number - while here a line's stored endpoint and an
    arc's `centre + r sin(angle)` are two computations of the same corner that
    differ in the last bit. Left alone that difference loses a crossing: probing
    the CENTRE of a circle drawn as two half arcs read OUTSIDE it, measured,
    because the half that ends at 2 pi puts their shared corner a
    thousandth of a nanometre below where the half that starts at 0 puts it, and
    the half-open straddle test then counts neither side of the corner. So every
    join takes one value: the start of the primitive that begins there.
    """
    pieces = []
    for index, primitive in enumerate(loop):
        own = _monotone_pieces(primitive)
        following = loop[(index + 1) % len(loop)]
        own[0] = own[0]._replace(y0=primitive.start[1])
        own[-1] = own[-1]._replace(y1=following.start[1])
        pieces.extend(own)
    return pieces


def point_inside_loop(loop: list, point: tuple) -> bool:
    """True when *point* lies inside the closed chain. Undefined ON the boundary.

    The inside test counts crossings and takes their PARITY. No signed turn
    total is summed anywhere, because that arithmetic fails on arcs. KiCad
    answers the same question the same way -
    `SHAPE_LINE_CHAIN_BASE::PointInside` draws a ray in the positive x direction
    and flips a flag per crossing, "If it intersects an even number of segments,
    the point is outside the line chain (it had to first enter and then exit)"
    (`libs/kimath/src/geometry/shape_line_chain.cpp:1990-2037`, KiCad 10.0.3) -
    and this is that rule with true arcs where KiCad has a polygon.

    WHY NOT A WINDING NUMBER: a winding number that walks each arc in chords of
    at most a right angle and sums the angle each chord subtends answers for
    the CHORD POLYGON and not for the outline. Measured on a camera board: the
    lens bulge is an 11 mm arc
    sweeping 280.389 degrees, walked in four chords of 70.097 degrees, each of
    which cuts 1.995 mm of real material off the shape - and the board's four
    1.3 mm cutouts sit in that band at 9.551 mm from the arc's centre, so two
    read inside and two read outside though board and cutouts are both symmetric
    about y = 127.5. Here an arc carries its own curvature exactly and there is
    no band to fall into.

    A probe ON the boundary has no answer - it is neither in nor out - so every
    caller keeps its probes a measured distance clear. See
    `eedesignpanels.material`, where that distance is half the cutter. A probe
    level with a corner is fine: :func:`_loop_pieces` makes the two primitives
    that meet there agree on it, and :func:`_crosses_the_ray` then counts it
    once or twice as the outline's own shape requires.
    """
    px, py = point
    crossings = sum(
        1 for piece in _loop_pieces(loop)
        if _crosses_the_ray(piece.y0, piece.y1, py) and piece.crossing_x(py) > px
    )
    return crossings % 2 == 1


def clip_to_bbox(origin: tuple, unit: tuple, bbox: tuple):
    """The parameter range of the line `origin + t * unit` inside *bbox*.

    None when the line misses the box entirely, which an outline's own line
    never does.
    """
    low, high = -math.inf, math.inf
    for axis in (0, 1):
        direction = unit[axis]
        lower, upper = bbox[axis], bbox[axis + 2]
        if abs(direction) < 1e-12:
            if origin[axis] < lower - TOL_MM or origin[axis] > upper + TOL_MM:
                return None
            continue
        first = (lower - origin[axis]) / direction
        second = (upper - origin[axis]) / direction
        low = max(low, min(first, second))
        high = min(high, max(first, second))
    return None if high <= low else (low, high)


def blade_extent(candidate, bbox: tuple) -> tuple:
    """The candidate's blade line produced to the outline's bounding box.

    A v-score line reaches the board's own extents, not merely
    the stretch of outline that happens to touch it. The saw enters at one edge
    of the board and leaves at the other, so the line that stands for the cut
    says where the blade travels - `length_mm` is still how much outline lies on
    it, and the two are different facts.

    Returns `(start, end)` in the candidate's own direction of travel, or the
    candidate's own ends when its line misses the box, which an outline's own
    line never does.
    """
    unit = candidate.unit
    span = clip_to_bbox(candidate.start, unit, bbox)
    if span is None:
        return (candidate.start, candidate.end)
    low, high = span
    return (
        _add(candidate.start, _scale(unit, low)),
        _add(candidate.start, _scale(unit, high)),
    )


def on_blade(candidate, point: tuple, tol: float = COLLINEAR_TOL_MM) -> bool:
    """True when *point* lies on the candidate's blade LINE, however far along.

    The line, not the span: a blade cuts the whole line across the panel, so
    "which candidate is this?" is a question about the line and nothing else.
    That is what makes the decision model extent-tolerant - a designer may draw
    the score shorter or longer than the outline it takes, and it is still the
    same cut.
    """
    return abs(_cross(_sub(point, candidate.start), candidate.unit)) <= tol


def collinear_line_runs(chain: list) -> list:
    """The chain's indices grouped into maximal runs of collinear lines.

    Cyclic, and in chain order. Two lines are one run when the second continues
    the first along the same straight line in the same direction - which is what
    a saw sees: one cut, however many items the designer drew it as.
    """
    count = len(chain)

    def continues(before: int, after: int) -> bool:
        one, other = chain[before], chain[after]
        if not isinstance(one, Line) or not isinstance(other, Line):
            return False
        if _dot(one.tangent_at_end(), other.tangent_at_start()) <= 0.0:
            return False
        unit = one.tangent_at_end()
        return all(
            abs(_cross(_sub(point, one.p0), unit)) <= COLLINEAR_TOL_MM
            for point in (other.p0, other.p1)
        )

    heads = [
        index for index in range(count)
        if isinstance(chain[index], Line)
        and not continues((index - 1) % count, index)
    ]
    groups = []
    for head in heads:
        group = [head]
        following = (head + 1) % count
        while following != head and continues(group[-1], following):
            group.append(following)
            following = (following + 1) % count
        groups.append(group)
    return groups


def _extensions_outside(chain: list, origin: tuple, unit: tuple,
                        length: float, bbox: tuple) -> bool:
    """True when the span's line, extended to the bbox, meets no material.

    The blade cannot stop inside the board, so a scoreable span is one whose own
    line runs clear to the outline's extents. "Clear" allows two things a saw
    allows: passing through free space, and running ALONG more outline that is
    collinear with it. Anything else - a crossing into material - disqualifies
    the span.
    """
    span = clip_to_bbox(origin, unit, bbox)
    if span is None:
        return False
    for low, high in ((min(span[0], 0.0), 0.0), (length, max(span[1], length))):
        if high - low <= CONTACT_TOL_MM:
            continue
        probe = Line(p0=_add(origin, _scale(unit, low)),
                     p1=_add(origin, _scale(unit, high)))
        cuts = {low, high}
        for primitive in chain:
            for point in bounded_intersections(probe, primitive, tol=1e-7):
                cuts.add(_dot(_sub(point, origin), unit))
        ordered = sorted(value for value in cuts if low <= value <= high)
        for before, after in zip(ordered, ordered[1:]):
            if after - before <= CONTACT_TOL_MM:
                continue
            middle = _add(origin, _scale(unit, (before + after) / 2.0))
            _, clearance = nearest_on_path(middle, chain)
            if clearance <= CONTACT_TOL_MM:
                continue  # the probe is running along the outline itself
            if point_inside_loop(chain, middle):
                return False
    return True


def _projected(point: tuple, anchor: tuple, unit: tuple) -> float:
    """How far along a blade line, measured from *anchor*, *point* lies."""
    return _dot(_sub(point, anchor), unit)


def _same_blade(anchor: tuple, unit: tuple, span: ScoreSpan,
                span_unit: tuple) -> bool:
    """Is *span* on the blade line through *anchor*, board on the same side?

    Direction matters as well as position. Two collinear spans travelled in
    OPPOSITE directions have the outline's material on opposite sides of the
    same line - the inner edges of a Z - and one saw pass cannot serve both, so
    they stay separate candidates and each is judged on its own.
    """
    if _dot(unit, span_unit) <= 0.0:
        return False
    if abs(_cross(unit, span_unit)) > BLADE_PARALLEL_TOL:
        return False
    return all(
        abs(_cross(_sub(point, anchor), unit)) <= COLLINEAR_TOL_MM
        for point in (span.start, span.end)
    )


def _gather_blades(records: list) -> list:
    """Turn `(span, unit, normal)` records into one candidate per blade line.

    Aggregation is safe by construction: every span here already passed the
    extension test, which walks its line to the outline's extents IN BOTH
    DIRECTIONS. So the stretch of line between two spans of one candidate has
    already been shown to meet no material, and the blade travels it.
    """
    groups = []
    for span, unit, normal in records:
        for entry in groups:
            if _same_blade(entry[2][0].start, entry[0], span, unit):
                entry[2].append(span)
                break
        else:
            groups.append((unit, normal, [span]))

    candidates = []
    for unit, normal, spans in groups:
        anchor = spans[0].start
        ordered = tuple(sorted(
            spans,
            key=lambda span: min(_projected(span.start, anchor, unit),
                                 _projected(span.end, anchor, unit)),
        ))
        ends = [point for span in ordered for point in (span.start, span.end)]
        start = min(ends, key=lambda point: _projected(point, anchor, unit))
        end = max(ends, key=lambda point: _projected(point, anchor, unit))
        candidates.append(ScoreCandidate(
            spans=ordered,
            start=start,
            end=end,
            normal=normal,
            length_mm=sum(span.length_mm for span in ordered),
            extent_mm=math.dist(start, end),
            items=tuple(index for span in ordered for index in span.items),
            sources=tuple(
                source for span in ordered for source in span.sources
            ),
            straightness_mm=max(span.straightness_mm for span in ordered),
        ))
    return candidates


def score_candidates(chain: list) -> list:
    """Every blade line a saw could score across *chain*, edge to edge.

    The plausible proposer, and deliberately generous: it says which lines the
    geometry permits, not which ones the panel wants. An ARC never qualifies -
    a v-groove is cut by a straight blade travelling in a straight line - and a
    stretch of outline qualifies only when its own line, produced to the
    outline's extents, touches board material nowhere except along outline
    collinear with it.

    One entry per blade LINE. Collinear stretches that a notch or an unbridged
    tab mouth separates are spans of a single candidate, because one pass of the
    saw takes all of them.
    """
    orientation = 1 if signed_area(chain) > 0.0 else -1
    bbox = chain_bbox(chain)
    records = []
    for group in collinear_line_runs(chain):
        start, end = chain[group[0]].start, chain[group[-1]].end
        length = math.dist(start, end)
        if length <= CONTACT_TOL_MM:
            continue
        unit = _unit(_sub(end, start))
        if not _extensions_outside(chain, start, unit, length, bbox):
            continue
        records.append((
            ScoreSpan(
                items=tuple(group),
                start=start,
                end=end,
                length_mm=length,
                sources=tuple(chain[index].source for index in group),
                straightness_mm=max(
                    abs(_cross(_sub(point, start), unit))
                    for index in group
                    for point in (chain[index].start, chain[index].end)
                ),
            ),
            unit,
            _scale(outward_normal(unit, orientation), -1.0),
        ))
    return _gather_blades(records)


# ---------------------------------------------------------------------------
# how close two curves come, and the index that makes asking affordable
# ---------------------------------------------------------------------------


def nearest_point_on(primitive, point: tuple) -> tuple:
    """The point of *primitive* closest to *point*, its own ends included.

    :func:`distance_to_primitive` answers how far; this answers where, which is
    what anything drawing the shortest cut between two curves needs.
    """
    if isinstance(primitive, Line):
        direction = _sub(primitive.p1, primitive.p0)
        length_squared = _dot(direction, direction)
        if length_squared < 1e-18:
            return primitive.p0
        along = _dot(_sub(point, primitive.p0), direction) / length_squared
        return _add(primitive.p0, _scale(direction, max(0.0, min(1.0, along))))

    radial = _sub(point, primitive.center)
    if math.hypot(*radial) < 1e-18:
        # Dead on the centre, where every point of the arc is equally close, so
        # the answer is a choice rather than a measurement. The start is the
        # choice that does not depend on which way the arc was walked.
        return primitive.start
    angle = math.atan2(radial[1], radial[0])
    if primitive.contains_angle(angle):
        return primitive.point_at_angle(angle)
    return min(
        (primitive.start, primitive.end),
        key=lambda end: (math.dist(point, end), end),
    )


def _radial_pairs(one, other) -> list:
    """Nearest-pair candidates that lie in both curves' INTERIORS.

    Two curves can face each other squarely somewhere in the middle of both,
    and no endpoint says so. Where a circle is involved that place is on the
    radial line - the perpendicular from the centre - so those are the only
    interior candidates there are, and two straight segments have none at all:
    a segment pair that does not cross is always nearest at an end.
    """
    if isinstance(one, Line) and isinstance(other, Line):
        return []

    if isinstance(one, Arc) and isinstance(other, Arc):
        span = _sub(other.center, one.center)
        if math.hypot(*span) < 1e-12:
            # Concentric: the gap is the same all the way round the overlap, so
            # the endpoints already carry it.
            return []
        base = math.atan2(span[1], span[0])
        pairs = []
        for first in (base, base + math.pi):
            if not one.contains_angle(first):
                continue
            for second in (base, base + math.pi):
                if other.contains_angle(second):
                    pairs.append((
                        one.point_at_angle(first),
                        other.point_at_angle(second),
                    ))
        return pairs

    swapped = isinstance(one, Arc)
    line, arc = (other, one) if swapped else (one, other)
    foot = nearest_point_on(line, arc.center)
    radial = _sub(foot, arc.center)
    if math.hypot(*radial) < 1e-12:
        return []
    base = math.atan2(radial[1], radial[0])
    pairs = []
    for angle in (base, base + math.pi):
        if arc.contains_angle(angle):
            on_arc = arc.point_at_angle(angle)
            pairs.append((nearest_point_on(line, on_arc), on_arc))
    return [(b, a) for a, b in pairs] if swapped else pairs


TIE_TOL_MM = 1e-9


def closest_approach(one, other) -> tuple:
    """The nearest pair of points on two curves: `(on_one, on_other, gap_mm)`.

    Exact - no sampling - and it answers the MIDDLE where the answer is a
    family rather than a point. Two parallel runs face each other along their
    whole overlap, so every pair across it is equally near; picking one end of
    that family would be arbitrary, would depend on which curve was handed over
    first, and - for anything that then draws a cut between the two points -
    would put that cut exactly on a run's own endpoint, where its round cap and
    the run's coincide and the arrangement has no way through. So the tied
    candidates are averaged and projected back onto both curves, which lands on
    the middle of the overlap for two parallel lines and on the angular middle
    for two concentric arcs.
    """
    crossings = bounded_intersections(one, other, tol=1e-7)
    if crossings:
        point = min(crossings)
        return (point, point, 0.0)

    candidates = [
        (point, nearest_point_on(other, point))
        for point in (one.start, one.end)
    ]
    candidates += [
        (nearest_point_on(one, point), point)
        for point in (other.start, other.end)
    ]
    candidates += _radial_pairs(one, other)
    measured = sorted(
        (math.dist(first, second), first, second)
        for first, second in candidates
    )
    gap, on_one, on_other = measured[0]

    tied = [entry for entry in measured if entry[0] <= gap + TIE_TOL_MM]
    if len(tied) > 1:
        middle_one = nearest_point_on(one, _mean(entry[1] for entry in tied))
        middle_other = nearest_point_on(other, _mean(entry[2] for entry in tied))
        # Only if the middle really is as near: averaging is a guess about the
        # shape of the family, and a wrong guess must not move the answer.
        if abs(math.dist(middle_one, middle_other) - gap) <= TIE_TOL_MM:
            return (middle_one, middle_other, gap)
    return (on_one, on_other, gap)


def _mean(points) -> tuple:
    collected = list(points)
    return (
        sum(point[0] for point in collected) / len(collected),
        sum(point[1] for point in collected) / len(collected),
    )


class Grid:
    """Buckets of bounding boxes on a uniform lattice. Answers "what is near".

    A coarse index and nothing more: :meth:`near` may over-report and never
    under-reports, so a caller still has to do the exact test on what comes
    back. It exists because every arrangement in this project asks about pairs,
    and a panel has enough curves that asking about all of them costs the
    square of the board.
    """

    def __init__(self, boxes: list, cell_mm: float):
        self.cell = max(cell_mm, 1e-6)
        self.buckets: dict = {}
        for index, box in enumerate(boxes):
            for key in self._keys(box):
                self.buckets.setdefault(key, []).append(index)

    def _keys(self, box: tuple):
        low_x = math.floor(box[0] / self.cell)
        low_y = math.floor(box[1] / self.cell)
        high_x = math.floor(box[2] / self.cell)
        high_y = math.floor(box[3] / self.cell)
        for x in range(low_x, high_x + 1):
            for y in range(low_y, high_y + 1):
                yield (x, y)

    def near(self, box: tuple) -> set:
        found: set = set()
        for key in self._keys(box):
            found.update(self.buckets.get(key, ()))
        return found


def padded_bbox(curve, pad: float = 0.0) -> tuple:
    """One curve's bounding box, grown on all four sides."""
    box = chain_bbox([curve])
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def boxes_overlap(one: tuple, other: tuple, tol: float = 1e-7) -> bool:
    """True when two bounding boxes have any point in common."""
    return not (
        one[2] < other[0] - tol or other[2] < one[0] - tol
        or one[3] < other[1] - tol or other[3] < one[1] - tol
    )


def index_cell_mm(boxes: list) -> float:
    """A lattice step near the size of the things being indexed."""
    if not boxes:
        return 1.0
    spans = [max(box[2] - box[0], box[3] - box[1]) for box in boxes]
    return max(sum(spans) / len(spans), 1e-3)
