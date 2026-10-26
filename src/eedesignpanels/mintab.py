"""The MINIMUM TAB: no accidental sliver of material survives between two cuts.

**Material left standing between two cuts is the centreline gap less one whole
cutter diameter**, because each cut takes a radius past its own centreline. So
a minimum tab WIDTH is a minimum toolpath DISTANCE, and this module is the one
place that arithmetic lives:

    material = centreline gap - (first cutter radius + second cutter radius)

**A tab is DECLARED.** A designer draws a staple on `User.8` and the router
stops either side of it; that is a tab, and it is protected. Everything else
the geometry happens to leave between two cuts is an ACCIDENT - a whisker of
board a hair wide, which breaks off in the fab's hands, jams a depanelizer, or
survives just long enough to tear a trace out of an edge. So any two cuts whose
surviving sliver would be narrower than `min_tab_mm` are treated as ONE CUT:
the sliver is consumed, by a straight cut joining the two nearest centreline
points at the wider of the two cutters. That is the shortest cut that consumes
it, and it is exact geometry - a line, which the file can state.

**A declared tab is never consumed silently, and never consumed at all.** When
`min_tab_mm` meets or exceeds a declared tab's own material width the setting
and the design disagree, and the DESIGN wins: the tab stands and is reported by
name, per tab. Warn, never fail - a number in a config file cannot
delete geometry a designer drew.

**What counts as an accident: the cut comes back near itself by a shortcut.**
Two points on ONE run are always some distance apart along the path, and along
a straight run that distance IS the gap - so a run travelling straight past
itself would otherwise report a sliver at every pair of items it is made of. It
is not one: the material there is not between two cuts, it is beside one. So a
pair on the same run is an accident only when the two points are FURTHER apart
along the path than the reach - `first radius + second radius + min_tab` - which
is precisely the condition for the gap through the air to be shorter than the
gap along the cut. A hairpin that turns back on itself qualifies; a straight
edge does not; and the two thresholds meet exactly, with no third constant.

**And a claim is measured against the WHOLE arrangement, over the whole width
of what it claims.** Two curves and two radii are not
the cut region: a third cut - very often the next item of one of their own runs
- may already have removed part of what the pair says stands between them. Any
pair CAN be asked, so every answer is checked (:func:`sliver_stands`), and the
inside of a corner is where checking only the narrowest point was not enough. A
run that turns a corner passes its own flank twice, so the arithmetic measures
from one leg to the other across the material's apex and calls the board's own
corner a sliver; the narrowest point of that measurement lands exactly on a cut
face, which counts as material, and the join that followed chamfered the corner
off - writing a straight panel edge no cutter produced. Measured on the corpus
panel at a 0.2 mm cutter: twenty-four such approaches, each claiming 0.38 to
0.44 mm where the arrangement leaves 0.18 to 0.33 mm, every one of them the
inside of a step in a board's own edge. A corner is not a whisker.

**A guard is a gap the design put there.** A tab's staple declares one, and so
does a scored edge the saw separates. A pair of points sitting in a guarded gap
is left alone. A tab guard is recognised by GEOMETRY - a chord across the gap
crosses both of the tab's stop lines - so the same rule serves the source board
and the panel, where the tabs are footprints and no run bookkeeping survives.

**A SLIVER IS A RIBBON, NOT A POINT.** An
approach is where two cuts are NEAREST, which is where a sliver is thinnest and
is not where it ends. Two cutter passes that leave 0.35 mm between them at their
closest usually leave about 0.35 mm between them for as far as they run
alongside each other, and one cut across the narrowest point makes the CUT
REGION continuous there - which is all the old rule claimed - while leaving the
rest of the ribbon standing: a detached chip on one side of the crossing and a
knife edge on the other. Measured on the corpus panel at a 0.6 mm cutter, the
old rule made twelve crossings and left twelve loose chips 0.35 x 0.19 mm and
twelve webs 12.5 um thick. So an approach is a SEED: the pair is walked in both
directions for as long as the material between them stands and stays under the
minimum tab, and the whole ribbon is consumed by a chain of crossings close
enough together that their swept stadiums overlap
(:func:`sliver_joins`). Every crossing is a pass of a real bit; nothing here
invents a cutter or a shape the file cannot state.

**What this rule still cannot see is the PINCH.** Where the material narrows to
nothing - two cuts that touch - the two centrelines are a whole cutter diameter
apart and no measurement between them will ever look at the point between. That
is found by the boolean instead, which is where it shows up as a node with four
pieces of edge meeting at it, and cut there:
:func:`eedesignpanels.channels.derived_boundary`.

**AND WHETHER ANY OF IT IS CUT AT ALL IS THE USER'S CHOICE.**
Consuming a sliver is more cutting than the design asked for, and a fab may
prefer the whisker to the extra pass. So the rule carries a second setting
beside its width: :class:`Rule` is the minimum tab as ONE number that also says
whether the router removes what falls under it. With it off nothing is
consumed, every ribbon that stands is named with its size and its position
(:func:`sliver_warning`), and the outline honestly follows the material that is
left. The width alone remains the whole of the arithmetic; the switch only says
whether the arithmetic's answer becomes a cut.

Pure geometry and one fabrication rule: millimetre floats, no board, no I/O.
It imports :mod:`eedesignpanels.toolpath` and `math`, and nothing else.
"""

import math
from typing import NamedTuple

from eedesignpanels.toolpath import (
    JOIN_TOL_MM,
    REACH_TOL_MM,
    Grid,
    Line,
    boxes_overlap,
    bounded_intersections,
    closest_approach,
    distance_to_primitive,
    index_cell_mm,
    nearest_point_on,
    padded_bbox,
    parameter_on,
    reverse,
    weld_gap,
)

# How much board the panel must leave between any two cuts that are not one
# declared tab. Half a millimetre is the fab's own figure for the thinnest
# whisker worth having: below it the material is swarf rather than structure.
MIN_TAB_DEFAULT_MM = 0.5

# ...and the floor under the setting itself. A tenth of a millimetre is the
# thinnest tab anybody has asked for and roughly the width of one v-groove
# shoulder; under it the "tab" is thinner than the tolerance any router holds,
# so a value below this cannot be honoured and is a CONFIG ERROR rather than a
# quietly clamped number. The core does not do what it was not asked to do.
MIN_TAB_FLOOR_MM = 0.1

# What a joining cut is called in the file, and in a derived uuid.
JOIN_ROLE = "min-tab"


class Rule(float):
    """The minimum tab: one width, and whether the router takes what is under it.

    **It IS the number**, a `float` subclass, so every caller that only ever
    wanted the millimetres - the arithmetic, the formatting, the comparisons,
    the config's own serialization - is untouched and cannot tell the difference.
    What rides along is one boolean, because the two halves of the build are
    reached through different calls and one fabrication rule stated twice is one
    that can disagree with itself (the argument `panel.json` already makes for
    keeping the width top level).

    A PLAIN float means `eliminate` is true, which is what this rule has always
    done - so a caller that drops the rule back to a number fails in the
    direction that keeps cutting the swarf away, and never in the direction that
    silently leaves a whisker in a fabrication output.
    """

    def __new__(cls, value, eliminate: bool = True) -> "Rule":
        rule = float.__new__(cls, value)
        rule.eliminate = bool(eliminate)
        return rule

    @classmethod
    def of(cls, value, eliminate=None) -> "Rule":
        """This rule from whatever the caller has: a number, or a rule already.

        *eliminate* is an explicit override - what a direct caller passes - and
        None means "whatever the value itself says", which is how the setting
        travels from the config to the two stages without anything in between
        having to know it exists.
        """
        return cls(value, eliminating(value, eliminate))

    def __repr__(self) -> str:
        return (f"{float(self):g} mm minimum tab"
                + ("" if self.eliminate else ", not enforced"))


def eliminating(value, override=None) -> bool:
    """Does this minimum tab remove what falls under it? A plain number does."""
    if override is not None:
        return bool(override)
    return bool(getattr(value, "eliminate", True))


def _unit_towards(one: tuple, other: tuple) -> tuple:
    """The direction from *one* to *other*, or +x when they are the same point."""
    span = math.hypot(other[0] - one[0], other[1] - one[1])
    if span == 0.0:
        return (1.0, 0.0)
    return ((other[0] - one[0]) / span, (other[1] - one[1]) / span)


def _stepped(point: tuple, unit: tuple, distance: float) -> tuple:
    return (point[0] + unit[0] * distance, point[1] + unit[1] * distance)


class Guard(NamedTuple):
    """A gap between two cuts that the DESIGN put there. Never consumed.

    `blades` are segments a chord across the gap must cross - ALL of them - for
    this guard to own the gap. A tab's two stop lines are its blades: the two
    cut faces sit one cutter radius back from them, so the chord between them
    spans the tab and crosses both, and nothing else on the board does.

    `ends` is the other way of saying it, for a caller that knows the two cut
    ends exactly: a v-score junction is recognised that way, because the runs
    either side of a scored edge end PARALLEL to the score rather than across
    it, and a chord between them crosses nothing at all.

    `material_mm` is the width the design declares for the gap, and NaN when it
    declares none - a score separates the board completely, so there is no tab
    there to protect or to report.
    """

    label: str
    kind: str
    at: tuple
    material_mm: float = float("nan")
    blades: tuple = ()
    ends: tuple = ()


class Protected(NamedTuple):
    """A DECLARED tab the minimum-tab setting would have eaten. The tab wins."""

    label: str
    at: tuple
    material_mm: float
    min_tab_mm: float


class Join(NamedTuple):
    """One accidental sliver, consumed. Two cuts become one.

    `at` and `to` are the nearest pair of CENTRELINE points on the two cuts -
    the measurement, and what the material figure is computed from.

    A join has two shapes, because it means two things:

    :attr:`move` is the cutter's TRAVEL from one run end to the other,
    centreline to centreline. That is what the source board's toolpath gets: an
    item of a run, welded to its neighbours at both ends, so it is a path the
    machine follows and its ends are shared nodes.

    :attr:`crossing` is the CUT ACROSS THE SLIVER - from one cut's face to the
    other's - which is what the panel's boolean gets, as a channel of its own.
    It stops short of both centrelines on purpose: a cut ending exactly on
    another cut's centreline has its round end tangent to BOTH of that cut's
    flanks at once, and a boundary cannot pass through a tangency. Face to face
    it crosses one flank squarely instead, and it removes nothing the two cuts
    had not already removed, because everything past a cut's face is inside it.
    """

    at: tuple
    to: tuple
    gap_mm: float
    material_mm: float
    radius_mm: float
    first_radius_mm: float = 0.0
    second_radius_mm: float = 0.0

    @property
    def length_mm(self) -> float:
        """How far the cutter travels to make the join."""
        return self.gap_mm

    @property
    def key(self) -> str:
        """This join's identity, from its geometry and nothing else.

        The two ends are sorted, so a join written travelling one way is the
        same join written travelling the other - which is what keeps a derived
        uuid from depending on how the runs happened to be walked.
        """
        low, high = sorted((self.at, self.to))
        return "|".join(
            [JOIN_ROLE] + [f"{value:.6f}" for point in (low, high)
                           for value in point]
        )

    @property
    def move(self) -> Line:
        """The cutter's travel, `at` -> `to`."""
        return Line(p0=self.at, p1=self.to, source=self.key, role=JOIN_ROLE)

    def move_from(self, point: tuple) -> Line:
        """The travel, run from whichever of its two ends is *point*."""
        if math.dist(point, self.at) <= math.dist(point, self.to):
            return self.move
        return reverse(self.move)

    @property
    def crossing(self) -> Line:
        """The cut across the sliver: face to face, and never shorter than the bit.

        Its length is the material it consumes - which at an exact tangency is
        nothing at all, and a cut of no length is not a cut. So it is floored at
        the joining bit's own radius, taken symmetrically out of the two cuts it
        joins. That bites into material both of them had already removed, and
        into nothing else.
        """
        axis = _unit_towards(self.at, self.to)
        first = self.first_radius_mm or self.radius_mm
        second = self.second_radius_mm or self.radius_mm
        short = max(0.0, self.radius_mm - self.material_mm) / 2.0
        start = _stepped(self.at, axis, first - short)
        end = _stepped(self.to, axis, -(second - short))
        return Line(p0=start, p1=end, source=self.key, role=JOIN_ROLE)


class Cut(NamedTuple):
    """One item of cutter path in a scan, and where it sits on its own run."""

    primitive: object
    radius: float
    owner: int
    at_mm: float
    owner_length_mm: float
    owner_closed: bool


class Approach(NamedTuple):
    """Two cuts at their nearest, and the material that would stand between."""

    first: int
    second: int
    at: tuple
    to: tuple
    gap_mm: float
    material_mm: float


class RunJoins(NamedTuple):
    """What enforcing the minimum tab did to one board's cutting runs."""

    runs: tuple
    joins: tuple
    protected: tuple

    @property
    def join_count(self) -> int:
        return len(self.joins)


def checked_min_tab(min_tab_mm: float, eliminate=None) -> Rule:
    """*min_tab_mm* if it can be honoured; otherwise say why it cannot.

    Comes back as a :class:`Rule` - the same number, carrying whether the router
    enforces it - so a stage that checks the setting has the whole setting.

    Raises:
        ValueError: below :data:`MIN_TAB_FLOOR_MM`. A stage that cannot do what
            it was asked says so and writes nothing.
    """
    if min_tab_mm < MIN_TAB_FLOOR_MM:
        raise ValueError(
            f"min_tab_mm is {min_tab_mm:.4f} mm, under the {MIN_TAB_FLOOR_MM} mm "
            f"floor. A tab that thin is under the tolerance a router holds, so "
            f"asking for it cannot be honoured - raise the value, or leave the "
            f"key out for the {MIN_TAB_DEFAULT_MM} mm default."
        )
    return Rule.of(min_tab_mm, eliminate)


def material_mm(gap_mm: float, first_radius: float,
                second_radius: float) -> float:
    """Board left standing between two cuts whose centrelines are *gap_mm* apart.

    The whole of the physics, in one line: each cut takes its own radius past
    its centreline, so the material is the gap less both radii - one whole
    cutter diameter when the same bit made both cuts.
    """
    return gap_mm - first_radius - second_radius


# ---------------------------------------------------------------------------
# the gaps the design declares
# ---------------------------------------------------------------------------


def tab_guards(tabs) -> tuple:
    """One guard per declared tab, recognised by its own two stop lines.

    Duck-typed on `route_toolpath.Tab`: `label`, `position`, `base` and
    `stop_lines`. The base's length is the tab's declared material width -
    measured from the board rather than derived, which is the same identity
    `Bridge.agrees` checks.
    """
    return tuple(
        Guard(
            label=tab.label,
            kind="tab",
            at=tab.position,
            material_mm=tab.base.length,
            blades=tuple(tab.stop_lines),
        )
        for tab in tabs
        if len(tab.stop_lines) == 2
    )


def score_guards(scores, junctions) -> tuple:
    """One guard per scored edge the router finished at both ends.

    A scored edge is a gap in the toolpath the designer asked for, and its two
    ends are exactly where the runs either side stopped - which the junction
    records already say. Recognised by those two points rather than by a chord
    test, because a run finishing past a score plane ends alongside the score,
    not across it.
    """
    by_score: dict = {}
    for junction in junctions:
        by_score.setdefault(junction.score, []).append(junction.at)
    guards = []
    for score in scores:
        points = sorted(by_score.get(score.index, ()))
        if len(points) == 2:
            guards.append(Guard(
                label=score.label,
                kind="score",
                at=points[0],
                ends=(points[0], points[1]),
            ))
    return tuple(guards)


def guarded(first: tuple, second: tuple, guards,
            tol: float = JOIN_TOL_MM) -> object:
    """The declared gap this pair of points sits in, or None."""
    for guard in guards:
        if guard.ends and _same_pair(guard.ends, first, second, tol):
            return guard
        if guard.blades and all(
            bounded_intersections(
                Line(p0=first, p1=second, source=JOIN_ROLE, role="chord"),
                blade, tol=1e-7,
            )
            for blade in guard.blades
        ):
            return guard
    return None


def _same_pair(ends: tuple, first: tuple, second: tuple, tol: float) -> bool:
    return (
        (math.dist(ends[0], first) <= tol and math.dist(ends[1], second) <= tol)
        or (math.dist(ends[0], second) <= tol
            and math.dist(ends[1], first) <= tol)
    )


def protected_tabs(guards, min_tab_mm: float) -> tuple:
    """Every declared tab this setting would have consumed. It consumes none."""
    return tuple(
        Protected(
            label=guard.label,
            at=guard.at,
            material_mm=guard.material_mm,
            min_tab_mm=min_tab_mm,
        )
        for guard in guards
        if guard.kind == "tab"
        and guard.material_mm == guard.material_mm  # not NaN
        and guard.material_mm <= min_tab_mm
    )


def protection_warning(entry: Protected) -> str:
    """One loud line per declared tab the setting exceeds. Never aggregated."""
    return (
        f"MIN TAB EXCEEDS A DECLARED TAB: min_tab_mm is "
        f"{entry.min_tab_mm:.4f} mm and tab {entry.label} declares "
        f"{entry.material_mm:.4f} mm of material, so this setting would consume "
        f"it. It is NOT consumed - a declared tab wins over the setting - but "
        f"the two disagree: lower min_tab_mm below {entry.material_mm:.4f} mm, "
        f"or widen the tab."
    )


def join_warning(joins, min_tab_mm: float) -> str:
    """One terse aggregated line for every sliver consumed, or "" for none."""
    if not joins:
        return ""
    worst = min(joins, key=lambda join: (join.material_mm, join.at))
    return (
        f"{len(joins)} accidental sliver(s) under the {min_tab_mm:.4f} mm "
        f"minimum tab were consumed into the cut, the thinnest "
        f"{worst.material_mm:.4f} mm at ({worst.at[0]:.4f}, {worst.at[1]:.4f}) "
        f"mm - no tab was declared there, so it was swarf rather than structure"
    )


# ---------------------------------------------------------------------------
# scanning a whole arrangement of cuts
# ---------------------------------------------------------------------------


def cuts_of(paths) -> list:
    """`(path, radius, closed)` triples flattened into indexed cut items."""
    cuts = []
    for owner, (path, radius, closed) in enumerate(paths):
        items = list(path)
        total = sum(item.length for item in items)
        at = 0.0
        for item in items:
            cuts.append(Cut(
                primitive=item,
                radius=radius,
                owner=owner,
                at_mm=at,
                owner_length_mm=total,
                owner_closed=closed,
            ))
            at += item.length
    return cuts


def material_index(cuts: list) -> Grid:
    """An index of the material every cut removes. See :func:`standing_at`."""
    return Grid(
        [padded_bbox(cut.primitive, cut.radius) for cut in cuts],
        index_cell_mm([padded_bbox(cut.primitive, cut.radius) for cut in cuts]),
    )


def standing_at(point: tuple, cuts: list, index: Grid) -> bool:
    """True when *point* is still BOARD - outside every cut's own ribbon.

    **The test that tells a sliver from an artefact of asking pairwise.** Two
    items of cutter path can be a millimetre apart and have nothing between
    them, because a third item - very often the next item of one of their own
    runs - already removed everything in between. The gap arithmetic cannot see
    that: it knows two curves and their radii, not the arrangement. So an
    approach counts only where there is material, which is what this asks, of
    the WHOLE arrangement, at one point. :func:`sliver_stands` asks it along the
    whole width the approach claims, which is what an approach is judged by.

    Measured on the corpus panel: without it, eighteen approaches inside the
    two routed notches of the board's own edges reported slivers of 0.30 to
    0.37 mm - every one of them a place the cutter had already been.
    """
    for other in index.near((point[0], point[1], point[0], point[1])):
        cut = cuts[other]
        if distance_to_primitive(point, cut.primitive) < cut.radius - REACH_TOL_MM:
            return False
    return True


def material_span(at: tuple, to: tuple, first_radius: float,
                  second_radius: float) -> Line:
    """The sliver two cuts claim to leave, as a segment: face to face.

    `Join.crossing` without its floor - the same construction, because it is
    the same thing: the part of the chord between the two cuts that is supposed
    to be board.
    """
    axis = _unit_towards(at, to)
    return Line(
        p0=_stepped(at, axis, first_radius),
        p1=_stepped(to, axis, -second_radius),
        source=JOIN_ROLE,
        role="chord",
    )


def sliver_stands(at: tuple, to: tuple, first_radius: float,
                  second_radius: float, cuts: list, index: Grid,
                  measured: tuple = ()) -> bool:
    """True when the whole sliver two cuts claim to leave is really board.

    **The test that tells a sliver from the inside of a corner.**
    :func:`standing_at` asks at the narrowest point of the approach, and that
    is the whole answer only where the two cuts asked about are the only ones
    near it. Where a run turns a corner it is not: the pair measures across the
    material's apex from one leg to the other, the narrowest point of that
    chord lands exactly on a cut's own face - which counts as material - and
    the arrangement has already removed most of what the arithmetic claims. So
    the claim is checked over its whole width: no cut may come nearer to the
    claimed sliver than its own radius.

    The two cuts BEING measured are `measured` and are exempt. The span's ends
    sit exactly on their faces by construction, and a face is not an intrusion.

    A tangency claims no width at all - the sliver is one point, which is under
    any minimum tab there is - so there is nothing to walk and the narrowest
    point is again the whole answer.
    """
    claimed = material_mm(math.dist(at, to), first_radius, second_radius)
    if claimed <= REACH_TOL_MM:
        return standing_at(
            ((at[0] + to[0]) / 2.0, (at[1] + to[1]) / 2.0), cuts, index
        )
    span = material_span(at, to, first_radius, second_radius)
    for other in index.near(padded_bbox(span)):
        if other in measured:
            continue
        cut = cuts[other]
        _, _, gap = closest_approach(span, cut.primitive)
        if gap < cut.radius - REACH_TOL_MM:
            return False
    return True


def _along_path(first: Cut, at: tuple, second: Cut, to: tuple) -> float:
    """How far apart two points on the SAME run are along the run itself."""
    one = first.at_mm + parameter_on(first.primitive, at) * first.primitive.length
    two = second.at_mm + parameter_on(second.primitive, to) * second.primitive.length
    apart = abs(one - two)
    if first.owner_closed and first.owner_length_mm > 0.0:
        apart = min(apart, first.owner_length_mm - apart)
    return apart


def approaches(cuts: list, min_tab_mm: float, guards=()) -> list:
    """Every place two cuts leave less than *min_tab_mm* of board between them.

    Thinned: two approaches between the same pair of runs are never reported
    closer together than the reach, because one join already makes the cut
    region continuous there and a second within a cutter's width of it would
    only remove material the first removed.
    """
    if not cuts:
        return []
    boxes = [
        padded_bbox(cut.primitive, cut.radius + min_tab_mm) for cut in cuts
    ]
    grid = Grid(boxes, index_cell_mm(boxes))
    material = material_index(cuts)

    found = []
    for index, box in enumerate(boxes):
        for other in sorted(grid.near(box)):
            if other <= index or not boxes_overlap(box, boxes[other]):
                continue
            first, second = cuts[index], cuts[other]
            reach = first.radius + second.radius + min_tab_mm
            at, to, gap = closest_approach(first.primitive, second.primitive)
            if gap >= reach:
                continue
            if (
                first.owner == second.owner
                and _along_path(first, at, second, to) <= reach
            ):
                continue
            if not sliver_stands(
                at, to, first.radius, second.radius, cuts, material,
                measured=(index, other),
            ):
                continue
            if guarded(at, to, guards) is not None:
                continue
            # Written lowest end first - and the two cuts follow their own
            # ends, because each end belongs to one of them - so the approach
            # between two cuts is one answer however they were handed over.
            ends = ((index, at), (other, to))
            (low_cut, low), (high_cut, high) = sorted(
                ends, key=lambda entry: entry[1]
            )
            found.append(Approach(
                first=low_cut,
                second=high_cut,
                at=low,
                to=high,
                gap_mm=gap,
                material_mm=material_mm(gap, first.radius, second.radius),
            ))

    found.sort(key=lambda entry: (entry.at, entry.to, entry.first, entry.second))
    return _thinned(found, cuts, min_tab_mm)


# ---------------------------------------------------------------------------
# A SLIVER IS A RIBBON, NOT A POINT
# ---------------------------------------------------------------------------
# :func:`approaches` finds where two cuts are NEAREST. That is where a sliver is
# thinnest and it is not where a sliver ends: two cutter passes that leave
# 0.35 mm between them at their closest usually leave about 0.35 mm between them
# for as far as they run alongside each other. One cut across the narrowest
# point makes the CUT REGION continuous there - which is all the old rule
# claimed - and leaves the rest of the ribbon standing: a detached chip on one
# side of the cut and a knife edge on the other. Measured on the corpus panel at
# a 0.6 mm cutter: twelve joins, twelve loose chips 0.35 x 0.19 mm, and twelve
# webs 12.5 um thick.
#
# So an approach is a SEED. From it the pair is walked in both directions for as
# long as the material between them stands and stays under the minimum tab, and
# the whole of that ribbon is cut away by a chain of crossings close enough
# together that their swept stadiums overlap.

# How far along the run each step of the walk goes, as a fraction of the minimum
# tab. A quarter is four readings across the narrowest thing the rule cares
# about, and the ribbon's ends are found to within that.
SLIVER_STEP_FRACTION = 0.25

# How far apart the crossings are placed, as a multiple of the joining bit's
# RADIUS. Each crossing removes a band a whole diameter wide across the ribbon,
# so anything under two radii overlaps its neighbour; 1.6 leaves a fifth of the
# bit's width of overlap at each joint, which is slack for the sub-micrometre
# welding without doubling the number of cuts.
COVER_FRACTION = 1.6

# Two crossings whose middles are this close - as a fraction of the joining
# radius - are one crossing. Ribbons found from two different seeds overlap
# where the same material was reached twice, and a duplicate cut removes
# nothing new.
DEDUPE_FRACTION = 0.25


def run_indices(cuts: list) -> dict:
    """`{owner: [cut index, ...]}` in the order each run was walked."""
    runs: dict = {}
    for index, cut in enumerate(cuts):
        runs.setdefault(cut.owner, []).append(index)
    for indices in runs.values():
        indices.sort(key=lambda index: cuts[index].at_mm)
    return runs


def along_run(cuts: list, indices: list, distance: float):
    """The point *distance* millimetres along a run, and which cut it is on.

    ``None`` when the distance falls off either end - an open run stops where it
    stops, which is what bounds a walk along it.
    """
    if distance < 0.0:
        return None
    for index in indices:
        cut = cuts[index]
        length = cut.primitive.length
        if distance <= cut.at_mm + length or index == indices[-1]:
            if distance > cut.at_mm + length:
                return None
            local = (distance - cut.at_mm) / length if length else 0.0
            if local < 0.0:
                return None
            return _point_at(cut.primitive, local), index
    return None


def _point_at(primitive, local: float) -> tuple:
    """A point a fraction *local* along one primitive."""
    if isinstance(primitive, Line):
        return (
            primitive.p0[0] + local * (primitive.p1[0] - primitive.p0[0]),
            primitive.p0[1] + local * (primitive.p1[1] - primitive.p0[1]),
        )
    sweep = primitive.sweep * (local if primitive.ccw else -local)
    return primitive.point_at_angle(primitive.a0 + sweep)


def distance_along(cuts: list, index: int, point: tuple) -> float:
    """How far along its own run a point on cut *index* is."""
    cut = cuts[index]
    return cut.at_mm + parameter_on(cut.primitive, point) * cut.primitive.length


# How far either side of the partner the walk looks, in items of the run it is
# measuring against. A ribbon is walked by FOLLOWING the cut that faces it, not
# by asking each time what is nearest: a step along one leg of a mouth is a
# fraction of a millimetre nearer the channel the mouth opens into than it is to
# the mouth's other leg, and a search for the nearest partner walks off the
# ribbon at the first step. Two items either side is enough to carry the partner
# round a fillet without letting it jump to another part of the board.
PARTNER_ITEMS = 2


def nearest_on_run(cuts: list, indices: list, point: tuple,
                   skip_near: tuple = (), near_item: int = -1) -> tuple:
    """The nearest point on a run to *point*, and which cut it is on.

    *skip_near* is `(distance_along, reach, total_length, closed)` and excludes
    the stretch of run within *reach* of that position - what a run measured
    against ITSELF has to leave out, because every point of a run is nearest to
    itself.

    *near_item* is a cut index the answer must stay beside: only that item and
    its :data:`PARTNER_ITEMS` neighbours along the run are considered. -1 looks
    at the whole run, which is what finding a partner for the first time means.
    """
    searched = indices
    if near_item >= 0 and near_item in indices:
        place = indices.index(near_item)
        low = max(0, place - PARTNER_ITEMS)
        searched = indices[low:place + PARTNER_ITEMS + 1]
    best, best_index, best_gap = None, -1, math.inf
    for index in searched:
        cut = cuts[index]
        if skip_near and _overlaps_along(cut, skip_near):
            continue
        found = nearest_point_on(cut.primitive, point)
        gap = math.dist(point, found)
        if gap < best_gap:
            best, best_index, best_gap = found, index, gap
    return best, best_index, best_gap


def _overlaps_along(cut, skip_near: tuple) -> bool:
    """Does this cut lie within the excluded stretch of its own run?"""
    at, reach, total, closed = skip_near
    low, high = cut.at_mm, cut.at_mm + cut.primitive.length
    for shift in ((0.0, -total, total) if closed and total > 0.0 else (0.0,)):
        if low - shift <= at + reach and high - shift >= at - reach:
            return True
    return False


def sliver_ribbon(seed: Approach, cuts: list, runs: dict, material: Grid,
                  min_tab_mm: float, guards=()) -> list:
    """The whole ribbon of thin material a seed approach sits on.

    Returns the walk's readings, in order along the first run:
    `[(distance_along, at, to, gap_mm), ...]`. Empty when the seed itself does
    not stand, which is how a seed that was only ever a corner drops out.
    """
    first, second = cuts[seed.first], cuts[seed.second]
    reach = first.radius + second.radius + min_tab_mm
    own = first.owner == second.owner
    indices = runs[second.owner]
    start = distance_along(cuts, seed.first, seed.at)
    step = max(min_tab_mm * SLIVER_STEP_FRACTION, 1e-3)
    forward = _walk_ribbon(cuts, runs[first.owner], indices, start, step,
                           first, second, material, min_tab_mm, guards,
                           reach, own, seed.second)
    backward = _walk_ribbon(cuts, runs[first.owner], indices, start, -step,
                            first, second, material, min_tab_mm, guards,
                            reach, own, seed.second)
    return sorted(set(backward + forward))


def _walk_ribbon(cuts, first_indices, second_indices, start, step, first,
                 second, material, min_tab_mm, guards, reach, own,
                 partner: int) -> list:
    """One direction of the ribbon walk, seed included, stopping at its end.

    *partner* is the cut on the far side of the ribbon at the seed, and the walk
    keeps hold of it: the far side is followed, never searched for again.
    """
    readings, distance = [], start
    while True:
        found = along_run(cuts, first_indices, distance)
        if found is None:
            return readings
        at, cut_index = found
        skip = ((distance, reach, first.owner_length_mm, first.owner_closed)
                if own else ())
        to, other_index, gap = nearest_on_run(
            cuts, second_indices, at, skip, near_item=partner
        )
        if to is None:
            return readings
        partner = other_index
        standing = material_mm(gap, first.radius, second.radius)
        if standing < 0.0 or standing >= min_tab_mm:
            return readings
        if not sliver_stands(at, to, first.radius, second.radius, cuts,
                             material, measured=(cut_index, other_index)):
            return readings
        if guarded(at, to, guards) is not None:
            return readings
        readings.append((round(distance, 9), at, to, gap))
        distance += step


class Sliver(NamedTuple):
    """One whole ribbon of accidental thin material, walked from end to end.

    What :func:`sliver_joins` cuts away, stated as the THING rather than as the
    passes - which is what a user who has switched the cutting off needs to
    read: how thin, how long, and where.

    `at` and `to` are the thinnest reading's two centreline points, so the
    position named is the worst of it. `readings` is the walk itself, which is
    what the covering chain is spaced along.
    """

    at: tuple
    to: tuple
    material_mm: float
    length_mm: float
    radius_mm: float
    first_radius_mm: float
    second_radius_mm: float
    readings: tuple = ()

    @property
    def middle(self) -> tuple:
        """The thinnest point itself - the middle of the material, not of a cut."""
        return ((self.at[0] + self.to[0]) / 2.0, (self.at[1] + self.to[1]) / 2.0)


def sliver_ribbons(cuts: list, min_tab_mm: float, guards=()) -> tuple:
    """Every ribbon of accidental thin material in one arrangement.

    Seeds come from :func:`approaches` - two cuts at their nearest, with
    material between them that stands and that no declared tab owns - and each
    is grown into the whole ribbon it sits on (:func:`sliver_ribbon`). A seed
    that turns out to be the inside of a corner walks nowhere and drops out.

    Two seeds can find one ribbon, so these are the RAW findings: the covering
    chain deduplicates its crossings and a report deduplicates the ribbons
    (:func:`distinct_slivers`). Neither is done here, because one join removed
    is a millimetre of cut and one line removed is a line of a report.
    """
    seeds = approaches(cuts, min_tab_mm, guards=guards)
    if not seeds:
        return ()
    runs = run_indices(cuts)
    material = material_index(cuts)
    found: list = []
    for seed in seeds:
        first, second = cuts[seed.first], cuts[seed.second]
        readings = sliver_ribbon(seed, cuts, runs, material, min_tab_mm, guards)
        if not readings:
            continue
        widths = [
            material_mm(gap, first.radius, second.radius)
            for _distance, _at, _to, gap in readings
        ]
        worst = min(range(len(widths)), key=lambda index: widths[index])
        found.append(Sliver(
            at=readings[worst][1],
            to=readings[worst][2],
            material_mm=widths[worst],
            length_mm=readings[-1][0] - readings[0][0],
            radius_mm=max(first.radius, second.radius),
            first_radius_mm=first.radius,
            second_radius_mm=second.radius,
            readings=tuple(readings),
        ))
    return tuple(found)


def distinct_slivers(ribbons) -> tuple:
    """One entry per ribbon of material, for a report. Order is the finding's.

    Two seeds that walked the same ribbon describe one piece of board, and a
    report that named it twice would have the user hunting for a second
    whisker that is not there. They are the same ribbon when their thinnest
    points coincide.
    """
    kept: list = []
    distinct: list = []
    for ribbon in ribbons:
        middle = ribbon.middle
        close = max(ribbon.radius_mm * DEDUPE_FRACTION, 1e-4)
        if any(math.dist(middle, seen) <= close for seen in kept):
            continue
        kept.append(middle)
        distinct.append(ribbon)
    return tuple(distinct)


def sliver_warning(ribbon: Sliver, min_tab_mm: float) -> str:
    """One loud line per ribbon left standing. Named, measured and located.

    Said per sliver rather than aggregated: the user switched the cutting
    off, so each of these is a piece of board the user has decided to keep and has to
    be able to find.
    """
    return (
        f"SLIVER LEFT STANDING: {ribbon.material_mm:.4f} mm of material runs "
        f"{ribbon.length_mm:.3f} mm at ({ribbon.middle[0]:.4f}, "
        f"{ribbon.middle[1]:.4f}) mm, under the {min_tab_mm:.4f} mm minimum tab "
        f"and with no tab declared across it - eliminate_slivers is off, so it "
        f"is routed round rather than away and the outline follows it"
    )


def sliver_joins(cuts: list, min_tab_mm: float, guards=()) -> tuple:
    """Every crossing needed to consume every accidental sliver. The whole rule.

    Each ribbon (:func:`sliver_ribbons`) is covered with crossings close enough
    together that their swept stadiums overlap; the crossings from every ribbon
    are then deduplicated, so a ribbon found twice is cut once.
    """
    made: list = []
    for ribbon in sliver_ribbons(cuts, min_tab_mm, guards=guards):
        for _distance, at, to, gap in _spaced(
            list(ribbon.readings), ribbon.radius_mm
        ):
            made.append(Join(
                at=at,
                to=to,
                gap_mm=gap,
                material_mm=material_mm(
                    gap, ribbon.first_radius_mm, ribbon.second_radius_mm
                ),
                radius_mm=ribbon.radius_mm,
                first_radius_mm=ribbon.first_radius_mm,
                second_radius_mm=ribbon.second_radius_mm,
            ))
    return _deduped(made)


def covering_joins(runs, radius: float, min_tab_mm: float,
                   guards=()) -> tuple:
    """The crossings one board's finished cutting runs need. Board level.

    The same rule the panel enforces between its channels, asked of the runs a
    single board's toolpath is made of - which is where it has to be asked,
    because a sliver inside a board is the BOARD's and the ink that removes it
    belongs in the board's own file.
    """
    paths = [(list(run), radius, False) for run in runs if run]
    if not paths:
        return ()
    return sliver_joins(cuts_of(paths), min_tab_mm, guards=guards)


def standing_slivers(runs, radius: float, min_tab_mm: float,
                     guards=()) -> tuple:
    """What :func:`covering_joins` would have cut, as pieces of board. Reporting."""
    paths = [(list(run), radius, False) for run in runs if run]
    if not paths:
        return ()
    return distinct_slivers(
        sliver_ribbons(cuts_of(paths), min_tab_mm, guards=guards)
    )


def _spaced(readings: list, radius: float) -> list:
    """Readings thinned to a covering chain: both ends, and no gap left open."""
    if not readings:
        return []
    spacing = max(COVER_FRACTION * radius, 1e-3)
    kept = [readings[0]]
    for reading in readings[1:-1]:
        if reading[0] - kept[-1][0] >= spacing:
            kept.append(reading)
    if len(readings) > 1 and readings[-1] is not kept[-1]:
        kept.append(readings[-1])
    return kept


def _deduped(joins: list) -> tuple:
    """Crossings whose middles coincide are one crossing. Order is the answer's."""
    kept: list = []
    for join in sorted(joins, key=lambda entry: (entry.at, entry.to)):
        middle = (
            (join.at[0] + join.to[0]) / 2.0, (join.at[1] + join.to[1]) / 2.0
        )
        close = max(join.radius_mm * DEDUPE_FRACTION, 1e-4)
        if any(
            math.dist(middle, other) <= close
            and abs(other_join.radius_mm - join.radius_mm) < 1e-9
            for other, other_join in kept
        ):
            continue
        kept.append((middle, join))
    return tuple(join for _middle, join in kept)


def _thinned(found: list, cuts: list, min_tab_mm: float) -> list:
    kept: list = []
    for entry in found:
        pair = tuple(sorted(
            (cuts[entry.first].owner, cuts[entry.second].owner)
        ))
        reach = (
            cuts[entry.first].radius + cuts[entry.second].radius + min_tab_mm
        )
        if any(
            other_pair == pair and math.dist(other.at, entry.at) <= reach
            for other_pair, other in kept
        ):
            continue
        kept.append((pair, entry))
    return [entry for _, entry in kept]


# ---------------------------------------------------------------------------
# the source board: joining the ENDS of cutting runs
# ---------------------------------------------------------------------------


def join_run_ends(runs, radius: float, min_tab_mm: float,
                  guards=(), consume: bool = True) -> RunJoins:
    """Merge cutting runs whose ends leave less than *min_tab_mm* between them.

    The board-level half of the rule, and it is about ENDS: a run of the offset
    loop stops where something declared stopped it, so a pair of ends closer
    than the reach is either a gap the design asked for - guarded, and left
    alone - or a sliver nobody asked for, in which case the two runs are one
    run and the cut travels straight across.

    Each end joins at most once, because a path has two ends and no more.
    Ties are settled by the thinnest sliver first, so the join made is the one
    that had to be made.

    *consume* false FINDS the same slivers and cuts none of them: the runs come
    back exactly as they went in and `joins` is what the setting would have done,
    which is what a report names when the user has switched it off. The
    measurement and the decision are separated rather than skipped, because a
    sliver nobody is told about is the same defect either way.
    """
    ends = []
    for index, run in enumerate(runs):
        if not run:
            continue
        ends.append((index, 0, run[0].start))
        ends.append((index, 1, run[-1].end))

    cuts = cuts_of([(run, radius, False) for run in runs if run])
    material = material_index(cuts) if cuts else None

    pairs = []
    for one in range(len(ends)):
        for two in range(one + 1, len(ends)):
            (run_one, side_one, point_one) = ends[one]
            (run_two, side_two, point_two) = ends[two]
            if run_one == run_two and side_one == side_two:
                continue
            gap = math.dist(point_one, point_two)
            # Two ends already together are already ONE cut - the closed loop a
            # board with no tabs produces is the everyday case - and there is
            # no sliver between them to consume.
            if gap <= JOIN_TOL_MM:
                continue
            left = material_mm(gap, radius, radius)
            if left >= min_tab_mm:
                continue
            middle = (
                (point_one[0] + point_two[0]) / 2.0,
                (point_one[1] + point_two[1]) / 2.0,
            )
            if material is not None and not standing_at(middle, cuts, material):
                continue
            if guarded(point_one, point_two, guards) is not None:
                continue
            pairs.append((left, point_one, point_two, gap, one, two))
    pairs.sort()

    used: set = set()
    links: dict = {}
    joins = []
    for left, point_one, point_two, gap, one, two in pairs:
        if one in used or two in used:
            continue
        used.update((one, two))
        low, high = sorted((point_one, point_two))
        join = Join(at=low, to=high, gap_mm=gap, material_mm=left,
                    radius_mm=radius, first_radius_mm=radius,
                    second_radius_mm=radius)
        joins.append(join)
        first_key = (ends[one][0], ends[one][1])
        second_key = (ends[two][0], ends[two][1])
        links[first_key] = (second_key, join)
        links[second_key] = (first_key, join)

    merged = (
        _assembled(runs, links) if links and consume
        else tuple(tuple(run) for run in runs if run)
    )
    return RunJoins(
        runs=merged,
        joins=tuple(sorted(joins, key=lambda join: (join.at, join.to))),
        protected=protected_tabs(guards, min_tab_mm),
    )


def _flipped(run) -> list:
    return [reverse(piece) for piece in reversed(list(run))]


def _appended(pieces: list, run: list) -> list:
    """*run* added to *pieces*, welding the seam exactly where there is one.

    Two ends a fraction of a micrometre apart are two nodes to anything that
    dilates this path later, and a boundary cannot pass through a node of
    degree four - the same reason `route_toolpath.emittable` welds what it
    drops.
    """
    if not pieces:
        return list(run)
    before, after = weld_gap(pieces[-1], run[0])
    return pieces[:-1] + [before, after] + list(run[1:])


def _assembled(runs, links: dict) -> tuple:
    """The runs walked into merged runs, following every join exactly once."""
    remaining = {index for index, run in enumerate(runs) if run}
    starts = [
        (index, side)
        for index in sorted(remaining)
        for side in (0, 1)
        if (index, side) not in links
    ]
    # ...and then whatever is left, which is a chain that closed on itself.
    starts += [(index, 0) for index in sorted(remaining)]

    merged = []
    for index, side in starts:
        if index not in remaining:
            continue
        merged.append(tuple(_walk(runs, links, remaining, index, side)))
    return tuple(merged)


def _walk(runs, links: dict, remaining: set, first: int, first_side: int) -> list:
    pieces: list = []
    current, entry = first, first_side
    while True:
        remaining.discard(current)
        run = list(runs[current]) if entry == 0 else _flipped(runs[current])
        pieces = _appended(pieces, run)
        link = links.get((current, 1 - entry))
        if link is None:
            return pieces
        (following, side), join = link
        cut = join.move_from(pieces[-1].end)
        if cut.length > JOIN_TOL_MM:
            pieces.append(cut)
        if following not in remaining:
            if cut.length <= JOIN_TOL_MM and len(pieces) > 1:
                before, after = weld_gap(pieces[-1], pieces[0])
                pieces[-1], pieces[0] = before, after
            return pieces
        current, entry = following, side


__all__ = [
    "JOIN_ROLE",
    "MIN_TAB_DEFAULT_MM",
    "MIN_TAB_FLOOR_MM",
    "Approach",
    "Cut",
    "Guard",
    "Join",
    "Protected",
    "Rule",
    "RunJoins",
    "Sliver",
    "approaches",
    "checked_min_tab",
    "covering_joins",
    "cuts_of",
    "distinct_slivers",
    "eliminating",
    "guarded",
    "join_run_ends",
    "join_warning",
    "material_index",
    "material_mm",
    "material_span",
    "protected_tabs",
    "protection_warning",
    "score_guards",
    "sliver_joins",
    "sliver_ribbons",
    "sliver_stands",
    "sliver_warning",
    "standing_at",
    "standing_slivers",
    "tab_guards",
]
