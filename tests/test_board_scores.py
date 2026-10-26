"""A score decision on the SOURCE board writes the keepout too, and it never doubles.

When v-score lines are added to the source board, a rule area is added with
them and the copper is re-filled, so the source board's zones are filled and
can be reviewed before they are frozen into polygons and moved to the panel.

What is pinned here is the file-level half of that - the keepout that rides with
a decision, the one number both stages take their width from, the geometry
read-back the panel recognises an inherited keepout by, and the arithmetic that
stops the panel guarding one line twice. The KiCad half (the re-fill itself) is
pinned in `test_board_refill.py`, which needs a KiCad on the machine; nothing
here does.
"""

import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    PcbLine,
    Stroke,
    Zone,
    ZoneKeepout,
    ZoneOutlinePolygon,
    dump_board,
)

from pydantic import ValidationError

from eedesignpanels.stages import board_scores, panel_scores
from eedesignpanels.stages.panel_scores import (
    RULE_AREA_NAME,
    SCORE_LAYER,
    keepout_span,
    rule_area_for,
    uncovered_runs,
)

# A board 20 mm across and 30 mm down, scored down its left edge at x = 4.
SCORE_START = (4.0, 0.0)
SCORE_END = (4.0, 30.0)


def board_with(*items) -> Board:
    """A board with three copper layers, the score layer, and *items* on it."""
    board = Board(layers=[
        BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
        BoardLayer(ordinal=1, canonical_name="In1.Cu", layer_type="signal"),
        BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
        BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
    ])
    board.drawings.extend(items)
    return board


def score_line(start=SCORE_START, end=SCORE_END, uid="score-1") -> PcbLine:
    return PcbLine(start=start, end=end, layer=SCORE_LAYER,
                   stroke=Stroke(width=0.1), id=uid)


class BoardKeepoutTests(unittest.TestCase):
    """One score line, one keepout, and the sync that keeps them in step."""

    def test_a_scored_board_gets_one_keepout_on_every_copper_layer(self):
        board = board_with(score_line())
        made = board_scores.sync(board, 0.3)
        self.assertEqual((made.added, made.removed, made.total), (1, 0, 1))
        self.assertEqual(len(board.zones), 1)
        zone = board.zones[0]
        self.assertEqual(zone.layers, ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertEqual(zone.name, RULE_AREA_NAME)

    def test_the_keepout_keeps_everything_out(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        keepout = board.zones[0].keepout
        for field in ("tracks", "vias", "pads", "copper_pour", "footprints"):
            self.assertFalse(getattr(keepout, field), field)

    def test_the_keepout_is_the_strip_either_side_of_the_line(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        start, end, half = keepout_span(board.zones[0])
        self.assertAlmostEqual(half, 0.3)
        self.assertAlmostEqual(start[0], 4.0)
        self.assertAlmostEqual(end[0], 4.0)
        self.assertAlmostEqual(min(start[1], end[1]), 0.0)
        self.assertAlmostEqual(max(start[1], end[1]), 30.0)

    def test_the_width_is_what_the_caller_asked_for(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.5)
        _start, _end, half = keepout_span(board.zones[0])
        self.assertAlmostEqual(half, 0.5)

    def test_syncing_twice_changes_nothing(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        before = dump_board(board)
        again = board_scores.sync(board, 0.3)
        self.assertEqual((again.added, again.removed), (0, 0))
        self.assertEqual(dump_board(board), before)

    def test_a_board_with_no_score_line_gets_no_keepout(self):
        board = board_with()
        before = dump_board(board)
        made = board_scores.sync(board, 0.3)
        self.assertEqual((made.added, made.removed, made.total), (0, 0, 0))
        self.assertEqual(dump_board(board), before)

    def test_removing_the_line_removes_the_keepout(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        board.drawings = []
        made = board_scores.sync(board, 0.3)
        self.assertEqual((made.added, made.removed, made.total), (0, 1, 0))
        self.assertEqual(board.zones, [])

    def test_a_width_that_changes_replaces_the_keepout_rather_than_adding_one(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        made = board_scores.sync(board, 0.5)
        self.assertEqual((made.added, made.removed, made.total), (1, 1, 1))
        self.assertEqual(len(board.zones), 1)

    def test_a_keepout_this_stage_did_not_make_is_never_touched(self):
        mine = Zone(
            layers=["F.Cu"], id="someone-elses", name="do not route here",
            keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                                copper_pour=False, footprints=False),
            polygons=[ZoneOutlinePolygon(pts=[
                (0.0, 0.0), (1.0, 0.0), (1.0, 5.0), (0.0, 5.0),
            ])],
        )
        board = board_with()
        board.zones.append(mine)
        made = board_scores.sync(board, 0.3)
        self.assertEqual((made.added, made.removed), (0, 0))
        self.assertEqual(board.zones, [mine])

    def test_two_score_lines_get_two_keepouts(self):
        board = board_with(
            score_line(),
            score_line((16.0, 0.0), (16.0, 30.0), uid="score-2"),
        )
        made = board_scores.sync(board, 0.3)
        self.assertEqual((made.added, made.total), (2, 2))
        self.assertEqual(
            sorted(keepout_span(zone)[0][0] for zone in board.zones),
            [4.0, 16.0],
        )


class KeepoutIdentityTests(unittest.TestCase):
    """The uuid is derived from the score line's, so it never moves."""

    def test_the_uuid_is_derived_from_the_score_line(self):
        board = board_with(score_line())
        board_scores.sync(board, 0.3)
        first = board.zones[0].id
        again = board_with(score_line())
        board_scores.sync(again, 0.3)
        self.assertEqual(again.zones[0].id, first)

    def test_a_different_line_gets_a_different_uuid(self):
        board = board_with(score_line(uid="score-1"))
        other = board_with(score_line(uid="score-2"))
        board_scores.sync(board, 0.3)
        board_scores.sync(other, 0.3)
        self.assertNotEqual(board.zones[0].id, other.zones[0].id)

    def test_a_different_width_gets_a_different_uuid(self):
        board = board_with(score_line())
        other = board_with(score_line())
        board_scores.sync(board, 0.3)
        board_scores.sync(other, 0.5)
        self.assertNotEqual(board.zones[0].id, other.zones[0].id)

    def test_a_line_with_no_uuid_is_keyed_on_where_it_is(self):
        line = score_line(uid=None)
        key = board_scores.rule_area_key(line, 0.3)
        self.assertIn("4.000000", key)
        self.assertIn("30.000000", key)


class HalfWidthSourceTests(unittest.TestCase):
    """ONE number for the board and the panel - see the module docstring."""

    def config_with(self, **extra):
        from eedesignpanels.config import PanelConfig

        return PanelConfig(
            source_board="board.kicad_pcb", config_path="panel.json",
            grid={"columns": 2, "rows": 3, "gap_mm": 3.0}, **extra
        )

    def test_a_config_with_no_scores_section_uses_the_model_default(self):
        self.assertEqual(
            board_scores.half_width_of(self.config_with()),
            panel_scores.DEFAULT_RULE_HALF_WIDTH_MM,
        )

    def test_a_config_with_no_panel_section_uses_the_model_default(self):
        config = self.config_with()
        self.assertIsNone(config.panel)
        self.assertEqual(board_scores.half_width_of(config), 0.3)

    def test_the_retired_panel_key_is_refused_with_no_prep_section_either(self):
        # `panel.scores.rule_half_width_mm` is not a key of this section, so
        # it is refused exactly like a typo, whether or not `prep` is named.
        with self.assertRaises(ValidationError) as raised:
            self.config_with(
                stage="build_panel",
                panel={"rails": 5.0, "scores": {"rule_half_width_mm": 0.45}},
            )
        self.assertIn("rule_half_width_mm", str(raised.exception))

    def test_the_board_default_and_the_panel_default_are_one_number(self):
        # The setting lives in prep, so the two spellings of the shipped
        # number are the config's constant and the stage's - fenced against
        # each other, because `protect` runs for a config that names no prep
        # at all.
        from eedesignpanels.config import (
            SCORE_RULE_HALF_WIDTH_DEFAULT_MM,
            VScoreConfig,
        )

        self.assertEqual(
            SCORE_RULE_HALF_WIDTH_DEFAULT_MM,
            panel_scores.DEFAULT_RULE_HALF_WIDTH_MM,
        )
        self.assertEqual(
            VScoreConfig().rule_half_width_mm,
            panel_scores.DEFAULT_RULE_HALF_WIDTH_MM,
        )

    def test_the_prep_section_is_where_the_width_is_declared(self):
        # The v-score keepout width is defined on the prep page. It is a board
        # setting, so it lives beside the cutter.
        config = self.config_with(
            prep={"cutter_diameter": 0.889,
                  "vscore": {"rule_half_width_mm": 0.52}},
        )
        self.assertEqual(board_scores.half_width_of(config), 0.52)
        self.assertEqual(config.score_rule_half_width_mm, 0.52)

    def test_a_retired_panel_width_is_refused_even_with_a_prep_section(self):
        with self.assertRaises(ValidationError) as raised:
            self.config_with(
                stage="build_panel",
                prep={"cutter_diameter": 0.889},
                panel={"rails": 5.0, "scores": {"rule_half_width_mm": 0.45}},
            )
        self.assertIn("rule_half_width_mm", str(raised.exception))

    def test_a_retired_width_beside_a_current_one_is_still_refused(self):
        with self.assertRaises(ValidationError):
            self.config_with(
                stage="build_panel",
                prep={"cutter_diameter": 0.889,
                     "vscore": {"rule_half_width_mm": 0.2}},
                panel={"rails": 5.0, "scores": {"rule_half_width_mm": 0.45}},
            )


class KeepoutReadBackTests(unittest.TestCase):
    """What makes an inherited keepout recognisable: its geometry, nothing else."""

    def test_a_rule_area_reads_back_as_the_line_it_was_drawn_on(self):
        zone = rule_area_for((1.0, 2.0), (1.0, 22.0), 0.3, ("F.Cu",))
        start, end, half = keepout_span(zone)
        self.assertAlmostEqual(half, 0.3)
        self.assertEqual(
            sorted([start, end]), [(1.0, 2.0), (1.0, 22.0)]
        )

    def test_a_diagonal_rule_area_reads_back_too(self):
        zone = rule_area_for((0.0, 0.0), (3.0, 4.0), 0.5, ("F.Cu",))
        start, end, half = keepout_span(zone)
        self.assertAlmostEqual(half, 0.5)
        self.assertAlmostEqual(start[0], 0.0)
        self.assertAlmostEqual(end[1], 4.0)

    def test_a_zone_that_allows_something_through_is_not_a_score_guard(self):
        zone = rule_area_for((1.0, 2.0), (1.0, 22.0), 0.3, ("F.Cu",))
        zone.keepout.copper_pour = True
        self.assertIsNone(keepout_span(zone))

    def test_a_zone_with_no_keepout_is_not_one(self):
        zone = Zone(layers=["F.Cu"], id="pour",
                    polygons=[ZoneOutlinePolygon(pts=[
                        (0.0, 0.0), (1.0, 0.0), (1.0, 5.0), (0.0, 5.0)])])
        self.assertIsNone(keepout_span(zone))

    def test_a_square_keepout_says_nothing_about_a_direction(self):
        # `rule_area_for` itself can no longer produce a square outline: the
        # rectangle now overhangs the line by the half width at each end,
        # so its length always exceeds its
        # width for any real line. What is fenced here is the bail-out itself
        # - built directly, because nothing this stage draws is ambiguous
        # any more.
        zone = Zone(
            layers=["F.Cu"],
            keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                               copper_pour=False, footprints=False),
            polygons=[ZoneOutlinePolygon(
                pts=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
            )],
        )
        self.assertIsNone(keepout_span(zone))

    def test_a_five_sided_keepout_is_not_a_strip(self):
        zone = rule_area_for((1.0, 2.0), (1.0, 22.0), 0.3, ("F.Cu",))
        zone.polygons[0].pts.append((0.0, 0.0))
        self.assertIsNone(keepout_span(zone))


class CoverageTests(unittest.TestCase):
    """The arithmetic that stops one line being guarded twice."""

    def line_keepout(self, start, end, half=0.3):
        zone = rule_area_for(start, end, half, ("F.Cu",))
        return (zone, *keepout_span(zone))

    def test_a_line_nothing_guards_is_uncovered_end_to_end(self):
        runs = uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0], (0.0, 0.0))
        self.assertAlmostEqual(runs[0][1][1], 30.0)

    def test_a_line_guarded_end_to_end_is_uncovered_nowhere(self):
        guard = self.line_keepout((0.0, 0.0), (0.0, 30.0))
        self.assertEqual(uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard]), ())

    def test_a_guard_in_the_middle_leaves_the_two_ends(self):
        guard = self.line_keepout((0.0, 10.0), (0.0, 20.0))
        runs = uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard])
        self.assertEqual(len(runs), 2)
        self.assertAlmostEqual(runs[0][1][1], 10.0)
        self.assertAlmostEqual(runs[1][0][1], 20.0)
        self.assertAlmostEqual(runs[1][1][1], 30.0)

    def test_three_instances_leave_the_rails_and_the_gaps(self):
        # A column of three boards 10 mm tall, 5 mm apart, inside a 10 mm rail.
        guards = [
            self.line_keepout((0.0, 10.0), (0.0, 20.0)),
            self.line_keepout((0.0, 25.0), (0.0, 35.0)),
            self.line_keepout((0.0, 40.0), (0.0, 50.0)),
        ]
        runs = uncovered_runs((0.0, 0.0), (0.0, 60.0), 0.3, guards)
        self.assertEqual(
            [(round(a[1], 4), round(b[1], 4)) for a, b in runs],
            [(0.0, 10.0), (20.0, 25.0), (35.0, 40.0), (50.0, 60.0)],
        )

    def test_two_guards_that_meet_are_one_run(self):
        guards = [
            self.line_keepout((0.0, 0.0), (0.0, 15.0)),
            self.line_keepout((0.0, 15.0), (0.0, 30.0)),
        ]
        self.assertEqual(
            uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, guards), ()
        )

    def test_a_guard_at_another_width_is_not_the_same_guard(self):
        guard = self.line_keepout((0.0, 0.0), (0.0, 30.0), half=0.5)
        warnings = []
        runs = uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard], warnings)
        self.assertEqual(len(runs), 1)
        self.assertTrue(any("0.5" in note and "0.3" in note
                            for note in warnings), warnings)

    def test_a_guard_off_the_line_guards_nothing_on_it(self):
        guard = self.line_keepout((2.0, 0.0), (2.0, 30.0))
        runs = uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard])
        self.assertEqual(len(runs), 1)

    def test_a_guard_running_the_other_way_is_the_same_guard(self):
        guard = self.line_keepout((0.0, 30.0), (0.0, 0.0))
        self.assertEqual(
            uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard]), ()
        )

    def test_a_guard_longer_than_the_line_is_clamped_to_it(self):
        guard = self.line_keepout((0.0, -10.0), (0.0, 40.0))
        self.assertEqual(
            uncovered_runs((0.0, 0.0), (0.0, 30.0), 0.3, [guard]), ()
        )


class PanelNeverDoublesTests(unittest.TestCase):
    """`rule_area_every_score` inherits what the boards brought with them."""

    def panel_with_inherited(self):
        """A panel line 0 to 60 mm with three instance keepouts already on it."""
        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        for low, high in ((10.0, 20.0), (25.0, 35.0), (40.0, 50.0)):
            board.zones.append(
                rule_area_for((0.0, low), (0.0, high), 0.3,
                              ("F.Cu", "In1.Cu", "B.Cu"))
            )
        return board

    def test_the_panel_adds_only_what_the_boards_left_uncovered(self):
        board = self.panel_with_inherited()
        made = panel_scores.rule_area_every_score(board, 0.3)
        self.assertEqual(len(made), 4)
        self.assertEqual(len(board.zones), 7)

    def test_a_line_guarded_end_to_end_gets_nothing_and_says_so(self):
        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        board.zones.append(
            rule_area_for((0.0, 0.0), (0.0, 60.0), 0.3, ("F.Cu",))
        )
        warnings = []
        made = panel_scores.rule_area_every_score(board, 0.3, warnings)
        self.assertEqual(made, ())
        self.assertEqual(len(board.zones), 1)
        self.assertTrue(any("guarded" in note for note in warnings), warnings)

    def test_a_panel_whose_boards_brought_nothing_is_guarded_as_before(self):
        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        made = panel_scores.rule_area_every_score(board, 0.3)
        self.assertEqual(len(made), 1)
        self.assertAlmostEqual(made[0].start[1], 0.0)
        self.assertAlmostEqual(made[0].end[1], 60.0)

    def test_protect_inherits_the_same_way(self):
        board = self.panel_with_inherited()
        made = panel_scores.protect(board)
        self.assertEqual(len(made.rule_areas), 4)
        self.assertTrue(made.protection_only)
        # Partly guarded is not guarded: this line still needed the panel.
        self.assertEqual(made.inherited_guarded, 0)

    def test_a_fully_guarded_line_is_counted_and_not_drawn_over(self):
        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        board.zones.append(
            rule_area_for((0.0, 0.0), (0.0, 60.0), 0.3, ("F.Cu",))
        )
        made = panel_scores.protect(board)
        self.assertEqual(made.rule_areas, ())
        self.assertEqual(made.inherited_guarded, 1)
        self.assertEqual(made.inherited_lines, 1)

    def test_the_count_is_taken_before_the_panel_adds_its_own(self):
        # The arithmetic that makes the sentence true: counting afterwards
        # would find every line guarded, by the panel's own work.
        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        made = panel_scores.protect(board)
        self.assertEqual(len(made.rule_areas), 1)
        self.assertEqual(made.inherited_guarded, 0)

    def test_the_same_panel_guarded_twice_names_the_same_zones(self):
        first = self.panel_with_inherited()
        second = self.panel_with_inherited()
        panel_scores.rule_area_every_score(first, 0.3)
        panel_scores.rule_area_every_score(second, 0.3)
        self.assertEqual(dump_board(first), dump_board(second))

    def test_a_footprint_hosted_keepout_counts_as_coverage(self):
        from eedesigncore import Footprint

        board = board_with(score_line((0.0, 0.0), (0.0, 60.0), uid="merged"))
        footprint = Footprint(
            fp_id="Test:Thing", layer="F.Cu", uuid="fp-1",
            at={"position": (0.0, 0.0), "angle": 0.0},
        )
        footprint.zones.append(
            rule_area_for((0.0, 0.0), (0.0, 60.0), 0.3, ("F.Cu",))
        )
        board.footprints.append(footprint)
        made = panel_scores.rule_area_every_score(board, 0.3)
        self.assertEqual(made, ())


if __name__ == "__main__":
    unittest.main()
