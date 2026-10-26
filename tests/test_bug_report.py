"""The problem report: one zip that lets a developer work on somebody's board.

The "Report a problem" key and button build an archive that has to carry
everything a developer needs for a board they have never opened. Five things
are pinned here, and each one is a way the archive could be quietly useless.

**The members are the run.** The source board and its project file, the panel
configuration, the saved panel when one exists, the session journal, the shot
this report just took, the prep and panel reports, the version facts, and the
KiCad library tables that decided where the footprints came from.

**The manifest is honest.** What went in, what was absent and why, and what was
skipped for size. A machine with no KiCad installed still produces a zip; the
tables are listed under `absent` with the reason, so a reader can tell "this
machine had none" from "the collector forgot".

**Nothing outside three places is read.** The project folder, the session
folder, and the named KiCad settings files. No credential, and no environment
variable beyond the KiCad path block.

**The journal is complete.** A button press, a parameter that was applied, a
value that was typed and a group that was folded all reach the file, because
the record of what the person did is the half of a bug report that no file on
their disk can replace.

**The browser owns the save dialog.** A page cannot write to a chosen folder,
so the zip is fetched through an anchor carrying `download` and the server
answers that fetch with the file it built last.

**The report stays on this computer.** It lands in `bug_reports/` beside the
design, or in the local folder the environment variable names, and nothing is
written anywhere else. No network folder and no mail address.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from eedesignpanels import config as config_module
from eedesignpanels.config import load_config
from eedesignpanels.preview import (
    actions,
    bugreport,
    page_panel,
    page_served,
    server as server_module,
)
from eedesignpanels.preview.session import PreviewSession
from test_preview import ServerTestCase, stub_render, write_project
from test_preview_journal import open_session, shot_payload

_SAVED_FOLDER_SETTING = None


def setUpModule():
    """Every test here sees the default folder unless it names another."""
    global _SAVED_FOLDER_SETTING
    _SAVED_FOLDER_SETTING = os.environ.pop(bugreport.REPORT_FOLDER_VARIABLE,
                                           None)


def tearDownModule():
    if _SAVED_FOLDER_SETTING is not None:
        os.environ[bugreport.REPORT_FOLDER_VARIABLE] = _SAVED_FOLDER_SETTING

# A global table that includes a second one, which is the shape KiCad ships by
# default (one nested row pointing at its own stock table).
INCLUDED_TABLE = (
    "(fp_lib_table\n  (version 7)\n"
    '  (lib (name "Stock")(type "KiCad")(uri "/somewhere/Stock.pretty")'
    '(options "")(descr ""))\n)\n'
)


def write_settings(folder: Path, nested: bool = True) -> Path:
    """A KiCad user settings folder: two tables, one nested, and the paths."""
    settings = folder / "kicad_settings"
    settings.mkdir(parents=True, exist_ok=True)
    included = settings / "included_fp_table"
    included.write_text(INCLUDED_TABLE, encoding="utf-8")
    row = (
        f'  (lib (name "Nested")(type "Table")(uri "{included.as_posix()}")'
        f'(options "")(descr ""))\n' if nested else ""
    )
    (settings / "fp-lib-table").write_text(
        f"(fp_lib_table\n  (version 7)\n{row}"
        '  (lib (name "Local")(type "KiCad")(uri "/here/Local.pretty")'
        '(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    (settings / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n'
        '  (lib (name "Sym")(type "KiCad")(uri "/here/Sym.kicad_sym")'
        '(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    (settings / "kicad_common.json").write_text(json.dumps({
        "environment": {"vars": {"MY_LIBRARIES": "D:/libraries"}},
        "session": {"remember_open_files": True},
        "do_not_show_again": {"scaled_3d_models_warning": True},
    }, indent=2), encoding="utf-8")
    return settings


# ---------------------------------------------------------------------------
# the pieces, one collector at a time
# ---------------------------------------------------------------------------


class NamingTest(unittest.TestCase):
    """The default file name the browser is offered."""

    def test_the_project_comes_first_and_the_stamp_last(self):
        name = bugreport.report_stem(
            Path("C:/x/100-200-3000_sensor-board-v2.kicad_pcb"),
            stamp="20260914_143000",
        )
        self.assertEqual(
            name, "100-200-3000_sensor-board-v2_bug_report_20260914_143000"
        )

    def test_a_name_that_could_be_a_path_never_becomes_one(self):
        name = bugreport.report_stem(Path("../../etc/pa ss wd.kicad_pcb"),
                                     stamp="20260914_143000")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn(" ", name)

    def test_no_board_at_all_still_names_the_file(self):
        self.assertTrue(
            bugreport.report_stem(None, stamp="20260914_143000")
            .startswith("panel_bug_report_")
        )


class VersionFactsTest(unittest.TestCase):
    """Every version a reader needs before they believe another file."""

    def test_it_names_the_three_pieces_of_software_and_the_machine(self):
        facts = bugreport.version_facts()
        for key in ("eedesignpanels", "eedesigncore", "kicad_cli", "python",
                    "os", "machine", "interpreter"):
            with self.subTest(key=key):
                self.assertIn(key, facts)
        self.assertTrue(facts["eedesignpanels"])
        self.assertTrue(facts["eedesigncore"])

    def test_no_kicad_cli_is_a_recorded_answer_and_not_an_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            said = bugreport.kicad_cli_version(Path(folder) / "nothing.exe")
        self.assertFalse(said["found"])
        self.assertEqual(said["version"], "")
        self.assertIn("no kicad-cli", said["reason"])


class KicadSettingsTest(unittest.TestCase):
    """The library tables and the path variables, and their absence."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.settings = write_settings(self.folder)

    def tearDown(self):
        self._folder.cleanup()

    def collect(self, settings=None):
        return bugreport.kicad_settings_pieces(settings or self.settings)

    def test_both_global_tables_are_collected(self):
        pieces, _ = self.collect()
        names = [one.name for one in pieces]
        self.assertIn("kicad/fp-lib-table", names)
        self.assertIn("kicad/sym-lib-table", names)

    def test_a_table_that_includes_another_brings_it_along(self):
        pieces, _ = self.collect()
        nested = [one for one in pieces
                  if one.name.startswith("kicad/fp-lib-table.included/")]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0].path.name, "included_fp_table")

    def test_only_the_environment_block_of_the_settings_file_travels(self):
        pieces, _ = self.collect()
        paths = [one for one in pieces if one.kind == "kicad_paths"]
        self.assertEqual(len(paths), 1)
        body = json.loads(paths[0].data.decode("utf-8"))
        self.assertEqual(body["environment"],
                         {"vars": {"MY_LIBRARIES": "D:/libraries"}})
        # The rest of that file is window positions and recent projects, and
        # none of it belongs in somebody else's hands.
        self.assertNotIn("remember_open_files", paths[0].data.decode("utf-8"))
        self.assertNotIn("do_not_show_again", paths[0].data.decode("utf-8"))

    def test_a_machine_with_no_kicad_says_so_and_collects_nothing(self):
        pieces, missing = self.collect(self.folder / "no_kicad_here")
        self.assertEqual(pieces, [])
        self.assertEqual(len(missing), 3)
        for one in missing:
            with self.subTest(name=one.name):
                self.assertIn("no KiCad settings folder", one.reason)

    def test_a_table_that_cannot_be_parsed_costs_its_chain_and_nothing_else(self):
        (self.settings / "fp-lib-table").write_text("(not a table",
                                                    encoding="utf-8")
        pieces, missing = self.collect()
        self.assertIn("kicad/fp-lib-table", [one.name for one in pieces])
        self.assertTrue(any("included tables" in one.name for one in missing))

    def test_something_far_too_large_is_not_a_library_table(self):
        (self.settings / "fp-lib-table").write_text(
            "x" * (bugreport.MAX_TABLE_BYTES + 1), encoding="utf-8"
        )
        pieces, missing = self.collect()
        self.assertNotIn("kicad/fp-lib-table", [one.name for one in pieces])
        self.assertTrue(any("not a library table" in one.reason
                            for one in missing))


# ---------------------------------------------------------------------------
# the zip, and the manifest inside it
# ---------------------------------------------------------------------------


class ZipCapTest(unittest.TestCase):
    """Size sanity: a piece that is skipped is skipped BY NAME."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def write(self, pieces, missing=(), **caps):
        return bugreport.write_zip(
            self.folder / "out.zip", list(pieces), list(missing),
            {"schema": bugreport.SCHEMA}, **caps
        )

    def test_a_member_over_the_cap_is_named_in_the_manifest(self):
        report = self.write(
            [bugreport.Piece("big.bin", "test", data=b"x" * 5000),
             bugreport.Piece("small.txt", "test", data=b"ok")],
            max_member_bytes=1000,
        )
        self.assertEqual([one["name"] for one in report.manifest["collected"]],
                         ["small.txt"])
        skipped = report.manifest["skipped"]
        self.assertEqual(skipped[0]["name"], "big.bin")
        self.assertIn("cap for one file", skipped[0]["reason"])
        self.assertEqual(skipped[0]["bytes"], 5000)

    def test_the_budget_stops_collecting_and_says_which_piece_it_stopped_on(self):
        report = self.write(
            [bugreport.Piece("a.bin", "test", data=b"x" * 600),
             bugreport.Piece("b.bin", "test", data=b"x" * 600)],
            max_total_bytes=1000,
        )
        self.assertEqual([one["name"] for one in report.manifest["collected"]],
                         ["a.bin"])
        self.assertIn("budget", report.manifest["skipped"][0]["reason"])

    def test_the_manifest_describes_what_went_in_not_what_was_wanted(self):
        report = self.write(
            [bugreport.Piece("big.bin", "test", data=b"x" * 5000)],
            max_member_bytes=10,
        )
        with zipfile.ZipFile(report.path) as archive:
            members = archive.namelist()
            manifest = json.loads(archive.read(bugreport.MANIFEST_NAME))
        self.assertEqual(members, [bugreport.MANIFEST_NAME])
        self.assertEqual(manifest["collected"], [])
        self.assertEqual(manifest["uncompressed_bytes"], 0)

    def test_an_absent_piece_carries_its_reason(self):
        report = self.write(
            [], [bugreport.Missing("kicad/fp-lib-table", "lib_table",
                                   "no KiCad settings folder at X")]
        )
        absent = report.manifest["absent"]
        self.assertEqual(absent[0]["name"], "kicad/fp-lib-table")
        self.assertIn("no KiCad settings folder", absent[0]["reason"])

    def test_a_file_that_vanished_is_skipped_rather_than_losing_the_report(self):
        report = self.write(
            [bugreport.Piece("gone.txt", "test", path=self.folder / "gone.txt"),
             bugreport.Piece("here.txt", "test", data=b"ok")]
        )
        self.assertEqual([one["name"] for one in report.manifest["collected"]],
                         ["here.txt"])
        self.assertEqual(report.manifest["skipped"][0]["name"], "gone.txt")


# ---------------------------------------------------------------------------
# one report, from a live session
# ---------------------------------------------------------------------------


class ReportSessionTestCase(unittest.TestCase):
    """A recorded session over the stub renderer: no kicad-cli anywhere."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.board_path = self.config.source_board_path
        self.settings = write_settings(self.folder)
        self.recording = open_session(self.folder, config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()
        self.assertEqual(self.session.error, "")

    def tearDown(self):
        self._folder.cleanup()

    def report(self, payload=None):
        body = shot_payload() if payload is None else payload
        return self.session.bug_report(body)

    def members(self, reply) -> dict:
        with zipfile.ZipFile(reply["file"]) as archive:
            return {info.filename: info.file_size for info in archive.infolist()}

    def manifest(self, reply) -> dict:
        with zipfile.ZipFile(reply["file"]) as archive:
            return json.loads(archive.read(bugreport.MANIFEST_NAME))


class ReportMembersTest(ReportSessionTestCase):
    """What one report holds on a session that has run."""

    def setUp(self):
        super().setUp()
        self.reply = self.report()
        self.member = self.members(self.reply)
        self.body = self.manifest(self.reply)

    def test_it_holds_the_design_files_and_the_configuration(self):
        for name in ("project/board.kicad_pcb", "project/panel.json"):
            with self.subTest(name=name):
                self.assertIn(name, self.member)
        self.assertEqual(
            self.member["project/board.kicad_pcb"],
            self.board_path.stat().st_size,
        )

    def test_it_holds_the_session_journal_and_the_config_it_started_with(self):
        self.assertIn("session/journal.jsonl", self.member)
        self.assertIn("session/config_at_launch.json", self.member)
        self.assertGreater(self.member["session/journal.jsonl"], 0)

    def test_it_holds_the_shot_this_report_took(self):
        shot = self.reply["shot"]
        self.assertIn(f"view/{shot:03d}_board.svg", self.member)
        self.assertIn(f"view/{shot:03d}_state.json", self.member)

    def test_it_holds_the_prep_and_panel_reports(self):
        for name in ("reports/prep_warnings.txt", "reports/panel_report.txt",
                     "reports/checks.json"):
            with self.subTest(name=name):
                self.assertIn(name, self.member)

    def test_it_holds_the_version_facts(self):
        with zipfile.ZipFile(self.reply["file"]) as archive:
            facts = json.loads(archive.read(bugreport.VERSIONS_NAME))
        self.assertTrue(facts["eedesignpanels"])
        self.assertTrue(facts["eedesigncore"])
        self.assertIn("path", facts["kicad_cli"])
        self.assertTrue(facts["os"])

    def test_the_manifest_says_what_it_is_and_where_it_was_written(self):
        self.assertEqual(self.body["schema"], bugreport.SCHEMA)
        self.assertEqual(self.body["report_folder"],
                         str(self.config.panel_dir / bugreport.REPORT_FOLDER_NAME))
        self.assertEqual(self.body["summary"], bugreport.SUMMARY_NAME)
        self.assertEqual(self.body["privacy"], bugreport.PRIVACY_LINE)
        self.assertTrue(self.body["created_at"].endswith("Z"))
        self.assertEqual(self.body["report"], self.reply["name"])

    def test_the_manifest_names_the_settings_revision(self):
        # The zip says which revision of the settings it carries, beside the
        # settings themselves, so a report answers "where did this config
        # come from" without being opened.
        revision = self.body["settings_revision"]
        self.assertEqual(revision["file"], config_module.CONFIG_REVISION)
        self.assertEqual(revision["this_tool"], config_module.CONFIG_REVISION)
        self.assertEqual(revision["written_by"], config_module.WRITTEN_BY)

    def test_every_name_the_manifest_collected_is_a_member(self):
        for one in self.body["collected"]:
            with self.subTest(name=one["name"]):
                self.assertIn(one["name"], self.member)
                self.assertEqual(one["bytes"], self.member[one["name"]])

    def test_an_absent_panel_is_listed_as_absent_and_not_invented(self):
        # Nothing has been saved into the panel folder in this session.
        absent = {one["name"] for one in self.body["absent"]}
        self.assertIn("panel/panel.kicad_pcb", absent)
        self.assertNotIn("panel/panel.kicad_pcb", self.member)

    def test_the_zip_lands_beside_the_design_and_the_reply_says_where(self):
        kept = Path(self.reply["file"])
        self.assertTrue(kept.is_file())
        self.assertEqual(kept.parent,
                         self.config.panel_dir / bugreport.REPORT_FOLDER_NAME)
        self.assertEqual(self.reply["folder"], str(kept.parent))

    def test_the_reply_names_the_summary_beside_the_zip(self):
        summary = Path(self.reply["summary_file"])
        self.assertTrue(summary.is_file())
        self.assertEqual(summary.parent, Path(self.reply["file"]).parent)
        self.assertEqual(summary.name,
                         Path(self.reply["file"]).stem + ".summary.json")

    def test_the_reply_carries_the_name_the_browser_should_offer(self):
        self.assertTrue(self.reply["name"].endswith(".zip"))
        self.assertIn("_bug_report_", self.reply["name"])
        self.assertEqual(self.reply["download"], bugreport.DOWNLOAD_PATH)

    def test_building_a_report_leaves_the_project_alone(self):
        before = (self.board_path.read_bytes(), self.config_path.read_bytes())
        self.report()
        self.assertEqual(
            (self.board_path.read_bytes(), self.config_path.read_bytes()),
            before,
        )

    def test_it_is_journaled_with_what_it_collected(self):
        logged = [r for r in self.recording.records()
                  if r["kind"] == "bug_report"][-1]["data"]
        self.assertEqual(logged["file"], self.reply["name"])
        self.assertEqual(logged["collected"], self.reply["collected"])
        self.assertEqual(logged["absent"], self.reply["absent"])


class ReportNoteTest(ReportSessionTestCase):
    """The note field survives: a report carries the person's own line."""

    def test_the_typed_line_becomes_note_txt(self):
        payload = shot_payload()
        payload["note"] = "the left tab is cut through on copy 3"
        reply = self.report(payload)
        with zipfile.ZipFile(reply["file"]) as archive:
            self.assertEqual(
                archive.read(bugreport.NOTE_NAME).decode("utf-8").strip(),
                "the left tab is cut through on copy 3",
            )
        self.assertEqual(self.manifest(reply)["note"],
                         "the left tab is cut through on copy 3")

    def test_the_older_note_box_key_still_carries_the_line(self):
        payload = shot_payload()
        payload["note_box"] = "it built the wrong way round"
        reply = self.report(payload)
        with zipfile.ZipFile(reply["file"]) as archive:
            self.assertIn("it built the wrong way round",
                          archive.read(bugreport.NOTE_NAME).decode("utf-8"))

    def test_no_line_is_an_absence_with_a_reason_not_an_empty_file(self):
        reply = self.report()
        self.assertNotIn(bugreport.NOTE_NAME, self.members(reply))
        absent = {one["name"]: one["reason"]
                  for one in self.manifest(reply)["absent"]}
        self.assertIn("nobody typed a line", absent[bugreport.NOTE_NAME])

    def test_the_note_endpoint_still_keeps_a_line_against_the_shot(self):
        reply = self.report()
        kept = self.session.note({"text": "and this one too"})
        self.assertEqual(kept["shot"], reply["shot"])


class ReportPrivacyTest(ReportSessionTestCase):
    """Nothing outside the project, the session and the KiCad settings."""

    def test_every_collected_file_comes_from_one_of_the_three_places(self):
        body = self.manifest(self.report())
        allowed = (
            self.board_path.parent.resolve(),
            self.recording.folder.resolve(),
        )
        for one in body["collected"]:
            source = one.get("source")
            if not source:
                continue  # built here, not read from disk
            path = Path(source).resolve()
            under = any(path == root or root in path.parents
                        for root in allowed)
            with self.subTest(name=one["name"]):
                self.assertTrue(under or one["kind"] == "lib_table", source)

    def test_no_credential_shaped_value_reaches_the_archive(self):
        self.session.journal_events({"events": [
            {"type": "control", "id": "pb-apply", "token": "abcd1234"},
        ]})
        reply = self.report()
        with zipfile.ZipFile(reply["file"]) as archive:
            text = archive.read("session/journal.jsonl").decode("utf-8")
        self.assertNotIn("abcd1234", text)
        self.assertIn("[redacted]", text)


class ReportJournalCompletenessTest(ServerTestCase):
    """Every press, every number and every fold reaches the archived journal.

    Driven over the socket, because half the record is written by the SERVER -
    an applied parameter is a `server.action` line with its payload - and a
    test that called the session directly would be fencing its own fixture.
    """

    def before_start(self):
        self.recording = open_session(self.folder, stamp="20260914_110000",
                                      config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()

    def setUp(self):
        super().setUp()
        self.post("set_param", {"cutter_diameter": 1.2})
        self.post("journal", {"events": [
            {"type": "control", "id": "pb-build", "label": "Build Panel",
             "where": "controls"},
            {"type": "param", "cause": "enter", "cutter_diameter": 1.2},
            {"type": "fold", "group": "carrier", "collapsed": True,
             "page": "panel"},
            {"type": "said", "text": "that number is not a number",
             "page": "prep"},
        ]})
        _, self.reply = self.post("bug_report", shot_payload())
        with zipfile.ZipFile(self.reply["file"]) as archive:
            self.lines = [
                json.loads(line) for line in
                archive.read("session/journal.jsonl").decode("utf-8")
                .splitlines() if line.strip()
            ]

    def of_kind(self, kind: str) -> list:
        return [record for record in self.lines if record["kind"] == kind]

    def test_a_button_press_is_in_the_archived_journal(self):
        pressed = self.of_kind("client.control")
        self.assertEqual(pressed[-1]["data"]["id"], "pb-build")
        self.assertEqual(pressed[-1]["data"]["label"], "Build Panel")

    def test_a_typed_value_is_in_it_with_its_number(self):
        typed = self.of_kind("client.param")
        self.assertEqual(typed[-1]["data"]["cutter_diameter"], 1.2)
        self.assertEqual(typed[-1]["data"]["cause"], "enter")

    def test_an_applied_parameter_is_in_it_as_a_server_action(self):
        applied = [r for r in self.of_kind("server.action")
                   if r["data"]["endpoint"] == "/set_param"]
        self.assertEqual(applied[-1]["data"]["payload"],
                         {"cutter_diameter": 1.2})
        self.assertTrue(applied[-1]["data"]["ok"])

    def test_a_fold_is_in_it_with_the_group_and_the_page(self):
        folded = self.of_kind("client.fold")
        self.assertEqual(folded[-1]["data"]["group"], "carrier")
        self.assertTrue(folded[-1]["data"]["collapsed"])

    def test_what_the_page_told_the_person_is_in_it(self):
        said = self.of_kind("client.said")
        self.assertIn("not a number", said[-1]["data"]["text"])

    def test_the_view_the_shot_was_taken_from_is_in_it(self):
        opened = self.of_kind("session.start")
        self.assertEqual(len(opened), 1)
        self.assertTrue(self.of_kind("compute"))

    def test_the_report_line_lands_after_the_archive_is_sealed(self):
        # The zip holds the journal AS IT WAS when the button was pressed, so
        # it cannot hold its own line. The live journal gets one immediately
        # after, which is where a reader of the SESSION looks.
        self.assertNotIn("bug_report", [r["kind"] for r in self.lines])
        live = [r for r in self.recording.records() if r["kind"] == "bug_report"]
        self.assertEqual(live[-1]["data"]["file"], self.reply["name"])

    def test_the_page_journals_every_one_of_those_by_itself(self):
        # The calls the tests above stand in for. A test that posted events
        # without this would be fencing its own fixture.
        script = page_served.CONTROLS_JS + page_panel.PANEL_JS
        for call in ("logEvent('control'", "logEvent('param'",
                     "logEvent('fold'", "logEvent('said'",
                     "logEvent('view'", "logEvent('layers'",
                     "logEvent('action'", "logEvent('page.open'"):
            with self.subTest(call=call):
                self.assertIn(call, script)


class ReportWithoutKicadTest(unittest.TestCase):
    """A report from a machine with no KiCad is still a report."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.recording = open_session(self.folder, config_path=self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def build(self, settings_dir):
        return bugreport.build(
            self.config, self.recording, {"results": {}, "panel": {}},
            note="no kicad here", shot=0, settings_dir=settings_dir,
            cli_path=self.folder / "no-kicad-cli.exe", stamp="20260914_090000",
        )

    def test_the_zip_is_built_and_the_tables_are_listed_as_absent(self):
        report = self.build(self.folder / "nothing")
        self.assertTrue(report.path.is_file())
        absent = {one["name"] for one in report.manifest["absent"]}
        self.assertIn("kicad/fp-lib-table", absent)
        self.assertIn("kicad/sym-lib-table", absent)
        with zipfile.ZipFile(report.path) as archive:
            self.assertIn("project/board.kicad_pcb", archive.namelist())

    def test_the_version_facts_say_kicad_cli_was_not_there(self):
        report = self.build(self.folder / "nothing")
        self.assertFalse(report.manifest["versions"]["kicad_cli"]["found"])

    def test_with_settings_present_the_same_build_collects_them(self):
        settings = write_settings(self.folder)
        report = self.build(settings)
        names = {one["name"] for one in report.manifest["collected"]}
        self.assertIn("kicad/fp-lib-table", names)
        self.assertIn("kicad/sym-lib-table", names)
        self.assertIn("kicad/kicad_common.environment.json", names)

    def test_a_shot_that_was_never_taken_is_an_absence(self):
        report = self.build(self.folder / "nothing")
        absent = {one["name"]: one["reason"]
                  for one in report.manifest["absent"]}
        self.assertIn("took no shot", absent["view/"])

    def test_a_session_with_no_folder_cannot_build_one(self):
        with self.assertRaises(bugreport.ReportError):
            bugreport.build(self.config, None, {})


def every_file(root: Path) -> dict:
    """Every file under *root*, with its bytes, so a write anywhere shows."""
    return {path.resolve(): path.read_bytes()
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


class LocalReportFolderTest(unittest.TestCase):
    """The report lands in one local folder, and nothing else is written."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.recording = open_session(self.folder, config_path=self.config_path)
        self.recording.append("note", text="a record to collect")

    def tearDown(self):
        self._folder.cleanup()

    def build(self, **extra):
        return bugreport.build(
            self.config, self.recording,
            {"results": {"ok": False, "error": "the outline does not close",
                         "error_at": [12.5, 3.0], "warnings": ["one gap"]},
             "panel": {}},
            note="the outline has a gap", settings_dir=self.folder / "none",
            cli_path=self.folder / "no-kicad-cli.exe", stamp="20261023_120000",
            session_settings={"grid": {"columns": 3}}, **extra,
        )

    def test_the_default_folder_is_bug_reports_in_the_panel_folder(self):
        self.assertEqual(bugreport.report_folder(self.config, environ={}),
                         self.config.panel_dir / bugreport.REPORT_FOLDER_NAME)

    def test_the_report_lands_in_the_default_folder_and_nowhere_else(self):
        before = every_file(self.folder)
        report = self.build()
        after = every_file(self.folder)
        folder = (self.config.panel_dir / bugreport.REPORT_FOLDER_NAME).resolve()
        written = [path for path in after if before.get(path) != after[path]]
        self.assertEqual(sorted(written), sorted([report.path.resolve(),
                                                  report.summary_path.resolve()]))
        for path in written:
            with self.subTest(path=path):
                self.assertEqual(path.parent, folder)
        self.assertEqual([path for path in before if path not in after], [])

    def test_a_named_folder_takes_every_write_and_the_default_gets_none(self):
        chosen = (self.folder / "elsewhere" / "reports").resolve()
        before = every_file(self.folder)
        os.environ[bugreport.REPORT_FOLDER_VARIABLE] = str(chosen)
        try:
            report = self.build()
        finally:
            os.environ.pop(bugreport.REPORT_FOLDER_VARIABLE, None)
        after = every_file(self.folder)
        written = [path for path in after if before.get(path) != after[path]]
        self.assertTrue(written)
        for path in written:
            with self.subTest(path=path):
                self.assertEqual(path.parent, chosen)
        self.assertEqual(report.path.parent.resolve(), chosen)
        self.assertFalse(
            (self.config.panel_dir / bugreport.REPORT_FOLDER_NAME).exists()
        )

    def test_a_relative_name_is_taken_from_the_panel_folder(self):
        folder = bugreport.report_folder(
            self.config, environ={bugreport.REPORT_FOLDER_VARIABLE: "reports"})
        self.assertEqual(folder, self.config.panel_dir / "reports")

    def test_a_network_share_or_a_url_is_refused_by_name(self):
        for named in (r"\\server\share\reports", "//server/share",
                      "https://example.com/upload"):
            with self.subTest(named=named), \
                    self.assertRaises(bugreport.ReportError) as caught:
                bugreport.report_folder(
                    self.config,
                    environ={bugreport.REPORT_FOLDER_VARIABLE: named})
            self.assertIn("not a local folder", str(caught.exception))

    def test_the_summary_is_first_in_the_zip_and_beside_it(self):
        report = self.build()
        with zipfile.ZipFile(report.path) as archive:
            self.assertEqual(archive.namelist()[0], bugreport.SUMMARY_NAME)
            inside = archive.read(bugreport.SUMMARY_NAME)
        self.assertEqual(report.summary_path.read_bytes(), inside)

    def test_the_summary_names_version_time_board_settings_and_error(self):
        summary = json.loads(self.build().summary_path.read_text("utf-8"))
        self.assertEqual(summary["schema"], bugreport.SUMMARY_SCHEMA)
        self.assertEqual(summary["engine_version"], bugreport.__version__)
        self.assertTrue(summary["eedesigncore_version"])
        self.assertTrue(summary["created_at"].endswith("Z"))
        self.assertEqual(summary["board_file"],
                         str(self.config.source_board_path))
        self.assertEqual(summary["settings_file"],
                         str(self.config.config_path))
        self.assertEqual(summary["settings_in_session"],
                         {"grid": {"columns": 3}})
        self.assertTrue(summary["settings_differ_from_file"])
        self.assertIn("grid.columns",
                      [row["setting"] for row in summary["settings_differences"]])
        self.assertEqual(summary["error"], "the outline does not close")
        self.assertEqual(summary["error_where"], "12.5000, 3.0000 mm")
        self.assertEqual(summary["note"], "the outline has a gap")
        self.assertEqual(summary["warnings"], ["one gap"])

    def test_the_engine_names_no_mail_address_and_no_network_folder(self):
        source = Path(bugreport.__file__).read_text(encoding="utf-8")
        page = Path(page_served.__file__).read_text(encoding="utf-8")
        for text in (source, page):
            self.assertNotIn("mailto:", text)
            self.assertNotIn("panelizer_bug_reports", text)
            self.assertIsNone(re.search(r"[\w.]+@[\w-]+\.(com|org|net)", text))


class ReportRefusalTest(ReportSessionTestCase):
    """A report is never lost to its picture."""

    def test_a_viewport_that_will_not_serialize_costs_the_picture_only(self):
        payload = shot_payload()
        payload["viewports"][0]["svg"] = "<html>not a drawing</html>"
        reply = self.report(payload)
        self.assertTrue(reply["ok"])
        self.assertTrue(any("no picture" in one for one in reply["warnings"]))
        member = self.members(reply)
        self.assertIn("project/board.kicad_pcb", member)
        self.assertIn(f"view/{reply['shot']:03d}_state.json", member)
        self.assertNotIn(f"view/{reply['shot']:03d}_board.svg", member)

    def test_the_manifest_carries_the_reason_there_is_no_picture(self):
        payload = shot_payload()
        payload["viewports"][0]["svg"] = "<html>not a drawing</html>"
        body = self.manifest(self.report(payload))
        self.assertTrue(any("no picture" in one
                            for one in body["shot_warnings"]))

    def test_a_session_with_no_folder_refuses_by_name(self):
        loose = PreviewSession(self.config_path, self.folder / "loose",
                               render=stub_render)
        loose.refresh()
        with self.assertRaises(actions.ActionError) as caught:
            loose.bug_report(shot_payload())
        self.assertIn("nowhere to build a report", str(caught.exception))

    def test_a_body_that_is_not_a_body_still_builds_a_report(self):
        reply = self.session.bug_report("not a dict")
        self.assertTrue(reply["ok"])


# ---------------------------------------------------------------------------
# over the wire
# ---------------------------------------------------------------------------


class ReportServerTest(ServerTestCase):
    """The POST that builds it and the GET the browser fetches it with."""

    def before_start(self):
        self.recording = open_session(self.folder, stamp="20260914_100000",
                                      config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()

    def raw_get(self, path: str):
        with urllib.request.urlopen(self.url.rstrip("/") + path,
                                    timeout=20) as reply:
            return reply.status, dict(reply.headers), reply.read()

    def test_the_download_route_is_a_404_before_anything_is_built(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.raw_get(bugreport.DOWNLOAD_PATH)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_the_post_builds_it_and_the_get_hands_over_the_same_bytes(self):
        status, reply = self.post("bug_report", shot_payload())
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        code, headers, body = self.raw_get(bugreport.DOWNLOAD_PATH)
        self.assertEqual(code, 200)
        self.assertEqual(body, Path(reply["file"]).read_bytes())
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn(reply["name"], headers["Content-Disposition"])
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_the_bytes_that_arrive_open_as_a_zip(self):
        self.post("bug_report", shot_payload())
        _, _, body = self.raw_get(bugreport.DOWNLOAD_PATH)
        with tempfile.TemporaryDirectory() as folder:
            got = Path(folder) / "got.zip"
            got.write_bytes(body)
            with zipfile.ZipFile(got) as archive:
                self.assertIn(bugreport.MANIFEST_NAME, archive.namelist())
                self.assertIsNone(archive.testzip())

    def test_the_action_line_names_the_viewports_and_the_note_not_the_markup(self):
        payload = shot_payload()
        payload["note"] = "it cut the tab"
        self.post("bug_report", payload)
        logged = [r for r in self.recording.records()
                  if r["kind"] == "server.action"][-1]["data"]
        self.assertEqual(logged["endpoint"], "/bug_report")
        self.assertEqual(logged["payload"]["viewports"], ["board"])
        self.assertEqual(logged["payload"]["note"], "it cut the tab")
        self.assertNotIn("<path", json.dumps(logged))

    def test_a_report_body_may_be_as_large_as_a_snapshot_body(self):
        self.assertEqual(server_module.BODY_LIMITS["/bug_report"],
                         server_module.BODY_LIMITS["/snapshot"])


# ---------------------------------------------------------------------------
# what the page says before anything is collected
# ---------------------------------------------------------------------------


class ReportControlTest(ServerTestCase):
    """The control, as served. Both pages carry the identical header tools."""

    def setUp(self):
        super().setUp()
        _, self.prep = self.get("")
        _, self.panel = self.get("panel")

    def pages(self):
        return {"prep": self.prep, "panel": self.panel}

    def test_the_button_says_what_it_does_on_both_pages(self):
        # The button is a bare name; the hotkey rides in its `title`, not as
        # a parenthetical.
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn('id=pb-report-btn', markup)
                self.assertIn(page_served.REPORT_LABEL, markup)
                self.assertIn(
                    f"Hotkey: {page_served.REPORT_KEY.upper()}", markup
                )

    def test_the_header_carries_no_privacy_line(self):
        # What the report holds is said once, in the report dialog, and not
        # in the page header.
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertNotIn("pb-privacy", markup)

    def test_the_dialog_says_what_the_report_holds_and_that_it_stays_here(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn(bugreport.PRIVACY_LINE, markup)
                self.assertIn(page_served.REPORT_COPY_LABEL, markup)

    def test_no_mail_address_or_network_folder_is_on_the_page(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertNotIn("mailto:", markup)
                self.assertNotIn("panelizer_bug_reports", markup)

    def test_the_destination_is_a_block_in_the_report_dialog_that_copies(self):
        # The note and the destination are both shown in the report dialog,
        # not in the page header. The folder is the local one beside the
        # design, and a click selects its whole path for copying.
        folder = (load_config(self.config_path).panel_dir
                  / bugreport.REPORT_FOLDER_NAME)
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn(f"<code>{folder}</code>", markup)
        self.assertIn(".ask code{", page_served.CONTROLS_CSS)
        dest_rule = page_served.CONTROLS_CSS.split(".ask code{", 1)[1]
        self.assertIn("user-select:all", dest_rule.split("}", 1)[0])

    def test_the_old_snapshot_control_is_gone_from_both_pages(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertNotIn("pb-snap", markup)
                self.assertNotIn("snapshot (S)", markup)

    def test_the_page_fetches_the_zip_through_an_anchor_it_clicks(self):
        # A page cannot write to a chosen folder; the anchor is the only way it
        # can hand a file to the person reading it.
        for token in ("function downloadReport", "link.download = reply.name",
                      "link.href = reply.download", "link.click()"):
            with self.subTest(token=token):
                self.assertIn(token, self.prep)

    def test_the_page_still_references_nothing_outside_itself(self):
        for name, markup in self.pages().items():
            from eedesignpanels.preview import icons
            icon = icons.BOARD_ICON if name == "prep" else icons.PANEL_ICON
            self.assertEqual(markup.count(icon), 1)
            self.assertIn('href="data:image/svg+xml,', icon)
            markup = markup.replace(icon, "")
            for forbidden in ("<link", "<script src", "@import", "url(http"):
                with self.subTest(page=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, markup)


@unittest.skipUnless(shutil.which("node"), "no node to parse the page's script")
class ReportScriptTest(unittest.TestCase):
    """The renamed control's script still parses, on both pages."""

    def check(self, source: str) -> int:
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "controls.mjs"
            script.write_text(
                source.replace("__STATE__", "{}")
                .replace("__REPORT_KEY__", page_served.REPORT_KEY),
                encoding="utf-8",
            )
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), "--check", str(script)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.returncode

    def test_the_prep_page_script_is_valid_javascript(self):
        self.check(page_served.CONTROLS_JS)

    def test_the_panel_page_script_is_valid_javascript(self):
        self.check(page_served.page_script(page_panel.PANEL_JS, {}))

    def test_the_hotkey_is_the_one_the_page_states(self):
        self.assertIn(f"const REPORT_KEY = '{page_served.REPORT_KEY}'",
                      page_served.page_script(page_served.PREP_JS, {}))


if __name__ == "__main__":
    unittest.main()
