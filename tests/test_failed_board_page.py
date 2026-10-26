"""A board that will not prepare costs the viewport and nothing else.

A failed board load affects only the board view area. Everything else in the
GUI still comes up, so the user can reach the 'report a problem' button and
adjust the heal settings.

What is pinned here, on the BYTES the server sends:

**The page comes up.** The header, the report button, both step links, the
layer panel and every setting on step 1 are in the markup of a page whose board
could not be prepared. No whole-page notice replaces them -
`page_served.error_page` does not exist in the module.

**The failure is drawn where the board would have been.** One card, carrying
the reason in plain words and the coordinate the stage named, and a control that
tries again. No traceback anywhere near it.

**The settings are live, not decoration.** The fields carry what the config
holds, a change to one is accepted, and the change recomputes - which is the
retry, and is why a person can fix the number that caused the failure without
restarting the tool.

**The problem report works.** The zip builds over a failed load and carries the
reason and the coordinate, because that is the one report a stuck engineer
sends.

**Step 2 stands.** A failed board leaves the panel page exactly as it was, with
its own sentence about there being no panel.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from eedesignpanels.preview import bugreport, page_served
from eedesignpanels.preview.session import PreviewSession
from test_preview import stub_render, write_project
from test_preview_journal import open_session, shot_payload
from test_vscore import scoreable_board

# The one thing the stage refuses: an outline that does not close. Two of the
# fixture's four edges, so the walk runs out of material at a corner and names
# it - which gives this file both a reason and a coordinate to assert on.
BROKEN_AT = (10.0, 10.0)
# The card itself, which is in the markup only when there is no board to draw.
# The id of its reason line is not the fence: the script that keeps that line
# true is on every page, prepared or not.
FAILED_CARD = 'class="card failed"'


def broken_project(folder: Path) -> Path:
    """A project whose board cannot be prepared, written to *folder*."""
    board = scoreable_board()
    board.drawings = board.drawings[:2]
    return write_project(folder, board)


class FailureWordsTest(unittest.TestCase):
    """The reason, in words a non-programmer reads once."""

    def test_the_coordinate_comes_off_the_sentence_and_onto_its_own_line(self):
        words = page_served.failure_words(
            "the outline does not continue - nothing joins here at "
            "(10.0000, 10.0000) mm",
            BROKEN_AT,
        )
        self.assertEqual(words["where"], "10.0000, 10.0000 mm")
        self.assertTrue(words["reason"].endswith("nothing joins here"))
        self.assertNotIn("(10.0000", words["reason"])

    def test_a_failure_that_named_no_place_keeps_its_whole_sentence(self):
        words = page_served.failure_words("kicad-cli is not installed")
        self.assertEqual(words["where"], "")
        self.assertEqual(words["reason"], "kicad-cli is not installed")

    def test_a_reason_nobody_recorded_still_reads_as_a_sentence(self):
        words = page_served.failure_words("")
        self.assertEqual(words["reason"], "the reason was not recorded")

    def test_the_place_is_millimetres_or_says_why_not(self):
        self.assertEqual(bugreport.failure_place([1.5, -2.25]),
                         "1.5000, -2.2500 mm")
        self.assertEqual(bugreport.failure_place(None),
                         "no coordinate was named")
        self.assertIn("unreadable", bugreport.failure_place(["left", "a bit"]))


class FailedViewportTest(unittest.TestCase):
    """The card, on its own: what it says and what it does not carry."""

    def setUp(self):
        self.markup = page_served.failed_viewport(
            "the outline does not continue at (10.0000, 10.0000) mm",
            BROKEN_AT,
        )

    def test_it_is_a_card_with_a_viewport_and_no_plot_in_it(self):
        # The shared viewport wiring walks every `.card` and steps over one
        # with no SVG under it, which is what keeps this card inert.
        self.assertIn("class=\"card failed\"", self.markup)
        self.assertIn("class=viewport", self.markup)
        self.assertNotIn("<svg", self.markup)

    def test_it_states_what_failed_where_and_what_to_try(self):
        self.assertIn("10.0000, 10.0000 mm", self.markup)
        self.assertIn("does not continue", self.markup)
        for line in page_served.FAILURE_ADVICE:
            with self.subTest(line=line):
                self.assertIn(line, self.markup)

    def test_it_carries_a_control_that_tries_again(self):
        self.assertIn("pb-failed-again", self.markup)

    def test_no_traceback_ever_reaches_it(self):
        for word in ("Traceback", "File \"", "raise ", "Error:"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.markup)


class FailedPageTest(unittest.TestCase):
    """The served bytes of step 1 over a board that could not be prepared."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = broken_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()
        self.assertTrue(self.session.error)
        self.markup = self.session.page()

    def tearDown(self):
        self._folder.cleanup()

    def test_the_whole_page_notice_is_gone_from_the_module(self):
        self.assertFalse(hasattr(page_served, "error_page"))

    def test_the_header_and_the_report_button_come_up(self):
        self.assertIn("pb-report-btn", self.markup)
        self.assertIn(page_served.REPORT_LABEL, self.markup)
        self.assertIn("class=titlebar", self.markup)

    def test_both_step_links_come_up(self):
        self.assertIn(page_served.PREP_STEP.label, self.markup)
        self.assertIn(page_served.PANEL_STEP.label, self.markup)

    def test_every_setting_on_step_one_comes_up(self):
        for field in ("pb-cutter", "pb-past", "pb-scorew", "pb-mintab",
                      "pb-elim", "pb-apply", "pb-save", "pb-reload"):
            with self.subTest(field=field):
                self.assertIn(field, self.markup)

    def test_the_settings_carry_the_values_the_config_holds(self):
        # The fields are filled by the script from this block, so the block is
        # what a fence can read off the bytes.
        self.assertIn(
            f'"cutter_diameter": {self.session.config.prep.cutter_diameter}',
            self.markup,
        )
        self.assertIn('"ok": false', self.markup)

    def test_the_layer_panel_comes_up_over_a_board_that_loaded(self):
        # Prep failed, the file still parsed, so there is a layer table to show
        # and switching a layer off here still reaches step 2.
        self.assertIn("layerPanel('lp-viewer'", self.markup)
        self.assertIn("class=lp-row", self.markup)

    def test_the_failure_is_drawn_in_the_viewport(self):
        self.assertIn(FAILED_CARD, self.markup)
        self.assertIn("pb-failed-again", self.markup)
        self.assertIn("10.0000, 10.0000 mm", self.markup)

    def test_the_session_holds_the_coordinate_as_numbers(self):
        self.assertEqual(self.session.error_at, BROKEN_AT)
        self.assertEqual(self.session.state()["error_at"], list(BROKEN_AT))

    def test_step_two_still_stands(self):
        panel = self.session.panel_page()
        self.assertIn("pb-report-btn", panel)
        self.assertIn(page_served.PREP_STEP.label, panel)
        self.assertIn(self.session.panel_error[:30], panel)

    def test_a_prepared_board_still_gets_its_plot_and_no_failed_card(self):
        elsewhere = self.folder / "good"
        elsewhere.mkdir(exist_ok=True)
        good = PreviewSession(write_project(elsewhere),
                              self.folder / "work2", render=stub_render)
        good.refresh()
        markup = good.page()
        self.assertEqual(good.error, "")
        self.assertIn("<svg", markup)
        # The card, not the handler that redraws it: the script that keeps a
        # failed viewport true is on every page, failed or not.
        self.assertNotIn(FAILED_CARD, markup)
        self.assertIn('"ok": true', markup)


class LiveSettingsTest(unittest.TestCase):
    """A number changed on a failed page is accepted, and it retries."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = broken_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self._folder.cleanup()

    def test_the_fragment_carries_the_settings_with_no_picture_to_draw(self):
        fragment = self.session.fragment()
        self.assertFalse(fragment["ok"])
        self.assertEqual(fragment["error_at"], list(BROKEN_AT))
        self.assertEqual(fragment["params"]["cutter_diameter"],
                         self.session.config.prep.cutter_diameter)

    def test_a_changed_cutter_diameter_is_taken_and_recomputed(self):
        before = self.session.generation
        reply = self.session.set_param({"cutter_diameter": 0.6})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.session.config.prep.cutter_diameter, 0.6)
        self.assertEqual(self.session.data["prep"]["cutter_diameter"], 0.6)
        # The recompute IS the retry, and it is what carries a new reason.
        self.assertGreater(self.session.generation, before)
        self.assertEqual(self.session.fragment()["params"]["cutter_diameter"],
                         0.6)

    def test_the_page_still_comes_up_after_the_change(self):
        self.session.set_param({"cutter_diameter": 0.6})
        markup = self.session.page()
        self.assertIn("pb-report-btn", markup)
        self.assertIn('"cutter_diameter": 0.6', markup)

    def test_trying_again_over_a_board_still_broken_is_a_state(self):
        reply = self.session.reload_from_disk()
        self.assertTrue(reply["ok"])
        self.assertTrue(self.session.error)
        self.assertIn(FAILED_CARD, self.session.page())

    def test_trying_again_over_a_board_now_fixed_brings_the_plot_back(self):
        write_project(self.folder, scoreable_board())
        self.session.reload_from_disk()
        self.assertEqual(self.session.error, "")
        markup = self.session.page()
        self.assertIn("<svg", markup)
        self.assertNotIn(FAILED_CARD, markup)


class FailedReportTest(unittest.TestCase):
    """The one report a stuck engineer sends, and what it has to carry."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = broken_project(self.folder)
        self.recording = open_session(self.folder,
                                      config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()
        self.session.start_panel()
        self.reply = self.session.bug_report(shot_payload())

    def tearDown(self):
        self._folder.cleanup()

    def read(self, name: str) -> str:
        with zipfile.ZipFile(self.reply["file"]) as archive:
            return archive.read(name).decode("utf-8")

    def test_the_report_builds_at_all(self):
        self.assertTrue(self.reply["ok"])
        self.assertTrue(Path(self.reply["file"]).is_file())

    def test_the_reason_and_the_coordinate_are_in_the_report_text(self):
        text = self.read("reports/prep_warnings.txt")
        self.assertIn("prepared: no", text)
        self.assertIn("does not continue", text)
        self.assertIn("10.0000, 10.0000 mm", text)

    def test_the_manifest_says_the_board_never_prepared(self):
        body = json.loads(self.read(bugreport.MANIFEST_NAME))
        self.assertFalse(body["board"]["prepared"])
        self.assertEqual(body["board"]["error_at"], list(BROKEN_AT))
        self.assertIn("does not continue", body["board"]["error"])

    def test_the_backend_state_carries_the_place_as_numbers(self):
        state = json.loads(self.read(bugreport.BACKEND_STATE_NAME))
        self.assertEqual(state["results"]["error_at"], list(BROKEN_AT))


if __name__ == "__main__":
    unittest.main()
