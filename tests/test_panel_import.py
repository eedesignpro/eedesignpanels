"""Import settings from another project: what comes across, and what does not.

The import option pre-populates every setting from another project's file and
leaves each one editable.

Four subjects, and each one is a way this could quietly be wrong.

**Identity does not import.** `source_board` names THIS project's design, so it
is kept and the imported file's own path is dropped - asserted with a settings
file that names a board which does not exist, because that is the case where a
merge in the other direction would point the session at nothing. Every other
top-level key is replaced wholesale, absent ones included: after an import the
panel is exactly the panel the imported file describes, built over this
project's board.

**A settings file is untrusted input**, validated before use. It goes through
the same config model every typed number goes through, and a file that will not
parse or will not validate is refused with the reason and changes NOTHING - not
the held data, not the pending count, not the generation.

**An import is a set_param of every key at once.** It leaves one pending change,
writes no file, recalculates the panel, and recomputes prep only when a prep
number moved. The panel it leaves behind is compared with a FROM-SCRATCH build
of what the session now holds, byte for byte, which is the only form of "the
cache invalidated correctly" that means anything (`test_panel_cache`).

**The page says it in the same clothes as every other group** - one
collapsible group, a bold title, one helper line, a native picker - and the
picker's own handler is lifted out of the served page and RUN, because a string
assertion cannot tell "wired" from "wired wrongly".
"""

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from eedesigncore import Board

from eedesignpanels.config import CONFIG_REVISION
from eedesignpanels.preview import actions, page_panel, page_served
from eedesignpanels.preview import server as server_module
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages import step_and_repeat
from test_panel_page import a_session, controls_of, digest, write_panel_project
from test_preview_journal import js_function, run_js

# ANOTHER PROJECT'S `panel.json`, and every value in it differs from the
# fixture's. It names a board of its own - one that does not exist here - which
# is what makes "identity is kept" an assertion rather than a coincidence.
OTHER_PROJECT = {
    "revision": CONFIG_REVISION,
    "source_board": "../some-other-design.kicad_pcb",
    "stage": "build_panel",
    "grid": {"columns": 3, "rows": 2, "gap_mm": 3.0,
             "gap_x_mm": 4.0, "gap_y_mm": 5.0},
    "min_tab_mm": 0.4,
    "exclude_layers": ["User.41"],
    "prep": {
        "cutter_diameter": 1.0,
        "vscore": {"detect": "auto", "route_past_score_mm": 1.5,
                  "rule_half_width_mm": 0.4},
    },
    "panel": {
        "rails": {"top": 8.0, "bottom": 8.0, "left": 3.0, "right": 3.0},
        "tooling_holes": {"enabled": True, "diameter_mm": 3.4,
                          "placement": "rail_corner_center"},
        "scores": {"enabled": True, "inter_column": True, "inter_row": False,
                   "rails_horizontal": True, "rails_vertical": True},
        "dimensions": {"enabled": True, "layer": "User.16", "precision": 2},
    },
    "review": {"layers": "all"},
    "verify": {"enabled": True, "um_per_pixel": 4.0},
}

# The same file with nothing prep would read moved: only the carrier and the
# layout differ from the fixture, so an import of it must not pay for a prep
# recompute.
CARRIER_ONLY = {
    "revision": CONFIG_REVISION,
    "source_board": "../some-other-design.kicad_pcb",
    "stage": "build_panel",
    "grid": {"columns": 2, "rows": 1, "gap_mm": 3.0},
    "min_tab_mm": 0.5,
    "prep": {
        "cutter_diameter": 0.889,
        "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
    },
    "panel": {"rails": 5.0,
              "tooling_holes": {"enabled": True, "diameter_mm": 3.4}},
}


def settings_text(settings: dict) -> str:
    """A settings file as the browser would post it: the file's own text."""
    return json.dumps(settings, indent=2) + "\n"


def posted(settings: dict, name: str = "panel.json") -> dict:
    return {"name": name, "content": settings_text(settings)}


# ---------------------------------------------------------------------------
# the merge: what crosses, and what is refused
# ---------------------------------------------------------------------------


class MergeTest(unittest.TestCase):
    """`actions.import_settings`: the whole file in, one validated config out."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def do_import(self, settings, name: str = "panel.json"):
        return actions.import_settings(self.data, self.config_path,
                                       posted(settings, name))

    def test_this_project_keeps_its_own_board(self):
        imported = self.do_import(OTHER_PROJECT)
        self.assertEqual(imported.data["source_board"],
                         self.data["source_board"])
        self.assertEqual(imported.config.source_board_path,
                         Path(self.folder / "board.kicad_pcb").resolve())
        self.assertNotIn("source_board", imported.keys)

    def test_every_other_setting_comes_across(self):
        config = self.do_import(OTHER_PROJECT).config
        self.assertEqual(config.grid.columns, 3)
        self.assertEqual(config.grid.rows, 2)
        self.assertEqual(config.grid.gap_across_mm, 4.0)
        self.assertEqual(config.grid.gap_down_mm, 5.0)
        self.assertEqual(config.min_tab_mm, 0.4)
        self.assertEqual(config.exclude_layers, ["User.41"])
        self.assertEqual(config.prep.cutter_diameter, 1.0)
        self.assertEqual(config.prep.vscore.route_past_score_mm, 1.5)
        self.assertEqual(config.rail_widths, (8.0, 8.0, 3.0, 3.0))
        self.assertFalse(config.panel.rails_linked)
        self.assertEqual(config.panel.tooling_holes.diameter_mm, 3.4)
        self.assertTrue(config.panel.scores.inter_column)
        self.assertTrue(config.panel.scores.rails_horizontal)
        self.assertTrue(config.panel.scores.rails_vertical)
        self.assertEqual(config.panel.dimensions.precision, 2)
        self.assertEqual(config.verify.um_per_pixel, 4.0)

    def test_a_key_the_imported_file_does_not_carry_is_taken_away(self):
        # WHOLESALE, not a per-key merge: a panel described half by one project
        # and half by another is a panel nobody can check. `verify` is the key
        # under test because the fixture has none of its own, so the ONLY way it
        # could be present afterwards is a merge that kept something.
        lean = {key: value for key, value in OTHER_PROJECT.items()
                if key not in ("verify", "exclude_layers")}
        imported = self.do_import(lean)
        self.assertNotIn("verify", imported.data)
        self.assertNotIn("exclude_layers", imported.data)
        # ...and the model's own defaults are then what the config means.
        self.assertTrue(imported.config.verify.enabled)
        self.assertEqual(imported.config.verify.um_per_pixel, 5.0)

    def test_the_keys_it_moved_are_named_in_the_file_s_own_order(self):
        keys = self.do_import(OTHER_PROJECT).keys
        self.assertEqual(
            keys,
            ("grid", "min_tab_mm", "exclude_layers", "prep", "panel",
             "review", "verify"),
        )

    def test_a_file_that_says_what_this_config_says_changes_nothing(self):
        same = dict(self.data)
        same["source_board"] = "../elsewhere.kicad_pcb"
        imported = self.do_import(same)
        self.assertEqual(imported.keys, ())
        self.assertFalse(imported.changed)

    def test_a_prep_number_is_named_as_one(self):
        self.assertTrue(self.do_import(OTHER_PROJECT).prep_moved)
        self.assertFalse(self.do_import(CARRIER_ONLY).prep_moved)

    def test_the_imported_file_is_kept_for_the_journal(self):
        imported = self.do_import(OTHER_PROJECT)
        self.assertEqual(imported.settings, OTHER_PROJECT)

    # -- the refusals ---------------------------------------------------

    def refused(self, payload) -> str:
        before = json.dumps(self.data, sort_keys=True)
        with self.assertRaises(actions.ActionError) as caught:
            actions.import_settings(self.data, self.config_path, payload)
        # NOTHING WAS CHANGED, which is the half of a refusal that matters.
        self.assertEqual(json.dumps(self.data, sort_keys=True), before)
        return str(caught.exception)

    def test_a_file_that_is_not_json_is_refused_by_name(self):
        said = self.refused({"name": "broken.json", "content": "{not json,"})
        self.assertIn("broken.json", said)
        self.assertIn("not valid JSON", said)

    def test_a_file_that_is_not_an_object_is_refused(self):
        said = self.refused({"name": "list.json", "content": "[1, 2, 3]"})
        self.assertIn("not a panel config", said)
        self.assertIn("list.json", said)

    def test_an_empty_post_is_refused(self):
        self.assertIn("no settings arrived",
                      self.refused({"name": "panel.json", "content": "   "}))
        self.assertIn("no settings arrived", self.refused({"name": "x.json"}))
        self.assertIn("no settings arrived",
                      self.refused({"name": "x.json", "content": 12}))

    def test_a_body_that_is_not_an_object_is_refused(self):
        self.assertIn("must be a JSON object", self.refused(["panel.json"]))

    def test_settings_the_schema_refuses_are_refused_here(self):
        bad = json.loads(json.dumps(OTHER_PROJECT))
        bad["grid"]["columns"] = 0
        said = self.refused(posted(bad, "zero.json"))
        self.assertIn("zero.json", said)
        self.assertIn("was refused", said)
        self.assertIn("columns", said)

    def test_a_key_the_schema_never_heard_of_is_refused(self):
        bad = json.loads(json.dumps(OTHER_PROJECT))
        bad["panelise_it_all"] = True
        self.assertIn("panelise_it_all", self.refused(posted(bad)))

    def test_a_rotation_this_build_cannot_do_is_still_refused(self):
        # A QUARTER turn is what a panel copy may take, and the schema refuses
        # anything else by NAME rather than silently building a board KiCad
        # would tessellate. An import is one more way in.
        bad = json.loads(json.dumps(OTHER_PROJECT))
        bad["grid"]["rotation_deg"] = 45.0
        self.assertIn("rotation_deg", self.refused(posted(bad)))

    def test_a_quarter_turn_imports(self):
        turned = json.loads(json.dumps(OTHER_PROJECT))
        turned["grid"]["rotation_deg"] = 90.0
        self.assertEqual(self.do_import(turned).config.grid.rotation_deg, 90.0)

    def test_a_minimum_tab_under_the_fab_floor_is_refused(self):
        bad = json.loads(json.dumps(OTHER_PROJECT))
        bad["min_tab_mm"] = 0.01
        self.assertIn("min_tab_mm", self.refused(posted(bad)))

    def test_a_settings_file_carrying_config_path_is_refused(self):
        bad = json.loads(json.dumps(OTHER_PROJECT))
        bad["config_path"] = "C:/somewhere/panel.json"
        self.assertIn("config_path", self.refused(posted(bad)))

    # -- ANOTHER REVISION --------------------------------------------------
    # An imported file is a config arriving over a socket, so it is judged the
    # way a launch judges one: by revision, in a sentence that names the file's
    # and this tool's, and never by naming a field.

    def test_settings_from_an_older_revision_are_refused_as_older(self):
        old = json.loads(json.dumps(OTHER_PROJECT))
        old["revision"] = CONFIG_REVISION - 1
        said = self.refused(posted(old, "lastweek.json"))
        self.assertIn("lastweek.json", said)
        self.assertIn("older version", said)
        self.assertIn("Nothing was changed", said)

    def test_settings_from_a_newer_revision_say_so(self):
        newer = json.loads(json.dumps(OTHER_PROJECT))
        newer["revision"] = CONFIG_REVISION + 1
        said = self.refused(posted(newer, "colleague.json"))
        self.assertIn("newer version", said)
        self.assertNotIn("invalid", said)

    def test_settings_with_no_revision_are_refused_as_older(self):
        none = {key: value for key, value in OTHER_PROJECT.items()
                if key != "revision"}
        self.assertIn("no revision number", self.refused(posted(none)))

    def test_settings_with_no_prep_section_are_refused(self):
        # A launch refuses one of these too (`preview.__main__`): with no
        # declared cutter diameter this session could not prepare the board, and
        # step 1 would go dark on an import nobody could undo.
        bad = {key: value for key, value in OTHER_PROJECT.items()
               if key != "prep"}
        said = self.refused(posted(bad, "noprep.json"))
        self.assertIn("noprep.json", said)
        self.assertIn("prep", said)
        self.assertIn("cutter diameter", said)


class ImportNameTest(unittest.TestCase):
    """The posted name is DISPLAYED and never opened. It is a name, not a path."""

    def test_a_browser_fakepath_comes_out_as_the_file_name(self):
        self.assertEqual(actions.import_name("C:\\fakepath\\panel.json"),
                         "panel.json")
        self.assertEqual(actions.import_name("/home/x/y/panel.json"),
                         "panel.json")

    def test_control_characters_are_dropped(self):
        self.assertEqual(actions.import_name("pa\nnel\t.json"), "panel.json")

    def test_a_long_name_is_cut_to_something_printable(self):
        name = actions.import_name("a" * 400 + ".json")
        self.assertEqual(len(name), actions.MAX_IMPORT_NAME_CHARS)

    def test_no_name_at_all_still_reads_as_a_sentence(self):
        for value in (None, "", "   ", 7, {}):
            with self.subTest(value=value):
                self.assertEqual(actions.import_name(value),
                                 "the imported file")


# ---------------------------------------------------------------------------
# the session: pending, recalculation, and the bytes
# ---------------------------------------------------------------------------


class SessionImportTest(unittest.TestCase):
    """The endpoint, driven headless: one pending change and nothing written."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.board_path = self.folder / "board.kicad_pcb"
        self.session = a_session(self.folder, self.config_path)
        self.session.start_panel()
        self.assertEqual(self.session.panel_error, "")
        self.before = (digest(self.config_path), digest(self.board_path))

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def assert_nothing_written(self):
        self.assertEqual((digest(self.config_path), digest(self.board_path)),
                         self.before)

    def assert_not_stale(self, what: str):
        """The held panel is the panel a from-scratch build writes. Bytes."""
        self.assertEqual(self.session.panel_error, "", what)
        board = Board.from_string(actions.board_for_save(
            self.session.board, self.session.data).to_string())
        fresh = step_and_repeat.build_from(
            self.session.config, board, fill_rails=False
        ).board.to_string()
        self.assertEqual(self.session.panel.outcome.board.to_string(), fresh,
                         f"the panel after {what} is not what a build writes")

    def test_a_full_import_lands_every_setting_in_the_held_config(self):
        reply = self.session.import_settings(posted(OTHER_PROJECT))
        self.assertTrue(reply["ok"])
        self.assertEqual(self.session.error, "")
        config = self.session.config
        self.assertEqual((config.grid.columns, config.grid.rows), (3, 2))
        self.assertEqual(config.rail_widths, (8.0, 8.0, 3.0, 3.0))
        self.assertEqual(config.min_tab_mm, 0.4)
        self.assertEqual(config.prep.cutter_diameter, 1.0)
        self.assertEqual(
            self.session.data["panel"]["scores"]["rails_horizontal"], True
        )

    def test_the_import_says_which_file_and_which_settings(self):
        reply = self.session.import_settings(
            posted(OTHER_PROJECT, "C:\\fakepath\\other-panel.json")
        )
        self.assertIn("other-panel.json", reply["message"])
        self.assertIn("grid", reply["message"])
        self.assertNotIn("fakepath", reply["message"])

    def test_this_project_keeps_its_own_board(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.assertEqual(self.session.data["source_board"],
                         "../board.kicad_pcb")
        self.assertEqual(self.session.config.source_board_path,
                         self.board_path.resolve())

    def test_it_is_one_pending_change_and_no_file_is_touched(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.assertEqual(self.session.pending_count, 1)
        self.assertEqual(self.session.pending_config, 1)
        self.assertEqual(self.session.pending_board, 0)
        self.assert_nothing_written()
        self.assertIn("imported from panel.json",
                      self.session.pending[0].message)

    def test_the_save_is_what_writes_it(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.session.save()
        self.assertEqual(self.session.pending_count, 0)
        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["source_board"], "../board.kicad_pcb")
        self.assertEqual(written["grid"]["columns"], 3)
        self.assertEqual(written["panel"]["rails"],
                         {"top": 8.0, "bottom": 8.0, "left": 3.0,
                          "right": 3.0})

    def test_both_pages_redraw_every_field_from_the_import(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        panel = self.session.panel_fragment()["params"]
        self.assertEqual(panel["columns"], 3)
        self.assertEqual(panel["rows"], 2)
        self.assertEqual(panel["gap_x_mm"], 4.0)
        self.assertEqual(panel["gap_y_mm"], 5.0)
        self.assertFalse(panel["rails_linked"])
        self.assertEqual(panel["rail_top_mm"], 8.0)
        self.assertEqual(panel["rail_left_mm"], 3.0)
        self.assertEqual(panel["hole_diameter_mm"], 3.4)
        self.assertTrue(panel["scores_on"])
        self.assertTrue(panel["score_rails_horizontal"])
        self.assertTrue(panel["score_rails_vertical"])
        prep = self.session.fragment()["params"]
        self.assertEqual(prep["cutter_diameter"], 1.0)
        self.assertEqual(prep["route_past_score_mm"], 1.5)
        self.assertEqual(prep["min_tab_mm"], 0.4)

    def test_the_panel_recalculates_and_is_not_stale(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.assertEqual(self.session.panel_stale, [])
        self.assertEqual(self.session.config.grid.columns
                         * self.session.config.grid.rows, 6)
        self.assert_not_stale("a full import")

    def test_a_carrier_only_import_recalculates_the_panel_too(self):
        self.session.import_settings(posted(CARRIER_ONLY))
        self.assertEqual(self.session.panel_stale, [])
        self.assert_not_stale("a carrier-only import")

    def test_a_prep_number_recomputes_the_board(self):
        view = self.session.view
        self.session.import_settings(posted(OTHER_PROJECT))
        self.assertIsNot(self.session.view, view)
        self.assertEqual(self.session.error, "")

    def test_a_carrier_only_import_pays_for_no_prep_recompute(self):
        view = self.session.view
        self.session.import_settings(posted(CARRIER_ONLY))
        self.assertIs(self.session.view, view)

    def test_the_stepped_panel_is_kept_when_only_the_carrier_moved(self):
        # The cache's own rule, read where it is decided: a carrier-only import
        # reuses the six stepped copies, a layout one does not.
        kept = self.session._panel_cache
        self.session.import_settings(posted(CARRIER_ONLY))
        self.assertIs(self.session._panel_cache, kept)
        self.session.import_settings(posted(OTHER_PROJECT))
        self.assertIsNot(self.session._panel_cache, kept)
        self.assert_not_stale("a layout import after a carrier one")

    def test_a_second_import_overwrites_the_first(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.session.import_settings(posted(CARRIER_ONLY))
        config = self.session.config
        self.assertEqual((config.grid.columns, config.grid.rows), (2, 1))
        self.assertEqual(config.rail_widths, (5.0, 5.0, 5.0, 5.0))
        self.assertTrue(config.panel.rails_linked)
        self.assertIsNone(config.panel.score_settings)
        self.assertEqual(self.session.pending_count, 2)
        self.assert_not_stale("a second import")

    def test_an_imported_value_stays_editable(self):
        self.session.import_settings(posted(OTHER_PROJECT))
        self.session.set_param({"rail_left_mm": 4.5})
        self.assertEqual(self.session.config.rail_widths,
                         (8.0, 8.0, 4.5, 3.0))
        self.assertEqual(self.session.pending_count, 2)
        self.assert_not_stale("a typed rail after an import")

    def test_a_file_that_changes_nothing_says_so_and_stays_clean(self):
        same = json.loads(self.config_path.read_text(encoding="utf-8"))
        reply = self.session.import_settings(posted(same, "same.json"))
        self.assertIn("nothing changed", reply["message"])
        self.assertEqual(self.session.pending_count, 0)
        self.assert_nothing_written()

    def test_a_refused_import_changes_nothing_at_all(self):
        held = json.dumps(self.session.data, sort_keys=True)
        generation = self.session.generation
        config = self.session.config
        panel = self.session.panel
        with self.assertRaises(actions.ActionError):
            self.session.import_settings({"name": "bad.json",
                                          "content": "{oops"})
        self.assertEqual(json.dumps(self.session.data, sort_keys=True), held)
        self.assertEqual(self.session.generation, generation)
        self.assertIs(self.session.config, config)
        self.assertIs(self.session.panel, panel)
        self.assertEqual(self.session.pending_count, 0)
        self.assert_nothing_written()

    def test_a_config_with_no_carrier_imports_and_the_panel_stays_true(self):
        lean = {key: value for key, value in CARRIER_ONLY.items()
                if key != "panel"}
        self.session.import_settings(posted(lean))
        self.assertIsNone(self.session.config.panel)
        self.assertIsNone(self.session.panel_fragment()["params"]["rail_mm"])
        self.assert_not_stale("an import with no carrier")

    def test_the_saved_panel_is_what_the_command_line_builds(self):
        # THE SAVE PATH, after an import: the files a fabricator gets are the
        # files a plain build of the imported settings writes.
        self.session.import_settings(posted(OTHER_PROJECT))
        self.session.save()
        self.session.save_panel()
        written = Path(self.session.panel_saved.output_path).read_text(
            encoding="utf-8"
        )
        board = Board.from_file(self.session.config.source_board_path)
        fresh = step_and_repeat.build_from(
            self.session.config, board, fill_rails=False
        ).board.to_string()
        self.assertEqual(written, fresh)

    def test_two_sessions_importing_the_same_file_build_the_same_bytes(self):
        # Same input, same output, reached by two roads: an
        # import into this session, and a session launched on a config that
        # already said it.
        self.session.import_settings(posted(OTHER_PROJECT))
        mine = self.session.panel.outcome.board.to_string()

        elsewhere = Path(self.folder) / "twin"
        twin_config = write_panel_project(elsewhere)
        settings = json.loads(json.dumps(OTHER_PROJECT))
        settings["source_board"] = "../board.kicad_pcb"
        twin_config.write_text(settings_text(settings), encoding="utf-8")
        twin = PreviewSession(twin_config, elsewhere / "work",
                              render=self.session._render)
        try:
            twin.refresh()
            twin.start_panel()
            self.assertEqual(twin.panel_error, "")
            self.assertEqual(twin.panel.outcome.board.to_string(), mine)
        finally:
            twin.close()


class ImportJournalTest(unittest.TestCase):
    """What the journal keeps: the file's name, and which settings moved."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.records = []
        self.session = a_session(self.folder, self.config_path)
        self.session.journal = _Recorder(self.records)

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def imports(self) -> list:
        return [data for kind, data in self.records if kind == "import"]

    def test_an_import_is_recorded_with_its_file_and_its_keys(self):
        self.session.import_settings(posted(OTHER_PROJECT, "other.json"))
        record = self.imports()[0]
        self.assertEqual(record["name"], "other.json")
        self.assertTrue(record["changed"])
        self.assertTrue(record["prep_moved"])
        self.assertIn("grid", record["keys"])
        self.assertIn("panel", record["keys"])
        self.assertNotIn("source_board", record["keys"])
        # The file itself, so a session can be replayed from its journal.
        self.assertEqual(record["settings"]["grid"]["columns"], 3)

    def test_an_import_that_changed_nothing_is_recorded_as_one(self):
        same = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.session.import_settings(posted(same, "same.json"))
        record = self.imports()[0]
        self.assertFalse(record["changed"])
        self.assertEqual(record["keys"], [])


class _Recorder:
    """A journal that keeps its lines in a list. Only what the session calls."""

    folder = Path("journal")
    last_shot = 0

    def __init__(self, records):
        self.records = records

    def append(self, kind, **data):
        self.records.append((kind, data))

    def close(self, reason: str = ""):
        pass


# ---------------------------------------------------------------------------
# the page: one group, one picker, and the handler behind it
# ---------------------------------------------------------------------------


class ImportPageTest(unittest.TestCase):
    """The control, read off the bytes the panel page serves."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.session.start_panel()
        cls.markup = cls.session.panel_page()
        cls.controls = controls_of(cls.markup)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls._folder.cleanup()

    def test_the_group_is_there_and_reads_as_a_group(self):
        self.assertIn('<section id=pb-sec-import data-group=pb-sec-import',
                      self.controls)
        self.assertIn('<span class=nm>Import settings</span>', self.controls)

    def test_it_is_served_folded_with_its_body_hidden(self):
        section = self.controls.split("<section id=pb-sec-import")[1]
        section = section.split("</section>")[0]
        self.assertIn('data-collapsed=1', section)
        self.assertIn('<div class=foldbody hidden>', section)
        self.assertIn('aria-expanded=false', section)

    def test_it_is_the_first_group_in_the_column(self):
        self.assertLess(self.controls.index("pb-sec-import"),
                        self.controls.index("pb-sec-layout"))

    def test_the_control_is_a_native_file_picker(self):
        self.assertIn('<input type=file id=pb-import', self.controls)
        self.assertIn('accept=".json,application/json"', self.controls)

    def test_the_picker_is_disabled_while_an_action_runs(self):
        # `data-act` is what the busy state switches off, so a file picked
        # during a build cannot start a second action.
        field = self.controls.split("<input type=file id=pb-import")[1]
        self.assertIn("data-act=param", field.split(">")[0])

    def test_the_field_wears_a_title_and_one_helper_line(self):
        body = self.controls.split('<label class="field wide">')[1]
        body = body.split("</label>")[0]
        self.assertIn("<span class=t>Settings file</span>", body)
        helper = body.split("<span class=h>")[1].split("</span>")[0]
        self.assertLessEqual(len(helper), 72)
        self.assertIn(f'title="{helper}"', body)

    def test_the_group_says_what_is_kept_and_what_is_not_written(self):
        self.assertIn("stays editable", self.controls)
        self.assertIn("keeps its own board file", self.controls)
        self.assertIn("held in memory until Save Panel Files", self.controls)

    def test_no_page_asks_for_a_path(self):
        # A browser does not hand a page one, so a page that asked for one
        # would be asking for something nobody can supply.
        self.assertNotIn("paste the path", self.markup)
        self.assertNotIn("type the path", self.markup)


IMPORT_HARNESS = """
%(functions)s
let busy = false, stopped = false;
const out = {posted: [], said: [], logged: []};
function say(text, kind) { out.said.push({text: text, kind: kind}); }
function logEvent(type, data) { out.logged.push({type: type, data: data}); }
async function post(path, body, what) {
  out.posted.push({path: path, body: body, what: what});
  return {ok: true};
}
// The browser's own File, modelled to the one method the page uses.
function file(name, text, fails) {
  return {name: name, text: async () => {
    if (fails) throw new Error('unreadable');
    return text;
  }};
}
out.picked = await importSettings(file('other.json', '{"grid": 1}'), 'file');
out.nothing = await importSettings(null, 'file');
out.broken = await importSettings(file('x.json', '', true), 'file');
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientImportTest(unittest.TestCase):
    """`importSettings`, RUN: the picker's File becomes a posted body."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_js(IMPORT_HARNESS % {
            "functions": js_function(page_panel.PANEL_JS, "importSettings"),
        })

    def test_the_file_s_contents_are_what_is_posted(self):
        self.assertEqual(len(self.out["posted"]), 1)
        sent = self.out["posted"][0]
        self.assertEqual(sent["path"], "/import_settings")
        self.assertEqual(sent["body"],
                         {"name": "other.json", "content": '{"grid": 1}'})
        self.assertIn("other.json", sent["what"])

    def test_the_import_is_journaled_by_the_page_that_asked_for_it(self):
        logged = self.out["logged"][0]
        self.assertEqual(logged["type"], "import")
        self.assertEqual(logged["data"]["name"], "other.json")
        self.assertEqual(logged["data"]["page"], "panel")
        self.assertEqual(logged["data"]["bytes"], len('{"grid": 1}'))

    def test_no_file_posts_nothing(self):
        self.assertIsNone(self.out["nothing"])

    def test_a_file_that_cannot_be_read_says_so_and_posts_nothing(self):
        self.assertIsNone(self.out["broken"])
        self.assertEqual(len(self.out["posted"]), 1)
        self.assertIn("could not be read", self.out["said"][0]["text"])
        self.assertEqual(self.out["said"][0]["kind"], "bad")

    def test_the_picker_is_cleared_before_the_post(self):
        # Picking the same file twice has to be two attempts: a refused import
        # leaves the control holding the file it refused.
        self.assertIn("importEl.value = '';", page_panel.PANEL_JS)


class ImportRouteTest(unittest.TestCase):
    """The route table: gated like every other change, summarised in the log."""

    def test_the_endpoint_is_one_of_the_changing_actions(self):
        self.assertIn("/import_settings", server_module.CHANGING)
        self.assertIn("/import_settings", server_module.BODY_LIMITS)

    def test_the_journal_line_names_the_file_and_not_its_contents(self):
        summary = server_module._loggable_payload(
            "/import_settings",
            {"name": "C:\\fakepath\\other.json", "content": "x" * 900},
        )
        self.assertEqual(summary, {"name": "other.json", "content_bytes": 900})

    def test_the_page_posts_to_the_endpoint_the_server_serves(self):
        self.assertIn("post('/import_settings'", page_panel.PANEL_JS)


class ServedImportTest(unittest.TestCase):
    """The endpoint over the real loopback server: 200, 400, and the fragment."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = a_session(self.folder, self.config_path)
        self.session.start_panel()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def tearDown(self):
        self.server.stop()
        self._folder.cleanup()

    def post(self, payload: dict, timeout: float = 60.0) -> tuple:
        request = urllib.request.Request(
            self.url + "import_settings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def fragment(self) -> dict:
        with urllib.request.urlopen(self.url + "panel/fragment",
                                    timeout=60.0) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def test_a_posted_file_is_accepted_and_the_fragment_says_so(self):
        status, reply = self.post(posted(OTHER_PROJECT, "other.json"))
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["pending"], 1)
        fragment = self.fragment()
        self.assertEqual(fragment["params"]["columns"], 3)
        self.assertEqual(fragment["pending_config"], 1)
        self.assertEqual(fragment["stale"], [])

    def test_a_refused_file_is_a_400_with_the_reason(self):
        status, reply = self.post({"name": "bad.json", "content": "{oops"})
        self.assertEqual(status, 400)
        self.assertFalse(reply["ok"])
        self.assertIn("bad.json", reply["error"])
        self.assertEqual(self.fragment()["pending_config"], 0)

    def test_a_body_larger_than_the_limit_is_refused(self):
        status, reply = self.post(
            {"name": "huge.json",
             "content": " " * (server_module.MAX_IMPORT_BYTES + 1)}
        )
        self.assertEqual(status, 400)
        self.assertIn("too large", reply["error"])


class ImportFieldBuilderTest(unittest.TestCase):
    """`page_served.file_field`: the same clothes every entry field wears."""

    def markup(self, accept: str = "") -> str:
        return page_served.file_field("pb-x", "A file", "One short line.",
                                      accept=accept)

    def test_it_is_a_field_with_a_title_and_a_helper(self):
        markup = self.markup()
        self.assertIn('<label class="field wide">', markup)
        self.assertIn("<span class=t>A file</span>", markup)
        self.assertIn("<span class=h>One short line.</span>", markup)
        self.assertIn('title="One short line."', markup)

    def test_the_accept_list_is_optional_and_escaped(self):
        self.assertNotIn("accept", self.markup())
        self.assertIn('accept=".json"', self.markup(".json"))

    def test_the_helper_and_the_title_are_escaped_together(self):
        markup = page_served.file_field("pb-x", "T", 'a "quoted" one', "")
        self.assertIn("<span class=h>a &quot;quoted&quot; one</span>", markup)
        self.assertIn('title="a &quot;quoted&quot; one"', markup)

    def test_the_picker_has_a_style_of_its_own(self):
        self.assertIn("input[type=file]", page_served.CONTROLS_CSS)


if __name__ == "__main__":  # pragma: no cover - the runner is the entry point
    unittest.main()
