"""Panel scores and the DRC rule areas that guard every score on the panel.

Pure geometry plus board writing, in memory. **Nobody types a score
position.** Switches choose which families of saw pass a panel carries, and
the positions are COMPUTED - the middle of each gap between board columns or
rows, and the inner edge of each rail that exists. The datum is the number the
panel REPORTS: across or down from the blank's top-left corner.

Also pinned: the full-blank extent of a pass, old typed position lists having
no effect, a rail edge the boards have
already scored not being drawn twice, and the ONE mechanism that gives a rule
area to the panel's own scores and to the merged instance-edge ones alike.
"""

import unittest

from eedesigncore import Board, BoardLayer, PcbLine, Stroke, dump_board

from eedesignpanels.channels import Rails, Rect
from eedesignpanels.stages.panel_scores import (
    RULE_AREA_NAME,
    SCORE_LAYER,
    Settings,
    already_on,
    apply,
    copper_layers,
    crossed_boards,
    gap_midlines,
    plan,
    position_of,
    rail_inner_edges,
    rule_area_corners,
    rule_area_for,
    score_lines_on,
    score_span,
    span_at,
    spans_of,
)

BLANK = Rect(min_x=0.0, min_y=0.0, max_x=44.0, max_y=47.5)
RAILS = Rails(top=5.0, bottom=5.0, left=5.0, right=5.0)
# Two columns and two rows of 15.5 x 10.5 boards with a 3 mm gap, inside a
# 5 mm rail: the corpus arrangement, cut down to four boards.
BOXES = (
    (5.0, 5.0, 20.5, 15.5),
    (23.5, 5.0, 39.0, 15.5),
    (5.0, 18.5, 20.5, 29.0),
    (23.5, 18.5, 39.0, 29.0),
)
# The one blank this file measures against is 44 x 47.5 mm, so the boxes above
# sit 5 mm inside it on the left and top and the bottom rail is wider. Stated
# rather than derived, because a fixture that computes itself proves nothing.


def board_with_layers(*extra) -> Board:
    layers = [
        BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
        BoardLayer(ordinal=1, canonical_name="In1.Cu", layer_type="signal"),
        BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
        BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
    ]
    board = Board(layers=layers)
    board.drawings.extend(extra)
    return board


def merged_pass(x, uid="merged"):
    """A score line already on the panel, the way merge_scores leaves one."""
    return PcbLine(
        start=(x, BLANK.min_y), end=(x, BLANK.max_y), layer=SCORE_LAYER,
        stroke=Stroke(width=0.1), id=uid,
    )


def merged_across(y, uid="across"):
    """The same thing, running the other way."""
    return PcbLine(
        start=(BLANK.min_x, y), end=(BLANK.max_x, y), layer=SCORE_LAYER,
        stroke=Stroke(width=0.1), id=uid,
    )


class SettingsTest(unittest.TestCase):
    def test_defaults_are_four_switches_that_are_off(self):
        settings = Settings()
        self.assertFalse(settings.rails_horizontal)
        self.assertFalse(settings.rails_vertical)
        self.assertFalse(settings.inter_row)
        self.assertFalse(settings.inter_column)
        self.assertEqual(settings.rule_half_width_mm, 0.3)
        self.assertFalse(settings.any_on)

    def test_a_mapping_reads_as_settings(self):
        settings = Settings.of({"inter_column": True, "rails_horizontal": True,
                                "rule_half_width_mm": 0.5})
        self.assertTrue(settings.inter_column)
        self.assertFalse(settings.inter_row)
        self.assertTrue(settings.rails_horizontal)
        self.assertEqual(settings.rule_half_width_mm, 0.5)

    def test_the_switches_that_are_on_are_named_in_page_order(self):
        settings = Settings(inter_column=True, rails_horizontal=True)
        self.assertEqual(settings.switches_on,
                         ("rails_horizontal", "inter_column"))
        self.assertTrue(settings.any_on)

    def test_the_retired_position_lists_have_no_effect_at_all(self):
        # An old typed position list is not one of the fields `Settings.of`
        # reads, so it switches nothing on.
        settings = Settings.of({"vertical_mm": [22.0], "horizontal_mm": []})
        self.assertFalse(settings.inter_column)
        self.assertFalse(settings.inter_row)
        self.assertFalse(settings.any_on)

    def test_an_object_with_the_current_fields_reads_as_settings(self):
        from eedesignpanels.config import PanelScoresConfig

        settings = Settings.of(PanelScoresConfig(inter_row=True))
        self.assertTrue(settings.inter_row)
        self.assertFalse(settings.inter_column)


class MidlineTest(unittest.TestCase):
    """A pass runs down the MIDDLE of the gap, and the middle is arithmetic."""

    def test_the_columns_are_read_off_the_boxes_in_order(self):
        self.assertEqual(spans_of(BOXES, "vertical"),
                         ((5.0, 20.5), (23.5, 39.0)))
        self.assertEqual(spans_of(BOXES, "horizontal"),
                         ((5.0, 15.5), (18.5, 29.0)))

    def test_the_vertical_midline_is_halfway_across_the_gap(self):
        # 20.5 to 23.5 is a 3 mm gap, so the blade runs at 22.0.
        self.assertEqual(gap_midlines(BOXES, "vertical"), (22.0,))

    def test_the_horizontal_midline_is_halfway_down_the_gap(self):
        self.assertEqual(gap_midlines(BOXES, "horizontal"), (17.0,))

    def test_three_columns_give_two_midlines_ascending(self):
        boxes = ((0.0, 0.0, 10.0, 10.0), (13.0, 0.0, 23.0, 10.0),
                 (26.0, 0.0, 36.0, 10.0))
        self.assertEqual(gap_midlines(boxes, "vertical"), (11.5, 24.5))

    def test_an_uneven_gap_still_lands_in_its_middle(self):
        boxes = ((0.0, 0.0, 10.0, 10.0), (17.0, 0.0, 27.0, 10.0))
        self.assertEqual(gap_midlines(boxes, "vertical"), (13.5,))

    def test_one_column_has_no_gap_and_therefore_no_pass(self):
        self.assertEqual(gap_midlines(BOXES[:1], "vertical"), ())

    def test_boards_that_touch_leave_nothing_to_saw_down_and_say_so(self):
        boxes = ((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0))
        warnings = []
        self.assertEqual(gap_midlines(boxes, "vertical", warnings), ())
        self.assertEqual(len(warnings), 1)
        self.assertIn("nothing between those boards", warnings[0])


class RailEdgeTest(unittest.TestCase):
    """A rail score sits where the carrier meets the field of boards."""

    def test_each_rail_that_exists_gives_one_inner_edge(self):
        self.assertEqual(
            rail_inner_edges(BLANK, RAILS),
            (("vertical", 5.0, "left"), ("vertical", 39.0, "right"),
             ("horizontal", 5.0, "top"), ("horizontal", 42.5, "bottom")),
        )

    def test_a_rail_of_no_width_is_no_rail_and_gets_no_score(self):
        edges = rail_inner_edges(BLANK, Rails(top=0.0, bottom=5.0, left=3.0,
                                              right=0.0))
        self.assertEqual([side for _, _, side in edges], ["left", "bottom"])

    def test_the_inner_edge_is_the_blank_edge_plus_that_rails_own_width(self):
        rails = Rails(top=8.0, bottom=2.0, left=3.0, right=7.0)
        edges = dict((side, at) for _, at, side in rail_inner_edges(BLANK,
                                                                    rails))
        self.assertEqual(edges["left"], BLANK.min_x + 3.0)
        self.assertEqual(edges["right"], BLANK.max_x - 7.0)
        self.assertEqual(edges["top"], BLANK.min_y + 8.0)
        self.assertEqual(edges["bottom"], BLANK.max_y - 2.0)

    def test_one_number_stands_for_four_rails(self):
        self.assertEqual(rail_inner_edges(BLANK, 5.0),
                         rail_inner_edges(BLANK, RAILS))


class PlanTest(unittest.TestCase):
    """Which passes a layout calls for, and in what order they are written."""

    def test_nothing_is_planned_when_no_switch_is_set(self):
        self.assertEqual(plan(BLANK, Settings(), BOXES, RAILS), ())

    def test_the_inter_column_switch_plans_the_column_gaps_alone(self):
        planned = plan(BLANK, Settings(inter_column=True), BOXES, RAILS)
        self.assertEqual([(item.axis, item.at_mm, item.source)
                          for item in planned],
                         [("vertical", 22.0, "column gap 1")])

    def test_the_inter_row_switch_plans_the_row_gaps_alone(self):
        planned = plan(BLANK, Settings(inter_row=True), BOXES, RAILS)
        self.assertEqual([(item.axis, item.at_mm, item.source)
                          for item in planned],
                         [("horizontal", 17.0, "row gap 1")])

    def test_the_two_rail_switches_together_plan_four_edges(self):
        planned = plan(
            BLANK,
            Settings(rails_horizontal=True, rails_vertical=True),
            BOXES, RAILS,
        )
        self.assertEqual([item.source for item in planned],
                         ["left rail", "right rail", "top rail",
                          "bottom rail"])

    def test_rails_horizontal_alone_plans_only_the_horizontal_edges(self):
        planned = plan(BLANK, Settings(rails_horizontal=True), BOXES, RAILS)
        self.assertEqual([item.source for item in planned],
                         ["top rail", "bottom rail"])

    def test_rails_vertical_alone_plans_only_the_vertical_edges(self):
        planned = plan(BLANK, Settings(rails_vertical=True), BOXES, RAILS)
        self.assertEqual([item.source for item in planned],
                         ["left rail", "right rail"])

    def test_all_four_come_out_verticals_first_then_ascending(self):
        planned = plan(
            BLANK,
            Settings(inter_column=True, inter_row=True,
                    rails_horizontal=True, rails_vertical=True),
            BOXES, RAILS,
        )
        self.assertEqual(
            [(item.axis, item.at_mm) for item in planned],
            [("vertical", 5.0), ("vertical", 22.0), ("vertical", 39.0),
             ("horizontal", 5.0), ("horizontal", 17.0),
             ("horizontal", 42.5)],
        )

    def test_two_features_on_one_line_are_one_pass(self):
        # A rail wide enough to reach the gap's middle would ask for the same
        # cut twice; the file cannot hold two lines in one place.
        rails = Rails(top=5.0, bottom=5.0, left=22.0, right=5.0)
        planned = plan(
            BLANK, Settings(inter_column=True, rails_vertical=True),
            BOXES, rails,
        )
        verticals = [item.at_mm for item in planned if item.axis == "vertical"]
        self.assertEqual(verticals, [22.0, 39.0])

    def test_rail_scores_asked_for_with_no_rails_stated_say_so(self):
        warnings = []
        self.assertEqual(
            plan(BLANK, Settings(rails_horizontal=True, rails_vertical=True),
                BOXES, None, warnings),
            (),
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("how wide the rails are", warnings[0])


class DatumTest(unittest.TestCase):
    """A pass spans the whole blank, and is reported from its top-left corner."""

    def test_a_vertical_score_spans_the_whole_blank_height(self):
        self.assertEqual(
            score_span("vertical", 22.0, BLANK), ((22.0, 0.0), (22.0, 47.5))
        )

    def test_a_horizontal_score_spans_the_whole_blank_width(self):
        self.assertEqual(
            score_span("horizontal", 17.0, BLANK), ((0.0, 17.0), (44.0, 17.0))
        )

    def test_a_span_at_a_coordinate_carries_that_exact_coordinate(self):
        moved = Rect(min_x=190.642984, min_y=118.271287,
                     max_x=234.642984, max_y=165.771287)
        start, end = span_at("vertical", 212.642984, moved)
        # EXACTLY, not to a tolerance: the file carries the number the layout
        # produced, never the datum plus a difference.
        self.assertEqual(start, (212.642984, moved.min_y))
        self.assertEqual(end, (212.642984, moved.max_y))

    def test_the_position_reported_is_the_distance_from_the_corner(self):
        moved = Rect(min_x=190.642984, min_y=118.271287,
                     max_x=234.642984, max_y=165.771287)
        self.assertAlmostEqual(
            position_of("vertical", 212.642984, moved), 22.0, places=9
        )
        self.assertAlmostEqual(
            position_of("horizontal", 135.271287, moved), 17.0, places=9
        )

    def test_zero_and_the_full_width_are_on_the_blank(self):
        self.assertEqual(score_span("vertical", 0.0, BLANK)[0], (0.0, 0.0))
        self.assertEqual(score_span("vertical", 44.0, BLANK)[0], (44.0, 0.0))

    def test_a_position_past_the_blank_is_an_error_naming_it(self):
        with self.assertRaises(ValueError) as caught:
            score_span("vertical", 44.5, BLANK)
        self.assertIn("44.5", str(caught.exception))
        self.assertIn("off the blank", str(caught.exception))

    def test_a_coordinate_past_the_blank_is_the_same_error(self):
        with self.assertRaises(ValueError) as caught:
            span_at("horizontal", 60.0, BLANK)
        self.assertIn("off the blank", str(caught.exception))

    def test_a_negative_position_is_an_error(self):
        with self.assertRaises(ValueError):
            score_span("horizontal", -0.5, BLANK)

    def test_an_unknown_axis_is_an_error(self):
        with self.assertRaises(ValueError):
            score_span("diagonal", 1.0, BLANK)
        with self.assertRaises(ValueError):
            span_at("diagonal", 1.0, BLANK)


class CrossingTest(unittest.TestCase):
    """A computed pass crosses nothing; the guard that says so still works."""

    def test_a_score_down_a_gap_crosses_nothing(self):
        self.assertEqual(crossed_boards("vertical", (22.0, 0.0), BOXES), 0)

    def test_a_score_through_the_boards_counts_them(self):
        self.assertEqual(crossed_boards("vertical", (12.0, 0.0), BOXES), 2)

    def test_a_score_on_a_board_edge_is_not_a_crossing(self):
        self.assertEqual(crossed_boards("vertical", (5.0, 0.0), BOXES), 0)
        self.assertEqual(crossed_boards("vertical", (20.5, 0.0), BOXES), 0)

    def test_no_computed_pass_on_this_layout_warns_at_all(self):
        board = board_with_layers()
        warnings = []
        apply(board, BLANK,
             Settings(inter_column=True, inter_row=True,
                      rails_horizontal=True, rails_vertical=True),
              instance_boxes=BOXES, rails=RAILS, warnings=warnings)
        self.assertEqual(warnings, [])

    def test_a_layout_whose_boards_straddle_a_rail_edge_warns(self):
        # A rail narrower than the boards' inset would put its inner edge
        # inside a board. Nothing builds that today; the guard is what would
        # say so if a placement stage ever did.
        board = board_with_layers()
        warnings = []
        apply(board, BLANK,
             Settings(rails_horizontal=True, rails_vertical=True),
             instance_boxes=BOXES,
              rails=Rails(top=10.0, bottom=5.0, left=5.0, right=5.0),
              warnings=warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("top rail", warnings[0])
        self.assertIn("2 board instance(s)", warnings[0])


class RuleAreaTest(unittest.TestCase):
    """One thin keepout per score line, on every copper layer."""

    def test_the_rectangle_is_centred_across_and_overhangs_lengthways(self):
        # THE GUARD RUNS PAST THE END OF THE LINE: the rectangle is the
        # line's length plus the half width
        # at each end, not the bare line width by width.
        corners = rule_area_corners((10.0, 0.0), (10.0, 20.0), 0.3)
        self.assertEqual(
            [(round(x, 6), round(y, 6)) for x, y in corners],
            [(9.7, -0.3), (9.7, 20.3), (10.3, 20.3), (10.3, -0.3)],
        )

    def test_a_vertical_line_widens_across_and_grows_by_the_overhang(self):
        corners = rule_area_corners((10.0, 0.0), (10.0, 20.0), 0.3)
        self.assertEqual(
            sorted({round(corner[0], 6) for corner in corners}), [9.7, 10.3]
        )
        self.assertEqual(
            sorted({round(corner[1], 6) for corner in corners}), [-0.3, 20.3]
        )

    def test_a_diagonal_line_is_offset_perpendicular_to_itself(self):
        corners = rule_area_corners((0.0, 0.0), (3.0, 4.0), 0.5)
        self.assertEqual(
            [(round(x, 6), round(y, 6)) for x, y in corners],
            [(-0.7, -0.1), (2.9, 4.7), (3.7, 4.1), (0.1, -0.7)],
        )

    def test_a_horizontal_line_widens_down(self):
        corners = rule_area_corners((0.0, 10.0), (20.0, 10.0), 0.3)
        ys = sorted({round(corner[1], 6) for corner in corners})
        self.assertEqual(ys, [9.7, 10.3])

    def test_a_line_of_no_length_has_no_rule_area(self):
        with self.assertRaises(ValueError) as caught:
            rule_area_corners((1.0, 2.0), (1.0, 2.0), 0.3)
        self.assertIn("1.0000", str(caught.exception))

    def test_the_zone_keeps_out_everything(self):
        # ZoneKeepout's fields read "is this ALLOWED": False is what writes
        # `(tracks not_allowed)`. With them True the pour ran straight across
        # every score on the corpus panel and DRC said nothing.
        zone = rule_area_for((0.0, 10.0), (44.0, 10.0), 0.3, ("F.Cu", "B.Cu"))
        self.assertIsNotNone(zone.keepout)
        self.assertFalse(zone.keepout.tracks)
        self.assertFalse(zone.keepout.vias)
        self.assertFalse(zone.keepout.pads)
        self.assertFalse(zone.keepout.copper_pour)
        self.assertFalse(zone.keepout.footprints)
        self.assertEqual(zone.name, RULE_AREA_NAME)
        self.assertEqual(zone.layers, ["F.Cu", "B.Cu"])

    def test_the_keepout_writes_not_allowed_in_the_file(self):
        board = board_with_layers()
        apply(board, BLANK, Settings(inter_column=True), instance_boxes=BOXES)
        text = dump_board(board)
        self.assertIn("(tracks not_allowed)", text)
        self.assertIn("(copperpour not_allowed)", text)
        self.assertIn("(footprints not_allowed)", text)
        self.assertNotIn("(copperpour allowed)", text)

    def test_the_zone_pours_nothing(self):
        zone = rule_area_for((0.0, 10.0), (44.0, 10.0), 0.3, ("F.Cu",))
        self.assertEqual(zone.filled_polygons, [])
        self.assertFalse(zone.fill.filled)

    def test_the_layers_are_every_copper_layer_in_file_order(self):
        self.assertEqual(
            copper_layers(board_with_layers()), ("F.Cu", "In1.Cu", "B.Cu")
        )

    def test_every_computed_pass_is_accompanied_by_its_rule_area(self):
        board = board_with_layers()
        made = apply(board, BLANK,
                    Settings(inter_column=True, inter_row=True,
                             rails_horizontal=True, rails_vertical=True),
                     instance_boxes=BOXES, rails=RAILS)
        self.assertEqual(made.count, 6)
        self.assertEqual(len(made.rule_areas), 6)
        self.assertEqual(
            len([zone for zone in board.zones if zone.name == RULE_AREA_NAME]),
            6,
        )
        for area in made.rule_areas:
            self.assertEqual(area.half_width_mm, 0.3)
            self.assertEqual(area.layers, ("F.Cu", "In1.Cu", "B.Cu"))


class OneMechanismTest(unittest.TestCase):
    """Merged instance passes get exactly the same rule area as our own."""

    def test_every_score_line_on_the_panel_gets_one(self):
        board = board_with_layers(merged_pass(5.0), merged_pass(20.5, "m2"))
        made = apply(board, BLANK, Settings(inter_column=True),
                     instance_boxes=BOXES)
        self.assertEqual(made.inherited_lines, 2)
        self.assertEqual(made.count, 1)
        self.assertEqual(len(made.rule_areas), 3)
        self.assertEqual(len(board.zones), 3)

    def test_a_stage_run_with_no_switch_still_guards_the_merged_passes(self):
        board = board_with_layers(merged_pass(5.0))
        made = apply(board, BLANK, Settings(), instance_boxes=BOXES)
        self.assertEqual(made.count, 0)
        self.assertEqual(len(made.rule_areas), 1)
        self.assertEqual(board.zones[0].name, RULE_AREA_NAME)

    def test_the_rule_areas_come_out_in_geometry_order_not_file_order(self):
        first = board_with_layers(merged_pass(30.0, "a"), merged_pass(5.0, "b"))
        second = board_with_layers(merged_pass(5.0, "b"), merged_pass(30.0, "a"))
        made_first = apply(first, BLANK, Settings(), instance_boxes=BOXES)
        made_second = apply(second, BLANK, Settings(), instance_boxes=BOXES)
        self.assertEqual(
            [area.start for area in made_first.rule_areas],
            [area.start for area in made_second.rule_areas],
        )


class AlreadyScoredTest(unittest.TestCase):
    """A rail edge the BOARDS scored is one pass, not two on one line."""

    def test_a_pass_already_on_the_panel_is_recognised(self):
        lines = [merged_pass(5.0), merged_across(42.5)]
        self.assertTrue(already_on(lines, "vertical", 5.0))
        self.assertTrue(already_on(lines, "horizontal", 42.5))
        self.assertFalse(already_on(lines, "vertical", 39.0))
        # Same coordinate, wrong axis: a vertical pass at 5 is not a horizontal
        # pass at 5.
        self.assertFalse(already_on(lines, "horizontal", 5.0))

    def test_the_rail_edge_the_boards_scored_is_not_drawn_twice(self):
        board = board_with_layers(merged_pass(5.0))
        made = apply(board, BLANK,
                    Settings(rails_horizontal=True, rails_vertical=True),
                    instance_boxes=BOXES, rails=RAILS)
        self.assertEqual([score.source for score in made.scores],
                         ["right rail", "top rail", "bottom rail"])
        self.assertEqual(made.already_scored, ("left rail",))
        self.assertEqual(len(score_lines_on(board)), 4)
        # ...and the merged pass still gets its keep-out, from the one
        # mechanism that gives every line on the layer one.
        self.assertEqual(len(made.rule_areas), 4)

    def test_the_skipped_pass_is_said_in_the_warnings(self):
        board = board_with_layers(merged_pass(5.0))
        warnings = []
        apply(board, BLANK,
             Settings(rails_horizontal=True, rails_vertical=True),
             instance_boxes=BOXES, rails=RAILS, warnings=warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("left rail", warnings[0])
        self.assertIn("already on", warnings[0])


class DrawingTest(unittest.TestCase):
    """The lines the stage draws, and where they land."""

    def test_a_score_lands_on_the_score_layer_at_the_right_place(self):
        board = board_with_layers()
        apply(board, BLANK, Settings(inter_row=True), instance_boxes=BOXES)
        line = score_lines_on(board)[0]
        self.assertEqual(line.layer, SCORE_LAYER)
        self.assertEqual(line.start, (0.0, 17.0))
        self.assertEqual(line.end, (44.0, 17.0))

    def test_the_switches_this_run_used_are_reported_back(self):
        board = board_with_layers()
        made = apply(board, BLANK,
                    Settings(inter_column=True, rails_horizontal=True),
                    instance_boxes=BOXES, rails=RAILS)
        self.assertEqual(made.switches, ("rails_horizontal", "inter_column"))

    def test_a_panel_with_no_copper_layer_says_the_drc_cannot_help(self):
        board = Board(layers=[
            BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
        ])
        warnings = []
        apply(board, BLANK, Settings(inter_column=True), instance_boxes=BOXES,
              warnings=warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no copper layer", warnings[0])


class IdentityTest(unittest.TestCase):
    """Deterministic uuids, derived from the geometry and from nothing else."""

    def built(self):
        board = board_with_layers(merged_pass(5.0))
        apply(board, BLANK,
             Settings(inter_column=True, inter_row=True,
                      rails_horizontal=True, rails_vertical=True),
              instance_boxes=BOXES, rails=RAILS)
        return board

    def test_two_builds_name_the_same_score_and_the_same_rule_areas(self):
        first, second = self.built(), self.built()
        self.assertEqual(
            [line.id for line in score_lines_on(first)],
            [line.id for line in score_lines_on(second)],
        )
        self.assertEqual(
            [zone.id for zone in first.zones], [zone.id for zone in second.zones]
        )

    def test_two_builds_write_the_same_bytes(self):
        self.assertEqual(dump_board(self.built()), dump_board(self.built()))

    def test_the_lines_come_out_in_one_order_however_the_boxes_arrived(self):
        forward = board_with_layers()
        backward = board_with_layers()
        settings = Settings(inter_column=True, inter_row=True)
        apply(forward, BLANK, settings, instance_boxes=BOXES)
        apply(backward, BLANK, settings, instance_boxes=tuple(reversed(BOXES)))
        self.assertEqual(dump_board(forward), dump_board(backward))

    def test_a_gap_that_moves_gives_its_score_a_new_identity(self):
        here = board_with_layers()
        there = board_with_layers()
        wider = tuple(
            (box[0], box[1], box[2], box[3]) if box[0] < 21.0
            else (box[0] + 1.0, box[1], box[2] + 1.0, box[3])
            for box in BOXES
        )
        apply(here, BLANK, Settings(inter_column=True), instance_boxes=BOXES)
        apply(there, BLANK, Settings(inter_column=True), instance_boxes=wider)
        self.assertNotEqual(
            score_lines_on(here)[0].id, score_lines_on(there)[0].id
        )

    def test_every_generated_identity_is_a_version_five_uuid(self):
        board = board_with_layers()
        apply(board, BLANK, Settings(inter_column=True), instance_boxes=BOXES)
        generated = [line.id for line in score_lines_on(board)] + [
            zone.id for zone in board.zones
        ]
        self.assertEqual(len(generated), 2)
        for value in generated:
            self.assertEqual(value[14], "5")

    def test_no_two_rule_areas_on_one_panel_share_an_identity(self):
        board = board_with_layers(merged_pass(5.0), merged_pass(20.5, "m2"))
        apply(board, BLANK, Settings(inter_column=True, inter_row=True),
              instance_boxes=BOXES)
        self.assertEqual(len({zone.id for zone in board.zones}), 4)


if __name__ == "__main__":
    unittest.main()
