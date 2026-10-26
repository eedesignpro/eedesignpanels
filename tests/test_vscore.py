"""V-scores: which edges a saw could take, and what that does to the toolpath.

Three parts, kept apart on purpose.

The PROPOSER is pure geometry - `eedesignpanels.toolpath.score_candidates` on a
chain of lines and arcs - so the cases here are shapes whose answer can be read
off a drawing: a rounded corner whose straight side does not reach the board's
extents is still scoreable, because the blade passes outside the fillet; an
inner edge of an L is not, because the same blade would run through the other
leg.

The DECISION MODEL is the second. One shape yields one
deterministic candidate set; which of them are scored is the DESIGNER's choice,
and it lives in the board as `User.38` lines. Prep never writes that layer - it
validates, by geometry, extent-tolerantly - so the cases here are about
matching, about a line that matches nothing, and about the no-write invariant
itself.

The third is the STAGE, where a validated score is a route suppressor. What
matters there is arithmetic a machinist would check: the run beside a score ends
where the cutting EDGE - not the centre - is the declared distance past the
score plane, which is a different number from the centre by exactly one radius.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    PcbArc,
    PcbLine,
    Property,
    Setup,
    Stroke,
    item_layers,
)

from eedesignpanels import checks, prep
from eedesignpanels.config import (
    CONFIG_REVISION,
    DEFAULT_EXCLUDED_LAYERS,
    VScoreConfig,
    load_config,
)
from eedesignpanels.stages import route_toolpath
from eedesignpanels.stages.route_toolpath import (
    SCORE_LAYER,
    SCORE_LINE_WIDTH_MM,
    STOP_LINE_LAYER,
    TAB_TOOL_MAX_FIELD,
    TOOLPATH_LAYER,
    JUNCTION_NORMAL,
    JUNCTION_WARN,
    VERDICT_PROPOSED,
    VERDICT_REJECTED,
    VERDICT_SCORED,
    Score,
    ScoreLine,
    blocking_tabs,
    board_scores,
    build,
    candidate_id,
    junction_verdict,
    line_on_blade,
    outline_bbox,
    outline_primitives,
    routed_scores,
    score_line_for,
    score_proposals,
    score_signature,
    split_run_at_scores,
    standing_edge_mm,
    tabs_of,
    validate_scores,
)
from eedesignpanels.toolpath import (
    Line,
    arc_from_three_points,
    blade_extent,
    chain_bbox,
    chain_loop,
    collinear_line_runs,
    point_inside_loop,
    nearest_on_path,
    on_blade,
    score_candidates,
    weld_chain,
)

CUTTER = 0.889
RADIUS = CUTTER / 2.0
STOP_LENGTH = 0.6


# ---------------------------------------------------------------------------
# chains, built by hand - the proposer needs no board
# ---------------------------------------------------------------------------


def polygon(points: list) -> list:
    """A closed chain of lines through *points*, in the order given."""
    return [
        Line(p0=points[index], p1=points[(index + 1) % len(points)],
             source=f"l{index}")
        for index in range(len(points))
    ]


def square_chain(side: float = 10.0) -> list:
    return polygon([(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)])


def corner_arc(start: tuple, end: tuple, center: tuple, source: str):
    """The quarter arc from *start* to *end* about *center*, through its middle."""
    first = math.atan2(start[1] - center[1], start[0] - center[0])
    last = math.atan2(end[1] - center[1], end[0] - center[0])
    delta = (last - first + math.pi) % (2.0 * math.pi) - math.pi
    radius = math.dist(start, center)
    middle = (
        center[0] + radius * math.cos(first + delta / 2.0),
        center[1] + radius * math.sin(first + delta / 2.0),
    )
    return arc_from_three_points(start, middle, end, source=source)


def filleted_chain(radius: float = 2.0) -> list:
    """A 20x10 rectangle whose two RIGHT corners are rounded.

    The corpus board's shape, in miniature: the right edge is a straight span
    that stops short of the board's own extents, and the only thing its line
    meets on the way to them is the fillet it is tangent to.
    """
    return [
        Line(p0=(0.0, 0.0), p1=(20.0 - radius, 0.0), source="bottom"),
        corner_arc((20.0 - radius, 0.0), (20.0, radius),
                   (20.0 - radius, radius), "fillet-lower"),
        Line(p0=(20.0, radius), p1=(20.0, 10.0 - radius), source="right"),
        corner_arc((20.0, 10.0 - radius), (20.0 - radius, 10.0),
                   (20.0 - radius, 10.0 - radius), "fillet-upper"),
        Line(p0=(20.0 - radius, 10.0), p1=(0.0, 10.0), source="top"),
        Line(p0=(0.0, 10.0), p1=(0.0, 0.0), source="left"),
    ]


def notched_edge_chain() -> list:
    """A 20x10 rectangle with a bite taken out of its `y = 0` edge.

    The corpus board's top edge in miniature: ONE straight edge the designer
    interrupted, so the outline lies on that line in two separate places. A saw
    cuts both in one pass, which is why they are one candidate and not two.
    """
    return polygon([
        (0.0, 0.0), (8.0, 0.0), (8.0, 3.0), (12.0, 3.0), (12.0, 0.0),
        (20.0, 0.0), (20.0, 10.0), (0.0, 10.0),
    ])


def ell_chain() -> list:
    """An L: 20x4 along the bottom with a 10x10 leg standing on its left."""
    return polygon([
        (0.0, 0.0), (10.0, 0.0), (10.0, 6.0), (20.0, 6.0),
        (20.0, 10.0), (0.0, 10.0),
    ])


def spans_of(candidates: list) -> set:
    return {
        (round(candidate.start[0], 6), round(candidate.start[1], 6),
         round(candidate.end[0], 6), round(candidate.end[1], 6))
        for candidate in candidates
    }


class ChainBoxTest(unittest.TestCase):
    def test_an_arc_bulge_is_in_the_box(self):
        # The extent of a fillet is not either of its endpoints, and a bbox that
        # missed it would stop a score's line short of the board.
        chain = chain_loop(filleted_chain())
        for measured, expected in zip(chain_bbox(chain), (0.0, 0.0, 20.0, 10.0)):
            self.assertAlmostEqual(measured, expected, places=9)


class PointInsideLoopTest(unittest.TestCase):
    """The proposer's inside test, by parity count.

    The chord-walk cases live in `tests/test_material.py`, with the 280 degree
    bulge that `winding_number` misreads. These are the shapes the PROPOSER
    asks about, kept here beside it.
    """
    def test_inside_and_outside_a_square(self):
        chain = chain_loop(square_chain())
        self.assertTrue(point_inside_loop(chain, (5.0, 5.0)))
        self.assertFalse(point_inside_loop(chain, (-1.0, 5.0)))
        self.assertFalse(point_inside_loop(chain, (15.0, 5.0)))

    def test_the_hollow_of_an_l_is_outside(self):
        chain = chain_loop(ell_chain())
        self.assertTrue(point_inside_loop(chain, (5.0, 5.0)))
        self.assertFalse(point_inside_loop(chain, (15.0, 2.0)))

    def test_an_arc_carries_its_own_bulge(self):
        chain = chain_loop(filleted_chain())
        # Just inside the fillet, and just outside the square corner it replaces.
        self.assertTrue(point_inside_loop(chain, (18.5, 0.5)))
        self.assertFalse(point_inside_loop(chain, (19.8, 0.2)))


class CollinearRunTest(unittest.TestCase):
    def test_one_edge_drawn_as_three_items_is_one_run(self):
        chain = chain_loop(polygon([
            (0.0, 0.0), (3.0, 0.0), (7.0, 0.0), (10.0, 0.0),
            (10.0, 10.0), (0.0, 10.0),
        ]))
        merged = [group for group in collinear_line_runs(chain) if len(group) > 1]
        self.assertEqual(len(merged), 1)
        group = merged[0]
        self.assertEqual(len(group), 3)
        self.assertEqual(chain[group[0]].start, (0.0, 0.0))
        self.assertEqual(chain[group[-1]].end, (10.0, 0.0))

    def test_a_nanometre_of_drift_is_still_one_run(self):
        # A real file's straight edge is not exactly straight: an item drawn up
        # to a footprint's declared point picks up that point's rounding.
        chain = chain_loop(polygon([
            (0.0, 0.0), (3.0, 0.000001), (7.0, 0.0), (10.0, 0.0),
            (10.0, 10.0), (0.0, 10.0),
        ]))
        self.assertTrue(any(len(group) == 3 for group in collinear_line_runs(chain)))

    def test_a_real_corner_is_not_a_run(self):
        chain = chain_loop(square_chain())
        self.assertTrue(all(len(group) == 1 for group in collinear_line_runs(chain)))


class ProposerTest(unittest.TestCase):
    def test_every_side_of_a_square_could_be_scored(self):
        candidates = score_candidates(chain_loop(square_chain()))
        self.assertEqual(len(candidates), 4)

    def test_the_normal_points_into_the_board(self):
        for candidate in score_candidates(chain_loop(square_chain(10.0))):
            inside = (
                candidate.midpoint[0] + candidate.normal[0],
                candidate.midpoint[1] + candidate.normal[1],
            )
            self.assertTrue(point_inside_loop(chain_loop(square_chain(10.0)), inside))

    def test_a_span_short_of_the_extents_still_scores_past_a_fillet(self):
        # THE CASE THIS EXISTS FOR. The right edge stops 2 mm short of the
        # board's own extents at each end, and its line reaches them through the
        # air outside the two fillets, touching each at exactly one point. A
        # tangency is not a crossing, so the blade can travel it.
        candidates = score_candidates(chain_loop(filleted_chain()))
        self.assertIn((20.0, 2.0, 20.0, 8.0), spans_of(candidates))

    def test_an_arc_is_never_a_candidate(self):
        candidates = score_candidates(chain_loop(filleted_chain()))
        # Four straight sides, no fillet among them, however scoreable the
        # straight parts either side of one are.
        self.assertEqual(len(candidates), 4)
        for candidate in candidates:
            self.assertNotIn("fillet", " ".join(candidate.sources))

    def test_an_edge_whose_line_crosses_the_board_is_not_a_candidate(self):
        candidates = spans_of(score_candidates(chain_loop(ell_chain())))
        # The L's inner edges: one runs into the other leg, the other into the
        # tall part. Neither is a cut a blade could make.
        self.assertNotIn((10.0, 0.0, 10.0, 6.0), candidates)
        self.assertNotIn((10.0, 6.0, 20.0, 6.0), candidates)
        self.assertNotIn((20.0, 6.0, 10.0, 6.0), candidates)
        # ...while the outer ones are.
        self.assertIn((0.0, 0.0, 10.0, 0.0), candidates)
        self.assertIn((20.0, 6.0, 20.0, 10.0), candidates)

    def test_a_merged_span_is_proposed_whole(self):
        chain = chain_loop(polygon([
            (0.0, 0.0), (3.0, 0.0), (10.0, 0.0),
            (10.0, 10.0), (0.0, 10.0),
        ]))
        found = [
            candidate for candidate in score_candidates(chain)
            if len(candidate.items) > 1
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].start, (0.0, 0.0))
        self.assertEqual(found[0].end, (10.0, 0.0))

    def test_a_broken_edge_is_ONE_candidate_carrying_TWO_spans(self):
        # THE COUNTING RULE. A v-score candidate is a BLADE LINE. The bite out
        # of the top edge splits the outline on that line into two stretches,
        # and one pass of the saw takes both, so the designer counts one.
        candidates = score_candidates(chain_loop(notched_edge_chain()))
        self.assertEqual(len(candidates), 4)
        broken = [entry for entry in candidates if len(entry.spans) > 1]
        self.assertEqual(len(broken), 1)
        self.assertEqual(len(broken[0].spans), 2)
        self.assertEqual(
            [(span.start, span.end) for span in broken[0].spans],
            [((0.0, 0.0), (8.0, 0.0)), ((12.0, 0.0), (20.0, 0.0))],
        )

    def test_the_candidate_measures_both_its_edge_and_its_reach(self):
        # 16 mm of outline lies on a line the blade travels 20 mm of. Both
        # numbers are real and they are not the same number.
        broken = [
            entry for entry in score_candidates(chain_loop(notched_edge_chain()))
            if len(entry.spans) > 1
        ][0]
        self.assertAlmostEqual(broken.length_mm, 16.0, places=9)
        self.assertAlmostEqual(broken.extent_mm, 20.0, places=9)
        self.assertEqual(broken.start, (0.0, 0.0))
        self.assertEqual(broken.end, (20.0, 0.0))

    def test_the_spans_of_a_candidate_carry_every_source_item(self):
        broken = [
            entry for entry in score_candidates(chain_loop(notched_edge_chain()))
            if len(entry.spans) > 1
        ][0]
        self.assertEqual(
            sorted(broken.sources),
            sorted(source for span in broken.spans for source in span.sources),
        )
        self.assertEqual(len(broken.items), 2)

    def test_two_edges_on_one_line_facing_apart_stay_two_candidates(self):
        # A Z: both stretches lie on y = 5, but the material is BELOW one and
        # ABOVE the other. One saw pass cannot serve both, so they are judged
        # separately rather than merged into a line that means nothing.
        chain = chain_loop(polygon([
            (0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (20.0, 5.0),
            (20.0, 10.0), (10.0, 10.0), (10.0, 5.0), (0.0, 5.0),
        ]))
        on_the_line = [
            entry for entry in score_candidates(chain)
            if abs(entry.start[1] - 5.0) < 1e-9 and abs(entry.end[1] - 5.0) < 1e-9
        ]
        for entry in on_the_line:
            self.assertEqual(len(entry.spans), 1)

    def test_straightness_is_measured_not_assumed(self):
        # A kink of a tenth of a micrometre is still one cut, and the score line
        # says how far it strayed from the outline it replaced.
        chain = chain_loop(polygon([
            (0.0, 0.0), (3.0, 0.0001), (10.0, 0.0),
            (10.0, 10.0), (0.0, 10.0),
        ]))
        merged = [
            candidate for candidate in score_candidates(chain)
            if len(candidate.items) == 2
        ]
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].straightness_mm, 0.0001, places=7)


# ---------------------------------------------------------------------------
# boards, for the stage
# ---------------------------------------------------------------------------


def edge(x0, y0, x1, y1, layer="Edge.Cuts", uuid_value=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer,
        id=uuid_value or f"e-{x0}-{y0}-{x1}-{y1}",
    )


def staple(half_gap=0.41, length=STOP_LENGTH, tag="s"):
    return [
        PcbLine(start=(-half_gap, 0.0), end=(-half_gap, -length),
                layer=STOP_LINE_LAYER, id=f"{tag}-leg0"),
        PcbLine(start=(-half_gap, 0.0), end=(half_gap, 0.0),
                layer=STOP_LINE_LAYER, id=f"{tag}-base"),
        PcbLine(start=(half_gap, 0.0), end=(half_gap, -length),
                layer=STOP_LINE_LAYER, id=f"{tag}-leg1"),
    ]


def tab_footprint(position, angle=0.0, uuid_value="tab-1", tool_max="0.8923"):
    return Footprint(
        fp_id="PanelTab:Panel_Tab_test",
        layer="F.Cu",
        uuid=uuid_value,
        at={"position": position, "angle": angle},
        properties=[
            Property(name="Reference", value=uuid_value, id=f"{uuid_value}-ref"),
            Property(name=TAB_TOOL_MAX_FIELD, value=tool_max,
                     id=f"{uuid_value}-max"),
        ],
        drawings=staple(tag=uuid_value),
    )


def scoreable_board(side: float = 10.0) -> Board:
    """A square with a tab on three of its four sides.

    The fourth - the LEFT edge - is then the only span a saw could take, because
    a tab on a line is a designer saying that line is routed. One score, two
    junctions, and every other edge behaving exactly as it did before scores
    existed.
    """
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            # A board KiCad will open carries a back copper layer. MEASURED:
            # without it kicad-cli answers "Failed to load board"
            # with rc=3, and the preview now renders through kicad-cli - one
            # call, every layer - so a fixture missing it cannot be drawn at
            # all.
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
            # The solder mask, in the ordinals KiCad 10 writes. A non-plated
            # hole opens an aperture here and nowhere else - the pad is
            # `(layers "*.Cu" "*.Mask")` with no annular ring - so a fixture
            # without these two cannot show a tooling hole at all.
            BoardLayer(ordinal=1, canonical_name="F.Mask", layer_type="user"),
            BoardLayer(ordinal=3, canonical_name="B.Mask", layer_type="user"),
            BoardLayer(ordinal=25, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=53, canonical_name=STOP_LINE_LAYER,
                       layer_type="user", user_name="User.8-RouteToolStops"),
            BoardLayer(ordinal=109, canonical_name=TOOLPATH_LAYER,
                       layer_type="user", user_name="User.36-RouteToolPath"),
            BoardLayer(ordinal=113, canonical_name=SCORE_LAYER,
                       layer_type="user", user_name="User.38-VScore"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    board.drawings = [
        edge(0.0, 0.0, side, 0.0),
        edge(side, 0.0, side, side),
        edge(side, side, 0.0, side),
        edge(0.0, side, 0.0, 0.0),
    ]
    board.footprints = [
        tab_footprint((side / 2.0, 0.0), 0.0, "TOP"),
        tab_footprint((side / 2.0, side), 180.0, "BOTTOM"),
        tab_footprint((side, side / 2.0), 270.0, "RIGHT"),
    ]
    return board


def candidates_of(board: Board) -> list:
    """The board's candidate blade lines, the way the stage derives them."""
    return score_candidates(weld_chain(chain_loop(outline_primitives(board))))


def accept(board: Board, *, x: float = None, y: float = None) -> Board:
    """Draw the DESIGNER's decision: accept the candidate on the named line.

    Exactly what the accept action writes - :func:`score_line_for`, at the
    outline's bounding-box reach - so the tests exercise the accept surface
    rather than a fixture invented beside it. Named by WHERE the line is,
    because that is how the decision model identifies anything.
    """
    bbox = outline_bbox(board)
    found = [
        candidate for candidate in candidates_of(board)
        if (x is None or (abs(candidate.start[0] - x) < 1e-9
                          and abs(candidate.end[0] - x) < 1e-9))
        and (y is None or (abs(candidate.start[1] - y) < 1e-9
                           and abs(candidate.end[1] - y) < 1e-9))
    ]
    if len(found) != 1:
        raise AssertionError(
            f"expected one candidate at x={x} y={y}, found {len(found)}"
        )
    board.drawings.append(score_line_for(found[0], bbox))
    return board


def accept_every_candidate(board: Board) -> Board:
    """The decision "score all of it", for a board with nothing in the way."""
    bbox = outline_bbox(board)
    for candidate in candidates_of(board):
        board.drawings.append(score_line_for(candidate, bbox))
    return board


def items_on(board: Board, layer: str) -> list:
    return [
        item for item in (*board.drawings, *board.trailing_drawings)
        if layer in item_layers(item)
    ]


def toolpath_points(board: Board) -> list:
    return [
        point for item in items_on(board, TOOLPATH_LAYER)
        for point in (item.start, item.end)
    ]


def past_the_plane(board: Board) -> list:
    """Every toolpath endpoint PAST the score plane at x = 0, ordered.

    On these fixtures the board is at x > 0 and its offset never reaches x < 0
    of its own accord, so what this finds is exactly the ends the junction
    produced - and their number is part of the answer.
    """
    return sorted(
        point[0] for point in toolpath_points(board) if point[0] < -1e-9
    )


def filleted_board(radius: float = 2.0) -> Board:
    """A bare rectangle whose RIGHT edge is short, between two fillets.

    The corpus board's right-hand end: the scoreable edge is 6 mm of a 10 mm
    side, and the runs that meet it arrive round a fillet rather than driving
    straight at the plane. There the centreline and the contact point differ.
    """
    board = scoreable_board()
    board.footprints = []
    board.drawings = [
        edge(0.0, 0.0, 20.0 - radius, 0.0, uuid_value="bottom"),
        PcbArc(start=(20.0 - radius, 0.0), mid=(19.414214, 0.585786),
               end=(20.0, radius), layer="Edge.Cuts", id="fillet-lower"),
        edge(20.0, radius, 20.0, 10.0 - radius, uuid_value="right"),
        PcbArc(start=(20.0, 10.0 - radius), mid=(19.414214, 9.414214),
               end=(20.0 - radius, 10.0), layer="Edge.Cuts",
               id="fillet-upper"),
        edge(20.0 - radius, 10.0, 0.0, 10.0, uuid_value="top"),
        edge(0.0, 10.0, 0.0, 0.0, uuid_value="left"),
    ]
    return board


def notched_board() -> Board:
    """A square whose top edge is interrupted, with ONE tab on that same edge.

    The corpus board's shape as far as the saw is concerned: the top line
    carries outline in two places and a mousebite between them, so it is one
    candidate, and one tab is enough to rule the whole line out.
    """
    board = scoreable_board()
    board.drawings = [
        edge(0.0, 0.0, 4.0, 0.0),
        edge(4.0, 0.0, 4.0, 1.5),
        edge(4.0, 1.5, 6.0, 1.5),
        edge(6.0, 1.5, 6.0, 0.0),
        edge(6.0, 0.0, 10.0, 0.0),
        edge(10.0, 0.0, 10.0, 10.0),
        edge(10.0, 10.0, 0.0, 10.0),
        edge(0.0, 10.0, 0.0, 0.0),
    ]
    board.footprints = [tab_footprint((2.0, 0.0), 0.0, "TOP")]
    return board


class AggregationOnABoardTest(unittest.TestCase):
    """The count a designer reads is candidate LINES, off a real board."""

    def candidates(self, board: Board) -> list:
        return score_candidates(weld_chain(chain_loop(outline_primitives(board))))

    def test_an_interrupted_edge_counts_once(self):
        candidates = self.candidates(notched_board())
        self.assertEqual(len(candidates), 4)
        self.assertEqual(
            sorted(len(entry.spans) for entry in candidates), [1, 1, 1, 2]
        )

    def test_one_tab_rules_out_the_whole_line_once(self):
        board = notched_board()
        proposals = score_proposals(
            self.candidates(board), [], tabs_of(board)
        )
        # One rejection for one line, not one per span on it.
        refused = [entry for entry in proposals if not entry.proposable]
        self.assertEqual(len(refused), 1)
        self.assertEqual(len(refused[0].candidate.spans), 2)
        self.assertIn("passes through 1 tab", refused[0].summary)

    def test_the_stage_reports_the_line_count_it_proposed(self):
        made = build(notched_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(len(made.candidates), 4)
        self.assertEqual(len(made.proposals), 4)
        # NOTHING is scored: the board carries no `User.38` line, and under the
        # decision model that is the designer declining every one of them.
        self.assertEqual(sum(1 for entry in made.proposals if entry.accepted), 0)
        self.assertEqual(
            sum(1 for entry in made.proposals if entry.proposable), 3
        )


class CandidateIdentityTest(unittest.TestCase):
    """Every candidate has a name, and the name survives the file changing."""

    def ids(self, board: Board) -> set:
        chain = weld_chain(chain_loop(outline_primitives(board)))
        return {candidate_id(entry) for entry in score_candidates(chain)}

    def test_every_candidate_gets_one_and_they_are_all_different(self):
        board = notched_board()
        ids = self.ids(board)
        self.assertEqual(len(ids), 4)
        for value in ids:
            self.assertEqual(len(value), 36)

    def test_the_same_board_names_them_the_same_way_twice(self):
        self.assertEqual(self.ids(notched_board()), self.ids(notched_board()))

    def test_reordering_the_outline_items_renames_nothing(self):
        # A save can write the drawings in another order. The id is derived from
        # the SORTED uuids of the items the candidate's spans are made of, so
        # every candidate keeps its name.
        board = notched_board()
        before = self.ids(board)
        shuffled = notched_board()
        drawings = shuffled.drawings
        shuffled.drawings = drawings[5:] + list(reversed(drawings[:5]))
        self.assertNotEqual(
            [item.id for item in shuffled.drawings],
            [item.id for item in board.drawings],
        )
        self.assertEqual(self.ids(shuffled), before)

    def test_a_different_edge_gets_a_different_name(self):
        board = notched_board()
        moved = notched_board()
        moved.drawings[0] = edge(0.0, 0.0, 4.0, 0.0, uuid_value="renamed")
        self.assertNotEqual(self.ids(moved), self.ids(board))

    def test_the_verdict_table_carries_the_id_of_every_candidate(self):
        made = build(notched_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(
            {entry.id for entry in made.proposals},
            self.ids(notched_board()),
        )
        for entry in made.proposals:
            self.assertEqual(entry.short_id, entry.id[:8])
            self.assertIn(
                entry.verdict,
                (VERDICT_SCORED, VERDICT_PROPOSED, VERDICT_REJECTED),
            )
            self.assertTrue(entry.reason)

    def test_the_table_reads_in_board_order_not_verdict_order(self):
        made = build(notched_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        starts = [
            (round(entry.candidate.start[0], 6), round(entry.candidate.start[1], 6))
            for entry in made.proposals
        ]
        self.assertEqual(starts, sorted(starts))


class ProposabilityTest(unittest.TestCase):
    """What the TOOL will and will not put in front of the designer."""

    def test_a_tab_on_the_line_rules_the_edge_out(self):
        board = scoreable_board()
        proposals = score_proposals(
            candidates_of(board), [], tabs_of(board)
        )
        offered = [entry for entry in proposals if entry.proposable]
        refused = [entry for entry in proposals if not entry.proposable]
        self.assertEqual(len(offered), 1)
        self.assertEqual(len(refused), 3)
        self.assertAlmostEqual(offered[0].candidate.start[0], 0.0)
        self.assertAlmostEqual(offered[0].candidate.end[0], 0.0)
        for turned_down in refused:
            self.assertIn("tab", turned_down.reason)
            self.assertEqual(turned_down.verdict, VERDICT_REJECTED)

    def test_a_tab_anywhere_on_the_line_counts_not_only_on_the_span(self):
        # The blade cuts the whole line across the panel, so a tab beyond the
        # end of the drawn span still rules that line out. This is the corpus
        # board's bottom edge, where the span between two tabs is scoreable
        # geometry and an unscoreable cut.
        board = scoreable_board()
        board.drawings[0] = edge(0.0, 0.0, 3.0, 0.0, uuid_value="top-left")
        board.drawings.insert(1, edge(3.0, 0.0, 10.0, 0.0, uuid_value="top-right"))
        tabs = tabs_of(board)
        clear = [
            candidate for candidate in candidates_of(board)
            if not blocking_tabs(candidate, tabs)
        ]
        self.assertEqual([round(entry.start[0], 6) for entry in clear], [0.0])

    def test_a_candidate_nobody_accepted_is_PROPOSED_not_rejected(self):
        # The wording matters: the tool proposes and the designer declines. This
        # candidate has nothing wrong with it.
        board = scoreable_board()
        proposals = score_proposals(candidates_of(board), [], tabs_of(board))
        offered = [entry for entry in proposals if entry.proposable][0]
        self.assertFalse(offered.accepted)
        self.assertEqual(offered.verdict, VERDICT_PROPOSED)
        self.assertNotIn("reject", offered.reason.lower())


class DecisionReadingTest(unittest.TestCase):
    """`User.38` at board level is the decision, and it is READ."""

    def test_every_board_level_line_is_read(self):
        board = scoreable_board()
        board.drawings.append(PcbLine(
            start=(0.0, 0.0), end=(0.0, 10.0), layer=SCORE_LAYER, id="one",
        ))
        board.trailing_drawings.append(PcbLine(
            start=(1.0, 0.0), end=(1.0, 10.0), layer=SCORE_LAYER, id="two",
        ))
        warnings = []
        read = board_scores(board, warnings)
        self.assertEqual([line.id for line in read], ["one", "two"])
        self.assertEqual(warnings, [])

    def test_a_footprint_hosted_score_item_is_not_a_decision(self):
        # It belongs to the footprint library, not to the designer's decision
        # about this board - and prep leaves it exactly where it is.
        board = scoreable_board()
        board.footprints[0].drawings.append(PcbLine(
            start=(0.0, 0.0), end=(1.0, 0.0), layer=SCORE_LAYER, id="fp-score",
        ))
        self.assertEqual(board_scores(board, []), [])
        build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertIn(
            "fp-score", [item.id for item in board.footprints[0].drawings]
        )

    def test_something_that_is_not_a_line_is_named_and_ignored(self):
        board = scoreable_board()
        board.drawings.append(PcbArc(
            start=(0.0, 0.0), mid=(1.0, 1.0), end=(2.0, 0.0),
            layer=SCORE_LAYER, id="curved",
        ))
        warnings = []
        self.assertEqual(board_scores(board, warnings), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("curved", warnings[0])
        self.assertIn("not a straight line", warnings[0])

    def test_a_line_with_no_length_names_no_blade_line(self):
        board = scoreable_board()
        board.drawings.append(PcbLine(
            start=(3.0, 3.0), end=(3.0, 3.0), layer=SCORE_LAYER, id="dot",
        ))
        warnings = []
        self.assertEqual(board_scores(board, warnings), [])
        self.assertIn("no length", warnings[0])


class ValidationTest(unittest.TestCase):
    """Matching a drawn decision to a candidate, by GEOMETRY and nothing else."""

    def setUp(self):
        self.board = scoreable_board()
        self.candidates = candidates_of(self.board)
        self.left = [
            entry for entry in self.candidates
            if abs(entry.start[0]) < 1e-9 and abs(entry.end[0]) < 1e-9
        ][0]

    def validate(self, *lines):
        warnings = []
        scores, stale = validate_scores(self.candidates, list(lines), warnings)
        return scores, stale, warnings

    def test_a_line_on_the_blade_line_scores_that_candidate(self):
        scores, stale, warnings = self.validate(
            ScoreLine(start=(0.0, 10.0), end=(0.0, 0.0), id="decision")
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].id, candidate_id(self.left))
        self.assertEqual([line.id for line in scores[0].lines], ["decision"])
        self.assertEqual(stale, [])
        self.assertEqual(warnings, [])

    def test_a_uuid_nobody_recognises_changes_nothing(self):
        # Identity is WHERE the line is. Operations clobber uuids and a uuid
        # says nothing about where a cut goes.
        first, _, _ = self.validate(
            ScoreLine(start=(0.0, 10.0), end=(0.0, 0.0), id="one")
        )
        second, _, _ = self.validate(
            ScoreLine(start=(0.0, 10.0), end=(0.0, 0.0), id="quite-different")
        )
        self.assertEqual(first[0].id, second[0].id)

    def test_a_short_line_on_the_blade_line_still_accepts_it(self):
        # EXTENT-TOLERANT. A designer may draw the score across two millimetres
        # of a ten-millimetre edge; it is still that cut.
        scores, stale, _ = self.validate(
            ScoreLine(start=(0.0, 4.0), end=(0.0, 6.0), id="short")
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(stale, [])

    def test_a_line_produced_past_the_board_still_accepts_it(self):
        # ...and past the other way, which is what a panel-level score looks
        # like once the frame is around it.
        scores, stale, _ = self.validate(
            ScoreLine(start=(0.0, -50.0), end=(0.0, 60.0), id="long")
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(stale, [])

    def test_a_line_beside_the_blade_line_matches_nothing(self):
        scores, stale, warnings = self.validate(
            ScoreLine(start=(0.5, 10.0), end=(0.5, 0.0), id="off")
        )
        self.assertEqual(scores, [])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].line.id, "off")
        self.assertEqual(len(warnings), 1)
        self.assertIn("STALE SCORE", warnings[0])
        self.assertIn("no longer lies on a scoreable edge", warnings[0])
        self.assertIn("0.5000", warnings[0])

    def test_a_line_at_an_angle_to_the_blade_line_matches_nothing(self):
        scores, stale, _ = self.validate(
            ScoreLine(start=(0.0, 10.0), end=(1.0, 0.0), id="skew")
        )
        self.assertEqual(scores, [])
        self.assertEqual(len(stale), 1)

    def test_two_lines_on_one_blade_line_score_it_once(self):
        scores, stale, _ = self.validate(
            ScoreLine(start=(0.0, 10.0), end=(0.0, 6.0), id="upper"),
            ScoreLine(start=(0.0, 4.0), end=(0.0, 0.0), id="lower"),
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(
            [line.id for line in scores[0].lines], ["upper", "lower"]
        )

    def test_the_same_board_validates_the_same_way_twice(self):
        line = ScoreLine(start=(0.0, 10.0), end=(0.0, 0.0), id="decision")
        first, _, _ = self.validate(line)
        second, _, _ = self.validate(line)
        self.assertEqual(
            [score.id for score in first], [score.id for score in second]
        )


class StaleDecisionTest(unittest.TestCase):
    """The outline moved, so the decision made about it stopped meaning anything.

    A decision stops matching only when the shape it was made about actually
    changed, and then a human decides again. A stale line that LOOKS like a
    score does not suppress routing on an edge that has moved.
    """

    def moved_board(self) -> Board:
        """`scoreable_board`, with its left edge pushed 0.5 mm inward.

        The score the designer drew at `x = 0` is now over free air, and the
        edge it was about is somewhere else.
        """
        board = scoreable_board()
        board.footprints = []
        board.drawings = [
            edge(0.0, 0.0, 10.0, 0.0),
            edge(10.0, 0.0, 10.0, 10.0),
            edge(10.0, 10.0, 0.5, 10.0),
            edge(0.5, 10.0, 0.5, 0.0),
            edge(0.5, 0.0, 0.0, 0.0),
        ]
        board.drawings.append(PcbLine(
            start=(0.0, 10.0), end=(0.0, 0.0), layer=SCORE_LAYER,
            stroke=Stroke(width=SCORE_LINE_WIDTH_MM), id="yesterdays-decision",
        ))
        return board

    def test_the_run_says_the_score_is_stale_and_names_where(self):
        made = build(self.moved_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(len(made.stale_scores), 1)
        self.assertEqual(made.stale_scores[0].line.id, "yesterdays-decision")
        self.assertTrue(any(
            "STALE SCORE" in warning and "0.0000" in warning
            for warning in made.warnings
        ))

    def test_a_stale_score_suppresses_no_routing(self):
        made = build(self.moved_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(made.scores, [])
        self.assertEqual(made.junctions, [])

    def test_the_stale_line_is_left_on_the_board_for_the_designer(self):
        # Prep does not tidy it away. It is not prep's line to delete, and a
        # decision that silently vanished is worse than one that is flagged.
        board = self.moved_board()
        build(board, CUTTER, VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(
            [item.id for item in items_on(board, SCORE_LAYER)],
            ["yesterdays-decision"],
        )


class NoWriteTest(unittest.TestCase):
    """THE INVARIANT: prep never writes `User.38`. It is the designer's layer."""

    def test_a_build_leaves_the_score_layer_exactly_as_it_found_it(self):
        board = accept(scoreable_board(), x=0.0)
        before = score_signature(board)
        build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(score_signature(board), before)

    def test_two_builds_leave_it_alone_twice(self):
        board = accept(scoreable_board(), x=0.0)
        before = score_signature(board)
        build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(score_signature(board), before)

    def test_a_board_with_no_score_gets_none_written_to_it(self):
        board = scoreable_board()
        build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(items_on(board, SCORE_LAYER), [])

    def test_a_score_line_the_stage_disagrees_with_is_still_not_touched(self):
        # A line lying on a candidate whose blade line carries tabs: the stage
        # warns loudly and honours it. The designer's authority is the model.
        board = scoreable_board()
        top = [
            entry for entry in candidates_of(board)
            if abs(entry.start[1]) < 1e-9 and abs(entry.end[1]) < 1e-9
        ][0]
        board.drawings.append(score_line_for(top, outline_bbox(board)))
        before = score_signature(board)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(score_signature(board), before)
        self.assertEqual(len(made.scores), 1)
        self.assertTrue(any(
            "passes through" in warning for warning in made.warnings
        ))

    def test_the_stage_still_owns_the_toolpath_layer(self):
        # `User.36` is prep's, and the ownership rule for it has not moved.
        board = accept(scoreable_board(), x=0.0)
        board.drawings.append(PcbLine(
            start=(1.0, 1.0), end=(2.0, 2.0), layer=TOOLPATH_LAYER, id="stale",
        ))
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(made.removed, 1)
        self.assertNotIn(
            "stale", [item.id for item in items_on(board, TOOLPATH_LAYER)]
        )


class AcceptSurfaceTest(unittest.TestCase):
    """What ACCEPTING a candidate writes. Prep never calls it while prepping."""

    def test_the_line_reaches_the_outline_bounding_box(self):
        # A score line spans the board's extents, not the
        # stretch of edge that touches it. The filleted board is the case - its
        # right edge is 6 mm of outline on a 10 mm blade line.
        chain = chain_loop(filleted_chain())
        bbox = chain_bbox(chain)
        right = [
            entry for entry in score_candidates(chain)
            if abs(entry.start[0] - 20.0) < 1e-9
        ][0]
        self.assertAlmostEqual(right.length_mm, 6.0, places=9)
        line = score_line_for(right, bbox)
        self.assertAlmostEqual(line.start[0], 20.0, places=9)
        self.assertAlmostEqual(line.end[0], 20.0, places=9)
        self.assertAlmostEqual(
            min(line.start[1], line.end[1]), 0.0, places=9
        )
        self.assertAlmostEqual(
            max(line.start[1], line.end[1]), 10.0, places=9
        )

    def test_a_broken_edge_writes_ONE_line_across_the_gaps(self):
        # SUPERSEDES the one-line-per-span rule. The blade crosses the notch,
        # and the line that stands for the cut says where the cut goes.
        chain = chain_loop(notched_edge_chain())
        candidate = [
            entry for entry in score_candidates(chain) if len(entry.spans) > 1
        ][0]
        line = score_line_for(candidate, chain_bbox(chain))
        self.assertAlmostEqual(
            math.dist(line.start, line.end), 20.0, places=9
        )

    def test_the_line_is_drawn_thin_on_the_score_layer(self):
        chain = chain_loop(square_chain())
        line = score_line_for(score_candidates(chain)[0], chain_bbox(chain))
        self.assertEqual(line.layer, SCORE_LAYER)
        self.assertAlmostEqual(line.stroke.width, SCORE_LINE_WIDTH_MM)

    def test_the_identity_comes_from_the_outline_item(self):
        # Same source items, same uuid, on any machine and in any run - which is
        # what lets a re-accept land on byte-identical bytes.
        chain = chain_loop(square_chain())
        bbox = chain_bbox(chain)
        candidate = score_candidates(chain)[0]
        self.assertEqual(
            score_line_for(candidate, bbox).id,
            score_line_for(candidate, bbox).id,
        )
        elsewhere = candidate._replace(sources=("elsewhere",))
        self.assertNotEqual(
            score_line_for(candidate, bbox).id,
            score_line_for(elsewhere, bbox).id,
        )

    def test_the_score_line_keeps_its_name_when_the_file_is_reordered(self):
        # The uuid is derived from the SORTED source uuids, so a board saved
        # with its outline items in another order writes the same score line.
        chain = chain_loop(square_chain())
        bbox = chain_bbox(chain)
        candidate = score_candidates(chain)[0]
        shuffled = candidate._replace(
            sources=tuple(reversed(candidate.sources))
        )
        self.assertEqual(
            score_line_for(candidate, bbox).id,
            score_line_for(shuffled, bbox).id,
        )

    def test_what_it_writes_validates_back_to_the_candidate_it_came_from(self):
        # The round trip the whole model rests on: accepting writes a line, and
        # the next prep run reads that line back as the same decision.
        board = accept(scoreable_board(), x=0.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(len(made.scores), 1)
        self.assertEqual(made.stale_scores, [])
        self.assertAlmostEqual(made.scores[0].start[0], 0.0)

    def test_the_score_layer_is_replicated_into_the_panel(self):
        # A prepped board has to panelize WITH its scores, so the layer must
        # stay out of the drawing-scale exclusion set.
        self.assertNotIn(SCORE_LAYER, DEFAULT_EXCLUDED_LAYERS)


class BladeGeometryTest(unittest.TestCase):
    """The two pure functions the decision model is built out of."""

    def candidate(self):
        return score_candidates(chain_loop(square_chain()))[0]

    def test_a_point_on_the_line_is_on_the_blade_however_far_along(self):
        candidate = self.candidate()
        for along in (-100.0, 0.0, 0.5, 1000.0):
            point = (candidate.start[0] + candidate.unit[0] * along,
                     candidate.start[1] + candidate.unit[1] * along)
            self.assertTrue(on_blade(candidate, point))

    def test_a_point_beside_the_line_is_not(self):
        candidate = self.candidate()
        beside = (candidate.start[0] + candidate.normal[0] * 0.5,
                  candidate.start[1] + candidate.normal[1] * 0.5)
        self.assertFalse(on_blade(candidate, beside))

    def test_a_micrometre_off_is_still_on_it(self):
        # A real file's straight edge is not exactly straight, and the same
        # allowance that merges collinear items has to accept a drawn score.
        candidate = self.candidate()
        drifted = (candidate.start[0] + candidate.normal[0] * 1e-6,
                   candidate.start[1] + candidate.normal[1] * 1e-6)
        self.assertTrue(on_blade(candidate, drifted))

    def test_the_extent_is_the_box_and_the_direction_is_the_candidates(self):
        chain = chain_loop(square_chain())
        candidate = score_candidates(chain)[0]
        start, end = blade_extent(candidate, (-5.0, -5.0, 15.0, 15.0))
        # Travelled the way the candidate travels, from one wall to the other.
        direction = (end[0] - start[0], end[1] - start[1])
        self.assertGreater(
            direction[0] * candidate.unit[0] + direction[1] * candidate.unit[1],
            0.0,
        )
        self.assertAlmostEqual(math.dist(start, end), 20.0, places=9)

    def test_a_drawn_line_matches_when_both_its_ends_are_on_the_blade(self):
        candidate = self.candidate()
        along = candidate.unit
        start = (candidate.start[0] - along[0] * 3.0,
                 candidate.start[1] - along[1] * 3.0)
        end = (candidate.start[0] + along[0] * 3.0,
               candidate.start[1] + along[1] * 3.0)
        self.assertTrue(line_on_blade(candidate, ScoreLine(start=start, end=end)))
        moved = (end[0] + candidate.normal[0] * 0.5,
                 end[1] + candidate.normal[1] * 0.5)
        self.assertFalse(line_on_blade(candidate, ScoreLine(start=start, end=moved)))


class SuppressionTest(unittest.TestCase):
    """A scored edge is a route suppressor: the router never travels it."""

    def test_the_scored_edge_leaves_the_toolpath(self):
        board = accept(scoreable_board(), x=0.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(len(made.scores), 1)
        self.assertEqual(made.scores[0].dropped, 1)
        self.assertAlmostEqual(made.scores[0].dropped_mm, 10.0, places=6)
        # Nothing is left running DOWN the offset of the scored edge - the two
        # run ends reach past it, but nothing travels along it.
        for item in items_on(board, TOOLPATH_LAYER):
            if type(item).__name__ != "PcbLine":
                continue
            along = (
                abs(item.start[0] + RADIUS) < 1e-6
                and abs(item.end[0] + RADIUS) < 1e-6
            )
            self.assertFalse(along, "a run still travels the scored edge")

    def test_declining_the_candidate_leaves_the_edge_routed(self):
        # The other half of the decision: the same board with no `User.38` line
        # on it is routed all the way round, and that is not a defect.
        board = scoreable_board()
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(made.scores, [])
        self.assertEqual(made.junctions, [])
        travelled = [
            item for item in items_on(board, TOOLPATH_LAYER)
            if type(item).__name__ == "PcbLine"
            and abs(item.start[0] + RADIUS) < 1e-6
            and abs(item.end[0] + RADIUS) < 1e-6
        ]
        self.assertTrue(travelled, "the declined edge should be routed")

    def test_the_tabs_are_untouched_by_scoring(self):
        scored = build(accept(scoreable_board(), x=0.0), CUTTER,
                       VScoreConfig(route_past_score_mm=1.0))
        plain = build(scoreable_board(), CUTTER, VScoreConfig(detect="none"))
        self.assertEqual(len(scored.stops), len(plain.stops))
        self.assertEqual(
            [round(bridge.material_mm, 6) for bridge in scored.bridges],
            [round(bridge.material_mm, 6) for bridge in plain.bridges],
        )
        self.assertTrue(scored.rout_through == [] == plain.rout_through)

    def test_scoring_splits_the_run_that_travelled_the_edge(self):
        scored = build(accept(scoreable_board(), x=0.0), CUTTER,
                       VScoreConfig(route_past_score_mm=1.0))
        plain = build(scoreable_board(), CUTTER, VScoreConfig(detect="none"))
        self.assertEqual(len(scored.run_lengths), len(plain.run_lengths) + 1)

    def test_a_board_scored_all_round_is_routed_nowhere(self):
        # Every side of a bare square is scoreable, so a designer who accepts
        # all four has asked for a board the saw separates entirely. What is
        # left is four score-by-score corners, and a corner like that is no
        # cutter's business: the toolpath is empty.
        board = scoreable_board()
        board.footprints = []
        made = build(accept_every_candidate(board), CUTTER,
                     VScoreConfig(route_past_score_mm=0.5))
        self.assertEqual(len(made.scores), 4)
        self.assertEqual(list(made.run_lengths), [])
        self.assertEqual(list(made.junctions), [])
        self.assertEqual(items_on(board, TOOLPATH_LAYER), [])


class JunctionTest(unittest.TestCase):
    """Where a run meets a score plane, the TANGENT POINT is what stops there.

    `route_past_score_mm` measures the cutter's tangent
    point - the last of the board edge taken to full depth - so the CENTRELINE
    end sits that far past the plane and the round end sweeps one radius
    further, into air. Measuring that outer extreme instead would put the
    tangent point one radius short of where it belongs.
    """

    def ends_near_the_score(self, route_past: float) -> list:
        board = accept(scoreable_board(), x=0.0)
        build(board, CUTTER, VScoreConfig(route_past_score_mm=route_past))
        return past_the_plane(board)

    def test_the_run_is_produced_until_the_cut_reaches_past_the_plane(self):
        # The score plane is x = 0 and the board is at x > 0, so a centre - and
        # with it the tangent point square across the run - sits at -route_past.
        ends = self.ends_near_the_score(1.0)
        self.assertEqual(len(ends), 2)
        for end in ends:
            self.assertAlmostEqual(end, -1.0, places=9)

    def test_a_short_overshoot_lands_in_the_same_place(self):
        # A fifth of a millimetre is less than the corner arc's own reach. The
        # arc's contact point is the corner VERTEX and never moves, so
        # trimming the arc gives nothing: the straight edge is produced to
        # 0.2.
        ends = self.ends_near_the_score(0.2)
        self.assertEqual(len(ends), 2)
        for end in ends:
            self.assertAlmostEqual(end, -0.2, places=9)

    def test_the_end_does_not_move_with_the_cutter(self):
        # The correction in one assertion: where the run stops is the fab's
        # declared distance and the plane, and nothing else. Under the rule this
        # replaced every one of these ends sat one radius further in, so all
        # four answers differed.
        for cutter in (0.4, CUTTER, 1.5, 2.0):
            board = accept(scoreable_board(), x=0.0)
            board.footprints = []
            build(board, cutter, VScoreConfig(route_past_score_mm=1.0))
            ends = past_the_plane(board)
            with self.subTest(cutter=cutter):
                self.assertEqual(len(ends), 2)
                for end in ends:
                    self.assertAlmostEqual(end, -1.0, places=9)

    def test_the_junction_records_what_it_did(self):
        board = accept(scoreable_board(), x=0.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(len(made.junctions), 2)
        for junction in made.junctions:
            self.assertTrue(junction.reached)
            self.assertEqual(junction.action, "extended")
            self.assertAlmostEqual(junction.past_mm, 1.0, places=9)
            self.assertAlmostEqual(junction.wanted_mm, 1.0)
            self.assertGreater(junction.change_mm, 0.0)

    def test_a_produced_junction_says_so_however_short_the_overshoot(self):
        # Any overshoot at all is produced from the straight edge, because the
        # corner arc between edge and score takes no board edge to depth.
        for route_past in (0.05, 0.2, 1.0):
            made = build(accept(scoreable_board(), x=0.0), CUTTER,
                         VScoreConfig(route_past_score_mm=route_past))
            with self.subTest(route_past=route_past):
                self.assertEqual(
                    {junction.action for junction in made.junctions},
                    {"extended"},
                )

    def test_a_trimmed_junction_says_so(self):
        # Trimming is what happens where the board edge itself carries the cut
        # past the depth asked for: a corner fillet tangent to the scored edge
        # reaches the plane on its own, so asking for nothing cuts the fillet
        # back to exactly there.
        made = build(accept(filleted_board(), x=20.0), CUTTER,
                     VScoreConfig(route_past_score_mm=0.0))
        self.assertEqual(
            {junction.action for junction in made.junctions}, {"trimmed"}
        )
        for junction in made.junctions:
            self.assertTrue(junction.reached)
            self.assertAlmostEqual(junction.past_mm, 0.0, delta=1e-3)

    def test_no_declared_overshoot_is_an_error_naming_the_junction(self):
        with self.assertRaises(ValueError) as raised:
            build(accept(scoreable_board(), x=0.0), CUTTER, VScoreConfig())
        message = str(raised.exception)
        self.assertIn("route_past_score_mm", message)
        self.assertIn("v-score", message)
        self.assertIn("mm", message)

    def test_a_board_that_accepted_nothing_needs_no_declaration(self):
        # The ordinary board: candidates proposed, none accepted, no junction
        # and so nothing for the fab to declare.
        made = build(scoreable_board(), CUTTER, VScoreConfig())
        self.assertEqual(made.scores, [])
        self.assertEqual(made.junctions, [])
        self.assertEqual(len(made.candidates), 4)

    def test_the_caller_that_asks_for_nothing_gets_the_old_behaviour(self):
        # `build(board, cutter)` predates scores and still means "route it all".
        # Under the new `detect` semantics that also skips the PROPOSER: nothing
        # is computed, so there is nothing to report either.
        made = build(accept(scoreable_board(), x=0.0), CUTTER)
        self.assertEqual(made.scores, [])
        self.assertEqual(list(made.candidates), [])
        self.assertEqual(list(made.proposals), [])

    def test_detect_none_says_out_loud_that_it_is_ignoring_a_decision(self):
        # Silently routing over an edge the designer scored is the one thing
        # `"none"` must not do quietly.
        made = build(accept(scoreable_board(), x=0.0), CUTTER,
                     VScoreConfig(detect="none"))
        self.assertEqual(made.scores, [])
        self.assertEqual(len(made.score_lines), 1)
        self.assertTrue(any(
            "detect is 'none'" in warning for warning in made.warnings
        ))


class ScoreCornerTest(unittest.TestCase):
    """Two scored edges meeting: the corner is the SAW's, and no cutter goes there.

    The offset walks a corner arc round the vertex between the two dropped
    spans, and neither end can produce it to the depth asked for. Kept, it
    shows as a short stub of toolpath where two accepted v-scores meet.
    `route_past_score_mm` applies where a score meets a ROUTED
    edge and nowhere else; both these edges separate under the blade, so the
    corner emits nothing at any value.
    """

    SWEEP = (0.0, 0.5, 1.0, 2.0)

    def cornered_board(self) -> Board:
        """A bare square with the left AND bottom edges scored - one corner."""
        board = scoreable_board()
        board.footprints = []
        accept(board, x=0.0)
        accept(board, y=0.0)
        return board

    def items_within(self, board, at: tuple, reach: float) -> list:
        return [
            item for item in items_on(board, TOOLPATH_LAYER)
            if any(
                math.dist(point, at) <= reach
                for point in (item.start, item.end)
            )
        ]

    def test_nothing_is_cut_at_the_corner_at_any_overshoot(self):
        for route_past in self.SWEEP:
            board = self.cornered_board()
            made = build(board, CUTTER,
                         VScoreConfig(route_past_score_mm=route_past))
            with self.subTest(route_past=route_past):
                self.assertEqual(len(made.scores), 2)
                # The corner of the two scored edges, and everything the bit
                # would have to be within to leave a stub there.
                self.assertEqual(self.items_within(board, (0.0, 0.0), 3.0), [])

    def test_the_corner_is_not_counted_as_a_junction(self):
        # A junction is a place a routed run had to be finished. Nothing is
        # routed at this corner, so there is nothing to record and nothing for
        # the check page to call short.
        for route_past in self.SWEEP:
            made = build(self.cornered_board(), CUTTER,
                         VScoreConfig(route_past_score_mm=route_past))
            with self.subTest(route_past=route_past):
                self.assertEqual(len(made.junctions), 2)
                self.assertTrue(all(
                    junction.reached for junction in made.junctions
                ))
                for junction in made.junctions:
                    self.assertGreater(math.dist(junction.at, (0.0, 0.0)), 3.0)

    def test_the_other_end_of_each_score_still_gets_its_overshoot(self):
        # The same board proves the rule is about the CORNER and not about the
        # scores: where each of these two scores meets a routed edge, the cut is
        # still produced the declared distance past the plane.
        made = build(self.cornered_board(), CUTTER,
                     VScoreConfig(route_past_score_mm=0.75))
        self.assertEqual(len(made.junctions), 2)
        for junction in made.junctions:
            self.assertTrue(junction.reached)
            self.assertAlmostEqual(junction.past_mm, 0.75, places=9)

    def test_a_corner_with_no_routed_edge_needs_no_declaration(self):
        # `route_past_score_mm` is the fab's number and an undeclared one is an
        # error - but only where something is actually routed into a score. A
        # board the saw separates entirely has no such place.
        board = scoreable_board()
        board.footprints = []
        made = build(accept_every_candidate(board), CUTTER, VScoreConfig())
        self.assertEqual(len(made.scores), 4)
        self.assertEqual(list(made.junctions), [])
        self.assertEqual(items_on(board, TOOLPATH_LAYER), [])

    def test_a_chamfer_between_two_scores_is_still_routed(self):
        # The counter-case, and the reason the rule reads "no piece from any
        # outline item" rather than "two scores meet here": a chamfer across the
        # corner is board edge, no saw takes it, and it is cut with a junction
        # at each end.
        board = scoreable_board()
        board.footprints = []
        board.drawings = [
            edge(1.0, 0.0, 10.0, 0.0, uuid_value="bottom"),
            edge(10.0, 0.0, 10.0, 10.0, uuid_value="right"),
            edge(10.0, 10.0, 0.0, 10.0, uuid_value="top"),
            edge(0.0, 10.0, 0.0, 1.0, uuid_value="left"),
            edge(0.0, 1.0, 1.0, 0.0, uuid_value="chamfer"),
        ]
        accept(board, x=0.0)
        accept(board, y=0.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=0.3))
        self.assertEqual(len(made.scores), 2)
        cut = [
            item for item in items_on(board, TOOLPATH_LAYER)
            if any(
                math.dist(point, (0.5, 0.5)) <= 2.0
                for point in (item.start, item.end)
            )
        ]
        self.assertTrue(cut, "the chamfer between the two scores is not cut")
        self.assertEqual(len(made.junctions), 4)


class JunctionOracleTest(unittest.TestCase):
    """The junction judged by the CUT, not by the record that made it.

    Everything else about a junction is read off the stage's own arithmetic.
    This asks the emitted geometry a machinist's question instead: sweep the bit
    down the emitted centreline and find how far along the board edge the swept
    cut severs the board from what is outside it. Where that severing stops is
    where the cut leaves the board edge - the tangent point - and it sits
    `route_past_score_mm` PAST the score plane.

    The probe is a point one picometre outside the board edge: material there is
    the last of the board's own edge, and it is cut exactly when the swept
    region covers it. Bisecting on that predicate locates the tangent point to
    about a nanometre - the offset is quadratic in the distance past the run's
    end - which is three orders finer than anything the file records and six
    finer than anything a mill holds. It also puts the probe on the right SIDE
    of the geometry, which a bare distance-to-the-path test does not: a round
    end sweeping past a corner leaves points on the far side of its own arc at
    exactly the bit's radius, and none of those is a cut board edge.
    """

    # How far outside the board edge the probe sits, and what the bisection
    # therefore resolves the tangent point to: sqrt(2 * r * 1e-12) is about a
    # nanometre at any of these radii.
    PROBE_MM = 1e-12
    PLACE_MM = 5e-6

    def cut_path(self, board) -> list:
        """The emitted toolpath as offsettable primitives - the cutter's centre."""
        path = []
        for item in items_on(board, TOOLPATH_LAYER):
            if type(item).__name__ == "PcbLine":
                path.append(Line(p0=item.start, p1=item.end, source="cut"))
            else:
                path.append(arc_from_three_points(
                    item.start, item.mid, item.end, source="cut"
                ))
        return path

    def severed(self, path: list, x: float, edge_y: float, outward: float,
                radius: float) -> bool:
        """Is the board edge at *x* cut through - the material just outside it gone?"""
        point = (x, edge_y + outward * self.PROBE_MM)
        return nearest_on_path(point, path)[1] < radius

    def cut_reaches_to(self, path: list, edge_y: float, outward: float,
                       radius: float, cut_x: float, clear_x: float) -> float:
        """How far along the edge `y = edge_y` the cut severs the board.

        *cut_x* is a station on that edge the cut certainly takes and *clear_x*
        one beyond its end that it certainly does not, so the answer is
        bracketed by the caller and found by halving. Returned in x, which on
        these fixtures is signed distance past the score plane at x = 0.
        """
        low, high = clear_x, cut_x
        self.assertTrue(self.severed(path, high, edge_y, outward, radius))
        self.assertFalse(self.severed(path, low, edge_y, outward, radius))
        for _ in range(200):
            middle = (low + high) / 2.0
            if self.severed(path, middle, edge_y, outward, radius):
                high = middle
            else:
                low = middle
        return high

    def scored_board(self, cutter: float, route_past: float, tabs: bool = True):
        """The square fixture with its left edge scored, prepared and measured."""
        board = accept(scoreable_board(), x=0.0)
        if not tabs:
            board.footprints = []
        build(board, cutter, VScoreConfig(route_past_score_mm=route_past))
        return board

    def test_the_cut_leaves_the_board_edge_exactly_where_it_was_told_to(self):
        # The whole rule, measured: the cut severs the bottom edge to 1 mm past
        # the score plane at x = 0, and stops there, to a nanometre.
        board = self.scored_board(CUTTER, 1.0, tabs=False)
        path = self.cut_path(board)
        for edge_y, outward in ((0.0, -1.0), (10.0, 1.0)):
            with self.subTest(edge=edge_y):
                at = self.cut_reaches_to(path, edge_y, outward, RADIUS, 5.0, -3.0)
                self.assertAlmostEqual(at, -1.0, delta=self.PLACE_MM)

    def test_it_stays_put_as_the_cutter_grows(self):
        # The tangent point does not move with the bit. Placed at
        # `radius - route_past`, it would walk in with the bit - a millimetre
        # of bit moving it half a millimetre - and cross back over the plane
        # as soon as the radius reached the overshoot.
        for cutter in (0.4, CUTTER, 1.5):
            board = self.scored_board(cutter, 1.0, tabs=False)
            at = self.cut_reaches_to(
                self.cut_path(board), 0.0, -1.0, cutter / 2.0, 5.0, -3.0
            )
            with self.subTest(cutter=cutter):
                self.assertAlmostEqual(at, -1.0, delta=self.PLACE_MM)

    def test_a_cutter_wider_than_the_overshoot_leaves_no_edge_standing(self):
        # r >= route_past: a centre stopped `radius - route_past` INSIDE the
        # plane would leave that much of the board's own edge unsevered and
        # the board joined to what is outside it. Whatever bit is fitted, the
        # cut reaches the plane at worst.
        for cutter, route_past in ((2.0, 0.5), (3.175, 0.5), (1.0, 0.2)):
            board = self.scored_board(cutter, route_past, tabs=False)
            path = self.cut_path(board)
            radius = cutter / 2.0
            with self.subTest(cutter=cutter):
                # Every station of the board's own edge from the plane inward is
                # severed - the old rule left the first `radius - route_past` of
                # it standing.
                for x in (1e-6, 0.001, 0.05, radius, 1.0):
                    self.assertTrue(
                        self.severed(path, x, 0.0, -1.0, radius),
                        f"board edge standing at x = {x} between route and score",
                    )

    def test_the_centreline_ends_past_the_plane_at_any_cutter(self):
        # The same case read off the record rather than the geometry: the end
        # is PAST the plane (negative x here) for every bit, where the old rule
        # put it inside the plane for every bit wider than 1 mm.
        for cutter in (0.4, CUTTER, 2.0, 3.175):
            board = accept(scoreable_board(), x=0.0)
            board.footprints = []
            made = build(board, cutter, VScoreConfig(route_past_score_mm=0.5))
            with self.subTest(cutter=cutter):
                self.assertEqual(len(made.junctions), 2)
                for junction in made.junctions:
                    self.assertTrue(junction.reached)
                    self.assertAlmostEqual(junction.at[0], -0.5, places=9)
                    self.assertAlmostEqual(junction.past_mm, 0.5, places=9)

    # -- the blind spot, named ---------------------------------------------
    #
    # Every case above probes a STRAIGHT edge driving square at the plane, and
    # on that geometry the centreline and the contact point are the same
    # measurement, so none of it can see a gap at a fillet. Where a run arrives
    # round a corner FILLET the contact point lags the centreline by up to a
    # radius, so the probe has to walk the board's own outline instead of one
    # straight line along it.

    def standing_edge(self, board, cutter: float, plane_x: float,
                      reach: float = 1.0, step: float = 5e-3) -> list:
        """Stations of the board's own edge near the plane the cut did not sever.

        Walks the whole outline the stage routed, keeps the stations between the
        plane and *reach* millimetres inside it, and probes a picometre outside
        each one. What comes back is board edge left standing between the route
        and the score - the thing the user can see and the junction record
        could not.
        """
        radius = cutter / 2.0
        path = self.cut_path(board)
        chain = weld_chain(chain_loop(outline_primitives(board)))
        # These fixtures are convex about their own middle, so a step outwards
        # from the centre leaves the board wherever it is taken.
        low_x, low_y, high_x, high_y = chain_bbox(chain)
        middle = ((low_x + high_x) / 2.0, (low_y + high_y) / 2.0)
        standing = []
        for primitive in chain:
            steps = max(2, math.ceil(primitive.length / step))
            for index in range(steps + 1):
                along = index / steps
                if isinstance(primitive, Line):
                    point = (
                        primitive.p0[0]
                        + (primitive.p1[0] - primitive.p0[0]) * along,
                        primitive.p0[1]
                        + (primitive.p1[1] - primitive.p0[1]) * along,
                    )
                else:
                    sign = 1.0 if primitive.ccw else -1.0
                    point = primitive.point_at_angle(
                        primitive.a0 + sign * primitive.sweep * along
                    )
                inside = plane_x - point[0]
                if inside <= 1e-6 or inside > reach:
                    continue
                away = math.dist(point, middle)
                probe = (
                    point[0] + (point[0] - middle[0]) / away * self.PROBE_MM,
                    point[1] + (point[1] - middle[1]) / away * self.PROBE_MM,
                )
                if nearest_on_path(probe, path)[1] >= radius:
                    standing.append((point, inside))
        return standing

    def test_no_board_edge_stands_where_the_run_arrives_round_a_fillet(self):
        # The runs either side of this score arrive round a 2 mm fillet.
        # Trimming their CENTRELINES to the plane would leave the contact
        # point, and so the full-depth cut, 0.3636 mm short of it at the corpus
        # cutter, with 1.22 mm of board edge standing at each corner.
        for cutter in (0.4, CUTTER, 1.5):
            for route_past in (0.0, 0.2, 0.5):
                board = accept(filleted_board(), x=20.0)
                build(board, cutter,
                      VScoreConfig(route_past_score_mm=route_past))
                standing = self.standing_edge(board, cutter, 20.0)
                with self.subTest(cutter=cutter, route_past=route_past):
                    self.assertEqual(
                        standing, [],
                        f"{len(standing)} station(s) of board edge standing "
                        f"between route and score, the worst "
                        f"{max((entry[1] for entry in standing), default=0.0):.4f}"
                        f" mm from the plane",
                    )

    def test_the_record_names_the_tangent_point_and_not_the_centreline(self):
        # The junction record: `past_mm` is measured at the
        # CONTACT point, which on a fillet sits a radius back along the arc from
        # where the centre stops. Read off `at`, this junction would report
        # itself a radius past the plane while the cut was short of it.
        made = build(accept(filleted_board(), x=20.0), CUTTER,
                     VScoreConfig(route_past_score_mm=0.0))
        self.assertEqual(len(made.junctions), 2)
        for junction in made.junctions:
            self.assertIsNotNone(junction.contact)
            self.assertAlmostEqual(junction.contact[0], 20.0, delta=1e-3)
            self.assertAlmostEqual(junction.past_mm, 0.0, delta=1e-3)
            # The centre is one radius further out, sweeping air.
            self.assertAlmostEqual(junction.at[0], 20.0 + RADIUS, delta=1e-3)
            self.assertAlmostEqual(
                math.dist(junction.at, junction.contact), RADIUS, delta=1e-3
            )


class RoutedScoreGuardTest(unittest.TestCase):
    """A cut running ALONG a scored edge is the failure this stage prevents."""

    def score_at_x(self, x: float = 0.0) -> Score:
        candidate = [
            entry for entry in score_candidates(chain_loop(square_chain()))
            if abs(entry.start[0] - x) < 1e-9 and abs(entry.end[0] - x) < 1e-9
        ][0]
        return Score(index=0, label="v-score @ test", candidate=candidate,
                     items=frozenset(candidate.items))

    def test_a_run_down_the_scored_edge_is_caught(self):
        score = self.score_at_x(0.0)
        run = [Line(p0=(-RADIUS, 0.5), p1=(-RADIUS, 9.5), source="rogue")]
        hits = routed_scores([run], [score], RADIUS)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].clearance_mm, RADIUS, places=9)
        self.assertEqual(hits[0].score, 0)

    def test_a_run_a_whole_diameter_off_is_not(self):
        score = self.score_at_x(0.0)
        run = [Line(p0=(-CUTTER * 2, 0.5), p1=(-CUTTER * 2, 9.5), source="clear")]
        self.assertEqual(routed_scores([run], [score], RADIUS), [])

    def test_the_run_ends_that_touch_the_score_ends_stay_silent(self):
        # The channel either side of a score is tangent to the edge the score
        # lies on. That contact is the design, and it dies away within a
        # fraction of a millimetre of the corner.
        board = accept(scoreable_board(), x=0.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertEqual(made.routed_scores, [])

    def test_the_real_board_warns_in_words(self):
        score = self.score_at_x(0.0)
        run = [Line(p0=(-RADIUS, 0.5), p1=(-RADIUS, 9.5), source="rogue")]
        hits = routed_scores([run], [score], RADIUS)
        self.assertIn("v-score", hits[0].label)


class SplitRunTest(unittest.TestCase):
    def test_a_run_with_no_scored_piece_comes_back_whole(self):
        run = [Line(p0=(0.0, 0.0), p1=(1.0, 0.0), source="a", origin=0)]
        self.assertEqual(split_run_at_scores(run, {}), [(run, None, None)])

    def test_a_closed_run_is_opened_at_its_scored_piece(self):
        candidate = score_candidates(chain_loop(square_chain()))[0]
        score = Score(index=0, label="s", candidate=candidate,
                      items=frozenset({7}))
        run = [
            Line(p0=(0.0, 0.0), p1=(1.0, 0.0), source="a", origin=7),
            Line(p0=(1.0, 0.0), p1=(1.0, 1.0), source="b", origin=1),
            Line(p0=(1.0, 1.0), p1=(0.0, 0.0), source="c", origin=2),
        ]
        stretches = split_run_at_scores(run, {7: score})
        self.assertEqual(len(stretches), 1)
        pieces, before, after = stretches[0]
        self.assertEqual([piece.source for piece in pieces], ["b", "c"])
        self.assertIs(before, score)
        self.assertIs(after, score)


class ArcOutlineTest(unittest.TestCase):
    """The fillet case, end to end on a board rather than on a bare chain."""

    def filleted_board(self) -> Board:
        return filleted_board()

    def test_the_short_right_edge_is_scored(self):
        board = accept(self.filleted_board(), x=20.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=0.2))
        spans = {round(score.length_mm, 6) for score in made.scores}
        self.assertIn(6.0, spans)

    def test_the_line_that_accepts_it_is_longer_than_the_edge_it_takes(self):
        # The extent rule on a real board: 6 mm of outline, a 10 mm score line,
        # and the validator matches it anyway.
        board = accept(self.filleted_board(), x=20.0)
        line = items_on(board, SCORE_LAYER)[0]
        self.assertAlmostEqual(
            abs(line.start[1] - line.end[1]), 10.0, places=9
        )
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=0.2))
        self.assertEqual(len(made.scores), 1)
        self.assertEqual(made.stale_scores, [])

    def test_a_run_arriving_alongside_the_plane_gets_no_overshoot(self):
        # An arc curling round a fillet travels PARALLEL to the score plane at
        # the junction; producing it never crosses the plane at any length. The
        # stage leaves the run where the fillet put it and records the quota it
        # did not deliver.
        board = accept(self.filleted_board(), x=20.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        short = [
            junction for junction in made.junctions if not junction.reached
        ]
        self.assertTrue(short)
        for junction in short:
            self.assertEqual(junction.action, "unchanged")
            # The fillet carries the cut to the plane and no further: it is
            # tangent to the scored edge there, so the board's own edge ends at
            # the plane and there is nothing past it to take to depth. Read off
            # the CENTRELINE it would look like a radius past the plane.
            self.assertAlmostEqual(junction.past_mm, 0.0, delta=1e-6)

    def test_that_junction_is_NORMAL_and_says_why(self):
        # The verdict judges STANDING MATERIAL, not the distance quota.
        # Nothing stands here - the
        # board's own edge ends at the score - so the junction is normal, and
        # the reason is the shape rather than the number.
        board = accept(self.filleted_board(), x=20.0)
        made = build(board, CUTTER, VScoreConfig(route_past_score_mm=1.0))
        self.assertTrue(made.junctions)
        for junction in made.junctions:
            self.assertEqual(junction.verdict, JUNCTION_NORMAL)
            self.assertTrue(junction.normal)
            self.assertAlmostEqual(junction.standing_mm, 0.0, delta=1e-6)
            self.assertIn("the board edge ends at the score", junction.reason)
        # The quota is still recorded, and still says the overshoot was not
        # delivered - which is exactly why the two are separate fields.
        self.assertFalse(all(junction.reached for junction in made.junctions))

    def test_the_page_is_not_warned_about_a_fillet(self):
        # A WARN here would give the designer nothing to act on: the fillet
        # shape, not the toolpath, stops the overshoot. No warning mentions
        # this junction at any overshoot.
        for route_past in (0.0, 0.2, 0.5, 1.0):
            board = accept(self.filleted_board(), x=20.0)
            made = build(board, CUTTER,
                         VScoreConfig(route_past_score_mm=route_past))
            with self.subTest(route_past=route_past):
                self.assertEqual(
                    [
                        warning for warning in made.warnings
                        if "score plane" in warning or "standing" in warning
                    ],
                    [],
                )


class JunctionVerdictTest(unittest.TestCase):
    """The junction verdict, on plain numbers.

    Four cases: a junction is judged on the
    board edge it leaves STANDING, not on whether the fab's overshoot number was
    delivered, because at a corner whose own edge ends at the score no overshoot
    is possible and the board still separates cleanly.
    """

    def test_material_standing_warns_whatever_the_quota_says(self):
        verdict, reason = junction_verdict(
            past_mm=-0.3636, wanted_mm=0.0, standing_mm=1.2245,
            producible=False,
        )
        self.assertEqual(verdict, JUNCTION_WARN)
        self.assertIn("1.2245 mm of board edge stands", reason)

    def test_the_declared_margin_delivered_is_normal(self):
        verdict, reason = junction_verdict(
            past_mm=0.5, wanted_mm=0.5, standing_mm=0.0, producible=True,
        )
        self.assertEqual(verdict, JUNCTION_NORMAL)
        self.assertIn("0.5000 mm past the score plane", reason)

    def test_nothing_further_to_cut_is_normal_and_says_so(self):
        # The fillet case, in the designer's words: nothing is left to cut,
        # so the junction is normal.
        verdict, reason = junction_verdict(
            past_mm=0.0, wanted_mm=1.0, standing_mm=0.0, producible=False,
        )
        self.assertEqual(verdict, JUNCTION_NORMAL)
        self.assertIn("the board edge ends at the score", reason)
        self.assertIn("nothing further", reason)

    def test_a_square_edge_that_could_have_reached_and_did_not_warns(self):
        # The counter-case that keeps the rule honest: this edge drives straight
        # at the plane, producing it is exactly what the stage does, and it
        # still fell short. The designer can act on that one.
        verdict, reason = junction_verdict(
            past_mm=0.1, wanted_mm=0.5, standing_mm=0.0, producible=True,
        )
        self.assertEqual(verdict, JUNCTION_WARN)
        self.assertIn("drives straight at the score plane", reason)
        self.assertIn("0.4000 mm short", reason)

    def test_an_exceeded_margin_is_normal_even_on_a_square_edge(self):
        verdict, _ = junction_verdict(
            past_mm=0.9, wanted_mm=0.5, standing_mm=0.0, producible=True,
        )
        self.assertEqual(verdict, JUNCTION_NORMAL)


class StandingEdgeTest(unittest.TestCase):
    """The severing measurement the verdict is read off.

    Board edge left between where the cut leaves the outline and the score
    plane, in millimetres of OUTLINE - which is not the perpendicular shortfall.
    Round the corpus board's fillet the two differ by more than three times:
    0.3636 mm short of the plane left 1.2245 mm of edge standing, which is the
    figure a user sees in the preview.
    """

    def chain(self):
        return weld_chain(chain_loop(outline_primitives(filleted_board())))

    def candidate(self):
        """The blade line on the filleted board's short right edge, x = 20."""
        found = [
            entry for entry in score_candidates(self.chain())
            if abs(entry.start[0] - 20.0) < 1e-6
            and abs(entry.end[0] - 20.0) < 1e-6
        ]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_contact_at_the_plane_leaves_nothing_standing(self):
        # The tangent point where the lower fillet meets the scored edge.
        self.assertAlmostEqual(
            standing_edge_mm(self.chain(), self.candidate(), (20.0, 2.0)),
            0.0, places=9,
        )

    def test_a_contact_past_the_plane_leaves_nothing_standing(self):
        self.assertAlmostEqual(
            standing_edge_mm(self.chain(), self.candidate(), (20.5, 2.0)),
            0.0, places=9,
        )

    def station(self, short: float) -> tuple:
        """A point on the lower fillet *short* millimetres inside the plane.

        The fillet is centred at (18, 2) with a 2 mm radius, tangent to y = 0 at
        (18, 0) and to the scored edge x = 20 at (20, 2).
        """
        angle = math.acos((2.0 - short) / 2.0)
        return (20.0 - short, 2.0 - 2.0 * math.sin(angle))

    def test_a_contact_short_of_the_plane_measures_the_edge_it_left(self):
        # A contact 0.3636 mm short of the plane, part way round the fillet:
        # the edge between there and the score is more than a millimetre of
        # arc - three times the perpendicular shortfall, which is why the
        # shortfall is not the figure. The last 0.063 mm of it is inside
        # CONTACT_TOL_MM of the plane, where the arc runs a micrometre off it,
        # and is not counted.
        standing = standing_edge_mm(
            self.chain(), self.candidate(), self.station(0.3636)
        )
        self.assertAlmostEqual(standing, 1.1618, delta=1e-3)
        self.assertGreater(standing, 0.3636 * 3.0)

    def test_it_grows_with_the_shortfall(self):
        candidate, chain = self.candidate(), self.chain()
        measured = [
            standing_edge_mm(chain, candidate, self.station(short))
            for short in (0.1, 0.3636, 0.5, 1.0)
        ]
        self.assertEqual(measured, sorted(measured))

    def test_the_shorter_way_round_the_loop_is_the_answer(self):
        # The material behind the cut is already gone, so only the reach on the
        # score's side counts. Walking the other way round this loop is more
        # than 50 mm; the answer stays the millimetre and a bit.
        standing = standing_edge_mm(
            self.chain(), self.candidate(), self.station(0.5)
        )
        self.assertLess(standing, 2.0)


class PrepVerdictTest(unittest.TestCase):
    """What the PAGE is told about the junctions of one whole prep run.

    The two properties are different questions, and this is where
    the difference lands: the filleted board is CLEAR and not REACHED, and it is
    `junctions_clear` the page judges on.
    """

    def project(self, folder: Path, route_past: float) -> tuple:
        board = accept(filleted_board(), x=20.0)
        board_path = folder / "board.kicad_pcb"
        board.to_file(board_path)
        panel = folder / "panel"
        panel.mkdir()
        config_path = panel / "panel.json"
        config_path.write_text(json.dumps({
            "revision": CONFIG_REVISION,
            "source_board": "../board.kicad_pcb",
            "grid": {"columns": 1, "rows": 1, "gap_mm": 0.0},
            "prep": {
                "cutter_diameter": CUTTER,
                "vscore": {
                    "detect": "auto", "route_past_score_mm": route_past
                },
            },
        }), encoding="utf-8")
        return load_config(config_path), board_path

    def result(self, route_past: float):
        with tempfile.TemporaryDirectory() as folder:
            config, _ = self.project(Path(folder), route_past)
            return route_toolpath.run(config)

    def test_a_filleted_board_is_clear_without_being_reached(self):
        result = self.result(1.0)
        self.assertTrue(result.junctions)
        self.assertTrue(result.junctions_clear)
        self.assertFalse(result.junctions_reached)

    def test_the_notice_says_the_shape_and_not_a_shortfall(self):
        notice = prep.score_notice(self.result(1.0))
        self.assertIn("the board edge ends at the score", notice)
        self.assertNotIn("standing", notice)

    def test_nothing_about_a_junction_reaches_the_toolpath_alarms(self):
        # Warn-never-block: a junction is a report about a valid path, so the
        # preview must not paint the toolpath red over one.
        self.assertEqual(checks.toolpath_alarms(self.result(1.0)), ())


class FileLevelNoWriteTest(unittest.TestCase):
    """The invariant where it counts: on a FILE, in bytes, through `run`.

    The object-level check in :class:`NoWriteTest` proves `build` adds nothing;
    this proves the whole prep round trip - load, build, write - hands the
    designer's `User.38` back character for character. It is the assertion the
    corpus gate makes by hand, kept where it runs every time.
    """

    def project(self, folder: Path) -> object:
        board = accept(scoreable_board(), x=0.0)
        board_path = folder / "board.kicad_pcb"
        board.to_file(board_path)
        panel = folder / "panel"
        panel.mkdir()
        config_path = panel / "panel.json"
        config_path.write_text(json.dumps({
            "revision": CONFIG_REVISION,
            "source_board": "../board.kicad_pcb",
            "grid": {"columns": 1, "rows": 1, "gap_mm": 0.0},
            "prep": {
                "cutter_diameter": CUTTER,
                "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
            },
        }), encoding="utf-8")
        return load_config(config_path), board_path

    def score_section(self, path: Path) -> list:
        """Every `(gr_line …)` block naming the score layer, verbatim.

        Read out of the FILE by balancing parentheses rather than reloaded
        through the library, so the comparison is over characters on disk and
        not over a model that could round two different files to one answer.
        """
        text = path.read_text(encoding="utf-8")
        blocks, index = [], 0
        while True:
            index = text.find("(gr_line", index)
            if index < 0:
                return blocks
            depth, end = 0, index
            for end in range(index, len(text)):
                if text[end] == "(":
                    depth += 1
                elif text[end] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            block = text[index:end + 1]
            if f'(layer "{SCORE_LAYER}")' in block:
                blocks.append(block)
            index = end + 1

    def test_prep_hands_the_score_layer_back_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as folder:
            config, board_path = self.project(Path(folder))
            before = self.score_section(board_path)
            self.assertEqual(len(before), 1)
            result = route_toolpath.run(config)
            self.assertTrue(result.scores_untouched)
            self.assertEqual(self.score_section(board_path), before)
            self.assertEqual(len(result.scores), 1)

    def test_a_second_prep_writes_the_same_file(self):
        with tempfile.TemporaryDirectory() as folder:
            config, board_path = self.project(Path(folder))
            first = route_toolpath.run(config)
            second = route_toolpath.run(config)
            self.assertEqual(first.board_sha256, second.board_sha256)
            self.assertTrue(second.scores_untouched)

    def test_the_decision_survives_the_round_trip_as_a_decision(self):
        # Written by the accept surface, read back by the validator, scored.
        with tempfile.TemporaryDirectory() as folder:
            config, _ = self.project(Path(folder))
            result = route_toolpath.run(config)
            self.assertEqual(len(result.score_lines), 1)
            self.assertEqual(result.stale_scores, [])
            self.assertEqual(len(result.junctions), 2)


if __name__ == "__main__":
    unittest.main()
