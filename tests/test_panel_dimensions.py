"""Panel dimensions: the value, the side, the identity, and the file format.

The stage writes NATIVE KiCad aligned dimension items, so the tests are about
the four things a wrong one would hide.

**The value.** KiCad measures in integer nanometres and rounds halfway cases
away from zero, so the number on the drawing is pinned to the micrometre
against the panel's known extents and one board's known size - and the
generated text is pinned against KiCad's own formatting rules, which is what
makes "180 mm" not "179.9997 mm".

**Which board.** The inner pair measures ONE instance, and which one is derived
from the corner the two overall dimensions share.
No corner is named in the stage, so the tests move the overall dimensions'
endpoints and assert the chosen board moves with them.

**The side.** `height` is signed and its sign is which side of the measured
line the crossbar sits on (`pcb_dimension.cpp:960-987`). Getting it backwards
draws the dimension THROUGH the panel, which is legal, plots, and is wrong. So
the crossbar's own coordinates are computed and asserted to be outside the
blank, on the side asked for, with the board size inside the overall extents
and never on top of them.

**The identity.** Derived uuids, so two builds are byte-identical and a panel
that changed size gets new ones: same input, same output.

**The file format.** A dimension KiCad ITSELF wrote is parsed and re-emitted
here byte for byte, which is the gate for trusting eedesigncore's model at
all; and our own output is checked against a golden block, because the item's
token order is load-bearing - KiCad's own writer puts the `gr_text` last "to be
sure the text options are known when reading the file".
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    BoardLayer,
    Dimension,
    Footprint,
    PcbLine,
    Setup,
    Stroke,
    dump_board,
)
from eedesigncore.mod.writer import _dump_dimension
from eedesigncore.sexpr.writer import prettify
from pydantic import ValidationError

from dimension_clearance import (
    clearance_mm,
    line_pen_mm,
    measured,
    parts_of,
    text_ink_mm,
    text_pen_mm,
)
from eedesignpanels.channels import Rect
from eedesignpanels.config import (
    MAX_TEXT_HEIGHT_MM,
    PanelDimensionsConfig,
    PanelizeConfig,
)
from eedesignpanels.preview import actions, page_panel
from eedesignpanels.stages import panel_dimensions
from eedesignpanels.stages.panel_dimensions import (
    DIMENSION_LAYER,
    SIDES,
    TEXT_POSITION_INLINE,
    TEXT_POSITION_MANUAL,
    Settings,
    apply,
    checked,
    crossbar_nm,
    dimension_for,
    effective_pen_mm,
    outside_placement,
    text_outset_mm,
    board_drafts,
    extension_height_mm,
    instance_rects,
    kiround,
    longest_extent_of,
    manual_placement,
    measured_nm,
    nearest_instance,
    plan,
    shared_corner_of,
    signed_height,
    text_height_for,
    text_placement,
    to_nm,
    value_text,
)
from eedesignpanels.stages.step_and_repeat import build
from test_panel_page import write_panel_project

# A 44 x 47.5 mm blank, the corpus panel's own extents with the origin moved to
# zero so the arithmetic in the assertions can be read.
BLANK = Rect(min_x=0.0, min_y=0.0, max_x=44.0, max_y=47.5)

# Four 15 x 18 mm boards in a 2 x 2 grid inside that blank, 5 mm rails and a
# 4 mm gutter between the columns, 1.5 mm between the rows. Index 1 is the
# top-right copy, which is the one the overall dimensions' shared corner picks.
BOXES = (
    (5.0, 5.0, 20.0, 23.0),
    (24.0, 5.0, 39.0, 23.0),
    (5.0, 24.5, 20.0, 42.5),
    (24.0, 24.5, 39.0, 42.5),
)

# The corner the two overall dimensions share on this blank: top-right.
SHARED = (44.0, 0.0)

# FOUR BOARDS THE SIZE OF THE CORPUS PANEL'S OWN: 15.5 mm across and 10.5 mm
# down, which is what the reference panel measures. The value printed for
# the short side reads "10.5 mm" and is 12.7 mm long at 2 mm lettering - LONGER
# THAN THE BOARD IT MEASURES - so the arrowheads at the crossbar's ends fall
# under the lettering. That is the case the outset value exists for.
NARROW_BOXES = (
    (5.0, 5.0, 20.5, 15.5),
    (23.5, 5.0, 39.0, 15.5),
    (5.0, 32.0, 20.5, 42.5),
    (23.5, 32.0, 39.0, 42.5),
)

CUTTER = 0.889
RADIUS = CUTTER / 2.0


def board_with_layers(*names) -> Board:
    """A bare board declaring exactly *names*, for the layer advisory."""
    return Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=index, canonical_name=name, layer_type="user")
            for index, name in enumerate(names)
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )


def outline(x0, y0, x1, y1, layer="Edge.Cuts", width=0.05, uid=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer, stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def source_board() -> Board:
    """A 10 x 10 mm board with a toolpath round three sides, ready to panelize."""
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
            BoardLayer(ordinal=17, canonical_name="Dwgs.User",
                       layer_type="user", user_name="User.Drawings"),
            BoardLayer(ordinal=25, canonical_name="Edge.Cuts",
                       layer_type="user"),
            # The dimensions layer, declared as the corpus board declares it.
            BoardLayer(ordinal=69, canonical_name="User.16",
                       layer_type="user", user_name="User.16-Dimensions"),
            BoardLayer(ordinal=109, canonical_name="User.36",
                       layer_type="user"),
            BoardLayer(ordinal=113, canonical_name="User.38",
                       layer_type="user"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    corners = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    board.drawings = [
        outline(*corners[index], *corners[(index + 1) % 4], uid=f"edge-{index}")
        for index in range(4)
    ]
    path = [(-RADIUS, -RADIUS), (10 + RADIUS, -RADIUS),
            (10 + RADIUS, 10 + RADIUS), (-RADIUS, 10 + RADIUS)]
    for index in range(3):
        board.drawings.append(outline(
            *path[index], *path[index + 1], layer="User.36", width=CUTTER,
            uid=f"path-{index}",
        ))
    board.footprints = [Footprint(
        fp_id="lib:R", layer="F.Cu", uuid="fp-1",
        at={"position": (5.0, 5.0), "angle": 0.0},
    )]
    return board


class NanometreTest(unittest.TestCase):
    """The unit KiCad measures in, and the roundings it applies."""

    def test_halfway_cases_go_away_from_zero(self):
        # Python's own round() breaks ties to even and would answer 2 and -2.
        self.assertEqual(kiround(2.5), 3)
        self.assertEqual(kiround(-2.5), -3)
        self.assertEqual(kiround(0.5), 1)
        self.assertEqual(kiround(1.4999), 1)

    def test_millimetres_become_whole_nanometres(self):
        self.assertEqual(to_nm(1.0), 1_000_000)
        # Half a nanometre, so this is the tie: away from zero, not to even.
        self.assertEqual(to_nm(0.0015875), 1588)
        self.assertEqual(to_nm(-2.54), -2_540_000)

    def test_a_measurement_is_the_distance_to_the_nanometre(self):
        self.assertEqual(measured_nm((0.0, 0.0), (44.0, 0.0)), 44_000_000)
        self.assertEqual(measured_nm((0.0, 0.0), (0.0, 47.5)), 47_500_000)
        self.assertEqual(measured_nm((5.0, 5.0), (39.0, 5.0)), 34_000_000)

    def test_a_diagonal_is_measured_the_way_kicad_measures_it(self):
        # KiROUND of the Euclidean norm over nanometre coordinates.
        want = kiround(math.hypot(3_000_000, 4_000_000))
        self.assertEqual(measured_nm((0.0, 0.0), (3.0, 4.0)), want)
        self.assertEqual(want, 5_000_000)


class ValueTextTest(unittest.TestCase):
    """What the drawing SAYS, per GetValueText and updateText."""

    def test_millimetres_carry_the_bare_suffix(self):
        self.assertEqual(value_text(180_000_000, Settings()), "180 mm")

    def test_trailing_zeroes_are_suppressed_but_the_value_is_not(self):
        settings = Settings(precision=3)
        self.assertEqual(value_text(47_625_000, settings), "47.625 mm")
        self.assertEqual(value_text(47_600_000, settings), "47.6 mm")
        self.assertEqual(value_text(47_000_000, settings), "47 mm")

    def test_keeping_the_zeroes_states_every_place(self):
        settings = Settings(precision=3, suppress_zeroes=False)
        self.assertEqual(value_text(47_000_000, settings), "47.000 mm")

    def test_the_precision_is_a_number_of_decimal_places(self):
        self.assertEqual(
            value_text(47_625_400, Settings(precision=2, suppress_zeroes=False)),
            "47.63 mm",
        )
        self.assertEqual(
            value_text(47_625_400, Settings(precision=0)), "48 mm",
        )

    def test_a_half_at_the_last_place_rounds_up_not_to_even(self):
        settings = Settings(precision=2, suppress_zeroes=False)
        self.assertEqual(value_text(47_625_000, settings), "47.63 mm")
        self.assertEqual(value_text(47_615_000, settings), "47.62 mm")


class SettingsTest(unittest.TestCase):
    """The refusals: a dimension nobody can read is not written."""

    def test_a_value_dependent_precision_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            checked(Settings(precision=6))
        self.assertIn("decimal places", str(caught.exception))

    def test_a_zero_sized_dimension_is_refused(self):
        for field in ("text_height_mm", "text_thickness_mm",
                      "line_thickness_mm", "arrow_length_mm"):
            with self.assertRaises(ValueError) as caught:
                checked(Settings(**{field: 0.0}))
            self.assertIn(field, str(caught.exception))

    def test_a_negative_offset_is_refused(self):
        with self.assertRaises(ValueError):
            checked(Settings(first_offset_mm=-1.0))

    def test_settings_come_from_a_mapping_a_model_or_themselves(self):
        wanted = Settings(layer="User.16", precision=2)
        self.assertEqual(Settings.of(wanted), wanted)
        self.assertEqual(
            Settings.of({"layer": "User.16", "precision": 2}), wanted,
        )
        self.assertEqual(
            Settings.of(PanelDimensionsConfig(layer="User.16", precision=2)),
            wanted,
        )

    def test_nothing_at_all_means_the_defaults(self):
        self.assertEqual(Settings.of(None), Settings())
        self.assertEqual(Settings().layer, DIMENSION_LAYER)

    def test_a_derived_text_size_is_not_a_zero_sized_one(self):
        # None is the two text sizes saying "scale me", which `checked` must
        # let past - the refusal above is about a stated size of zero.
        self.assertIsNone(checked(Settings()).text_height_mm)


class LetteringTest(unittest.TestCase):
    """The scaling rule: longest extent over 50, nearest 0.5 mm, floor 2 mm."""

    def test_the_corpus_panel_keeps_the_two_millimetre_text(self):
        self.assertEqual(text_height_for(47.5), 2.0)

    def test_the_envelope_maximum_letters_at_eight(self):
        self.assertEqual(text_height_for(400.0), 8.0)

    def test_it_steps_in_half_millimetres(self):
        self.assertEqual(text_height_for(160.0), 3.0)
        self.assertEqual(text_height_for(175.0), 3.5)
        self.assertEqual(text_height_for(180.0), 3.5)
        self.assertEqual(text_height_for(190.0), 4.0)

    def test_a_half_step_rounds_up_not_to_even(self):
        # 137.5 / 50 = 2.75, which is exactly between 2.5 and 3.0.
        self.assertEqual(text_height_for(137.5), 3.0)
        self.assertEqual(text_height_for(112.5), 2.5)

    def test_nothing_smaller_than_the_floor_is_ever_written(self):
        # The floor holds up to the point the rule itself reaches 2 mm, which
        # is a 112.5 mm panel - half of 2.25 rounds up to 2.5.
        for extent in (1.0, 20.0, 47.5, 100.0, 112.4):
            self.assertEqual(text_height_for(extent), 2.0)

    def test_a_panel_with_no_extent_names_the_problem(self):
        with self.assertRaises(ValueError) as caught:
            text_height_for(0.0)
        self.assertIn("longest extent", str(caught.exception))

    def test_the_longest_side_is_what_scales_it(self):
        self.assertEqual(longest_extent_of(BLANK), 47.5)
        self.assertEqual(longest_extent_of(Rect(0.0, 0.0, 300.0, 200.0)), 300.0)

    def test_the_stroke_follows_the_height_at_kicads_own_ratio(self):
        resolved = Settings().resolved(400.0)
        self.assertEqual(resolved.text_height_mm, 8.0)
        self.assertEqual(resolved.text_thickness_mm, 1.2)

    def test_the_floor_keeps_the_stroke_this_stage_always_wrote(self):
        resolved = Settings().resolved(47.5)
        self.assertEqual(resolved.text_height_mm, 2.0)
        self.assertEqual(resolved.text_thickness_mm, 0.3)

    def test_a_stated_height_wins_and_turns_the_scaling_off(self):
        resolved = Settings(text_height_mm=1.5).resolved(400.0)
        self.assertEqual(resolved.text_height_mm, 1.5)
        self.assertEqual(resolved.text_thickness_mm, 0.225)

    def test_a_stated_stroke_wins_too(self):
        resolved = Settings(text_thickness_mm=0.5).resolved(400.0)
        self.assertEqual(resolved.text_thickness_mm, 0.5)

    def test_resolving_twice_changes_nothing(self):
        once = Settings().resolved(400.0)
        self.assertEqual(once.resolved(47.5), once)

    def test_no_panel_to_measure_is_the_floor(self):
        self.assertEqual(Settings().resolved().text_height_mm, 2.0)

    def test_the_rows_move_out_with_the_lettering(self):
        # Scaled lettering needs scaled rows: 8 mm text on a 5 mm pitch would
        # print the overall extents THROUGH the hole spacing.
        resolved = Settings().resolved(400.0)
        self.assertEqual(resolved.first_offset_mm, 20.0)
        self.assertEqual(resolved.row_pitch_mm, 28.0)

    def test_the_floor_keeps_the_offset_this_stage_always_wrote(self):
        resolved = Settings().resolved(47.5)
        self.assertEqual(resolved.first_offset_mm, 5.0)

    def test_the_pitch_is_wider_than_the_first_offset(self):
        # The row pitch is a multiple of the lettering, not a millimetre
        # figure, and it is wider than the first offset so the two rows of
        # values stay apart. 3.5 x 2.0 mm lettering is 7.0 mm, against a
        # 5.0 mm first offset.
        resolved = Settings().resolved(47.5)
        self.assertEqual(resolved.row_pitch_mm, 7.0)
        self.assertGreater(resolved.row_pitch_mm, resolved.first_offset_mm)

    def test_the_pitch_scales_with_a_stated_lettering_height(self):
        self.assertEqual(
            Settings(text_height_mm=6.0).resolved().row_pitch_mm, 21.0
        )

    def test_a_stated_offset_wins(self):
        resolved = Settings(first_offset_mm=3.0).resolved(400.0)
        self.assertEqual(resolved.first_offset_mm, 3.0)
        self.assertEqual(resolved.row_pitch_mm, 28.0)

    def test_no_row_ever_prints_through_the_one_outside_it(self):
        # The gap between two crossbars has to clear the lettering that sits on
        # them, at every panel size the rule covers.
        for extent in (47.5, 120.0, 200.0, 400.0):
            resolved = Settings().resolved(extent)
            self.assertGreater(resolved.row_pitch_mm,
                               resolved.text_height_mm * 1.5)

    def test_the_pen_and_the_arrows_do_not_scale(self):
        # KiCad's own figures at every panel size, so the panel's dimensions
        # are drawn with the same pen as its boards' (stated for overrule).
        resolved = Settings().resolved(400.0)
        self.assertEqual(resolved.line_thickness_mm, 0.1)
        self.assertEqual(resolved.arrow_length_mm, 1.27)

    def test_the_plan_letters_a_big_panel_bigger(self):
        wide = plan(Rect(0.0, 0.0, 400.0, 300.0), BOXES)
        self.assertEqual(wide.text_height_mm, 8.0)
        self.assertEqual(plan(BLANK, BOXES).text_height_mm, 2.0)

    def test_the_drawn_item_carries_the_derived_size(self):
        board = board_with_layers(DIMENSION_LAYER)
        made = apply(board, Rect(0.0, 0.0, 400.0, 300.0), BOXES)
        self.assertEqual(made.text_height_mm, 1.25)
        for item in board.drawings:
            self.assertEqual(item.text.attributes.font_size, (1.25, 1.25))
            self.assertEqual(item.text.attributes.font_thickness, 0.1875)


class SideTest(unittest.TestCase):
    """Which side of the measured line the crossbar lands on."""

    def test_a_left_to_right_dimension_needs_a_negative_height_to_sit_above(self):
        # Board y runs DOWN, so "above" is the negative extension direction.
        height = signed_height((0.0, 0.0), (44.0, 0.0), (0.0, -1.0), 5.0)
        self.assertEqual(height, -5.0)
        (start, end) = crossbar_nm((0.0, 0.0), (44.0, 0.0), height)
        self.assertEqual(start, (0, -5_000_000))
        self.assertEqual(end, (44_000_000, -5_000_000))

    def test_a_top_to_bottom_dimension_sits_to_the_right(self):
        height = signed_height((44.0, 0.0), (44.0, 47.5), (1.0, 0.0), 5.0)
        self.assertEqual(height, -5.0)
        (start, end) = crossbar_nm((44.0, 0.0), (44.0, 47.5), height)
        self.assertEqual(start, (49_000_000, 0))
        self.assertEqual(end, (49_000_000, 47_500_000))

    def test_the_other_sign_puts_it_on_the_other_side(self):
        (start, _) = crossbar_nm((0.0, 0.0), (44.0, 0.0), 5.0)
        self.assertEqual(start, (0, 5_000_000))

    def test_a_crossbar_not_perpendicular_to_the_line_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            signed_height((0.0, 0.0), (44.0, 0.0), (1.0, 0.0), 5.0)
        self.assertIn("perpendicular", str(caught.exception))

    def test_two_feature_points_in_one_place_are_refused(self):
        with self.assertRaises(ValueError):
            crossbar_nm((1.0, 1.0), (1.0, 1.0), -5.0)


class TextPlacementTest(unittest.TestCase):
    """The inline value on the crossbar, per PCB_DIM_ALIGNED::updateText."""

    def test_a_horizontal_dimension_reads_left_to_right(self):
        position, angle = text_placement((0.0, 0.0), (44.0, 0.0), -5.0)
        self.assertEqual(position, (22.0, -5.0))
        self.assertEqual(angle, 0.0)

    def test_a_vertical_dimension_reads_bottom_to_top(self):
        # KiCad's own file for the same case carries 90 degrees.
        position, angle = text_placement((44.0, 0.0), (44.0, 47.5), -5.0)
        self.assertEqual(position, (49.0, 23.75))
        self.assertEqual(angle, 90.0)


class ExtensionHeightTest(unittest.TestCase):
    """The stub past the crossbar - stored, never recomputed by KiCad."""

    def test_kicads_own_arrow_gives_kicads_own_number(self):
        self.assertEqual(extension_height_mm(1.27), 0.58642)

    def test_it_is_derived_from_the_arrow_and_truncated(self):
        want = int(2_000_000 * math.sin(math.radians(27.5))) / 1e6
        self.assertEqual(extension_height_mm(2.0), want)


class PlanTest(unittest.TestCase):
    """What gets measured, where it is drawn, and in what order."""

    def setUp(self):
        self.made = plan(BLANK, BOXES, None, [])
        self.by_name = self.made.by_name

    def test_four_dimensions_by_name(self):
        self.assertEqual(self.made.count, 4)
        self.assertEqual(
            set(self.by_name),
            {"panel_width", "panel_length", "board_width", "board_length"},
        )

    def test_the_values_are_the_panels_own_extents_to_the_micrometre(self):
        self.assertEqual(self.made.value_of("panel_width"), 44.0)
        self.assertEqual(self.made.value_of("panel_length"), 47.5)

    def test_the_values_are_one_boards_own_bounding_box(self):
        self.assertEqual(self.made.value_of("board_width"), 15.0)
        self.assertEqual(self.made.value_of("board_length"), 18.0)

    def test_the_board_measured_is_reported_with_the_corner_that_chose_it(self):
        self.assertEqual(self.made.board_index, 1)
        self.assertEqual(self.made.board_box, BOXES[1])
        self.assertEqual(self.made.board_corner, SHARED)

    def test_the_text_states_those_values(self):
        self.assertEqual(self.by_name["panel_width"].text, "44 mm")
        self.assertEqual(self.by_name["panel_length"].text, "47.5 mm")
        self.assertEqual(self.by_name["board_width"].text, "15 mm")
        self.assertEqual(self.by_name["board_length"].text, "18 mm")

    def test_the_width_is_on_top_and_the_length_is_on_the_right(self):
        self.assertEqual(self.by_name["panel_width"].side, "top")
        self.assertEqual(self.by_name["board_width"].side, "top")
        self.assertEqual(self.by_name["panel_length"].side, "right")
        self.assertEqual(self.by_name["board_length"].side, "right")

    def test_the_board_pair_spans_the_chosen_rectangle(self):
        width = self.by_name["board_width"]
        self.assertEqual((width.start[0], width.end[0]), (24.0, 39.0))
        length = self.by_name["board_length"]
        self.assertEqual((length.start[1], length.end[1]), (5.0, 23.0))

    def test_each_board_dimension_sits_on_the_edge_nearest_the_corner(self):
        # The extension lines run the short way out to the crossbar, so the
        # line measured is the rectangle edge closest to where it is read.
        self.assertEqual(self.by_name["board_width"].start[1], 5.0)
        self.assertEqual(self.by_name["board_length"].start[0], 39.0)

    def test_every_crossbar_is_outside_the_blank(self):
        for measure in self.made.measures:
            start, end = crossbar_nm(measure.start, measure.end,
                                     measure.height_mm)
            for point in (start, end):
                if measure.side == "top":
                    self.assertLess(point[1], to_nm(BLANK.min_y), measure.name)
                else:
                    self.assertGreater(point[0], to_nm(BLANK.max_x),
                                       measure.name)

    def test_the_board_size_sits_inside_the_overall_extents(self):
        # Smallest measurement closest to the part, the way a drafter draws it.
        self.assertLess(self.by_name["board_width"].offset_mm,
                        self.by_name["panel_width"].offset_mm)
        self.assertLess(self.by_name["board_length"].offset_mm,
                        self.by_name["panel_length"].offset_mm)

    def test_the_two_rows_on_a_side_are_a_full_pitch_apart(self):
        settings = Settings(first_offset_mm=3.0, row_pitch_mm=7.0).resolved()
        made = plan(BLANK, BOXES, settings, [])
        board = made.by_name["board_width"]
        panel = made.by_name["panel_width"]
        self.assertEqual(panel.offset_mm - board.offset_mm, 7.0)

    def test_no_crossbar_crosses_the_row_outside_it(self):
        # The crossbars are parallel lines at different offsets, so the only
        # way two could touch is a shared offset - asserted here per side.
        for side in ("top", "right"):
            lines = []
            for measure in self.made.measures:
                if measure.side != side:
                    continue
                start, end = crossbar_nm(measure.start, measure.end,
                                         measure.height_mm)
                index = 1 if side == "top" else 0
                self.assertEqual(start[index], end[index], measure.name)
                lines.append(start[index])
            self.assertEqual(len(lines), len(set(lines)))

    def test_no_two_crossbars_on_a_side_share_a_line(self):
        for side in ("top", "right"):
            offsets = [measure.offset_mm for measure in self.made.measures
                       if measure.side == side]
            self.assertEqual(len(offsets), len(set(offsets)))

    def test_the_rows_step_by_the_pitch_from_the_first_offset(self):
        settings = Settings(first_offset_mm=3.0, row_pitch_mm=7.0)
        made = plan(BLANK, BOXES, settings, [])
        offsets = sorted(measure.offset_mm for measure in made.measures
                         if measure.side == "top")
        self.assertEqual(offsets, [3.0, 10.0])

    def test_the_reach_carries_the_boards_own_inset(self):
        # The board edge is 5 mm inside the panel, so its crossbar has to reach
        # 5 mm further than the panel edge's does to land at the same offset.
        across = self.by_name["board_width"]
        self.assertEqual(across.reach_mm, across.offset_mm + 5.0)
        self.assertEqual(self.by_name["panel_width"].reach_mm,
                         self.by_name["panel_width"].offset_mm)

    def test_the_order_is_stable_and_reads_side_then_row(self):
        first = [measure.name for measure in plan(BLANK, BOXES).measures]
        second = [measure.name for measure in
                  plan(BLANK, tuple(reversed(BOXES))).measures]
        self.assertEqual(first, second)

    def test_a_panel_with_no_extent_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            plan(Rect(0.0, 0.0, 0.0, 10.0), ())
        self.assertIn("no extent", str(caught.exception))


class WithoutBoardBoxesTest(unittest.TestCase):
    """A panel handed no board rectangles still says how big it is."""

    def test_the_overall_extents_are_still_dimensioned(self):
        made = plan(BLANK, (), None, [])
        self.assertEqual(made.count, 2)
        self.assertEqual(made.value_of("panel_width"), 44.0)
        self.assertIsNone(made.value_of("board_width"))

    def test_nothing_measured_is_reported_as_nothing_chosen(self):
        made = plan(BLANK, None, None, [])
        self.assertIsNone(made.board_box)
        self.assertIsNone(made.board_index)
        self.assertIsNone(made.board_corner)

    def test_the_missing_board_dimension_is_stated_by_name(self):
        warnings = []
        plan(BLANK, (), None, warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no board rectangle", warnings[0])

    def test_the_innermost_row_is_the_first_offset(self):
        made = plan(BLANK, (), Settings(first_offset_mm=4.0), [])
        self.assertEqual(
            sorted(measure.offset_mm for measure in made.measures), [4.0, 4.0],
        )

    def test_overall_dimensions_that_meet_at_no_corner_are_reported(self):
        # Unreachable through `plan` today - the two overall measures always
        # meet at the blank's top-right corner - and kept because the corner is
        # DERIVED, not fixed: a future pair of sides that does not meet must
        # say why no board was measured rather than drop it silently.
        warnings = []
        panel_dimensions._report_missing_board(BOXES, None, warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("shared corner", warnings[0])


class InstanceRectsTest(unittest.TestCase):
    """Whatever shape the caller has the board rectangles in."""

    def test_nothing_is_no_rectangles(self):
        self.assertEqual(instance_rects(None), ())
        self.assertEqual(instance_rects(()), ())

    def test_a_corner_swapped_box_is_normalised(self):
        self.assertEqual(instance_rects([(20.0, 23.0, 5.0, 5.0)]),
                         ((5.0, 5.0, 20.0, 23.0),))

    def test_an_arrangements_own_boxes_are_read(self):
        class Arrangement:
            boxes = [(5.0, 5.0, 20.0, 23.0)]

        self.assertEqual(instance_rects(Arrangement()),
                         ((5.0, 5.0, 20.0, 23.0),))

    def test_a_box_that_is_not_four_numbers_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            instance_rects([(1.0, 2.0, 3.0)])
        self.assertIn("four numbers", str(caught.exception))


class SharedCornerTest(unittest.TestCase):
    """The corner the two overall dimensions meet at - never a named corner."""

    def test_it_is_the_endpoint_both_overall_dimensions_touch(self):
        self.assertEqual(shared_corner_of(plan(BLANK, BOXES).measures), SHARED)

    def test_moving_the_overall_dimensions_moves_the_corner(self):
        # The two overall measures rewritten onto the bottom-left corner. The
        # stage names no corner, so the answer follows the geometry.
        made = plan(BLANK, BOXES)
        width = made.by_name["panel_width"]._replace(
            start=(0.0, 47.5), end=(44.0, 47.5))
        length = made.by_name["panel_length"]._replace(
            start=(0.0, 0.0), end=(0.0, 47.5))
        self.assertEqual(shared_corner_of([width, length]), (0.0, 47.5))

    def test_two_lines_that_do_not_meet_share_no_corner(self):
        # The length dimension moved off the width's ends entirely: no endpoint
        # is shared, so no board is chosen and the stage says so.
        made = plan(BLANK, BOXES)
        length = made.by_name["panel_length"]._replace(
            start=(44.0, 2.0), end=(44.0, 49.5))
        self.assertIsNone(
            shared_corner_of([made.by_name["panel_width"], length])
        )

    def test_one_overall_dimension_alone_names_no_corner(self):
        made = plan(BLANK, BOXES)
        self.assertIsNone(shared_corner_of([made.by_name["panel_width"]]))


class NearestInstanceTest(unittest.TestCase):
    """The rule: the board closest to the corner the outer dimensions share."""

    def test_the_top_right_corner_picks_the_top_right_board(self):
        self.assertEqual(nearest_instance(BOXES, SHARED), 1)

    def test_each_corner_picks_its_own_board(self):
        wanted = {(0.0, 0.0): 0, (44.0, 0.0): 1,
                  (0.0, 47.5): 2, (44.0, 47.5): 3}
        for corner, index in wanted.items():
            with self.subTest(corner=corner):
                self.assertEqual(nearest_instance(BOXES, corner), index)

    def test_a_corner_on_a_board_is_zero_away_from_it(self):
        self.assertEqual(nearest_instance(BOXES, (30.0, 10.0)), 1)

    def test_nothing_to_choose_from_chooses_nothing(self):
        self.assertIsNone(nearest_instance((), SHARED))
        self.assertIsNone(nearest_instance(BOXES, None))

    def test_a_tie_is_broken_the_same_way_every_time(self):
        # Two boards equally close to the corner: the same one is picked on
        # every run, whichever order they arrive in: same input, same output.
        boxes = ((0.0, 10.0, 10.0, 20.0), (10.0, 0.0, 20.0, 10.0))
        first = nearest_instance(boxes, (10.0, 10.0))
        second = nearest_instance(tuple(reversed(boxes)), (10.0, 10.0))
        self.assertEqual(boxes[first], tuple(reversed(boxes))[second])


class BoardDraftsTest(unittest.TestCase):
    """One board's own size, drawn on the sides the overall pair is on."""

    def setUp(self):
        self.overall = tuple(
            plan(BLANK, BOXES).by_name[name]
            for name in ("panel_width", "panel_length")
        )

    def test_it_measures_the_rectangles_own_extents(self):
        drafts = {draft.name: draft for draft in
                  board_drafts(BOXES[1], SHARED, self.overall, Settings())}
        self.assertEqual(drafts["board_width"].value_mm, 15.0)
        self.assertEqual(drafts["board_length"].value_mm, 18.0)

    def test_the_held_edge_follows_the_corner(self):
        # Same rectangle, the corner moved to the bottom-left: the width is now
        # measured along the rectangle's bottom edge, not its top.
        drafts = {draft.name: draft for draft in
                  board_drafts(BOXES[1], (0.0, 47.5), self.overall,
                               Settings())}
        self.assertEqual(drafts["board_width"].start[1], 23.0)
        self.assertEqual(drafts["board_length"].start[0], 24.0)

    def test_it_reads_the_same_way_round_as_the_dimension_it_nests_in(self):
        drafts = {draft.name: draft for draft in
                  board_drafts(BOXES[1], SHARED, self.overall, Settings())}
        for name, overall in zip(("board_width", "board_length"),
                                 self.overall):
            axis = 0 if name == "board_width" else 1
            with self.subTest(name=name):
                self.assertEqual(
                    drafts[name].end[axis] > drafts[name].start[axis],
                    overall.end[axis] > overall.start[axis],
                )


class IdentityTest(unittest.TestCase):
    """Derived uuids: the same panel twice, and a changed panel not."""

    def test_the_same_measurement_derives_the_same_uuid(self):
        measure = plan(BLANK, BOXES).by_name["panel_width"]
        self.assertEqual(dimension_for(measure).id, dimension_for(measure).id)

    def test_a_panel_of_a_different_size_derives_different_uuids(self):
        first = plan(BLANK, BOXES).by_name["panel_width"]
        wider = plan(Rect(0.0, 0.0, 50.0, 47.5), BOXES).by_name["panel_width"]
        self.assertNotEqual(dimension_for(first).id, dimension_for(wider).id)

    def test_moving_the_crossbar_derives_a_different_uuid(self):
        measure = plan(BLANK, BOXES).by_name["panel_width"]
        moved = plan(BLANK, BOXES,
                     Settings(first_offset_mm=9.0)).by_name["panel_width"]
        self.assertNotEqual(dimension_for(measure).id, dimension_for(moved).id)

    def test_no_two_dimensions_on_one_panel_share_an_identity(self):
        made = plan(BLANK, BOXES)
        ids = [dimension_for(measure).id for measure in made.measures]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_text_carries_the_dimensions_own_uuid_as_kicad_writes_it(self):
        item = dimension_for(plan(BLANK, BOXES).by_name["panel_width"])
        self.assertEqual(item.text.id, item.id)

    def test_the_uuids_are_version_5(self):
        for measure in plan(BLANK, BOXES).measures:
            self.assertEqual(dimension_for(measure).id[14], "5")


# One `(dimension (type aligned) …)` block as KICAD 10 ITSELF WROTE IT, lifted
# verbatim from the KiCad source tree's own regression board
# (`qa/data/pcbnew/api_kitchen_sink.kicad_pcb`). It is here as a FORMAT ORACLE:
# if eedesigncore can read this and write it back byte for byte, its dimension
# model can be trusted to hold everything KiCad puts in one.
KICAD_WRITTEN_ALIGNED = """(dimension
\t(type aligned)
\t(layer "F.SilkS")
\t(uuid "0bc42083-d926-4194-a819-d6be9c0115b7")
\t(pts
\t\t(xy 119.75 76.25) (xy 119.75 82.5)
\t)
\t(height -5.5)
\t(format
\t\t(prefix "")
\t\t(suffix "")
\t\t(units 1)
\t\t(units_format 1)
\t\t(precision 1)
\t)
\t(style
\t\t(thickness 0.1)
\t\t(arrow_length 1.27)
\t\t(text_position_mode 2)
\t\t(arrow_direction outward)
\t\t(extension_height 0.58642)
\t\t(extension_offset 0.5)
\t)
\t(gr_text "246.1 mils"
\t\t(at 126 79 0)
\t\t(layer "F.SilkS")
\t\t(uuid "0bc42083-d926-4194-a819-d6be9c0115b7")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.1)
\t\t\t)
\t\t)
\t)
)"""

# What this stage writes for the corpus panel's overall width. A GOLDEN: the
# token order is KiCad's own and the `gr_text` must come last. The layer is
# `User.16` and the lettering 2 mm, which is what a 47.5 mm panel derives.
# The 12 mm height is the outer row: 5 mm for the board's own dimension plus
# the 7 mm pitch, which is 3.5 times the lettering.
EXPECTED_WIDTH_BLOCK = """(dimension
\t(type aligned)
\t(layer "User.16")
\t(uuid "{uuid}")
\t(pts
\t\t(xy 0 0) (xy 44 0)
\t)
\t(height -12)
\t(format
\t\t(prefix "")
\t\t(suffix "")
\t\t(units 2)
\t\t(units_format 1)
\t\t(precision 3)
\t\t(suppress_zeroes yes)
\t)
\t(style
\t\t(thickness 0.1)
\t\t(arrow_length 1.27)
\t\t(text_position_mode 1)
\t\t(arrow_direction outward)
\t\t(extension_height 0.58642)
\t\t(extension_offset 0.5)
\t\t(keep_text_aligned yes)
\t)
\t(gr_text "44 mm"
\t\t(at 22 -12 0)
\t\t(layer "User.16")
\t\t(uuid "{uuid}")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 2 2)
\t\t\t\t(thickness 0.3)
\t\t\t)
\t\t)
\t)
)"""


class FileFormatTest(unittest.TestCase):
    """The item on disk: eedesigncore's fidelity, and our token order."""

    def test_a_dimension_kicad_wrote_round_trips_byte_for_byte(self):
        board = board_with_layers("F.SilkS")
        board.drawings.append(_load_one(KICAD_WRITTEN_ALIGNED))
        emitted = _dimension_blocks(board)
        self.assertEqual(emitted, [KICAD_WRITTEN_ALIGNED])

    def test_our_own_dimension_is_written_in_kicads_token_order(self):
        measure = plan(BLANK, BOXES).by_name["panel_width"]
        item = dimension_for(measure)
        self.assertEqual(
            prettify(_dump_dimension(item)).rstrip("\n"),
            EXPECTED_WIDTH_BLOCK.format(uuid=item.id),
        )

    def test_a_dimension_this_stage_wrote_reloads_unchanged(self):
        board = board_with_layers(DIMENSION_LAYER)
        apply(board, BLANK, BOXES)
        reloaded = Board.from_string(board.to_string())
        # Keyed by uuid, not by position: the writer decides the order items
        # appear in the file, and that is the library's business not ours.
        before = _dumps_by_id(board)
        after = _dumps_by_id(reloaded)
        self.assertEqual(len(after), 7)
        self.assertEqual(after, before)

    def test_a_reloaded_panel_writes_the_same_bytes_again(self):
        board = board_with_layers(DIMENSION_LAYER)
        apply(board, BLANK, BOXES)
        text = board.to_string()
        self.assertEqual(Board.from_string(text).to_string(), text)

    def test_the_written_file_is_byte_identical_twice(self):
        first, second = (board_with_layers(DIMENSION_LAYER),
                         board_with_layers(DIMENSION_LAYER))
        apply(first, BLANK, BOXES)
        apply(second, BLANK, BOXES)
        self.assertEqual(dump_board(first), dump_board(second))


def _dumps_by_id(board: Board) -> dict:
    """Every dimension of *board* as a plain dict, keyed by its uuid."""
    return {
        item.id: item.model_dump()
        for item in board.drawings if isinstance(item, Dimension)
    }


def _load_one(text: str) -> Dimension:
    """One dimension block, through a whole board so nothing private is used."""
    board = board_with_layers("F.SilkS")
    board.drawings.append(PcbLine(
        start=(0.0, 0.0), end=(1.0, 0.0), layer="F.SilkS",
        stroke=Stroke(width=0.1), id="placeholder",
    ))
    text_of_board = board.to_string().replace("\r\n", "\n")
    marker = "\t(gr_line"
    head, _, tail = text_of_board.partition(marker)
    indented = "\n".join(f"\t{line}" for line in text.splitlines())
    spliced = f"{head}{indented}\n{marker}{tail}"
    loaded = Board.from_string(spliced)
    return next(item for item in loaded.drawings
                if isinstance(item, Dimension))


def _dimension_blocks(board: Board) -> list:
    """Every `(dimension …)` block of *board*'s written text, un-indented."""
    text = board.to_string().replace("\r\n", "\n")
    blocks, index = [], 0
    while True:
        index = text.find("\t(dimension\n", index)
        if index < 0:
            return blocks
        depth, cursor = 0, index
        while True:
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        blocks.append("\n".join(
            line[1:] if line.startswith("\t") else line
            for line in text[index:cursor].splitlines()
        ))
        index = cursor


class ApplyTest(unittest.TestCase):
    """Putting them on a board, and the advisory when they cannot be seen."""

    def test_the_dimensions_reach_the_board_in_the_planned_order(self):
        board = board_with_layers(DIMENSION_LAYER)
        made = apply(board, BLANK, BOXES)
        items = [item for item in board.drawings if isinstance(item, Dimension)]
        self.assertEqual(len(items), made.count)
        self.assertEqual([item.id for item in items],
                         [dimension_for(m).id for m in made.measures])

    def test_they_land_on_the_configured_layer(self):
        board = board_with_layers("Cmts.User")
        made = apply(board, BLANK, BOXES, Settings(layer="Cmts.User"))
        self.assertEqual(made.layer, "Cmts.User")
        for item in board.drawings:
            self.assertEqual(item.layer, "Cmts.User")
            self.assertEqual(item.text.layer, "Cmts.User")

    def test_a_layer_the_panel_does_not_declare_is_a_warning_not_a_refusal(self):
        # Most designs do NOT declare User.16, and KiCad reads an item on an
        # undeclared user layer without complaint - it simply never enables the
        # layer. So the warning has to name it.
        board = board_with_layers("F.Cu")
        warnings = []
        made = apply(board, BLANK, BOXES, warnings=warnings)
        self.assertEqual(made.count, 7)
        warnings = [warning for warning in warnings if "does not declare layer" in warning]
        self.assertEqual(len(warnings), 1)
        self.assertIn(DIMENSION_LAYER, warnings[0])
        self.assertIn("disabled", warnings[0])
        self.assertIn("panel.dimensions.layer", warnings[0])

    def test_a_declared_layer_says_nothing(self):
        warnings = []
        apply(board_with_layers(DIMENSION_LAYER), BLANK, BOXES,
              warnings=warnings)
        self.assertFalse(any("does not declare layer" in warning for warning in warnings))


class ConfigTest(unittest.TestCase):
    """The config: on when absent, off only when said so."""

    def test_absent_means_the_defaults_and_not_off(self):
        # The section is always a
        # `PanelDimensionsConfig`, defaulting to `enabled=True` - never
        # `None` - so `dimension_settings` is the one place that reads
        # whether the stage runs.
        panel = PanelizeConfig(rails=5.0)
        self.assertTrue(panel.dimensions.enabled)
        self.assertIsNotNone(panel.dimension_settings)
        self.assertEqual(panel.dimension_settings.layer, "User.16")

    def test_the_default_layer_is_the_one_the_boards_declare(self):
        # `(69 "User.16" user "User.16-Dimensions")` on the corpus board, and
        # the canonical name is what the stage and `--layers` take.
        self.assertEqual(DIMENSION_LAYER, "User.16")
        self.assertEqual(PanelDimensionsConfig().layer, DIMENSION_LAYER)

    def test_the_text_sizes_are_derived_until_somebody_states_them(self):
        settings = PanelDimensionsConfig()
        self.assertIsNone(settings.text_height_mm)
        self.assertIsNone(settings.text_thickness_mm)
        self.assertEqual(
            PanelDimensionsConfig(text_height_mm=3.0).text_height_mm, 3.0,
        )

    def test_switched_off_is_off(self):
        panel = PanelizeConfig(rails=5.0, dimensions={"enabled": False})
        self.assertIsNone(panel.dimension_settings)

    def test_a_misspelled_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValidationError):
            PanelDimensionsConfig(offset_mm=5.0)

    def test_a_value_dependent_precision_is_refused_by_the_config_too(self):
        with self.assertRaises(ValidationError):
            PanelDimensionsConfig(precision=6)

    def test_a_zero_sized_text_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelDimensionsConfig(text_height_mm=0.0)


class WiringTest(unittest.TestCase):
    """The stage inside the panel build."""

    def panel(self, **kwargs):
        return build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     **kwargs)

    def test_the_dimensions_reach_the_panel_and_the_report(self):
        made = self.panel(dimensions=PanelDimensionsConfig()).panelize
        self.assertEqual(made.dimensions.count, 6)
        self.assertEqual(made.dimensions.layer, "User.16")

    def test_they_measure_the_blank_the_panel_half_derived(self):
        outcome = self.panel(dimensions=PanelDimensionsConfig())
        made = outcome.panelize
        self.assertEqual(made.dimensions.value_of("panel_width"),
                         round(made.blank.width_mm, 6))
        self.assertEqual(made.dimensions.value_of("panel_length"),
                         round(made.blank.height_mm, 6))

    def test_one_board_is_measured_from_the_instances_the_build_placed(self):
        outcome = self.panel(dimensions=PanelDimensionsConfig())
        made = outcome.panelize
        boxes = list(outcome.geometry.instance_boxes)
        self.assertIn(made.dimensions.board_box, boxes)
        chosen = made.dimensions.board_box
        self.assertAlmostEqual(made.dimensions.value_of("board_width"),
                               round(chosen[2] - chosen[0], 6), places=6)
        self.assertAlmostEqual(made.dimensions.value_of("board_length"),
                               round(chosen[3] - chosen[1], 6), places=6)

    def test_the_board_measured_is_the_one_nearest_the_shared_corner(self):
        # The panel is two boards side by side, so the corner the overall
        # dimensions share - top-right - picks the right-hand copy.
        outcome = self.panel(dimensions=PanelDimensionsConfig())
        made = outcome.panelize
        boxes = list(outcome.geometry.instance_boxes)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(made.dimensions.board_corner,
                         (made.blank.max_x, made.blank.min_y))
        self.assertEqual(made.dimensions.board_box,
                         max(boxes, key=lambda box: box[2]))

    def test_the_board_dimensions_are_inside_the_panels_own(self):
        made = self.panel(dimensions=PanelDimensionsConfig()).panelize
        names = made.dimensions.by_name
        for inner, outer in (("board_width", "panel_width"),
                             ("board_length", "panel_length")):
            with self.subTest(inner=inner):
                self.assertLess(names[inner].value_mm, names[outer].value_mm)
                self.assertLess(names[inner].offset_mm,
                                names[outer].offset_mm)

    def test_nothing_is_drawn_when_the_config_declined_them(self):
        outcome = self.panel()
        self.assertIsNone(outcome.panelize.dimensions)
        self.assertEqual(
            [item for item in outcome.board.drawings
             if isinstance(item, Dimension)],
            [],
        )

    def test_declining_them_builds_the_panel_it_built_before(self):
        self.assertEqual(dump_board(self.panel().board),
                         dump_board(self.panel().board))

    def test_two_builds_with_dimensions_are_byte_identical(self):
        first = self.panel(tooling_holes={"diameter_mm": 2.0},
                           dimensions=PanelDimensionsConfig()).board
        second = self.panel(tooling_holes={"diameter_mm": 2.0},
                            dimensions=PanelDimensionsConfig()).board
        self.assertEqual(dump_board(first), dump_board(second))

    def test_the_dimensions_are_the_only_thing_they_add(self):
        without = self.panel().board
        with_them = self.panel(dimensions=PanelDimensionsConfig()).board
        added = [item for item in with_them.drawings
                 if isinstance(item, Dimension)]
        self.assertEqual(len(added), 6)
        self.assertEqual(
            len(with_them.drawings), len(without.drawings) + len(added),
        )
        self.assertEqual(len(with_them.footprints), len(without.footprints))
        self.assertEqual(len(with_them.zones), len(without.zones))


def drawn_items(blank, boxes, settings=None, inline_everything=False) -> list:
    """The dimension items this panel gets, ready to measure.

    *inline_everything* draws every value on its own crossbar, which is what
    lets the overlap that layout causes be measured rather than described.
    """
    made = plan(blank, boxes, settings)
    resolved = Settings.of(settings).resolved(
        panel_dimensions.longest_extent_of(blank)
    )
    items = []
    for measure in made.measures:
        if inline_everything:
            measure = measure._replace(text_position=TEXT_POSITION_INLINE)
        items.append(dimension_for(measure, resolved))
    return items


def outward_of(measure, item) -> float:
    """How far the value sits from its crossbar, along the row's outward side.

    Positive is away from the panel, which is the direction the value must
    sit in: outset, not inset. One number
    answers for the top row and for the right one, so a per-axis mistake cannot
    hide behind a per-axis assertion.
    """
    outward = SIDES[measure.side]
    axis = 0 if abs(outward[0]) > 0.5 else 1
    bar = crossbar_nm(measure.start, measure.end, measure.height_mm)
    return ((item.text.position[axis] - panel_dimensions.to_mm(bar[0][axis]))
            * outward[axis])


def own_clearance_mm(item, *parts) -> float:
    """The gap between one dimension's value and named parts of its own lines.

    Named parts, because the value must clear two of the three - its own
    crossbar and arrowheads - and the third answers a different question - see
    :func:`dimension_clearance.parts_of`.
    """
    split = parts_of(item)
    segments = [
        segment for part in (parts or tuple(split)) for segment in split[part]
    ]
    return clearance_mm(text_ink_mm(item), text_pen_mm(item),
                        segments, line_pen_mm(item))


class OutsetTextTest(unittest.TestCase):
    """The inner pair's value sits BESIDE its crossbar, AWAY FROM THE PANEL.

    The per-board value is outset from its arrows so the two cannot overlap.
    KiCad's own `DIM_TEXT_POSITION::OUTSIDE` is not enough for that: it sends a
    VERTICAL value to `-x` whatever direction the measured line runs
    (`PCB_DIM_ALIGNED::updateText`, `pcb_dimension.cpp:1035-1046`), and on the
    right-hand row that is the panel side, so the value lands inset.

    So the mode is `MANUAL`, the DIRECTION is the row's own "away from the
    panel" (:data:`SIDES`) and the DISTANCE is still KiCad's own figure. MANUAL
    is what makes it stick: the parser ends on `Update()`
    (`pcb_io_kicad_sexpr_parser.cpp:4924`), which recomputes an OUTSIDE position
    and leaves a MANUAL one alone.

    Every clearance here is MEASURED, painted edge to painted edge, through
    eedesigncore's ports of KiCad's own line work and stroke font
    (:mod:`dimension_clearance`). Nothing is eyeballed and nothing is asserted
    about a picture.
    """

    def test_the_inner_pair_is_outset_and_the_overall_pair_is_not(self):
        modes = {
            measure.name: measure.text_position
            for measure in plan(BLANK, BOXES).measures
        }
        self.assertEqual(modes["board_width"], TEXT_POSITION_MANUAL)
        self.assertEqual(modes["board_length"], TEXT_POSITION_MANUAL)
        # UNCHANGED, deliberately: no overlap was reported on these two.
        self.assertEqual(modes["panel_width"], TEXT_POSITION_INLINE)
        self.assertEqual(modes["panel_length"], TEXT_POSITION_INLINE)

    def test_the_written_item_says_which_mode_it_is(self):
        items = {item.text.content: item for item in drawn_items(BLANK, BOXES)}
        # MANUAL is ordinal 2 (`pcb_dimension.h:59-66`) and KiCad's parser
        # clamps the token to 0..3 (`pcb_io_kicad_sexpr_parser.cpp:4738-4745`),
        # so a 2 in the file is read as the mode it names.
        self.assertEqual(TEXT_POSITION_MANUAL, 2)
        self.assertEqual(items["15 mm"].style.text_position_mode,
                         TEXT_POSITION_MANUAL)
        self.assertEqual(items["44 mm"].style.text_position_mode,
                         TEXT_POSITION_INLINE)

    def test_both_board_values_sit_on_the_far_side_of_their_crossbar(self):
        # The outset, as an assertion. Measured along the row's own
        # outward direction, so one number covers the top row and the right one:
        # both values are a full outset OUTBOARD of their crossbar.
        settings = Settings().resolved(longest_extent_of(BLANK))
        for measure in plan(BLANK, BOXES).measures:
            if measure.name not in ("board_width", "board_length"):
                continue
            item = dimension_for(measure, settings)
            with self.subTest(measure.name):
                self.assertAlmostEqual(outward_of(measure, item),
                                       text_outset_mm(settings), places=6)

    def test_kicads_own_outside_mode_puts_the_vertical_one_inboard(self):
        # WHY THE MODE IS MANUAL, measured on the port of KiCad's own rule: the
        # right-hand row's outward direction is +x and OUTSIDE goes to -x.
        measure = plan(BLANK, BOXES).by_name["board_length"]
        settings = Settings().resolved(longest_extent_of(BLANK))
        bar = crossbar_nm(measure.start, measure.end, measure.height_mm)
        bar_x = panel_dimensions.to_mm(bar[0][0])
        kicads = outside_placement(measure.start, measure.end,
                                   measure.height_mm,
                                   text_outset_mm(settings))
        self.assertLess(kicads[0][0] - bar_x, 0.0)
        ours = manual_placement(measure.start, measure.end, measure.height_mm,
                                SIDES[measure.side], text_outset_mm(settings))
        self.assertGreater(ours[0][0] - bar_x, 0.0)

    def test_reversing_the_feature_points_does_not_move_kicads_choice(self):
        # The module docstring's claim, asserted: the quarter turn's sign and
        # the rotation's sign flip together, so OUTSIDE cannot be talked into
        # the other side by measuring the same line the other way.
        forward = outside_placement((10.0, 0.0), (10.0, 20.0), -5.0, 2.3)
        backward = outside_placement((10.0, 20.0), (10.0, 0.0), 5.0, 2.3)
        self.assertEqual(forward[0], backward[0])

    def test_a_crossbar_the_outward_direction_runs_along_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            manual_placement((0.0, 0.0), (10.0, 0.0), -5.0, (1.0, 0.0), 2.3)
        self.assertIn("beside its crossbar", str(raised.exception))

    def test_the_outset_is_kicads_own_figure(self):
        # `GetEffectiveTextPenWidth() + GetTextHeight()`, pcb_dimension.cpp:1031.
        settings = Settings().resolved(44.0)
        self.assertEqual(text_outset_mm(settings), 2.3)
        self.assertEqual(settings.text_height_mm, 2.0)
        self.assertEqual(effective_pen_mm(settings), 0.3)

    def test_the_outset_moves_the_value_off_the_crossbar_by_that_much(self):
        measure = plan(BLANK, BOXES).by_name["board_width"]
        settings = Settings().resolved(44.0)
        item = dimension_for(measure, settings)
        crossbar = crossbar_nm(measure.start, measure.end, measure.height_mm)
        bar_y = panel_dimensions.to_mm(crossbar[0][1])
        # Straight out from the panel, which is where the crossbar's own
        # quarter turn points on the top side.
        self.assertAlmostEqual(bar_y - item.text.position[1],
                               text_outset_mm(settings), places=6)

    def test_it_scales_with_a_stated_lettering_height(self):
        settings = Settings(text_height_mm=6.0).resolved(44.0)
        self.assertEqual(text_outset_mm(settings), 6.9)

    def test_the_pen_is_clamped_the_way_kicad_clamps_it(self):
        # ClampTextPenSize, gr_text.cpp:73-96: a quarter of the size, at most.
        fat = Settings(text_height_mm=2.0, text_thickness_mm=1.5).resolved()
        self.assertEqual(effective_pen_mm(fat), 0.5)
        self.assertEqual(text_outset_mm(fat), 2.5)

    def test_the_value_still_reads_along_the_crossbar(self):
        measure = plan(BLANK, BOXES).by_name["board_length"]
        settings = Settings().resolved(47.5)
        outset = manual_placement(measure.start, measure.end,
                                  measure.height_mm, SIDES[measure.side],
                                  text_outset_mm(settings))
        inline = text_placement(measure.start, measure.end, measure.height_mm)
        self.assertEqual(outset[1], inline[1])
        self.assertEqual(outset[1], 90.0)

    def test_a_diagonal_crossbar_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError) as raised:
            outside_placement((0.0, 0.0), (10.0, 10.0), 5.0, 2.3)
        self.assertIn("along an axis", str(raised.exception))

    def test_the_row_gap_holds_both_values_clear_at_the_derived_lettering(self):
        # Measured ink to ink at the 2.0 mm lettering the corpus derives, on
        # the tighter of the two panels: a 5.0 mm pitch leaves 0.40 mm and the
        # 7.0 mm one leaves over 2 mm.
        items = {item.text.content: item
                 for item in drawn_items(BLANK, NARROW_BOXES)}
        gap = clearance_mm(
            text_ink_mm(items["10.5 mm"]), text_pen_mm(items["10.5 mm"]),
            text_ink_mm(items["47.5 mm"]), text_pen_mm(items["47.5 mm"]),
        )
        self.assertGreater(gap, 2.0)

    def test_the_inline_value_overlapped_its_own_arrows_on_a_small_board(self):
        # Why the value is outset, measured: with every value inline, the
        # value is wider than the board, so the arrowheads at the crossbar's
        # ends are drawn under the lettering.
        before = {
            item.text.content: own_clearance_mm(item, "crossbar", "arrows")
            for item in drawn_items(BLANK, NARROW_BOXES, inline_everything=True)
        }
        self.assertLess(before["10.5 mm"], 0.0)
        # ...and the other one is 0.05 mm off touching, which is no clearance.
        self.assertLess(before["15.5 mm"], 0.1)

    def test_the_outset_value_clears_its_crossbar_and_its_arrowheads(self):
        # Each row's text sits clear of its own crossbar and arrowheads.
        for boxes, name in ((BOXES, "15 x 18 mm boards"),
                            (NARROW_BOXES, "15.5 x 10.5 mm boards")):
            for item in drawn_items(BLANK, boxes):
                with self.subTest(panel=name, value=item.text.content):
                    self.assertGreater(
                        own_clearance_mm(item, "crossbar", "arrows"), 0.0
                    )

    def test_a_value_longer_than_what_it_measures_clears_its_extensions_too(self):
        # A 10.5 mm board is measured by a value 12.7 mm long. On the panel
        # side of its crossbar that value would run over the two extension
        # lines that mark the board's own edges - measured at -0.20 mm.
        # Outboard of the crossbar it is past the end of those lines, so it
        # clears them by 0.64 mm.
        narrow = {
            item.text.content: own_clearance_mm(item, "extensions")
            for item in drawn_items(BLANK, NARROW_BOXES)
        }
        self.assertGreater(narrow["10.5 mm"], 0.0)
        # ...and a board wide enough for its own value is clear of them as well.
        roomy = {
            item.text.content: own_clearance_mm(item, "extensions")
            for item in drawn_items(BLANK, BOXES)
        }
        self.assertGreater(roomy["15 mm"], 0.0)

    def test_no_value_overlaps_another_value_or_another_row(self):
        for boxes, name in ((BOXES, "15 x 18 mm boards"),
                            (NARROW_BOXES, "15.5 x 10.5 mm boards")):
            gaps = {
                key: value for key, value in measured(drawn_items(BLANK, boxes))
                .items() if not key[0].startswith("own")
            }
            worst = min(gaps.items(), key=lambda pair: pair[1])
            with self.subTest(panel=name, worst=worst[0]):
                self.assertGreater(worst[1], 0.0)

    def test_it_still_clears_at_the_biggest_panel_this_tool_letters(self):
        # 400 mm is the stated envelope maximum, where the lettering derives
        # 8 mm: every distance in the stage is a multiple of the text height,
        # so the clearance has to grow with it rather than shrink.
        blank = Rect(min_x=0.0, min_y=0.0, max_x=400.0, max_y=300.0)
        boxes = ((10.0, 10.0, 60.0, 70.0), (340.0, 10.0, 390.0, 70.0))
        gaps = measured(drawn_items(blank, boxes))
        worst = min(gaps.items(), key=lambda pair: pair[1])
        self.assertGreater(worst[1], 0.0, worst[0])


class LetteringHeightSettingTest(unittest.TestCase):
    """The lettering height is a setting the user can see.

    It is on the panel page, EMPTY MEANS DERIVED, and it behaves like the other
    settings - written to the config explicitly, live on a config that never
    named it, and refused by a sentence when it is not a size.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def set_height(self, value, data=None):
        return actions.set_param(
            self.data if data is None else data, self.config_path,
            "dimension_text_height_mm", value,
        )

    # -- what the run says it did -------------------------------------------

    def test_an_empty_height_is_reported_as_derived(self):
        made = plan(BLANK, BOXES)
        self.assertTrue(made.text_height_derived)
        self.assertEqual(made.text_height_mm, 2.0)

    def test_a_stated_height_is_reported_as_set(self):
        made = plan(BLANK, BOXES, {"text_height_mm": 3.5})
        self.assertFalse(made.text_height_derived)
        self.assertEqual(made.text_height_mm, 3.5)

    def test_the_drawn_panel_reports_it_too(self):
        board = board_with_layers(DIMENSION_LAYER)
        self.assertTrue(apply(board, BLANK, BOXES).text_height_derived)
        board = board_with_layers(DIMENSION_LAYER)
        made = apply(board, BLANK, BOXES, {"text_height_mm": 4.0})
        self.assertFalse(made.text_height_derived)
        self.assertEqual(made.text_height_mm, 4.0)

    def test_a_stated_height_letters_every_dimension_at_it(self):
        board = board_with_layers(DIMENSION_LAYER)
        apply(board, BLANK, BOXES, {"text_height_mm": 4.0})
        drawn = [item for item in board.drawings
                 if isinstance(item, Dimension)]
        self.assertEqual(len(drawn), 7)
        for item in drawn:
            self.assertEqual(item.text.attributes.font_size, (4.0, 4.0))

    # -- the config ----------------------------------------------------------

    def test_the_config_takes_a_number_and_takes_it_back(self):
        self.assertIsNone(PanelDimensionsConfig().text_height_mm)
        self.assertEqual(
            PanelDimensionsConfig(text_height_mm=3.0).text_height_mm, 3.0
        )
        self.assertIsNone(
            PanelDimensionsConfig(text_height_mm=None).text_height_mm
        )

    def test_a_height_of_zero_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelDimensionsConfig(text_height_mm=0.0)

    def test_a_height_past_the_bound_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelDimensionsConfig(text_height_mm=MAX_TEXT_HEIGHT_MM + 1.0)

    # -- the page's own endpoint --------------------------------------------

    def test_setting_it_states_the_whole_section(self):
        changed = self.set_height(3.0)
        self.assertTrue(changed.changed)
        section = changed.data["panel"]["dimensions"]
        self.assertEqual(section["text_height_mm"], 3.0)
        # Stated in full, like every other group: the file
        # says every number the panel was built from.
        self.assertTrue(section["enabled"])
        self.assertEqual(section["layer"], "User.16")
        self.assertIsNone(section["text_thickness_mm"])
        self.assertEqual(changed.config.panel.dimensions.text_height_mm, 3.0)

    def test_clearing_it_writes_null_and_derives_again(self):
        stated = self.set_height(3.0).data
        cleared = self.set_height(None, data=stated)
        self.assertTrue(cleared.changed)
        self.assertIsNone(
            cleared.data["panel"]["dimensions"]["text_height_mm"]
        )
        self.assertIsNone(cleared.config.panel.dimensions.text_height_mm)

    def test_clearing_an_already_empty_height_moves_nothing(self):
        # The section is written out the first time anything touches it, so the
        # FIRST clear is a change to the file even though the height did not
        # move. The second is not.
        stated = self.set_height(None)
        again = self.set_height(None, data=stated.data)
        self.assertFalse(again.changed)

    def test_a_height_past_the_bound_is_refused_by_name(self):
        with self.assertRaises(actions.ActionError) as raised:
            self.set_height(MAX_TEXT_HEIGHT_MM + 1.0)
        self.assertIn("dimension_text_height_mm", str(raised.exception))
        self.assertIn("Clear the field", str(raised.exception))

    def test_a_height_of_zero_is_refused_by_name(self):
        with self.assertRaises(actions.ActionError):
            self.set_height(0.0)

    def test_an_empty_value_is_refused_for_every_other_number(self):
        # Only one parameter may be empty. A cleared rail width is a rail
        # nobody typed, not a rail the panel sizes itself.
        with self.assertRaises(actions.ActionError) as raised:
            actions.set_param(self.data, self.config_path, "rail_mm", None)
        self.assertIn("not a value", str(raised.exception))

    def test_it_still_needs_the_panel_section_itself(self):
        path = write_panel_project(self.folder / "plain", panelize=False)
        bare = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaises(actions.ActionError) as raised:
            actions.set_param(bare, path, "dimension_text_height_mm", 3.0)
        self.assertIn("'panel' section", str(raised.exception))

    def test_only_automatic_text_heights_are_nullable(self):
        self.assertEqual(actions.NULLABLE_PARAMS,
                         frozenset({"dimension_text_height_mm", "panel_text_height_mm"}))

    # -- the page ------------------------------------------------------------

    def test_the_panel_page_carries_the_field_and_one_line(self):
        markup = page_panel._dimensions_html()
        self.assertIn("Dimension text height (mm)", markup)
        self.assertIn("Empty sizes the lettering from the panel.", markup)
        self.assertIn("id=pb-dim-text", markup)

    def test_the_group_offers_no_unit_anywhere(self):
        # Millimetres only, no units setting, and the units fields are not
        # touched.
        markup = page_panel._dimensions_html().lower()
        self.assertNotIn("<select", markup)
        self.assertNotIn("inch", markup)
        self.assertNotIn("units", markup)

    def test_the_page_reads_the_height_and_whether_they_are_drawn(self):
        params = page_panel.carrier_params(PanelizeConfig(
            rails=5.0,
            dimensions=PanelDimensionsConfig(text_height_mm=3.0),
        ))
        self.assertTrue(params["dimensions_on"])
        self.assertEqual(params["dimension_text_height_mm"], 3.0)

    def test_an_empty_height_reaches_the_page_as_nothing_typed(self):
        params = page_panel.carrier_params(PanelizeConfig(rails=5.0))
        self.assertTrue(params["dimensions_on"])
        self.assertIsNone(params["dimension_text_height_mm"])

    def test_a_config_that_declines_them_says_so_to_the_page(self):
        params = page_panel.carrier_params(PanelizeConfig(
            rails=5.0, dimensions=PanelDimensionsConfig(enabled=False),
        ))
        self.assertFalse(params["dimensions_on"])

    def test_the_field_is_an_optional_number_in_the_pages_own_table(self):
        self.assertIn(
            "['pb-dim-text', 'dimension_text_height_mm', 'optnumber']",
            page_panel.PANEL_JS,
        )


class ModuleSurfaceTest(unittest.TestCase):
    """The stage imports nothing it is not allowed to."""

    def test_the_stage_names_what_it_exports(self):
        for name in panel_dimensions.__all__:
            self.assertTrue(hasattr(panel_dimensions, name), name)


if __name__ == "__main__":
    unittest.main()
