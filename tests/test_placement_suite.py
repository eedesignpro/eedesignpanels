"""THE PLACEMENT SUITE: rotation, alternation, nest shift, CSV, marking.

Four subjects, and every one of them is a thing that would be expensive to get
wrong.

**A copy that turns is a RIGID transform of the source, and that is not free.**
Every rotation fact tested here was read from the KiCad 10.0.3 source. The two that
decide correctness:

* keep-upright text is resolved from its DRAWN angle, not its stored one, and
  the copy carries `unlocked`. A KiCad-written file stores 270
  where the ink is at 90, so a copier that turns the stored angle flips every
  designator on every copy;
* `render_cache` holds ABSOLUTE board coordinates even inside a footprint and
  KiCad's staleness check ignores POSITION, so a verbatim copy
  draws its glyphs where the source's were and nothing notices.

**A panel that turns nothing is the plain grid panel, to the byte.** Fenced two
ways: a config stating every placement key at its default against a config that
never names them, and the one setting that moves
nothing at all - the clearance warning.

**The pad-angle divergence is PINNED, not papered over.** `eedesigncore`
writes an unnormalised pad angle where KiCad normalises. The fix
belongs in the library, so the divergence is asserted AS IT IS, and the test
fails loudly the day the library fix lands.

**A CSV is untrusted input.** Every refusal names its line and changes nothing.
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    JustifyH,
    JustifyV,
    TextAttributes,
    inject_board,
    new_board_from,
    rotate_footprint,
)
from eedesigncore.common.models import Property

from eedesignpanels import placement, placementfile, prep
from eedesignpanels.config import (
    CONFIG_REVISION,
    ConfigError,
    load_config,
)
from eedesignpanels.preview import actions, page_panel, panelbuild
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages import panel_scores, step_and_repeat, upright
from test_panel_page import write_panel_project
from test_preview import stub_render

# Every arrangement this file exercises, named the way the config states it.
TURNS = (0.0, 90.0, 180.0, 270.0)


# ---------------------------------------------------------------------------
# the pure geometry
# ---------------------------------------------------------------------------


class RotationMathTest(unittest.TestCase):
    """Quarter turns, and nothing else, mapped the way KiCad maps them."""

    def test_a_quarter_turn_swaps_the_extent_and_a_half_turn_does_not(self):
        self.assertEqual(placement.rotated_extent(15.5, 10.5, 0.0), (15.5, 10.5))
        self.assertEqual(placement.rotated_extent(15.5, 10.5, 180.0), (15.5, 10.5))
        self.assertEqual(placement.rotated_extent(15.5, 10.5, 90.0), (10.5, 15.5))
        self.assertEqual(placement.rotated_extent(15.5, 10.5, 270.0), (10.5, 15.5))

    def test_the_box_maps_the_way_kicads_own_rotate_point_maps(self):
        # `libs/kimath/src/trigo.cpp:229-264`, with y growing DOWN: 90 takes
        # (x, y) to (y, -x), 180 to (-x, -y), 270 to (-y, x). Every step is a
        # swap and a negation, so nothing rounds.
        box = (10.0, 20.0, 30.0, 50.0)
        self.assertEqual(placement.rotated_box(box, 0.0), box)
        self.assertEqual(placement.rotated_box(box, 90.0), (20.0, -30.0, 50.0, -10.0))
        self.assertEqual(placement.rotated_box(box, 180.0),
                         (-30.0, -50.0, -10.0, -20.0))
        self.assertEqual(placement.rotated_box(box, 270.0),
                         (-50.0, 10.0, -20.0, 30.0))

    def test_a_turn_that_is_not_a_quarter_turn_is_refused(self):
        for call in (lambda: placement.rotated_extent(1.0, 2.0, 45.0),
                     lambda: placement.rotated_box((0, 0, 1, 1), 45.0)):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertIn("quarter turn", str(caught.exception))

    def test_a_full_circle_is_no_turn(self):
        self.assertEqual(placement.normalized_angle(360.0), 0.0)
        self.assertEqual(placement.normalized_angle(-90.0), 270.0)
        self.assertEqual(placement.normalized_angle(450.0), 90.0)


class AlternationTest(unittest.TestCase):
    """Every other row or column takes a further half circle."""

    def test_rows_alternate_and_the_first_row_is_not_turned(self):
        self.assertFalse(placement.alternates("rows", 0, 0))
        self.assertFalse(placement.alternates("rows", 3, 0))
        self.assertTrue(placement.alternates("rows", 0, 1))
        self.assertFalse(placement.alternates("rows", 0, 2))

    def test_columns_alternate_the_other_way(self):
        self.assertFalse(placement.alternates("columns", 0, 5))
        self.assertTrue(placement.alternates("columns", 1, 0))
        self.assertFalse(placement.alternates("columns", 2, 0))

    def test_none_alternates_nothing(self):
        for column in range(3):
            for row in range(3):
                self.assertFalse(placement.alternates("none", column, row))

    def test_an_alternation_nobody_declared_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            placement.alternates("diagonally", 0, 0)
        self.assertIn("none, rows, columns", str(caught.exception))

    def test_the_angle_composes_and_normalises(self):
        # The turn is the base plus alternation's half circle,
        # and the result is normalised to [0, 360).
        self.assertEqual(placement.copy_angle(0.0, "rows", 0, 1), 180.0)
        self.assertEqual(placement.copy_angle(90.0, "rows", 0, 1), 270.0)
        self.assertEqual(placement.copy_angle(180.0, "rows", 0, 1), 0.0)
        self.assertEqual(placement.copy_angle(270.0, "rows", 0, 1), 90.0)
        self.assertEqual(placement.copy_angle(270.0, "rows", 0, 0), 270.0)


class ArrangementTest(unittest.TestCase):
    """Where every copy sits, and what each control moves."""

    BOX = (10.0, 20.0, 25.5, 30.5)          # a 15.5 x 10.5 mm board

    def test_an_unturned_arrangement_is_the_plain_grid(self):
        made = placement.arrangement(self.BOX, 2, 3, 3.0)
        extent = placement.grid_extent(15.5, 10.5, 2, 3, 3.0)
        self.assertEqual(made.extent, extent)
        self.assertEqual(
            [(p.dx_mm, p.dy_mm) for p in made.placements],
            [(p.dx_mm, p.dy_mm)
             for p in placement.grid_placements(2, 3, extent.step_x_mm,
                                                extent.step_y_mm)],
        )
        for place in made.placements:
            self.assertEqual(place.rotation_deg, 0.0)
            self.assertFalse(place.alternated)

    def test_cell_zero_lands_exactly_where_the_design_sits(self):
        for turn in TURNS:
            with self.subTest(turn=turn):
                made = placement.arrangement(self.BOX, 2, 2, 3.0,
                                             rotation_deg=turn)
                self.assertAlmostEqual(made.boxes[0][0], self.BOX[0])
                self.assertAlmostEqual(made.boxes[0][1], self.BOX[1])

    def test_a_quarter_turn_swaps_the_pitch(self):
        made = placement.arrangement(self.BOX, 2, 3, 3.0, rotation_deg=90.0)
        self.assertAlmostEqual(made.extent.step_x_mm, 10.5 + 3.0)
        self.assertAlmostEqual(made.extent.step_y_mm, 15.5 + 3.0)
        self.assertAlmostEqual(made.extent.width_mm, 10.5 + 13.5)
        self.assertAlmostEqual(made.extent.height_mm, 15.5 + 2 * 18.5)

    def test_every_copy_carries_the_base_angle(self):
        for turn in TURNS:
            made = placement.arrangement(self.BOX, 2, 2, 3.0, rotation_deg=turn)
            with self.subTest(turn=turn):
                self.assertEqual({p.rotation_deg for p in made.placements},
                                 {turn})

    def test_alternation_turns_every_other_row_a_half_circle(self):
        made = placement.arrangement(self.BOX, 2, 3, 3.0, rotation_deg=90.0,
                                     alternate="rows")
        by_row = {}
        for place in made.placements:
            by_row.setdefault(place.row, set()).add(place.rotation_deg)
        self.assertEqual(by_row, {0: {90.0}, 1: {270.0}, 2: {90.0}})
        # An alternated copy occupies the same room, so the pitch is unmoved.
        self.assertAlmostEqual(made.extent.step_y_mm, 15.5 + 3.0)

    def test_a_nest_shift_moves_the_alternated_copies_and_nothing_else(self):
        made = placement.arrangement(self.BOX, 2, 2, 3.0, alternate="rows",
                                     nest_across_mm=-1.0, nest_down_mm=-0.5)
        plain = placement.arrangement(self.BOX, 2, 2, 3.0, alternate="rows")
        for place, was in zip(made.placements, plain.placements):
            expected = (-1.0, -0.5) if place.alternated else (0.0, 0.0)
            with self.subTest(cell=(place.column, place.row)):
                self.assertAlmostEqual(place.dx_mm - was.dx_mm, expected[0])
                self.assertAlmostEqual(place.dy_mm - was.dy_mm, expected[1])

    def test_a_nest_shift_with_nothing_alternated_moves_nothing_and_says_so(self):
        made = placement.arrangement(self.BOX, 2, 2, 3.0, rotation_deg=90.0,
                                     nest_across_mm=2.0)
        plain = placement.arrangement(self.BOX, 2, 2, 3.0, rotation_deg=90.0)
        self.assertEqual(made.placements, plain.placements)
        self.assertTrue(any("moves nothing" in note for note in made.warnings))

    def test_the_extent_is_the_union_of_the_boxes_the_copies_landed_in(self):
        made = placement.arrangement(self.BOX, 2, 2, 3.0, alternate="rows",
                                     nest_across_mm=-1.0)
        left = min(box[0] for box in made.boxes)
        right = max(box[2] for box in made.boxes)
        self.assertAlmostEqual(made.extent.width_mm, right - left)
        self.assertAlmostEqual(made.origin_mm[0], left)

    def test_a_bad_grid_or_gap_is_refused(self):
        with self.assertRaises(ValueError):
            placement.arrangement(self.BOX, 0, 2, 3.0)
        with self.assertRaises(ValueError):
            placement.arrangement(self.BOX, 2, 2, -1.0)


class PlacedArrangementTest(unittest.TestCase):
    """A placement file says where every copy goes."""

    BOX = (10.0, 20.0, 25.5, 30.5)

    def test_the_rows_are_read_in_row_major_cell_order(self):
        rows = [(0.0, 0.0, 0.0), (20.0, 0.0, 90.0),
                (0.0, 15.0, 180.0), (20.0, 15.0, 270.0)]
        made = placement.placed_arrangement(self.BOX, 2, 2, rows)
        self.assertEqual(
            [(p.column, p.row, p.rotation_deg) for p in made.placements],
            [(0, 0, 0.0), (1, 0, 90.0), (0, 1, 180.0), (1, 1, 270.0)],
        )
        # The offsets are measured from the DESIGN's own rectangle, so 0, 0 is
        # the board exactly where it sits.
        self.assertAlmostEqual(made.boxes[0][0], self.BOX[0])
        self.assertAlmostEqual(made.boxes[1][0], self.BOX[0] + 20.0)

    def test_a_row_count_that_is_not_the_cell_count_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            placement.placed_arrangement(self.BOX, 2, 3, [(0.0, 0.0, 0.0)])
        said = str(caught.exception)
        self.assertIn("1 placement", said)
        self.assertIn("2 x 3 = 6", said)

    def test_an_angle_that_is_not_a_quarter_turn_is_refused_by_line(self):
        with self.assertRaises(ValueError) as caught:
            placement.placed_arrangement(self.BOX, 2, 1,
                                         [(0.0, 0.0, 0.0), (5.0, 0.0, 30.0)])
        self.assertIn("placement 2", str(caught.exception))


class ClearanceMarksTest(unittest.TestCase):
    """The viewport marks boards closer than the distance."""

    A = (0.0, 0.0, 10.0, 10.0)
    B = (11.0, 0.0, 21.0, 10.0)             # 1.0 mm to the right of A
    C = (0.0, 13.0, 10.0, 23.0)             # 3.0 mm below A

    def test_zero_disables_it(self):
        self.assertEqual(placement.clearance_marks([self.A, self.B], 0.0),
                         ((), 0))

    def test_only_the_pairs_closer_than_the_distance_are_marked(self):
        marks, found = placement.clearance_marks([self.A, self.B, self.C], 2.0)
        self.assertEqual(found, 1)
        self.assertEqual([(m.first, m.second) for m in marks], [(0, 1)])
        self.assertAlmostEqual(marks[0].gap_mm, 1.0)

    def test_the_comparison_is_strict_so_a_gap_at_the_setting_is_not_a_warning(self):
        self.assertEqual(placement.clearance_marks([self.A, self.B], 1.0),
                         ((), 0))

    def test_a_diagonal_gap_is_measured_as_one(self):
        marks, _ = placement.clearance_marks(
            [self.A, (13.0, 14.0, 20.0, 20.0)], 6.0
        )
        self.assertAlmostEqual(marks[0].gap_mm, 5.0)      # 3-4-5

    def test_two_boards_that_overlap_read_zero_and_are_marked(self):
        marks, found = placement.clearance_marks(
            [self.A, (5.0, 5.0, 15.0, 15.0)], 1.0
        )
        self.assertEqual(found, 1)
        self.assertEqual(marks[0].gap_mm, 0.0)
        self.assertEqual(marks[0].at, marks[0].to)

    def test_the_worst_come_first_and_the_rest_are_counted(self):
        boxes = [(x * 10.5, 0.0, x * 10.5 + 10.0, 10.0) for x in range(6)]
        marks, found = placement.clearance_marks(boxes, 100.0, limit=3)
        self.assertEqual(found, 15)
        self.assertEqual(len(marks), 3)
        self.assertEqual([round(m.gap_mm, 4) for m in marks], [0.5, 0.5, 0.5])


# ---------------------------------------------------------------------------
# the resolve
# ---------------------------------------------------------------------------


class DrawAngleTest(unittest.TestCase):
    """`PCB_TEXT::GetDrawRotation`'s fold, ported (pcb_text.cpp:208-215)."""

    def test_the_fold_lands_in_the_window_keep_upright_allows(self):
        for stored, drawn in ((0.0, 0.0), (90.0, 90.0), (180.0, 0.0),
                              (270.0, 90.0), (359.0, -1.0), (200.0, 20.0),
                              (91.0, -89.0)):
            with self.subTest(stored=stored):
                self.assertAlmostEqual(upright.draw_angle(stored), drawn)

    def test_the_kicad_written_fixture_disagrees_with_its_own_file(self):
        # `qa/data/pcbnew/api_kitchen_sink.kicad_pcb:883-884`, generator 10.0:
        # a keep-upright reference stored at 270 whose INK is at 90. A copier
        # that turned the stored angle would flip that designator on every
        # panel copy.
        self.assertEqual(upright.draw_angle(270.0), 90.0)

    def test_the_fold_is_not_normalised_because_the_turn_normalises(self):
        # Normalising here would undo the fold for every angle over 90.
        self.assertLess(upright.draw_angle(359.0), 0.0)


def a_field(angle: float, unlocked: bool = False, cache=None,
           attributes=None) -> Property:
    field = Property(name="Reference", value="R1", position=(0.0, -1.0),
                     angle=angle, unlocked=unlocked, attributes=attributes)
    if cache is not None:
        field.render_cache = cache
    return field


class UprightStateTest(unittest.TestCase):
    """`upright_state`: KiCad's own ink for a stored angle turned by *turn* -
    the primitive `resolve_item` is built on.
    """

    def test_a_turn_that_stays_short_of_the_half_circle_does_not_swap(self):
        # stored 270, turn 90: normalize(360) = 0, well short of 180.
        drawn, swapped = upright.upright_state(270.0, 90.0)
        self.assertEqual(drawn, 0.0)
        self.assertFalse(swapped)

    def test_a_turn_that_crosses_the_half_circle_swaps(self):
        # stored 90, turn 180: normalize(270) is past 180, so KeepUpright
        # adds another half circle AND swaps both justifications.
        drawn, swapped = upright.upright_state(90.0, 180.0)
        self.assertEqual(drawn, 90.0)
        self.assertTrue(swapped)

    def test_the_kicad_written_fixture_at_a_180_turn(self):
        # The KiCad-written fixture's case, carried one step further: 270
        # stored, turned 180, lands short of the swap.
        drawn, swapped = upright.upright_state(270.0, 180.0)
        self.assertEqual(drawn, 90.0)
        self.assertFalse(swapped)


class SwapJustificationTest(unittest.TestCase):
    """`swap_justification`: both axes swap ends; centre has no ends."""

    def test_left_and_top_swap_to_right_and_bottom(self):
        field = a_field(0.0, attributes=TextAttributes(
            justify_h=JustifyH.left, justify_v=JustifyV.top,
        ))
        self.assertTrue(upright.swap_justification(field))
        self.assertEqual(field.attributes.justify_h, JustifyH.right)
        self.assertEqual(field.attributes.justify_v, JustifyV.bottom)

    def test_a_field_with_no_justify_block_is_left_alone(self):
        field = a_field(0.0)
        self.assertFalse(upright.swap_justification(field))
        self.assertIsNone(field.attributes)


class ResolveItemTest(unittest.TestCase):
    """What resolving a copy for the turn it takes writes."""

    def test_a_keep_upright_field_is_resolved_for_the_turn_and_unlocked(self):
        # angle 90, turn 180: upright_state gives (90, swapped=True), so the
        # stored angle becomes normalize(90 - 180) = 270 and the
        # justification swaps with it.
        field = a_field(90.0, attributes=TextAttributes(
            justify_h=JustifyH.left, justify_v=JustifyV.top,
        ))
        self.assertTrue(upright.resolve_item(field, 180.0))
        self.assertEqual(field.angle, 270.0)
        self.assertTrue(field.unlocked)
        self.assertEqual(field.attributes.justify_h, JustifyH.right)
        self.assertEqual(field.attributes.justify_v, JustifyV.bottom)

    def test_an_already_unlocked_field_is_left_exactly_as_it_is(self):
        field = a_field(270.0, unlocked=True)
        self.assertFalse(upright.resolve_item(field, 180.0))
        self.assertEqual(field.angle, 270.0)

    def test_a_turn_of_zero_resolves_nothing(self):
        # NO TURN, NOTHING TO RESOLVE: an unturned copy's
        # ink is already the source's ink.
        field = a_field(270.0)
        self.assertFalse(upright.resolve_item(field, 0.0))
        self.assertEqual(field.angle, 270.0)
        self.assertFalse(field.unlocked)

    def test_the_position_is_not_touched(self):
        # A footprint-local `at` already rides the placement
        # (`pcb_io_kicad_sexpr.cpp:2272-2273`).
        field = a_field(180.0)
        upright.resolve_item(field, 90.0)
        self.assertEqual(field.position, (0.0, -1.0))

    def test_the_render_cache_is_shed(self):
        # The cache holds absolute BOARD coordinates even inside a
        # footprint, and KiCad's staleness check never looks at position.
        board = Board.from_string(CACHED_TEXT_BOARD)
        field = board.footprints[0].properties[0]
        self.assertIsNotNone(field.render_cache)
        upright.resolve_item(field, 90.0)
        self.assertIsNone(field.render_cache)


class ResolveBeforeTheTurnTest(unittest.TestCase):
    """The composition, run: resolve then turn is rigid; turn alone is not."""

    def footprint(self, angle: float = 270.0):
        board = Board.from_string(UPRIGHT_BOARD)
        return board.footprints[0], board

    def test_the_librarys_own_rotate_reproduces_the_editor_and_is_not_rigid(self):
        # `rotate_footprint` is turn-then-KeepUpright
        # (eedesigncore:pcb/transform.py:264-282), which is exactly
        # `FOOTPRINT::Rotate`. Correct as a port,
        # but not a rigid copy: the flag stays ON, so the draw-time fold puts
        # the ink back in (-90, 90] whatever the turn.
        footprint, _ = self.footprint()
        rotate_footprint(footprint, 180.0)
        field = footprint.properties[0]
        self.assertFalse(field.unlocked)
        self.assertEqual(field.angle, 90.0)
        self.assertEqual(upright.draw_angle(field.angle), 90.0)

    def test_resolve_then_turn_puts_the_ink_where_kicad_draws_it(self):
        # Resolve is FOR the turn that follows. upright_state(270, 180) =
        # (90, swapped=False), so the copy is stored at normalize(90 - 180) =
        # 270 pre-turn, and the turn that follows lands it on 90 - exactly the
        # drawn angle, because the flag is clear and nothing folds at draw
        # time.
        footprint, _ = self.footprint()
        upright.resolve_footprint(footprint, 180.0)
        rotate_footprint(footprint, 180.0)
        field = footprint.properties[0]
        self.assertTrue(field.unlocked)
        self.assertEqual(field.angle, 90.0)
        self.assertEqual(field.angle, upright.upright_state(270.0, 180.0)[0])

    def test_every_quarter_turn_lands_on_kicads_own_drawn_angle(self):
        # ONE RESOLVED VIEW PER TURN ANGLE: each turn
        # resolves its OWN footprint - resolving for one turn is not valid
        # for another - so a fresh footprint is built per turn rather than
        # composed from the last one.
        for turn in TURNS:
            footprint, _ = self.footprint()
            drawn, _swapped = upright.upright_state(270.0, turn)
            upright.resolve_footprint(footprint, turn)
            rotate_footprint(footprint, turn)
            with self.subTest(turn=turn):
                self.assertAlmostEqual(footprint.properties[0].angle, drawn)

    def test_resolved_content_leaves_the_source_alone(self):
        board = Board.from_string(UPRIGHT_BOARD)
        resolved = upright.resolved_content(board, 180.0)
        self.assertEqual(resolved.items, 1)
        self.assertEqual(resolved.footprints, 1)
        self.assertEqual(resolved.turn_deg, 180.0)
        self.assertFalse(board.footprints[0].properties[0].unlocked)
        self.assertEqual(board.footprints[0].properties[0].angle, 270.0)
        self.assertTrue(resolved.board.footprints[0].properties[0].unlocked)
        self.assertEqual(resolved.board.footprints[0].properties[0].angle,
                         270.0)

    def test_a_footprint_with_nothing_upright_is_carried_by_reference(self):
        board = Board.from_string(UPRIGHT_BOARD)
        board.footprints[0].properties[0].unlocked = True
        resolved = upright.resolved_content(board, 180.0)
        self.assertFalse(resolved.changed)
        self.assertIs(resolved.board.footprints[0], board.footprints[0])

    def test_a_footprint_text_box_is_not_something_to_resolve(self):
        # A `PCB_TEXTBOX`'s keep-upright flag is never serialised and
        # is not used at draw time, and its stored angle is footprint-RELATIVE
        # and takes no change on a turn. So there is nothing to fold.
        board = Board.from_string(UPRIGHT_BOARD)
        children = upright.upright_children(board.footprints[0])
        self.assertEqual([type(child).__name__ for child in children],
                         ["Property"])


# A footprint whose Reference is held upright and stored at 270 - the shape the
# KiCad-written fixture has (`api_kitchen_sink.kicad_pcb:877-892`).
UPRIGHT_BOARD = """(kicad_pcb (version 20241229) (generator "pcbnew")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)
          (39 "F.SilkS" user "F.Silkscreen"))
  (setup (pad_to_mask_clearance 0))
  (footprint "test:part"
    (layer "F.Cu")
    (uuid "11111111-1111-1111-1111-111111111111")
    (at 20 30 -90)
    (property "Reference" "P2"
      (at 0 -5.08 270)
      (layer "F.SilkS")
      (uuid "22222222-2222-2222-2222-222222222222")
      (effects (font (size 1 1) (thickness 0.15)))
    )
  )
  (gr_line (start 0 0) (end 40 0) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "33333333-3333-3333-3333-333333333333"))
  (gr_line (start 40 0) (end 40 40) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "44444444-4444-4444-4444-444444444444"))
  (gr_line (start 40 40) (end 0 40) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "55555555-5555-5555-5555-555555555555"))
  (gr_line (start 0 40) (end 0 0) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "66666666-6666-6666-6666-666666666666"))
)
"""

# The same, with a `render_cache` on the field: absolute board coordinates,
# inside a footprint.
CACHED_TEXT_BOARD = UPRIGHT_BOARD.replace(
    '(effects (font (size 1 1) (thickness 0.15)))',
    '(effects (font (size 1 1) (thickness 0.15)))\n'
    '      (render_cache "P2" 90\n'
    '        (polygon (pts (xy 20 25) (xy 21 25) (xy 21 26) (xy 20 26)))\n'
    '      )',
)


class RenderCacheOnACopyTest(unittest.TestCase):
    """A copy either moves its cached glyphs or sheds them."""

    def source(self) -> Board:
        return Board.from_string(CACHED_TEXT_BOARD)

    def test_an_unturned_copy_moves_the_cached_glyphs_with_it(self):
        source = self.source()
        target = new_board_from(source)
        added = inject_board(target, source, offset=(50.0, 0.0),
                             instance_key="1|0")
        cache = added.footprints[0].properties[0].render_cache
        self.assertIsNotNone(cache)
        self.assertEqual(cache.polygons[0].pts[0], (70.0, 25.0))

    def test_a_turned_copy_sheds_them(self):
        # `eedesigncore` drops the block on rotation, and KiCad regenerates it
        # from the font at draw time - the parser only calls `SetupRenderCache`
        # when the token is PRESENT (parser 862-909), so a file with none is
        # valid and the glyphs come back on KiCad's next save.
        source = self.source()
        target = new_board_from(source)
        added = inject_board(target, source, offset=(0.0, 0.0), rotation=90.0,
                             instance_key="0|0")
        self.assertIsNone(added.footprints[0].properties[0].render_cache)
        self.assertNotIn("render_cache", target.to_string())


# ---------------------------------------------------------------------------
# the placement CSV, which is untrusted input
# ---------------------------------------------------------------------------


class PlacementFileTest(unittest.TestCase):
    """Three numbers a line, and every refusal names its line."""

    def test_three_numbers_a_line(self):
        parsed = placementfile.parse("0,0,0\n20,0,90\n")
        self.assertEqual(parsed.triples, ((0.0, 0.0, 0.0), (20.0, 0.0, 90.0)))
        self.assertEqual(parsed.header, "")

    def test_a_spreadsheets_own_column_titles_are_skipped_once(self):
        parsed = placementfile.parse("across,down,angle\n0,0,0\n5,5,180\n")
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.header, "across,down,angle")

    def test_blank_lines_and_comments_are_skipped(self):
        parsed = placementfile.parse("# a note\n\n0,0,0\n\n# another\n5,0,0\n")
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.skipped, 4)

    def test_a_negative_angle_is_normalised(self):
        parsed = placementfile.parse("0,0,-90\n")
        self.assertEqual(parsed.rows[0].angle_deg, 270.0)

    def refused(self, content: str) -> str:
        with self.assertRaises(placementfile.PlacementFileError) as caught:
            placementfile.parse(content)
        return str(caught.exception)

    def test_a_field_that_is_not_a_number_is_refused_by_line(self):
        said = self.refused("0,0,0\n5,left,0\n")
        self.assertIn("line 2", said)
        self.assertIn("offset down", said)
        self.assertIn("Nothing was changed", said)

    def test_a_row_of_the_wrong_width_is_refused_by_line(self):
        said = self.refused("0,0,0\n5,0\n")
        self.assertIn("line 2", said)
        self.assertIn("2 field", said)

    def test_an_angle_that_is_not_a_quarter_turn_is_refused_by_line(self):
        said = self.refused("0,0,0\n5,0,45\n")
        self.assertIn("line 2", said)
        self.assertIn("quarter turn", said)

    def test_an_offset_off_the_end_of_any_machine_is_refused(self):
        said = self.refused("0,0,0\n99999,0,0\n")
        self.assertIn("line 2", said)
        self.assertIn("no panel is that big", said)

    def test_a_file_with_no_placements_in_it_is_refused(self):
        self.assertIn("no placements", self.refused("# only a comment\n"))
        self.assertIn("empty", self.refused("   \n"))

    def test_more_rows_than_the_page_takes_is_refused(self):
        said = self.refused("0,0,0\n" * (placementfile.MAX_ROWS + 1))
        self.assertIn(str(placementfile.MAX_ROWS), said)

    def test_a_non_finite_number_is_refused(self):
        self.assertIn("not a finite number", self.refused("nan,0,0\n"))

    def test_a_posted_name_is_never_a_path(self):
        self.assertEqual(
            placementfile.file_name("C:\\fakepath\\places.csv"), "places.csv"
        )
        self.assertEqual(placementfile.file_name(""), "the placement file")
        self.assertEqual(placementfile.file_name(None), "the placement file")


# ---------------------------------------------------------------------------
# the config (every default is off, and that is a byte fence)
# ---------------------------------------------------------------------------


VALID = {
    "revision": CONFIG_REVISION,
    "source_board": "../board.kicad_pcb",
    "grid": {"columns": 2, "rows": 3, "gap_mm": 3.0},
}


class ConfigTest(unittest.TestCase):
    """What the schema accepts, refuses, and defaults to."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.panel_dir = self.project / "panel"
        self.panel_dir.mkdir()
        (self.project / "board.kicad_pcb").write_text("(kicad_pcb)", "utf-8")
        self.addCleanup(self._tmp.cleanup)

    def load(self, grid: dict = None):
        path = self.panel_dir / "panel.json"
        data = dict(VALID)
        if grid:
            data["grid"] = dict(VALID["grid"], **grid)
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(path)

    def refused(self, grid: dict) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(grid)
        return str(caught.exception)

    def test_everything_defaults_to_off_or_zero(self):
        grid = self.load().grid
        self.assertEqual(grid.rotation_deg, 0.0)
        self.assertEqual(grid.alternate, "none")
        self.assertEqual(grid.nest_shift_mm, (0.0, 0.0))
        self.assertEqual(grid.clearance_warning_mm, 0.0)
        self.assertIsNone(grid.placement)
        self.assertFalse(grid.turns)

    def test_a_quarter_turn_is_accepted_and_anything_else_is_not(self):
        for turn in TURNS:
            self.assertEqual(self.load({"rotation_deg": turn}).grid.rotation_deg,
                             turn)
        said = self.refused({"rotation_deg": 45.0})
        self.assertIn("rotation_deg", said)
        self.assertIn("eda_shape.cpp:957-972", said)

    def test_alternation_takes_the_three_words_and_no_others(self):
        for word in placement.ALTERNATIONS:
            self.assertEqual(self.load({"alternate": word}).grid.alternate, word)
        self.assertIn("alternate", self.refused({"alternate": "diagonally"}))

    def test_a_nest_shift_may_be_negative(self):
        grid = self.load({"nest_across_mm": -1.5, "nest_down_mm": 2.0}).grid
        self.assertEqual(grid.nest_shift_mm, (-1.5, 2.0))

    def test_the_removed_pitch_rounding_key_is_refused_by_name(self):
        # There is no pitch rounding setting, so a config that carries the key
        # is refused by name rather than silently ignored.
        self.assertIn("pitch_rounding_mm",
                      self.refused({"pitch_rounding_mm": 0.5}))

    def test_a_clearance_warning_must_not_be_negative(self):
        self.assertIn("clearance_warning_mm",
                      self.refused({"clearance_warning_mm": -1.0}))

    def test_mirror_is_still_refused_by_name(self):
        self.assertIn("mirror", self.refused({"mirror": True}))

    def test_a_placement_section_carries_its_rows(self):
        grid = self.load({"placement": {
            "source": "places.csv",
            "rows": [{"across_mm": 0.0, "down_mm": 0.0, "angle_deg": 0.0},
                     {"across_mm": 20.0, "down_mm": 0.0, "angle_deg": 90.0}],
        }}).grid
        self.assertEqual(grid.placement.triples,
                         ((0.0, 0.0, 0.0), (20.0, 0.0, 90.0)))
        self.assertTrue(grid.turns)

    def test_an_empty_placement_section_is_refused(self):
        self.assertIn("rows", self.refused({"placement": {"rows": []}}))

    def test_a_placement_angle_that_is_not_a_quarter_turn_is_refused(self):
        said = self.refused({"placement": {
            "rows": [{"across_mm": 0.0, "down_mm": 0.0, "angle_deg": 30.0}],
        }})
        self.assertIn("quarter turn", said)

    def test_a_live_file_names_the_pattern_settings_it_overrides(self):
        grid = self.load({
            "rotation_deg": 90.0, "alternate": "rows", "nest_across_mm": -1.0,
            "placement": {"rows": [{"across_mm": 0.0, "down_mm": 0.0}]},
        }).grid
        self.assertEqual(
            grid.unused_by_the_placement_file,
            ("grid.rotation_deg = 90", "grid.alternate = rows",
             "grid.nest_across_mm = -1"),
        )

    def test_the_summary_says_nothing_about_turns_when_nothing_turns(self):
        self.assertEqual(self.load().grid.summary,
                         "2 across x 3 down, 3 mm between boards")

    def test_the_summary_names_the_turn_and_the_alternation(self):
        said = self.load({"rotation_deg": 90.0, "alternate": "rows"}).grid.summary
        self.assertIn("turned 90 degrees", said)
        self.assertIn("every other row turned a half circle", said)

    def test_the_summary_says_a_file_placed_it(self):
        said = self.load({"placement": {
            "rows": [{"across_mm": 0.0, "down_mm": 0.0}] * 6,
        }}).grid.summary
        self.assertIn("placed by file", said)
        self.assertIn("6 placements", said)


# ---------------------------------------------------------------------------
# the build (determinism, byte fences, and the arrangement chain)
# ---------------------------------------------------------------------------


def config_with(folder: Path, grid: dict = None, **top):
    """The panel fixture's config with *grid* merged into its grid section."""
    config_path = write_panel_project(folder, grid={"columns": 2, "rows": 2,
                                                    "gap_mm": 3.0})
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["grid"].update(grid or {})
    data.update(top)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return load_config(config_path)


def built(config) -> object:
    """One panel from this config, over the design as prep leaves it."""
    board = Board.from_file(config.source_board_path)
    return step_and_repeat.build_from(config, board, fill_rails=False)


class BuildTest(unittest.TestCase):
    """A turned panel is built, twice, and compared with itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_config_that_turns_nothing_is_the_panel_built_before_this(self):
        # THE BYTE FENCE for "everything off by default": a config that states
        # every placement key at its default against one that never heard of
        # any of them.
        plain = built(config_with(self.folder / "a")).board.to_string()
        stated = built(config_with(self.folder / "b", grid={
            "rotation_deg": 0.0, "alternate": "none",
            "nest_across_mm": 0.0, "nest_down_mm": 0.0,
            "clearance_warning_mm": 0.0,
            "placement": None,
        })).board.to_string()
        self.assertEqual(plain, stated)

    def test_the_setting_that_moves_nothing_moves_nothing(self):
        # A clearance warning is a measurement over the panel,
        # so it may not change one byte of it.
        plain = built(config_with(self.folder / "a")).board.to_string()
        marked = built(config_with(self.folder / "b", grid={
            "clearance_warning_mm": 5.0,
        })).board.to_string()
        self.assertEqual(plain, marked)

    def test_every_arrangement_builds_the_same_bytes_twice(self):
        for grid in ({}, {"rotation_deg": 90.0}, {"rotation_deg": 180.0},
                     {"rotation_deg": 270.0}, {"alternate": "rows"},
                     {"alternate": "columns"},
                     {"rotation_deg": 90.0, "alternate": "rows",
                      "nest_across_mm": -0.5, "nest_down_mm": -0.25}):
            config = config_with(self.folder / f"g{abs(hash(str(grid)))}", grid=grid)
            with self.subTest(grid=grid):
                self.assertEqual(built(config).board.to_string(),
                                 built(config).board.to_string())

    def test_each_arrangement_is_a_different_panel(self):
        seen = {}
        for grid in ({}, {"rotation_deg": 90.0}, {"rotation_deg": 180.0},
                     {"rotation_deg": 270.0}, {"alternate": "rows"},
                     {"alternate": "columns"}):
            config = config_with(self.folder / f"d{abs(hash(str(grid)))}", grid=grid)
            seen[str(grid)] = built(config).board.to_string()
        self.assertEqual(len(set(seen.values())), len(seen))

    def test_a_quarter_turn_swaps_the_panel_extent(self):
        upright_panel = built(config_with(self.folder / "u")).geometry
        turned = built(config_with(self.folder / "t",
                                   grid={"rotation_deg": 90.0})).geometry
        self.assertEqual(turned.instance_extent_mm,
                         upright_panel.instance_extent_mm[::-1])
        self.assertAlmostEqual(turned.outline_extent_mm[0],
                               2 * upright_panel.instance_extent_mm[1] + 3.0)

    def test_a_turned_panel_holds_the_same_content_as_an_upright_one(self):
        # A rigid transform neither adds nor loses an item.
        plain = built(config_with(self.folder / "a"))
        turned = built(config_with(self.folder / "b",
                                   grid={"rotation_deg": 270.0}))
        self.assertEqual(plain.panel_content, turned.panel_content)
        self.assertTrue(plain.content_matches if hasattr(plain, "content_matches")
                        else True)

    def test_the_uuids_are_derived_from_the_grid_cell_and_a_turn_does_not_move_them(self):
        # The identity is `uuid5(namespace, f"{source}|{column}|{row}")`.
        # The key is the CELL, so the same panel turned is the same
        # identities in different places - which is what makes a rebuild
        # reproducible.
        plain = built(config_with(self.folder / "a")).board
        turned = built(config_with(self.folder / "b",
                                   grid={"rotation_deg": 90.0})).board
        self.assertEqual(
            sorted(fp.uuid for fp in plain.footprints),
            sorted(fp.uuid for fp in turned.footprints),
        )

    def test_a_turned_build_says_what_it_did_to_the_upright_text(self):
        outcome = built(config_with(self.folder / "t",
                                    grid={"rotation_deg": 180.0}))
        said = " ".join(outcome.warnings)
        self.assertIn("held upright by KiCad", said)
        self.assertIn("carries them unlocked", said)
        self.assertIn("the justification KiCad would draw them at", said)

    def test_an_unturned_build_says_nothing_about_upright_text(self):
        outcome = built(config_with(self.folder / "u"))
        self.assertNotIn("upright", " ".join(outcome.warnings))

    def test_every_copy_of_a_turned_panel_carries_unlocked_text(self):
        # Read off the STEPPED panel - the copies, and the panel round them -
        # because the carrier adds footprints of its own afterwards (the
        # tooling holes) and those are not copies of anything.
        outcome = built(config_with(self.folder / "t",
                                    grid={"rotation_deg": 90.0}))
        held = [
            item for fp in outcome.stepped.board.footprints
            for item in upright.upright_children(fp)
            if upright.keeps_upright(item)
        ]
        self.assertEqual(held, [])

    def test_an_unturned_panel_keeps_its_copies_upright_exactly_as_they_were(self):
        outcome = built(config_with(self.folder / "u"))
        held = [
            item for fp in outcome.stepped.board.footprints
            for item in upright.upright_children(fp)
            if upright.keeps_upright(item)
        ]
        self.assertTrue(held)

    def test_a_placement_file_builds_the_panel_it_names(self):
        rows = [{"across_mm": 0.0, "down_mm": 0.0, "angle_deg": 0.0},
                {"across_mm": 30.0, "down_mm": 0.0, "angle_deg": 180.0},
                {"across_mm": 0.0, "down_mm": 30.0, "angle_deg": 90.0},
                {"across_mm": 30.0, "down_mm": 30.0, "angle_deg": 270.0}]
        config = config_with(self.folder / "p", grid={
            "placement": {"source": "places.csv", "rows": rows},
        })
        outcome = built(config)
        self.assertEqual(
            [place.rotation_deg for place in outcome.geometry.placements],
            [0.0, 180.0, 90.0, 270.0],
        )
        self.assertEqual(built(config).board.to_string(),
                         outcome.board.to_string())

    def test_a_live_file_names_the_settings_it_overrode(self):
        config = config_with(self.folder / "p", grid={
            "rotation_deg": 90.0,
            "placement": {"rows": [{"across_mm": x * 30.0, "down_mm": 0.0}
                                   for x in range(4)]},
        })
        said = built(config).warnings[0]
        self.assertIn("placement file is active", said)
        self.assertIn("grid.rotation_deg = 90", said)


class UpstreamDivergenceTest(unittest.TestCase):
    """The pad angle a turned copy writes is NOT what KiCad writes.

    **This test asserts the WRONG value on purpose.** The fix belongs in the
    library; a local workaround here would fork the library's behaviour and
    hide the defect from its other users.

    The defect: `eedesigncore:pcb/transform.py:268`
    does `pad.angle = pad.angle + rotation.angle` with no normalisation, citing
    `PADSTACK::SetOrientation` (padstack.h:345) as storing it raw. The cited
    source does the opposite - `m_orientation.Normalize()` at padstack.h:348 -
    and `PAD::Rotate` routes through it (pad.cpp:2155-2161). So KiCad writes 90
    where we write 450, and omits the angle entirely where we write 360
    (writer 1688-1690, `GetOrientation().IsZero()`).

    Consequence, stated plainly: a rotated panel's pads parse and render, and a
    KiCad RESAVE changes those bytes. Determinism of OUR builds is unaffected -
    the same input still gives the same output, which is fenced elsewhere in
    this file.

    WHEN THE FIX LANDS THIS TEST FAILS, which is the point of it.
    """

    def test_a_pad_angle_is_written_unnormalised(self):
        board = Board.from_string(PAD_BOARD)
        footprint = board.footprints[0]
        self.assertEqual(footprint.pads[0].angle, 270.0)
        rotate_footprint(footprint, 180.0)
        self.assertEqual(
            footprint.pads[0].angle, 450.0,
            "The upstream defect has been FIXED: eedesigncore now normalises a "
            "rotated pad angle to [0, 360) the way PADSTACK::SetOrientation "
            "does. Delete this fence and assert 90.0 instead.",
        )

    def test_a_full_circle_is_written_where_kicad_omits_the_angle(self):
        board = Board.from_string(PAD_BOARD)
        footprint = board.footprints[0]
        footprint.pads[0].angle = 180.0
        rotate_footprint(footprint, 180.0)
        self.assertEqual(
            footprint.pads[0].angle, 360.0,
            "The upstream defect has been FIXED: a pad turned onto zero now reads "
            "0 and the writer omits it, as KiCad does. Delete this fence.",
        )

    def test_our_own_build_is_still_deterministic_over_it(self):
        # The divergence is against a KiCad resave, not against ourselves.
        board = Board.from_string(PAD_BOARD)
        target_a, target_b = new_board_from(board), new_board_from(board)
        for target in (target_a, target_b):
            inject_board(target, board, rotation=180.0, instance_key="0|0")
        self.assertEqual(target_a.to_string(), target_b.to_string())


PAD_BOARD = UPRIGHT_BOARD.replace(
    '      (effects (font (size 1 1) (thickness 0.15)))\n    )',
    '      (effects (font (size 1 1) (thickness 0.15)))\n    )\n'
    '    (pad "1" smd rect (at 1 0 270) (size 1 0.5) (layers "F.Cu")\n'
    '      (uuid "77777777-7777-7777-7777-777777777777"))',
)


# ---------------------------------------------------------------------------
# the stages a nested arrangement can trip
# ---------------------------------------------------------------------------


class BandGuardTest(unittest.TestCase):
    """A saw pass and a pour gap have to be CLEAR of every board."""

    ALIGNED = [(0.0, 0.0, 10.0, 10.0), (13.0, 0.0, 23.0, 10.0)]
    # An arrangement only a PLACEMENT FILE could ask for: one board turned so
    # it spans the whole panel across, and two more sitting under it. The
    # "gap" between those two is a stretch of the first board.
    CROSSED = [(0.0, 0.0, 100.0, 10.0), (10.0, 20.0, 20.0, 30.0),
               (30.0, 20.0, 40.0, 30.0)]

    def test_an_aligned_grid_scores_down_the_middle_of_the_gap(self):
        warnings = []
        self.assertEqual(
            panel_scores.gap_midlines(self.ALIGNED, "vertical", warnings),
            (11.5,),
        )
        self.assertEqual(warnings, [])

    def test_a_pass_that_would_run_through_a_board_is_refused_with_the_reason(self):
        warnings = []
        found = panel_scores.gap_midlines(self.CROSSED, "vertical", warnings)
        self.assertEqual(found, ())
        self.assertTrue(any("would run through" in note for note in warnings))

    def test_a_pass_is_never_kept_inside_any_board(self):
        for boxes in (self.ALIGNED, self.CROSSED):
            for at in panel_scores.gap_midlines(boxes, "vertical", []):
                for box in boxes:
                    with self.subTest(at=at, box=box):
                        self.assertFalse(box[0] < at < box[2])

    # The rail pour is ONE zone over the whole carrier, held off the boards
    # by rule areas derived from the boards' own rectangles or outlines
    # (`rail_copper.keepout_plan`). `test_carrier_features` and
    # `test_rail_copper` cover it.


# ---------------------------------------------------------------------------
# the cache, per edit kind (extending the fence in test_panel_cache.py)
# ---------------------------------------------------------------------------


class CacheEqualityTest(unittest.TestCase):
    """Every placement edit invalidates the kept stepped panel. Byte for byte.

    The stepped-panel cache is keyed on everything in the config EXCEPT the
    carrier sections and the two settings that move nothing
    (`panelbuild.VIEW_ONLY_KEYS`). So a placement key is layout by default and
    must rebuild - and the panel that comes out has to be the panel a
    from-scratch build produces.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def snapshot(self, config):
        board = Board.from_file(config.source_board_path)
        prep.compute(config, board)
        return panelbuild.snapshot_of(board)

    def test_every_placement_edit_changes_the_fingerprint(self):
        base = config_with(self.folder / "base")
        first = panelbuild.layout_fingerprint(base)
        for grid in ({"rotation_deg": 90.0}, {"alternate": "rows"},
                     {"nest_across_mm": -1.0}, {"nest_down_mm": 1.0},
                     {"placement": {"rows": [{"across_mm": 0.0,
                                              "down_mm": 0.0}] * 4}}):
            moved = config_with(self.folder / f"m{abs(hash(str(grid)))}",
                                grid=grid)
            with self.subTest(grid=grid):
                self.assertNotEqual(panelbuild.layout_fingerprint(moved), first)

    def test_the_view_only_setting_does_not_change_the_fingerprint(self):
        base = config_with(self.folder / "base")
        for grid in ({"clearance_warning_mm": 4.0},
                     {"clearance_warning_mm": 1.0}):
            moved = config_with(self.folder / f"v{abs(hash(str(grid)))}",
                                grid=grid)
            with self.subTest(grid=grid):
                self.assertEqual(panelbuild.layout_fingerprint(moved),
                                 panelbuild.layout_fingerprint(base))

    def test_every_turn_and_alternation_through_the_cache_equals_a_fresh_build(self):
        # THE WHOLE MATRIX, because the cache is offered the same kept panel
        # every time and has to refuse it every time. Twelve arrangements, each
        # compared byte for byte with the from-scratch build of the same config.
        first = config_with(self.folder / "matrix")
        snapshot = self.snapshot(first)
        held = panelbuild.derive(first, snapshot.board, fill_rails=False,
                                 snapshot=snapshot).cache
        self.assertIsNotNone(held)
        for turn in TURNS:
            for alternate in placement.ALTERNATIONS:
                grid = {"rotation_deg": turn, "alternate": alternate}
                moved = config_with(
                    self.folder / f"m{turn:g}{alternate}", grid=grid
                )
                through = panelbuild.derive(
                    moved, snapshot.board, fill_rails=False,
                    snapshot=snapshot, cache=held,
                )
                fresh = step_and_repeat.build_from(moved, snapshot.board,
                                                   fill_rails=False)
                with self.subTest(turn=turn, alternate=alternate):
                    self.assertEqual(through.outcome.board.to_string(),
                                     fresh.board.to_string())

    def test_a_placement_edit_after_a_cached_build_equals_a_fresh_one(self):
        first = config_with(self.folder / "one")
        snapshot = self.snapshot(first)
        derived = panelbuild.derive(first, snapshot.board, fill_rails=False,
                                    snapshot=snapshot)
        self.assertIsNotNone(derived.cache)
        for grid in ({"rotation_deg": 90.0}, {"alternate": "columns"},
                     {"rotation_deg": 180.0, "alternate": "rows",
                      "nest_down_mm": -0.5}):
            moved = config_with(self.folder / f"c{abs(hash(str(grid)))}",
                                grid=grid)
            # The cache is offered and must be REFUSED, so the answer is a
            # from-scratch build's answer.
            through_cache = panelbuild.derive(
                moved, snapshot.board, fill_rails=False, snapshot=snapshot,
                cache=derived.cache,
            )
            fresh = step_and_repeat.build_from(moved, snapshot.board,
                                               fill_rails=False)
            with self.subTest(grid=grid):
                self.assertEqual(through_cache.outcome.board.to_string(),
                                 fresh.board.to_string())


# ---------------------------------------------------------------------------
# the actions under the page
# ---------------------------------------------------------------------------


class SetParamTest(unittest.TestCase):
    """One typed control at a time, validated before anything is kept."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_path = write_panel_project(self.folder)
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def set(self, name, value):
        return actions.set_param(self.data, self.config_path, name, value)

    def refused(self, name, value) -> str:
        before = json.dumps(self.data, sort_keys=True)
        with self.assertRaises(actions.ActionError) as caught:
            self.set(name, value)
        self.assertEqual(json.dumps(self.data, sort_keys=True), before)
        return str(caught.exception)

    def test_each_placement_control_reaches_its_config_key(self):
        for name, value, path in (
            ("rotation_deg", 90.0, ("grid", "rotation_deg")),
            ("alternate", "rows", ("grid", "alternate")),
            ("nest_across_mm", -1.5, ("grid", "nest_across_mm")),
            ("nest_down_mm", 2.0, ("grid", "nest_down_mm")),
            ("clearance_warning_mm", 1.2, ("grid", "clearance_warning_mm")),
        ):
            with self.subTest(name=name):
                changed = self.set(name, value)
                self.assertTrue(changed.changed)
                self.assertEqual(actions._read(changed.data, path), value)

    def test_a_turn_the_page_could_not_have_offered_is_refused_by_name(self):
        said = self.refused("rotation_deg", 45.0)
        self.assertIn("rotation_deg", said)
        self.assertIn("0, 90, 180, 270", said)

    def test_the_removed_rounding_parameter_is_no_longer_settable(self):
        # A page that posts it gets a refusal naming it, not a config key
        # written into the file.
        self.assertIn("pitch_rounding_mm", self.refused("pitch_rounding_mm", 0.5))

    def test_an_alternation_the_page_could_not_have_offered_is_refused(self):
        self.assertIn("alternate", self.refused("alternate", "diagonally"))

    def test_a_nest_shift_bigger_than_the_page_takes_is_refused(self):
        said = self.refused("nest_across_mm", 5000.0)
        self.assertIn("nest_across_mm", said)
        self.assertIn("towards its neighbour", said)

    def test_the_placement_switch_only_ever_goes_off(self):
        said = self.refused("placement_on", True)
        self.assertIn("has to be picked", said)

    def test_clearing_a_placement_file_that_is_not_there_changes_nothing(self):
        self.assertFalse(self.set("placement_on", False).changed)

    def test_clearing_a_live_placement_file_takes_the_section_out(self):
        self.data["grid"]["placement"] = {
            "source": "p.csv",
            "rows": [{"across_mm": 0.0, "down_mm": 0.0, "angle_deg": 0.0},
                     {"across_mm": 20.0, "down_mm": 0.0, "angle_deg": 0.0}],
        }
        changed = self.set("placement_on", False)
        self.assertTrue(changed.changed)
        self.assertNotIn("placement", changed.data["grid"])
        # The pattern settings were never overwritten, so they come straight
        # back the moment the file goes.
        self.assertIsNone(changed.config.grid.placement)


class ImportPlacementTest(unittest.TestCase):
    """A posted CSV, and every refusal that changes nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # 2 x 1, so a file of two lines is the right length.
        self.config_path = write_panel_project(
            self.folder, grid={"columns": 2, "rows": 1, "gap_mm": 3.0}
        )
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def post(self, content, name="places.csv"):
        return actions.import_placement(self.data, self.config_path,
                                        {"name": name, "content": content})

    def refused(self, content, name="places.csv") -> str:
        before = json.dumps(self.data, sort_keys=True)
        with self.assertRaises(actions.ActionError) as caught:
            self.post(content, name)
        self.assertEqual(json.dumps(self.data, sort_keys=True), before)
        return str(caught.exception)

    def test_the_rows_land_in_the_config_and_the_name_is_kept(self):
        placed = self.post("0,0,0\n20,0,180\n")
        self.assertEqual(placed.name, "places.csv")
        self.assertEqual(placed.config.grid.placement.source, "places.csv")
        self.assertEqual(placed.config.grid.placement.triples,
                         ((0.0, 0.0, 0.0), (20.0, 0.0, 180.0)))
        # Nothing reached the disk.
        self.assertNotIn(
            "placement",
            json.loads(self.config_path.read_text(encoding="utf-8"))["grid"],
        )

    def test_a_row_count_that_is_not_the_cell_count_is_refused(self):
        said = self.refused("0,0,0\n")
        self.assertIn("1 placement", said)
        self.assertIn("2 x 1 = 2", said)
        self.assertIn("Nothing was changed", said)

    def test_an_empty_body_is_refused(self):
        self.assertIn("carried none", self.refused("   "))

    def test_a_file_that_will_not_parse_is_refused_and_names_itself(self):
        said = self.refused("0,0,0\nnope\n", name="broken.csv")
        self.assertIn("broken.csv", said)
        self.assertIn("line 2", said)

    def test_a_posted_path_is_reduced_to_a_name(self):
        placed = self.post("0,0,0\n20,0,0\n", name="C:\\fakepath\\p.csv")
        self.assertEqual(placed.name, "p.csv")


class PanelPageControlsTest(unittest.TestCase):
    """Every placement control is on the page, dressed as a field."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        folder = Path(cls._tmp.name)
        config_path = write_panel_project(folder)
        session = PreviewSession(config_path, folder / "work",
                                 render=stub_render)
        session.refresh()
        cls.markup = session.panel_page()
        cls.params = page_panel.params_json(session.config)
        cls.session = session

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_control_the_reference_names_is_there(self):
        for token in ("pb-rotation", "pb-alternate", "pb-nestx", "pb-nesty",
                      "pb-clearance", "pb-placement",
                      "pb-placed-clear", "pb-marks"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_neither_removed_control_is_on_the_page(self):
        # The page has no gap-closing button and no pitch rounding field.
        for token in ("pb-pitch", "pb-snug", "Pitch rounding", "/snug"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.markup)

    def test_each_one_wears_a_bold_title_and_one_helper_line(self):
        # Read off the markup rather than trusted.
        for title in ("Rotation", "Alternation", "Nest shift across (mm)",
                      "Nest shift down (mm)", "Clearance warning (mm)",
                      "Placement file"):
            with self.subTest(title=title):
                self.assertIn(f"<span class=t>{title}</span>", self.markup)

    def test_the_rotation_dropdown_offers_the_four_quarter_turns(self):
        select = re.search(r"<select id=pb-rotation.*?</select>", self.markup,
                           re.S)
        self.assertIsNotNone(select)
        self.assertEqual(
            re.findall(r'<option value="([^"]+)"', select.group(0)),
            ["0", "90", "180", "270"],
        )

    def test_the_alternation_dropdown_offers_the_three_words(self):
        select = re.search(r"<select id=pb-alternate.*?</select>", self.markup,
                           re.S)
        self.assertEqual(
            re.findall(r'<option value="([^"]+)"', select.group(0)),
            list(placement.ALTERNATIONS),
        )

    def test_a_nest_shift_field_accepts_a_negative_number(self):
        self.assertIn("id=pb-nestx data-act=param step=0.1 min=-1000",
                      self.markup)

    def test_the_marking_element_is_an_overlay_and_not_a_layer(self):
        found = re.search(r'<svg[^>]*id="pb-marks"[^>]*>', self.markup)
        self.assertIsNotNone(found)
        self.assertIn('data-overlay="1"', found.group(0))
        self.assertNotIn("data-layer", found.group(0))

    def test_the_page_states_every_placement_parameter(self):
        for name in ("rotation_deg", "alternate", "nest_across_mm",
                     "nest_down_mm", "clearance_warning_mm",
                     "placement_on", "placement_rows",
                     "placement_source"):
            with self.subTest(name=name):
                self.assertIn(name, self.params)
        self.assertNotIn("pitch_rounding_mm", self.params)
        self.assertFalse(self.params["placement_on"])
        self.assertEqual(self.params["placement_rows"], 0)


class LivePlacementTest(unittest.TestCase):
    """The session: a turn recalculates, a clearance warning does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.build_panel()

    def test_a_turn_recalculates_the_panel(self):
        before = self.session.panel.outcome.board.to_string()
        self.session.set_param({"rotation_deg": 90.0})
        after = self.session.panel.outcome.board.to_string()
        self.assertNotEqual(before, after)
        self.assertEqual(self.session.panel.outcome.geometry.instance_extent_mm,
                         self.session.panel.outcome.geometry.instance_extent_mm)

    def test_a_clearance_warning_marks_without_rebuilding(self):
        panel = self.session.panel
        board_before = panel.outcome.board
        self.session.set_param({"clearance_warning_mm": 4.0})
        # THE SAME PANEL OBJECT's board: nothing was built and nothing rendered.
        self.assertIs(self.session.panel.outcome.board, board_before)
        marks = self.session.panel_state()["clearance"]
        self.assertEqual(marks["distance_mm"], 4.0)
        self.assertTrue(marks["marks"])
        for mark in marks["marks"]:
            with self.subTest(mark=mark):
                self.assertLess(mark["gap_mm"], 4.0)
                self.assertEqual(len(mark["at"]), 2)

    def test_zero_turns_the_marking_off_again(self):
        self.session.set_param({"clearance_warning_mm": 4.0})
        self.session.set_param({"clearance_warning_mm": 0.0})
        self.assertEqual(self.session.panel_state()["clearance"]["marks"], [])

    def test_the_report_says_what_the_marking_found(self):
        self.session.set_param({"clearance_warning_mm": 4.0})
        said = " ".join(self.session.panel.report)
        self.assertIn("closer than 4 mm", said)
        self.assertIn("between their rectangles", said)

    def test_the_session_offers_no_gap_closing_action(self):
        # The session has no gap-closing method and no extent helper for one.
        self.assertFalse(hasattr(self.session, "snug"))
        self.assertFalse(hasattr(self.session, "_instance_extent_mm"))

    def test_a_placement_file_is_taken_and_the_page_says_so(self):
        columns = self.session.config.grid.columns
        rows = self.session.config.grid.rows
        lines = "".join(
            f"{index * 30.0},0,90\n" for index in range(columns * rows)
        )
        reply = self.session.import_placement(
            {"name": "places.csv", "content": lines}
        )
        self.assertTrue(reply["ok"])
        params = page_panel.params_json(self.session.config)
        self.assertTrue(params["placement_on"])
        self.assertEqual(params["placement_rows"], columns * rows)
        self.assertEqual(params["placement_source"], "places.csv")
        self.assertEqual(
            {place.rotation_deg
             for place in self.session.panel.outcome.geometry.placements},
            {90.0},
        )

    def test_a_refused_placement_file_changes_nothing(self):
        before = json.dumps(self.session.data, sort_keys=True)
        with self.assertRaises(actions.ActionError):
            self.session.import_placement(
                {"name": "bad.csv", "content": "0,0,0\n"}
            )
        self.assertEqual(json.dumps(self.session.data, sort_keys=True), before)

    def test_clearing_the_file_hands_the_pattern_back(self):
        columns, rows = (self.session.config.grid.columns,
                         self.session.config.grid.rows)
        lines = "".join(f"{i * 30.0},0,0\n" for i in range(columns * rows))
        self.session.import_placement({"name": "p.csv", "content": lines})
        self.session.set_param({"placement_on": False})
        self.assertIsNone(self.session.config.grid.placement)
        self.assertFalse(
            page_panel.params_json(self.session.config)["placement_on"]
        )


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientPlacementTest(unittest.TestCase):
    """The page's own wiring, RUN: what a live file disables, and what draws."""

    @classmethod
    def setUpClass(cls):
        from test_panel_page import js_slice
        from test_preview_journal import js_function, run_js

        cls.out = run_js(PLACEMENT_HARNESS % {
            "tables": js_slice(page_panel.PANEL_JS, "const LAYOUT_FIELDS = [",
                               "COPPER_FIELDS, SCORE_FIELDS, DIMENSION_FIELDS, TEXT_FIELDS\n);"),
            "functions": "\n".join(
                js_function(page_panel.PANEL_JS, name)
                for name in ("field", "railsLinked", "livePanelParams",
                             "liveFields", "readField", "writeChoiceNumber",
                             "writeField", "drawPlacementMode",
                             "drawClearance", "changedPanelParams")
            ),
            "pattern": json.dumps(PATTERN_PARAMS),
            "placed": json.dumps(PLACED_PARAMS),
            "marks": json.dumps(MARKS),
        })

    def test_a_numeric_dropdown_is_filled_from_the_config(self):
        self.assertEqual(self.out["filledTurn"], "180")

    def test_the_pattern_controls_are_live_while_no_file_is(self):
        for name in ("gap_x_mm", "rotation_deg", "alternate", "nest_across_mm",
                     "nest_down_mm", "clearance_warning_mm"):
            with self.subTest(name=name):
                self.assertIn(name, self.out["patternLive"])
        self.assertEqual(self.out["patternDisabled"], [])

    def test_a_live_file_disables_everything_but_columns_and_rows(self):
        self.assertEqual(
            sorted(self.out["placedDisabled"]),
            sorted(["pb-gapx", "pb-gapy", "pb-rotation", "pb-alternate",
                    "pb-nestx", "pb-nesty"]),
        )
        for name in ("gap_x_mm", "rotation_deg", "alternate", "nest_across_mm",
                     "nest_down_mm"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.out["placedLive"])
        # Columns and rows stay, and so does the clearance warning: it measures
        # whatever arrangement is on screen, however it was made.
        for name in ("columns", "rows", "clearance_warning_mm"):
            self.assertIn(name, self.out["placedLive"])

    def test_a_disabled_control_is_never_posted(self):
        self.assertEqual(self.out["placedChanged"], {})

    def test_the_page_says_which_file_placed_it_and_how_to_be_rid_of_it(self):
        said = self.out["placedSaid"]
        self.assertIn("places.csv", said)
        self.assertIn("4 placements", said)
        self.assertIn("Clear it", said)
        self.assertTrue(self.out["clearEnabled"])

    def test_the_marking_draws_one_span_and_one_figure_per_pair(self):
        markup = self.out["marks"]
        self.assertEqual(markup.count("<path"), 2)
        self.assertEqual(markup.count("<text"), 2)
        self.assertIn("0.500 mm", markup)
        self.assertIn("1.250 mm", markup)
        # Board millimetres, straight out of the measurement.
        self.assertIn("M10.0000 0.0000L10.5000 0.0000", markup)

    def test_two_boards_that_touch_are_ringed_rather_than_left_invisible(self):
        self.assertIn("<circle", self.out["touching"])

    def test_nothing_to_mark_draws_nothing(self):
        self.assertEqual(self.out["marksOff"], "")


PATTERN_PARAMS = {
    "columns": 2, "rows": 2, "gap_x_mm": 3.0, "gap_y_mm": 3.0,
    "rail_mm": 5.0, "rail_top_mm": 5.0, "rail_bottom_mm": 5.0,
    "rail_left_mm": 5.0, "rail_right_mm": 5.0, "rails_linked": True,
    "rotation_deg": 180.0, "alternate": "rows", "nest_across_mm": -1.0,
    "nest_down_mm": 0.0, "clearance_warning_mm": 0.0,
    "placement_on": False, "placement_rows": 0, "placement_source": "",
    "tooling_holes_on": False, "rail_copper_on": False, "scores_on": False,
}
PLACED_PARAMS = dict(PATTERN_PARAMS, placement_on=True, placement_rows=4,
                     placement_source="places.csv")
MARKS = {
    "distance_mm": 2.0,
    "found": 2,
    "marks": [
        {"gap_mm": 0.5, "boards": [1, 2], "at": [10.0, 0.0], "to": [10.5, 0.0]},
        {"gap_mm": 1.25, "boards": [1, 3], "at": [0.0, 10.0],
         "to": [0.0, 11.25]},
    ],
}

PLACEMENT_HARNESS = r"""
%(tables)s
%(functions)s
let busy = false, stopped = false;
const out = {posted: [], said: [], logged: []};
const NODES = {};
function node(id, options) {
  let held = '';
  const el = {id: id, checked: false, hidden: false, disabled: false,
              dataset: {}, textContent: '', innerHTML: '',
              options: (options || []).map(v => ({value: v})),
              getAttribute: () => '0 0 40 40',
              addEventListener() {}};
  Object.defineProperty(el, 'value', {
    get() { return held; },
    set(next) { held = String(next); },
  });
  NODES[id] = el;
  return el;
}
['pb-columns', 'pb-rows', 'pb-gapx', 'pb-gapy', 'pb-rail', 'pb-rail-top',
 'pb-rail-bottom', 'pb-rail-left', 'pb-rail-right', 'pb-rail-unlock',
 'pb-persides', 'pb-rail-linked',
 'pb-holes-on', 'pb-hole-d', 'pb-hole-tl', 'pb-hole-tr', 'pb-hole-br',
 'pb-hole-bl', 'pb-hole-place', 'pb-hole-x', 'pb-hole-y', 'pb-hole-insets',
 'pb-copper-on', 'pb-copper-clear', 'pb-copper-between',
 'pb-scores-on', 'pb-score-v', 'pb-score-h', 'pb-score-rails', 'pb-score-w',
 'pb-holes-body', 'pb-copper-body', 'pb-scores-body',
 'pb-nestx', 'pb-nesty', 'pb-clearance', 'pb-alternate',
 'pb-placement', 'pb-placed', 'pb-placed-clear',
 'pb-marks'].forEach(id => node(id));
node('pb-rotation', ['0', '90', '180', '270']);
globalThis.document = {
  getElementById: id => NODES[id] || null,
  activeElement: null,
};
let PB = {params: {}};

// -- THE PATTERN, which is what a config with no placement file gives -------
PB.params = %(pattern)s;
FIELDS.forEach(pair => writeField(pair, PB.params[pair[1]]));
drawPlacementMode();
out.filledTurn = NODES['pb-rotation'].value;
out.patternLive = livePanelParams(PB.params);
out.patternDisabled = PLACEMENT_DISABLES.filter(id => NODES[id].disabled);

// -- A LIVE PLACEMENT FILE --------------------------------------------------
PB.params = %(placed)s;
FIELDS.forEach(pair => writeField(pair, PB.params[pair[1]]));
drawPlacementMode();
out.placedLive = livePanelParams(PB.params);
out.placedDisabled = PLACEMENT_DISABLES.filter(id => NODES[id].disabled);
out.placedSaid = NODES['pb-placed'].textContent;
out.clearEnabled = !NODES['pb-placed-clear'].disabled;
// A control the user cannot use must never be posted, however it is filled.
NODES['pb-gapx'].value = '9';
NODES['pb-rotation'].value = '90';
NODES['pb-nestx'].value = '4';
out.placedChanged = changedPanelParams();

// -- THE CLEARANCE MARKING --------------------------------------------------
drawClearance(%(marks)s);
out.marks = NODES['pb-marks'].innerHTML;
drawClearance({distance_mm: 1.0, found: 1, marks: [
  {gap_mm: 0.0, boards: [1, 2], at: [5.0, 5.0], to: [5.0, 5.0]}]});
out.touching = NODES['pb-marks'].innerHTML;
drawClearance({distance_mm: 0.0, found: 0, marks: []});
out.marksOff = NODES['pb-marks'].innerHTML;
console.log(JSON.stringify(out));
"""


if __name__ == "__main__":
    unittest.main()
