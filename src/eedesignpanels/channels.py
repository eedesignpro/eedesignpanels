"""The panel's outline: the boundary of the BLANK minus the routed channels.

One boolean, and it is the whole of the panel's edge definition. The blank is
the boards plus their rails - a rectangle of material; the router removes a ribbon of it wherever a toolpath
runs; what is left is the panel, and its boundary is what `Edge.Cuts` has to
say. Nothing here draws a rail, a bridge or a tab: those are consequences of
where the toolpath was, not shapes anybody places.

**A channel is the toolpath's own stroke, taken literally.** Each `User.36`
item is drawn at a stroke width that IS the cutter diameter, so the material it
removes is that item dilated by half its width - a stadium for a line, an
annular band with round ends for an arc. The channel region is the union of all
of them, and the panel is `blank - channels`.

**THE INVARIANT, stated once**: every routed
section of `Edge.Cuts` is the outline of the toolpath layer's own ink, with only
the blank's edges exempt. So no pass may shape the outline without existing on
that layer - not a minimum-tab crossing and not a pinch cut - and
:func:`derived_boundary` hands every pass it adds back on `Derived.added` for
the caller to write down BEFORE the outline is derived from it. A covering pass
that changed the boundary and left the toolpath alone is exactly the defect the
invariant forbids: the fab would be handed an outline no program cuts.

The boundary of that region lives at distance exactly *r* from some toolpath
run, or on the blank's own rectangle. So the method is the one
:mod:`eedesignpanels.toolpath` already uses for the mill: build every curve the
answer can be made of, cut them all at every mutual crossing, keep the pieces
that are on the boundary, and chain what survives into closed loops. Nothing is
approximated by segments - what comes out is true lines and true arcs.

**A run's dilation is an offset of the run travelled out and back.** Walking a
run forwards and then backwards is a closed chain of zero area whose outward
offset is exactly the boundary of the run's dilation: the two flanks fall out as
the outward offsets of the forward and reversed copies, and the half turn at each
end becomes the round cap. That is why this module can hand the whole
construction to `toolpath.candidate_curves` rather than write a second offset.

**No accidental sliver of material survives, and that takes two passes.**
:func:`close_slivers` consumes the ribbons of material thinner than the minimum
tab that two cutter passes leave between them - the rule and its arithmetic live
in :mod:`eedesignpanels.mintab`. What no measurement between two centrelines can
see is material that narrows to NOTHING: two walls that touch leave a sliver of
width zero, and the two paths that made them are a whole cutter diameter apart.
The boolean is what finds those, because that is exactly where its arrangement
stops being a region with a boundary - four pieces of edge meet at the touching
point and "which way round" has no answer, or the walk is left holding a hair of
edge with cut on both sides of it. It reports them as a :class:`PinchError`, the
cut goes there, and the boolean runs again: :func:`derived_boundary` is the
whole of it in the order it has to happen.

Two things are errors rather than results, because each means the input is not
what this is defined for: a toolpath that BRANCHES (three items meeting at a
point is not a run), and a boundary piece whose end no other piece meets (the
region did not close). Everything else - overlapping channels, channels
crossing the outer edge, a channel that swallows a whole corner - is ordinary
geometry and comes out as loops.

Pure geometry: millimetre floats, no board, no I/O. It imports
:mod:`eedesignpanels.toolpath`, :mod:`eedesignpanels.mintab` and `math`, and
nothing else.
"""

import math
from typing import NamedTuple

from eedesignpanels import mintab
from eedesignpanels.toolpath import (
    JOIN_TOL_MM,
    REACH_TOL_MM,
    Grid,
    Line,
    bounded_intersections,
    boxes_overlap,
    candidate_curves,
    chain_bbox,
    distance_to_primitive,
    index_cell_mm,
    merge_same_curve,
    midpoint_of,
    nearest_point_on,
    node_of,
    padded_bbox,
    reverse,
    signed_area,
    split_primitive,
    weld_chain,
    weld_end,
    weld_start,
)

# How far apart two toolpath items may be drawn and still be one run. The
# toolpath is written to the file at the nanometre grid, and an arc's endpoint
# is computed from its centre and its angles rather than stored, so two items a
# designer sees as joined can land a nanometre apart. A tenth of a micrometre
# is a hundred times that and four orders below the shortest piece of channel
# anybody draws.
RUN_JOIN_TOL_MM = JOIN_TOL_MM

# A piece of cutter path shorter than this is dropped, and its neighbours are
# welded across the gap it leaves.
#
# **Dropping it is bounded, and keeping it is not.** Every point the dropped
# piece would have removed is within its own length of a point its neighbours
# remove, so the channel changes by at most that length - ten micrometres at
# this setting, and 1.7 um in fact on the corpus panel. Keeping it costs far
# more than that: a two-micrometre arc is a curve whose radius the file cannot
# state to better than a part in a thousand, and its inner wall - the arc's
# radius less the cutter's - comes out microns across, small enough that the
# sub-nanometre welding every offset does swings its direction right round the
# circle. What that produces is not a small error, it is a whole extra loop
# through the panel.
#
# Measured on the corpus panel: 96 of the 318 toolpath items are 1.33 to
# 1.73 um long and the next-shortest is 85 um - a gap of a factor of 49 with
# this threshold sitting in the middle of it. They are splinters the offset
# arrangement left where two curves touched, not anything a cutter does.
MIN_ITEM_MM = 0.01

# The two tolerances the arrangement is resolved at, and they are a PAIR: a
# piece shorter than the first is dropped, and two ends closer than the second
# are one node, so the second has to be the larger or dropping a piece would
# open a gap nothing closes.
#
# Both are set from what the geometry actually contains. Wherever the toolpath
# turns through a hair of an angle, the wall's corner arc is a hair long and
# its two ends are nanometres apart - dust, and every one of them measured
# under a micrometre on the corpus panel. Wherever it turns through a real
# corner, or runs along a real edge, the piece is 85 um or more. So a
# micrometre is five times above the dust and eighty-five times below the
# geometry, and welding an end by up to five micrometres - which only ever
# happens where a piece that small was dropped - is two hundred times finer
# than the tolerance any router holds.
PIECE_MIN_MM = 1e-3
BOUNDARY_NODE_TOL_MM = 5e-3
# ...and an arc wall thinner than a piece is not a wall. The inner side of the
# channel round a corner fillet has radius exactly the fillet's less the
# cutter's, and the toolpath's fillets ARE the cutter's radius, so that inner
# wall pinches to a point - which floating point renders as a circle a couple
# of nanometres across rather than as nothing.
WALL_MIN_RADIUS_MM = PIECE_MIN_MM
# Two pieces of wall this alike are the same piece of wall - see
# :func:`_deduplicated`. A tenth of a micrometre is a hundred times the
# nanometre the two copies of a corner differ by, and a tenth of the shortest
# piece kept.
DUPLICATE_TOL_MM = 1e-4
# A CHANNEL WALL THIS CLOSE TO THE BLANK'S OWN EDGE IS THAT EDGE.
# A rail of zero width puts the boards' edge exactly on the blank's, and the
# channel that runs round those boards has its inner wall exactly there too: two
# coincident curves, split at different points, meeting three at a node - which
# is an outline that cannot close, and is what a zero rail used to raise. The
# blank's edge is the piece the boundary keeps, because it is the one that is
# there whether or not a cutter came near it. The same tenth of a micrometre
# :data:`DUPLICATE_TOL_MM` uses, for the same reason: it is a hundred times the
# nanometre coincident geometry differs by and a tenth of the shortest piece the
# walk keeps.
BLANK_EDGE_TOL_MM = DUPLICATE_TOL_MM


class ChannelError(Exception):
    """The channels cannot be made into a boundary. Carries where, in mm."""

    def __init__(self, message: str, at: tuple):
        super().__init__(f"{message} at ({at[0]:.4f}, {at[1]:.4f}) mm")
        self.at = at


class PinchError(ChannelError):
    """THE MATERIAL IS SEVERED HERE AND THE BOUNDARY CANNOT SAY SO.

    Two cuts that touch leave material of width ZERO between them. The region
    is still perfectly well defined - it is simply not a region with a boundary
    a walk can follow: at the touching point four pieces of edge meet and
    "which way round" has no answer, and where the two walls only graze each
    other the walk is left holding a hair of edge with cut on both sides of it.

    Both are the same physical fact and it is a fabrication fact: material of
    zero width is under any minimum tab there is, so the cut belongs there
    (:mod:`eedesignpanels.mintab`). This is raised so the caller can put it there
    and ask again - see :func:`derived_boundary` - rather than so anybody has to
    reason about a pinch.
    """

    def __init__(self, message: str, at: tuple, points=()):
        super().__init__(message, at)
        self.points = tuple(points) or (at,)


class Rails(NamedTuple):
    """The four rails, in millimetres: the material standing round the boards.

    Named by the side of the panel as a reader sees it, and file coordinates run
    y DOWNWARD, so `top` grows the box at `min_y` and `bottom` at `max_y`.

    Zero removes that side's rail, which is a real arrangement and not a
    degenerate one: a panel routed flush on its left edge has no left rail.
    """

    top: float
    bottom: float
    left: float
    right: float

    @classmethod
    def of(cls, value) -> "Rails":
        """Rails from whatever the caller has: four of them, or one for all four.

        One number is the LINKED view of four equal ones, not a different kind of
        setting, so both spellings land here and everything
        downstream sees four numbers.
        """
        if isinstance(value, Rails):
            rails = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            rails = cls(float(value), float(value), float(value), float(value))
        elif isinstance(value, dict):
            rails = cls(**{key: float(number) for key, number in value.items()})
        elif isinstance(value, (tuple, list)):
            if len(value) != 4:
                raise ValueError(
                    f"rails are four widths - top, bottom, left and right - "
                    f"and this is {len(value)}"
                )
            rails = cls(*(float(number) for number in value))
        else:
            rails = cls(
                top=float(value.top), bottom=float(value.bottom),
                left=float(value.left), right=float(value.right),
            )
        for side, width in zip(rails._fields, rails):
            if width < 0.0:
                raise ValueError(
                    f"the {side} rail must not be negative: {width}"
                )
        return rails

    @property
    def uniform(self) -> bool:
        """Are all four the same? What makes a panel the one today's build makes."""
        return len(set(self)) == 1

    @property
    def linked_mm(self) -> float:
        """The one number that stands for four - the widest of them.

        What the linked field shows, and therefore what re-linking applies to
        every side. The widest, because collapsing four rails into one must not
        quietly delete material the user asked for.
        """
        return max(self)

    @property
    def summary(self) -> str:
        """How the rails read in a report, in millimetres and in plain words."""
        if self.uniform:
            return f"{self.top:g} mm rails"
        return (
            f"rails {self.top:g} mm top, {self.bottom:g} mm bottom, "
            f"{self.left:g} mm left, {self.right:g} mm right"
        )


class Rect(NamedTuple):
    """An axis-aligned rectangle in board millimetres - the panel blank."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width_mm(self) -> float:
        return self.max_x - self.min_x

    @property
    def height_mm(self) -> float:
        return self.max_y - self.min_y

    @property
    def bbox(self) -> tuple:
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    @property
    def extent_mm(self) -> tuple:
        return (self.width_mm, self.height_mm)

    def contains(self, point: tuple, tol: float = 0.0) -> bool:
        """True when *point* is inside, edges included."""
        return (
            self.min_x - tol <= point[0] <= self.max_x + tol
            and self.min_y - tol <= point[1] <= self.max_y + tol
        )

    def edges(self) -> list:
        """The four sides, travelled round the rectangle in file coordinates."""
        corners = (
            (self.min_x, self.min_y),
            (self.max_x, self.min_y),
            (self.max_x, self.max_y),
            (self.min_x, self.max_y),
        )
        return [
            Line(
                p0=corners[index],
                p1=corners[(index + 1) % 4],
                source=f"blank{index}",
                role="blank",
                origin=index,
            )
            for index in range(4)
        ]


def grown(bbox: tuple, rails) -> Rect:
    """A bounding box grown by its rails - the panel blank.

    Derived, never typed: the instance grid decides where the boards are and the
    rails decide how much material stands round them, so a panel size is a
    consequence of the config rather than a number anybody enters twice.

    *rails* is a :class:`Rails`, or one number meaning all four sides.
    """
    sides = Rails.of(rails)
    return Rect(
        min_x=bbox[0] - sides.left,
        min_y=bbox[1] - sides.top,
        max_x=bbox[2] + sides.right,
        max_y=bbox[3] + sides.bottom,
    )


class Channel(NamedTuple):
    """One connected run of cutter path, and how wide a ribbon it removes.

    `path` is welded: consecutive items share an endpoint exactly, which is
    what lets the dilation's corner arcs land on their neighbours rather than a
    nanometre off them.
    """

    path: tuple
    radius: float
    closed: bool

    @property
    def length_mm(self) -> float:
        return sum(item.length for item in self.path)

    @property
    def bbox(self) -> tuple:
        return chain_bbox(list(self.path))


class RunSet(NamedTuple):
    """The channels a board's toolpath makes, and what reading it cost."""

    channels: tuple
    dropped: int
    worst_dropped_mm: float

    @property
    def item_count(self) -> int:
        return sum(len(channel.path) for channel in self.channels)

    @property
    def length_mm(self) -> float:
        return sum(channel.length_mm for channel in self.channels)


class Boundary(NamedTuple):
    """The finished panel edge: closed loops of true lines and arcs."""

    loops: tuple
    candidates: int
    pieces: int
    # How close the routed material came to the blank's own outer edge, and
    # whether any of it reached across. Measured rather than assumed: whether
    # the junction runs poke out of the panel is a property of the rails.
    clearance_mm: float

    @property
    def loop_count(self) -> int:
        return len(self.loops)

    @property
    def length_mm(self) -> float:
        return sum(
            sum(piece.length for piece in loop) for loop in self.loops
        )

    @property
    def item_count(self) -> int:
        return sum(len(loop) for loop in self.loops)

    @property
    def bbox(self) -> tuple:
        return chain_bbox([piece for loop in self.loops for piece in loop])

    @property
    def extent_mm(self) -> tuple:
        box = self.bbox
        return (box[2] - box[0], box[3] - box[1])

    @property
    def reaches_outer_edge(self) -> bool:
        return self.clearance_mm <= 0.0


# ---------------------------------------------------------------------------
# runs: what the loose toolpath items actually are
# ---------------------------------------------------------------------------


def chain_runs(items: list, tol: float = RUN_JOIN_TOL_MM,
               min_item_mm: float = MIN_ITEM_MM) -> RunSet:
    """Group `(primitive, radius)` pairs into welded runs, in a fixed order.

    Connectivity decides what a run is, and the radius decides where one has to
    stop: a chain whose stroke width changes partway is two channels, because a
    dilation has one distance.

    Dust is dropped on the way through - see :data:`MIN_ITEM_MM` - and counted,
    so what it cost is a number in the run output rather than an assumption.

    Raises:
        ChannelError: when three or more items meet at a point. A toolpath is a
            path; a junction means the input is not the thing this dilates, and
            guessing which way through it goes would be inventing a cut.
    """
    primitives = [item for item, _ in items]
    radii = [radius for _, radius in items]

    nodes: list = []
    ends = [
        (node_of(nodes, item.start, tol), node_of(nodes, item.end, tol))
        for item in primitives
    ]
    at_node: dict = {}
    for index, (start, end) in enumerate(ends):
        at_node.setdefault(start, []).append(index)
        at_node.setdefault(end, []).append(index)
    for node, incident in at_node.items():
        if len(incident) > 2:
            raise ChannelError(
                f"{len(incident)} toolpath items meet at one point, so these "
                f"channels are not runs a cutter could follow",
                nodes[node],
            )

    snapped = [
        weld_end(weld_start(item, nodes[start]), nodes[end])
        for item, (start, end) in zip(primitives, ends)
    ]

    runs, dropped, worst = [], 0, 0.0
    for order, closed in _ordered_runs(snapped, ends, at_node):
        for stretch, radius in _split_by_radius(order, radii):
            path = [
                snapped[index] if forward else reverse(snapped[index])
                for index, forward in stretch
            ]
            kept = [item for item in path if item.length > min_item_mm]
            dropped += len(path) - len(kept)
            worst = max(
                [worst] + [
                    item.length for item in path if item.length <= min_item_mm
                ]
            )
            if not kept:
                continue
            whole = closed and len(stretch) == len(order) and len(kept) == len(path)
            runs.append(Channel(
                path=tuple(weld_along(kept, closed=whole)),
                radius=radius,
                # A run cut short by a radius change, or by dropping a piece of
                # itself, is no longer the closed loop the component was.
                closed=whole,
            ))
    runs.sort(key=lambda run: (run.bbox, run.radius))
    return RunSet(channels=tuple(runs), dropped=dropped, worst_dropped_mm=worst)


def weld_along(path: list, closed: bool = False) -> list:
    """Close the sub-micrometre gaps along an OPEN chain, exactly.

    `toolpath.weld_chain` for a run that does not come back to where it
    started, and by the same rule: the line moves, because its endpoint is a
    stored coordinate and moving it changes nothing else; an arc keeps its
    circle and only slides along it. What a run's two loose ends do is nothing
    at all - there is no joint there to close.
    """
    welded = list(path)
    joints = range(len(welded) if closed else len(welded) - 1)
    for index in joints:
        following = (index + 1) % len(welded)
        current, after = welded[index], welded[following]
        if current.end == after.start:
            continue
        if isinstance(after, Line):
            welded[following] = after._replace(p0=current.end)
        elif isinstance(current, Line):
            welded[index] = current._replace(p1=after.start)
        else:
            welded[following] = weld_start(after, current.end)
    return welded


def _ordered_runs(items: list, ends: list, at_node: dict) -> list:
    """Each connected component, walked into order. `(steps, closed)` per run.

    A step is `(index, forward)`: whether the item is travelled the way it was
    drawn. Walking starts at a loose end where there is one, so an open run
    comes out from one end to the other rather than from the middle.
    """
    unused = set(range(len(items)))
    loose = [
        node for node, incident in at_node.items() if len(incident) == 1
    ]
    starts = sorted(loose) + sorted(at_node)
    runs = []
    for node in starts:
        for first in at_node[node]:
            if first not in unused:
                continue
            steps, closed = _walk_run(first, node, items, ends, at_node, unused)
            runs.append((steps, closed))
    return runs


def _walk_run(first: int, node: int, items: list, ends: list, at_node: dict,
              unused: set) -> tuple:
    """One run, from *node* onwards, consuming items out of *unused*."""
    steps, current, entry = [], first, node
    while True:
        unused.discard(current)
        forward = ends[current][0] == entry
        steps.append((current, forward))
        exit_node = ends[current][1] if forward else ends[current][0]
        following = [
            index for index in at_node[exit_node]
            if index != current and index in unused
        ]
        if not following:
            closed = exit_node == node and len(steps) > 1
            return steps, closed
        current, entry = following[0], exit_node


def _split_by_radius(steps: list, radii: list) -> list:
    """Cut a walked run wherever the stroke width changes."""
    stretches, current = [], []
    for step in steps:
        radius = radii[step[0]]
        if current and radii[current[-1][0]] != radius:
            stretches.append((current, radii[current[-1][0]]))
            current = []
        current.append(step)
    if current:
        stretches.append((current, radii[current[-1][0]]))
    return stretches


# ---------------------------------------------------------------------------
# the dilation of one run
# ---------------------------------------------------------------------------


def doubled_chain(path: list) -> list:
    """The run travelled out and back: a closed chain of zero area.

    Its outward offset IS the boundary of the run's dilation. Travelling an
    item and then its reverse puts the two flanks on the two sides, and the
    place where the walk turns round is a corner of exactly half a turn - which
    is the round cap a cutter leaves at the end of a run.
    """
    return weld_chain([*path, *[reverse(item) for item in reversed(path)]])


def dilation_candidates(channel: Channel) -> tuple:
    """Every curve this channel's wall can be made of, and its welding slack."""
    curves, slacks, _knives = candidate_curves(
        doubled_chain(list(channel.path)), channel.radius, 1
    )
    return curves, slacks


# ---------------------------------------------------------------------------
# the minimum tab: no accidental sliver survives between two channels
# ---------------------------------------------------------------------------


class MinTab(NamedTuple):
    """What enforcing the minimum tab did to one panel's channels.

    `channels` is what the boolean is then given: the toolpath's own runs plus
    one single-item channel per join. A join is carried as its OWN channel
    rather than spliced into a run because a cut that meets another cut
    part-way along it is a T, and a run is a path - three items at one point is
    the thing :func:`chain_runs` refuses. The union does not care: overlapping
    channels are ordinary geometry to it.

    `added` is exactly the channels this rule PUT THERE, in order - what the
    toolpath layer has to gain so that the outline derived below is the outline
    of the ink. `standing` is the other
    answer: the ribbons left alone because the setting is off, each one a piece
    of board somebody has to be told about.
    """

    channels: tuple
    joins: tuple
    protected: tuple
    added: tuple = ()
    standing: tuple = ()

    @property
    def join_count(self) -> int:
        return len(self.joins)

    @property
    def consumed_mm(self) -> float:
        """The widest sliver any join consumed - the worst accident found."""
        return max((join.material_mm for join in self.joins), default=0.0)


def close_slivers(channel_list: list, min_tab_mm: float,
                  guards: tuple = (), eliminate=None) -> MinTab:
    """Make the cut region continuous wherever it would leave an accidental sliver.

    Two channels that approach until the material between them is thinner than
    `min_tab_mm` are ONE cut - see :mod:`eedesignpanels.mintab` for why, and for
    what `guards` exempt. Each crossing is a straight cut between two facing
    centreline points at the wider of the two cutters, and there is one every
    cutter width along the WHOLE ribbon of thin material rather than one at its
    narrowest point: a sliver cut through in the middle leaves the rest of
    itself standing.

    What this does NOT close is a PINCH - material that narrows to nothing,
    where the two centrelines are a whole cutter diameter apart and nothing
    measured between them can see the point between. The boolean finds those;
    :func:`derived_boundary` is the two halves in the order they have to run.

    *eliminate* false MEASURES the same ribbons and cuts none of them: the
    channels come back as they arrived and `standing` names what was left, one
    entry per piece of board (:func:`mintab.standing_slivers`). None takes the
    answer from the minimum tab itself, which is how the user's switch
    reaches here (:class:`mintab.Rule`).
    """
    cuts = mintab.cuts_of(
        [(channel.path, channel.radius, channel.closed)
         for channel in channel_list]
    )
    if not mintab.eliminating(min_tab_mm, eliminate):
        return MinTab(
            channels=tuple(channel_list),
            joins=(),
            protected=mintab.protected_tabs(guards, min_tab_mm),
            added=(),
            standing=mintab.distinct_slivers(
                mintab.sliver_ribbons(cuts, min_tab_mm, guards=guards)
            ),
        )
    # The joining cut is made by the WIDER of the two bits: one cut that
    # certainly consumes the sliver beats two that might. There is one crossing
    # per cutter-width of ribbon, not one per sliver - see
    # :func:`mintab.sliver_joins`.
    joins = mintab.sliver_joins(cuts, min_tab_mm, guards=guards)
    extra = tuple(
        Channel(path=(join.crossing,), radius=join.radius_mm, closed=False)
        for join in joins
    )
    return MinTab(
        channels=tuple(channel_list) + extra,
        joins=joins,
        protected=mintab.protected_tabs(guards, min_tab_mm),
        added=extra,
    )


# ---------------------------------------------------------------------------
# the boolean
# ---------------------------------------------------------------------------


class Derived(NamedTuple):
    """A panel edge and everything the minimum tab had to do to get one.

    `channels` is EVERY channel the boundary was taken from - the toolpath's own
    runs and every pass this added - and `added` is the second of those two on
    its own. The caller writes `added` to the toolpath layer and then writes
    `boundary` to `Edge.Cuts`, in that order, because that is the order the two
    things exist in: a pass that shapes the outline is a pass the router makes.
    """

    boundary: Boundary
    channels: tuple
    joins: tuple
    protected: tuple
    pinches: tuple
    passes: int
    added: tuple = ()
    standing: tuple = ()


# How many times the pinch cut is allowed to be added before the build gives up
# and says so. One pass finds every pinch the first arrangement has; a second
# exists because a cut can in principle graze something else; a third is the
# margin. Beyond that the geometry is not converging and a silent loop would be
# worse than an error.
MAX_PINCH_PASSES = 3


def derived_boundary(blank: Rect, channel_list: list, min_tab_mm: float,
                     guards: tuple = (), eliminate=None) -> Derived:
    """The panel edge, with every accidental sliver and every pinch cut away.

    THE WHOLE OF THE MINIMUM TAB IS HERE, in the order the two halves of it have
    to run. :func:`close_slivers` consumes the ribbons of thin material that the
    toolpath's own geometry leaves; the boolean then either produces a boundary
    or reports that the material is SEVERED somewhere - two walls touching,
    which is a sliver of width zero and which no measurement between two
    centrelines can find, because the two centrelines are nowhere near each
    other. That is cut and the boolean asked again.

    **Everything this adds comes back on `added`, and it is the answer's first
    half.** The outline is the boundary of the blank minus THESE channels, so
    the caller writes them to the toolpath layer and derives `Edge.Cuts` from
    the same set - one region, stated once, read twice.

    *eliminate* false leaves the thin ribbons standing (`standing` names them).
    **The PINCH pass is not on that switch**: material of ZERO width is not thin
    material a fab might prefer to keep, it is two cuts that met, and until it is
    cut the region has no boundary a walk can follow at all - so there would be
    no outline to leave alone. It is reported either way.

    Raises:
        ChannelError: when the boundary does not close for any other reason, and
            when the pinches do not stop coming - see :data:`MAX_PINCH_PASSES`.
    """
    enforced = close_slivers(
        list(channel_list), min_tab_mm, guards=guards, eliminate=eliminate
    )
    channels = list(enforced.channels)
    added = list(enforced.added)
    pinches: list = []
    for attempt in range(MAX_PINCH_PASSES + 1):
        try:
            boundary = region_boundary(blank, channels)
        except PinchError as pinch:
            if attempt == MAX_PINCH_PASSES:
                raise ChannelError(
                    f"the panel edge still pinches after {attempt} pass(es) of "
                    f"cutting the pinches out, so this arrangement is not "
                    f"converging: {pinch}",
                    pinch.at,
                ) from pinch
            found = _pinch_cuts(pinch.points, channels)
            if not found:
                raise
            pinches.extend(point for point, _ in found)
            channels.extend(channel for _, channel in found)
            added.extend(channel for _, channel in found)
            continue
        return Derived(
            boundary=boundary,
            channels=tuple(channels),
            joins=enforced.joins,
            protected=enforced.protected,
            pinches=tuple(pinches),
            passes=attempt + 1,
            added=tuple(added),
            standing=enforced.standing,
        )
    raise AssertionError("unreachable")


def _pinch_cuts(points, channels: list) -> list:
    """One cut per pinch: a short pass of the nearest bit, across the pinch.

    Across, because the two walls that touch run ALONG the pinch: the cut has to
    open the material out either side of the touching point, and the direction
    that does it is the one that leads away from the cut whose wall it is. Its
    length is that bit's own radius, which with the round ends of the pass makes
    a cut two radii wide and three long, centred on the pinch - enough to open
    any pinch, and confined to material that was within a cutter's reach of a
    cut already.
    """
    made = []
    for point in sorted(set(points)):
        nearest, radius = _nearest_item(point, channels)
        if nearest is None:
            continue
        foot = nearest_point_on(nearest, point)
        span = math.hypot(point[0] - foot[0], point[1] - foot[1])
        axis = ((point[0] - foot[0]) / span, (point[1] - foot[1]) / span) \
            if span > 1e-12 else (1.0, 0.0)
        half = radius / 2.0
        cut = Line(
            p0=(point[0] - axis[0] * half, point[1] - axis[1] * half),
            p1=(point[0] + axis[0] * half, point[1] + axis[1] * half),
            source=f"pinch|{point[0]:.6f}|{point[1]:.6f}",
            role="pinch",
        )
        made.append((point, Channel(path=(cut,), radius=radius, closed=False)))
    return made


def _nearest_item(point: tuple, channels: list) -> tuple:
    """The toolpath item nearest a point, and the radius it removes at."""
    best, best_radius, best_gap = None, 0.0, math.inf
    for channel in channels:
        for item in channel.path:
            gap = distance_to_primitive(point, item)
            if gap < best_gap:
                best, best_radius, best_gap = item, channel.radius, gap
    return best, best_radius


def region_boundary(blank: Rect, channels: list,
                    node_tol: float = BOUNDARY_NODE_TOL_MM) -> Boundary:
    """The boundary of the `blank` minus every channel, as closed loops.

    Raises:
        ChannelError: when the surviving pieces do not close into loops, which
            means the arrangement disagrees with itself and the answer would be
            an outline with a gap in it.
    """
    walls = []
    for channel in channels:
        curves, slacks = dilation_candidates(channel)
        walls.extend(
            (curve, slack, True)
            for curve, slack in zip(curves, slacks)
            if getattr(curve, "radius", math.inf) >= WALL_MIN_RADIUS_MM
        )
    candidates = walls + [(edge, 0.0, False) for edge in blank.edges()]
    if not candidates:
        raise ValueError(
            "a panel boundary needs a blank and it has no edges"
        )

    boxes = [padded_bbox(curve) for curve, _, _ in candidates]
    cell = index_cell_mm(boxes)
    grid = Grid(boxes, cell)
    cuts = [[] for _ in candidates]
    for index, box in enumerate(boxes):
        for other in grid.near(box):
            if other <= index or not boxes_overlap(box, boxes[other]):
                continue
            for point in bounded_intersections(
                candidates[index][0], candidates[other][0], tol=1e-7
            ):
                cuts[index].append(point)
                cuts[other].append(point)
    _split_edges_where_walls_land(candidates, cuts)

    items = [(item, channel.radius)
             for channel in channels for item in channel.path]
    material = Grid([padded_bbox(item, radius) for item, radius in items], cell)

    kept, pinched = [], []
    for (curve, slack, is_wall), points in zip(candidates, cuts):
        for piece in split_primitive(curve, points):
            if piece.length <= PIECE_MIN_MM:
                continue
            middle = midpoint_of(piece)
            # A wall outside the blank was never material, and a wall ON the
            # blank's edge is the blank's edge (BLANK_EDGE_TOL_MM): the panel
            # keeps one piece there, and it is the one the material has.
            if is_wall and not blank.contains(middle, tol=-BLANK_EDGE_TOL_MM):
                continue
            if _inside_the_cut(piece, middle, items, material, slack):
                continue
            if is_wall and _pinched(piece, middle, blank, items, material):
                pinched.append(middle)
                continue
            kept.append(piece)
    if pinched:
        raise PinchError(
            f"{len(pinched)} piece(s) of wall have cut on BOTH sides of them, "
            f"so the material is severed there and has no width",
            min(pinched), pinched,
        )

    loops = _loops(_deduplicated(kept), node_tol)
    return Boundary(
        loops=tuple(loops),
        candidates=len(candidates),
        pieces=len(kept),
        clearance_mm=_outer_clearance(blank, items),
    )


def _split_edges_where_walls_land(candidates: list, cuts: list) -> None:
    """Split the blank's edges wherever a channel wall ENDS on one. In place.

    Crossings are found by intersecting curves, and a wall that only TOUCHES the
    blank's edge crosses nothing: the routed fillet round a board's rounded
    corner runs tangent to the edge and ends on it, which is the ordinary
    arrangement once a rail is zero and the boards' own edge is the panel's. Left
    unsplit, the edge stays one long piece with a cut halfway along it - and the
    wall arriving at its middle makes three pieces meet at a node, which is the
    outline that would not close.

    Cheap: the walls' ends against four edges, and only ever adds a split point
    that the geometry already has a wall ending on (:data:`BLANK_EDGE_TOL_MM`).
    A panel whose rails are wider than the cutter has no such point at all,
    which is what makes this invisible to every panel built before it.
    """
    edges = [
        (index, curve)
        for index, (curve, _, is_wall) in enumerate(candidates)
        if not is_wall
    ]
    for curve, _, is_wall in candidates:
        if not is_wall:
            continue
        for point in (curve.start, curve.end):
            for index, edge in edges:
                if distance_to_primitive(point, edge) <= BLANK_EDGE_TOL_MM:
                    cuts[index].append(point)


# How far either side of a wall the pinch probe is taken. Four times
# BOUNDARY_NODE_TOL_MM, so a wall welded five micrometres onto its neighbour is
# still clearly on one side of the probe, and a fifth of the narrowest wall a
# 0.2 mm cutter leaves.
PINCH_PROBE_MM = 0.02


def _pinched(piece, middle: tuple, blank: Rect, items: list,
             index: Grid) -> bool:
    """Is this piece of wall standing in severed material?

    A piece of boundary has material on ONE side of it. A piece with cut on both
    - or with nothing on either, being outside the blank on one side and inside a
    cut on the other - is where two walls grazed each other and the material
    pinched to nothing. It is not an edge of anything, and keeping it writes a
    hair of `Edge.Cuts` that no cutter made.
    """
    normal = _normal_at(piece, middle)
    for sign in (1.0, -1.0):
        probe = (middle[0] + normal[0] * sign * PINCH_PROBE_MM,
                 middle[1] + normal[1] * sign * PINCH_PROBE_MM)
        if blank.contains(probe) and not _cut_away(probe, items, index, 0.0):
            return False
    return True


def _normal_at(piece, middle: tuple) -> tuple:
    """A unit normal to *piece* at its middle."""
    if isinstance(piece, Line):
        dx, dy = piece.p1[0] - piece.p0[0], piece.p1[1] - piece.p0[1]
        length = math.hypot(dx, dy) or 1.0
        return (-dy / length, dx / length)
    dx = middle[0] - piece.center[0]
    dy = middle[1] - piece.center[1]
    length = math.hypot(dx, dy) or 1.0
    return (dx / length, dy / length)


def _cut_away(point: tuple, items: list, index: Grid, slack: float) -> bool:
    """True when *point* is inside the material a cutter already removed.

    The test is the definition: a point is in the channel when it is closer to
    some toolpath item than that item's own radius. `slack` forgives the
    nanometre a curve moved when its ends were welded to its neighbours - the
    same allowance the mill offset makes, and for the same reason.
    """
    for other in index.near((point[0], point[1], point[0], point[1])):
        item, radius = items[other]
        if distance_to_primitive(point, item) < radius - REACH_TOL_MM - slack:
            return True
    return False


def _inside_the_cut(piece, middle: tuple, items: list, index: Grid,
                    slack: float) -> bool:
    """True when this piece of wall runs through material already removed.

    THE MIDDLE ALONE DOES NOT DECIDE IT. One probe settles a
    whole piece only because a piece is split wherever another curve CROSSES
    it - and two curves that lie on the SAME circle cross nowhere, so nothing
    splits them. That is the ordinary arrangement at a toolpath joint: the
    corner fillet is a full circle round the joint, the walk out and the walk
    back each produce it, and the two copies are coincident along part of their
    length and split at different points. It is the same coincidence
    :data:`BLANK_EDGE_TOL_MM` was written for, one circle further in.

    MEASURED on a camera board at a 1 mm gap: the long copy of one
    corner circle at (256.6743, 146.0600) mm read EXACTLY on the wall at its
    middle - zero to within a femtometre - and 370 um inside the minimum-tab
    cut at both of its ends. It was kept, and left one piece standing at a node
    where a boundary meets two, which is the refusal the panel build ended in.

    So the ENDS are probed too. A piece with an end inside the cut is not a
    piece of the edge: nothing survives there for it to meet.

    AN END IS PROBED AT THE NODE TOLERANCE, not at the nanometre the middle is.
    An end sits where another wall crosses, which is ON that wall, and a
    crossing solved in floating point lands tens of picometres to either side
    of it - measured: a wall end on the same board read 1e-8 mm inside a
    channel it only touches, ten times the welding allowance, and refusing it
    cost the whole panel. :data:`BOUNDARY_NODE_TOL_MM` is the distance at which
    two ends are already one node, so an end nearer the cut than that is
    indistinguishable from an end on its wall. Past it the end is inside, and
    370 um - the case this was written for - is seventy times past it.
    """
    if _cut_away(middle, items, index, slack):
        return True
    reach = slack + BOUNDARY_NODE_TOL_MM
    return any(
        _cut_away(point, items, index, reach)
        for point in (piece.start, piece.end)
    )


def _outer_clearance(blank: Rect, items: list) -> float:
    """How much bare material stands between the channels and the outer edge.

    Negative when a channel reaches across it, which is not an error: the
    boundary simply follows the channel out through the edge, and the panel has
    a bite taken out of its side.
    """
    clearance = min(blank.width_mm, blank.height_mm)
    for item, radius in items:
        box = chain_bbox([item])
        clearance = min(
            clearance,
            box[0] - blank.min_x - radius,
            box[1] - blank.min_y - radius,
            blank.max_x - box[2] - radius,
            blank.max_y - box[3] - radius,
        )
    return clearance


def _deduplicated(pieces: list) -> list:
    """Drop pieces that are the same piece of wall twice over.

    **Every corner of a run comes out twice, and that is by construction.**
    Walking a run out and back passes each joint twice - once travelling
    forwards, where the corner arc turns through the outside of the bend, and
    once travelling back, where the arc turns the long way round and its middle
    is that same outside of the bend. Both are at the offset distance from the
    path, so both survive; a node with two identical arcs leaving it is a node
    the boundary has no way through.

    They are compared as geometry rather than as numbers, because the two
    copies are not bit-identical - each was welded onto a different neighbour,
    so they differ in the last nanometre. Two pieces of the same kind, the same
    length and the same middle to within a tenth of a micrometre are one piece.
    """
    middles = [midpoint_of(piece) for piece in pieces]
    boxes = [
        (point[0] - DUPLICATE_TOL_MM, point[1] - DUPLICATE_TOL_MM,
         point[0] + DUPLICATE_TOL_MM, point[1] + DUPLICATE_TOL_MM)
        for point in middles
    ]
    grid = Grid(boxes, index_cell_mm(boxes))
    dropped: set = set()
    for index, piece in enumerate(pieces):
        if index in dropped:
            continue
        for other in grid.near(boxes[index]):
            if other <= index or other in dropped:
                continue
            twin = pieces[other]
            if (
                type(twin) is type(piece)
                and abs(twin.length - piece.length) <= DUPLICATE_TOL_MM
                and math.dist(middles[other], middles[index]) <= DUPLICATE_TOL_MM
            ):
                dropped.add(other)
    return [
        piece for index, piece in enumerate(pieces) if index not in dropped
    ]


def _loops(pieces: list, tol: float) -> list:
    """Chain the surviving pieces into closed loops, welded at their nodes."""
    if not pieces:
        return []
    nodes: list = []
    ends = [
        (node_of(nodes, piece.start, tol), node_of(nodes, piece.end, tol))
        for piece in pieces
    ]
    welded = [
        weld_end(weld_start(piece, nodes[start]), nodes[end])
        for piece, (start, end) in zip(pieces, ends)
    ]
    at_node: dict = {}
    for index, (start, end) in enumerate(ends):
        at_node.setdefault(start, []).append(index)
        at_node.setdefault(end, []).append(index)
    crowded = [
        nodes[node] for node, incident in at_node.items() if len(incident) > 2
    ]
    if crowded:
        # More than two pieces at a node means two walls crossed there and both
        # sides of both survived, which is the material pinching to zero width.
        # It is a cut that has to be made, not a walk that has to be cleverer.
        raise PinchError(
            f"{len(crowded)} node(s) where more than two pieces of the panel "
            f"edge meet, so the material is severed there and has no width",
            min(crowded), crowded,
        )
    for node, incident in at_node.items():
        if len(incident) != 2:
            raise ChannelError(
                f"{len(incident)} piece(s) of the panel edge meet here, and a "
                f"boundary meets exactly two - so this outline does not close",
                nodes[node],
            )

    unused = set(range(len(welded)))
    loops = []
    while unused:
        first = min(unused)
        steps, entry, current = [], ends[first][0], first
        while True:
            unused.discard(current)
            forward = ends[current][0] == entry
            steps.append(welded[current] if forward
                         else reverse(welded[current]))
            exit_node = ends[current][1] if forward else ends[current][0]
            following = [
                index for index in at_node[exit_node]
                if index != current and index in unused
            ]
            if not following:
                if exit_node != ends[first][0]:
                    raise ChannelError(
                        "the panel edge stops here instead of closing",
                        nodes[exit_node],
                    )
                break
            current, entry = following[0], exit_node
        loops.append(_canonical(merge_same_curve(steps)))
    loops.sort(key=lambda loop: chain_bbox(list(loop)))
    return loops


def _canonical(loop: list) -> tuple:
    """One loop written the same way every time it is computed.

    Counter-clockwise in file coordinates, starting at its lowest-leftmost
    corner. Neither is meaningful to KiCad - an `Edge.Cuts` loop is a loop
    whichever way round it is drawn - and both are what makes two builds of one
    panel byte-identical however the arrangement happened to be walked.
    """
    if signed_area(loop) < 0.0:
        loop = [reverse(piece) for piece in reversed(loop)]
    first = min(
        range(len(loop)),
        key=lambda index: (loop[index].start, index),
    )
    return tuple(loop[first:] + loop[:first])


__all__ = [
    "MAX_PINCH_PASSES",
    "MIN_ITEM_MM",
    "Boundary",
    "Channel",
    "ChannelError",
    "Derived",
    "MinTab",
    "PinchError",
    "Rect",
    "RunSet",
    "chain_runs",
    "close_slivers",
    "derived_boundary",
    "dilation_candidates",
    "doubled_chain",
    "grown",
    "region_boundary",
    "weld_along",
]
