"""The analytic oracle itself: does it say yes to right and no to wrong?

A checker that never fails is worth nothing, so every rule it carries is shown
here failing on geometry built to break it, and passing on the same geometry
put right. The geometry is all assembled in memory - a blank, a channel or two,
and outlines edited by hand - so nothing here needs a board, KiCad, or the
corpus.
"""

import math
import unittest

from eedesignpanels import channels, outline_check
from eedesignpanels.channels import Channel, Rect
from eedesignpanels.toolpath import Arc, Line

BLANK = Rect(0.0, 0.0, 20.0, 20.0)
RADIUS = 0.5


def line(x0, y0, x1, y1, source="cut") -> Line:
    return Line(p0=(x0, y0), p1=(x1, y1), source=source)


def channel(*points, radius=RADIUS) -> Channel:
    path = tuple(
        line(points[index][0], points[index][1],
             points[index + 1][0], points[index + 1][1], f"cut{index}")
        for index in range(len(points) - 1)
    )
    return Channel(path=path, radius=radius, closed=False)


def derived(channel_list, blank=BLANK):
    return channels.region_boundary(blank, list(channel_list))


class ValidOutlineTest(unittest.TestCase):
    """One channel straight across the blank: two pieces of panel, both right."""

    def setUp(self):
        self.channels = [channel((2.0, 10.0), (18.0, 10.0))]
        self.boundary = derived(self.channels)

    def test_the_geometry_is_what_the_test_thinks_it_is(self):
        # The blank, and the slot the channel cut in the middle of it.
        self.assertEqual(self.boundary.loop_count, 2)

    def test_the_checker_passes_it(self):
        report = outline_check.check(
            BLANK, self.boundary.loops, self.channels
        )
        self.assertTrue(report.ok, report.summary)
        self.assertGreater(report.edge_samples, 1000)
        self.assertGreater(report.region_samples, 1000)

    def test_it_passes_with_the_minimum_tab_rules_on_too(self):
        report = outline_check.check(
            BLANK, self.boundary.loops, self.channels, min_tab_mm=0.5
        )
        self.assertTrue(report.ok, report.summary)


class MovedWallTest(unittest.TestCase):
    """A wall nudged off the offset is a cut no cutter made."""

    def setUp(self):
        self.channels = [channel((2.0, 10.0), (18.0, 10.0))]
        self.boundary = derived(self.channels)

    def moved(self, shift: float):
        loops = []
        for loop in self.boundary.loops:
            pieces = []
            for piece in loop:
                if isinstance(piece, Line) and abs(piece.p0[1] - 9.5) < 1e-9 \
                        and abs(piece.p1[1] - 9.5) < 1e-9:
                    piece = piece._replace(
                        p0=(piece.p0[0], piece.p0[1] - shift),
                        p1=(piece.p1[0], piece.p1[1] - shift),
                    )
                pieces.append(piece)
            loops.append(tuple(pieces))
        return tuple(loops)

    def test_a_wall_moved_a_fifth_of_a_millimetre_is_caught(self):
        report = outline_check.check(BLANK, self.moved(0.2), self.channels)
        self.assertFalse(report.ok)
        self.assertIn("off_wall", report.kinds)

    def test_the_report_says_where_in_millimetres(self):
        report = outline_check.check(BLANK, self.moved(0.2), self.channels)
        worst = max(report.of_kind("off_wall"), key=lambda entry: entry.span_mm)
        self.assertAlmostEqual(worst.at[1], 9.3, places=2)
        self.assertGreater(worst.span_mm, 0.1)

    def test_a_wall_moved_a_micrometre_is_not(self):
        """The tolerance is real: welding moves a wall by microns, honestly."""
        report = outline_check.check(BLANK, self.moved(0.001), self.channels)
        self.assertTrue(report.ok, report.summary)


class OpenLoopTest(unittest.TestCase):
    def test_a_loop_with_a_gap_in_it_is_caught(self):
        loop = (
            line(1.0, 1.0, 5.0, 1.0),
            line(5.0, 1.0, 5.0, 5.0),
            line(5.0, 5.0, 1.0, 5.0),
            line(1.0, 5.0, 1.0, 1.2),
        )
        found = outline_check.check_topology([loop])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "open")
        self.assertAlmostEqual(found[0].span_mm, 0.2)


class CrossingTest(unittest.TestCase):
    def test_two_pieces_that_cross_away_from_a_node_are_caught(self):
        loop = (
            line(1.0, 1.0, 5.0, 5.0),
            line(5.0, 5.0, 5.0, 1.0),
            line(5.0, 1.0, 1.0, 5.0),
            line(1.0, 5.0, 1.0, 1.0),
        )
        found = outline_check.check_crossings([loop])
        self.assertTrue(found)
        self.assertEqual(found[0].kind, "crossing")
        self.assertAlmostEqual(found[0].at[0], 3.0, places=6)
        self.assertAlmostEqual(found[0].at[1], 3.0, places=6)

    def test_a_plain_rectangle_has_none(self):
        loop = (
            line(1.0, 1.0, 5.0, 1.0),
            line(5.0, 1.0, 5.0, 5.0),
            line(5.0, 5.0, 1.0, 5.0),
            line(1.0, 5.0, 1.0, 1.0),
        )
        self.assertEqual(outline_check.check_crossings([loop]), [])


class RegionParityTest(unittest.TestCase):
    """The ray caster the checker decides "inside" with, on shapes by hand."""

    SQUARE = (
        line(2.0, 2.0, 8.0, 2.0),
        line(8.0, 2.0, 8.0, 8.0),
        line(8.0, 8.0, 2.0, 8.0),
        line(2.0, 8.0, 2.0, 2.0),
    )

    def test_inside_and_outside_a_square(self):
        region = outline_check.OutlineRegion([self.SQUARE])
        self.assertTrue(region.contains((5.0, 5.0)))
        self.assertFalse(region.contains((1.0, 5.0)))
        self.assertFalse(region.contains((9.0, 5.0)))
        self.assertFalse(region.contains((5.0, 1.0)))

    def test_a_hole_inside_the_square_reads_as_outside(self):
        hole = (
            line(4.0, 4.0, 4.0, 6.0),
            line(4.0, 6.0, 6.0, 6.0),
            line(6.0, 6.0, 6.0, 4.0),
            line(6.0, 4.0, 4.0, 4.0),
        )
        region = outline_check.OutlineRegion([self.SQUARE, hole])
        self.assertTrue(region.contains((3.0, 5.0)))
        self.assertFalse(region.contains((5.0, 5.0)))

    def test_a_ray_level_with_a_vertex_still_gets_an_answer(self):
        """A ray exactly through two vertices has no honest parity; it nudges.

        The answer a hair inside an edge is the inside, and a hair outside is
        the outside - the nudge is micrometres, so it only decides cases that
        were picometres from the line in the first place.
        """
        region = outline_check.OutlineRegion([self.SQUARE])
        self.assertTrue(region.contains((5.0, 2.001)))
        self.assertTrue(region.contains((5.0, 7.999)))
        self.assertFalse(region.contains((5.0, 1.999)))
        self.assertIn(region.contains((5.0, 2.0)), (True, False))

    def test_a_circle_is_met_exactly_and_not_by_segments(self):
        loop = (
            Arc(center=(5.0, 5.0), radius=2.0, a0=0.0, a1=math.pi, ccw=True,
                source="a"),
            Arc(center=(5.0, 5.0), radius=2.0, a0=math.pi, a1=2.0 * math.pi,
                ccw=True, source="b"),
        )
        region = outline_check.OutlineRegion([loop])
        self.assertTrue(region.contains((5.0, 5.0)))
        self.assertTrue(region.contains((6.9, 5.0)))
        self.assertFalse(region.contains((7.1, 5.0)))


class SamplingTest(unittest.TestCase):
    def test_a_point_along_a_line(self):
        piece = line(0.0, 0.0, 4.0, 0.0)
        self.assertEqual(outline_check.point_on(piece, 0.25), (1.0, 0.0))
        self.assertEqual(outline_check.normal_on(piece, 0.5), (0.0, 1.0))

    def test_a_point_along_an_arc_is_on_the_arc(self):
        piece = Arc(center=(0.0, 0.0), radius=2.0, a0=0.0,
                    a1=math.pi / 2.0, ccw=True, source="a")
        for position in (0.0, 0.25, 0.5, 1.0):
            point = outline_check.point_on(piece, position)
            self.assertAlmostEqual(math.hypot(*point), 2.0)
        self.assertAlmostEqual(
            outline_check.point_on(piece, 1.0)[1], 2.0, places=9
        )

    def test_samples_never_land_on_an_end(self):
        piece = line(0.0, 0.0, 1.0, 0.0)
        positions = outline_check.samples_on(piece, 0.02)
        self.assertGreaterEqual(len(positions), 50)
        self.assertGreater(min(positions), 0.0)
        self.assertLess(max(positions), 1.0)

    def test_a_piece_shorter_than_the_step_still_gets_looked_at(self):
        piece = line(0.0, 0.0, 0.001, 0.0)
        self.assertEqual(len(outline_check.samples_on(piece, 0.02)), 3)


class ChipTest(unittest.TestCase):
    """A loose piece of material under the minimum tab is swarf, and is named."""

    RING = Channel(
        path=(
            line(9.75, 9.75, 10.25, 9.75),
            line(10.25, 9.75, 10.25, 10.25),
            line(10.25, 10.25, 9.75, 10.25),
            line(9.75, 10.25, 9.75, 9.75),
        ),
        radius=0.1,
        closed=True,
    )

    def test_a_small_island_of_material_is_a_chip(self):
        """A 0.1 mm bit run round a 0.5 mm square leaves a 0.3 mm island."""
        boundary = derived([self.RING])
        self.assertEqual(boundary.loop_count, 3)
        material = outline_check.Material(
            BLANK, outline_check.sweeps_of([self.RING])
        )
        found, advisories, _thin, _at = outline_check.check_slivers(
            boundary.loops, material, 0.5
        )
        chips = [defect for defect in found if defect.kind == "chip"]
        self.assertEqual(len(chips), 1, [str(entry) for entry in advisories])
        self.assertAlmostEqual(chips[0].at[0], 10.0, places=4)
        self.assertAlmostEqual(chips[0].at[1], 10.0, places=4)
        self.assertAlmostEqual(chips[0].span_mm, 0.3, places=4)

    def test_the_same_island_over_the_minimum_tab_is_not_a_chip(self):
        boundary = derived([self.RING])
        material = outline_check.Material(
            BLANK, outline_check.sweeps_of([self.RING])
        )
        found, _advisories, _thin, _at = outline_check.check_slivers(
            boundary.loops, material, 0.2
        )
        self.assertEqual(
            [defect for defect in found if defect.kind == "chip"], []
        )

    def test_the_big_pieces_of_the_same_panel_are_not_chips(self):
        cuts = [channel((2.0, 10.0), (18.0, 10.0))]
        boundary = derived(cuts)
        material = outline_check.Material(BLANK, outline_check.sweeps_of(cuts))
        found, _advisories, _thin, _at = outline_check.check_slivers(
            boundary.loops, material, 0.5
        )
        self.assertEqual(found, ())


class GuardTest(unittest.TestCase):
    """A chord across a declared tab's two stop lines is a declared gap."""

    class FakeGuard:
        def __init__(self, blades=(), ends=()):
            self.blades = blades
            self.ends = ends

    def test_a_chord_crossing_both_blades_is_guarded(self):
        guard = self.FakeGuard(blades=(
            line(1.0, 0.0, 1.0, 2.0), line(3.0, 0.0, 3.0, 2.0),
        ))
        self.assertTrue(
            outline_check.guarded_here((0.5, 1.0), (3.5, 1.0), [guard])
        )

    def test_a_chord_crossing_only_one_is_not(self):
        guard = self.FakeGuard(blades=(
            line(1.0, 0.0, 1.0, 2.0), line(3.0, 0.0, 3.0, 2.0),
        ))
        self.assertFalse(
            outline_check.guarded_here((0.5, 1.0), (2.0, 1.0), [guard])
        )

    def test_a_guard_named_by_its_two_ends_is_recognised(self):
        guard = self.FakeGuard(ends=((1.0, 1.0), (2.0, 1.0)))
        self.assertTrue(
            outline_check.guarded_here((1.05, 1.0), (1.95, 1.0), [guard])
        )
        self.assertFalse(
            outline_check.guarded_here((8.0, 8.0), (9.0, 9.0), [guard])
        )


class MaterialTest(unittest.TestCase):
    def test_a_point_inside_a_cut_is_not_material(self):
        cuts = [channel((2.0, 10.0), (18.0, 10.0))]
        material = outline_check.Material(BLANK, outline_check.sweeps_of(cuts))
        self.assertFalse(material.is_material((10.0, 10.0)))
        self.assertTrue(material.is_material((10.0, 11.0)))

    def test_a_point_outside_the_blank_is_not_material(self):
        material = outline_check.Material(BLANK, [])
        self.assertFalse(material.is_material((-1.0, 10.0)))
        self.assertTrue(material.is_material((1.0, 10.0)))

    def test_the_clearance_is_the_distance_out_of_the_nearest_cut(self):
        cuts = [channel((2.0, 10.0), (18.0, 10.0))]
        material = outline_check.Material(BLANK, outline_check.sweeps_of(cuts))
        self.assertAlmostEqual(material.clearance_mm((10.0, 11.0)), 0.5)
        self.assertAlmostEqual(material.clearance_mm((10.0, 10.2)), -0.3)


if __name__ == "__main__":
    unittest.main()
