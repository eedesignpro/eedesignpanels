"""The minimum tab, checked against the arithmetic it is defined by.

Every case here is one anybody can work out on paper: material between two cuts
is the centreline gap less both cutter radii, so a 1.0 mm gap between two
0.5 mm-radius cuts leaves nothing at all, and 1.5 mm leaves exactly half a
millimetre. The cases that earn their keep are the ones on the threshold - the
exact tangency that used to deadlock the panel boundary, and the near miss a
hair above it that must be left alone - and the ones where the arithmetic is
right and the answer is still no: a gap the design declared, and a gap that
looks like material until you notice the cutter has already been there.

Pure geometry throughout, plus two stage-level cases that check the rule is
wired to the config rather than to a constant.
"""

import math
import unittest

from eedesignpanels import channels, mintab
from eedesignpanels.channels import Channel, Rect, chain_runs
from eedesignpanels.config import CONFIG_REVISION, ConfigError, PanelConfig
from eedesignpanels.mintab import (
    MIN_TAB_DEFAULT_MM,
    MIN_TAB_FLOOR_MM,
    Guard,
    Join,
    approaches,
    checked_min_tab,
    cuts_of,
    guarded,
    join_run_ends,
    join_warning,
    material_index,
    material_mm,
    protected_tabs,
    protection_warning,
    standing_at,
)
from eedesignpanels.stages import panelize, route_toolpath
from eedesignpanels.toolpath import (
    Arc,
    Line,
    arc_from_three_points,
    closest_approach,
    distance_to_primitive,
    midpoint_of,
    nearest_point_on,
)

from test_route_toolpath import (
    CUTTER,
    RADIUS,
    square_board,
    tab_footprint,
    toolpath_items,
)

R = 0.5


def line(x0, y0, x1, y1, source="a"):
    return Line(p0=(x0, y0), p1=(x1, y1), source=source)


def at_param(primitive, along: float) -> tuple:
    """A point a fraction of the way along a primitive - for brute force only."""
    if isinstance(primitive, Line):
        return (
            primitive.p0[0] + (primitive.p1[0] - primitive.p0[0]) * along,
            primitive.p0[1] + (primitive.p1[1] - primitive.p0[1]) * along,
        )
    sign = 1.0 if primitive.ccw else -1.0
    return primitive.point_at_angle(primitive.a0 + sign * primitive.sweep * along)


def sampled_gap(one, other, steps: int = 400) -> float:
    """The closest approach found by sampling both curves. The slow oracle."""
    first = [at_param(one, index / steps) for index in range(steps + 1)]
    second = [at_param(other, index / steps) for index in range(steps + 1)]
    return min(math.dist(a, b) for a in first for b in second)


def straight_channel(x0, x1, y=0.0, radius=R) -> Channel:
    """One straight routed run, built the way the panel reads it off the board."""
    return chain_runs([(line(x0, y, x1, y), radius)]).channels[0]


class MaterialTest(unittest.TestCase):
    def test_the_material_is_the_gap_less_both_radii(self):
        self.assertAlmostEqual(material_mm(1.5, 0.5, 0.5), 0.5)
        self.assertAlmostEqual(material_mm(1.0, 0.5, 0.5), 0.0)
        self.assertAlmostEqual(material_mm(1.709, 0.4445, 0.4445), 0.82)

    def test_one_whole_cutter_diameter_goes_when_one_bit_made_both_cuts(self):
        for gap in (1.0, 2.0, 3.5):
            self.assertAlmostEqual(
                material_mm(gap, RADIUS, RADIUS), gap - CUTTER
            )

    def test_two_different_bits_each_take_their_own_radius(self):
        self.assertAlmostEqual(material_mm(2.0, 0.25, 0.75), 1.0)

    def test_overlapping_cuts_leave_negative_material(self):
        self.assertLess(material_mm(0.6, 0.5, 0.5), 0.0)


class FloorTest(unittest.TestCase):
    def test_the_default_is_half_a_millimetre(self):
        self.assertEqual(MIN_TAB_DEFAULT_MM, 0.5)

    def test_the_floor_is_a_tenth_of_a_millimetre(self):
        self.assertEqual(MIN_TAB_FLOOR_MM, 0.1)

    def test_a_value_at_the_floor_is_honoured(self):
        self.assertEqual(checked_min_tab(MIN_TAB_FLOOR_MM), MIN_TAB_FLOOR_MM)

    def test_a_value_under_the_floor_is_refused_and_says_why(self):
        with self.assertRaises(ValueError) as caught:
            checked_min_tab(0.05)
        message = str(caught.exception)
        self.assertIn("0.0500", message)
        self.assertIn("floor", message)

    def test_zero_is_refused_rather_than_meaning_no_minimum(self):
        with self.assertRaises(ValueError):
            checked_min_tab(0.0)


class ConfigTest(unittest.TestCase):
    def config(self, **fields) -> PanelConfig:
        return PanelConfig(
            source_board="../board.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 0.0},
            **fields,
        )

    def test_a_config_that_says_nothing_takes_the_default(self):
        self.assertEqual(self.config().min_tab_mm, MIN_TAB_DEFAULT_MM)

    def test_the_value_is_adjustable_per_config(self):
        self.assertEqual(self.config(min_tab_mm=0.8).min_tab_mm, 0.8)

    def test_a_value_under_the_floor_is_a_config_error(self):
        with self.assertRaises(Exception) as caught:
            self.config(min_tab_mm=0.05)
        self.assertIn("floor", str(caught.exception))

    def test_the_floor_error_reaches_load_config_as_a_config_error(self):
        import json
        import tempfile
        from pathlib import Path

        from eedesignpanels.config import load_config

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            board = root / "board.kicad_pcb"
            board.write_text("(kicad_pcb)", encoding="utf-8")
            panel_dir = root / "panel"
            panel_dir.mkdir()
            config_path = panel_dir / "panel.json"
            config_path.write_text(json.dumps({
                "revision": CONFIG_REVISION,
                "source_board": "../board.kicad_pcb",
                "grid": {"columns": 1, "rows": 1, "gap_mm": 0.0},
                "min_tab_mm": 0.09,
            }), encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(config_path)
        self.assertIn("floor", str(caught.exception))


class NearestPointTest(unittest.TestCase):
    def test_a_foot_inside_a_segment_is_the_perpendicular(self):
        self.assertEqual(
            nearest_point_on(line(0.0, 0.0, 10.0, 0.0), (4.0, 3.0)), (4.0, 0.0)
        )

    def test_a_foot_off_the_end_clamps_to_the_end(self):
        self.assertEqual(
            nearest_point_on(line(0.0, 0.0, 10.0, 0.0), (-5.0, 1.0)), (0.0, 0.0)
        )

    def test_a_point_within_an_arcs_span_lands_on_the_arc(self):
        arc = Arc(center=(0.0, 0.0), radius=2.0, a0=0.0, a1=math.pi / 2.0,
                  ccw=True)
        found = nearest_point_on(arc, (3.0, 3.0))
        self.assertAlmostEqual(math.hypot(*found), 2.0)
        self.assertAlmostEqual(found[0], found[1])

    def test_a_point_outside_the_span_lands_on_the_nearer_end(self):
        arc = Arc(center=(0.0, 0.0), radius=2.0, a0=0.0, a1=math.pi / 2.0,
                  ccw=True)
        found = nearest_point_on(arc, (5.0, -5.0))
        self.assertAlmostEqual(found[0], 2.0)
        self.assertAlmostEqual(found[1], 0.0)

    def test_the_centre_of_an_arc_gets_a_stated_answer(self):
        arc = Arc(center=(1.0, 1.0), radius=2.0, a0=0.0, a1=math.pi, ccw=True)
        self.assertEqual(nearest_point_on(arc, (1.0, 1.0)), arc.start)


class ClosestApproachTest(unittest.TestCase):
    def assert_agrees_with_sampling(self, one, other, places=3):
        _, _, gap = closest_approach(one, other)
        self.assertAlmostEqual(gap, sampled_gap(one, other), places=places)

    def test_two_parallel_segments_are_their_perpendicular_apart(self):
        first, second = line(0.0, 0.0, 10.0, 0.0), line(0.0, 3.0, 10.0, 3.0)
        at, to, gap = closest_approach(first, second)
        self.assertAlmostEqual(gap, 3.0)
        self.assertAlmostEqual(at[0], to[0])

    def test_a_family_of_equal_answers_comes_out_the_same_every_time(self):
        first, second = line(0.0, 0.0, 10.0, 0.0), line(0.0, 3.0, 10.0, 3.0)
        answers = {closest_approach(first, second) for _ in range(5)}
        self.assertEqual(len(answers), 1)
        # ...and the same however the two are handed over.
        one, two, gap = closest_approach(second, first)
        self.assertAlmostEqual(gap, 3.0)

    def test_a_tied_family_is_answered_in_the_middle_not_at_an_end(self):
        """A join placed at a run's own endpoint has no way through."""
        at, to, gap = closest_approach(
            line(0.0, 0.0, 10.0, 0.0), line(0.0, 3.0, 10.0, 3.0)
        )
        self.assertAlmostEqual(at[0], 5.0)
        self.assertAlmostEqual(to[0], 5.0)
        self.assertAlmostEqual(gap, 3.0)

    def test_a_partly_overlapping_tied_family_is_answered_in_its_own_middle(self):
        at, to, gap = closest_approach(
            line(0.0, 0.0, 10.0, 0.0), line(6.0, 3.0, 20.0, 3.0)
        )
        self.assertAlmostEqual(gap, 3.0)
        self.assertAlmostEqual(at[0], 8.0)
        self.assertAlmostEqual(to[0], 8.0)

    def test_two_concentric_arcs_are_answered_on_their_angular_middle(self):
        first = Arc(center=(0.0, 0.0), radius=3.0, a0=0.0, a1=1.0, ccw=True)
        second = Arc(center=(0.0, 0.0), radius=5.0, a0=0.0, a1=1.0, ccw=True)
        at, to, gap = closest_approach(first, second)
        self.assertAlmostEqual(gap, 2.0)
        self.assertAlmostEqual(math.atan2(at[1], at[0]), 0.5)
        self.assertAlmostEqual(math.atan2(to[1], to[0]), 0.5)

    def test_a_unique_answer_is_not_moved_by_the_tie_rule(self):
        at, to, gap = closest_approach(
            line(0.0, 0.0, 4.0, 0.0), line(5.0, 0.0, 9.0, 0.0)
        )
        self.assertEqual((at, to), ((4.0, 0.0), (5.0, 0.0)))
        self.assertAlmostEqual(gap, 1.0)

    def test_two_crossing_segments_touch(self):
        at, to, gap = closest_approach(
            line(0.0, 0.0, 10.0, 0.0), line(5.0, -2.0, 5.0, 2.0)
        )
        self.assertEqual(gap, 0.0)
        self.assertEqual(at, to)
        self.assertAlmostEqual(at[0], 5.0)

    def test_two_end_to_end_segments_are_their_gap_apart(self):
        at, to, gap = closest_approach(
            line(0.0, 0.0, 4.0, 0.0), line(5.0, 0.0, 9.0, 0.0)
        )
        self.assertAlmostEqual(gap, 1.0)
        self.assertEqual(at, (4.0, 0.0))
        self.assertEqual(to, (5.0, 0.0))

    def test_a_segment_and_an_arc_agree_with_sampling(self):
        arc = Arc(center=(5.0, 5.0), radius=2.0, a0=math.pi, a1=0.0, ccw=False)
        self.assert_agrees_with_sampling(line(0.0, 0.0, 10.0, 0.0), arc)

    def test_a_segment_facing_an_arcs_far_side_agrees_with_sampling(self):
        arc = Arc(center=(5.0, 5.0), radius=2.0, a0=0.0, a1=math.pi, ccw=True)
        self.assert_agrees_with_sampling(line(0.0, 0.0, 10.0, 0.0), arc)

    def test_two_arcs_agree_with_sampling(self):
        first = Arc(center=(0.0, 0.0), radius=3.0, a0=-1.0, a1=1.0, ccw=True)
        second = Arc(center=(8.0, 0.0), radius=2.0, a0=math.pi - 1.0,
                     a1=math.pi + 1.0, ccw=False)
        self.assert_agrees_with_sampling(first, second)

    def test_two_arcs_on_the_same_centre_agree_with_sampling(self):
        first = Arc(center=(0.0, 0.0), radius=3.0, a0=0.0, a1=1.0, ccw=True)
        second = Arc(center=(0.0, 0.0), radius=5.0, a0=0.2, a1=0.8, ccw=True)
        self.assert_agrees_with_sampling(first, second)

    def test_a_swapped_pair_gives_the_same_gap(self):
        arc = Arc(center=(5.0, 4.0), radius=2.0, a0=math.pi, a1=0.0, ccw=False)
        segment = line(0.0, 0.0, 10.0, 0.0)
        self.assertAlmostEqual(
            closest_approach(segment, arc)[2],
            closest_approach(arc, segment)[2],
        )


class StandingTest(unittest.TestCase):
    def test_a_point_clear_of_every_cut_is_material(self):
        cuts = cuts_of([((line(0.0, 0.0, 10.0, 0.0),), R, False)])
        self.assertTrue(standing_at((5.0, 3.0), cuts, material_index(cuts)))

    def test_a_point_inside_a_cut_is_not_material(self):
        cuts = cuts_of([((line(0.0, 0.0, 10.0, 0.0),), R, False)])
        self.assertFalse(standing_at((5.0, 0.2), cuts, material_index(cuts)))

    def test_a_point_exactly_on_a_cut_wall_is_still_material(self):
        cuts = cuts_of([((line(0.0, 0.0, 10.0, 0.0),), R, False)])
        self.assertTrue(standing_at((5.0, R), cuts, material_index(cuts)))

    def test_a_gap_a_third_cut_already_removed_is_not_a_sliver(self):
        """The corpus false positive, reduced: the cutter has been there."""
        # Two cuts 1.2 mm apart end to end - 0.2 mm of material by the
        # arithmetic - with a third cut running straight through the middle.
        one = straight_channel(0.0, 4.0)
        two = straight_channel(5.2, 9.0)
        through = chain_runs([(line(4.6, -3.0, 4.6, 3.0), R)]).channels[0]
        without = channels.close_slivers([one, two], 0.5)
        self.assertEqual(len(without.joins), 1)
        with_third = channels.close_slivers([one, two, through], 0.5)
        self.assertEqual(with_third.joins, ())


class GuardTest(unittest.TestCase):
    def tab(self, centre=5.0, half=0.41):
        board = square_board(tabs=[tab_footprint(
            position=(centre, 0.0), half_gap=half
        )])
        return route_toolpath.tabs_of(board)[0]

    def test_a_tab_guard_carries_its_own_declared_width(self):
        guard = mintab.tab_guards([self.tab()])[0]
        self.assertEqual(guard.kind, "tab")
        self.assertAlmostEqual(guard.material_mm, 0.82)
        self.assertEqual(len(guard.blades), 2)

    def test_a_chord_across_the_tab_crosses_both_stop_lines(self):
        guards = mintab.tab_guards([self.tab()])
        found = guarded((5.0 - 0.8545, -RADIUS), (5.0 + 0.8545, -RADIUS), guards)
        self.assertIsNotNone(found)

    def test_a_chord_nowhere_near_the_tab_is_not_guarded(self):
        guards = mintab.tab_guards([self.tab()])
        self.assertIsNone(guarded((0.0, -RADIUS), (1.2, -RADIUS), guards))

    def test_a_chord_across_only_one_stop_line_is_not_the_tabs_gap(self):
        guards = mintab.tab_guards([self.tab()])
        self.assertIsNone(
            guarded((5.0 - 0.8545, -RADIUS), (5.0, -RADIUS), guards)
        )

    def test_a_declared_end_pair_is_matched_either_way_round(self):
        guard = Guard(label="s", kind="score", at=(0.0, 0.0),
                      ends=((0.0, 0.0), (1.0, 0.0)))
        self.assertIsNotNone(guarded((0.0, 0.0), (1.0, 0.0), [guard]))
        self.assertIsNotNone(guarded((1.0, 0.0), (0.0, 0.0), [guard]))
        self.assertIsNone(guarded((0.0, 0.0), (2.0, 0.0), [guard]))


class ProtectionTest(unittest.TestCase):
    def guard(self, width=0.82):
        return Guard(label="TAB** @ (5.000, 0.000)", kind="tab",
                     at=(5.0, 0.0), material_mm=width,
                     blades=(line(4.59, 0.0, 4.59, -0.6),
                             line(5.41, 0.0, 5.41, -0.6)))

    def test_a_tab_wider_than_the_setting_is_not_reported(self):
        self.assertEqual(protected_tabs([self.guard(0.82)], 0.5), ())

    def test_a_tab_the_setting_exceeds_is_reported_by_name(self):
        found = protected_tabs([self.guard(0.82)], 1.0)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].material_mm, 0.82)
        self.assertAlmostEqual(found[0].min_tab_mm, 1.0)
        self.assertIn("TAB**", found[0].label)

    def test_a_setting_that_exactly_meets_the_tab_is_reported(self):
        self.assertEqual(len(protected_tabs([self.guard(0.5)], 0.5)), 1)

    def test_the_report_is_one_loud_line_per_tab(self):
        text = protection_warning(protected_tabs([self.guard(0.82)], 1.0)[0])
        self.assertIn("MIN TAB EXCEEDS A DECLARED TAB", text)
        self.assertIn("0.8200", text)
        self.assertIn("1.0000", text)
        self.assertIn("NOT consumed", text)

    def test_a_score_declares_no_width_and_is_never_reported(self):
        guard = Guard(label="score", kind="score", at=(0.0, 0.0),
                      ends=((0.0, 0.0), (1.0, 0.0)))
        self.assertEqual(protected_tabs([guard], 5.0), ())

    def test_a_guarded_tab_gap_is_never_consumed_however_thin_the_setting_calls_it(self):
        # The two cut faces of a 0.82 mm tab, 1.709 mm apart on the centreline,
        # with the minimum tab set above the tab's own width.
        one = straight_channel(0.0, 5.0 - 0.8545, radius=RADIUS)
        two = straight_channel(5.0 + 0.8545, 10.0, radius=RADIUS)
        board = square_board(tabs=[tab_footprint(position=(5.0, 0.0))])
        guards = mintab.tab_guards(route_toolpath.tabs_of(board))
        # The stop lines stand at y from 0 to -0.6 and the cut is at y = 0.
        enforced = channels.close_slivers([one, two], 1.0, guards=guards)
        self.assertEqual(enforced.joins, ())
        self.assertEqual(len(enforced.protected), 1)
        # ...and without the guard the same gap is consumed.
        self.assertEqual(len(channels.close_slivers([one, two], 1.0).joins), 1)


class SliverTest(unittest.TestCase):
    """The threshold, on two collinear cuts a measured distance apart."""

    def channels_apart(self, gap: float):
        return [straight_channel(0.0, 10.0), straight_channel(10.0 + gap, 20.0)]

    def test_a_sliver_thinner_than_the_minimum_tab_is_consumed(self):
        found = channels.close_slivers(self.channels_apart(1.4), 0.5)
        self.assertEqual(len(found.joins), 1)
        self.assertAlmostEqual(found.joins[0].material_mm, 0.4)
        self.assertAlmostEqual(found.joins[0].gap_mm, 1.4)

    def test_material_exactly_the_minimum_tab_is_left_alone(self):
        self.assertEqual(channels.close_slivers(self.channels_apart(1.5), 0.5).joins, ())

    def test_a_near_miss_a_hair_above_the_threshold_is_left_alone(self):
        self.assertEqual(
            channels.close_slivers(self.channels_apart(1.500001), 0.5).joins, ()
        )

    def test_a_hair_under_the_threshold_is_consumed(self):
        self.assertEqual(
            len(channels.close_slivers(self.channels_apart(1.499), 0.5).joins), 1
        )

    def test_a_lower_setting_moves_the_threshold_with_it(self):
        self.assertEqual(channels.close_slivers(self.channels_apart(1.4), 0.3).joins, ())
        self.assertEqual(
            len(channels.close_slivers(self.channels_apart(1.25), 0.3).joins), 1
        )

    def test_the_join_is_measured_between_the_two_nearest_points(self):
        join = channels.close_slivers(self.channels_apart(1.4), 0.5).joins[0]
        self.assertIsInstance(join.move, Line)
        self.assertEqual(join.move.p0, (10.0, 0.0))
        self.assertEqual(join.move.p1, (11.4, 0.0))
        self.assertAlmostEqual(join.length_mm, 1.4)
        self.assertAlmostEqual(join.radius_mm, R)

    def test_the_join_is_carried_as_its_own_channel(self):
        found = channels.close_slivers(self.channels_apart(1.4), 0.5)
        self.assertEqual(len(found.channels), 3)
        self.assertEqual(len(found.channels[-1].path), 1)

    def test_two_different_cutters_join_at_the_wider_one(self):
        wide = straight_channel(0.0, 10.0, radius=0.75)
        narrow = straight_channel(11.5, 20.0, radius=0.25)
        found = channels.close_slivers([wide, narrow], 0.6)
        self.assertEqual(len(found.joins), 1)
        self.assertAlmostEqual(found.joins[0].material_mm, 0.5)
        self.assertAlmostEqual(found.joins[0].radius_mm, 0.75)

    def test_two_channels_running_side_by_side_are_joined_where_they_approach(self):
        first = straight_channel(0.0, 10.0, y=0.0)
        second = straight_channel(0.0, 10.0, y=1.4)
        found = channels.close_slivers([first, second], 0.5)
        self.assertGreaterEqual(len(found.joins), 1)
        for join in found.joins:
            self.assertAlmostEqual(join.material_mm, 0.4)

    def test_a_rail_between_two_neighbouring_instances_builds_as_one_hole(self):
        """Two parallel channels: the join goes in the middle of the overlap.

        Two parallel channels are equally near all along their overlap, so a
        join at an endpoint would leave no way through and the panel would
        refuse. It goes in the middle, and stops short of both centrelines -
        and there is one every cutter width along the WHOLE 26 mm the two run side by side, because a
        rail 0.22 mm wide is swarf for its whole length and not only where it
        was measured.
        """
        rail = 0.22
        first = straight_channel(1.0, 27.0, y=-(CUTTER + rail) / 2.0,
                                 radius=RADIUS)
        second = straight_channel(1.0, 27.0, y=(CUTTER + rail) / 2.0,
                                  radius=RADIUS)
        found = channels.close_slivers([first, second], 0.5)
        self.assertGreaterEqual(len(found.joins), int(26.0 / (2.0 * RADIUS)))
        for join in found.joins:
            self.assertAlmostEqual(join.material_mm, rail)
        along = sorted(join.at[0] for join in found.joins)
        self.assertLessEqual(along[0], 1.0 + 2.0 * RADIUS)
        self.assertGreaterEqual(along[-1], 27.0 - 2.0 * RADIUS)
        for before, after in zip(along, along[1:]):
            self.assertLessEqual(after - before, 2.0 * RADIUS)
        boundary = channels.region_boundary(
            Rect(-5.0, -10.0, 33.0, 10.0), found.channels
        )
        self.assertEqual(boundary.loop_count, 2)

    def test_the_same_rail_a_hair_wider_stays_two_holes(self):
        rail = 0.52
        first = straight_channel(1.0, 27.0, y=-(CUTTER + rail) / 2.0,
                                 radius=RADIUS)
        second = straight_channel(1.0, 27.0, y=(CUTTER + rail) / 2.0,
                                  radius=RADIUS)
        found = channels.close_slivers([first, second], 0.5)
        self.assertEqual(found.joins, ())
        boundary = channels.region_boundary(
            Rect(-5.0, -10.0, 33.0, 10.0), found.channels
        )
        self.assertEqual(boundary.loop_count, 3)

    def test_a_run_travelling_straight_past_itself_is_not_a_sliver(self):
        """The along-path exemption: material beside one cut, not between two."""
        run = chain_runs([
            (line(0.0, 0.0, 1.0, 0.0, "a"), R),
            (line(1.0, 0.0, 2.0, 0.0, "b"), R),
            (line(2.0, 0.0, 3.0, 0.0, "c"), R),
        ]).channels[0]
        self.assertEqual(channels.close_slivers([run], 0.5).joins, ())

    def test_a_hairpin_that_comes_back_on_itself_is_a_sliver(self):
        run = chain_runs([
            (line(0.0, 0.0, 6.0, 0.0, "a"), R),
            (line(6.0, 0.0, 6.0, 1.4, "b"), R),
            (line(6.0, 1.4, 0.0, 1.4, "c"), R),
        ]).channels[0]
        found = channels.close_slivers([run], 0.5)
        self.assertGreaterEqual(len(found.joins), 1)

    def test_the_widest_sliver_consumed_is_reported(self):
        found = channels.close_slivers(self.channels_apart(1.4), 0.5)
        self.assertAlmostEqual(found.consumed_mm, 0.4)
        self.assertEqual(channels.close_slivers(self.channels_apart(3.0), 0.5).consumed_mm, 0.0)

    def test_the_answer_does_not_depend_on_the_order_the_channels_arrive_in(self):
        forward = channels.close_slivers(self.channels_apart(1.4), 0.5)
        backward = channels.close_slivers(
            list(reversed(self.channels_apart(1.4))), 0.5
        )
        self.assertEqual(
            [(join.at, join.to) for join in forward.joins],
            [(join.at, join.to) for join in backward.joins],
        )

    def test_the_warning_is_one_aggregated_line(self):
        found = channels.close_slivers(self.channels_apart(1.4), 0.5)
        text = join_warning(found.joins, 0.5)
        self.assertIn("1 accidental sliver", text)
        self.assertIn("0.4000", text)
        self.assertEqual(join_warning((), 0.5), "")


class TurnedCornerTest(unittest.TestCase):
    """The inside of a corner is not a sliver.

    A step in the board's own edge, offset at a 0.2 mm cutter: the run travels
    the upper edge, turns a right angle round the material's corner, drops
    0.1 mm and fillets away along the lower edge. Asked pairwise, the leg
    BEFORE the corner and the fillet AFTER it are 0.5795 mm apart with
    0.3795 mm of material between them - under the 0.5 mm minimum tab - and the
    along-path exemption misses them by nine tenths of a micrometre: 0.7010 mm
    travelled against a 0.7000 mm reach.

    A join there would chamfer the board's own corner off and write a
    0.3795 mm STRAIGHT panel edge across the toolpath band, plus a cap arc at
    each end of it - four times per instance, and an edge no cutter produces.

    The arrangement says it was never a sliver: the cut wraps round that
    corner, so of the 0.3795 mm claimed only 0.1832 mm is board and the rest is
    inside the cut already. Measured on the corpus panel at a 0.2 mm cutter,
    twenty-four approaches were exactly this - every one the inside of a step in
    a board edge, and none of them a whisker.
    """

    RADIUS = 0.1
    APEX = (10.0, 0.0)
    BLANK = Rect(6.0, -2.0, 14.0, 4.0)

    def quarter(self, start, end, center, source):
        """One arc through a right angle, from its two ends and its centre."""
        radius = math.dist(start, center)
        first = math.atan2(start[1] - center[1], start[0] - center[0])
        last = math.atan2(end[1] - center[1], end[0] - center[0])
        turn = (last - first + math.pi) % (2.0 * math.pi) - math.pi
        middle = first + turn / 2.0
        return arc_from_three_points(
            start, (center[0] + radius * math.cos(middle),
                    center[1] + radius * math.sin(middle)), end, source=source,
        )

    def channel(self):
        """The toolpath as prep writes it: the offset of a stepped board edge."""
        radius = self.RADIUS
        items = [
            (line(8.0, -radius, 9.5561, -radius, "upper"), radius),
            (line(9.5561, -radius, 10.0, -radius, "upper-end"), radius),
            (self.quarter((10.0, -radius), (10.0 + radius, 0.0),
                          self.APEX, "corner"), radius),
            (line(10.0 + radius, 0.0, 10.0 + radius, 0.1, "step"), radius),
            (self.quarter((10.0 + radius, 0.1), (10.5, 0.5),
                          (10.5, 0.1), "fillet"), radius),
            (line(10.5, 0.5, 12.0, 0.5, "lower"), radius),
        ]
        return chain_runs(items).channels[0]

    def cuts(self):
        run = self.channel()
        return cuts_of([(run.path, run.radius, run.closed)])

    def approach(self):
        """The pairwise measurement across the corner: leg to fillet."""
        cuts = self.cuts()
        first, second = cuts[0].primitive, cuts[4].primitive
        return closest_approach(first, second)

    def standing_mm(self, at, to, steps=800):
        """How much of the chord is really board. The slow oracle."""
        cuts = self.cuts()
        index = material_index(cuts)
        gap = math.dist(at, to)
        unit = ((to[0] - at[0]) / gap, (to[1] - at[1]) / gap)
        inside = [
            standing_at(
                (at[0] + unit[0] * gap * step / steps,
                 at[1] + unit[1] * gap * step / steps),
                cuts, index,
            )
            for step in range(steps + 1)
        ]
        return sum(inside) * gap / steps

    def test_the_run_is_one_connected_path_round_the_corner(self):
        run = self.channel()
        self.assertEqual(len(run.path), 6)
        self.assertFalse(run.closed)

    def test_the_pairwise_arithmetic_claims_a_sliver_at_the_corner(self):
        """The trap, in numbers: under the reach, and over it along the path."""
        at, to, gap = self.approach()
        self.assertAlmostEqual(gap, 0.5795, places=4)
        self.assertAlmostEqual(
            material_mm(gap, self.RADIUS, self.RADIUS), 0.3795, places=4
        )
        self.assertLess(gap, self.RADIUS + self.RADIUS + 0.5)
        cuts = self.cuts()
        along = mintab._along_path(cuts[0], at, cuts[4], to)
        self.assertAlmostEqual(along, 0.7010, places=4)
        self.assertGreater(along, self.RADIUS + self.RADIUS + 0.5)

    def test_barely_half_the_claimed_sliver_is_really_board(self):
        at, to, _ = self.approach()
        self.assertAlmostEqual(self.standing_mm(at, to), 0.1832, places=3)

    def test_the_claim_is_refused_over_its_whole_width(self):
        at, to, _ = self.approach()
        cuts = self.cuts()
        self.assertFalse(mintab.sliver_stands(
            at, to, self.RADIUS, self.RADIUS, cuts, material_index(cuts),
            measured=(0, 4),
        ))
        # ...and the narrowest point alone says nothing: it sits exactly on the
        # cut's own face, which counts as material.
        middle = ((at[0] + to[0]) / 2.0, (at[1] + to[1]) / 2.0)
        self.assertTrue(standing_at(middle, cuts, material_index(cuts)))

    def test_the_corner_is_not_consumed(self):
        self.assertEqual(channels.close_slivers([self.channel()], 0.5).joins, ())

    def test_the_panel_edge_keeps_the_corner_the_cutter_left(self):
        found = channels.close_slivers([self.channel()], 0.5)
        boundary = channels.region_boundary(self.BLANK, found.channels)
        corners = [
            piece.start for loop in boundary.loops for piece in loop
            if math.dist(piece.start, self.APEX) < 1e-6
        ]
        self.assertEqual(len(corners), 1)

    def test_no_panel_edge_piece_is_the_chamfer(self):
        found = channels.close_slivers([self.channel()], 0.5)
        boundary = channels.region_boundary(self.BLANK, found.channels)
        chamfers = [
            piece for loop in boundary.loops for piece in loop
            if isinstance(piece, Line) and abs(piece.length - 0.3795) < 1e-3
        ]
        self.assertEqual(chamfers, [])

    def test_every_panel_edge_piece_lies_on_the_swept_boundary(self):
        """The fence: the panel edge is what the CUTTER left, or the blank.

        Every piece of it is exactly one cutter radius from some toolpath item,
        or on the blank's own rectangle. A join the toolpath does not contain
        fails this wherever it shows, which is how the chamfer was caught.
        """
        run = self.channel()
        found = channels.close_slivers([run], 0.5)
        boundary = channels.region_boundary(self.BLANK, found.channels)
        for loop in boundary.loops:
            for piece in loop:
                if piece.role == "blank":
                    continue
                middle = midpoint_of(piece)
                off = min(
                    abs(distance_to_primitive(middle, item) - run.radius)
                    for item in run.path
                )
                self.assertLess(off, 1e-6, f"{piece} stands {off:.6f} mm off")

    def test_the_same_corner_builds_the_same_way_twice(self):
        first = channels.region_boundary(
            self.BLANK, channels.close_slivers([self.channel()], 0.5).channels
        )
        second = channels.region_boundary(
            self.BLANK, channels.close_slivers([self.channel()], 0.5).channels
        )
        self.assertEqual(first.loops, second.loops)

    def test_a_real_sliver_at_the_same_cutter_is_still_consumed(self):
        """The control: the rule still bites where the material IS a whisker."""
        radius = self.RADIUS
        hairpin = chain_runs([
            (line(8.0, 0.0, 12.0, 0.0, "out"), radius),
            (line(12.0, 0.0, 12.0, 0.6, "turn"), radius),
            (line(12.0, 0.6, 8.0, 0.6, "back"), radius),
        ]).channels[0]
        found = channels.close_slivers([hairpin], 0.5)
        self.assertGreaterEqual(len(found.joins), 1)
        for join in found.joins:
            self.assertAlmostEqual(join.material_mm, 0.4)


class RetiredPinchTest(unittest.TestCase):
    """The tangential channel pinch point, from a defect inspection.

    Two collinear routed channels whose facing ends are exactly one cutter
    diameter apart, so each end-cap circle is tangent to the other's at one
    point. That node had degree FOUR, and a boundary passes through a node only
    when exactly two pieces meet there, so the panel build refused with a
    located message. The sliver at a tangency is zero wide, which is under any
    minimum tab there is, so the two channels are now one cut.
    """

    SHEET = Rect(185.0, 140.0, 224.0, 155.0)
    Y = 147.1744

    def pair(self, gap: float = 1.0):
        return [
            straight_channel(190.0, 204.1430, y=self.Y),
            straight_channel(204.1430 + gap, 219.0, y=self.Y),
        ]

    def test_the_tangency_is_exact(self):
        one, two = self.pair()
        at, to, gap = closest_approach(one.path[0], two.path[0])
        self.assertAlmostEqual(gap, 1.0)
        self.assertAlmostEqual(material_mm(gap, R, R), 0.0)

    def test_the_walk_still_refuses_the_raw_tangency(self):
        """And it now says WHAT it is refusing, so a caller can cut it out."""
        with self.assertRaises(channels.PinchError) as caught:
            channels.region_boundary(self.SHEET, self.pair())
        self.assertIn("more than two pieces", str(caught.exception))
        self.assertAlmostEqual(caught.exception.at[0], 204.6430, places=4)
        self.assertEqual(len(caught.exception.points), 1)

    def test_the_minimum_tab_joins_them_and_the_panel_builds(self):
        found = channels.close_slivers(self.pair(), MIN_TAB_DEFAULT_MM)
        # More than one crossing: the tangency itself claims no width, and the
        # wedge either side of it is still under the minimum tab for a fraction
        # of a millimetre, so the covering chain takes that too. The thinnest
        # reading is the tangency, and none of them is over the setting.
        self.assertGreaterEqual(len(found.joins), 1)
        self.assertAlmostEqual(
            min(join.material_mm for join in found.joins), 0.0
        )
        for join in found.joins:
            self.assertLess(join.material_mm, MIN_TAB_DEFAULT_MM)
        boundary = channels.region_boundary(self.SHEET, found.channels)
        # The sheet, and ONE hole where the two channels used to be two.
        self.assertEqual(boundary.loop_count, 2)

    def test_the_two_channels_come_out_as_one_hole(self):
        found = channels.close_slivers(self.pair(), MIN_TAB_DEFAULT_MM)
        boundary = channels.region_boundary(self.SHEET, found.channels)
        holes = [
            loop for loop in boundary.loops
            if sum(piece.length for piece in loop) < 100.0
        ]
        self.assertEqual(len(holes), 1)
        box = channels.chain_bbox(list(holes[0]))
        self.assertAlmostEqual(box[0], 190.0 - R, places=4)
        self.assertAlmostEqual(box[2], 219.0 + R, places=4)

    def test_the_control_case_still_builds_as_two_holes(self):
        """The 20 um wider gap the inspection page used as its control."""
        found = channels.close_slivers(self.pair(gap=1.02), 0.0001)
        self.assertEqual(found.joins, ())
        boundary = channels.region_boundary(self.SHEET, found.channels)
        self.assertEqual(boundary.loop_count, 3)

    def test_the_same_geometry_builds_the_same_way_twice(self):
        first = channels.region_boundary(
            self.SHEET,
            channels.close_slivers(self.pair(), MIN_TAB_DEFAULT_MM).channels,
        )
        second = channels.region_boundary(
            self.SHEET,
            channels.close_slivers(self.pair(), MIN_TAB_DEFAULT_MM).channels,
        )
        self.assertEqual(first.loops, second.loops)


class ApproachTest(unittest.TestCase):
    def test_an_empty_arrangement_has_no_approaches(self):
        self.assertEqual(approaches([], 0.5), [])

    def test_consecutive_items_of_one_run_are_not_an_approach(self):
        cuts = cuts_of([((line(0.0, 0.0, 1.0, 0.0), line(1.0, 0.0, 1.0, 1.0)),
                         R, False)])
        self.assertEqual(approaches(cuts, 0.5), [])

    def test_a_closed_run_measures_the_short_way_round(self):
        run = chain_runs([
            (line(0.0, 0.0, 6.0, 0.0, "a"), R),
            (line(6.0, 0.0, 6.0, 6.0, "b"), R),
            (line(6.0, 6.0, 0.0, 6.0, "c"), R),
            (line(0.0, 6.0, 0.0, 0.0, "d"), R),
        ]).channels[0]
        self.assertTrue(run.closed)
        self.assertEqual(channels.close_slivers([run], 0.5).joins, ())


class JoinShapeTest(unittest.TestCase):
    """A join means two things, and has a shape for each - see :class:`Join`."""

    def join(self, gap, radius=R):
        at, to = (0.0, 0.0), (gap, 0.0)
        return Join(at=at, to=to, gap_mm=gap,
                    material_mm=material_mm(gap, radius, radius),
                    radius_mm=radius, first_radius_mm=radius,
                    second_radius_mm=radius)

    def test_the_travel_runs_centreline_to_centreline(self):
        move = self.join(1.4).move
        self.assertEqual((move.p0, move.p1), ((0.0, 0.0), (1.4, 0.0)))
        self.assertAlmostEqual(move.length, 1.4)

    def test_the_crossing_runs_face_to_face_and_is_the_material_it_consumes(self):
        # 1.6 mm apart, 0.5 mm radii: 0.6 mm of material, and the crossing
        # spans exactly that - from one cut's face to the other's.
        join = self.join(1.6)
        self.assertAlmostEqual(join.material_mm, 0.6)
        crossing = join.crossing
        self.assertAlmostEqual(crossing.length, 0.6)
        self.assertAlmostEqual(crossing.p0[0], 0.5)
        self.assertAlmostEqual(crossing.p1[0], 1.1)

    def test_the_crossing_never_ends_on_the_other_cuts_centreline(self):
        for gap in (1.0, 1.2, 1.4, 1.49):
            crossing = self.join(gap).crossing
            self.assertGreater(crossing.p0[0], 0.0)
            self.assertLess(crossing.p1[0], gap)

    def test_a_crossing_shorter_than_the_bit_is_floored_symmetrically(self):
        # An exact tangency consumes nothing, and a cut of no length is not a
        # cut: it is floored at the bit's own radius, centred on the contact.
        join = self.join(1.0)
        self.assertAlmostEqual(join.material_mm, 0.0)
        crossing = join.crossing
        self.assertAlmostEqual(crossing.length, R)
        middle = (crossing.p0[0] + crossing.p1[0]) / 2.0
        self.assertAlmostEqual(middle, 0.5)

    def test_the_crossing_stays_inside_both_cuts_it_joins(self):
        """The floor bites into material both cuts had already removed."""
        join = self.join(1.0)
        crossing = join.crossing
        # Both ends are within a radius of one of the two centreline points,
        # so neither reaches past a cut and into standing board.
        self.assertLessEqual(math.dist(crossing.p0, join.at), R)
        self.assertLessEqual(math.dist(crossing.p1, join.to), R)


class JoinKeyTest(unittest.TestCase):
    def join(self, at, to):
        return Join(at=at, to=to, gap_mm=math.dist(at, to),
                    material_mm=0.1, radius_mm=R)

    def test_the_key_is_the_same_whichever_way_the_join_is_written(self):
        self.assertEqual(
            self.join((1.0, 2.0), (3.0, 4.0)).key,
            self.join((3.0, 4.0), (1.0, 2.0)).key,
        )

    def test_two_joins_in_different_places_have_different_keys(self):
        self.assertNotEqual(
            self.join((1.0, 2.0), (3.0, 4.0)).key,
            self.join((1.0, 2.0), (3.0, 4.5)).key,
        )

    def test_the_travel_can_be_run_from_either_end(self):
        join = self.join((1.0, 0.0), (3.0, 0.0))
        self.assertEqual(join.move_from((1.0, 0.0)).p0, (1.0, 0.0))
        self.assertEqual(join.move_from((3.0, 0.0)).p0, (3.0, 0.0))


class RunEndsTest(unittest.TestCase):
    """The board half: joining the ENDS of the cutting runs."""

    def runs_apart(self, gap: float):
        return [
            [line(0.0, 0.0, 10.0, 0.0, "a")],
            [line(10.0 + gap, 0.0, 20.0, 0.0, "b")],
        ]

    def test_two_runs_whose_ends_leave_a_sliver_become_one_run(self):
        found = join_run_ends(self.runs_apart(1.2), R, 0.5)
        self.assertEqual(len(found.runs), 1)
        self.assertEqual(len(found.joins), 1)
        self.assertAlmostEqual(found.joins[0].material_mm, 0.2)
        # ...and the merged run is continuous: line, join, line.
        pieces = found.runs[0]
        self.assertEqual(len(pieces), 3)
        for before, after in zip(pieces, pieces[1:]):
            self.assertEqual(before.end, after.start)

    def test_the_joining_cut_carries_the_min_tab_role(self):
        pieces = join_run_ends(self.runs_apart(1.2), R, 0.5).runs[0]
        self.assertEqual(pieces[1].role, mintab.JOIN_ROLE)
        self.assertTrue(pieces[1].source.startswith(mintab.JOIN_ROLE))

    def test_runs_far_enough_apart_are_left_as_they_were(self):
        found = join_run_ends(self.runs_apart(2.0), R, 0.5)
        self.assertEqual(len(found.runs), 2)
        self.assertEqual(found.joins, ())

    def test_a_guarded_gap_leaves_the_two_runs_alone(self):
        guard = Guard(label="TAB", kind="tab", at=(10.6, 0.0),
                      material_mm=0.2,
                      ends=((10.0, 0.0), (11.2, 0.0)))
        found = join_run_ends(self.runs_apart(1.2), R, 0.5, guards=[guard])
        self.assertEqual(len(found.runs), 2)
        self.assertEqual(found.joins, ())
        self.assertEqual(len(found.protected), 1)

    def test_a_run_whose_two_ends_nearly_meet_closes_into_a_loop(self):
        runs = [[
            line(0.0, 0.0, 6.0, 0.0, "a"),
            line(6.0, 0.0, 6.0, 6.0, "b"),
            line(6.0, 6.0, 0.0, 6.0, "c"),
            line(0.0, 6.0, 0.0, 1.2, "d"),
        ]]
        found = join_run_ends(runs, R, 0.5)
        self.assertEqual(len(found.joins), 1)
        self.assertEqual(len(found.runs), 1)
        pieces = found.runs[0]
        self.assertEqual(len(pieces), 5)
        self.assertAlmostEqual(math.dist(pieces[-1].end, pieces[0].start), 0.0)

    def test_a_closed_run_is_not_joined_to_itself(self):
        runs = [[
            line(0.0, 0.0, 6.0, 0.0, "a"),
            line(6.0, 0.0, 6.0, 6.0, "b"),
            line(6.0, 6.0, 0.0, 6.0, "c"),
            line(0.0, 6.0, 0.0, 0.0, "d"),
        ]]
        found = join_run_ends(runs, R, 0.5)
        self.assertEqual(found.joins, ())
        self.assertEqual(len(found.runs[0]), 4)

    def test_each_end_joins_at_most_once_and_the_thinnest_wins(self):
        # Three runs whose ends all crowd one place: the closest pair joins and
        # the third is left with nothing to join to.
        runs = [
            [line(0.0, 0.0, 10.0, 0.0, "a")],
            [line(11.1, 0.0, 20.0, 0.0, "b")],
            [line(10.0, 1.3, 10.0, 8.0, "c")],
        ]
        found = join_run_ends(runs, R, 0.5)
        self.assertEqual(len(found.joins), 1)
        self.assertAlmostEqual(found.joins[0].gap_mm, 1.1)

    def test_two_ends_already_together_are_already_one_cut(self):
        runs = [
            [line(0.0, 0.0, 10.0, 0.0, "a")],
            [line(10.0, 0.0, 20.0, 0.0, "b")],
        ]
        self.assertEqual(join_run_ends(runs, R, 0.5).joins, ())

    def test_a_gap_the_run_itself_spans_is_not_a_sliver(self):
        """One short run's own two ends: the cut goes from one to the other."""
        runs = [[line(0.0, 0.0, 1.2, 0.0, "a")]]
        self.assertEqual(join_run_ends(runs, R, 0.5).joins, ())

    def test_the_result_does_not_depend_on_the_order_the_runs_arrive_in(self):
        forward = join_run_ends(self.runs_apart(1.2), R, 0.5)
        backward = join_run_ends(list(reversed(self.runs_apart(1.2))), R, 0.5)
        self.assertEqual(
            [join.key for join in forward.joins],
            [join.key for join in backward.joins],
        )

    def test_a_value_under_the_floor_is_refused_here_too(self):
        with self.assertRaises(ValueError):
            route_toolpath.build(square_board(), CUTTER, None, 0.05)


class PrepStageTest(unittest.TestCase):
    """The rule reaches the stage, and the corpus board's shape is untouched."""

    def two_tab_board(self):
        return square_board(tabs=[
            tab_footprint(position=(3.0, 0.0), uuid_value="tab-1"),
            tab_footprint(position=(7.0, 0.0), uuid_value="tab-2"),
        ])

    def test_an_ordinary_two_tab_board_has_nothing_to_consume(self):
        made = route_toolpath.build(self.two_tab_board(), CUTTER, None, 0.5)
        self.assertEqual(made.joins, ())
        self.assertEqual(made.protected_tabs, ())
        self.assertAlmostEqual(made.min_tab_mm, 0.5)

    def test_the_setting_is_carried_into_the_result(self):
        made = route_toolpath.build(self.two_tab_board(), CUTTER, None, 0.25)
        self.assertAlmostEqual(made.min_tab_mm, 0.25)

    def test_a_setting_above_the_declared_tab_width_reports_every_tab(self):
        made = route_toolpath.build(self.two_tab_board(), CUTTER, None, 1.0)
        self.assertEqual(len(made.protected_tabs), 2)
        self.assertEqual(made.joins, ())
        loud = [w for w in made.warnings if "MIN TAB EXCEEDS" in w]
        self.assertEqual(len(loud), 2)

    def test_a_protected_tab_still_holds_the_board(self):
        """Warn, never fail: the tab's bridge is still in the toolpath."""
        made = route_toolpath.build(self.two_tab_board(), CUTTER, None, 1.0)
        self.assertEqual(len(made.bridges), 2)
        for bridge in made.bridges:
            self.assertAlmostEqual(bridge.material_mm, 0.82, places=4)
        self.assertTrue(all(bridge.agrees for bridge in made.bridges))

    def built(self, min_tab):
        board = self.two_tab_board()
        route_toolpath.build(board, CUTTER, None, min_tab)
        return board

    def test_a_protected_tab_changes_not_one_byte_of_the_toolpath(self):
        loose = toolpath_items(self.built(0.5))
        tight = toolpath_items(self.built(1.0))
        self.assertEqual(
            [(item.start, item.end, item.id) for item in loose],
            [(item.start, item.end, item.id) for item in tight],
        )

    def test_the_same_board_prepped_twice_writes_the_same_items(self):
        first = toolpath_items(self.built(0.5))
        second = toolpath_items(self.built(0.5))
        self.assertEqual(
            [(item.start, item.end, item.id) for item in first],
            [(item.start, item.end, item.id) for item in second],
        )


class PanelizeStageTest(unittest.TestCase):
    """The panel half: the rule acts between the instances' channels."""

    def panel(self, spacing: float):
        """Two instances one above the other, each routed round three sides."""
        from test_panelize import channel_of, outline_of

        from eedesigncore import Board, BoardLayer

        board = Board(layers=[
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user"),
        ])
        for row in range(2):
            base = spacing * row
            board.drawings += outline_of(0.0, base, 10.0, base + 5.0, f"i{row}")
            board.drawings += channel_of(
                -RADIUS, base - RADIUS, 10.0 + RADIUS, base + 5.0 + RADIUS,
                f"c{row}",
            )
        return board

    def test_instances_far_enough_apart_leave_the_rail_standing(self):
        made = panelize.apply(self.panel(9.0), 5.0, 0.5)
        self.assertEqual(made.joins, ())
        self.assertTrue(made.min_tab_clear)
        self.assertAlmostEqual(made.min_tab_mm, 0.5)

    def test_instances_close_enough_to_leave_a_sliver_are_joined_and_reported(self):
        # 5 mm boards, channels half a cutter clear either side: a 7.0 mm step
        # puts the two channel centrelines 7.0 - 5 - 0.889 = 1.111 mm apart, so
        # the rail between them is 1.111 - 0.889 = 0.222 mm of board.
        made = panelize.apply(self.panel(7.0), 5.0, 0.5)
        self.assertGreaterEqual(len(made.joins), 1)
        for join in made.joins:
            self.assertLess(join.material_mm, 0.5)
        self.assertTrue(any("accidental sliver" in w for w in made.warnings))

    def test_a_value_under_the_floor_is_refused(self):
        with self.assertRaises(ValueError):
            panelize.apply(self.panel(9.0), 5.0, 0.05)

    def test_the_channel_guards_are_the_tabs_the_instances_brought(self):
        from test_panelize import panel_board, tab_footprint as panel_tab

        board = panel_board()
        board.footprints = [panel_tab("1")]
        # One stop line is not a staple, so it declares no tab.
        self.assertEqual(panelize.channel_guards(board), ())


if __name__ == "__main__":
    unittest.main()
