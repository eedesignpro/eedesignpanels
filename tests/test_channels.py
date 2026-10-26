"""The sheet-minus-channels boolean, checked against closed forms.

`eedesignpanels.channels` is pure geometry, so every case here has a perimeter
anybody can work out on paper: a rectangle is `2(w+h)`, a stadium is twice the
segment plus a whole circle, an inset rectangle is the rectangle less
`8 x radius` of corner. That is the point of testing it this way - a boolean
that is wrong by a fraction of a millimetre draws a panel that looks right.

The cases that earn their keep are the ones where the topology changes: a
channel that stays inside the sheet leaves a hole, one that crosses the sheet
cuts it in two, a closed ring leaves an island, and a run made of dust leaves
nothing but the sheet.
"""

import math
import unittest

from eedesignpanels.channels import (
    BOUNDARY_NODE_TOL_MM,
    MIN_ITEM_MM,
    Channel,
    ChannelError,
    Rect,
    _inside_the_cut,
    chain_runs,
    grown,
    region_boundary,
)
from eedesignpanels.toolpath import (
    Arc,
    Grid,
    Line,
    arc_from_three_points,
    index_cell_mm,
    midpoint_of,
    padded_bbox,
)

RADIUS = 0.5


def sheet(width: float = 20.0, height: float = 10.0) -> Rect:
    return Rect(0.0, 0.0, width, height)


def line(x0, y0, x1, y1, source="a"):
    return Line(p0=(x0, y0), p1=(x1, y1), source=source)


def loop_lengths(boundary) -> list:
    return sorted(
        round(sum(piece.length for piece in loop), 6)
        for loop in boundary.loops
    )


class SheetTest(unittest.TestCase):
    def test_the_sheet_is_the_grid_grown_by_the_margin(self):
        rect = grown((10.0, 20.0, 30.0, 50.0), 5.0)
        self.assertEqual(rect, Rect(5.0, 15.0, 35.0, 55.0))
        self.assertEqual(rect.extent_mm, (30.0, 40.0))

    def test_a_zero_margin_is_the_grid_itself(self):
        self.assertEqual(grown((0.0, 0.0, 3.0, 4.0), 0.0), Rect(0, 0, 3, 4))

    def test_a_negative_margin_is_refused(self):
        with self.assertRaises(ValueError):
            grown((0.0, 0.0, 1.0, 1.0), -0.1)

    def test_the_four_edges_close_the_rectangle(self):
        edges = sheet().edges()
        self.assertEqual(len(edges), 4)
        for before, after in zip(edges, edges[1:] + edges[:1]):
            self.assertEqual(before.p1, after.p0)


class RunTest(unittest.TestCase):
    def test_connected_items_become_one_run(self):
        runs = chain_runs([
            (line(1.0, 1.0, 5.0, 1.0, "a"), RADIUS),
            (line(5.0, 1.0, 5.0, 4.0, "b"), RADIUS),
        ])
        self.assertEqual(len(runs.channels), 1)
        self.assertEqual(len(runs.channels[0].path), 2)
        self.assertFalse(runs.channels[0].closed)

    def test_separate_items_stay_separate_runs(self):
        runs = chain_runs([
            (line(1.0, 1.0, 5.0, 1.0, "a"), RADIUS),
            (line(1.0, 8.0, 5.0, 8.0, "b"), RADIUS),
        ])
        self.assertEqual(len(runs.channels), 2)

    def test_a_ring_is_one_closed_run(self):
        corners = [(5.0, 3.0), (15.0, 3.0), (15.0, 7.0), (5.0, 7.0)]
        runs = chain_runs([
            (line(*corners[i], *corners[(i + 1) % 4], f"r{i}"), RADIUS)
            for i in range(4)
        ])
        self.assertEqual(len(runs.channels), 1)
        self.assertTrue(runs.channels[0].closed)

    def test_a_stroke_width_change_ends_the_run(self):
        runs = chain_runs([
            (line(1.0, 1.0, 5.0, 1.0, "a"), 0.5),
            (line(5.0, 1.0, 9.0, 1.0, "b"), 0.25),
        ])
        self.assertEqual(len(runs.channels), 2)
        self.assertEqual(
            sorted(run.radius for run in runs.channels), [0.25, 0.5]
        )

    def test_a_nanometre_gap_is_still_one_run(self):
        runs = chain_runs([
            (line(1.0, 1.0, 5.0, 1.0, "a"), RADIUS),
            (line(5.000001, 1.0, 9.0, 1.0, "b"), RADIUS),
        ])
        self.assertEqual(len(runs.channels), 1)
        path = runs.channels[0].path
        self.assertEqual(path[0].end, path[1].start)

    def test_three_items_meeting_at_a_point_is_an_error_with_a_location(self):
        with self.assertRaises(ChannelError) as caught:
            chain_runs([
                (line(0.0, 5.0, 5.0, 5.0, "a"), RADIUS),
                (line(5.0, 5.0, 9.0, 5.0, "b"), RADIUS),
                (line(5.0, 5.0, 5.0, 9.0, "c"), RADIUS),
            ])
        self.assertEqual(caught.exception.at, (5.0, 5.0))

    def test_dust_is_dropped_and_counted(self):
        runs = chain_runs([
            (line(1.0, 1.0, 5.0, 1.0, "a"), RADIUS),
            (line(5.0, 1.0, 5.0 + MIN_ITEM_MM / 2.0, 1.0, "dust"), RADIUS),
            (line(5.0 + MIN_ITEM_MM / 2.0, 1.0, 9.0, 1.0, "b"), RADIUS),
        ])
        self.assertEqual(runs.dropped, 1)
        self.assertAlmostEqual(runs.worst_dropped_mm, MIN_ITEM_MM / 2.0)
        self.assertEqual(runs.item_count, 2)
        # ...and the gap it left is welded shut, so the run is still a run.
        path = runs.channels[0].path
        self.assertEqual(path[0].end, path[1].start)

    def test_a_run_of_nothing_but_dust_disappears(self):
        runs = chain_runs([(line(1.0, 1.0, 1.0001, 1.0, "dust"), RADIUS)])
        self.assertEqual(runs.channels, ())
        self.assertEqual(runs.dropped, 1)


class BoundaryTest(unittest.TestCase):
    def test_a_sheet_with_nothing_routed_is_its_own_outline(self):
        boundary = region_boundary(sheet(), [])
        self.assertEqual(boundary.loop_count, 1)
        self.assertEqual(boundary.item_count, 4)
        self.assertAlmostEqual(boundary.length_mm, 60.0)
        self.assertEqual(boundary.extent_mm, (20.0, 10.0))

    def test_a_channel_inside_the_sheet_leaves_a_stadium_hole(self):
        runs = chain_runs([(line(5.0, 5.0, 15.0, 5.0), RADIUS)])
        boundary = region_boundary(sheet(), runs.channels)
        self.assertEqual(boundary.loop_count, 2)
        stadium = 2.0 * 10.0 + 2.0 * math.pi * RADIUS
        self.assertEqual(loop_lengths(boundary),
                         sorted([round(stadium, 6), 60.0]))
        self.assertFalse(boundary.reaches_outer_edge)
        self.assertAlmostEqual(boundary.clearance_mm, 5.0 - RADIUS)

    def test_a_channel_across_the_sheet_cuts_it_in_two(self):
        runs = chain_runs([(line(-2.0, 5.0, 22.0, 5.0), RADIUS)])
        boundary = region_boundary(sheet(), runs.channels)
        self.assertEqual(boundary.loop_count, 2)
        # Each half: the full width twice, plus the two sides of what is left.
        half = 2.0 * 20.0 + 2.0 * (5.0 - RADIUS)
        self.assertEqual(loop_lengths(boundary), [round(half, 6)] * 2)
        self.assertTrue(boundary.reaches_outer_edge)

    def test_a_closed_ring_leaves_an_island_inside_it(self):
        corners = [(5.0, 3.0), (15.0, 3.0), (15.0, 7.0), (5.0, 7.0)]
        runs = chain_runs([
            (line(*corners[i], *corners[(i + 1) % 4], f"r{i}"), RADIUS)
            for i in range(4)
        ])
        boundary = region_boundary(sheet(), runs.channels)
        self.assertEqual(boundary.loop_count, 3)
        outer = 2.0 * (10.0 + 4.0) + 2.0 * math.pi * RADIUS
        island = 2.0 * (10.0 - 2 * RADIUS + 4.0 - 2 * RADIUS)
        self.assertEqual(
            loop_lengths(boundary),
            sorted([60.0, round(outer, 6), round(island, 6)]),
        )

    def test_an_arc_channel_comes_back_as_true_arcs(self):
        quarter = arc_from_three_points(
            (10.0, 2.0),
            (10.0 + 3.0 * math.sqrt(0.5), 5.0 - 3.0 * math.sqrt(0.5)),
            (13.0, 5.0),
            source="q",
        )
        runs = chain_runs([(quarter, RADIUS)])
        boundary = region_boundary(sheet(), runs.channels)
        walls = [
            piece for loop in boundary.loops for piece in loop
            if piece.role != "blank"
        ]
        self.assertTrue(all(isinstance(piece, Arc) for piece in walls))
        radii = sorted({round(piece.radius, 6) for piece in walls})
        # The two flanks are concentric with the path; the caps are the cutter.
        self.assertEqual(radii, [0.5, 2.5, 3.5])

    def test_two_channels_that_cross_leave_one_hole(self):
        runs = chain_runs([
            (line(5.0, 5.0, 15.0, 5.0, "a"), RADIUS),
            (line(10.0, 2.0, 10.0, 8.0, "b"), RADIUS),
        ])
        boundary = region_boundary(sheet(), runs.channels)
        self.assertEqual(boundary.loop_count, 2)

    def test_the_channels_never_reach_outside_the_sheet(self):
        runs = chain_runs([(line(-5.0, 5.0, 5.0, 5.0), RADIUS)])
        boundary = region_boundary(sheet(), runs.channels)
        box = boundary.bbox
        self.assertGreaterEqual(round(box[0], 9), 0.0)
        self.assertLessEqual(round(box[2], 9), 20.0)

    def test_every_loop_closes_exactly(self):
        runs = chain_runs([
            (line(5.0, 5.0, 15.0, 5.0, "a"), RADIUS),
            (line(15.0, 5.0, 15.0, 8.0, "b"), RADIUS),
        ])
        boundary = region_boundary(sheet(), runs.channels)
        for loop in boundary.loops:
            for before, after in zip(loop, loop[1:] + loop[:1]):
                self.assertLess(math.dist(before.end, after.start), 1e-9)

    def test_the_same_channels_give_the_same_boundary_whatever_the_order(self):
        items = [
            (line(5.0, 5.0, 15.0, 5.0, "a"), RADIUS),
            (line(5.0, 8.0, 15.0, 8.0, "b"), RADIUS),
        ]
        first = region_boundary(sheet(), chain_runs(items).channels)
        second = region_boundary(sheet(), chain_runs(items[::-1]).channels)
        self.assertEqual(
            [[piece for piece in loop] for loop in first.loops],
            [[piece for piece in loop] for loop in second.loops],
        )

    def test_a_channel_that_swallows_a_corner_still_closes(self):
        runs = chain_runs([(line(-1.0, 3.0, 3.0, -1.0), RADIUS)])
        boundary = region_boundary(sheet(), runs.channels)
        self.assertEqual(boundary.loop_count, 2)
        self.assertTrue(boundary.reaches_outer_edge)


class ChannelShapeTest(unittest.TestCase):
    def test_a_channel_measures_its_own_path(self):
        channel = Channel(path=(line(0.0, 0.0, 3.0, 4.0),), radius=RADIUS,
                          closed=False)
        self.assertAlmostEqual(channel.length_mm, 5.0)
        self.assertEqual(channel.bbox, (0.0, 0.0, 3.0, 4.0))


class InsideTheCutTest(unittest.TestCase):
    """A piece of wall is judged along its whole length, not at its middle.

    The middle alone decides a piece only when every place
    another curve crosses it is a split, and two curves on ONE circle cross
    nowhere. The corner fillet at a toolpath joint is that circle, produced by
    the walk out and by the walk back, so this case is ordinary rather than
    exotic - measured on a camera board, where it cost the whole panel.
    """

    def cut(self, *items_and_radii):
        """The `(items, index)` pair `region_boundary` probes against."""
        items = list(items_and_radii)
        boxes = [padded_bbox(item, radius) for item, radius in items]
        return items, Grid(boxes, index_cell_mm(boxes))

    def test_a_piece_whose_end_is_in_the_cut_is_in_the_cut(self):
        # Channel A is a single point with the whole circle of radius 1 as its
        # wall, so no probe along that circle reads inside it - exactly the
        # corner fillet round a joint. Channel B swallows one end of the arc
        # and reaches nowhere near its middle, which is the arrangement the
        # middle probe alone cannot see.
        items, index = self.cut(
            (Line(p0=(0.0, 0.0), p1=(0.0, 0.0)), 1.0),
            (Line(p0=(1.0, 0.0), p1=(1.0, 0.0)), 0.4),
        )
        reaching_in = Arc(center=(0.0, 0.0), radius=1.0, a0=0.0,
                          a1=math.pi * 1.5, ccw=True)
        self.assertTrue(
            _inside_the_cut(reaching_in, midpoint_of(reaching_in), items,
                            index, 0.0)
        )
        clear = Arc(center=(0.0, 0.0), radius=1.0, a0=math.pi / 2.0,
                    a1=math.pi * 1.5, ccw=True)
        self.assertFalse(
            _inside_the_cut(clear, midpoint_of(clear), items, index, 0.0)
        )

    def test_an_end_on_a_wall_is_not_in_the_cut(self):
        # The end sits where the split put it - ON another channel's wall,
        # within the node tolerance. That is every crossing, and refusing it
        # would throw away the boundary itself.
        channel = (Line(p0=(0.0, 0.0), p1=(10.0, 0.0)), 1.0)
        items, index = self.cut(channel)
        inside = BOUNDARY_NODE_TOL_MM / 2.0
        piece = Line(p0=(5.0, 1.0 - inside), p1=(5.0, 4.0))
        self.assertFalse(
            _inside_the_cut(piece, midpoint_of(piece), items, index, 0.0)
        )

    def test_an_end_well_inside_the_cut_is_in_the_cut(self):
        channel = (Line(p0=(0.0, 0.0), p1=(10.0, 0.0)), 1.0)
        items, index = self.cut(channel)
        piece = Line(p0=(5.0, 0.5), p1=(5.0, 4.0))
        self.assertTrue(
            _inside_the_cut(piece, midpoint_of(piece), items, index, 0.0)
        )


if __name__ == "__main__":
    unittest.main()
