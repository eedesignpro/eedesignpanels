"""comes_apart: does the panel come apart, and in what order.

The verdict reports whether the panel comes apart; it does not raise on a
panel that does not. Three kinds of scenario are fenced, matching the module's
own three-way split:

* a panel that COMES APART - a saw pass runs clear across two touching boards;
* a panel with a TRAPPED board - nothing (no cut, no tab) ever frees it;
* a panel HELD ONLY BY TABS - no saw pass anywhere, and the board is still
  loose because every point of its edge stands on a tab.

The end-to-end scenarios drive :func:`comes_apart.verdict` itself, over a real
`eedesigncore` board and a minimal stand-in for `PanelGeometry` and
`panelize.PanelizeResult` - a `SimpleNamespace` carrying only the attributes
`verdict` reads (`blank`, `cut_channels`, `instance_boxes`, `placements`,
`source_outline`), which is the same pattern `test_panel_carrier_page.py`'s
`made_with` uses for a stage's own report object. The smaller pure functions
underneath are fenced directly, on synthetic geometry, because that is what
lets the recursion's own rules - a split never re-attaches anything, a tab
frees a board with no saw at all - be pinned without building a whole panel
for each one.
"""

import unittest
from types import SimpleNamespace

from eedesigncore import Board, BoardLayer, PcbLine, Stroke

from eedesignpanels import comes_apart
from eedesignpanels.channels import Rect
from eedesignpanels.toolpath import Arc, Grid, Line, index_cell_mm, padded_bbox


def a_placement(column: int, row: int, dx_mm: float = 0.0, dy_mm: float = 0.0,
                rotation_deg: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(column=column, row=row, dx_mm=dx_mm, dy_mm=dy_mm,
                           rotation_deg=rotation_deg)


def score_board(*lines) -> Board:
    """A board carrying only the layers `saw_lines` and `score_lines_on` read."""
    board = Board(layers=[
        BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
        BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
    ])
    board.drawings = list(lines)
    return board


def user38(start, end, uid) -> PcbLine:
    return PcbLine(start=start, end=end, layer="User.38",
                   stroke=Stroke(width=0.1), id=uid)


class VerdictEndToEndTest(unittest.TestCase):
    """`verdict()` itself, over two boards touching edge to edge in a blank."""

    BLANK = Rect(min_x=0.0, min_y=0.0, max_x=20.0, max_y=10.0)
    BOXES = ((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0))

    def geometry(self) -> SimpleNamespace:
        return SimpleNamespace(
            instance_boxes=self.BOXES,
            placements=[a_placement(0, 0), a_placement(1, 0)],
            source_outline=(),
        )

    def made(self) -> SimpleNamespace:
        return SimpleNamespace(blank=self.BLANK, cut_channels=())

    def test_a_saw_pass_clear_across_both_boards_lets_them_apart(self):
        # The one line down the middle is the whole panel score: it runs the
        # full height of the blank, so it splits the panel in two and each
        # half exactly matches one board's own box.
        board = score_board(user38((10.0, 0.0), (10.0, 10.0), "split"))
        result = comes_apart.verdict(board, self.made(), self.geometry())
        self.assertTrue(result.comes_apart)
        self.assertEqual(result.trapped, ())
        self.assertEqual(len(result.steps), 3)  # one saw, two snaps
        self.assertIn("Saw down the line", result.steps[0])

    def test_two_boards_with_nothing_between_them_are_trapped(self):
        # Same two boards, same blank, no saw line anywhere and no tab: the
        # shared edge at x=10 is plain material, never cut, never a tab.
        board = score_board()
        result = comes_apart.verdict(board, self.made(), self.geometry())
        self.assertFalse(result.comes_apart)
        self.assertEqual(result.trapped_count, 2)
        self.assertTrue(all("joined to the panel" in line
                            for line in result.trapped))

    def test_report_lines_name_every_trapped_board(self):
        board = score_board()
        result = comes_apart.verdict(board, self.made(), self.geometry())
        lines = comes_apart.report_lines(result)
        self.assertIn("2 boards cannot be freed from this panel.", lines[0])
        self.assertEqual(len(lines), 1 + result.trapped_count)

    def test_report_lines_number_every_step_when_it_comes_apart(self):
        board = score_board(user38((10.0, 0.0), (10.0, 10.0), "split"))
        result = comes_apart.verdict(board, self.made(), self.geometry())
        lines = comes_apart.report_lines(result)
        self.assertIn("The panel comes apart in 3 steps:", lines[0])
        self.assertEqual(lines[1], f"1. {result.steps[0]}")
        self.assertEqual(lines[-1], f"3. {result.steps[2]}")


class BreakUpTabsOnlyTest(unittest.TestCase):
    """A board held only by tabs comes apart with no saw pass at all.

    Driven at :func:`comes_apart.break_up` directly, on a synthetic
    `comes_apart.Board`, because the point being fenced is the recursion's
    own rule - `loose_in` accepts a "tab" point wherever it sits - and that
    rule needs no real staple geometry to exercise.
    """

    def test_a_board_whose_whole_edge_is_tabbed_is_snapped_out_unsawn(self):
        piece = (0.0, 0.0, 20.0, 20.0)
        box = (5.0, 5.0, 15.0, 15.0)
        points = tuple(comes_apart.rect_points(box))
        board = comes_apart.Board(
            label="column 1, row 1", box=box, points=points,
            kinds=tuple("tab" for _ in points),
        )
        steps: list = []
        stuck: list = []
        comes_apart.break_up(piece, [board], splits=[], lines=[],
                             blank=Rect(*piece), steps=steps, stuck=stuck)
        self.assertEqual(stuck, [])
        self.assertEqual(steps, ["Snap out the board at column 1, row 1."])

    def test_the_same_board_with_plain_edges_instead_is_trapped(self):
        # THE CONTROL: identical geometry, "joined" instead of "tab" - and
        # with no saw pass to free it either, it is exactly what a check that
        # understood only saw passes would (wrongly) expect of the tabbed
        # case too.
        piece = (0.0, 0.0, 20.0, 20.0)
        box = (5.0, 5.0, 15.0, 15.0)
        points = tuple(comes_apart.rect_points(box))
        board = comes_apart.Board(
            label="column 1, row 1", box=box, points=points,
            kinds=tuple("joined" for _ in points),
        )
        steps: list = []
        stuck: list = []
        comes_apart.break_up(piece, [board], splits=[], lines=[],
                             blank=Rect(*piece), steps=steps, stuck=stuck)
        self.assertEqual(stuck, [board])
        self.assertEqual(steps, [])


class WalkTest(unittest.TestCase):
    """Points along one primitive, ends included, no wider apart than the step."""

    def test_a_line_is_walked_end_to_end(self):
        points = comes_apart.walk(Line(p0=(0.0, 0.0), p1=(2.0, 0.0)), step_mm=1.0)
        self.assertEqual(points, [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])

    def test_a_short_line_still_carries_both_ends(self):
        points = comes_apart.walk(Line(p0=(0.0, 0.0), p1=(0.1, 0.0)))
        self.assertEqual(points, [(0.0, 0.0), (0.1, 0.0)])

    def test_an_arc_is_walked_by_angle_ccw(self):
        import math
        arc = Arc(center=(0.0, 0.0), radius=1.0, a0=0.0, a1=math.pi / 2.0,
                 ccw=True)
        points = comes_apart.walk(arc, step_mm=math.pi / 2.0)
        self.assertAlmostEqual(points[0][0], 1.0, places=9)
        self.assertAlmostEqual(points[0][1], 0.0, places=9)
        self.assertAlmostEqual(points[-1][0], 0.0, places=9)
        self.assertAlmostEqual(points[-1][1], 1.0, places=9)


class RectPointsTest(unittest.TestCase):
    def test_every_point_lies_on_the_rectangle_and_no_corner_repeats(self):
        points = comes_apart.rect_points((0.0, 0.0, 4.0, 2.0), step_mm=2.0)
        # One point per side start, corners not doubled (the last point of a
        # side is the first point of the next one, and `walk`'s own end is
        # dropped for exactly that reason).
        self.assertEqual(
            points,
            [(0.0, 0.0), (2.0, 0.0), (4.0, 0.0),
             (4.0, 2.0), (2.0, 2.0), (0.0, 2.0)],
        )
        for x, y in points:
            self.assertTrue(x in (0.0, 4.0) or y in (0.0, 2.0))


class SplitsOfTest(unittest.TestCase):
    """Which saw passes could ever split something."""

    BOXES = ((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0))

    def test_a_pass_along_the_shared_edge_is_a_split(self):
        line = Line(p0=(10.0, 0.0), p1=(10.0, 10.0))
        self.assertEqual(comes_apart.splits_of([line], self.BOXES),
                         [("vertical", 10.0)])

    def test_a_pass_through_the_inside_of_a_board_is_not_a_split(self):
        line = Line(p0=(5.0, 0.0), p1=(5.0, 10.0))
        self.assertEqual(comes_apart.splits_of([line], self.BOXES), [])

    def test_a_diagonal_line_splits_nothing(self):
        line = Line(p0=(0.0, 0.0), p1=(10.0, 10.0))
        self.assertEqual(comes_apart.splits_of([line], self.BOXES), [])

    def test_two_lines_on_the_same_split_count_once(self):
        lines = [Line(p0=(10.0, 0.0), p1=(10.0, 5.0)),
                Line(p0=(10.0, 5.0), p1=(10.0, 10.0))]
        self.assertEqual(comes_apart.splits_of(lines, self.BOXES),
                         [("vertical", 10.0)])


class SpansCutsHalvesTest(unittest.TestCase):
    PIECE = (0.0, 0.0, 10.0, 10.0)

    def test_a_line_the_full_height_spans_a_vertical_piece(self):
        line = Line(p0=(5.0, 0.0), p1=(5.0, 10.0))
        self.assertTrue(comes_apart.spans(line, self.PIECE))

    def test_a_line_short_of_the_edge_does_not_span(self):
        line = Line(p0=(5.0, 1.0), p1=(5.0, 9.0))
        self.assertFalse(comes_apart.spans(line, self.PIECE))

    def test_cuts_in_finds_only_the_splits_inside_the_piece(self):
        lines = [Line(p0=(5.0, 0.0), p1=(5.0, 10.0))]
        self.assertEqual(
            comes_apart.cuts_in(self.PIECE, [("vertical", 5.0)], lines),
            [("vertical", 5.0)],
        )
        self.assertEqual(
            comes_apart.cuts_in(self.PIECE, [("vertical", 0.0)], lines), []
        )

    def test_halves_splits_a_piece_on_its_axis(self):
        self.assertEqual(
            comes_apart.halves(self.PIECE, "vertical", 4.0),
            ((0.0, 0.0, 4.0, 10.0), (4.0, 0.0, 10.0, 10.0)),
        )
        self.assertEqual(
            comes_apart.halves(self.PIECE, "horizontal", 4.0),
            ((0.0, 0.0, 10.0, 4.0), (0.0, 4.0, 10.0, 10.0)),
        )


class InsideOnEdgeLooseTest(unittest.TestCase):
    def test_a_boards_middle_decides_which_piece_it_belongs_to(self):
        self.assertTrue(comes_apart.inside((0.0, 0.0, 4.0, 4.0),
                                           (0.0, 0.0, 10.0, 10.0)))
        self.assertFalse(comes_apart.inside((11.0, 11.0, 20.0, 20.0),
                                            (0.0, 0.0, 10.0, 10.0)))

    def test_on_edge_is_true_on_any_of_the_four_sides(self):
        piece = (0.0, 0.0, 10.0, 10.0)
        for point in ((0.0, 5.0), (10.0, 5.0), (5.0, 0.0), (5.0, 10.0)):
            with self.subTest(point=point):
                self.assertTrue(comes_apart.on_edge(point, piece))
        self.assertFalse(comes_apart.on_edge((5.0, 5.0), piece))

    def test_loose_in_needs_every_point_cut_tab_or_on_the_edge(self):
        piece = (0.0, 0.0, 10.0, 10.0)
        board = comes_apart.Board(
            label="b", box=piece, points=((5.0, 5.0), (0.0, 0.0)),
            kinds=("joined", "cut"),
        )
        self.assertFalse(comes_apart.loose_in(board, piece))
        freed = board._replace(kinds=("tab", "cut"))
        self.assertTrue(comes_apart.loose_in(freed, piece))


class ClassifyTest(unittest.TestCase):
    """What is at one point on a board's edge, in the order a fabricator reads."""

    def test_a_point_within_the_cutters_reach_is_cut(self):
        line = Line(p0=(0.0, 0.0), p1=(10.0, 0.0))
        items = [(line, 0.5)]
        boxes = [padded_bbox(line, 0.5)]
        index = Grid(boxes, index_cell_mm(boxes))
        self.assertEqual(comes_apart.classify((5.0, 0.0), items, index, [], []),
                         "cut")

    def test_a_point_on_a_tabs_base_is_a_tab(self):
        base = Line(p0=(0.0, 0.0), p1=(2.0, 0.0))
        self.assertEqual(
            comes_apart.classify((1.0, 0.0), [], Grid([], 1.0), [base], []),
            "tab",
        )

    def test_a_point_on_a_saw_line_is_saw(self):
        saw = Line(p0=(0.0, 0.0), p1=(0.0, 10.0), role="saw")
        self.assertEqual(
            comes_apart.classify((0.0, 5.0), [], Grid([], 1.0), [], [saw]),
            "saw",
        )

    def test_a_point_touching_nothing_is_joined(self):
        self.assertEqual(
            comes_apart.classify((3.0, 3.0), [], Grid([], 1.0), [], []),
            "joined",
        )

    def test_cut_is_read_before_tab_and_saw(self):
        # The fabricator's own order: gone already beats held on purpose beats
        # standing on a line not yet run.
        line = Line(p0=(0.0, 0.0), p1=(10.0, 0.0))
        items = [(line, 0.5)]
        boxes = [padded_bbox(line, 0.5)]
        index = Grid(boxes, index_cell_mm(boxes))
        base = Line(p0=(0.0, 0.0), p1=(2.0, 0.0))
        saw = Line(p0=(0.0, 0.0), p1=(10.0, 0.0), role="saw")
        self.assertEqual(
            comes_apart.classify((1.0, 0.0), items, index, [base], [saw]),
            "cut",
        )


class BoardMeasurementTest(unittest.TestCase):
    def test_joined_mm_and_sawn_mm_count_their_own_kind_only(self):
        board = comes_apart.Board(
            label="b", box=(0.0, 0.0, 1.0, 1.0),
            points=((0, 0), (0, 0), (0, 0), (0, 0)),
            kinds=("joined", "joined", "saw", "cut"),
        )
        self.assertAlmostEqual(board.joined_mm, 2 * comes_apart.SAMPLE_MM)
        self.assertAlmostEqual(board.sawn_mm, comes_apart.SAMPLE_MM)


class DatumAndSentenceTest(unittest.TestCase):
    BLANK = Rect(min_x=100.0, min_y=50.0, max_x=200.0, max_y=150.0)

    def test_datum_reads_from_the_blanks_top_left_corner(self):
        self.assertAlmostEqual(comes_apart.datum("vertical", 150.0, self.BLANK),
                               50.0)
        self.assertAlmostEqual(comes_apart.datum("horizontal", 80.0, self.BLANK),
                               30.0)

    def test_the_saw_sentence_names_the_axis_and_the_datum(self):
        said = comes_apart._saw_sentence("vertical", 150.0, self.BLANK)
        self.assertIn("Saw down the line", said)
        self.assertIn("50.00 mm", said)
        said = comes_apart._saw_sentence("horizontal", 80.0, self.BLANK)
        self.assertIn("Saw across the line", said)
        self.assertIn("30.00 mm", said)

    def test_the_snap_sentence_lists_one_or_several_boards(self):
        self.assertEqual(comes_apart._snap_sentence(["a"]),
                         "Snap out the board at a.")
        self.assertEqual(comes_apart._snap_sentence(["a", "b"]),
                         "Snap out the 2 boards at a; b.")

    def test_the_stuck_sentence_names_joined_material_before_a_bare_saw_line(self):
        joined = comes_apart.Board(
            label="c1r1", box=(0.0, 0.0, 1.0, 1.0),
            points=((0, 0),), kinds=("joined",),
        )
        self.assertIn("joined to the panel", comes_apart._stuck_sentence(joined))
        sawn = comes_apart.Board(
            label="c2r1", box=(0.0, 0.0, 1.0, 1.0),
            points=((0, 0),), kinds=("saw",),
        )
        self.assertIn("saw line that no pass reaches",
                      comes_apart._stuck_sentence(sawn))


class VerdictStatusTest(unittest.TestCase):
    def test_comes_apart_reads_as_two_words(self):
        self.assertEqual(comes_apart.Verdict(comes_apart=True).status,
                         "comes apart")

    def test_trapped_names_the_count_singular_and_plural(self):
        self.assertEqual(
            comes_apart.Verdict(comes_apart=False, trapped=("one",)).status,
            "trapped: 1 board",
        )
        self.assertEqual(
            comes_apart.Verdict(comes_apart=False,
                                trapped=("one", "two")).status,
            "trapped: 2 boards",
        )


class PlacedTest(unittest.TestCase):
    def test_a_point_is_turned_then_moved(self):
        # `rotate_kicad(90)` is `(y, -x)` (KiCad's own Y-down convention), so
        # (1, 0) turns to (0, -1) before the offset is added.
        place = a_placement(0, 0, dx_mm=10.0, dy_mm=5.0, rotation_deg=90.0)
        moved = comes_apart.placed((1.0, 0.0), place)
        self.assertAlmostEqual(moved[0], 10.0, places=9)
        self.assertAlmostEqual(moved[1], 4.0, places=9)


if __name__ == "__main__":
    unittest.main()
