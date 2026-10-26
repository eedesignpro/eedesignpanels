"""Every closed loop as a routing job, on plain geometry - no board, no KiCad.

What is pinned here is three things, and nothing about how a board file
states them: which side the tool rides, whether
the declared cutter can make the feature at all, and the corners it rounds off
because a round tool cannot cut a square inside corner.
"""

import math
import unittest

from eedesignpanels import cuttability, material
from eedesignpanels.toolpath import (
    Arc,
    DoesNotFit,
    Line,
    chain_bbox,
    fillet_corners,
    resolve_offset,
    signed_area,
    weld_chain,
)

CUTTER = 2.0


def rectangle(x0: float, y0: float, x1: float, y1: float, tag: str = "r",
              clockwise: bool = False) -> list:
    """The four sides of a rectangle as a closed chain."""
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if clockwise:
        points = points[::-1]
    return [
        Line(p0=points[index], p1=points[(index + 1) % 4],
             source=f"{tag}{index}", origin=index)
        for index in range(4)
    ]


def circle(centre: tuple, radius: float, tag: str = "c") -> list:
    """A circle as the two half-arcs `outline_shapes` reads one as."""
    return [
        Arc(center=centre, radius=radius, a0=0.0, a1=math.pi, ccw=True,
            source=f"{tag}0", origin=0),
        Arc(center=centre, radius=radius, a0=math.pi, a1=2.0 * math.pi,
            ccw=True, source=f"{tag}1", origin=1),
    ]


def shouldered_board(inside_mm: float, outside_mm: float = 0.75,
                     tag: str = "s") -> list:
    """A board edge that steps by two TANGENT arcs, as a real camera board does.

    A REAL SHOULDER, TO THE MILLIMETRE. The top edge runs left,
    turns through a CONCAVE arc of *inside_mm*, turns back through a CONVEX arc
    of *outside_mm*, and carries on left one step higher. Every joint is
    tangent, so no vertex of this loop turns by more than a microradian - so a
    check that looks only at vertices misses it: the corner IS the concave
    arc, and the reader sees a corner rounded to the tool.
    """
    step = inside_mm + outside_mm
    return [
        Line(p0=(20.0, 0.0), p1=(4.0, 0.0), source=f"{tag}0", origin=0),
        Arc(center=(4.0, -inside_mm), radius=inside_mm,
            a0=math.pi / 2.0, a1=math.pi, ccw=True, source=f"{tag}1", origin=1),
        Arc(center=(4.0 - step, -inside_mm), radius=outside_mm,
            a0=0.0, a1=-math.pi / 2.0, ccw=False, source=f"{tag}2", origin=2),
        Line(p0=(4.0 - step, -step), p1=(0.0, -step),
             source=f"{tag}3", origin=3),
        Line(p0=(0.0, -step), p1=(0.0, 8.0), source=f"{tag}4", origin=4),
        Line(p0=(0.0, 8.0), p1=(20.0, 8.0), source=f"{tag}5", origin=5),
        Line(p0=(20.0, 8.0), p1=(20.0, 0.0), source=f"{tag}6", origin=6),
    ]


class InwardOffsetTest(unittest.TestCase):
    """`resolve_offset(..., inward=True)` is the erosion, not the dilation."""

    def test_a_square_erodes_by_the_radius_on_every_side(self):
        eroded = resolve_offset(rectangle(0.0, 0.0, 10.0, 10.0), 1.0,
                                inward=True)
        self.assertEqual(
            [round(value, 6) for value in chain_bbox(eroded.loop)],
            [1.0, 1.0, 9.0, 9.0],
        )

    def test_the_answer_does_not_depend_on_the_drawn_direction(self):
        one = resolve_offset(rectangle(0.0, 0.0, 10.0, 10.0), 1.0, inward=True)
        other = resolve_offset(
            rectangle(0.0, 0.0, 10.0, 10.0, clockwise=True), 1.0, inward=True)
        self.assertEqual(
            [round(v, 6) for v in chain_bbox(one.loop)],
            [round(v, 6) for v in chain_bbox(other.loop)],
        )

    def test_the_outward_offset_is_untouched(self):
        grown = resolve_offset(rectangle(0.0, 0.0, 10.0, 10.0), 1.0)
        self.assertEqual(
            [round(value, 6) for value in chain_bbox(grown.loop)],
            [-1.0, -1.0, 11.0, 11.0],
        )

    def test_a_circle_erodes_to_a_smaller_circle(self):
        eroded = resolve_offset(circle((0.0, 0.0), 3.0), 1.0, inward=True)
        self.assertEqual(
            [round(value, 6) for value in chain_bbox(eroded.loop)],
            [-2.0, -2.0, 2.0, 2.0],
        )

    def test_a_feature_smaller_than_the_cutter_says_it_does_not_fit(self):
        with self.assertRaises(DoesNotFit):
            resolve_offset(circle((0.0, 0.0), 0.65), 1.0, inward=True)


class FilletCornerTest(unittest.TestCase):
    """Which vertices come out rounded to the tool, and which do not."""

    def orientation(self, loop, rides_inside: bool) -> int:
        return cuttability.ride_orientation(loop, rides_inside)

    def test_a_square_board_rounds_no_corner(self):
        loop = weld_chain(rectangle(0.0, 0.0, 10.0, 10.0))
        self.assertEqual(
            fillet_corners(loop, 1.0, self.orientation(loop, False)), [])

    def test_a_square_hole_rounds_all_four(self):
        loop = weld_chain(rectangle(0.0, 0.0, 10.0, 10.0))
        self.assertEqual(
            len(fillet_corners(loop, 1.0, self.orientation(loop, True))), 4)

    def test_the_fillet_left_is_the_cutter_radius(self):
        loop = weld_chain(rectangle(0.0, 0.0, 10.0, 10.0))
        corner = fillet_corners(loop, 1.0, self.orientation(loop, True))[0]
        self.assertEqual(corner.radius_mm, 1.0)

    def test_a_notch_in_a_board_edge_rounds_its_reflex_corner(self):
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (5.0, 10.0),
                  (5.0, 5.0), (0.0, 5.0)]
        loop = weld_chain([
            Line(p0=points[index], p1=points[(index + 1) % len(points)],
                 source=f"n{index}", origin=index)
            for index in range(len(points))
        ])
        rounded = fillet_corners(loop, 1.0, self.orientation(loop, False))
        self.assertEqual(len(rounded), 1)
        self.assertEqual(rounded[0].at, (5.0, 5.0))


class TangentArcCornerTest(unittest.TestCase):
    """An inside corner drawn as an ARC, as real boards draw it.

    A shoulder that is a 0.25 mm concave arc joined tangentially at both ends
    shows a vertex test a turn of 0.01 degrees, which reads as noise. A larger
    cutter still rounds that corner to its own radius, so the arc itself has
    to be judged, and a corner tighter than the bit turns red.
    """

    def orientation(self, loop, rides_inside: bool = False) -> int:
        return cuttability.ride_orientation(loop, rides_inside)

    def loop(self, inside_mm: float):
        return weld_chain(shouldered_board(inside_mm))

    def test_no_vertex_of_the_step_turns_enough_to_be_a_corner(self):
        # The premise of the defect, measured rather than asserted: the step's
        # three joints are tangent, so nothing a vertex test can see is there to
        # be seen. The board's own outer corners are square and convex, and the
        # tool wraps round the outside of those.
        loop = self.loop(0.25)
        for index in (0, 1, 2):
            item = loop[index]
            following = loop[(index + 1) % len(loop)]
            before = item.tangent_at_end()
            after = following.tangent_at_start()
            turn = math.atan2(
                before[0] * after[1] - before[1] * after[0],
                before[0] * after[0] + before[1] * after[1],
            )
            self.assertLess(abs(math.degrees(turn)), 0.02, f"vertex {index}")

    def test_a_tangent_concave_arc_tighter_than_the_tool_is_a_corner(self):
        loop = self.loop(0.25)
        rounded = fillet_corners(loop, 1.0, self.orientation(loop))
        self.assertEqual(len(rounded), 1)
        self.assertEqual(rounded[0].radius_mm, 1.0)

    def test_the_corner_is_named_on_the_arc_itself(self):
        # One source, not a pair: the corner IS the arc, and `at` is its middle
        # so a red circle on the picture sits where the material stands.
        loop = self.loop(0.25)
        corner = fillet_corners(loop, 1.0, self.orientation(loop))[0]
        self.assertEqual(corner.sources, ("s1",))
        self.assertEqual(
            [round(value, 6) for value in corner.at],
            [round(value, 6) for value in (4.0 - 0.25 * math.sqrt(0.5),
                                           -0.25 + 0.25 * math.sqrt(0.5))],
        )

    def test_the_arc_turns_the_way_the_loop_travels(self):
        # Same sign convention as the vertex case: an inside corner is one the
        # outline turns AWAY from the side the tool rides.
        loop = self.loop(0.25)
        orientation = self.orientation(loop)
        corner = fillet_corners(loop, 1.0, orientation)[0]
        self.assertAlmostEqual(abs(corner.turn_deg), 90.0, places=6)
        self.assertLess(orientation * corner.turn_deg, 0.0)

    def test_a_concave_arc_wider_than_the_tool_is_not_a_corner(self):
        # The bit reaches all the way in, so there is nothing to report and
        # nothing to paint red.
        loop = self.loop(2.0)
        self.assertEqual(fillet_corners(loop, 1.0, self.orientation(loop)), [])

    def test_his_shoulder_at_his_cutter(self):
        # The measured case: 0.5838 mm inside radius, 1.2 mm cutter.
        loop = self.loop(0.5838)
        self.assertEqual(
            len(fillet_corners(loop, 0.6, self.orientation(loop))), 1)
        self.assertEqual(
            fillet_corners(loop, 0.5, self.orientation(loop)), [])

    def test_the_corner_is_red_and_the_board_still_routes(self):
        loops = [shouldered_board(0.25)]
        jobs = cuttability.plan(loops, 0, 2.0)
        self.assertTrue(jobs[0].made)
        self.assertEqual(len(jobs[0].rounded), 1)
        self.assertEqual([job.label for job in cuttability.marked(jobs)],
                         ["the board boundary"])
        self.assertEqual(len(cuttability.alarms(jobs)), 1)


class RideSideTest(unittest.TestCase):
    """The side the tool rides on is asserted against the material, not assumed."""

    def loops(self):
        return [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                circle((10.0, 10.0), 3.0, tag="h")]

    def test_a_hole_is_a_hole_and_the_boundary_is_not(self):
        loops = self.loops()
        self.assertFalse(material.loop_is_hole(loops, 0, CUTTER))
        self.assertTrue(material.loop_is_hole(loops, 1, CUTTER))

    def test_the_probes_straddle_the_edge(self):
        loops = self.loops()
        self.assertEqual(cuttability.straddles(loops, 0, False, CUTTER), "")
        self.assertEqual(cuttability.straddles(loops, 1, True, CUTTER), "")

    def test_riding_the_wrong_side_is_refused_by_name(self):
        loops = self.loops()
        said = cuttability.straddles(loops, 1, False, CUTTER)
        self.assertIn("wrong side", said)

    def test_the_ride_orientation_is_the_loop_s_own_when_riding_outside(self):
        loop = rectangle(0.0, 0.0, 10.0, 10.0)
        turn = 1 if signed_area(loop) > 0.0 else -1
        self.assertEqual(cuttability.ride_orientation(loop, False), turn)
        self.assertEqual(cuttability.ride_orientation(loop, True), -turn)


class PlanTest(unittest.TestCase):
    """One job per loop, numbered, judged, and reported the same way twice."""

    def loops(self, hole_radius: float = 3.0, holes: int = 1):
        return [rectangle(0.0, 0.0, 40.0, 20.0, tag="b")] + [
            circle((8.0 + 6.0 * index, 10.0), hole_radius, tag=f"h{index}")
            for index in range(holes)
        ]

    def test_every_loop_becomes_a_job(self):
        jobs = cuttability.plan(self.loops(holes=3), 0, CUTTER)
        self.assertEqual(len(jobs), 4)
        self.assertEqual(jobs[0].kind, cuttability.BOUNDARY)
        self.assertTrue(all(job.kind == cuttability.HOLE for job in jobs[1:]))

    def test_the_holes_are_numbered_from_one(self):
        jobs = cuttability.plan(self.loops(holes=3), 0, CUTTER)
        self.assertEqual([job.label for job in jobs[1:]],
                         ["hole 1", "hole 2", "hole 3"])

    def test_the_boundary_gets_no_path_of_its_own(self):
        jobs = cuttability.plan(self.loops(), 0, CUTTER)
        self.assertEqual(jobs[0].path, ())
        self.assertTrue(jobs[0].made)

    def test_a_hole_the_cutter_fits_is_cut_and_carries_its_path(self):
        job = cuttability.plan(self.loops(), 0, CUTTER)[1]
        self.assertEqual(job.verdict, cuttability.CUT)
        self.assertGreater(job.path_length_mm, 0.0)

    def test_a_hole_the_cutter_does_not_fit_is_not_made_and_says_why(self):
        job = cuttability.plan(self.loops(hole_radius=0.65), 0, CUTTER)[1]
        self.assertEqual(job.verdict, cuttability.NOT_MADE)
        self.assertEqual(job.path, ())
        self.assertIn("does not fit inside it", job.reason)

    def test_the_verdict_carries_the_size_and_the_centre(self):
        job = cuttability.plan(self.loops(hole_radius=0.65), 0, CUTTER)[1]
        self.assertIn("1.3000 by 1.3000 mm", job.where)
        self.assertIn("(8.0000, 10.0000) mm", job.where)

    def test_the_same_loops_plan_the_same_way_twice(self):
        first = cuttability.plan(self.loops(holes=3), 0, CUTTER)
        second = cuttability.plan(self.loops(holes=3), 0, CUTTER)
        self.assertEqual([job.label for job in first],
                         [job.label for job in second])
        self.assertEqual([job.verdict for job in first],
                         [job.verdict for job in second])


class ReportTest(unittest.TestCase):
    """What the report says, and what turns red."""

    def loops(self, hole_radius: float):
        return [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                circle((10.0, 10.0), hole_radius, tag="h")]

    def test_a_board_whose_features_all_cut_raises_no_alarm(self):
        jobs = cuttability.plan(self.loops(3.0), 0, CUTTER)
        self.assertEqual(cuttability.alarms(jobs), ())

    def test_a_feature_the_cutter_cannot_make_is_one_alarm(self):
        jobs = cuttability.plan(self.loops(0.65), 0, CUTTER)
        said = cuttability.alarms(jobs)
        self.assertEqual(len(said), 1)
        self.assertIn("hole 1 cannot be cut", said[0])

    def test_the_report_counts_what_was_cut_and_what_was_not(self):
        lines = cuttability.report_lines(
            cuttability.plan(self.loops(0.65), 0, CUTTER), CUTTER)
        self.assertIn("1 cut, 1 left uncut", lines[0])
        self.assertTrue(any("is NOT cut" in line for line in lines))

    def square_hole(self):
        return [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                rectangle(8.0, 6.0, 16.0, 14.0, tag="h")]

    def test_a_rounded_corner_is_reported(self):
        jobs = cuttability.plan(self.square_hole(), 0, CUTTER)
        self.assertEqual(len(jobs[1].rounded), 4)
        self.assertTrue(any("rounded to the tool" in line for line
                            in cuttability.report_lines(jobs, CUTTER)))

    def test_a_corner_tighter_than_the_bit_is_red(self):
        # A square inside corner cannot be cut by a round bit, so it is red -
        # four corners on every square hole.
        said = cuttability.alarms(cuttability.plan(self.square_hole(), 0, CUTTER))
        self.assertEqual(len(said), 4)

    def test_a_corner_alarm_names_its_place_and_its_radius(self):
        said = cuttability.alarms(cuttability.plan(self.square_hole(), 0, CUTTER))
        # ONE PLAIN SENTENCE: "cutter too large to
        # cut feature at (X,Y)", with the radius the fab actually receives.
        self.assertTrue(all("cutter too large to cut feature at" in line
                            for line in said))
        self.assertTrue(all(f"rounded to {CUTTER / 2.0:.4f} mm" in line
                            for line in said))
        self.assertIn("(8.0000, 6.0000) mm", " ".join(said))

    def test_a_corner_alarm_ends_with_the_radius_the_fab_receives(self):
        # The sentence is "cutter too large to cut feature at (X,Y) - it comes
        # out rounded to R mm", so it ends with the radius the fab receives.
        said = cuttability.alarms(cuttability.plan(self.square_hole(), 0, CUTTER))
        self.assertTrue(all(line.endswith(f"rounded to {CUTTER / 2.0:.4f} mm")
                            for line in said))

    def test_an_uncut_feature_comes_before_the_corners(self):
        loops = [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                 rectangle(8.0, 6.0, 16.0, 14.0, tag="h"),
                 circle((30.0, 10.0), 0.65, tag="s")]
        said = cuttability.alarms(cuttability.plan(loops, 0, CUTTER))
        self.assertIn("cannot be cut,", said[0])
        self.assertEqual(len(said), 5)

    def test_marked_holds_both_kinds_and_nothing_else(self):
        loops = [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                 rectangle(8.0, 6.0, 16.0, 14.0, tag="h"),
                 circle((30.0, 10.0), 3.0, tag="w")]
        jobs = cuttability.plan(loops, 0, CUTTER)
        self.assertEqual([job.label for job in cuttability.marked(jobs)],
                         ["hole 1"])

    def test_a_cutout_wider_than_the_cutter_says_it_leaves_a_slug(self):
        # Following the wall of a big cutout leaves the middle attached to
        # nothing. With pocketing off, which is the default, the slug is
        # reported.
        job = cuttability.plan(self.loops(3.0), 0, CUTTER)[1]
        self.assertTrue(job.leaves_slug)
        self.assertAlmostEqual(job.slug_mm[0], 2.0, places=6)
        self.assertTrue(any("loose piece" in line for line
                            in cuttability.report_lines(
                                cuttability.plan(self.loops(3.0), 0, CUTTER),
                                CUTTER)))

    def test_a_cutout_the_bit_covers_leaves_nothing_behind(self):
        job = cuttability.plan(self.loops(1.2), 0, CUTTER)[1]
        self.assertTrue(job.made)
        self.assertFalse(job.leaves_slug)


class PocketTest(unittest.TestCase):
    """The checkbox, OFF by default."""

    def loops(self, hole_radius: float = 3.0):
        return [rectangle(0.0, 0.0, 40.0, 20.0, tag="b"),
                circle((10.0, 10.0), hole_radius, tag="h")]

    def test_off_is_the_default_and_leaves_the_slug(self):
        job = cuttability.plan(self.loops(), 0, CUTTER)[1]
        self.assertEqual(job.pocket, ())
        self.assertFalse(job.pocketed)
        self.assertTrue(job.leaves_slug)

    def test_off_plans_exactly_what_it_planned_before_the_box_existed(self):
        without = cuttability.plan(self.loops(), 0, CUTTER)
        asked = cuttability.plan(self.loops(), 0, CUTTER, pocket=False)
        self.assertEqual(without, asked)

    def test_on_mills_the_middle_out_and_leaves_no_slug(self):
        job = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)[1]
        self.assertTrue(job.pocketed)
        self.assertFalse(job.leaves_slug)
        self.assertGreater(len(job.pocket), 0)

    def test_on_changes_nothing_about_the_wall_pass(self):
        # The checkbox adds passes. It does not move the one that was there.
        left = cuttability.plan(self.loops(), 0, CUTTER)[1]
        milled = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)[1]
        self.assertEqual(left.path, milled.path)
        self.assertEqual(left.slug_mm, milled.slug_mm)

    def test_a_cutout_the_bit_covers_gets_no_passes_either_way(self):
        job = cuttability.plan(self.loops(1.2), 0, CUTTER, pocket=True)[1]
        self.assertEqual(job.pocket, ())

    def test_the_boundary_is_never_pocketed(self):
        jobs = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)
        self.assertEqual(jobs[0].pocket, ())

    def test_the_passes_step_in_by_one_radius_each(self):
        passes, note = cuttability.pocket_passes(
            circle((10.0, 10.0), 3.0), CUTTER)
        self.assertEqual(note, "")
        radii = [round(chain_bbox(one)[2] - 10.0, 6) for one in passes]
        self.assertEqual(radii, [1.0])

    def test_every_pass_is_inside_the_one_before_it(self):
        passes, _note = cuttability.pocket_passes(
            circle((10.0, 10.0), 6.0), CUTTER)
        widths = [chain_bbox(one)[2] - chain_bbox(one)[0] for one in passes]
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_the_last_pass_reaches_the_middle(self):
        # Coverage is the point: the deepest pass plus the cutter radius has to
        # reach the deepest material, or a rib stands in the middle of the
        # cutout. Deepest material in a circle is its own centre.
        radius = 6.0
        passes, _note = cuttability.pocket_passes(
            circle((10.0, 10.0), radius), CUTTER)
        depth = radius - (chain_bbox(passes[-1])[2] - 10.0)
        self.assertGreaterEqual(depth + CUTTER / 2.0, radius - 1e-9)

    def test_the_same_loops_pocket_the_same_way_twice(self):
        first = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)
        second = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)
        self.assertEqual(first, second)

    def test_the_report_says_which_cutouts_were_pocketed(self):
        lines = cuttability.report_lines(
            cuttability.plan(self.loops(), 0, CUTTER, pocket=True), CUTTER)
        self.assertTrue(any("pocketed out in" in line for line in lines))
        self.assertFalse(any("leaves a loose piece" in line for line in lines))

    def test_the_report_says_which_were_left(self):
        lines = cuttability.report_lines(
            cuttability.plan(self.loops(), 0, CUTTER), CUTTER)
        self.assertTrue(any("leaves a loose piece" in line for line in lines))
        self.assertFalse(any("pocketed out in" in line for line in lines))

    def test_pocketing_raises_no_alarm_of_its_own(self):
        # A milled-out cutout is a cutout that came out right. Red is for what
        # the cutter cannot make.
        jobs = cuttability.plan(self.loops(), 0, CUTTER, pocket=True)
        self.assertEqual(cuttability.alarms(jobs), ())

    def test_no_jobs_at_all_says_nothing(self):
        self.assertEqual(cuttability.report_lines((), CUTTER), [])


if __name__ == "__main__":
    unittest.main()
