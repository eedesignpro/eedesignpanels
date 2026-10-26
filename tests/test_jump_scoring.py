"""Jump scoring: the saw stops short of the panel edge, and what it does not move.

Two distances under `panel.scores`, one for horizontal saw lines and one for
vertical ones, each stopping a score pass short of the panel edge at BOTH
ends so the panel keeps a continuous rim of material and stays rigid.

**Most of this file is about what jump scoring does and does not touch.** The
feature itself is four lines of arithmetic; the risk is all in the neighbours,
and each one has a test of its own here:

* the copper KEEP-OUT round a saw line follows the shortened line, because the
  keep-out guards the cut;
* the keep-outs that RIDE THE BOARDS in from step and repeat are not touched at
  all, because this never reads a zone;
* the ROUTE-PAST junction verdict does not move, because it is decided at prep
  time on the source board against the score PLANE, and this runs on the panel
  after the panel exists;
* the TOOLPATH layer and `Edge.Cuts` do not move, because a score line is not a
  cutter pass;
* a RAIL pass is not shortened, because it runs along the rim the jump keeps
  whole; inter-row and inter-column passes are shortened like a board's own;
* and a held stepped panel is not disturbed, because a shortened pass is a NEW
  drawing put in the old one's place rather than an edit to a shared item.

Zero, which is the default and what a config with no jump distance reads as,
is the full-span pass, to the byte.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from eedesigncore import Board, PcbLine, Stroke
from pydantic import ValidationError

from eedesignpanels import prep
from eedesignpanels.config import (
    CONFIG_REVISION,
    MAX_JUMP_MM,
    PanelScoresConfig,
    load_config,
)
from eedesignpanels.preview import panelbuild
from eedesignpanels.stages import panel_scores, panelize, step_and_repeat
from test_carrier_features import outline, source_board
from test_panel_cache import config_with, from_scratch, prepped_board
from test_vscore import accept, scoreable_board

# The fixture panel: two 10 mm boards side by side with a 3 mm gap and a 5 mm
# rail all round, so the blank runs -5..28 across and -5..15 down. A vertical
# pass therefore spans 20 mm and a horizontal one 33 mm - both long enough to
# jump and short enough to read in a failure message.
BLANK_TOP = -5.0
BLANK_BOTTOM = 15.0
BLANK_LEFT = -5.0
BLANK_RIGHT = 28.0


def scored_source() -> Board:
    """The carrier fixture board, with a TOP edge score beside its left one.

    Two scores on two axes is what makes the axis convention testable: one line
    must take the vertical distance and the other the horizontal one, and a
    fixture with one score cannot tell a right answer from a lucky one.
    """
    board = source_board()
    board.drawings.append(outline(
        0.0, 0.0, 10.0, 0.0, layer="User.38", width=0.1, uid="score-top",
    ))
    return board


def scores_config(**keys) -> PanelScoresConfig:
    return PanelScoresConfig(**keys)


def panel_with(**keys) -> tuple:
    """A 2 x 1 panel of the two-score board, and what panelizing reported."""
    outcome = step_and_repeat.build(
        scored_source(), 2, 1, 3.0, rails=5.0, fill_rails=False,
        scores=scores_config(**keys),
    )
    return outcome.board, outcome.panelize


def score_lines(board: Board) -> list:
    """Every `User.38` line on the panel, in a fixed order."""
    return panel_scores.score_lines_on(board)


def spans(board: Board) -> set:
    return {(line.start, line.end) for line in score_lines(board)}


class AxisConventionTest(unittest.TestCase):
    """THE DISTANCE BELONGS TO THE LINE'S OWN DIRECTION."""

    def test_a_line_running_up_and_down_is_vertical(self):
        self.assertEqual(panelize.jump_axis((4.0, -5.0), (4.0, 15.0)),
                         "vertical")

    def test_a_line_running_left_and_right_is_horizontal(self):
        self.assertEqual(panelize.jump_axis((-5.0, 4.0), (28.0, 4.0)),
                         "horizontal")

    def test_the_direction_it_was_drawn_in_does_not_change_the_answer(self):
        # A score is a cut, not a journey: the same pass drawn backwards is the
        # same pass, and `merge_scores` already normalises it.
        self.assertEqual(panelize.jump_axis((4.0, 15.0), (4.0, -5.0)),
                         "vertical")
        self.assertEqual(panelize.jump_axis((28.0, 4.0), (-5.0, 4.0)),
                         "horizontal")

    def test_a_diagonal_pass_takes_neither_distance(self):
        # Named rather than assigned to whichever axis it leans towards: a
        # v-score saw runs along an axis, so a diagonal pass is a question.
        self.assertIsNone(panelize.jump_axis((0.0, 0.0), (10.0, 10.0)))

    def test_a_hair_off_an_axis_is_still_that_axis(self):
        # The merge's own parallel tolerance, so a pass that merged as one blade
        # cannot fail to classify as one direction.
        self.assertEqual(panelize.jump_axis((0.0, 0.0), (0.0000001, 10.0)),
                         "vertical")


class JumpedSpanTest(unittest.TestCase):
    """The arithmetic: both ends, by the same distance, or not at all."""

    def test_both_ends_come_in_by_the_distance(self):
        got = panelize.jumped_span((4.0, -5.0), (4.0, 15.0), 5.0)
        self.assertEqual(got, ((4.0, 0.0), (4.0, 10.0)))

    def test_the_line_keeps_its_direction_and_its_middle(self):
        start, end = panelize.jumped_span((-5.0, 4.0), (28.0, 4.0), 2.5)
        self.assertAlmostEqual((start[0] + end[0]) / 2.0, 11.5)
        self.assertEqual(start[1], 4.0)
        self.assertEqual(end[1], 4.0)

    def test_a_pass_with_no_room_for_the_jump_is_declined(self):
        # 20 mm long, 9.75 mm off each end, 0.5 mm of cut left - which is under
        # the floor, so there is no saw pass to make.
        self.assertIsNone(
            panelize.jumped_span((4.0, -5.0), (4.0, 15.0), 9.75)
        )

    def test_the_floor_is_where_the_floor_is(self):
        # Exactly `MIN_JUMPED_PASS_MM` of cut left survives; a hair under does
        # not. Stated on the boundary because a floor nobody tests is a number.
        distance = (20.0 - panelize.MIN_JUMPED_PASS_MM) / 2.0
        self.assertIsNotNone(
            panelize.jumped_span((4.0, -5.0), (4.0, 15.0), distance)
        )
        self.assertIsNone(
            panelize.jumped_span((4.0, -5.0), (4.0, 15.0), distance + 0.001)
        )


class ConfigTest(unittest.TestCase):
    """Two keys under `panel.scores`, both distances, both zero by default."""

    def test_both_distances_ship_as_zero(self):
        section = PanelScoresConfig()
        self.assertEqual(section.jump_horizontal_mm, 0.0)
        self.assertEqual(section.jump_vertical_mm, 0.0)

    def test_a_negative_distance_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelScoresConfig(jump_horizontal_mm=-1.0)
        with self.assertRaises(ValidationError):
            PanelScoresConfig(jump_vertical_mm=-0.001)

    def test_a_distance_bigger_than_any_panel_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelScoresConfig(jump_vertical_mm=MAX_JUMP_MM + 1.0)

    def test_a_misspelled_distance_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValidationError):
            PanelScoresConfig(jump_mm=5.0)

    def test_the_settings_carry_both_distances_into_the_stage(self):
        settings = panel_scores.Settings.of(
            PanelScoresConfig(jump_horizontal_mm=2.0, jump_vertical_mm=3.0)
        )
        self.assertEqual(settings.jump_horizontal_mm, 2.0)
        self.assertEqual(settings.jump_vertical_mm, 3.0)
        self.assertTrue(settings.jumping)

    def test_a_section_written_before_the_jump_settings_reads_as_zero(self):
        # A mapping with neither key reads as zero, which is the full-span
        # pass.
        settings = panel_scores.Settings.of({"vertical": True})
        self.assertEqual(settings.jump_horizontal_mm, 0.0)
        self.assertEqual(settings.jump_vertical_mm, 0.0)
        self.assertFalse(settings.jumping)


class TrimTest(unittest.TestCase):
    """What the two distances do to a panel's merged board-defined passes."""

    def test_a_vertical_pass_takes_the_vertical_distance(self):
        # A greyed-out dependent setting contributes nothing:
        # `jump_vertical_mm` hangs off `rails_horizontal` - a vertical pass
        # ends at the top and bottom rails - so the governing switch has to
        # be on too, or the distance is zeroed before it is ever read.
        board, made = panel_with(jump_vertical_mm=4.0, rails_horizontal=True)
        verticals = [
            line for line in score_lines(board)
            if panelize.jump_axis(line.start, line.end) == "vertical"
        ]
        self.assertTrue(verticals)
        for line in verticals:
            self.assertAlmostEqual(line.start[1], BLANK_TOP + 4.0)
            self.assertAlmostEqual(line.end[1], BLANK_BOTTOM - 4.0)
        self.assertEqual(made.jumps.count, len(verticals))

    def test_a_horizontal_pass_is_untouched_by_the_vertical_distance(self):
        board, _ = panel_with(jump_vertical_mm=4.0, rails_horizontal=True)
        horizontals = [
            line for line in score_lines(board)
            if panelize.jump_axis(line.start, line.end) == "horizontal"
        ]
        self.assertTrue(horizontals)
        for line in horizontals:
            self.assertAlmostEqual(line.start[0], BLANK_LEFT)
            self.assertAlmostEqual(line.end[0], BLANK_RIGHT)

    def test_a_horizontal_pass_takes_the_horizontal_distance(self):
        # `jump_horizontal_mm` hangs off `rails_vertical` for the mirror
        # reason: a horizontal pass ends at the left and right rails.
        board, made = panel_with(jump_horizontal_mm=6.0, rails_vertical=True)
        horizontals = [
            line for line in score_lines(board)
            if panelize.jump_axis(line.start, line.end) == "horizontal"
        ]
        self.assertTrue(horizontals)
        for line in horizontals:
            self.assertAlmostEqual(line.start[0], BLANK_LEFT + 6.0)
            self.assertAlmostEqual(line.end[0], BLANK_RIGHT - 6.0)
        self.assertEqual(made.jumps.count, len(horizontals))

    def test_the_two_distances_are_separable(self):
        # Both governors on, and both shared-line ticks turned OFF: with the
        # boards' own left and top edges landing exactly on the rail inner
        # edges in this fixture, the default (rail) reading would put them
        # back to full span and there would be nothing left to measure.
        board, made = panel_with(
            jump_horizontal_mm=6.0, jump_vertical_mm=4.0,
            rails_horizontal=True, rails_vertical=True,
            shared_line_is_rail_score_horizontal=False,
            shared_line_is_rail_score_vertical=False,
        )
        lines = score_lines(board)
        left_edge = next(
            line for line in lines
            if panelize.jump_axis(line.start, line.end) == "vertical"
            and line.start[0] == 0.0
        )
        top_edge = next(
            line for line in lines
            if panelize.jump_axis(line.start, line.end) == "horizontal"
            and line.start[1] == 0.0
        )
        self.assertAlmostEqual(left_edge.start[1], BLANK_TOP + 4.0)
        self.assertAlmostEqual(left_edge.end[1], BLANK_BOTTOM - 4.0)
        self.assertAlmostEqual(top_edge.start[0], BLANK_LEFT + 6.0)
        self.assertAlmostEqual(top_edge.end[0], BLANK_RIGHT - 6.0)
        self.assertEqual(made.jumps.horizontal_mm, 6.0)
        self.assertEqual(made.jumps.vertical_mm, 4.0)

    def test_a_trimmed_pass_keeps_its_identity(self):
        # Same pass, not a second one: the uuid is derived from the score lines
        # the boards decided, and stopping short does not make it another cut.
        plain, _ = panel_with()
        jumped, _ = panel_with(jump_vertical_mm=4.0)
        self.assertEqual(
            sorted(line.id for line in score_lines(plain)),
            sorted(line.id for line in score_lines(jumped)),
        )

    def test_a_pass_with_no_room_is_left_alone_and_said(self):
        board, made = panel_with(jump_vertical_mm=9.9, rails_horizontal=True)
        self.assertEqual(made.jumps.count, 0)
        self.assertTrue(made.jumps.declined)
        self.assertIn("left running edge to edge", made.jumps.declined[0])
        verticals = [
            line for line in score_lines(board)
            if panelize.jump_axis(line.start, line.end) == "vertical"
        ]
        for line in verticals:
            self.assertAlmostEqual(line.start[1], BLANK_TOP)
            self.assertAlmostEqual(line.end[1], BLANK_BOTTOM)

    def test_a_declined_pass_reaches_the_run_report(self):
        _, made = panel_with(jump_vertical_mm=9.9, rails_horizontal=True)
        self.assertTrue(
            any("left running edge to edge" in note for note in made.warnings)
        )

    def test_reporting_is_silent_when_no_distance_was_asked_for(self):
        _, made = panel_with()
        self.assertIsNotNone(made.jumps)
        self.assertFalse(made.jumps.asked)
        self.assertEqual(panelbuild.jump_lines(made.jumps), [])

    def test_reporting_says_both_distances_when_one_was(self):
        _, made = panel_with(jump_vertical_mm=4.0, rails_horizontal=True)
        lines = panelbuild.jump_lines(made.jumps)
        self.assertTrue(lines)
        self.assertIn("short of the panel edge", lines[0])

    def test_no_scores_section_means_no_jump_at_all(self):
        # The two distances live in `panel.scores`, so a config with no section
        # says nothing about them - which is not the same as saying zero.
        outcome = step_and_repeat.build(
            scored_source(), 2, 1, 3.0, rails=5.0, fill_rails=False,
        )
        self.assertIsNone(outcome.panelize.jumps)


class LeavesTheNeighboursAloneTest(unittest.TestCase):
    """What jump scoring moves and does not move, asserted on the geometry."""

    @classmethod
    def setUpClass(cls):
        cls.plain, cls.plain_made = panel_with()
        cls.jumped, cls.jumped_made = panel_with(
            jump_vertical_mm=4.0, jump_horizontal_mm=6.0,
            rails_horizontal=True, rails_vertical=True,
            shared_line_is_rail_score_horizontal=False,
            shared_line_is_rail_score_vertical=False,
        )

    def test_the_keep_out_follows_the_shortened_line(self):
        # The keep-out guards the cut, so a jumped pass is guarded over
        # exactly its own shorter run, plus one keep-out width past each of
        # its closer-in ends.
        # The two rail switches also add "right rail" and "bottom rail" as
        # NEW passes of their own (they land nowhere the board already
        # scored), so `jumped` naturally carries more rule areas than
        # `plain` - what is fenced is that the two lines they SHARE
        # (the board's own left and top edges) come back shortened.
        plain_spans = {(area.start, area.end)
                       for area in self.plain_made.panel_scores.rule_areas}
        jumped_spans = {(area.start, area.end)
                        for area in self.jumped_made.panel_scores.rule_areas}
        self.assertFalse(plain_spans & jumped_spans)

    def test_the_keep_out_zones_in_the_file_differ_with_the_shortened_line(self):
        # Read off the FILE rather than off the report, because the report is
        # what the code says it did and the file is what a fabricator gets.
        self.assertNotEqual(
            [zone.model_dump_json() for zone in self.plain.zones],
            [zone.model_dump_json() for zone in self.jumped.zones],
        )

    def test_the_toolpath_layer_does_not_move(self):
        # A score line is not a cutter pass. The outline invariant reads this
        # layer, so a change here would be a change to the panel's edge.
        self.assertEqual(self.layer(self.plain, "User.36"),
                         self.layer(self.jumped, "User.36"))

    def test_the_panel_outline_does_not_move(self):
        self.assertEqual(self.layer(self.plain, "Edge.Cuts"),
                         self.layer(self.jumped, "Edge.Cuts"))

    def test_only_the_score_layer_differs_at_all(self):
        # The whole blast radius, stated as one assertion: strip `User.38` out
        # of both panels and they are the same file.
        self.assertEqual(self.without_scores(self.plain),
                         self.without_scores(self.jumped))
        self.assertNotEqual(self.plain.to_string(), self.jumped.to_string())

    def test_a_rail_pass_is_never_shortened(self):
        # A RAIL SCORE IS THE ONE EXCEPTION: it runs along the rim the jump
        # exists to keep whole. Inter-row and inter-column passes are
        # shortened (see below).
        board, made = panel_with(rails_horizontal=True, rails_vertical=True,
                                 jump_vertical_mm=4.0, jump_horizontal_mm=4.0)
        own = {
            (score.start, score.end) for score in made.panel_scores.scores
        }
        self.assertTrue(own)
        self.assertTrue(own <= spans(board))

    def test_an_inter_row_and_inter_column_pass_are_shortened_like_a_boards_own(
        self,
    ):
        # A jump shortens these exactly as it shortens the lines the boards
        # decided. The two rail switches are on only to unlock the two jump
        # distances they govern.
        _, made = panel_with(inter_column=True, inter_row=True,
                             rails_horizontal=True, rails_vertical=True,
                             jump_vertical_mm=4.0, jump_horizontal_mm=4.0)
        by_source = {score.source: score for score in made.panel_scores.scores}
        column = by_source["column gap 1"]
        self.assertAlmostEqual(column.start[1], BLANK_TOP + 4.0)
        self.assertAlmostEqual(column.end[1], BLANK_BOTTOM - 4.0)

    @staticmethod
    def layer(board: Board, name: str) -> list:
        return [
            item.model_dump_json() for item in board.drawings
            if getattr(item, "layer", None) == name
        ]

    @staticmethod
    def without_scores(board: Board) -> list:
        return [
            item.model_dump_json() for item in board.drawings
            if getattr(item, "layer", None) != "User.38"
        ]


class PrepIsUntouchedTest(unittest.TestCase):
    """The route-past junction verdict measures a PLANE, and lives at prep time.

    `route_past_score_mm` is decided on
    the SOURCE board, against the infinite plane the blade runs in, before any
    panel exists - so a drawn line that stops short of the panel edge is a
    statement about the saw's runway and cannot move a verdict. Asserted rather
    than reasoned about: the prepped board is compared byte for byte across a
    jump-distance change.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def prepped(self, name: str, scores: dict):
        """Prep a project whose board carries an ACCEPTED score, and report it.

        The board is `accept(scoreable_board())` - a square with three tabbed
        edges and a scored left one - because that is the smallest design whose
        prep produces junctions at all, and a junction is what this class is
        about.
        """
        folder = self.folder / name
        folder.mkdir(parents=True, exist_ok=True)
        accept(scoreable_board(), x=0.0).to_file(folder / "board.kicad_pcb")
        panel_folder = folder / "panel"
        panel_folder.mkdir(exist_ok=True)
        config_path = panel_folder / "panel.json"
        config_path.write_text(json.dumps({
            "revision": CONFIG_REVISION,
            "source_board": "../board.kicad_pcb",
            "stage": "build_panel",
            "grid": {"columns": 2, "rows": 1, "gap_mm": 3.0},
            "panel": {"rails": 5.0, "scores": scores},
            "prep": {
                "cutter_diameter": 0.889,
                "vscore": {"detect": "auto", "route_past_score_mm": 0.2},
            },
        }, indent=2) + "\n", encoding="utf-8")
        computed = prep.compute(load_config(config_path))
        return computed.board.to_string(), computed.result

    @staticmethod
    def verdicts(report) -> list:
        """Every junction's verdict, its overshoot and the material it left."""
        return [
            (junction.score, junction.at, junction.verdict, junction.reason,
             round(junction.past_mm, 9), round(junction.standing_mm, 9),
             junction.reached)
            for junction in report.junctions
        ]

    def test_a_jump_distance_moves_no_junction_verdict(self):
        # The drawn line is the saw's runway statement; the PLANE it runs in is infinite and is what the
        # junction rule measures - so a pass stopping short of the panel edge
        # cannot move a verdict, and nor can the setting that stops it.
        _, plain = self.prepped("plain", {})
        _, jumped = self.prepped(
            "jumped",
            {"jump_vertical_mm": 4.0, "jump_horizontal_mm": 4.0},
        )
        self.assertEqual(len(plain.junctions), 2)
        self.assertEqual(self.verdicts(plain), self.verdicts(jumped))
        self.assertEqual(list(plain.warnings), list(jumped.warnings))

    def test_a_jump_distance_changes_no_prepped_byte(self):
        one, _ = self.prepped("bytes-plain", {})
        two, _ = self.prepped(
            "bytes-jumped",
            {"jump_vertical_mm": 4.0, "jump_horizontal_mm": 4.0},
        )
        self.assertEqual(one, two)


class ByteIdentityTest(unittest.TestCase):
    """A jump distance of zero builds the full-span panel, to the byte."""

    def test_zero_builds_the_panel_a_section_with_no_keys_builds(self):
        old, _ = panel_with()
        new, _ = panel_with(jump_horizontal_mm=0.0, jump_vertical_mm=0.0)
        self.assertEqual(old.to_string(), new.to_string())

    def test_zero_builds_the_panel_no_jump_code_would_build(self):
        # The pass is produced to the blank's own edge, which is what
        # `merge_scores` has always done. Stated as the geometry rather than as
        # a hash, so a failure says which end moved.
        board, _ = panel_with()
        for line in score_lines(board):
            if panelize.jump_axis(line.start, line.end) == "vertical":
                self.assertAlmostEqual(line.start[1], BLANK_TOP)
                self.assertAlmostEqual(line.end[1], BLANK_BOTTOM)
            else:
                self.assertAlmostEqual(line.start[0], BLANK_LEFT)
                self.assertAlmostEqual(line.end[0], BLANK_RIGHT)

    def test_two_builds_at_one_distance_are_byte_identical(self):
        first, _ = panel_with(jump_vertical_mm=4.0, jump_horizontal_mm=6.0)
        second, _ = panel_with(jump_vertical_mm=4.0, jump_horizontal_mm=6.0)
        self.assertEqual(first.to_string(), second.to_string())

    def test_a_distance_that_moves_nothing_still_writes_the_same_panel(self):
        # A horizontal distance on a panel whose only pass is vertical: the
        # setting is live, the geometry is not, and the file must not drift.
        outcome = step_and_repeat.build(
            source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
            scores=scores_config(jump_horizontal_mm=6.0),
        )
        plain = step_and_repeat.build(
            source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
            scores=scores_config(),
        )
        self.assertEqual(outcome.board.to_string(), plain.board.to_string())


class HeldPanelIsSafeTest(unittest.TestCase):
    """A shortened pass is a NEW drawing, never an edit to a shared item.

    `step_and_repeat.finish` puts the carrier features on a cheap copy that
    SHARES its items with the stepped panel somebody else is holding, so a stage
    that changed an item in place would change a panel it does not own. Jump
    scoring is the first stage that replaces rather than appends, which is why
    this class exists.
    """

    def setUp(self):
        self.built = step_and_repeat.stepped_build(
            scored_source(), 2, 1, 3.0, rails=5.0,
        )

    def finish(self, **keys):
        return step_and_repeat.finish(
            self.built, scores=scores_config(**keys), fill_rails=False,
        )

    def test_a_jumped_finish_leaves_the_stepped_panel_alone(self):
        before = self.built.board.to_string()
        self.finish(jump_vertical_mm=4.0, jump_horizontal_mm=6.0)
        self.assertEqual(self.built.board.to_string(), before)

    def test_two_finishes_of_one_build_do_not_compound(self):
        # The failure this shape can have: the second finish finding the first
        # one's trim already applied and trimming it again.
        first = self.finish(jump_vertical_mm=4.0).board
        second = self.finish(jump_vertical_mm=4.0).board
        self.assertEqual(first.to_string(), second.to_string())

    def test_a_jumped_finish_then_a_plain_one_is_the_plain_panel(self):
        self.finish(jump_vertical_mm=4.0)
        after = self.finish().board
        plain = step_and_repeat.build(
            scored_source(), 2, 1, 3.0, rails=5.0, fill_rails=False,
            scores=scores_config(),
        ).board
        self.assertEqual(after.to_string(), plain.to_string())

    def test_the_shared_item_itself_is_never_edited(self):
        # Read off the objects: the drawing the stepped panel holds must be the
        # drawing it held, coordinates included.
        held = {
            id(item): (item.start, item.end)
            for item in self.built.board.drawings
            if isinstance(item, PcbLine)
        }
        self.finish(jump_vertical_mm=4.0, jump_horizontal_mm=6.0)
        for item in self.built.board.drawings:
            if isinstance(item, PcbLine):
                self.assertEqual((item.start, item.end), held[id(item)])


class CacheTest(unittest.TestCase):
    """A jump distance edit rebuilds from scratch, and agrees with one anyway.

    `panel.scores` carries the two rail SETBACKS too, and a setback GROWS THE
    BLANK - a real layout change - so the whole section counts as layout and
    the fingerprint does not exclude it just because a jump distance moves
    nothing by itself. What matters, cached or not, is that the panel a jump
    edit produces is the panel a FROM-SCRATCH build writes, byte for byte.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.plain = config_with(
            self.folder / "plain",
            panel={"scores": {"rails_horizontal": False,
                              "rails_vertical": False,
                              "inter_row": False, "inter_column": False}},
        )
        self.snapshot = prepped_board(self.plain)

    def tearDown(self):
        self._folder.cleanup()

    def jumped(self, name: str, **keys) -> object:
        section = {"rails_horizontal": False, "rails_vertical": False,
                  "inter_row": False, "inter_column": False}
        section.update(keys)
        return config_with(self.folder / name, panel={"scores": section})

    def test_a_jump_distance_now_invalidates_the_stepped_panel(self):
        # `panel.scores` also carries the rail setbacks, which grow the blank,
        # so the fingerprint does not exclude the section just because THIS
        # field moves nothing.
        one = panelbuild.layout_fingerprint(self.plain)
        two = panelbuild.layout_fingerprint(
            self.jumped("fp", jump_vertical_mm=4.0)
        )
        self.assertNotEqual(one, two)

    def test_every_jump_edit_writes_the_from_scratch_panel(self):
        built = step_and_repeat.stepped_from(self.plain, self.snapshot.board)
        four = self.jumped("four", jump_vertical_mm=4.0)
        none = self.jumped("none")
        for config in (four, none, four):
            with self.subTest(config=config.panel.scores.jump_vertical_mm):
                panel = step_and_repeat.finish_from(
                    config, built, fill_rails=False,
                ).board
                self.assertEqual(panel.to_string(),
                                 from_scratch(config, self.snapshot))


class LoadedConfigTest(unittest.TestCase):
    """The two keys survive a round trip through `panel.json`."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def config(self, section: dict):
        from test_panel_page import write_panel_project

        path = write_panel_project(self.folder)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["panel"]["scores"] = section
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return load_config(path)

    def test_the_distances_reach_the_build(self):
        config = self.config({"enabled": True, "inter_column": True,
                              "jump_horizontal_mm": 2.5,
                              "jump_vertical_mm": 1.5})
        _, scores, _ = config.carrier_features
        settings = panel_scores.Settings.of(scores)
        self.assertEqual(settings.jump_horizontal_mm, 2.5)
        self.assertEqual(settings.jump_vertical_mm, 1.5)

    def test_a_file_written_before_the_jump_settings_loads_at_zero(self):
        config = self.config({"inter_column": True})
        self.assertEqual(config.panel.scores.jump_horizontal_mm, 0.0)
        self.assertEqual(config.panel.scores.jump_vertical_mm, 0.0)

    def test_a_distance_bigger_than_any_panel_is_refused_by_the_loader(self):
        from eedesignpanels.config import ConfigError

        with self.assertRaises(ConfigError):
            self.config({"jump_vertical_mm": MAX_JUMP_MM + 1.0})


class TrimJumpsDirectlyTest(unittest.TestCase):
    """The function on its own, against a board it can be read off."""

    def board_with(self, *lines) -> Board:
        board = Board(version=20260206, generator="pcbnew",
                      generator_version="10.0")
        board.drawings = list(lines)
        return board

    def line(self, x0, y0, x1, y1, uid) -> PcbLine:
        return PcbLine(start=(x0, y0), end=(x1, y1), layer="User.38",
                       stroke=Stroke(width=0.1), id=uid)

    def blade(self, line) -> panelize.BladeLine:
        return panelize.BladeLine(start=line.start, end=line.end,
                                  width_mm=0.1, sources=(line.id,))

    def test_nothing_happens_when_both_distances_are_zero(self):
        one = self.line(0.0, -5.0, 0.0, 15.0, "a")
        board = self.board_with(one)
        got = panelize.trim_jumps(board, (self.blade(one),), 0.0, 0.0)
        self.assertFalse(got.asked)
        self.assertIs(board.drawings[0], one)

    def test_a_line_that_is_not_a_merged_blade_is_not_touched(self):
        # The panel's own passes are drawn by another stage and are not in the
        # blade list, which is how this tells the two apart.
        merged = self.line(0.0, -5.0, 0.0, 15.0, "a")
        own = self.line(11.5, -5.0, 11.5, 15.0, "b")
        board = self.board_with(merged, own)
        panelize.trim_jumps(board, (self.blade(merged),), 0.0, 4.0)
        self.assertIs(board.drawings[1], own)
        self.assertEqual(board.drawings[0].start, (0.0, -1.0))

    def test_the_trimmed_line_keeps_its_place_in_the_file(self):
        # Replaced in place rather than removed and appended, so the layer reads
        # in the order it read before and the bytes do not shuffle.
        first = self.line(0.0, -5.0, 0.0, 15.0, "a")
        second = self.line(13.0, -5.0, 13.0, 15.0, "b")
        board = self.board_with(first, second)
        panelize.trim_jumps(
            board, (self.blade(first), self.blade(second)), 0.0, 4.0
        )
        self.assertEqual([item.id for item in board.drawings], ["a", "b"])

    def test_a_diagonal_blade_is_declined_and_named(self):
        one = self.line(0.0, 0.0, 10.0, 10.0, "a")
        board = self.board_with(one)
        warnings = []
        got = panelize.trim_jumps(board, (self.blade(one),), 4.0, 4.0,
                                  warnings=warnings)
        self.assertEqual(got.count, 0)
        self.assertEqual(len(got.declined), 1)
        self.assertIn("neither axis", got.declined[0])
        self.assertEqual(warnings, list(got.declined))
        self.assertIs(board.drawings[0], one)

    def test_a_trailing_drawing_is_trimmed_too(self):
        # `User.38` lines can sit in either list, and a pass the saw runs is a
        # pass the saw runs whichever one holds it.
        one = self.line(0.0, -5.0, 0.0, 15.0, "a")
        board = self.board_with()
        board.trailing_drawings = [one]
        panelize.trim_jumps(board, (self.blade(one),), 0.0, 4.0)
        self.assertEqual(board.trailing_drawings[0].start, (0.0, -1.0))

    def test_the_length_reported_is_the_length_cut(self):
        one = self.line(0.0, -5.0, 0.0, 15.0, "a")
        board = self.board_with(one)
        got = panelize.trim_jumps(board, (self.blade(one),), 0.0, 4.0)
        self.assertEqual(got.count, 1)
        self.assertAlmostEqual(got.trimmed[0].length_mm, 12.0)
        self.assertEqual(got.trimmed[0].axis, "vertical")
        self.assertEqual(got.trimmed[0].was, ((0.0, -5.0), (0.0, 15.0)))

    def test_the_trim_is_symmetric_to_the_micrometre(self):
        one = self.line(0.0, -5.0, 0.0, 15.0, "a")
        board = self.board_with(one)
        panelize.trim_jumps(board, (self.blade(one),), 0.0, 3.3)
        line = board.drawings[0]
        self.assertAlmostEqual(line.start[1] - (-5.0), 15.0 - line.end[1],
                               places=9)
        self.assertAlmostEqual(math.dist(line.start, line.end), 20.0 - 6.6)


if __name__ == "__main__":
    unittest.main()
