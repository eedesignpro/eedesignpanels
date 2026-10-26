"""Is there material here: parity over the loops, probed half a cutter in.

Every case is one anybody can read off a drawing. A point inside one loop is
material; inside a loop inside that loop it is air; inside a loop inside THAT it
is material again. Nought loops is off the board.

THE CASE THIS FILE EXISTS FOR is `WideArcTest`, and it is real board
geometry rebuilt from nothing: a bulge of 11 mm radius sweeping 280 degrees,
with small cutouts placed symmetrically about the shape's own axis. A test
that walks an arc in chords of at most a right angle turns a 280 degree bulge
into four chords of 70 degrees, and every one of them cuts 1.995 mm of real
material off the shape. On this geometry such a test reads the +-57 degree
pair of cutouts inside the board and the +-123 degree pair outside it, though
the shape and the four cutouts are all symmetric about y = 0. The parity test
reads all four the same.

Pure geometry: no board, no files, no KiCad.
"""

import math
import unittest

from eedesignpanels.material import (
    cutter_centre_at_edge,
    is_material,
    is_material_under_cutter,
    loop_is_hole,
    loops_containing,
    probe_inside_loop,
)
from eedesignpanels.toolpath import (
    Arc,
    Line,
    chain_loop,
    point_inside_loop,
)

CUTTER = 0.889
RADIUS = CUTTER / 2.0

# The bulge, taken from that camera board by its numbers only.
BULGE_RADIUS = 11.0
BULGE_SWEEP_DEG = 280.0
# Where the cutouts sit relative to the arc's centre: far enough out to be in
# the band the chord walk cut away, and symmetric about the shape's axis.
CUTOUT_RADIUS_MM = 9.55
CUTOUT_ANGLES_DEG = (57.0, -57.0, 123.0, -123.0)
CUTOUT_DIAMETER_MM = 1.3


def polygon(points: list) -> list:
    """A closed chain of lines through *points*, in the order given."""
    return [
        Line(p0=points[index], p1=points[(index + 1) % len(points)],
             source=f"l{index}")
        for index in range(len(points))
    ]


def square_loop(side: float, at: tuple = (0.0, 0.0)) -> list:
    return chain_loop(polygon([
        (at[0], at[1]),
        (at[0] + side, at[1]),
        (at[0] + side, at[1] + side),
        (at[0], at[1] + side),
    ]))


def circle_loop(center: tuple, diameter: float, source: str = "circle") -> list:
    """A circle as the two half-arcs KiCad's own outline reader would see."""
    radius = diameter / 2.0
    return chain_loop([
        Arc(center=center, radius=radius, a0=0.0, a1=math.pi, ccw=True,
            source=source),
        Arc(center=center, radius=radius, a0=math.pi, a1=2.0 * math.pi,
            ccw=True, source=source),
    ])


def at_angle(radius: float, degrees: float, center: tuple = (0.0, 0.0)) -> tuple:
    return (center[0] + radius * math.cos(math.radians(degrees)),
            center[1] + radius * math.sin(math.radians(degrees)))


def bulge_loop(radius: float = BULGE_RADIUS,
               sweep_deg: float = BULGE_SWEEP_DEG) -> list:
    """A wide arc closed by one straight line: the lens bulge, on its own axis.

    The arc is travelled clockwise from `+sweep/2` to `-sweep/2`, so the shape
    is symmetric about `y = 0` and so is the chord walk that used to measure it.
    """
    half = math.radians(sweep_deg / 2.0)
    arc = Arc(center=(0.0, 0.0), radius=radius, a0=half, a1=-half, ccw=False,
              source="bulge")
    return chain_loop([arc, Line(p0=arc.end, p1=arc.start, source="flat")])


class InsideOneLoopTest(unittest.TestCase):
    def test_inside_and_outside_a_square(self):
        loop = square_loop(10.0)
        self.assertTrue(point_inside_loop(loop, (5.0, 5.0)))
        self.assertFalse(point_inside_loop(loop, (-1.0, 5.0)))
        self.assertFalse(point_inside_loop(loop, (15.0, 5.0)))

    def test_a_circle_is_measured_as_a_circle(self):
        loop = circle_loop((0.0, 0.0), 10.0)
        self.assertTrue(point_inside_loop(loop, (0.0, 0.0)))
        # 4.9 mm out on the diagonal is inside a circle and outside its chords.
        self.assertTrue(point_inside_loop(loop, at_angle(4.9, 45.0)))
        self.assertFalse(point_inside_loop(loop, at_angle(5.1, 45.0)))

    def test_a_vertex_sitting_exactly_on_the_ray_counts_once(self):
        # KiCad's half-open straddle test, and the reason for it: the ray from
        # each probe here runs +x exactly through the two vertices at y = 3.
        notched = chain_loop(polygon([
            (0.0, 0.0), (8.0, 0.0), (8.0, 3.0), (12.0, 3.0), (12.0, 0.0),
            (20.0, 0.0), (20.0, 10.0), (0.0, 10.0),
        ]))
        self.assertTrue(point_inside_loop(notched, (2.0, 3.0)))
        self.assertTrue(point_inside_loop(notched, (15.0, 3.0)))
        # And the bite itself is outside, from a probe level with its floor.
        self.assertFalse(point_inside_loop(notched, (10.0, 1.0)))


class ParityTest(unittest.TestCase):
    """Odd means material, even - nought included - means air."""

    def setUp(self):
        self.board = square_loop(20.0)
        self.cutout = square_loop(10.0, at=(5.0, 5.0))
        self.island = square_loop(2.0, at=(9.0, 9.0))
        self.loops = [self.board, self.cutout, self.island]

    def test_off_the_board_is_air(self):
        self.assertFalse(is_material(self.loops, (-5.0, -5.0)))

    def test_inside_one_loop_is_material(self):
        self.assertTrue(is_material(self.loops, (2.0, 2.0)))

    def test_inside_a_cutout_is_air(self):
        self.assertFalse(is_material(self.loops, (6.0, 6.0)))

    def test_an_island_in_the_cutout_is_material_again(self):
        self.assertTrue(is_material(self.loops, (10.0, 10.0)))

    def test_the_count_is_what_parity_is_read_from(self):
        self.assertEqual(loops_containing(self.loops, (-5.0, -5.0)), 0)
        self.assertEqual(loops_containing(self.loops, (2.0, 2.0)), 1)
        self.assertEqual(loops_containing(self.loops, (6.0, 6.0)), 2)
        self.assertEqual(loops_containing(self.loops, (10.0, 10.0)), 3)

    def test_a_loop_is_never_its_own_parent(self):
        # KiCad's rule takes the count with the contour itself left out, which
        # is the one term between "even is material" and "odd is material".
        self.assertEqual(loops_containing(self.loops, (10.0, 10.0), ignore=2), 2)
        self.assertEqual(loops_containing(self.loops, (10.0, 10.0), ignore=0), 2)


class HoleTest(unittest.TestCase):
    def setUp(self):
        self.loops = [
            square_loop(20.0),
            square_loop(10.0, at=(5.0, 5.0)),
            square_loop(2.0, at=(9.0, 9.0)),
        ]

    def test_the_boundary_is_not_a_hole(self):
        self.assertFalse(loop_is_hole(self.loops, 0, CUTTER))

    def test_a_loop_inside_the_boundary_is_a_hole(self):
        self.assertTrue(loop_is_hole(self.loops, 1, CUTTER))

    def test_a_loop_inside_a_hole_is_material_again(self):
        self.assertFalse(loop_is_hole(self.loops, 2, CUTTER))

    def test_two_separate_pieces_are_both_top_level(self):
        pieces = [square_loop(10.0), square_loop(10.0, at=(30.0, 0.0))]
        self.assertFalse(loop_is_hole(pieces, 0, CUTTER))
        self.assertFalse(loop_is_hole(pieces, 1, CUTTER))


class CutterProbeTest(unittest.TestCase):
    def test_the_probe_is_half_a_cutter_width_along_the_normal(self):
        seat = cutter_centre_at_edge((5.0, 0.0), (0.0, 1.0), CUTTER)
        self.assertAlmostEqual(seat[0], 5.0, places=12)
        self.assertAlmostEqual(seat[1], RADIUS, places=12)

    def test_a_cutter_of_no_diameter_is_an_error(self):
        for diameter in (0.0, -1.0):
            with self.assertRaises(ValueError) as caught:
                cutter_centre_at_edge((0.0, 0.0), (0.0, 1.0), diameter)
            self.assertIn("cuts nothing", str(caught.exception))

    def test_material_under_the_cutter_on_the_board_side(self):
        loops = [square_loop(20.0)]
        self.assertTrue(
            is_material_under_cutter(loops, (5.0, 0.0), (0.0, 1.0), CUTTER))
        self.assertFalse(
            is_material_under_cutter(loops, (5.0, 0.0), (0.0, -1.0), CUTTER))

    def test_the_cutter_sees_air_inside_a_cutout(self):
        loops = [square_loop(20.0), square_loop(10.0, at=(5.0, 5.0))]
        # Stepping INTO the cutout from its own edge: air, at 0.4445 mm in.
        self.assertFalse(
            is_material_under_cutter(loops, (10.0, 5.0), (0.0, 1.0), CUTTER))
        # ...and stepping the other way is the material around it.
        self.assertTrue(
            is_material_under_cutter(loops, (10.0, 5.0), (0.0, -1.0), CUTTER))

    def test_the_loop_probe_lands_exactly_one_radius_in(self):
        probe = probe_inside_loop(square_loop(10.0), CUTTER)
        self.assertAlmostEqual(probe[0], 5.0, places=12)
        self.assertAlmostEqual(probe[1], RADIUS, places=12)

    def test_the_probe_is_the_same_point_every_time(self):
        loop = bulge_loop()
        self.assertEqual(probe_inside_loop(loop, CUTTER),
                         probe_inside_loop(loop, CUTTER))

    def test_a_feature_smaller_than_the_cutter_still_gets_a_probe_inside(self):
        # The 1.3 mm FPC holes against a 2 mm cutter: half a cutter width would
        # land outside the hole, so the step is halved until it does not.
        hole = circle_loop((0.0, 0.0), CUTOUT_DIAMETER_MM)
        probe = probe_inside_loop(hole, 2.0)
        self.assertTrue(point_inside_loop(hole, probe))
        self.assertLess(math.dist(probe, (0.0, 0.0)), CUTOUT_DIAMETER_MM / 2.0)

    def test_an_empty_loop_encloses_nothing(self):
        with self.assertRaises(ValueError) as caught:
            probe_inside_loop([], CUTTER)
        self.assertIn("encloses nothing", str(caught.exception))

    def test_a_loop_thinner_than_the_tolerance_is_an_error_with_its_place(self):
        sliver = circle_loop((40.0, 0.0), 1e-4)
        with self.assertRaises(ValueError) as caught:
            probe_inside_loop(sliver, CUTTER)
        self.assertIn("40.0000", str(caught.exception))


class WideArcTest(unittest.TestCase):
    """THE REGRESSION. A 280 degree bulge, cutouts symmetric about its axis.

    The chord walk this replaces read the +-57 degree pair as inside the board
    and the +-123 degree pair as outside it, on a shape and a cutout set that
    are both symmetric about `y = 0`. The four are the same thing in four
    places, so four identical answers is the whole assertion.
    """

    def setUp(self):
        self.boundary = bulge_loop()
        self.centres = [
            at_angle(CUTOUT_RADIUS_MM, degrees) for degrees in CUTOUT_ANGLES_DEG
        ]
        self.loops = [self.boundary] + [
            circle_loop(centre, CUTOUT_DIAMETER_MM, f"cutout-{index}")
            for index, centre in enumerate(self.centres)
        ]

    def test_the_shape_really_does_sweep_280_degrees(self):
        self.assertAlmostEqual(math.degrees(self.boundary[0].sweep),
                               BULGE_SWEEP_DEG, places=9)

    def test_every_cutout_centre_is_inside_the_boundary(self):
        for centre in self.centres:
            self.assertTrue(point_inside_loop(self.boundary, centre), centre)

    def test_every_cutout_reads_as_a_hole(self):
        verdicts = [
            loop_is_hole(self.loops, index, CUTTER)
            for index in range(1, len(self.loops))
        ]
        self.assertEqual(verdicts, [True, True, True, True])

    def test_every_cutout_centre_reads_as_air(self):
        for centre in self.centres:
            self.assertFalse(is_material(self.loops, centre), centre)

    def test_the_pairs_symmetric_about_the_axis_agree(self):
        for degrees in (57.0, 123.0):
            above = at_angle(CUTOUT_RADIUS_MM, degrees)
            below = at_angle(CUTOUT_RADIUS_MM, -degrees)
            self.assertEqual(is_material(self.loops, above),
                             is_material(self.loops, below))

    def test_the_whole_bulge_is_material(self):
        # The band the chord walk cut away reaches from 9.005 mm out to the arc
        # itself, so the outer ring is in it at almost every angle: of these 55
        # probes at 10.5 mm, all of them material, the chord walk called 52 air.
        # The inner ring at 8.5 mm is the control - the chord walk got all 55 of
        # those right. Both rings clear the cutouts, which reach from 8.9 mm to
        # 10.2 mm out.
        wrong = [
            (radius, degrees)
            for radius in (8.5, 10.5)
            for degrees in range(-135, 136, 5)
            if not is_material(self.loops, at_angle(radius, float(degrees)))
        ]
        self.assertEqual(wrong, [])

    def test_the_air_outside_the_arc_is_still_air(self):
        for degrees in range(-135, 136, 5):
            point = at_angle(BULGE_RADIUS + 0.5, float(degrees))
            self.assertFalse(is_material(self.loops, point), point)


if __name__ == "__main__":
    unittest.main()
