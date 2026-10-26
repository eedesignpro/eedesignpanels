"""Step 2's carrier stages: the groups, the switches, and when the pour fills.

Four subjects, and each one is a thing the user would meet as a defect.

**Each carrier stage is a section of `panel.json`, and `enabled` is its
switch.** Switching a stage on writes its section with the model's own
defaults; switching it off writes `"enabled": false` and keeps the section. A
panel built with a stage switched on and off again is byte for byte the panel
built from a config that never named it. That is asserted on board text, not
on the shape of the code.

**The tooling holes run unasked**: a config that never names them builds them,
so their switch shows ON, and off has to be written as `"enabled": false`.

**A field of a stage that is off writes its section rather than being
refused.** Typing a score value writes `panel.scores` with `enabled: false`
and the field's own value. The only thing a field edit needs is the `panel`
section itself; which STAGE actually runs is `enabled`'s answer alone.

**THE FILL IS PAID OFF THE RESPONSIVE PATH.** Filling the rail pour is KiCad's
work and costs most of ten seconds; a change that recalculates on entry may cost
about one. So every build passes `fill_rails=False` - asserted on the CALL,
because that flag is invisible in the result - and the copper is filled
afterwards and transplanted into the panel that is held. The phase that does
that, and the save that will not write bare rails without it, are
`test_preview_pours`; what is asserted here is that no build asks for a fill.

**The page says all of it.** The groups, their helper lines, the insets that
appear only in fixed mode, and the sentence that explains an empty pour, read
off the bytes that are served.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from eedesigncore import Board

from eedesignpanels.config import load_config
from eedesignpanels.preview import (
    actions,
    naming,
    page_panel,
    page_served,
    panelbuild,
)
from eedesignpanels.stages import step_and_repeat
from test_panel_page import a_session, controls_of, write_panel_project
from test_preview_pours import HeldFill

ALL_GROUPS = ("tooling_holes_on", "rail_copper_on", "scores_on")


def panel_text(config, source: Board) -> str:
    """The panel this config builds, as file text. The byte-identity gate."""
    return panelbuild.derive(config, source, fill_rails=False).outcome \
        .board.to_string()


# ---------------------------------------------------------------------------
# the switches: a section, written once, and `enabled` inside it
# ---------------------------------------------------------------------------


class CarrierSwitchTest(unittest.TestCase):
    """Each carrier stage is a section of the config, switched by `enabled`."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def switch(self, data, name, on):
        return actions.set_param(data, self.config_path, name, on)

    def test_the_opt_in_stages_are_off_in_a_config_that_never_heard_of_them(self):
        config = load_config(self.config_path)
        params = page_panel.params_json(config)
        for name in ("scores_on", "rail_copper_on"):
            with self.subTest(group=name):
                self.assertFalse(params[name])
        _, scores, copper = config.carrier_features
        self.assertIsNone(scores)
        self.assertIsNone(copper)

    def test_the_tooling_holes_show_ON_in_a_config_that_never_named_them(self):
        # They run unasked, so the switch has to say so - a page showing them
        # off over a panel that has them would be wrong.
        config = load_config(self.config_path)
        self.assertTrue(page_panel.params_json(config)["tooling_holes_on"])
        self.assertIsNotNone(config.carrier_features[0])

    def test_switching_one_on_writes_its_section_with_stated_defaults(self):
        changed = self.switch(self.data, "scores_on", True)
        self.assertTrue(changed.changed)
        section = changed.data["panel"]["scores"]
        # EXPLICITLY, not as an empty object: panel.json fully determines the
        # panel, so what the user saw is what the file says. The keep-out
        # width is NOT among them - it is a prep setting - and the two
        # setbacks, the two jump distances and the two shared-line switches
        # are.
        self.assertEqual(
            sorted(section),
            ["enabled", "inter_column", "inter_row",
             "jump_horizontal_mm", "jump_vertical_mm",
             "rail_setback_horizontal_mm", "rail_setback_vertical_mm",
             "rails_horizontal", "rails_vertical",
             "shared_line_is_rail_score_horizontal",
             "shared_line_is_rail_score_vertical"],
        )
        self.assertEqual(section["jump_horizontal_mm"], 0.0)
        self.assertEqual(section["jump_vertical_mm"], 0.0)
        self.assertFalse(section["inter_column"])
        self.assertTrue(section["enabled"])
        self.assertIsNotNone(changed.config.panel.score_settings)

    def test_switching_the_holes_off_declines_them_in_the_file(self):
        # The section is the switch for the opt-in stages; for a stage that runs
        # unasked, `enabled` is - and taking the section out would turn it back
        # on. The rest of the numbers are stated with it.
        changed = self.switch(self.data, "tooling_holes_on", False)
        self.assertTrue(changed.changed)
        section = changed.data["panel"]["tooling_holes"]
        self.assertFalse(section["enabled"])
        self.assertEqual(section["diameter_mm"], 3.175)
        self.assertEqual(section["placement"], "rail_corner_center")
        self.assertIsNone(changed.config.panel.tooling_hole_settings)

    def test_switching_the_holes_back_on_enables_them_again(self):
        off = self.switch(self.data, "tooling_holes_on", False).data
        on = self.switch(off, "tooling_holes_on", True)
        self.assertTrue(on.changed)
        self.assertTrue(on.data["panel"]["tooling_holes"]["enabled"])
        self.assertIsNotNone(on.config.panel.tooling_hole_settings)

    def test_writing_a_carrier_section_the_first_time_is_always_a_change(self):
        # Writing the section IS a change even when the switch lands where it
        # already was: the panel's numbers are stated, where the file said
        # nothing before.
        first = self.switch(self.data, "tooling_holes_on", True)
        self.assertTrue(first.changed)

    def test_switching_the_holes_on_when_they_already_run_writes_nothing(self):
        first = self.switch(self.data, "tooling_holes_on", True)
        again = self.switch(first.data, "tooling_holes_on", True)
        self.assertFalse(again.changed)
        self.assertEqual(again.data, first.data)

    def test_each_switch_writes_only_its_own_section(self):
        # Every carrier section is written the first time anything touches
        # it, tooling holes included.
        data = self.data
        for name in ALL_GROUPS:
            data = self.switch(data, name, True).data
        panel = data["panel"]
        self.assertEqual(sorted(panel),
                         ["rail_copper", "rails", "scores", "tooling_holes"])
        # The score switches are written as switches, and no typed position
        # list is written with them.
        self.assertEqual(
            {key: panel["scores"][key]
             for key in ("rails_horizontal", "rails_vertical",
                        "inter_row", "inter_column")},
            {"rails_horizontal": False, "rails_vertical": False,
             "inter_row": False, "inter_column": False},
        )
        self.assertNotIn("vertical_mm", panel["scores"])
        self.assertEqual(panel["rail_copper"]["clearance_mm"], 0.635)
        self.assertFalse(panel["rail_copper"]["pour_between_boards"])

    def test_switching_one_off_again_disables_it_without_removing_the_section(
        self,
    ):
        # `enabled` is the switch for every carrier section, so off keeps the
        # section and writes `enabled: false`.
        on = self.switch(self.data, "scores_on", True).data
        off = self.switch(on, "scores_on", False)
        self.assertTrue(off.changed)
        self.assertIn("scores", off.data["panel"])
        self.assertFalse(off.data["panel"]["scores"]["enabled"])
        self.assertIsNone(off.config.panel.score_settings)

    def test_switching_a_stage_that_is_already_there_changes_nothing(self):
        on = self.switch(self.data, "scores_on", True)
        again = self.switch(on.data, "scores_on", True)
        self.assertFalse(again.changed)
        self.assertEqual(again.data, on.data)
        # ...and off, off - once the section has actually been written.
        off = self.switch(on.data, "scores_on", False)
        self.assertFalse(self.switch(off.data, "scores_on", False).changed)

    def test_writing_a_section_for_the_first_time_is_always_a_change(self):
        # A file that goes from no `scores` key to a full section with
        # `enabled: false` changed, even though it builds the same panel -
        # `panel.json` fully determines the panel, so what the user saw is
        # what the file now says explicitly.
        self.assertTrue(self.switch(self.data, "scores_on", False).changed)

    def test_on_and_off_again_builds_the_panel_it_built_before(self):
        # THE GATE: a config switched on and off again builds the same bytes as
        # one that never named the section, because a disabled section is the
        # stage not running (`score_settings`/`rail_copper_settings` read
        # `None`) whatever numbers the file still states.
        config = load_config(self.config_path)
        source = Board.from_file(config.source_board_path)
        before = panel_text(config, source)

        data = self.data
        for name in ALL_GROUPS:
            data = self.switch(data, name, True).data
        with_stages = actions.validate(data, self.config_path)
        self.assertNotEqual(panel_text(with_stages, source), before)

        for name in ALL_GROUPS:
            data = self.switch(data, name, False).data
        self.assertFalse(data["panel"]["scores"]["enabled"])
        self.assertFalse(data["panel"]["rail_copper"]["enabled"])
        self.assertFalse(data["panel"]["tooling_holes"]["enabled"])

        data = self.switch(data, "tooling_holes_on", True).data
        after = actions.validate(data, self.config_path)
        self.assertEqual(panel_text(after, source), before)

    def test_a_switch_takes_a_switch_and_not_a_number(self):
        with self.assertRaises(actions.ActionError) as raised:
            self.switch(self.data, "scores_on", 1.0)
        self.assertIn("true or false", str(raised.exception))

    def test_no_stage_is_switched_on_in_a_config_that_does_not_panelize(self):
        path = write_panel_project(self.folder / "plain", panelize=False)
        data = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaises(actions.ActionError) as raised:
            actions.set_param(data, path, "tooling_holes_on", True)
        self.assertIn("'panel' section", str(raised.exception))


class CarrierFieldTest(unittest.TestCase):
    """The fields inside a group: what they take, and when they may be typed."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        for name in ALL_GROUPS:
            data = actions.set_param(data, self.config_path, name, True).data
        self.data = data

    def tearDown(self):
        self._folder.cleanup()

    def set(self, name, value, data=None):
        return actions.set_param(
            self.data if data is None else data, self.config_path, name, value
        )

    def test_a_field_of_a_stage_that_is_off_materialises_the_section(self):
        # Tuning a field of a stage nobody switched on writes the section -
        # `enabled: false` and all - so the field's value is kept and the
        # stage stays off until its switch says otherwise.
        bare = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed = actions.set_param(bare, self.config_path,
                                    "score_inter_column", True)
        self.assertTrue(changed.changed)
        section = changed.data["panel"]["scores"]
        self.assertFalse(section["enabled"])
        self.assertTrue(section["inter_column"])
        self.assertIsNone(changed.config.panel.score_settings)

    def test_a_field_still_needs_the_panel_section_itself(self):
        path = write_panel_project(self.folder / "plain", panelize=False)
        bare = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaises(actions.ActionError) as raised:
            actions.set_param(bare, path, "score_inter_column", True)
        self.assertIn("'panel' section", str(raised.exception))

    def test_the_score_keepout_width_is_not_gated_by_the_scores_group(self):
        # The width lives in `prep.vscore`, not `panel.scores`, so it is typed
        # on step 1 and has nothing to do with whether the panel cuts saw
        # passes of its own.
        bare = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed = actions.set_param(bare, self.config_path,
                                    "score_rule_half_width_mm", 0.4)
        self.assertTrue(changed.changed)
        self.assertEqual(
            changed.data["prep"]["vscore"]["rule_half_width_mm"], 0.4
        )
        self.assertEqual(changed.config.score_rule_half_width_mm, 0.4)

    def test_a_hole_field_writes_the_section_the_stage_was_already_running(self):
        # The holes run whether or not the file names them, so typing a
        # diameter is TUNING and not switching a stage on by the back door.
        # The section arrives with every number stated.
        bare = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed = actions.set_param(bare, self.config_path,
                                    "hole_diameter_mm", 4.0)
        self.assertTrue(changed.changed)
        section = changed.data["panel"]["tooling_holes"]
        self.assertEqual(section["diameter_mm"], 4.0)
        self.assertTrue(section["enabled"])
        self.assertEqual(section["placement"], "rail_corner_center")
        self.assertEqual(changed.config.panel.tooling_hole_settings.diameter_mm,
                         4.0)

    def test_tuning_a_hole_field_to_the_value_it_already_had_still_states_it(
        self,
    ):
        # The first tune writes the section, which IS a change to the file even
        # when the number it states is the one the stage was using.
        bare = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed = actions.set_param(bare, self.config_path,
                                    "hole_diameter_mm", 3.175)
        self.assertTrue(changed.changed)
        self.assertEqual(
            changed.data["panel"]["tooling_holes"]["diameter_mm"], 3.175,
        )

    def test_a_number_lands_where_the_config_keeps_it(self):
        changed = self.set("hole_diameter_mm", 4.0)
        self.assertTrue(changed.changed)
        self.assertEqual(changed.config.panel.tooling_holes.diameter_mm, 4.0)

    def test_a_corner_is_a_switch_of_its_own(self):
        changed = self.set("hole_bottom_left", False)
        self.assertFalse(changed.config.panel.tooling_holes.corners.bottom_left)
        self.assertTrue(changed.config.panel.tooling_holes.corners.top_left)

    def test_the_placement_takes_one_of_the_two_named_ways(self):
        changed = self.set("hole_placement", "fixed_inset")
        self.assertEqual(changed.config.panel.tooling_holes.placement,
                         "fixed_inset")
        with self.assertRaises(actions.ActionError) as raised:
            self.set("hole_placement", "wherever")
        self.assertIn("rail_corner_center", str(raised.exception))
        self.assertIn("fixed_inset", str(raised.exception))

    def test_the_insets_are_still_validated_in_either_mode(self):
        changed = self.set("hole_inset_across_mm", 7.5)
        self.assertEqual(
            changed.config.panel.tooling_holes.inset_across_mm, 7.5
        )
        with self.assertRaises(actions.ActionError):
            self.set("hole_inset_across_mm", -1.0)

    def test_each_score_family_is_a_switch_of_its_own(self):
        # Four switches, and not one typed position. The switches say which
        # families of saw pass the panel carries; the build says where each
        # one goes.
        data = self.data
        for name, key in (("score_inter_column", "inter_column"),
                          ("score_inter_row", "inter_row"),
                          ("score_rails_horizontal", "rails_horizontal"),
                          ("score_rails_vertical", "rails_vertical")):
            with self.subTest(param=name):
                changed = self.set(name, True, data)
                self.assertTrue(changed.changed)
                self.assertTrue(getattr(changed.config.panel.scores, key))
                self.assertTrue(changed.data["panel"]["scores"][key])
                data = changed.data

    def test_the_switches_are_independent(self):
        changed = self.set("score_rails_horizontal", True)
        self.assertTrue(changed.config.panel.scores.rails_horizontal)
        self.assertFalse(changed.config.panel.scores.rails_vertical)
        self.assertFalse(changed.config.panel.scores.inter_column)
        self.assertFalse(changed.config.panel.scores.inter_row)

    def test_every_switch_off_is_a_stage_that_still_runs(self):
        # Every switch off is how a panel asks for the keep-outs round the
        # scores its boards already decided, and it is not the stage being off.
        changed = self.set("score_inter_column", False)
        self.assertIsNotNone(changed.config.panel.scores)
        self.assertFalse(changed.config.panel.scores.inter_column)

    def test_a_score_switch_takes_a_switch_and_not_a_position(self):
        for bad in ("11.5", [11.5], 11.5, 1, None):
            with self.subTest(bad=bad), self.assertRaises(actions.ActionError):
                self.set("score_inter_column", bad)

    def test_the_retired_position_lists_are_not_settable_at_all(self):
        # Score positions are computed, not typed, so the old position-list
        # parameters are refused.
        for name in ("score_vertical_mm", "score_horizontal_mm",
                     "score_vertical", "score_horizontal", "score_rails"):
            with self.subTest(param=name), \
                    self.assertRaises(actions.ActionError) as raised:
                self.set(name, [11.5])
            self.assertIn("not a preview parameter", str(raised.exception))

    def test_the_pour_takes_a_clearance_and_a_switch(self):
        changed = self.set("rail_copper_clearance_mm", 0.3)
        self.assertEqual(changed.config.panel.rail_copper.clearance_mm, 0.3)
        changed = self.set("rail_copper_between", True)
        self.assertTrue(changed.config.panel.rail_copper.pour_between_boards)
        with self.assertRaises(actions.ActionError):
            self.set("rail_copper_between", 1)

    def test_every_carrier_field_is_reported_back_by_the_page(self):
        # One table, both ways: everything a page may set is something the page
        # is told the value of, or a control could never be filled in.
        params = page_panel.params_json(
            actions.validate(self.data, self.config_path)
        )
        for name in (*actions.SETTABLE, *actions.CARRIER_SECTIONS):
            # A PREP parameter is reported by the PREP page. The score keep-out
            # width is one, despite its `score_` name.
            if name in actions.PREP_PARAMS:
                continue
            if name.startswith(("hole_", "score_", "rail_copper_")) \
                    or name in ALL_GROUPS:
                with self.subTest(param=name):
                    self.assertIn(name, params)


# ---------------------------------------------------------------------------
# the fill, and the one call that asks for it
# ---------------------------------------------------------------------------


class FillOnSaveTest(unittest.TestCase):
    """`fill_rails` on the call: False on EVERY build, save included.

    The save does not derive the panel a second time with the flag on: the pour
    is filled by a round trip and transplanted into the panel that is held,
    which was measured byte-identical to the filled build
    (`test_rail_copper.LateFillTest`), so a save that rebuilt would pay a
    second's arithmetic to arrive at the same bytes. What is
    asserted here is therefore that NOTHING rebuilds, and the fill seam is
    driven with a stub - the phase itself lives in `test_preview_pours`.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = a_session(self.folder, self.config_path)
        self.fill = HeldFill()
        self.session._fill = self.fill
        self.calls = []
        self.real_build_from = step_and_repeat.build_from
        step_and_repeat.build_from = self.spy

    def tearDown(self):
        step_and_repeat.build_from = self.real_build_from
        self._folder.cleanup()

    def spy(self, config, source, fill_rails=True):
        """The real build, with the flag it was asked for written down.

        The fill itself is never run here - the fixture has no rail copper
        unless a test switches it on, and a test that does switches the
        transplant off by never letting the flag reach a KiCad process.
        """
        self.calls.append(fill_rails)
        return self.real_build_from(config, source, fill_rails=False)

    def switch_on_the_pour(self):
        self.session.set_param({"rail_copper_on": True})

    def test_a_manual_build_asks_for_no_fill(self):
        self.session.build_panel({})
        self.assertEqual(self.calls, [False])
        self.assertFalse(self.session.panel.fill_rails)

    def test_a_responsive_recalculation_asks_for_no_fill(self):
        self.session.build_panel({})
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertEqual(self.calls, [False, False])

    def test_the_save_fills_the_panel_it_holds_and_rebuilds_nothing(self):
        self.session.build_panel({})
        self.switch_on_the_pour()
        self.calls.clear()
        self.session.save_panel()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fill.calls, 1)
        self.assertTrue(self.session.panel.fill_rails)

    def test_a_config_with_no_pour_does_not_rebuild_to_save(self):
        # Nothing to fill, so nothing is paid: the save writes what is held.
        self.session.build_panel({})
        self.calls.clear()
        self.session.save_panel()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fill.calls, 0)

    def test_a_second_save_does_not_fill_twice(self):
        self.session.build_panel({})
        self.switch_on_the_pour()
        self.session.save_panel()
        self.calls.clear()
        self.session.save_panel()
        self.assertEqual(self.fill.calls, 1)
        self.assertEqual(self.calls, [])

    def test_the_journal_says_which_of_the_two_a_build_was(self):
        from eedesignpanels.preview import journal as journal_module

        recording = journal_module.open_session(
            self.config_path, self.folder / "sessions", stamp="20260909_130000"
        )
        session = a_session(self.folder / "j",
                            write_panel_project(self.folder / "j"))
        session.journal = recording
        session.build_panel({})
        session.set_param({"gap_x_mm": 4.0})
        kinds = {
            record["kind"]: record["data"] for record in recording.records()
        }
        self.assertFalse(kinds["panel.build"]["fill_rails"])
        self.assertFalse(kinds["panel.recalc"]["fill_rails"])

    def test_the_state_file_says_whether_the_picture_carries_copper(self):
        self.session.build_panel({})
        state = self.session.panel_state()
        self.assertFalse(state["fills_on_save"])
        self.switch_on_the_pour()
        state = self.session.panel_state()
        self.assertTrue(state["fills_on_save"])
        self.assertFalse(state["rail_copper_filled"])
        self.assertTrue(
            self.session.backend_state()["panel"]["fills_on_save"]
        )


class FillFailureTest(unittest.TestCase):
    """A fill that cannot be done says so, and writes nothing."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = a_session(self.folder, self.config_path)
        self.session.build_panel({})
        self.session.set_param({"rail_copper_on": True})

    def tearDown(self):
        self._folder.cleanup()

    def test_a_refill_that_fails_is_one_sentence_and_no_files(self):
        from eedesignpanels.stages import refill

        def fail(*args, **kwargs):
            raise refill.RefillError("kicad-cli is not at C:/nowhere")

        # The seam that needs KiCad is the round trip, and this is it failing.
        self.session._fill = fail
        with self.assertRaises(actions.ActionError) as raised:
            self.session.save_panel()
        self.assertIn("kicad-cli", str(raised.exception))
        self.assertFalse(self.session.config.output_board_path.exists())


# ---------------------------------------------------------------------------
# what the report says each stage did
# ---------------------------------------------------------------------------


def made_with(holes=None, scores=None, poured=None, jumps=None):
    """A build outcome's panelize half, with only the carrier parts stated."""
    return SimpleNamespace(holes=holes, panel_scores=scores, rail_copper=poured,
                           jumps=jumps)


class CarrierReportTest(unittest.TestCase):
    """One line per stage, and silence when the stage did not run."""

    def test_a_panel_with_no_carrier_stages_says_nothing_about_them(self):
        self.assertEqual(panelbuild.carrier_lines(made_with()), [])

    def test_the_holes_are_counted_measured_and_placed(self):
        holes = SimpleNamespace(
            count=4, diameter_mm=3.175, placement="rail_corner_center",
            holes=[SimpleNamespace(clearance_mm=1.25),
                   SimpleNamespace(clearance_mm=0.91)],
            skipped=(),
        )
        lines = panelbuild.carrier_lines(made_with(holes=holes))
        self.assertIn("4 tooling holes of 3.175 mm", lines[0])
        self.assertIn("the middle of each rail corner", lines[0])
        self.assertIn("0.91 mm clear", lines[1])

    def test_holes_nothing_measured_say_only_what_is_known(self):
        holes = SimpleNamespace(
            count=1, diameter_mm=3.0, placement="fixed_inset",
            holes=[SimpleNamespace(clearance_mm=None)],
            skipped=(),
        )
        lines = panelbuild.carrier_lines(made_with(holes=holes))
        self.assertEqual(len(lines), 1)
        self.assertIn("a fixed inset", lines[0])

    def test_a_corner_with_no_rail_material_says_so_by_name(self):
        # THE ZERO RAIL: a corner that could not be drilled is a
        # sentence in the report, not a hole quietly missing from the picture.
        holes = SimpleNamespace(
            count=2, diameter_mm=3.175, placement="rail_corner_center",
            holes=[SimpleNamespace(clearance_mm=3.4)],
            skipped=(SimpleNamespace(
                corner="top_left",
                reason=("no rail material stands in the top left corner: the "
                        "left rail is 0 mm wide"),
            ),),
        )
        lines = panelbuild.carrier_lines(made_with(holes=holes))
        self.assertIn(
            "No rail material stands in the top left corner: the left rail is "
            "0 mm wide.",
            lines,
        )

    def test_the_scores_name_the_datum_and_their_keep_outs(self):
        scores = SimpleNamespace(count=2, inherited_lines=1, half_width_mm=0.3,
                                 rule_areas=(1, 2, 3), protection_only=False,
                                 inherited_guarded=0, ignored=(),
                                 shared_lines=())
        lines = panelbuild.carrier_lines(made_with(scores=scores))
        self.assertIn("2 saw passes across the whole panel", lines[0])
        self.assertIn("blank's top-left corner", lines[0])
        self.assertIn("1 merged from the board edges", lines[0])
        self.assertIn("3 keep-out areas keep copper 0.3 mm clear", lines[1])

    def test_a_line_the_boards_guard_is_said_rather_than_counted_twice(self):
        # The panel adds nothing over a line its boards already guard, so a
        # small count of its own is not a small amount of protection - and
        # the report says which.
        scores = SimpleNamespace(count=0, inherited_lines=2, half_width_mm=0.3,
                                 rule_areas=(1,), protection_only=True,
                                 inherited_guarded=1, ignored=(),
                                 shared_lines=())
        lines = panelbuild.carrier_lines(made_with(scores=scores))
        self.assertIn(
            "1 saw line is guarded end to end by the keep-out the boards "
            "decided.", lines[2],
        )

    def test_keep_outs_with_no_scores_section_say_nobody_asked(self):
        # With the scores stage OFF, score lines merged from the boards are
        # still guarded: the protection is not on that switch, and the report
        # says which of the two things happened.
        scores = SimpleNamespace(count=0, inherited_lines=2, half_width_mm=0.3,
                                 rule_areas=(1, 2), protection_only=True,
                                 inherited_guarded=0, ignored=(),
                                 shared_lines=())
        lines = panelbuild.carrier_lines(made_with(scores=scores))
        self.assertIn("No panel saw pass was asked for", lines[0])
        self.assertIn("2 passes merged from the board edges are guarded",
                      lines[0])
        self.assertIn("2 keep-out areas keep copper 0.3 mm clear", lines[1])

    def test_an_unfilled_pour_says_why_it_is_empty(self):
        poured = SimpleNamespace(count=4, layers=("F.Cu", "B.Cu"),
                                 clearance_mm=0.635, pour_between_boards=False,
                                 refill=None)
        lines = panelbuild.carrier_lines(made_with(poured=poured))
        self.assertIn("4 copper pours over the rails", lines[0])
        self.assertIn("2 copper layers", lines[0])
        # The fill lands BEHIND the picture, so the sentence says what
        # happens next.
        self.assertIn("only KiCad can fill them", lines[1])
        self.assertIn("drawn again when that copper lands", lines[1])

    def test_a_filled_pour_says_what_it_cost(self):
        poured = SimpleNamespace(
            count=4, layers=("F.Cu",), clearance_mm=0.635,
            pour_between_boards=True,
            refill=SimpleNamespace(zones_filled=4, records=12, seconds=9.7),
        )
        lines = panelbuild.carrier_lines(made_with(poured=poured))
        self.assertIn("the rails and the gaps between boards", lines[0])
        self.assertIn("4 pours carry copper", lines[1])
        self.assertIn("9.7 s", lines[1])


class CarrierReportOnABuildTest(unittest.TestCase):
    """The same lines, off a real build driven through the page's own switches."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.session.build_panel({})
        for name in ALL_GROUPS:
            cls.session.set_param({name: True})
        cls.session.set_param({"score_inter_column": True})
        cls.report = list(cls.session.panel.report)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def said(self, fragment: str) -> bool:
        return any(fragment in line for line in self.report)

    def test_every_stage_that_ran_is_in_the_report(self):
        for fragment in ("tooling holes", "saw pass", "keep-out",
                         "copper pour"):
            with self.subTest(fragment=fragment):
                self.assertTrue(self.said(fragment), self.report)

    def test_the_pour_says_it_is_empty_until_kicad_has_filled_it(self):
        self.assertTrue(self.said("only KiCad can fill them"))

    def test_the_score_datum_is_stated_where_the_number_is_reported(self):
        self.assertTrue(self.said("blank's top-left corner"))

    def test_the_panel_that_follows_carries_what_was_switched_on(self):
        made = self.session.panel.outcome.panelize
        self.assertEqual(made.holes.count, 4)
        self.assertEqual(made.panel_scores.count, 1)
        self.assertGreater(made.rail_copper.count, 0)
        self.assertIsNone(made.rail_copper.refill)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


class CarrierPageTest(unittest.TestCase):
    """The three groups, read off the bytes the panel page serves."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.session.build_panel({})
        cls.markup = cls.session.panel_page()
        for name in ALL_GROUPS:
            cls.session.set_param({name: True})
        cls.session.set_param({"hole_placement": "fixed_inset"})
        cls.on = cls.session.panel_page()

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_every_group_is_on_the_page_with_its_own_switch(self):
        # The page names the rail pour COPPER FILL and the score group
        # V-SCORES; the config keys under either did not move.
        for token in ("pb-holes-on", "pb-copper-on", "pb-scores-on",
                      "Tooling Holes", "Copper Fill", "V-Scores"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_every_control_the_reference_names_is_there(self):
        for token in ("pb-hole-d", "pb-hole-tl", "pb-hole-tr", "pb-hole-br",
                      "pb-hole-bl", "pb-hole-place", "pb-hole-x", "pb-hole-y",
                      "pb-copper-clear", "pb-copper-between",
                      "pb-score-inter-col", "pb-score-inter-row",
                      "pb-score-rails-h", "pb-score-rails-v",
                      "pb-score-jump-h", "pb-score-jump-v"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_placement_dropdown_offers_the_two_ways_and_no_more(self):
        # Read off THAT SELECT, not off the page: the Layout group grew
        # dropdowns of its own with the placement suite, and this fence is
        # about the hole placement's two ways.
        select = re.search(
            r"<select id=pb-hole-place.*?</select>", self.markup, re.S
        )
        self.assertIsNotNone(select)
        options = re.findall(r"<option value=\"([^\"]+)\"", select.group(0))
        self.assertEqual(options, ["rail_corner_center", "fixed_inset"])

    def test_the_scores_are_four_checkboxes_and_no_typed_position(self):
        # Four switches, no typed position. The page has no text box for one,
        # and each switch says what it cuts rather than where.
        self.assertNotIn("<textarea", self.markup)
        for said in ("One pass down the middle of each gap between two "
                     "board columns.",
                     "One pass across the middle of each gap between two "
                     "board rows.",
                     "One pass along the inner edge of the top and the "
                     "bottom rail.",
                     "One pass along the inner edge of the left and the "
                     "right rail."):
            with self.subTest(said=said):
                self.assertIn(said, self.markup)

    def test_the_gui_carries_no_note_about_where_the_position_comes_from(self):
        # A control carries a name and one short helper line, not a paragraph
        # about the mechanism. The datum sentence lives in the RUN REPORT
        # (`CarrierReportOnABuildTest`), not on this group.
        self.assertNotIn("position is computed from the layout", self.markup)

    def test_the_page_says_when_a_pour_fills(self):
        self.assertIn("KiCad fills the pours behind the picture", self.markup)
        self.assertIn("a save waits for a fill still in flight", self.markup)

    def test_a_group_is_greyed_until_its_switch_is_on(self):
        # A stage that is off is served GREYED, not hidden, and the script
        # updates it from what the CONFIG says, not from a click alone.
        for group in ("pb-holes-body", "pb-copper-body", "pb-scores-body"):
            with self.subTest(group=group):
                self.assertIn(f'<div class="dependent off" id={group}>',
                              self.markup)
        self.assertIn("function drawGroups()", self.markup)
        self.assertIn("setDependent(body, carrier && on);", self.markup)

    def test_the_insets_are_revealed_only_by_the_fixed_inset_mode(self):
        self.assertIn('<div class="dependent off" id=pb-hole-insets>',
                      self.markup)
        self.assertIn("setDependent(insets, carrier", self.markup)
        self.assertIn(
            "PB.params.hole_placement === 'fixed_inset'", self.markup
        )

    def test_the_state_the_page_draws_from_carries_every_new_value(self):
        state = json.loads(
            re.search(r"let PB = (\{.*?\});\n", self.on, re.S).group(1)
        )
        params = state["params"]
        for name in (*ALL_GROUPS, "hole_diameter_mm", "hole_placement",
                     "score_inter_column", "score_rails_horizontal",
                     "rail_copper_clearance_mm"):
            with self.subTest(param=name):
                self.assertIn(name, params)
        self.assertTrue(params["tooling_holes_on"])
        self.assertEqual(params["hole_placement"], "fixed_inset")

    def test_the_save_button_says_why_it_is_slow_when_a_pour_is_on(self):
        # The popup reads the fragment's own answer: a fill still in flight is
        # a wait, and copper already in is a plain write.
        self.assertIn("waiting for KiCad to finish filling the rail copper",
                      self.markup)
        self.assertIn("PB.railCopperFilled", self.markup)

    def test_no_group_shows_a_bare_layer_name_or_the_retired_word(self):
        for markup in (self.markup, self.on):
            with self.subTest(page=markup[:40]):
                self.assertEqual(naming.violations(markup), [])
                self.assertEqual(naming.retired_words(markup), [])

    def test_the_report_on_the_page_lists_what_the_stages_did(self):
        self.assertIn("tooling holes", self.on)
        self.assertIn("copper pour", self.on)


class NoCarrierGroupsTest(unittest.TestCase):
    """A config that does not panelize still shows every carrier group.

    All five carrier groups are always on tab 2, so the settings can be found
    before there is a panel to judge them against. A config with no carrier
    serves the whole set - greyed by the script the instant it knows there is
    nothing to switch on - rather than the groups vanishing outright.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.session = a_session(
            self.folder, write_panel_project(self.folder, panelize=False)
        )

    def tearDown(self):
        self._folder.cleanup()

    def test_the_groups_are_present_and_the_rail_field_says_why_they_are_idle(
        self,
    ):
        markup = self.session.panel_page()
        column = controls_of(markup)
        for token in ("pb-holes-on", "pb-copper-on", "pb-scores-on",
                      "pb-hole-place"):
            with self.subTest(token=token):
                self.assertIn(token, column)
        self.assertNotIn("<textarea", column)
        self.assertIn("This config has no carrier", column)


CARRIER_HARNESS = r"""
%(tables)s
%(functions)s
const out = {};
const NODES = {};
function node(id, options) {
  let held = '';
  // `dataset`, `disabled` and `options` are modelled because the page uses all
  // three: `data-off` is the shared half's intrinsic-disabled idiom, and a
  // numeric dropdown is written by matching its own option values.
  // `querySelectorAll` is modelled empty because `setDependent` calls it to
  // disable a block's own controls, and this harness disables by reading
  // `classList` directly instead.
  const el = {id: id, checked: false, textContent: '',
              disabled: false, dataset: {},
              options: (options || []).map(v => ({value: v})),
              addEventListener() {},
              querySelectorAll: () => [],
              classList: {states: {},
                          toggle(name, state) { this.states[name] = !!state; },
                          contains(name) { return !!this.states[name]; }}};
  Object.defineProperty(el, 'value', {
    get() { return held; },
    set(next) { held = String(next); },
  });
  NODES[id] = el;
  return el;
}
['pb-columns', 'pb-rows', 'pb-gapx', 'pb-gapy', 'pb-rail', 'pb-rail-top',
 'pb-rail-bottom', 'pb-rail-left', 'pb-rail-right', 'pb-rail-unlock',
 'pb-persides', 'pb-rail-linked', 'pb-holes-on', 'pb-hole-d', 'pb-hole-tl',
 'pb-hole-tr', 'pb-hole-br', 'pb-hole-bl', 'pb-hole-place', 'pb-hole-x',
 'pb-hole-y', 'pb-hole-insets', 'pb-copper-on', 'pb-copper-clear',
 'pb-copper-between', 'pb-scores-on',
 'pb-score-rails-h', 'pb-score-rails-v',
 'pb-score-inter-row', 'pb-score-inter-col',
 'pb-holes-body', 'pb-copper-body', 'pb-scores-body',
 // THE PLACEMENT SUITE's own controls.
 'pb-nestx', 'pb-nesty', 'pb-clearance', 'pb-alternate',
 'pb-placement', 'pb-placed', 'pb-placed-clear'].forEach(id => node(id));
node('pb-rotation', ['0', '90', '180', '270']);
globalThis.document = {
  getElementById: id => NODES[id] || null,
  activeElement: null,
};
const unlockEl = NODES['pb-rail-unlock'];
const persidesEl = NODES['pb-persides'];
const linkedEl = NODES['pb-rail-linked'];
let PB = {params: {}};
// A stage that is off wears `classList` `off` (greyed), not `.hidden`.
function greyed() {
  return {holes: NODES['pb-holes-body'].classList.contains('off'),
          copper: NODES['pb-copper-body'].classList.contains('off'),
          scores: NODES['pb-scores-body'].classList.contains('off'),
          insets: NODES['pb-hole-insets'].classList.contains('off')};
}

// -- every stage off, which is a config that never heard of them -----------
drawPanelParams(%(off)s);
out.offGreyed = greyed();
out.offLive = livePanelParams(PB.params);
// A stage that is off must not post its fields, however they are filled in.
NODES['pb-hole-d'].value = '9';
NODES['pb-score-inter-col'].checked = true;
out.offChanged = changedPanelParams();

// -- every stage on, holes on a fixed inset --------------------------------
drawPanelParams(%(on)s);
out.onGreyed = greyed();
out.onLive = livePanelParams(PB.params);
out.filled = {
  diameter: NODES['pb-hole-d'].value,
  topLeft: NODES['pb-hole-tl'].checked,
  bottomLeft: NODES['pb-hole-bl'].checked,
  placement: NODES['pb-hole-place'].value,
  interColumn: NODES['pb-score-inter-col'].checked,
  interRow: NODES['pb-score-inter-row'].checked,
  railsHorizontal: NODES['pb-score-rails-h'].checked,
  railsVertical: NODES['pb-score-rails-v'].checked,
  between: NODES['pb-copper-between'].checked,
};
out.quiet = changedPanelParams();

// Two switches moved: one score family on, one hole corner off.
NODES['pb-score-rails-h'].checked = true;
NODES['pb-hole-tl'].checked = false;
out.changed = changedPanelParams();

// -- the insets, which exist only in fixed-inset mode ----------------------
drawPanelParams(%(corner)s);
out.cornerGreyed = greyed();
out.cornerLive = livePanelParams(PB.params);
NODES['pb-hole-x'].value = '77';
out.cornerChanged = changedPanelParams();
console.log(JSON.stringify(out));
"""

OFF_PARAMS = {
    "columns": 2, "rows": 1, "gap_x_mm": 3.0, "gap_y_mm": 3.0,
    "rail_mm": 5.0, "rail_top_mm": 5.0, "rail_bottom_mm": 5.0,
    "rail_left_mm": 5.0, "rail_right_mm": 5.0, "rails_linked": True,
    "tooling_holes_on": False, "rail_copper_on": False, "scores_on": False,
    "hole_diameter_mm": 3.175, "hole_top_left": True, "hole_top_right": True,
    "hole_bottom_right": True, "hole_bottom_left": True,
    "hole_placement": "rail_corner_center", "hole_inset_across_mm": 5.0,
    "hole_inset_down_mm": 5.0, "score_inter_column": False,
    "score_inter_row": False, "score_rails_horizontal": False,
    "score_rails_vertical": False,
    "score_rule_half_width_mm": 0.3,
    "rail_copper_clearance_mm": 0.635, "rail_copper_between": False,
}
ON_PARAMS = {
    **OFF_PARAMS,
    "tooling_holes_on": True, "rail_copper_on": True, "scores_on": True,
    "hole_placement": "fixed_inset", "hole_bottom_left": False,
    "score_inter_column": True, "score_inter_row": True,
    "rail_copper_between": True,
}
CORNER_PARAMS = {**ON_PARAMS, "hole_placement": "rail_corner_center"}


@unittest.skipUnless(__import__("shutil").which("node"),
                     "no node to run the page's own script")
class ClientCarrierGroupsTest(unittest.TestCase):
    """The group wiring, RUN: what is revealed, what is filled, what is posted.

    A string assertion cannot tell "wired" from "wired wrongly", so the page's
    own functions are lifted out and run against a DOM small enough to state -
    the same method the rails and the clickable captions are tested by.
    """

    @classmethod
    def setUpClass(cls):
        from test_panel_page import js_slice
        from test_preview_journal import js_function, run_js

        cls.out = run_js(CARRIER_HARNESS % {
            "tables": js_slice(page_panel.PANEL_JS, "const LAYOUT_FIELDS = [",
                               "COPPER_FIELDS, SCORE_FIELDS, DIMENSION_FIELDS, TEXT_FIELDS\n);"),
            "functions": "\n".join(
                [
                    js_function(page_panel.PANEL_JS, name)
                    for name in ("field", "hasCarrier", "railsLinked",
                                 "livePanelParams",
                                 "liveFields", "readField",
                                 "writeField", "drawRailMode",
                                 # The fiducials group's own two: the heading
                                 # line that says where the footprint list came
                                 # from, and the name dropdown's writer.
                                 "drawGroups", "drawFiducialLibrary",
                                 "writeName", "writeChoiceNumber",
                                 "drawPlacementMode", "drawPanelParams",
                                 "changedPanelParams")
                ]
                # `setDependent` is the shared half's: the same function greys
                # a stage's body on both pages.
                + [js_function(page_served.CONTROLS_JS, "setDependent")]
            ),
            "off": json.dumps(OFF_PARAMS),
            "on": json.dumps(ON_PARAMS),
            "corner": json.dumps(CORNER_PARAMS),
        })

    def test_a_stage_that_is_off_greys_its_fields(self):
        # A stage that is off wears `.off` (greyed), not `.hidden`.
        self.assertEqual(self.out["offGreyed"],
                         {"holes": True, "copper": True, "scores": True,
                          "insets": True})

    def test_a_stage_that_is_off_posts_nothing_however_it_is_filled_in(self):
        # THE TRAP: a number left over in a greyed group would arrive on the
        # next Enter and turn a stage on that nobody switched on.
        self.assertEqual(self.out["offChanged"], {})
        for name in ("hole_diameter_mm", "score_inter_column"):
            self.assertNotIn(name, self.out["offLive"])
        self.assertIn("tooling_holes_on", self.out["offLive"])

    def test_switching_a_stage_on_reveals_exactly_its_own_group(self):
        self.assertEqual(self.out["onGreyed"],
                         {"holes": False, "copper": False, "scores": False,
                          "insets": False})

    def test_every_kind_of_control_is_filled_from_the_config(self):
        # A SWITCH THAT IS OFF IS STILL WRITTEN: `false` is a value the config
        # states, and a box left ticked from a previous panel would be the page
        # lying about the file.
        self.assertEqual(self.out["filled"], {
            "diameter": "3.175", "topLeft": True, "bottomLeft": False,
            "placement": "fixed_inset", "interColumn": True,
            "interRow": True, "railsHorizontal": False,
            "railsVertical": False, "between": True,
        })

    def test_a_page_filled_from_the_config_has_nothing_to_post(self):
        self.assertEqual(self.out["quiet"], {})

    def test_a_score_switch_posts_the_switch_and_nothing_else(self):
        self.assertEqual(self.out["changed"],
                         {"score_rails_horizontal": True,
                          "hole_top_left": False})

    def test_the_insets_go_with_the_mode_that_uses_them(self):
        self.assertTrue(self.out["cornerGreyed"]["insets"])
        self.assertFalse(self.out["cornerGreyed"]["holes"])
        self.assertNotIn("hole_inset_across_mm", self.out["cornerLive"])
        self.assertEqual(self.out["cornerChanged"], {})


if __name__ == "__main__":
    unittest.main()
