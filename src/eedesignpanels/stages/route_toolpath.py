"""Source prep: draw the router's path on `User.36` and its v-scores on `User.38`.

The model: the router runs along the board outline
until it hits a tab, then it stops. So this offsets the closed `Edge.Cuts` loop
OUTWARD by half the cutter diameter - the path the middle of the bit follows -
splits that loop where each tab's stop lines block it, drops the span that
bridges each tab, and keeps the rest. The result is a set of open runs, written
as lines and arcs whose stroke width IS the cutter diameter, so the drawn ribbon
is the material the bit removes.

**A run ends where the cutting EDGE reaches the stop line, not where the centre
crosses it.** The bit is a disc: a centre sitting on the line has already cut a
radius past it, into the tab. So a run ends where the centre is one cutter
radius from the stop line - `toolpath.stadium_intersections` - and the cut face
left behind is the bit's semicircular silhouette tangent to that line, which is
the tab design's route-end scallop.

**This stage writes into the source board named by the config.** That is a
deliberate exception to "the core never modifies the input design", and it
applies to this stage alone. Prep is a designer-invoked operation on
the real project: the toolpath belongs to the DESIGN, is reviewed at
single-board level, and then rides ordinary step-and-repeat into the panel like
any other artwork.

**The outline is assembled in memory, and the file is not restructured.** A tab
footprint carries the true break contour on `Edge.Cuts` as well as its staple,
and the board-level `Edge.Cuts` runs up to that contour and stops. Neither half
is the outline on its own. So this stage reads the board-level items AND every
footprint-hosted one, maps the footprint's children into board coordinates the
way KiCad's own parser does, and offsets the result - while the FILE keeps its
footprints exactly as the designer placed them. Both ways of drawing a board are
therefore valid: one footprint defining outline and tabs together, or a tab
footprint per tab stitched together with board-level lines.

**The cutter follows the IDEAL edge across a tab, not the break contour.** A tab
declares its mouth with two POINTS on `User.8`, one at each vertex where the
contour meets the ideal edge, and the outline is bridged STRAIGHT between them.
The contour items strung between those two points are leftovers belonging to
that tab and are dropped from the routing outline; anything left over that no
tab accounts for is an error, because it means the outline is not the loop it
looks like.

That is a correction, not a refinement. Offsetting the contour itself works at
the cutter the tab was drawn for and fails below it: measured on the
corpus board, a 0.3 mm bit found NO stop line at all - the offset of the contour
dips INSIDE the ideal edge, where stop lines standing outward from the edge
cannot reach it - and routed straight through all four tabs, while 0.5 and
0.6 mm deadlocked the offset walk. Declared beats derived: the two points say
where the ideal edge resumes, and no cutter size can move them.

Points and stop lines share `User.8` and are told apart by item KIND - a point
is a `(point ...)`, a stop line a segment - so nothing has to classify geometry.
A tab whose mouth is already continuous ideal edge needs no points and keeps
working exactly as before.

**Some edges are cut by a saw, not by the router.** A LINE that runs clear to
the board's extents along outline and through air is a V-SCORE candidate: a
blade travelling it separates the board without ever entering it. The unit is
the blade LINE - every collinear stretch of edge one pass would take is one
candidate, however many spans the outline breaks it into - because that is what
a designer counts and what a fab cuts. `toolpath.score_candidates` proposes them
from the geometry alone.

**The DESIGNER decides which candidates are scored, and `User.38` is where the
decision lives**. One shape plus one tab
arrangement yields one deterministic candidate set, so a decision is simply
which of those candidates to keep - and it is recorded by the presence of a
board-level `User.38` line on the candidate's blade line. Acceptance is that
line existing; rejection is its absence. **This stage never writes `User.38`.**
It VALIDATES: `board_scores` reads what the designer drew, `validate_scores`
matches each line to a candidate BY GEOMETRY - no ids, because operations
clobber uuids and they carry no semantics - and a line that lies on no current
candidate is reported as stale and suppresses nothing. Matching is
extent-tolerant: the line has only to lie ON the blade line, however short or
long the designer drew it. `score_proposals` reports every candidate with one
of three verdicts - scored, proposed but not accepted, or rejected because its
blade line passes through a tab - so "why was that edge not scored?" is
answerable from the run output and from the page.

A validated score SUPPRESSES ROUTING: every piece of the offset loop that came
from that edge drops, exactly as a tab bridge does.

The runs either side are then finished at the junction. A run ends where the
CUTTER TANGENT POINT - the last point of the board edge the cut takes to full
depth - is `prep.vscore.route_past_score_mm` PAST the score plane. The tangent point is what matters because it is where the cut
leaves the board edge: the disc's leading extreme sweeps out into air one radius
further on, and measuring by that extreme
stops the full-depth cut one radius SHORT of the plane, leaving uncut board edge
between the route and the score. That distance is the fab's - it follows from
blade diameter and score depth - so it is declared, never inferred, and a
junction without it is an error naming the junction.

**The tangent point is measured on its own curve, not on the centreline.** It
sits one radius off the centre along the normal into
the material, so the two are the same measurement only where the run drives
square at the plane. Where a run finishes on a corner FILLET that normal turns
towards the plane, and stopping the centre on the plane left the cut 0.3636 mm
short of it on the corpus board - 1.22 mm of board edge standing at each of the
two filleted corners.
`toolpath.contact_curve` gives the curve that point traces, and the trim, the
production and the recorded `past_mm` are all read off it.

**A junction is judged on STANDING MATERIAL, not on the distance quota.**
`route_past_score_mm` is asked for so that the
score exits into routed air, so what is judged is whether the board separates:
:func:`standing_edge_mm` measures the board edge left between where the cut
leaves the outline and the score plane, and :func:`junction_verdict` reads the
verdict off it. Nothing standing and the declared margin delivered is normal;
nothing standing because the board's own edge ENDS at the score - a corner
fillet tangent to the scored edge - is equally normal, and the report says which
of the two it is. Board edge standing is a warning, and so is a run that drives
square at the plane, where producing it is exactly what this stage does, and
still falls short. The distance quota is still recorded, as `reached`.

**Two SCORED spans meeting at a corner emit no toolpath at all.**
`route_past_score_mm` is about a score meeting a ROUTED edge; where both edges
separate under the blade no cutter is involved, so the corner arc between the
two dropped spans is dropped whole and reported as no junction. See
:func:`between_scored_spans`. A chamfer or a fillet between
the two scored edges is board edge, and stays routed.

**Prep owns `User.36` and nothing else.** Every board-level item on that layer is
deleted and regenerated on each run, so prep is idempotent: running it twice on
an unchanged board gives byte-identical output, because every item's uuid is
derived from the outline item it came from rather than minted. Items on that
layer INSIDE a footprint are not touched - they belong to the footprint library,
not to this stage. `User.38` is DESIGNER-OWNED and read-only here. Nothing else
in the file is written at all.

**Nothing degenerate is written down.** The offset arrangement leaves a splinter
a micrometre or two long wherever two curves touch almost tangentially, and an
arc that short cannot be stored: KiCad keeps an arc as three points on a
nanometre grid, so a bow of a nanometre reads back as any radius at all - 0.22 mm
or 314 mm or a straight line - and plots as a spurious circle in one render and
as a diagonal across the whole panel in another. So a piece shorter than
:data:`MIN_ITEM_MM` is dropped and its neighbours welded across the gap, and an
arc flatter than :data:`MIN_SAGITTA_MM` is written as the line between its ends.
Both changes are bounded by their own size - micrometres in total - and neither
renames a surviving item: a dropped splinter does not renumber its neighbours,
because the number is part of the uuid.

`score_line_for` is the ACCEPT SURFACE, not a writer: it says what accepting a
candidate would put on `User.38`, at the extent a score line takes - the blade
line produced to the outline's bounding box - so the eventual accept action and
the tests agree on one answer. Nothing in this stage calls it while prepping.

Both are used as bare canonical names everywhere. The layer table's display
names (for example `User.36-RouteToolPath` and `User.38-VScore`) are human
help and nothing matches on them.
"""

import math
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from eedesigncore import (
    Board,
    PcbArc,
    PcbLine,
    Stroke,
    item_layers,
    items_on_layers,
)

from eedesignpanels import cuttability, mintab
from eedesignpanels.config import PanelConfig, VScoreConfig
from eedesignpanels.outline_shapes import shape_primitives
from eedesignpanels.stages import sha256_of
from eedesignpanels.toolpath import (
    CHAINING_EPSILON_MM,
    COLLINEAR_TOL_MM,
    CONTACT_TOL_MM,
    JOIN_TOL_MM,
    Arc,
    Line,
    blade_extent,
    bounded_intersections,
    chain_bbox,
    chain_loops,
    closest_feature,
    contact_curve,
    distance_to_primitive,
    nearest_on_path,
    on_blade,
    parameter_on,
    resolve_offset,
    reverse,
    score_candidates,
    signed_area,
    stadium_intersections,
    sub_primitive,
    weld_chain,
    weld_gap,
)

OUTLINE_LAYER = "Edge.Cuts"
# Where a tab draws the two lines that stop the router.
STOP_LINE_LAYER = "User.8"
# ...and, as `(point ...)` items rather than segments, the two vertices where
# its break contour meets the ideal edge. Same layer, different item kind.
MOUTH_POINT_LAYER = STOP_LINE_LAYER
# Where the toolpath is written. Prep owns this layer at board level.
TOOLPATH_LAYER = "User.36"
# Where the v-scores are written. Prep owns this one the same way, and for the
# same reason: what it wrote last time is not an input to what it writes now.
SCORE_LAYER = "User.38"
# The widest cutter a tab can survive, declared by the tab footprint itself.
TAB_TOOL_MAX_FIELD = "_PANEL.ROUTE_TOOL_D_MAX"

# How wide a score line is drawn. Unlike the toolpath's stroke, this width means
# nothing physical - a v-groove's width is the blade's, and no board file says
# what blade a fab will use - so it is KiCad's own default graphic line width,
# thin enough to read as a centreline at any zoom and never mistakable for the
# routed ribbon beside it.
SCORE_LINE_WIDTH_MM = 0.1

# uuid5(NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/route-toolpath").
# Fixed for all time: a derived uuid has to be the same value on every run and
# on every machine, which is what makes a re-prep byte-identical.
TOOLPATH_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/route-toolpath"
)
# The same, for score lines. A separate namespace so that a span which is scored
# today and routed tomorrow cannot hand its identity to the other layer's item.
SCORE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/v-score"
)
# ...and one more for the PROPOSAL. A candidate needs a name of its own before
# anybody can accept or reject it by name, and that name has to survive the
# board being re-saved, so it is derived from the SORTED uuids of the outline
# items the candidate's spans are made of - the one thing about a candidate that
# does not depend on the order items happen to sit in the file. Third namespace
# for the third meaning: proposing a line, scoring it, and routing round it are
# three different identities for the same piece of edge.
CANDIDATE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/v-score-candidate"
)
# How much of a candidate id is printed and drawn. Full uuids in a report are
# unreadable and nothing here is looked up by a prefix.
SHORT_ID_CHARS = 8


class Tab(NamedTuple):
    """One STAPLE: a base on the board edge and the two stop lines on its feet.

    A tab is drawn on `User.8` as three segments in the shape of a staple - the
    base lies along the ideal board edge and the two legs stand outward from
    its ends, marking where the router has to stop. The tab's origin is the
    BASE MIDPOINT, not the footprint's, because a footprint may define any
    number of staples and the count that matters is the staple count.

    `tool_max_mm` comes from the owning footprint's `_PANEL.ROUTE_TOOL_D_MAX`,
    which constrains every staple that footprint defines.

    `mouth` is the pair of `User.8` POINTS that declare where this tab's break
    contour meets the ideal edge, ordered along the base direction, or `()` when
    the tab declares none because its mouth is already ideal edge.
    """

    label: str
    position: tuple
    base: object
    tool_max_mm: float
    stop_lines: list
    owner: str
    mouth: tuple = ()


class Bridge(NamedTuple):
    """One tab, measured the way a designer and a machinist each need it.

    `material_mm` is the tab itself - the narrowest width of board left holding
    the part in, and the number that decides whether it snaps or tears. It is
    what `centreline_mm` leaves behind once each cut face is taken into account:
    the toolpath is a centreline, and the bit removes a radius past each end of
    it, so the material is the centreline gap less one whole cutter diameter.

    `centreline_mm` is the STRAIGHT distance between the two run ends, because
    that is what the two cut faces are measured across. It is not the length of
    the path between them: where the mouth of a break contour is wider than the
    bit, the offset dips into it and travels further than the gap it spans -
    and none of that extra path is cut, because it lies inside the tab.

    That arithmetic has an independent check built into it. The two cut faces
    land exactly on the two stop lines, so the material left between them must
    equal the stop-line spacing - which is the staple's base length, measured
    from the board rather than derived. `agrees` is that identity.
    """

    tab: int
    label: str
    centreline_mm: float
    material_mm: float
    stop_spacing_mm: float

    @property
    def agrees(self) -> bool:
        return abs(self.material_mm - self.stop_spacing_mm) < 1e-6


class BridgeSpan(NamedTuple):
    """The stretch of the cutter path a tab keeps the router out of.

    `start` and `length` locate it along the loop, which is how a question like
    "is this unreachable recess inside a tab?" gets answered without geometry:
    the break contour of a tab lies in the span the router never enters.
    """

    tab: int
    span: list
    start: float
    length: float


class MouthPoint(NamedTuple):
    """One `(point ...)` on `User.8`, in BOARD coordinates.

    KiCad stores a footprint-hosted point's `(at …)` in board coordinates, not
    footprint-local ones: the formatter prints `PCB_POINT::GetPosition()`
    verbatim with no footprint frame applied
    (`pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.cpp:1147-1158`, 10.0.3), and
    the parser reads it back the same way
    (`…_parser.cpp:8542-8590`, `T_point` at `…_parser.cpp:5566`). An `fp_line`
    is stored local and de-rotated; a point beside it is not. So this position
    is used as it is read, and `place_on_board` must NOT be applied to it -
    verified on the corpus board, where a point of a footprint at
    (200.842984, 123.271289) reads (199.986784, 123.271289) for a library-local
    (-0.8562, 0).
    """

    position: tuple
    id: str
    owner: str


class Mouth(NamedTuple):
    """One tab's declared mouth, and the contour the bridge replaced.

    `leftovers` is how many `Edge.Cuts` items were strung between the two
    points - the break contour, which belongs to the tab rather than to the
    route. Zero is equally correct twice over: a designer may leave the outline
    dangling at the mouth and let the bridge close it, or may declare points on
    a mouth that is already straight ideal edge, where the chord is the outline
    and `bridged` is False because there is nothing to add.
    """

    tab: int
    label: str
    start: tuple
    end: tuple
    width_mm: float
    leftovers: int
    leftover_mm: float
    bridged: bool = True


class RoutThrough(NamedTuple):
    """One tab the finished cut runs through - the failure this guard exists for.

    `clearance_mm` is how close the cutter CENTRE gets to the base's interior.
    Anything at or under the cutter radius means the swept cut has reached the
    material the tab is made of.
    """

    tab: int
    label: str
    at: tuple
    clearance_mm: float
    radius_mm: float


class ScoreLine(NamedTuple):
    """One board-level `User.38` line: a DECISION, exactly as the designer drew it.

    Read, never written. `id` is carried for reporting alone - the decision is
    matched to a candidate by geometry, because an operation on the board may
    clobber a uuid and a uuid says nothing about where a cut is.
    """

    start: tuple
    end: tuple
    id: str = ""

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)


class StaleScore(NamedTuple):
    """A `User.38` line lying on no current candidate's blade line.

    The decision model's expected failure, and acceptable: change the outline and
    every previous decision stops matching, so the human re-decides exactly when
    the shape actually changed. It is reported by POSITION, since that is the
    only thing about it that still means anything, and it suppresses no routing -
    the router goes round that edge as if nothing were drawn there.
    """

    line: ScoreLine
    reason: str


class Score(NamedTuple):
    """One VALIDATED v-score: a blade line the saw cuts instead of the bit.

    `candidate` is the geometry the proposer found (:class:`toolpath.
    ScoreCandidate`); `lines` are the board-level `User.38` lines that accepted
    it. `items` is repeated here because the loop is matched against it
    constantly - a piece of the offset whose `origin` is one of these was offset
    from a scored edge, and the router must not travel it. `id` is the
    candidate's, not the score's: accepting a proposal does not give it a new
    name.
    """

    index: int
    label: str
    candidate: object
    items: frozenset
    dropped: int = 0
    dropped_mm: float = 0.0
    id: str = ""
    lines: tuple = ()

    @property
    def start(self) -> tuple:
        return self.candidate.start

    @property
    def end(self) -> tuple:
        return self.candidate.end

    @property
    def length_mm(self) -> float:
        return self.candidate.length_mm

    @property
    def spans(self) -> tuple:
        return self.candidate.spans


# The three things that can become of a candidate blade line. The middle one is
# the DESIGNER's word, not the tool's: the tool proposes and the designer
# declines, so nothing here calls a person's choice a rejection.
VERDICT_SCORED = "scored"
VERDICT_PROPOSED = "proposed, not accepted"
VERDICT_REJECTED = "rejected"


class Proposal(NamedTuple):
    """One candidate blade line with its verdict - the review's unit of record.

    Every candidate in ONE list, in one order, because the question the page and
    the report both answer is "what did the proposer find and what became of
    each one". A page that shows only the accepted ones cannot be checked
    against the board.

    Three verdicts, and the difference between the last two is the whole point
    of the decision model:

    * `scored` - a board-level `User.38` line lies on this blade line, so the
      designer accepted it and the router keeps off that edge;
    * `proposed, not accepted` - the geometry permits it and no line accepts it.
      The designer declined, and the tool has no complaint;
    * `rejected` - the tool will not propose it at all, because its blade line
      passes through a tab and a saw travelling it would cut away the material
      holding the board in.

    `proposable` is that last distinction on its own, so nothing has to read
    verdict strings to tell a tool refusal from a human choice.
    """

    id: str
    label: str
    candidate: object
    accepted: bool
    verdict: str
    reason: str
    summary: str = ""
    proposable: bool = True

    @property
    def short_id(self) -> str:
        return self.id[:SHORT_ID_CHARS]


# What a junction is judged to be. NORMAL is the board separating cleanly -
# nothing stands between the routed channel and the score - and WARN is board
# edge the designer can act on. Warn-never-block either way: a junction is a
# report about a valid toolpath, not an alarm (see `checks.toolpath_alarms`).
JUNCTION_NORMAL = "normal"
JUNCTION_WARN = "warn"


class ScoreJunction(NamedTuple):
    """Where a cutting run meets a score plane, and what was done about it.

    `at` is where the CENTRELINE stops - the last position of the bit. `contact`
    is where the cut leaves the BOARD EDGE from there: the tangent point, one
    radius off the centreline along the normal into the material. `past_mm` is
    how far past the plane THAT point is, and `reached` says whether it is the
    number the fab asked for. The disc's leading extreme reaches one radius
    further than the contact point on its own, into air; the tangent point is
    what says where full-depth cutting stops, so it is the number recorded and
    the number declared.

    `past_mm` is NOT read off `at`. The two are the same number only where the
    run drives square at the plane. A run curling round a corner fillet would
    report the centreline's distance and so overstate its reach by up to a
    radius.

    **`reached` is the QUOTA and `verdict` is the JUDGEMENT.** They are
    different questions: `reached` says whether the fab's `route_past` number
    was delivered, while `verdict` says whether the board separates - which is
    what `route_past` exists to achieve. `standing_mm` is the measurement the
    verdict is read off: board edge left standing between where the cut leaves
    the outline and the score plane. `reason` is the verdict in designer words.
    """

    score: int
    label: str
    at: tuple
    past_mm: float
    wanted_mm: float
    action: str
    change_mm: float
    reached: bool
    contact: tuple = None
    # THE SEVERING MEASUREMENT (:func:`standing_edge_mm`): how much board edge
    # the router left holding the part in, right where the saw arrives. Zero is
    # the answer that matters, and it is zero whenever the cut leaves the board
    # edge at or past the plane.
    standing_mm: float = 0.0
    verdict: str = JUNCTION_NORMAL
    reason: str = ""
    end_id: str = ""
    approach_angle_deg: float = 90.0

    @property
    def normal(self) -> bool:
        """Nothing stands here - the routed channel and the score meet."""
        return self.verdict == JUNCTION_NORMAL


class ScoredEdgeRouted(NamedTuple):
    """A scored edge the finished cut runs along - the failure this guard is for.

    Routing an edge that is also scored means machining away the material the
    saw was going to separate, so the board leaves the panel through neither.
    """

    score: int
    label: str
    at: tuple
    clearance_mm: float
    radius_mm: float


class Stop(NamedTuple):
    """One stop line's effect on the loop, measured."""

    tab: int
    stop: int
    label: str
    point: tuple
    position: float
    before: dict
    after: dict
    back_mm: float
    forward_mm: float
    angle_deg: float
    hosted_on: str
    touch_case: tuple


class BuildResult(NamedTuple):
    """What :func:`build` did to one board.

    The field order is load-bearing: it is indexed positionally in places, and
    anything new goes on the END.
    """

    tabs: list
    stops: list
    removed: int
    outline_items: int
    loop_items: int
    items: list
    run_lengths: list
    bridges: list
    tool_max_mm: float
    warnings: list
    mouths: list
    rout_through: list
    candidates: list = ()
    scores: list = ()
    junctions: list = ()
    routed_scores: list = ()
    route_past_mm: float = None
    proposals: list = ()
    # What the designer drew on `User.38`, read and never written: every
    # board-level line, and the ones that match no current candidate.
    score_lines: list = ()
    stale_scores: list = ()
    # The degenerate geometry that never reached the file - see
    # :class:`Emission`. Reported rather than assumed, because the whole
    # geometry change this stage makes to a run is these two numbers.
    dropped: int = 0
    dropped_mm: float = 0.0
    straightened: int = 0
    straightened_mm: float = 0.0
    # THE MINIMUM TAB, enforced between the ends of the kept runs: the slivers
    # consumed, and every declared tab the setting would have eaten and did not.
    min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM
    joins: tuple = ()
    protected_tabs: tuple = ()
    # ...and the RIBBONS, which are the other half of the same rule. A run-end
    # join is one cut between two ends; a covering pass is a chain of cuts along
    # a ribbon of thin material that two passes of the cutter left beside each
    # other. Both are written to `User.36`, because the panel's outline is
    # derived from that layer and nothing else.
    # `standing` is what a run with the setting switched OFF left behind.
    eliminate_slivers: bool = True
    covering: tuple = ()
    standing: tuple = ()
    # EVERY CLOSED LOOP AS A ROUTING JOB, cut with
    # the one declared cutter: the boundary, every hole and every separate
    # piece, each carrying whether the declared cutter can make it.
    # :mod:`eedesignpanels.cuttability`.
    jobs: tuple = ()
    # EVERY GAP THAT WAS CLOSED RATHER THAN MET,
    # and the setting that allowed it. Both, because a reader of the result has
    # to know whether to fix the board or to move the number.
    heals: tuple = ()
    heal_gap_mm: float = CHAINING_EPSILON_MM
    # WHETHER A CUTOUT WIDER THAN THE CUTTER WAS MILLED OUT. Carried on the
    # result because the readout, the page and the panel report all say which
    # cutouts were pocketed and which were left, and "left" is only readable next
    # to the setting that left them.
    pocket_cutouts: bool = False

    # The same tab- and score-limited runs serve the edge-plating action.
    boundary_chain: tuple = ()
    boundary_runs: tuple = ()
    toolpath_layer: str = TOOLPATH_LAYER
    rigid_results: tuple = ()

    @property
    def covering_length_mm(self) -> float:
        """How far the cutter travels making the covering passes."""
        return sum(join.crossing.length for join in self.covering)

    @property
    def uncuttable(self) -> tuple:
        """Every feature the declared cutter cannot make. Empty is the good case."""
        return tuple(job for job in self.jobs if not job.made)


class PrepResult(NamedTuple):
    """What one prep run did, in millimetres and seconds."""

    board_path: Path
    board_sha256: str
    cutter_diameter_mm: float
    tabs: list
    stops: list
    removed: int
    outline_items: int
    loop_items: int
    written: int
    run_lengths: list
    bridges: list
    tool_max_mm: float
    warnings: list
    load_seconds: float
    build_seconds: float
    write_seconds: float
    mouths: list = ()
    rout_through: list = ()
    candidates: list = ()
    scores: list = ()
    junctions: list = ()
    routed_scores: list = ()
    route_past_mm: float = None
    proposals: list = ()
    score_lines: list = ()
    stale_scores: list = ()
    # The no-write invariant, measured rather than asserted: the board-level
    # `User.38` items as read, compared against the same items after the build.
    # A prep run that changed one of them has broken the decision model.
    scores_untouched: bool = True
    # Degenerate geometry kept out of the file - see :class:`Emission`.
    dropped: int = 0
    dropped_mm: float = 0.0
    straightened: int = 0
    straightened_mm: float = 0.0
    # See :class:`BuildResult`.
    min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM
    joins: tuple = ()
    protected_tabs: tuple = ()
    eliminate_slivers: bool = True
    covering: tuple = ()
    standing: tuple = ()
    # See :class:`BuildResult`.
    jobs: tuple = ()
    heals: tuple = ()
    heal_gap_mm: float = CHAINING_EPSILON_MM
    # WHETHER A CUTOUT WIDER THAN THE CUTTER WAS MILLED OUT. Carried on the
    # result because the readout, the page and the panel report all say which
    # cutouts were pocketed and which were left, and "left" is only readable next
    # to the setting that left them.
    pocket_cutouts: bool = False
    toolpath_layer: str = TOOLPATH_LAYER
    rigid_results: tuple = ()

    @property
    def covering_length_mm(self) -> float:
        return sum(join.crossing.length for join in self.covering)

    @property
    def uncuttable(self) -> tuple:
        """Every feature the declared cutter cannot make. Empty is the good case."""
        return tuple(job for job in self.jobs if not job.made)

    @property
    def features_cuttable(self) -> bool:
        """The check: the declared cutter makes every feature on this board."""
        return not self.uncuttable

    @property
    def min_tab_clear(self) -> bool:
        """No declared tab is narrower than the minimum tab asked for."""
        return not self.protected_tabs

    @property
    def cut_length_mm(self) -> float:
        return sum(self.run_lengths)

    @property
    def material_mm(self) -> float:
        """Total board left holding the part in, across every tab."""
        return sum(bridge.material_mm for bridge in self.bridges)

    @property
    def narrowest_tab_mm(self) -> float:
        """The thinnest tab on the board - the one that gives way first."""
        return min(
            (bridge.material_mm for bridge in self.bridges), default=float("nan")
        )

    @property
    def widths_agree(self) -> bool:
        """Every tab's computed width equals its measured stop-line spacing."""
        return all(bridge.agrees for bridge in self.bridges)

    @property
    def guard_clear(self) -> bool:
        """No tab has the finished cut running through it."""
        return not self.rout_through

    @property
    def scores_clear(self) -> bool:
        """No scored edge has the finished cut running along it."""
        return not self.routed_scores

    @property
    def junctions_reached(self) -> bool:
        """Every run meeting a score plane got the declared overshoot.

        The QUOTA, not the verdict. A junction where the board's own edge ends
        at the score cannot be given the overshoot and does not need it, so this
        reads False on a perfectly good board - use :attr:`junctions_clear`.
        """
        return all(junction.reached for junction in self.junctions)

    @property
    def junctions_clear(self) -> bool:
        """No junction leaves board edge standing between the route and the saw.

        THE VERDICT. What `route_past_score_mm` exists for is that the
        score exits into routed air, so a junction is judged on standing
        material rather than on a distance quota.
        """
        return all(junction.normal for junction in self.junctions)


# ---------------------------------------------------------------------------
# reading the board
# ---------------------------------------------------------------------------


def _outline_primitives(item, source: str, place=None) -> list:
    """One `Edge.Cuts` drawing as the primitives it contributes, board frame.

    All six shapes KiCad accepts on `Edge.Cuts`, which is
    why this returns a LIST: a rectangle is four runs of the boundary and a
    circle is two. The catalogue itself lives in `eedesignpanels.outline_shapes`
    so that this stage and `stages/panelize.py` read the same one.

    An arc is carried by its three stored points, so a placed one is exact and
    keeps the direction it was drawn in however the footprint is turned - which
    is not true of anything that transforms a centre and a sweep.
    """
    def at(point: tuple) -> tuple:
        return point if place is None else place_on_board(point, *place)

    return shape_primitives(item, source=source, at=at)


def outline_primitives(board: Board) -> list:
    """The whole outline: board-level `Edge.Cuts` plus every footprint's.

    A tab footprint carries the break contour that spans its mouth, and the
    board-level outline stops at that mouth, so neither is a loop by itself.
    Assembling both here - and only here, in memory - is what makes the two
    board-drawing conventions equivalent to this stage. The board file is not
    touched: the footprints keep their items.

    Every shape KiCad accepts on `Edge.Cuts` is read - line, arc, rectangle,
    circle, polygon and curve. A closed shape contributes
    its whole boundary as several runs; anything that is not a drawing shape
    contributes nothing, exactly as KiCad's own collector ignores it.

    Raises:
        ValueError: when the outline holds a shape with no extent at all - a
            rectangle with no area, a circle with no radius - which KiCad
            rejects too.
    """
    primitives = []
    for item in items_on_layers(
        (*board.drawings, *board.trailing_drawings), {OUTLINE_LAYER}
    ):
        primitives.extend(_outline_primitives(item, item.id))
    for footprint in board.footprints:
        placement = (
            ((0.0, 0.0), 0.0) if footprint.at is None
            else (footprint.at.position, footprint.at.angle)
        )
        for index, item in enumerate(
            items_on_layers(footprint.drawings, {OUTLINE_LAYER})
        ):
            primitives.extend(_outline_primitives(
                item,
                item.id or f"{footprint.uuid}#{index}",
                place=placement,
            ))
    if not primitives:
        raise ValueError(
            f"the board has no {OUTLINE_LAYER} drawings, so there is no outline "
            f"to run a cutter around"
        )
    return primitives


def boundary_loop(primitives: list, warnings: list,
                  cutter_diameter_mm: float,
                  heal_gap_mm: float = CHAINING_EPSILON_MM) -> list:
    """The one closed loop the cutter follows, out of all of `Edge.Cuts`.

    `Edge.Cuts` holds a SET of closed loops and the board
    boundary is the largest of them. KiCad reads the layer the same way - every
    closed contour is built, and a contour enclosed by an even number of others
    is a top-level outline while an odd number makes it a hole
    (`pcbnew/convert_shape_list_to_polygon.cpp:402-437`, 10.0.3) - and
    `BuildBoardPolygonOutlines` passes `aAllowDisjoint=true` (`:1318`), so
    several disjoint shapes on `Edge.Cuts` are a board KiCad builds. This stage
    routes one boundary; every other loop is reported, named by where it is and
    how big it is, and left uncut. Reporting is the honest answer: a cutout or a
    second piece is real geometry, and refusing the whole board over it would
    tell the user the outline was broken at a vertex where it was perfect.

    Each reported loop is named a HOLE in the board or a SEPARATE PIECE, by
    `material.loop_is_hole` - KiCad's own parity rule, probed half a cutter
    width inside the loop, which is why the cutter diameter is an argument here.

    *warnings* is appended to, the way every other advisory in this stage is.

    Raises:
        OffsetError: when a run of items is not a closed loop at all.
    """
    return routing_jobs(
        primitives, cutter_diameter_mm, warnings, heal_gap_mm
    )[0]


def routing_jobs(primitives: list, cutter_diameter_mm: float,
                 warnings: list,
                 heal_gap_mm: float = CHAINING_EPSILON_MM,
                 pocket: bool = False) -> tuple:
    """`(boundary chain, jobs, heals)` - every closed loop as a cutting job.

    Internal route-outs are cut with the same declared external route cutter,
    so each loop is a toolpath job: each loop is judged against the ONE declared
    cutter, told which side of itself is material, and either routed or reported
    as a feature that cutter cannot make. :mod:`eedesignpanels.cuttability` makes
    every one of those decisions; this names them for the reader.

    The boundary is the largest loop, and it comes back as the raw chain rather
    than as its job: the stage routes it with the tabs, the scores and the
    minimum tab in hand, which is a path no general loop-router can produce.

    *warnings* is appended to, the way every other advisory in this stage is,
    and that includes every gap `heal_gap_mm` allowed to be closed: where it is
    and how far the outline fell short there.

    Raises:
        OffsetError: when a run of items is not a closed loop at all. The
            message names both distances - how far apart the ends are, and the
            largest gap this setting closes.
    """
    heals = []
    loops = chain_loops(primitives, heal_gap_mm, heals)
    # EVERY HEAL IS ANNOUNCED. It goes in the
    # warnings because that is the one channel the page, the panel report and
    # the session record all already read - a repair stated in a place nobody
    # reads is a silent repair.
    for heal in heals:
        warnings.append(heal.sentence)
    # The largest loop is the boundary; the others are reported in the order
    # their items appear in the file. NOT in area order: four identical cutouts
    # differ only in float noise there, which repeats but explains nothing.
    areas = [abs(signed_area(loop)) for loop in loops]
    widest = areas.index(max(areas))
    # POCKETING IS A CHECKBOX AND IT IS OFF BY DEFAULT. Off, a cutout wider
    # than the cutter is walked round and its
    # middle is left for the board house's own CAM, which is what most of them
    # do; on, it is milled out. Passed through rather than decided here: the
    # setting is the user's and this stage only carries it.
    jobs = cuttability.plan(loops, widest, cutter_diameter_mm, pocket=pocket)
    for job in jobs:
        _report_job(job, warnings)
    return loops[widest], jobs, tuple(heals)


def _report_job(job, warnings: list) -> None:
    """One routing job, said in plain words for the warnings list.

    Four sentences at most, and the uncuttable one is first and loudest: a
    feature the cutter cannot make is REPORTED rather
    than skipped silently, which is what the board did before this existed.
    """
    if not job.made:
        warnings.append(
            f"CANNOT BE CUT: {len(job.loop)} {OUTLINE_LAYER} item(s) form "
            f"{job.kind} ({job.label}), {job.where}: {job.reason}"
        )
        return
    if not job.is_boundary:
        warnings.append(
            f"{len(job.loop)} {OUTLINE_LAYER} item(s) form {job.kind}, "
            f"{job.where}, and the cutter routes it as a job of its own"
        )
    if job.note:
        warnings.append(
            f"{job.label} is routed, and the material test could not confirm "
            f"which side the cutter rides: {job.note}"
        )
    # WHICH CUTOUTS WERE POCKETED AND WHICH WERE LEFT, EITHER WAY. Silence on a
    # pocketed cutout would read as a cutout
    # too small to matter, which is the opposite of what it is.
    if job.pocketed:
        warnings.append(
            f"POCKETED: {job.label} is wider than the cutter, so "
            f"{len(job.pocket)} concentric pass(es) inside its wall take the "
            f"middle out and no loose piece is left"
        )
    if job.pocket_note:
        warnings.append(f"NOT POCKETED: {job.label} - {job.pocket_note}")
    if job.leaves_slug:
        warnings.append(
            f"LOOSE PIECE: {job.label} is wider than the cutter, so following "
            f"its wall leaves {job.slug_mm[0]:.4f} by {job.slug_mm[1]:.4f} mm "
            f"of material in the middle of it, attached to nothing - it drops "
            f"out of the panel as a slug"
        )
    # EVERY TIGHT CORNER IS NAMED, AND EACH IS RED. One line per corner rather
    # than one per feature:
    # the alarm state names a place and a radius, and a reader matching a red
    # mark on the picture to a line in the list needs the line to name that one
    # corner. What a fab sees is a rounded corner where a sharp one was drawn.
    for corner in job.rounded:
        warnings.append(
            f"CANNOT BE CUT SQUARE: {job.label} has an inside corner at "
            f"({corner.at[0]:.4f}, {corner.at[1]:.4f}) mm the cutter cannot "
            f"reach - it comes out rounded to the tool's "
            f"{corner.radius_mm:.4f} mm radius rather than sharp"
        )
    for run in job.unreachable:
        warnings.append(
            f"{job.label} has {run.span_mm:.4f} mm of edge at "
            f"({run.at[0]:.4f}, {run.at[1]:.4f}) mm the cutter never touches, "
            f"left as a {run.fillet_mm:.4f} mm fillet"
        )


def outline_bbox(board: Board) -> tuple:
    """`(min_x, min_y, max_x, max_y)` of the whole board outline, arcs exact.

    What a v-score line reaches, and what the review overlay
    aligns to. It is the DRAWN outline - board-level `Edge.Cuts` plus every
    footprint's - so it does not move when a cutter size changes.
    """
    return chain_bbox(outline_primitives(board))


def rotate_kicad(point: tuple, angle_degrees: float) -> tuple:
    """Rotate about the origin exactly as KiCad's `RotatePoint` does.

    Ported from `libs/kimath/src/trigo.cpp:229-264`; the four quadrant angles
    take an exact branch, which is why a tab placed at 180 degrees picks up no
    rounding. Note the sign: the file's Y axis points DOWN, so
    `x' = y sin + x cos`, `y' = y cos - x sin` is a turn the same way KiCad's
    own dialog calls positive.
    """
    angle = math.fmod(angle_degrees, 360.0)
    if angle < 0.0:
        angle += 360.0
    x, y = point
    if angle == 0.0:
        return (x, y)
    if angle == 90.0:
        return (y, -x)
    if angle == 180.0:
        return (-x, -y)
    if angle == 270.0:
        return (-y, x)
    radians = math.radians(angle)
    sin_a, cos_a = math.sin(radians), math.cos(radians)
    return (y * sin_a + x * cos_a, y * cos_a - x * sin_a)


# KiCad stores board coordinates as integer nanometres, and a footprint child is
# mapped into them one point at a time.
IU_PER_MM = 1_000_000.0


def _kiround(value: float) -> int:
    """Round half away from zero, as KiCad `KiROUND` does."""
    return math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)


def place_on_board(point: tuple, position: tuple, angle: float) -> tuple:
    """A footprint-local point in BOARD coordinates, as KiCad's parser gets it.

    A footprint child is stored footprint-local and de-rotated; the parser turns
    it about the origin by the footprint's orientation and then moves it by the
    footprint's position (`pcb_io_kicad_sexpr_parser.cpp:3609-3613`, and
    `EDA_SHAPE::rotate` in `common/eda_shape.cpp` turns an arc's start, mid and
    end alike). The rotation itself is `RotatePoint`, which rounds to the
    nanometre grid - so this works in nanometres too, and a placed contour lands
    on exactly the coordinates a board-level line drawn up to it would carry.
    """
    local = (_kiround(point[0] * IU_PER_MM), _kiround(point[1] * IU_PER_MM))
    turned = rotate_kicad(local, angle)
    return (
        (_kiround(turned[0]) + _kiround(position[0] * IU_PER_MM)) / IU_PER_MM,
        (_kiround(turned[1]) + _kiround(position[1] * IU_PER_MM)) / IU_PER_MM,
    )


# How close two staple ends must be to count as joined. The generator emits the
# base's endpoint and the stop line's foot as the SAME footprint-local point, so
# after one shared placement transform they are bit-identical and any tolerance
# would do. This one is three orders below the shortest staple segment and two
# above the file's 1 nm coordinate grid, so it survives a generator that rounds
# without ever joining two things a designer drew apart.
STAPLE_JOIN_TOL_MM = 1e-4


def _user8_segments(board: Board) -> list:
    """Every `User.8` line in the board frame, with the footprint that owns it.

    A footprint child is stored footprint-local and de-rotated, so the
    placement carries both the turn and the move.
    """
    segments = []
    for footprint in board.footprints:
        position = footprint.at.position
        angle = footprint.at.angle
        reference = next(
            (p.value for p in footprint.properties if p.name == "Reference"), ""
        )
        raw = next(
            (p.value for p in footprint.properties if p.name == TAB_TOOL_MAX_FIELD),
            None,
        )
        for item in footprint.drawings:
            if STOP_LINE_LAYER not in item_layers(item):
                continue
            if type(item).__name__ != "PcbLine":
                continue
            segments.append({
                "line": Line(
                    p0=place_on_board(item.start, position, angle),
                    p1=place_on_board(item.end, position, angle),
                    source=item.id,
                    role="staple",
                ),
                "owner": reference or footprint.uuid or "",
                "tool_max": float(raw) if raw not in (None, "") else None,
            })
    return segments


def tabs_of(board: Board, warnings: list = None) -> list:
    """Every STAPLE on `User.8`, paired by connectivity rather than by footprint.

    A staple is three segments: a base, and two legs standing on the base's
    ends. The base is found by what only a base can do - have BOTH of its ends
    met by another segment - so one footprint may define one staple or twenty
    and the tab count is the staple count either way.

    Segments that belong to no staple are reported and ignored; falling back to
    "every `User.8` line is a stop line" would quietly treat a base as a stop
    that never reaches the cutter path.
    """
    notes = warnings if warnings is not None else []
    segments = _user8_segments(board)

    def joins(a: tuple, b: tuple) -> bool:
        return math.dist(a, b) <= STAPLE_JOIN_TOL_MM

    tabs, used = [], set()
    for index, candidate in enumerate(segments):
        base = candidate["line"]
        legs = []
        for end in (base.p0, base.p1):
            for other, entry in enumerate(segments):
                if other == index:
                    continue
                line = entry["line"]
                if joins(line.p0, end):
                    legs.append((other, line))
                    break
                if joins(line.p1, end):
                    legs.append((other, line._replace(p0=line.p1, p1=line.p0)))
                    break
        if len(legs) != 2 or legs[0][0] == legs[1][0]:
            continue

        midpoint = (
            (base.p0[0] + base.p1[0]) / 2.0, (base.p0[1] + base.p1[1]) / 2.0
        )
        tabs.append(Tab(
            label=f"{candidate['owner']} @ ({midpoint[0]:.3f}, {midpoint[1]:.3f})",
            position=midpoint,
            base=base,
            tool_max_mm=candidate["tool_max"],
            # Stored foot first, so a leg always runs away from the board edge.
            stop_lines=[leg._replace(role="stop") for _, leg in legs],
            owner=candidate["owner"],
        ))
        used.add(index)
        used.update(other for other, _ in legs)

    orphans = len(segments) - len(used)
    if orphans:
        notes.append(
            f"{orphans} {STOP_LINE_LAYER} segment(s) belong to no staple - a tab "
            f"is a base on the board edge with a stop line standing on each end, "
            f"and these were ignored"
        )
    return sorted(tabs, key=lambda tab: (tab.position[1], tab.position[0]))


# ---------------------------------------------------------------------------
# the mouth points, and the straight bridge across them
# ---------------------------------------------------------------------------

# How far off a staple's base line a point may sit and still be that staple's
# mouth marker. The generator puts the point and the base on the same line in
# the same footprint, so after one shared placement they are exact; this is the
# same allowance the staple's own joints get.
MOUTH_ON_BASE_TOL_MM = STAPLE_JOIN_TOL_MM


def mouth_points(board: Board) -> list:
    """Every `User.8` POINT on the board, board-level and footprint-hosted.

    Footprint membership is not what pairs a point to a tab - position on a
    staple's base line is, the same position rule that connects the rest of
    the tab geometry. So
    a designer may declare a mouth from the footprint that draws it or from the
    board, and one footprint may declare many.

    Positions are read verbatim: see :class:`MouthPoint` for why a
    footprint-hosted point is already in board coordinates.
    """
    found = []
    for point in board.points:
        if point.layer == MOUTH_POINT_LAYER:
            found.append(MouthPoint(
                position=point.position, id=point.id or "", owner=""
            ))
    for footprint in board.footprints:
        reference = next(
            (p.value for p in footprint.properties if p.name == "Reference"), ""
        )
        for point in footprint.points:
            if point.layer != MOUTH_POINT_LAYER:
                continue
            found.append(MouthPoint(
                position=point.position,
                id=point.id or "",
                owner=reference or footprint.uuid or "",
            ))
    return found


def base_coordinates(point: tuple, base) -> tuple:
    """*point* in the base's own frame: (along from its midpoint, across it)."""
    length = math.dist(base.p0, base.p1)
    if length <= 0.0:
        return (0.0, float("inf"))
    unit = (
        (base.p1[0] - base.p0[0]) / length, (base.p1[1] - base.p0[1]) / length
    )
    middle = ((base.p0[0] + base.p1[0]) / 2.0, (base.p0[1] + base.p1[1]) / 2.0)
    offset = (point[0] - middle[0], point[1] - middle[1])
    return (
        offset[0] * unit[0] + offset[1] * unit[1],
        offset[0] * unit[1] - offset[1] * unit[0],
    )


def pair_mouths(tabs: list, points: list, warnings: list) -> list:
    """Give each tab the two mouth points that sit on its base line.

    A point belongs to the staple whose base supporting LINE it lies on, and
    where several staples share that line - a row of tabs along one straight
    edge - to the nearest of them. Then each staple takes the nearest claimed
    point on each side of its base. That is 1:many safe in both directions: one
    footprint may draw twenty staples, and twenty staples may sit on one line.

    Returns the tabs with their `mouth` filled in. A tab that gets no points
    keeps `()` and is bridged by nothing, which is the continuous-ideal-edge
    board that has always worked.
    """
    claims = {}
    for index, point in enumerate(points):
        best = None
        for tab_index, tab in enumerate(tabs):
            along, across = base_coordinates(point.position, tab.base)
            if abs(across) > MOUTH_ON_BASE_TOL_MM:
                continue
            side = -1 if along < 0.0 else 1
            key = (abs(along), tab_index, side)
            if best is None or key < best:
                best = key
        if best is None:
            warnings.append(
                f"the {MOUTH_POINT_LAYER} point at ({point.position[0]:.4f}, "
                f"{point.position[1]:.4f}) mm lies on no tab's base line, so no "
                f"tab claims it as a mouth vertex and it was ignored"
            )
            continue
        claims.setdefault((best[1], best[2]), []).append((best[0], index))

    paired = []
    for tab_index, tab in enumerate(tabs):
        sides = []
        for side in (-1, 1):
            candidates = sorted(
                claims.get((tab_index, side), ()),
                key=lambda entry: (entry[0], points[entry[1]].id),
            )
            if len(candidates) > 1:
                extra = len(candidates) - 1
                warnings.append(
                    f"tab {tab.label} has {extra} extra {MOUTH_POINT_LAYER} "
                    f"point(s) on one side of its base; the nearest was used"
                )
            sides.append(points[candidates[0][1]] if candidates else None)
        if sides[0] is not None and sides[1] is not None:
            paired.append(tab._replace(mouth=(sides[0], sides[1])))
            continue
        if sides[0] is not None or sides[1] is not None:
            warnings.append(
                f"tab {tab.label} declares only one of its two mouth points, so "
                f"there is nothing to bridge between and its mouth was left as "
                f"drawn"
            )
        paired.append(tab._replace(mouth=()))
    return paired


def mouth_is_continuous(primitives: list, tab: Tab) -> bool:
    """True when the outline already runs straight along this tab's base line.

    That is the board whose tab needs no mouth points: the ideal edge passes
    unbroken through the tab region, so the cutter's offset of it is the cutter
    path and there is nothing to bridge. Anything else - a break contour spanning
    the mouth, a notch, a gap - is not continuous, and a tab drawn that way
    without its two points cannot be routed correctly at every cutter size.
    """
    half = math.dist(tab.base.p0, tab.base.p1) / 2.0
    for primitive in primitives:
        if not isinstance(primitive, Line):
            continue
        along_0, across_0 = base_coordinates(primitive.p0, tab.base)
        along_1, across_1 = base_coordinates(primitive.p1, tab.base)
        if max(abs(across_0), abs(across_1)) > MOUTH_ON_BASE_TOL_MM:
            continue
        if min(along_0, along_1) <= -half and max(along_0, along_1) >= half:
            return True
    return False


def _string_from(primitives: list, first: int, start: tuple, goal: tuple,
                 tol: float) -> list:
    """Chain outward from *start* through primitive *first* until *goal*.

    Returns the indices travelled, or None when the chain dead-ends or runs out
    of items first. The outline is a degree-2 curve, so "the next item" is
    whichever unused one shares the running endpoint.
    """
    item = primitives[first]
    if math.dist(item.start, start) <= tol:
        point = item.end
    elif math.dist(item.end, start) <= tol:
        point = item.start
    else:
        return None

    used, order = {first}, [first]
    while math.dist(point, goal) > tol:
        following = None
        for index, candidate in enumerate(primitives):
            if index in used:
                continue
            if math.dist(candidate.start, point) <= tol:
                following = (index, candidate.end)
                break
            if math.dist(candidate.end, point) <= tol:
                following = (index, candidate.start)
                break
        if following is None:
            return None
        used.add(following[0])
        order.append(following[0])
        point = following[1]
    return order


def string_between(primitives: list, start: tuple, goal: tuple,
                   avoid: list = (), tol: float = JOIN_TOL_MM) -> list:
    """The run of outline items strung between two mouth points.

    Two runs join any pair of points on a closed outline - the contour across
    the mouth, and the whole rest of the board. The contour is the one that
    passes through no OTHER tab's mouth, and, among what is left, the SHORTER
    WAY ROUND: a break contour is a small feature on an outline, so a run that
    is more than half of it is the board, not the tab. That is also the answer
    when only one run exists at all, which is the outline that dangles at the
    mouth with no contour drawn - it returns `[]`, and the chord closes the gap.
    """
    half = sum(item.length for item in primitives) / 2.0
    best = None
    for first, item in enumerate(primitives):
        if min(math.dist(item.start, start), math.dist(item.end, start)) > tol:
            continue
        order = _string_from(primitives, first, start, goal, tol)
        if order is None:
            continue
        span = sum(primitives[index].length for index in order)
        if span >= half:
            continue
        blocked = any(
            math.dist(end, other) <= tol
            for index in order[:-1]
            for end in (primitives[index].start, primitives[index].end)
            for other in avoid
        )
        if blocked:
            continue
        key = (span, len(order), first)
        if best is None or key < best[0]:
            best = (key, order)
    return [] if best is None else best[1]


def bridge_mouths(primitives: list, tabs: list, warnings: list) -> tuple:
    """Replace each tab's break contour with a straight chord across its mouth.

    Every string is found on the FULL outline before anything is removed, so the
    result does not depend on the order the tabs come in.

    Raises:
        ValueError: when two tabs claim the same outline item, which means the
            mouth points do not describe the board that was drawn.
    """
    others = [
        point.position
        for tab in tabs for point in tab.mouth
    ]
    strings, mouths, claimed = {}, [], {}
    for tab_index, tab in enumerate(tabs):
        if not tab.mouth:
            continue
        start, end = tab.mouth[0].position, tab.mouth[1].position
        elsewhere = [
            position for position in others
            if math.dist(position, start) > JOIN_TOL_MM
            and math.dist(position, end) > JOIN_TOL_MM
        ]
        order = string_between(primitives, start, end, avoid=elsewhere)
        for index in order:
            if index in claimed:
                raise ValueError(
                    f"tab {tab.label} and tab {tabs[claimed[index]].label} both "
                    f"claim the {OUTLINE_LAYER} item "
                    f"{primitives[index].source!r} as their break contour, so "
                    f"the mouth points do not describe this outline"
                )
            claimed[index] = tab_index
        # Points on a mouth that is ALREADY straight ideal edge need no chord -
        # the outline is the chord. Adding one anyway would put two items on the
        # same run of edge and the outline would branch rather than close.
        already = not order and mouth_is_continuous(primitives, tab)
        strings[tab_index] = (order, not already)
        mouths.append(Mouth(
            tab=tab_index,
            label=tab.label,
            start=start,
            end=end,
            width_mm=math.dist(start, end),
            leftovers=len(order),
            leftover_mm=sum(primitives[index].length for index in order),
            bridged=not already,
        ))

    kept = [
        primitive for index, primitive in enumerate(primitives)
        if index not in claimed
    ]
    for tab_index, (order, bridged) in sorted(strings.items()):
        tab = tabs[tab_index]
        if not bridged:
            continue
        if not order:
            warnings.append(
                f"tab {tab.label} declares its mouth but the outline runs "
                f"nowhere between the two points, so the bridge closes a gap "
                f"rather than replacing a contour"
            )
        kept.append(Line(
            p0=tab.mouth[0].position,
            p1=tab.mouth[1].position,
            source=f"mouth:{tab.mouth[0].id}>{tab.mouth[1].id}",
            role="mouth",
        ))
    return kept, mouths


# ---------------------------------------------------------------------------
# the rout-through guard
# ---------------------------------------------------------------------------

# How much of each end of a staple's base the guard does not look at. The
# finished cut is EXPECTED to touch the base line where the two cut faces land -
# a cut face is a disc tangent to that line - and that contact dies away within
# a few micrometres of the mouth. A cut running THROUGH a tab is tangent along
# the base's whole length instead, so ignoring a fiftieth of a millimetre at
# each end separates the two cleanly. Never more than a quarter of the base, so
# the guard always looks at half of even a very short one.
TANGENCY_MARGIN_MM = 0.05
# Sample spacing along the base. The distance to the toolpath is 1-Lipschitz
# along the base, so no dip deeper than this can hide between two samples.
GUARD_STEP_MM = 0.005
# The cutter reaches its own radius and no further; anything at that distance is
# already touching. A nanometre of slack keeps exact arithmetic exact.
GUARD_TOL_MM = 1e-6


def rout_through(runs: list, tabs: list, radius: float) -> list:
    """Every tab the finished cut runs through. Empty is the answer that is wanted.

    The swept cut is the toolpath dilated by the cutter radius, so a point of a
    tab's base is cut when it lies within one radius of a cutting run. This asks
    that of the base's INTERIOR, because the two ends are where the cut faces are
    supposed to touch.

    It is a check on the RESULT, not on the way the result was reached, which is
    the point: it catches a tab the router drives through no matter which part of
    this stage let it happen.
    """
    pieces = [piece for run in runs for piece in run]
    if not pieces:
        return []

    found = []
    for tab_index, tab in enumerate(tabs):
        base = tab.base
        length = math.dist(base.p0, base.p1)
        margin = min(TANGENCY_MARGIN_MM, 0.25 * length)
        span = length - 2.0 * margin
        if span <= 0.0:
            continue
        steps = max(2, math.ceil(span / GUARD_STEP_MM))
        closest, at = float("inf"), base.p0
        for step in range(steps):
            along = (margin + span * (step + 0.5) / steps) / length
            point = (
                base.p0[0] + (base.p1[0] - base.p0[0]) * along,
                base.p0[1] + (base.p1[1] - base.p0[1]) * along,
            )
            _, distance = nearest_on_path(point, pieces)
            if distance < closest:
                closest, at = distance, point
        if closest <= radius + GUARD_TOL_MM:
            found.append(RoutThrough(
                tab=tab_index, label=tab.label, at=at,
                clearance_mm=closest, radius_mm=radius,
            ))
    return found


# ---------------------------------------------------------------------------
# v-scores: reading the designer's decisions, and keeping the router off them
# ---------------------------------------------------------------------------


def candidate_id(candidate) -> str:
    """The stable name of one candidate blade line.

    Derived from the SORTED uuids of the outline items its spans are made of, so
    it is the same string on every machine, on every run, and after a save that
    reorders the file - which is the whole point of having it. Nothing keys off
    it yet; it exists so that the decision model coming next can say "score this
    one" in a config and mean the same line tomorrow.
    """
    return str(uuid.uuid5(CANDIDATE_NAMESPACE, "|".join(sorted(candidate.sources))))


def score_label(candidate) -> str:
    """How one candidate is named in warnings, the report and the page."""
    middle = candidate.midpoint
    label = (
        f"v-score @ ({middle[0]:.3f}, {middle[1]:.3f}) "
        f"{candidate.length_mm:.3f} mm"
    )
    if len(candidate.spans) > 1:
        label += f" in {len(candidate.spans)} spans"
    return label


def blocking_tabs(candidate, tabs: list) -> list:
    """Every tab whose base sits on this candidate's blade LINE.

    The line, not the drawn span: the blade cuts the whole line across the
    panel, so a tab beyond the end of the outline stretch still stands in the
    saw's way. A candidate with any of these is one the tool will not propose -
    a mousebite and a v-groove on one line contradict each other, and a designer
    who put a tab on an edge has said that edge is routed.
    """
    return [
        tab for tab in tabs
        if on_blade(candidate, tab.position, CONTACT_TOL_MM)
    ]


def board_scores(board: Board, warnings: list) -> list:
    """Every board-level `User.38` line - the decisions, as the designer drew them.

    Read only. Footprint-hosted items on the same layer belong to the footprint
    library and are not decisions; nor is anything that is not a straight line,
    since a blade line is a line and an arc names none.
    """
    found = []
    for item in items_on_layers(
        (*board.drawings, *board.trailing_drawings), {SCORE_LAYER}
    ):
        kind = type(item).__name__
        if kind != "PcbLine":
            warnings.append(
                f"the board-level {SCORE_LAYER} {kind} {item.id or '(no uuid)'} "
                f"is not a straight line, so it names no blade line and accepts "
                f"no v-score candidate; it was ignored"
            )
            continue
        line = ScoreLine(start=item.start, end=item.end, id=item.id or "")
        if line.length_mm <= CONTACT_TOL_MM:
            warnings.append(
                f"the {SCORE_LAYER} line at ({line.start[0]:.4f}, "
                f"{line.start[1]:.4f}) mm has no length, so it names no blade "
                f"line and accepts no v-score candidate; it was ignored"
            )
            continue
        found.append(line)
    return found


def line_on_blade(candidate, line: ScoreLine) -> bool:
    """Does this drawn `User.38` line lie on the candidate's blade line?

    Both ends on the line, at any extent along it. EXTENT-TOLERANT on purpose:
    what a score line means is which cut it is, and a
    designer who drew it shorter than the edge - or longer, out into the frame -
    still drew that cut.
    """
    return (
        on_blade(candidate, line.start, COLLINEAR_TOL_MM)
        and on_blade(candidate, line.end, COLLINEAR_TOL_MM)
    )


def score_on(candidate, scores: list):
    """The score standing on this candidate's BLADE LINE, or None.

    ONE TRUTH FOR "IS THIS EDGE SCORED?" A candidate is scored
    when a `User.38` line lies on its own blade line, and by nothing else - the
    same test `preview.actions._accepts` runs when a click asks to accept or
    decline, so the page's word and the board's state can never disagree.

    It replaces a lookup by `candidate_id`, which is a hash of the OUTLINE ITEMS
    a candidate came from. A RECTANGLE OUTLINE IS WHAT EXPOSED THAT: one
    `PcbRect` yields four boundary runs, so all four candidates carried one id,
    and the first accepted line marked every edge of the board SCORED while
    three of them had no line at all. Never narrow this back to a test on the
    item a candidate came from - an outline item is not a cut.
    """
    for score in scores:
        if any(line_on_blade(candidate, line) for line in score.lines):
            return score
    return None


def validate_scores(candidates: list, lines: list, warnings: list) -> tuple:
    """Match the designer's `User.38` lines to candidates. Returns (scores, stale).

    THE DECISION MODEL, and the whole of it: acceptance is a line existing on a
    candidate's blade line, rejection is its absence, and matching is by
    GEOMETRY - never by uuid, because operations clobber uuids and a uuid says
    nothing about where a cut is.

    A line matching no candidate is stale: the outline changed under it, so the
    decision it recorded is about a board that no longer exists. It is named by
    position, it suppresses no routing, and the human re-decides. That is the
    right cost - a decision only stops matching when the shape it was
    made about actually moved.

    A line lying on TWO candidates' blade lines accepts both. That is one saw
    pass, and the two stretches of edge on it are both cut by it, whatever the
    proposer's reason for counting them separately.
    """
    matched, stale = {}, []
    for line in lines:
        hits = [
            index for index, candidate in enumerate(candidates)
            if line_on_blade(candidate, line)
        ]
        if not hits:
            reason = "this score no longer lies on a scoreable edge"
            stale.append(StaleScore(line=line, reason=reason))
            warnings.append(
                f"STALE SCORE: the {SCORE_LAYER} line from "
                f"({line.start[0]:.4f}, {line.start[1]:.4f}) to "
                f"({line.end[0]:.4f}, {line.end[1]:.4f}) mm lies on no v-score "
                f"candidate - {reason}; the outline changed? It scores nothing, "
                f"so the router still cuts that edge"
            )
            continue
        for index in hits:
            matched.setdefault(index, []).append(line)

    scores = []
    for index in sorted(matched):
        candidate = candidates[index]
        scores.append(Score(
            index=len(scores),
            label=score_label(candidate),
            candidate=candidate,
            items=frozenset(candidate.items),
            id=candidate_id(candidate),
            lines=tuple(matched[index]),
        ))
    return scores, stale


def score_proposals(candidates: list, scores: list, tabs: list) -> list:
    """Every candidate the proposer found, with what became of it.

    Ordered by where the line lies rather than by verdict, so the table reads
    the way the board does and two runs over the same board list the same
    candidates in the same places.
    """
    table = []
    for candidate in candidates:
        identity = candidate_id(candidate)
        blocked = blocking_tabs(candidate, tabs)
        plural = "tab" if len(blocked) == 1 else "tabs"
        if score_on(candidate, scores) is not None:
            verdict, is_accepted = VERDICT_SCORED, True
            summary = "cut by the saw, not the router"
            reason = (
                f"{candidate.length_mm:.3f} mm of edge on this line is cut by "
                f"the saw, not the router - accepted by a {SCORE_LAYER} line on "
                f"the board"
            )
            if blocked:
                summary = f"scored, though the blade line passes through a {plural}"
                reason += (
                    f". Its blade line also passes through {len(blocked)} "
                    f"{plural}, starting with {blocked[0].label}"
                )
        elif blocked:
            verdict, is_accepted = VERDICT_REJECTED, False
            summary = f"the blade line passes through {len(blocked)} {plural}"
            reason = (
                f"the blade line passes through {len(blocked)} {plural}, "
                f"starting with {blocked[0].label} - an edge with a tab on it is "
                f"routed, not scored"
            )
        else:
            verdict, is_accepted = VERDICT_PROPOSED, False
            summary = "proposed; no score line on the board accepts it"
            reason = (
                f"{candidate.length_mm:.3f} mm of edge on this line could be cut "
                f"by the saw; no {SCORE_LAYER} line on the board accepts it, so "
                f"the router cuts that edge instead"
            )
        table.append(Proposal(
            id=identity,
            label=score_label(candidate),
            candidate=candidate,
            accepted=is_accepted,
            verdict=verdict,
            reason=reason,
            summary=summary,
            proposable=not blocked,
        ))
    return sorted(
        table,
        key=lambda proposal: (
            round(proposal.candidate.start[0], 6),
            round(proposal.candidate.start[1], 6),
            round(proposal.candidate.end[0], 6),
            round(proposal.candidate.end[1], 6),
        ),
    )


def plane_parameters(primitive, candidate, level: float) -> list:
    """Fractions along *primitive* at which its distance from the plane is *level*.

    Sorted, and only the ones inside the piece. A line's plane distance is
    linear in its own parameter; an arc's is `centre + radius cos(theta -
    facing)`, so it meets a level at up to two angles and each of them is a
    parameter along the arc. Used both to trim a run at the junction and to
    measure the board edge standing past one.
    """
    if isinstance(primitive, Line):
        first = candidate.plane_distance(primitive.p0)
        second = candidate.plane_distance(primitive.p1)
        if abs(second - first) < 1e-12:
            return []
        along = (level - first) / (second - first)
        return [along] if 0.0 <= along <= 1.0 else []

    if primitive.radius <= CONTACT_TOL_MM:
        return []
    middle = candidate.plane_distance(primitive.center)
    reach = (level - middle) / primitive.radius
    if abs(reach) > 1.0:
        return []
    facing = math.atan2(candidate.normal[1], candidate.normal[0])
    offset = math.acos(max(-1.0, min(1.0, reach)))
    found = []
    for angle in (facing + offset, facing - offset):
        if primitive.contains_angle(angle):
            found.append(parameter_on(primitive, primitive.point_at_angle(angle)))
    return sorted(set(found))


def _plane_crossings(contact, score: Score, wanted: float) -> list:
    """Where along a piece its CONTACT POINT is *wanted* off the score plane.

    *contact* is the piece's :func:`toolpath.contact_curve` - the curve the
    cutter's point of contact with the board edge traces while the centre walks
    the piece - and it carries the piece's own parameterisation, so what comes
    back is a fraction along the PIECE.

    Measuring the centreline instead is the same answer only where the run
    drives square at the plane: a run finishing on a corner fillet with its
    centreline stopped on the plane leaves its contact point - and so the
    full-depth cut - up to one radius
    short of it, with a strip of board edge standing between route and score.
    """
    candidate = score.candidate
    if isinstance(contact, Arc) and contact.radius <= CONTACT_TOL_MM:
        # A corner arc: the contact point is the vertex and never moves, so
        # pivoting there takes no further board edge to depth. The arc is
        # redundant once that vertex is as far past the plane as was asked -
        # trim at its start - and no help at all when it is not.
        middle = candidate.plane_distance(contact.center)
        return [0.0] if middle <= wanted + CONTACT_TOL_MM else []
    return plane_parameters(contact, candidate, wanted)


def _walk_to_plane(chain: list, index: int, along: float, candidate,
                   forward: bool, tol: float) -> float:
    """Outline length from `(index, along)` round the loop until it meets the plane.

    One direction of travel. The whole loop is the ceiling, which is the honest
    answer when this way round never reaches the plane at all.
    """
    if forward:
        pieces = [sub_primitive(chain[index], along, 1.0)] + [
            chain[(index + step) % len(chain)] for step in range(1, len(chain))
        ]
    else:
        pieces = [reverse(sub_primitive(chain[index], 0.0, along))] + [
            reverse(chain[(index - step) % len(chain)])
            for step in range(1, len(chain))
        ]
    walked = 0.0
    for piece in pieces:
        hits = plane_parameters(piece, candidate, tol)
        if hits:
            return walked + piece.length * min(hits)
        walked += piece.length
    return walked


def standing_edge_mm(chain: list, candidate, contact: tuple,
                     tol: float = CONTACT_TOL_MM) -> float:
    """Board edge left standing between the cut's last contact and the score plane.

    THE SEVERING MEASUREMENT, and the junction's verdict is read off it.
    `route_past_score_mm` exists so that the score exits into routed
    air; what can go wrong is a strip of board edge left holding the part in
    right where the saw arrives, and this is that strip, measured along the
    outline in millimetres.

    Zero whenever the cut leaves the board edge at or past the plane: everything
    the contact point travelled over is severed, so there is nothing between it
    and the score. Otherwise the outline is walked from the contact point both
    ways round the loop and the SHORTER reach to the plane is the answer - the
    material on the other side is behind the cut and already gone.

    It is measured rather than inferred from `past_mm` because the two are
    different quantities: the perpendicular shortfall is a lower bound on the
    edge, and round a fillet the edge is three times it (0.3636 mm short left
    1.2245 mm standing on the corpus board).
    """
    if not chain or candidate.plane_distance(contact) <= tol:
        return 0.0
    index = min(
        range(len(chain)),
        key=lambda where: distance_to_primitive(contact, chain[where]),
    )
    along = min(1.0, max(0.0, parameter_on(chain[index], contact)))
    return min(
        _walk_to_plane(chain, index, along, candidate, forward, tol)
        for forward in (True, False)
    )


def junction_verdict(past_mm: float, wanted_mm: float, standing_mm: float,
                     producible: bool, tol: float = CONTACT_TOL_MM) -> tuple:
    """Judge one junction, and say why in the designer's words. `(verdict, reason)`.

    **A junction's verdict judges STANDING MATERIAL, not a distance quota.**
    The point of
    `route_past_score_mm` is that the score exits into routed air with margin,
    so the question is whether the board separates there - and a corner where
    the board's own edge ENDS at the score separates perfectly while achieving
    no overshoot at all.

    NORMAL needs nothing standing, and then one of two things: the margin the
    fab asked for was delivered, or there was no board edge past the plane to
    take to depth and the cut reached the plane. The reason says which.

    WARN is board edge standing or a run
    that DRIVES square at the plane, where producing it further is exactly what
    this stage does, and still fell short of the number. That last one the
    designer can act on; a fillet the designer cannot.

    *producible* is whether the run arrives on a straight edge driving at the
    plane, which is the geometry the margin is achievable on.
    """
    if standing_mm > tol:
        return JUNCTION_WARN, (
            f"{standing_mm:.4f} mm of board edge stands between the routed "
            f"channel and the score, so the saw arrives into material"
        )
    if past_mm >= wanted_mm - tol:
        # Anything under the tolerance is nothing, and reads as "-0.0000 mm" if
        # it is printed as it stands - a trim that landed exactly on the plane.
        landed = past_mm if abs(past_mm) >= tol else 0.0
        return JUNCTION_NORMAL, (
            f"the cut leaves the board edge {landed:.4f} mm past the score "
            f"plane, as declared"
        )
    if producible:
        return JUNCTION_WARN, (
            f"the cut drives straight at the score plane and stops "
            f"{wanted_mm - past_mm:.4f} mm short of the {wanted_mm:.4f} mm "
            f"asked for, which this edge had room to give"
        )
    return JUNCTION_NORMAL, (
        f"the board edge ends at the score, so the cut reached the plane and "
        f"there is nothing further to take to depth; the {wanted_mm:.4f} mm "
        f"overshoot does not apply at this corner"
    )


def _end_run_at_score(pieces: list, score: Score, route_past: float,
                      warnings: list, chain: list, protect: int = 0) -> tuple:
    """Trim or extend the END of one run so the cut stops past the score plane.

    **The TANGENT POINT is what has to reach the plane**. The cut takes the board edge to full depth as far as the point
    where the disc touches that edge, and everything past that is the round end
    sweeping out into air, one radius further, which cuts nothing that was still
    holding the board. So `wanted` is a plain `-route_past` along the plane
    normal, and it is asked of the CONTACT POINT.

    **Of the contact point, not of the centreline.** The two are the same
    measurement only where the run drives square
    at the plane: the contact point sits one radius off the centreline along the
    normal into the material, so a run finishing on a corner fillet - where that
    normal turns towards the plane - with its centre stopped on the plane has
    its contact point up to a radius short of it. Measured on the corpus board:
    0.3636 mm short at a 0.889 mm cutter, leaving 1.22 mm of board edge standing
    between route and score, at both filleted corners.
    :func:`toolpath.contact_curve` is where that curve comes from.

    Three outcomes, in the order they are tried:

    * the run already crosses that depth - cut it there, dropping whatever came
      after, which is how a corner arc that curls round the score plane goes;
    * it does not, but its last real curve is a LINE driving at the plane -
      produce that line, dropping the corner arcs the score made meaningless;
    * it is an arc travelling ALONGSIDE the plane. Producing it never crosses
      the plane at all, so nothing is changed. That is not a failure by itself:
      whichever outcome this reaches, the junction is then JUDGED on the board
      edge it left standing (:func:`junction_verdict`), and a corner
      whose own edge ends at the score is normal.

    *protect* is how many pieces at the FRONT belong to the other end of this
    same stretch and are therefore not this end's to touch. A stretch bounded by
    one score at both ends - the whole loop of a board with no tabs - otherwise
    finishes its start, and then finds that finished piece when it searches
    backwards for its own crossing, and trims the entire run away to reach it.
    That collapse is fixed here: one end may
    not undo the other.
    """
    wanted = -route_past

    def touch(piece, at_start: bool = False) -> tuple:
        """Where the cut leaves the board edge, for a piece finishing the run."""
        curve = contact_curve(piece, chain)
        return curve.start if at_start else curve.end

    def drives_at_plane(run: list) -> bool:
        """Does this run arrive on a straight edge driving AT the plane?

        The geometry the declared overshoot is achievable on, and the one this
        stage can produce. An arc arriving tangentially cannot be given any
        overshoot at all, which is the whole difference the verdict turns on.
        """
        index = len(run) - 1
        while index >= 0 and run[index].role in ("join", "joinback"):
            index -= 1
        if index < 0 or not isinstance(run[index], Line):
            return False
        heading = run[index].tangent_at_end()
        return abs(
            heading[0] * score.candidate.normal[0]
            + heading[1] * score.candidate.normal[1]
        ) > 1e-9

    producible = drives_at_plane(pieces)

    def judged(junction: ScoreJunction) -> ScoreJunction:
        """Measure what stands at this junction, judge it, and say so out loud."""
        standing = standing_edge_mm(chain, score.candidate, junction.contact)
        verdict, reason = junction_verdict(
            junction.past_mm, junction.wanted_mm, standing, producible
        )
        if verdict == JUNCTION_WARN:
            warnings.append(
                f"the cutting run meeting {score.label} at "
                f"({junction.at[0]:.4f}, {junction.at[1]:.4f}) mm: {reason}"
            )
        return junction._replace(
            standing_mm=standing, verdict=verdict, reason=reason
        )

    for index in reversed(range(protect, len(pieces))):
        crossings = _plane_crossings(
            contact_curve(pieces[index], chain), score, wanted
        )
        if not crossings:
            continue
        along = max(crossings)
        kept = pieces[:index]
        if along * pieces[index].length > CONTACT_TOL_MM:
            kept = kept + [sub_primitive(pieces[index], 0.0, along)]
        if kept:
            end, contact = kept[-1].end, touch(kept[-1])
        else:
            end, contact = pieces[0].start, touch(pieces[0], at_start=True)
        past = -score.candidate.plane_distance(contact)
        return kept, judged(ScoreJunction(
            score=score.index, label=score.label, at=end,
            past_mm=past,
            wanted_mm=route_past, action="trimmed",
            change_mm=sum(piece.length for piece in pieces)
            - sum(piece.length for piece in kept),
            reached=past >= route_past - CONTACT_TOL_MM, contact=contact,
        ))

    last = len(pieces) - 1
    while last >= protect and pieces[last].role in ("join", "joinback"):
        last -= 1
    if last >= protect and isinstance(pieces[last], Line):
        piece = pieces[last]
        direction = piece.tangent_at_end()
        approach = (
            direction[0] * score.candidate.normal[0]
            + direction[1] * score.candidate.normal[1]
        )
        if abs(approach) > 1e-9:
            # The contact point of a straight run travels with the centre, so
            # the distance to produce is read off the contact point and applied
            # to the centreline.
            travel = (
                wanted - score.candidate.plane_distance(touch(piece))
            ) / approach
            if travel > 0.0:
                grown = piece._replace(p1=(
                    piece.p1[0] + direction[0] * travel,
                    piece.p1[1] + direction[1] * travel,
                ))
                kept = pieces[:last] + [grown]
                contact = touch(grown)
                past = -score.candidate.plane_distance(contact)
                return kept, judged(ScoreJunction(
                    score=score.index, label=score.label, at=grown.p1,
                    past_mm=past,
                    wanted_mm=route_past, action="extended",
                    change_mm=travel,
                    reached=past >= route_past - CONTACT_TOL_MM,
                    contact=contact,
                ))

    end = pieces[-1].end if pieces else score.start
    contact = touch(pieces[-1]) if pieces else end
    past = -score.candidate.plane_distance(contact)
    return pieces, judged(ScoreJunction(
        score=score.index, label=score.label, at=end, past_mm=past,
        wanted_mm=route_past, action="unchanged", change_mm=0.0,
        reached=past >= route_past - CONTACT_TOL_MM,
        contact=contact,
    ))


def _flip(pieces: list) -> list:
    """The same run travelled the other way - so one END rule serves both ends."""
    return [reverse(piece) for piece in reversed(pieces)]


def split_run_at_scores(run: list, by_item: dict) -> list:
    """One run cut into the stretches between its scored pieces.

    Returns `[(pieces, score_before, score_after), ...]` in travel order, where
    each `score` is the :class:`Score` whose span bounds that stretch or None at
    a free end. A CLOSED run - the whole loop, on a board with no tabs - is
    rotated first so it begins after a scored piece, which turns it into one
    open stretch with a score at each end.
    """
    scored = [piece.origin in by_item for piece in run]
    if not any(scored):
        return [(run, None, None)]

    order = list(range(len(run)))
    closed = len(run) > 1 and math.dist(run[0].start, run[-1].end) <= JOIN_TOL_MM
    if closed:
        first = next(index for index in order if scored[index])
        order = order[first:] + order[:first]

    stretches, current, before = [], [], None
    for index in order:
        if scored[index]:
            if current:
                stretches.append((current, before, by_item[run[index].origin]))
                current = []
            before = by_item[run[index].origin]
            continue
        current.append(run[index])
    if current:
        # A closed run has no free end: whatever trails the last scored piece
        # runs on into the first one.
        stretches.append((
            current, before,
            by_item[run[order[0]].origin] if closed else None,
        ))
    return stretches


def between_scored_spans(pieces: list, before, after) -> bool:
    """Is this stretch nothing but the corner where two scored spans meet?

    `route_past_score_mm` applies where a v-score
    meets a ROUTED edge, and nowhere else. Where two scored spans meet, both
    edges separate under the saw and no cutter is involved, so the corner emits
    NO TOOLPATH at all - at any `route_past`.

    A corner like that is a stretch with a score at each end and not one piece
    that came from an outline item: pure corner geometry, the arc the offset
    walks round the vertex between two dropped spans. It was emitted as a stub
    of a cut a couple of tenths long, and only vanished at `route_past` 0
    because both ends then happened to trim it to nothing. A chamfer or a fillet
    between the two scored edges is a different thing - that IS board edge, no
    saw takes it, and it stays routed with a junction at each end.
    """
    return (
        before is not None
        and after is not None
        and all(piece.origin < 0 for piece in pieces)
    )


def apply_scores(runs: list, scores: list, route_past: float,
                 warnings: list, chain: list, along_score: float = 0.0,
                 tangent_angle: float = 0.0, overrides=None, warn_unmatched=True,
                 operation="") -> tuple:
    """Take the scored edges out of the toolpath and finish the runs that met them.

    A scored edge is a route SUPPRESSOR: the saw separates the board there, so
    every piece of the offset loop that came from it drops, exactly the way a
    tab bridge does. What is left is finished at the junction, so the routed
    channel crosses the score plane by the declared amount and the score exits
    into air rather than into standing material.

    *chain* is the outline the offset was taken from, and it is here because the
    board edge is what the junction rule measures: a run is finished where its
    CONTACT POINT with that outline reaches past the plane, and
    :func:`toolpath.contact_curve` needs the item each piece came from to say
    where that point is.

    Two things a junction is NOT. It is not measured off the bit's width - the
    tangent point travels with the centreline - and it is not a
    corner where two SCORED spans meet: see :func:`between_scored_spans`.
    """
    if not scores:
        if warn_unmatched:
            for key in sorted(overrides or {}):
                warnings.append(f"Route end override {key} no longer matches this outline. Reassign or remove it.")
        return runs, []

    import hashlib
    import json
    overrides = overrides or {}
    used = set()

    def finish(pieces, score, protect=0):
        if not pieces:
            return _end_run_at_score(pieces, score, route_past, warnings, chain, protect)
        last = pieces[-1]
        actual = next((piece for piece in reversed(pieces)
                       if piece.role not in ("join", "joinback")), last)
        direction = actual.tangent_at_end()
        normal = score.candidate.normal
        angle = math.degrees(math.asin(min(1.0, abs(direction[0] * normal[0] + direction[1] * normal[1]))))
        # Geometry and source ownership identify an end independently of run order.
        identity = [operation, last.source, list(last.end), list(direction),
                    list(score.start), list(score.end)]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        used.add(key)
        near_tangent = angle < tangent_angle or angle < 1e-9
        along_enabled = near_tangent and (along_score > 0 or key in overrides)
        distance = overrides.get(key, along_score if along_enabled else route_past)
        if along_enabled and distance > 0:
            direction = last.tangent_at_end()
            end = (last.end[0] + direction[0] * distance,
                   last.end[1] + direction[1] * distance)
            extension = Line(last.end, end, source=last.source, role="score_extension", origin=last.origin)
            contact = contact_curve(last, chain).end
            junction = ScoreJunction(score.index, score.label, end, distance, distance,
                                     "extended along score", distance, True, contact=contact)
            return pieces + [extension], junction._replace(end_id=key, approach_angle_deg=angle)
        finished, junction = _end_run_at_score(pieces, score, distance, warnings, chain, protect)
        return finished, junction._replace(end_id=key, approach_angle_deg=angle)

    by_item = {item: score for score in scores for item in score.items}
    kept, junctions = [], []
    for run in runs:
        for pieces, before, after in split_run_at_scores(run, by_item):
            if between_scored_spans(pieces, before, after):
                continue
            protect = 0
            if before is not None:
                pieces, junction = finish(_flip(pieces), before)
                pieces = _flip(pieces)
                junctions.append(junction)
                # Whatever finishing the START produced is now the first piece,
                # and it is not the other end's to trim back to.
                protect = 1 if pieces else 0
            if after is not None:
                pieces, junction = finish(pieces, after, protect=protect)
                junctions.append(junction)
            if pieces:
                kept.append(pieces)
    for key in sorted(set(overrides) - used) if warn_unmatched else ():
        warnings.append(f"Route end override {key} no longer matches this outline. Reassign or remove it.")
    return kept, sorted(junctions, key=lambda entry: (entry.score, entry.at))


def measure_scores(scores: list, loop: list) -> list:
    """Each score with the offset it took out of the toolpath counted."""
    measured = []
    for score in scores:
        pieces = [piece for piece in loop if piece.origin in score.items]
        measured.append(score._replace(
            dropped=len(pieces),
            dropped_mm=sum(piece.length for piece in pieces),
        ))
    return measured


def routed_scores(runs: list, scores: list, radius: float) -> list:
    """Every scored edge the finished cut runs along. Empty is the answer wanted.

    The same shape as :func:`rout_through`, and for the same reason: it asks of
    the RESULT, so it catches a scored edge the router travels no matter which
    part of this stage let it happen. The ends of a score are skipped, because a
    run that finishes past the plane is SUPPOSED to reach the score there - the
    channel is tangent to the edge the score lies on, and that tangency dies away
    within a fraction of a millimetre of the corner.
    """
    pieces = [piece for run in runs for piece in run]
    if not pieces:
        return []

    found = []
    for score in scores:
        closest, at = float("inf"), score.start
        # Walked SPAN by span: the gaps between a candidate's spans are not
        # scored edge, and the router is entitled to be in them.
        for span in score.candidate.spans:
            length = span.length_mm
            margin = min(TANGENCY_MARGIN_MM, 0.25 * length)
            walked = length - 2.0 * margin
            if walked <= 0.0:
                continue
            steps = max(2, math.ceil(walked / GUARD_STEP_MM))
            for step in range(steps):
                along = (margin + walked * (step + 0.5) / steps) / length
                point = (
                    span.start[0] + (span.end[0] - span.start[0]) * along,
                    span.start[1] + (span.end[1] - span.start[1]) * along,
                )
                nearest, distance = nearest_on_path(point, pieces)
                # A requested tangent extension can touch the score without
                # removing material across it. Keep warnings for penetration.
                if pieces[nearest].role == "score_extension" and distance >= radius - GUARD_TOL_MM:
                    continue
                if distance < closest:
                    closest, at = distance, point
        if closest <= radius + GUARD_TOL_MM:
            found.append(ScoredEdgeRouted(
                score=score.index, label=score.label, at=at,
                clearance_mm=closest, radius_mm=radius,
            ))
    return found


def first_score_junction(runs: list, scores: list) -> tuple:
    """The first place a cutting run meets a score plane, or `(None, None)`.

    Asked before anything is written, because a junction with no declared
    `route_past_score_mm` is an error rather than a default: how far the router
    must overshoot the blade is the fab's number and this stage cannot invent
    it. A score with no run either side of it - one whose whole span falls
    inside a tab bridge, say - is no junction, and that board needs no
    declaration. Neither is a corner where two scored spans meet
    (:func:`between_scored_spans`): nothing is routed there, so there is nothing
    for the fab to declare about it.
    """
    by_item = {item: score for score in scores for item in score.items}
    for run in runs:
        for pieces, before, after in split_run_at_scores(run, by_item):
            if between_scored_spans(pieces, before, after):
                continue
            if before is not None:
                return before, (pieces[0].start if pieces else before.start)
            if after is not None:
                return after, (pieces[-1].end if pieces else after.end)
    return None, None


# ---------------------------------------------------------------------------
# cutting the loop
# ---------------------------------------------------------------------------


def cumulative_lengths(loop: list) -> tuple:
    """Distance along the loop at which each primitive starts, and the total."""
    starts, running = [], 0.0
    for primitive in loop:
        starts.append(running)
        running += primitive.length
    return starts, running


def closest_parameter(primitive, point: tuple) -> float:
    """Where along *primitive* the nearest point to *point* sits, from 0 to 1."""
    if isinstance(primitive, Line):
        direction = (
            primitive.p1[0] - primitive.p0[0], primitive.p1[1] - primitive.p0[1]
        )
        length_squared = direction[0] ** 2 + direction[1] ** 2
        if length_squared <= 0.0:
            return 0.0
        along = (
            (point[0] - primitive.p0[0]) * direction[0]
            + (point[1] - primitive.p0[1]) * direction[1]
        ) / length_squared
        return max(0.0, min(1.0, along))
    angle = math.atan2(
        point[1] - primitive.center[1], point[0] - primitive.center[0]
    )
    return max(0.0, min(1.0, parameter_on(primitive, primitive.point_at_angle(angle))))


def position_of(loop: list, starts: list, point: tuple) -> float:
    """How far round the loop the point nearest *point* lies."""
    index, _ = nearest_on_path(point, loop)
    return starts[index] + loop[index].length * closest_parameter(loop[index], point)


def span_between(loop: list, first: dict, second: dict) -> list:
    """The run of the loop travelled forward from one cut to the next."""
    index_a, u_a = first["index"], first["u"]
    index_b, u_b = second["index"], second["u"]
    if index_a == index_b and u_b > u_a:
        return [sub_primitive(loop[index_a], u_a, u_b)]

    pieces = [sub_primitive(loop[index_a], u_a, 1.0)]
    index = (index_a + 1) % len(loop)
    while index != index_b:
        pieces.append(loop[index])
        index = (index + 1) % len(loop)
    pieces.append(sub_primitive(loop[index_b], 0.0, u_b))
    return [piece for piece in pieces if piece.length > 1e-9]


def edge_trims(loop: list, starts: list, total: float, stop: Line,
               crossing_at: float, radius: float) -> tuple:
    """Where the cutter's EDGE first touches *stop*, going each way.

    One such point behind the crossing and one ahead; the tab keeps everything
    between them and the router cuts everything outside.
    """
    candidates = []
    for index, primitive in enumerate(loop):
        for point in stadium_intersections(primitive, stop.p0, stop.p1, radius):
            u = parameter_on(primitive, point)
            candidates.append({
                "index": index,
                "u": u,
                "point": point,
                "position": starts[index] + loop[index].length * u,
            })

    before = after = None
    nearest_back = nearest_forward = float("inf")
    for candidate in candidates:
        forward = (candidate["position"] - crossing_at) % total
        backward = (crossing_at - candidate["position"]) % total
        if 1e-12 < forward < nearest_forward:
            nearest_forward, after = forward, candidate
        if 1e-12 < backward < nearest_back:
            nearest_back, before = backward, candidate
    return before, after, nearest_back, nearest_forward


def find_stops(loop: list, tabs: list, radius: float, warnings: list) -> list:
    """Where every tab stops the cutter, in travel order.

    Each stop line gives a landmark - where the tool CENTRE crosses it - and the
    two points either side where the tool EDGE just reaches it. What bounds a
    cutting run is the edge.
    """
    starts, total = cumulative_lengths(loop)
    stops = []
    for tab_index, tab in enumerate(tabs):
        for stop_index, stop in enumerate(tab.stop_lines):
            hits = [
                (index, point)
                for index, primitive in enumerate(loop)
                for point in bounded_intersections(stop, primitive, tol=1e-7)
            ]
            if not hits:
                warnings.append(
                    f"tab {tab.label} stop line {stop_index + 1} does not reach "
                    f"the cutter path for a {radius * 2.0:.4f} mm cutter, so the "
                    f"path is left uncut there"
                )
                continue
            if len(hits) > 1:
                warnings.append(
                    f"tab {tab.label} stop line {stop_index + 1} crosses the "
                    f"cutter path {len(hits)} times; the first crossing is used"
                )
            index, point = hits[0]
            u = parameter_on(loop[index], point)
            crossing_at = starts[index] + loop[index].length * u
            before, after, back_mm, forward_mm = edge_trims(
                loop, starts, total, stop, crossing_at, radius
            )
            if before is None or after is None:
                warnings.append(
                    f"tab {tab.label} stop line {stop_index + 1} gives no edge "
                    f"contact at radius {radius:.4f} mm, so the path is left "
                    f"uncut there"
                )
                continue

            host = loop[index]
            tangent = (
                host.tangent_at_end() if isinstance(host, Line)
                else host.tangent_at_angle(math.atan2(
                    point[1] - host.center[1], point[0] - host.center[0]
                ))
            )
            length = math.dist(stop.p0, stop.p1)
            direction = (
                (stop.p1[0] - stop.p0[0]) / length,
                (stop.p1[1] - stop.p0[1]) / length,
            )
            stops.append(Stop(
                tab=tab_index,
                stop=stop_index,
                label=tab.label,
                point=point,
                position=crossing_at,
                before=before,
                after=after,
                back_mm=back_mm,
                forward_mm=forward_mm,
                angle_deg=math.degrees(math.asin(min(1.0, abs(
                    tangent[0] * direction[1] - tangent[1] * direction[0]
                )))),
                hosted_on=type(host).__name__,
                touch_case=tuple(
                    closest_feature(end["point"], stop.p0, stop.p1)
                    for end in (before, after)
                ),
            ))
    return sorted(stops, key=lambda entry: entry.position)


def base_anchor(loop: list, tab: Tab) -> float:
    """Where on the loop the cutter passes OVER this tab's base.

    The staple's base lies on the ideal board edge, so the piece of the offset
    loop directly outside it is the piece the router must not cut. Taking the
    loop point nearest the base midpoint names that piece without appeal to
    ordering, span length or how many tabs the board has.
    """
    starts, _ = cumulative_lengths(loop)
    return position_of(loop, starts, tab.position)


def split_loop(loop: list, tabs: list, stops: list, warnings: list) -> tuple:
    """Split the loop into the runs the cutter travels and the tab bridges.

    A tab's BRIDGE is the span of the loop lying over that tab's BASE: the board
    stays attached there and the router never enters it. Its ends are the EDGE
    contact points of the tab's two stop lines, so a bridge is wider than the
    stop-line spacing by one cutter radius at each end - exactly the material
    the bit would have removed had it driven on to the line. Everything else is
    cut.

    Naming the bridge by the base retires every heuristic that came before it.
    "The span between two stops of the same tab" does not say which of the two,
    "the shorter one" can be the sliver between trims that reach past each
    other, and "the span covering both crossings" needed a special case when a
    board had only one tab. The base is where the tab physically is, so asking
    which span lies over it answers all three at once, for one tab or twenty.
    """
    if not stops:
        return [], []

    _, total = cumulative_lengths(loop)

    def covers(start: float, length: float, position: float) -> bool:
        return (position - start) % total <= length + 1e-9

    by_tab = {}
    for position, stop in enumerate(stops):
        by_tab.setdefault(stop.tab, []).append(position)

    bridge_at = {}
    for tab_index, positions in by_tab.items():
        tab = tabs[tab_index]
        if len(positions) != 2:
            warnings.append(
                f"tab {tab.label} has {len(positions)} of its 2 stop lines on "
                f"the cutter path, so its bridge could not be closed and the "
                f"path runs through it"
            )
            continue
        anchor = base_anchor(loop, tab)
        chosen = None
        for position in positions:
            other = [p for p in positions if p != position][0]
            span = span_between(loop, stops[position].before, stops[other].after)
            start = stops[position].before["position"]
            length = sum(piece.length for piece in span)
            if covers(start, length, anchor):
                chosen = (position, other, span)
                break
        if chosen is None:
            warnings.append(
                f"tab {tab.label} has no span of the cutter path lying over its "
                f"base, so its bridge could not be identified"
            )
            continue
        bridge_at[chosen[0]] = (chosen[1], chosen[2], tab_index)

    bridges = [
        BridgeSpan(
            tab=tab_index,
            span=span,
            start=stops[entered].before["position"],
            length=sum(piece.length for piece in span),
        )
        for entered, (_, span, tab_index) in bridge_at.items()
    ]

    # A run goes from where one bridge lets the cutter back on to where the next
    # bridge takes it off again. `enters` is the stop a bridge starts at and
    # `leaves` the stop it ends at, so a run pairs each `leaves` with the next
    # `enters` in travel order.
    enters = sorted(bridge_at)
    leaves = [other for other, _, _ in bridge_at.values()]
    runs = []
    for leaving in leaves:
        after = stops[leaving].after["position"]
        entering = min(
            enters,
            key=lambda stop, a=after: (
                stops[stop].before["position"] - a
            ) % total,
        )
        runs.append(span_between(
            loop, stops[leaving].after, stops[entering].before
        ))
    return runs, bridges


# ---------------------------------------------------------------------------
# writing the board
# ---------------------------------------------------------------------------


def derived_uuid(key: str) -> str:
    """The uuid of one toolpath item: a function of its inputs alone."""
    return str(uuid.uuid5(TOOLPATH_NAMESPACE, key))


def derived_score_uuid(key: str) -> str:
    """The uuid of one score line, derived from the outline it was read off."""
    return str(uuid.uuid5(SCORE_NAMESPACE, key))


def clear_layer(board: Board, layer: str) -> int:
    """Delete every BOARD-LEVEL drawing on *layer*. Returns the count.

    This is what makes prep idempotent on the layers it owns: it starts from
    nothing rather than adding a second path beside the first. Footprint-level
    items on the same layer belong to the footprint library and are left alone.
    """
    before = len(board.drawings) + len(board.trailing_drawings)
    board.drawings = [
        item for item in board.drawings if layer not in item_layers(item)
    ]
    board.trailing_drawings = [
        item for item in board.trailing_drawings if layer not in item_layers(item)
    ]
    return before - len(board.drawings) - len(board.trailing_drawings)


def clear_toolpath(board: Board) -> int:
    """Delete every board-level item on the toolpath layer. Returns the count."""
    return clear_layer(board, TOOLPATH_LAYER)


def score_line_for(candidate, bbox: tuple) -> PcbLine:
    """The `User.38` line that ACCEPTING this candidate writes. Prep never writes it.

    The accept surface, in one function, so the eventual accept action, the
    corpus and the tests all agree on one answer.

    **One item per CANDIDATE, at the outline's bounding-box reach.** A v-score
    is one
    pass of a blade entering at one edge of the board and leaving at the other,
    so the line that stands for it says where the cut goes rather than where the
    outline happened to be interrupted. Merging scores across a panel, producing
    them into the frame and giving the blade its runway remain the panel's
    business.

    The uuid comes from the SORTED source uuids of the candidate's items, so
    re-saving the board in a different order does not rename the score line.
    """
    start, end = blade_extent(candidate, bbox)
    return PcbLine(
        start=start, end=end, layer=SCORE_LAYER,
        stroke=Stroke(width=SCORE_LINE_WIDTH_MM),
        id=derived_score_uuid("|".join(sorted(candidate.sources))),
    )


def score_lines_for(candidates: list, bbox: tuple) -> list:
    """:func:`score_line_for` over a list, in the order given."""
    return [score_line_for(candidate, bbox) for candidate in candidates]


# Nothing shorter than this is written down. The offset arrangement leaves a
# splinter wherever two curves touch almost tangentially - genuinely different
# curves, so `toolpath.merge_same_curve` is right not to weld them, and far too
# short to mean anything. Measured on the corpus board's own toolpath: 16 of its
# 53 items were 1.33 to 1.73 um long and the next-shortest was 85.09 um, a gap
# of a factor of 49 with ten micrometres sitting in the middle of it.
#
# It is the same number and the same rule as `channels.MIN_ITEM_MM`, applied one
# stage earlier: doing it here fixes
# the PLOT as well as the panel boolean, and leaves the panel's own floor as
# insurance rather than load-bearing. The cost is bounded by the dropped length,
# because a dropped piece's neighbours are welded across the gap it leaves.
MIN_ITEM_MM = 0.01

# ...and an arc that bows less than this off the straight line between its ends
# is written down as that line.
#
# A KiCad arc is stored as three points on a one-nanometre grid, so its centre
# is recovered from the file by circumscribing three rounded points. The radius
# that comes back is wrong by roughly the grid step over the sagitta - so at a
# sagitta of one nanometre it is wrong by about 100%, which is exactly what the
# corpus showed: eight arcs stored with a sagitta of 0 or 1 nm, reading back at
# radii from 0.22 mm to 314 mm and infinity, plotting as spurious circles a
# millimetre across in one render and as giant diagonals across the panel in
# another. `eedesigncore.render_board_svg` raises OverflowError on them.
#
# One micrometre is a thousand times that grid step, so the radius survives the
# round trip to within a tenth of a percent, and sixteen times below the
# smallest sagitta any real arc on the corpus board has (15.9 um). Straightening
# costs the sagitta itself - at most one micrometre, a tenth of the dust floor
# above and eighty-five times below the shortest item kept.
MIN_SAGITTA_MM = 1e-3


class Emission(NamedTuple):
    """The board items one build wrote, and the degenerate geometry it did not.

    `dropped_mm` is the whole of the geometry change: every point a dropped
    splinter would have covered lies within its own length of a point its
    welded neighbours cover. `straightened_mm` is the same accounting for the
    arcs, whose chords depart from them by at most one sagitta each.
    """

    items: list
    dropped: int
    dropped_mm: float
    straightened: int
    straightened_mm: float


def sagitta_mm(arc) -> float:
    """How far an arc bows off the straight line between its two ends.

    Written as `2 r sin^2(sweep/4)` rather than `r (1 - cos(sweep/2))`, which
    is the same identity without the cancellation: the arcs this exists to
    catch have sweeps of a few microradians, where `1 - cos` is all rounding.
    """
    return 2.0 * arc.radius * math.sin(arc.sweep / 4.0) ** 2


def emittable(run: list) -> tuple:
    """One run's pieces worth writing down, welded across what was dropped.

    Returns `(kept, dropped, dropped_mm)`, where `kept` is a list of
    `(index, piece)` pairs carrying each piece's ORIGINAL position in the run.
    That position is part of the surviving item's derived uuid, so renumbering
    after a drop would rename items that did not change - and re-prepping an
    unchanged board has to give back the same bytes.

    The weld happens only where a piece was actually dropped. Consecutive
    pieces of a run are contiguous by construction, so a gap anywhere else
    would be a discontinuity this stage has no business closing. It is
    :func:`toolpath.weld_gap`, so the two ends end up EXACTLY together
    wherever one of the pair is a line: a residue of a few nanometres is two
    nodes to the panel boolean that dilates this path later, and a boundary
    cannot pass through a node of degree four. Measured on the corpus,
    sliding the arc alone left 18 nm at one tab corner and the panel would
    not close.
    """
    kept, dropped, dropped_mm, pending = [], 0, 0.0, False
    for index, piece in enumerate(run):
        if piece.length < MIN_ITEM_MM:
            dropped += 1
            dropped_mm += piece.length
            pending = True
            continue
        if pending and kept:
            before, piece = weld_gap(kept[-1][1], piece)
            kept[-1] = (kept[-1][0], before)
        pending = False
        kept.append((index, piece))
    return kept, dropped, dropped_mm


def to_board_items(runs: list, width: float, layer: str = TOOLPATH_LAYER) -> Emission:
    """Every cutting run as stroked lines and arcs, with derived identities.

    Two kinds of degenerate geometry never reach the file: a splinter
    shorter than :data:`MIN_ITEM_MM`, which is dropped and welded over, and an
    arc flatter than :data:`MIN_SAGITTA_MM`, which is written as the line
    between its ends because that is what the file can say about it.
    """
    items, dropped, dropped_mm = [], 0, 0.0
    straightened, straightened_mm = 0, 0.0
    for run_index, run in enumerate(runs):
        kept, run_dropped, run_dropped_mm = emittable(run)
        dropped += run_dropped
        dropped_mm += run_dropped_mm
        for piece_index, piece in kept:
            key = f"{piece.source}|{piece.role}|{run_index}|{piece_index}"
            if layer != TOOLPATH_LAYER:
                key = f"{layer}|{key}"
            stroke = Stroke(width=width)
            if isinstance(piece, Line):
                items.append(PcbLine(
                    start=piece.p0, end=piece.p1, layer=layer,
                    stroke=stroke, id=derived_uuid(key),
                ))
                continue
            bow = sagitta_mm(piece)
            if bow < MIN_SAGITTA_MM:
                straightened += 1
                straightened_mm += bow
                items.append(PcbLine(
                    start=piece.start, end=piece.end, layer=layer,
                    stroke=stroke, id=derived_uuid(key),
                ))
                continue
            items.append(PcbArc(
                start=piece.start, mid=piece.mid, end=piece.end,
                layer=layer, stroke=stroke, id=derived_uuid(key),
            ))
    return Emission(
        items=items,
        dropped=dropped,
        dropped_mm=dropped_mm,
        straightened=straightened,
        straightened_mm=straightened_mm,
    )


def report_unreachable(unreachable: list, loop: list, bridges: list,
                       warnings: list, scored_sources: frozenset = frozenset()
                       ) -> None:
    """Warn about material left standing, and only where it is a surprise.

    A tab's break contour is drawn tighter than any cutter on purpose: it is the
    shape the board snaps along, made by drilling, and the router is meant to
    stop short of it. That geometry sits inside a span the router never enters,
    so it says nothing. The same recess on a stretch the router DOES cut is a
    different fact - the finished edge will not be the drawn edge there - and it
    gets one terse line naming where and how much rounding is left behind.
    """
    starts, total = cumulative_lengths(loop)
    for run in unreachable:
        position = position_of(loop, starts, run.at)
        if any(
            (position - bridge.start) % total <= bridge.length + 1e-9
            for bridge in bridges
        ):
            continue
        # Nor on an edge the saw takes: the router never goes there either.
        if all(source in scored_sources for source in run.sources):
            continue
        warnings.append(
            f"{run.span_mm:.3f} mm of outline near ({run.at[0]:.4f}, "
            f"{run.at[1]:.4f}) mm is tighter than the cutter, so it is left "
            f"standing with a {run.fillet_mm:.4f} mm fillet instead of cut"
        )


def _measure_bridge(bridge: BridgeSpan, tabs: list,
                    cutter_diameter_mm: float) -> Bridge:
    """One tab measured across its two cut faces, not along the path between."""
    gap = math.dist(bridge.span[0].start, bridge.span[-1].end)
    return Bridge(
        tab=bridge.tab,
        label=tabs[bridge.tab].label,
        centreline_mm=gap,
        material_mm=gap - cutter_diameter_mm,
        stop_spacing_mm=math.dist(
            tabs[bridge.tab].base.p0, tabs[bridge.tab].base.p1
        ),
    )


def build(board: Board, cutter_diameter_mm: float, vscore=None,
          min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM,
          eliminate_slivers=None,
          heal_gap_mm: float = CHAINING_EPSILON_MM,
          pocket_cutouts: bool = False, *,
          outline_override=None, tabs_override=None,
          toolpath_layer: str = TOOLPATH_LAYER, warn_unmatched=True) -> tuple:
    """Put the toolpath on *board* in place, honouring its v-scores. Returns what it did.

    `min_tab_mm` is the minimum tab: two kept runs whose ends leave less board
    than that between them are ONE run and the cutter travels straight across
    (:mod:`eedesignpanels.mintab`). The gaps the design declares - a tab's
    staple, a scored edge's two junctions - are exempt, and a declared tab
    narrower than the setting is reported rather than consumed.

    **THE SLIVERS A BOARD LEAVES ARE THE BOARD'S, AND SO IS THE INK THAT TAKES
    THEM OUT**. Two passes of the cutter running
    alongside each other leave a ribbon of thin material that is nothing to do
    with the panel - it exists in one board, at one cutter size - so the passes
    that remove it are computed HERE and written to `User.36` with everything
    else. The panel then steps them like any other toolpath ink, and its own
    outline comes out of a layer that already says everything the router does.

    `eliminate_slivers` false leaves every accidental sliver standing - the run
    ends are not merged and no covering pass is made - and each ribbon is named
    with its width and its position in the warnings. None takes the answer from
    `min_tab_mm` itself (:class:`mintab.Rule`), which is how the config's switch
    arrives without another parameter on every call between here and there.

    `pocket_cutouts` is a checkbox. FALSE, the default, routes the wall of a
    cutout wider than the cutter and leaves the middle standing as a loose slug,
    because most board houses do their own CAM. True adds the concentric passes
    that clear it.

    `heal_gap_mm` is the largest gap between two outline ends this run will
    close. It DEFAULTS to KiCad's own figure, so
    the default accepts every board KiCad accepts; zero refuses any gap. Every
    gap it does close is named in the warnings with its coordinate and its
    distance, because a repair nobody is told about is how a defective outline
    reaches a fab.

    **`User.38` is read, never written.** The scores this honours are the ones
    the designer drew; what this stage adds to the board is the toolpath and
    nothing else.

    `vscore` is a :class:`config.VScoreConfig`. **None means no v-score stage at
    all** - the board is routed all the way round, which is what every caller
    that predates scoring means and what a geometry test asking about the
    toolpath alone wants. `eedesignpanels.prep` always passes the config's
    section, whose own default is `detect="auto"`, so a project config that says
    nothing about scoring still gets its edges looked at.

    Raises:
        ValueError: when there is no outline to follow, or when a cutting run
            meets a score plane and no `route_past_score_mm` is declared. The
            second is an error for the same reason the cutter diameter is one:
            it is the fab's number, and inventing it would put a wrong overshoot
            into a real fabrication output.
        OffsetError: when the outline does not close. Anything the cutter
            merely cannot reach is reported and routed around, the way a mill
            would; only an outline that is not a loop stops this stage.
    """
    warnings = []
    radius = cutter_diameter_mm / 2.0
    min_tab_mm = mintab.checked_min_tab(min_tab_mm, eliminate_slivers)
    eliminate = min_tab_mm.eliminate
    vscore = vscore if vscore is not None else VScoreConfig(detect="none")

    drawn = outline_primitives(board) if outline_override is None else list(outline_override)
    tabs = (pair_mouths(tabs_of(board, warnings), mouth_points(board), warnings)
            if tabs_override is None else list(tabs_override))
    for tab in tabs:
        if tab.mouth or mouth_is_continuous(drawn, tab):
            continue
        warnings.append(
            f"tab {tab.label} declares no {MOUTH_POINT_LAYER} mouth points and "
            f"the outline does not run straight across its base at "
            f"({tab.position[0]:.4f}, {tab.position[1]:.4f}) mm, so the cutter "
            f"follows the break contour there instead of the ideal edge - "
            f"correct at the cutter this tab was drawn for, and wrong below it"
        )
    primitives, mouths = bridge_mouths(drawn, tabs, warnings)

    limits = [tab.tool_max_mm for tab in tabs if tab.tool_max_mm is not None]
    tool_max = min(limits) if limits else None
    for tab in tabs:
        if tab.tool_max_mm is None:
            warnings.append(
                f"tab {tab.label} carries no {TAB_TOOL_MAX_FIELD} field, so "
                f"nothing there constrains the cutter"
            )
        elif cutter_diameter_mm > tab.tool_max_mm:
            warnings.append(
                f"cutter diameter {cutter_diameter_mm:.4f} mm exceeds what tab "
                f"{tab.label} allows ({tab.tool_max_mm:.4f} mm)"
            )

    boundary, jobs, heals = routing_jobs(
        primitives, cutter_diameter_mm, warnings, heal_gap_mm,
        pocket=pocket_cutouts,
    )
    chain = weld_chain(boundary)
    # The decision model: the candidate set is derived from the shape, and the
    # board's own `User.38` lines say which of them the designer accepted.
    # `detect="none"` skips the whole stage - no proposals computed, no
    # decisions read - which is what `build(board, cutter)` has always meant.
    drawn = board_scores(board, warnings)
    if vscore.detect == "none":
        candidates, scores, stale = [], [], []
        if drawn:
            warnings.append(
                f"{len(drawn)} {SCORE_LAYER} line(s) on this board are ignored "
                f"because prep.vscore.detect is 'none', so every edge is routed "
                f"- including the ones the designer scored"
            )
    else:
        candidates = score_candidates(chain)
        scores, stale = validate_scores(candidates, drawn, warnings)
        for score in scores:
            blocked = blocking_tabs(score.candidate, tabs)
            if blocked:
                plural = "tab" if len(blocked) == 1 else "tabs"
                warnings.append(
                    f"{score.label} is accepted by a {SCORE_LAYER} line, and its "
                    f"blade line passes through {len(blocked)} {plural}, starting "
                    f"with {blocked[0].label} - the saw would cut through the "
                    f"material those tabs hold the board with"
                )

    offset = resolve_offset(chain, radius)
    loop = offset.loop
    scores = measure_scores(scores, loop)
    stops = find_stops(loop, tabs, radius, warnings)
    runs, bridges = split_loop(loop, tabs, stops, warnings)
    report_unreachable(
        offset.unreachable, loop, bridges, warnings,
        frozenset(source for score in scores for source in score.candidate.sources),
    )

    if not stops:
        warnings.append(
            "no stop line reached the cutter path, so the toolpath is the whole "
            "closed loop and no tab is preserved"
        )
        runs = [loop]

    junction, at = first_score_junction(runs, scores)
    if junction is not None and vscore.route_past_score_mm is None:
        raise ValueError(
            f"the cutting run meets {junction.label} at ({at[0]:.4f}, "
            f"{at[1]:.4f}) mm, and prep.vscore.route_past_score_mm is not "
            f"declared. How far the router must overshoot the score plane comes "
            f"from the fab's blade geometry, so this stage will not guess it - "
            f"declare it, or set prep.vscore.detect to 'none' to route this "
            f"edge instead of scoring it."
        )
    runs, junctions = apply_scores(
        runs, scores, vscore.route_past_score_mm or 0.0, warnings, chain,
        along_score=vscore.route_along_score_mm,
        tangent_angle=vscore.route_tangent_angle_deg,
        overrides=vscore.route_end_overrides if vscore.custom_route_ends else {},
        warn_unmatched=warn_unmatched,
        operation=toolpath_layer,
    )

    # THE MINIMUM TAB, last: it acts on the FINISHED runs, so a sliver left by
    # a tab, by a score junction or by the two together is caught the same way
    # whatever produced it.
    guards = mintab.tab_guards(tabs) + mintab.score_guards(scores, junctions)
    enforced = mintab.join_run_ends(
        runs, radius, min_tab_mm, guards=guards, consume=eliminate,
    )
    runs = [list(run) for run in enforced.runs]
    for entry in enforced.protected:
        warnings.append(mintab.protection_warning(entry))
    note = mintab.join_warning(enforced.joins, min_tab_mm)
    if note and eliminate:
        warnings.append(note)

    # ...AND THE RIBBONS, on the runs as they now stand. A run-end join joins
    # two ENDS; this is the material two passes leave beside each other, which
    # has a length and needs a chain of crossings to take out. Every
    # crossing is written to `User.36` below, so the board file alone says
    # everything the router does - which is what the panel's outline is derived
    # from.
    covering = (
        mintab.covering_joins(runs, radius, min_tab_mm, guards=guards)
        if eliminate else ()
    )
    standing = (
        () if eliminate
        else mintab.standing_slivers(runs, radius, min_tab_mm, guards=guards)
    )
    for ribbon in standing:
        warnings.append(mintab.sliver_warning(ribbon, min_tab_mm))
    for entry in (() if eliminate else enforced.joins):
        warnings.append(
            f"SLIVER LEFT STANDING: {entry.material_mm:.4f} mm of material "
            f"stands between two cut ends at ({entry.at[0]:.4f}, "
            f"{entry.at[1]:.4f}) mm, under the {min_tab_mm:.4f} mm minimum tab "
            f"- eliminate_slivers is off, so the two runs are not joined"
        )

    # The last word on the result, independent of everything that produced it.
    through = rout_through(runs, tabs, radius)
    on_score = routed_scores(runs, scores, radius)
    for hit in on_score:
        warnings.append(
            f"ROUTED SCORE: the finished cut runs along {hit.label} - the cutter "
            f"centre comes within {hit.clearance_mm:.4f} mm of the score at "
            f"({hit.at[0]:.4f}, {hit.at[1]:.4f}) mm, and the bit's radius is "
            f"{hit.radius_mm:.4f} mm, so this edge is being machined away rather "
            f"than scored"
        )
    for hit in through:
        warnings.append(
            f"ROUT-THROUGH: the finished cut runs through tab {hit.label} - the "
            f"cutter centre comes within {hit.clearance_mm:.4f} mm of the base "
            f"at ({hit.at[0]:.4f}, {hit.at[1]:.4f}) mm, and the bit's radius is "
            f"{hit.radius_mm:.4f} mm, so this tab does not hold the board"
        )

    # `User.36` only. `User.38` is the designer's layer and this stage has read
    # it; touching it here is what the decision model forbids.
    #
    # The covering passes go on the layer WITH the runs, each as a path of its
    # own: a cut that meets another cut part-way along it is a T, and a run is a
    # path. They are last, and in the order the rule produced them, so a board
    # with no sliver in it writes exactly the items it wrote before this existed.
    # EVERY OTHER LOOP IS A JOB OF ITS OWN, cut
    # with the SAME declared cutter. They are emitted LAST and in loop order, so
    # a board whose outline is one loop writes exactly the items it wrote before
    # any of this existed - which is what keeps the corpus byte-identical.
    # Nothing is welded across jobs: a tab, a score and the minimum tab are all
    # facts about the boundary, and a second loop is a separate cut.
    # A POCKETED CUTOUT'S PASSES FOLLOW ITS OWN WALL PASS, in that order and
    # nowhere else, so a board with the box clear writes no pocketing items.
    interior = []
    for job in jobs:
        if job.is_boundary or not job.made or not job.path:
            continue
        interior.append(list(job.path))
        interior.extend(list(ring) for ring in job.pocket)
    removed = clear_layer(board, toolpath_layer)
    emission = to_board_items(
        [*runs, *([join.crossing] for join in covering), *interior],
        cutter_diameter_mm, layer=toolpath_layer,
    )
    items = emission.items
    board.drawings.extend(items)

    declared = {layer.canonical_name for layer in board.layers}
    if toolpath_layer not in declared:
        warnings.append(
            f"the board's layer table does not declare {toolpath_layer}, so "
            f"KiCad will not plot the toolpath that was just written to it"
        )
    if drawn and SCORE_LAYER not in declared:
        warnings.append(
            f"the board's layer table does not declare {SCORE_LAYER}, so KiCad "
            f"will not plot the {len(drawn)} v-score line(s) on it"
        )

    return BuildResult(
        tabs=tabs,
        stops=stops,
        removed=removed,
        outline_items=len(primitives),
        loop_items=len(loop),
        items=items,
        run_lengths=[sum(p.length for p in run) for run in runs],
        bridges=[
            _measure_bridge(bridge, tabs, cutter_diameter_mm)
            for bridge in bridges
        ],
        tool_max_mm=tool_max,
        warnings=warnings,
        mouths=mouths,
        rout_through=through,
        candidates=candidates,
        scores=scores,
        junctions=junctions,
        routed_scores=on_score,
        route_past_mm=vscore.route_past_score_mm,
        proposals=score_proposals(candidates, scores, tabs),
        score_lines=drawn,
        stale_scores=stale,
        dropped=emission.dropped,
        dropped_mm=emission.dropped_mm,
        straightened=emission.straightened,
        straightened_mm=emission.straightened_mm,
        min_tab_mm=min_tab_mm,
        joins=enforced.joins,
        protected_tabs=enforced.protected,
        eliminate_slivers=eliminate,
        covering=covering,
        standing=standing,
        jobs=jobs,
        heals=heals,
        heal_gap_mm=heal_gap_mm,
        pocket_cutouts=bool(pocket_cutouts),
        boundary_chain=tuple(chain),
        boundary_runs=tuple(tuple(run) for run in runs),
        toolpath_layer=toolpath_layer,
    )


def score_signature(board: Board) -> list:
    """Every board-level `User.38` item, reduced to what a diff would show.

    The measurement behind the no-write invariant. Prep must leave the
    designer's decisions exactly as it found them, and the cheapest honest way
    to say so is to look at them before and after.
    """
    return [
        (
            type(item).__name__,
            item.id,
            getattr(item, "start", None),
            getattr(item, "end", None),
            getattr(item, "mid", None),
        )
        for item in items_on_layers(
            (*board.drawings, *board.trailing_drawings), {SCORE_LAYER}
        )
    ]


def run(config: PanelConfig) -> PrepResult:
    """Prepare the source board named by the config. WRITES TO IT.

    It writes `User.36`. It does NOT write `User.38`: the scores are the
    designer's decisions and this stage only reads and honours them.
    """
    if config.prep is None:
        raise ValueError(
            "this config has no 'prep' section, so there is no cutter diameter "
            "to prepare with. A cutter diameter is declared, never inferred."
        )

    board_path = config.source_board_path
    started = time.monotonic()
    board = Board.from_file(board_path)
    loaded = time.monotonic()

    before = score_signature(board)
    from eedesignpanels import rigid_flex
    if rigid_flex.is_rigid_flex(board, config.source_project_path):
        made = rigid_flex.build(board, config)
    else:
        made = build(board, config.prep.cutter_diameter, config.prep.vscore,
                     config.min_tab_mm, heal_gap_mm=config.outline_heal_mm,
                     pocket_cutouts=config.pocket_cutouts)
    untouched = score_signature(board) == before
    built = time.monotonic()

    board.to_file(board_path)
    written = time.monotonic()

    return PrepResult(
        board_path=board_path,
        board_sha256=sha256_of(board_path),
        cutter_diameter_mm=config.prep.cutter_diameter,
        tabs=made.tabs,
        stops=made.stops,
        removed=made.removed,
        outline_items=made.outline_items,
        loop_items=made.loop_items,
        written=len(made.items),
        run_lengths=made.run_lengths,
        bridges=made.bridges,
        tool_max_mm=made.tool_max_mm,
        warnings=made.warnings,
        load_seconds=loaded - started,
        build_seconds=built - loaded,
        write_seconds=written - built,
        mouths=made.mouths,
        rout_through=made.rout_through,
        candidates=made.candidates,
        scores=made.scores,
        junctions=made.junctions,
        routed_scores=made.routed_scores,
        route_past_mm=made.route_past_mm,
        proposals=made.proposals,
        score_lines=made.score_lines,
        stale_scores=made.stale_scores,
        scores_untouched=untouched,
        dropped=made.dropped,
        dropped_mm=made.dropped_mm,
        straightened=made.straightened,
        straightened_mm=made.straightened_mm,
        min_tab_mm=made.min_tab_mm,
        joins=made.joins,
        protected_tabs=made.protected_tabs,
        eliminate_slivers=made.eliminate_slivers,
        covering=made.covering,
        standing=made.standing,
        jobs=tuple(made.jobs) + tuple(job for rigid in made.rigid_results for job in rigid.jobs),
        heals=made.heals,
        heal_gap_mm=made.heal_gap_mm,
        pocket_cutouts=made.pocket_cutouts,
        toolpath_layer=made.toolpath_layer,
        rigid_results=made.rigid_results,
    )


__all__ = [
    "Arc",
    "Emission",
    "Line",
    "MIN_ITEM_MM",
    "MIN_SAGITTA_MM",
    "MOUTH_POINT_LAYER",
    "Mouth",
    "MouthPoint",
    "JUNCTION_NORMAL",
    "JUNCTION_WARN",
    "OUTLINE_LAYER",
    "PrepResult",
    "Proposal",
    "RoutThrough",
    "SCORE_LAYER",
    "SCORE_LINE_WIDTH_MM",
    "STOP_LINE_LAYER",
    "TAB_TOOL_MAX_FIELD",
    "TOOLPATH_LAYER",
    "Score",
    "ScoreJunction",
    "ScoreLine",
    "ScoredEdgeRouted",
    "StaleScore",
    "VERDICT_PROPOSED",
    "VERDICT_REJECTED",
    "VERDICT_SCORED",
    "apply_scores",
    "between_scored_spans",
    "blocking_tabs",
    "board_scores",
    "boundary_loop",
    "bridge_mouths",
    "build",
    "candidate_id",
    "clear_toolpath",
    "emittable",
    "first_score_junction",
    "junction_verdict",
    "line_on_blade",
    "measure_scores",
    "mouth_is_continuous",
    "mouth_points",
    "outline_bbox",
    "pair_mouths",
    "plane_parameters",
    "rout_through",
    "routing_jobs",
    "routed_scores",
    "run",
    "sagitta_mm",
    "score_label",
    "score_line_for",
    "score_lines_for",
    "score_on",
    "score_proposals",
    "score_signature",
    "split_run_at_scores",
    "standing_edge_mm",
    "string_between",
    "tabs_of",
    "to_board_items",
    "validate_scores",
]
