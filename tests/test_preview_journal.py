"""Session instrumentation: the journal, the snapshot, the note.

The user is the reviewer, and this file's subject is the record of a review
session. Four things are pinned here, and each one is a way the record could be
quietly useless.

**A journal line is a complete, self-describing record.** One JSON object per
line, ordered by a monotonic `seq`, timestamped, `kind` plus `data` - because
the deferred replay feature will read this file forwards and must not have to
guess what a line meant. Credential-looking keys are redacted and long values
truncated, so no journal line can be the thing that fails.

**A snapshot saves all three artifact kinds under one number.** The vector
file, the raster, and the state file that carries the config, the results and
the client's view state. The raster is capped on its long edge, and its
absence - the dev extras are optional - costs the PNG and never the shot.

**The counter and the note tie together.** Shot numbers are monotonic and never
reused, and a note attaches to the newest one, because that is how a reviewer
refers to a shot out loud.

**The instrumentation is served-mode only.** The frozen review artifact is a
portable file with nothing to talk to; every endpoint and every control added
here has to be absent from it and from the shared components it is built from.

The server half runs in-process on 127.0.0.1 driven by `urllib`, on the same
terms as :mod:`test_preview` - no browser is automated.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from eedesignpanels.config import load_config
from eedesignpanels.preview import (
    actions,
    journal as journal_module,
    page_served,
    raster as raster_module,
    server as server_module,
    session as session_module,
    snapshot as snapshot_module,
)
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.review import layerpanel
from eedesignpanels.review import page as review_page
from eedesignpanels.review import render, theme
from test_preview import ServerTestCase, stub_render, write_project

# What the page posts: a standalone document, in board millimetres, on a solid
# background, with one group per visible layer. The real one is the browser's
# serialization of the live stack; this is the same shape by hand.
SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
    'width="20.0000mm" height="10.0000mm" viewBox="0.000000 0.000000 '
    '20.000000 10.000000">'
    '<title>board - generation 1</title>'
    '<rect x="0" y="0" width="20" height="10" fill="rgb(20, 22, 26)"/>'
    '<g data-layer="Edge.Cuts" opacity="1">'
    '<path d="M0 0 L20 10" style="stroke:#d0d0d0;stroke-width:0.15;fill:none"/>'
    "</g></svg>"
)

VIEW_STATE = {
    "viewBox": [0.0, 0.0, 20.0, 10.0],
    "fit_viewbox": [0.0, 0.0, 20.0, 10.0],
    "zoom_pct": 100.0,
    "size_px": [1200, 600],
    "background": "rgb(20, 22, 26)",
    "layers": [
        {"canonical": "Edge.Cuts", "visible": True, "opacity": 1, "z": 99},
        {"canonical": "User.8", "visible": False, "opacity": 0.22, "z": 1},
    ],
}


def shot_payload(name: str = "board", svg: str = SAMPLE_SVG) -> dict:
    """One viewport posted, the way the page posts it."""
    return {
        "cause": "hotkey",
        "generation": 1,
        "layout": "side-by-side",
        "note_box": "",
        "panel": {"mode": 1, "active": "Edge.Cuts", "hidden": ["User.8"]},
        "screen": {"width": 3840, "height": 2160, "dpr": 1.5},
        "viewports": [{"name": name, "svg": svg, "view": dict(VIEW_STATE)}],
    }


def open_session(folder: Path, stamp: str = "20260908_120000",
                 config_path: Path = None) -> journal_module.UiSession:
    return journal_module.open_session(
        config_path, folder / "scratch", stamp=stamp, backend="test",
    )


# ---------------------------------------------------------------------------
# the journal
# ---------------------------------------------------------------------------


class JournalShapeTest(unittest.TestCase):
    """One launch, one folder, one line per event - and the line's shape."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.session = open_session(self.folder, config_path=self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def test_the_folder_is_named_for_the_launch_and_holds_the_three_things(self):
        self.assertEqual(self.session.folder.name, "20260908_120000_ui_session")
        self.assertTrue(self.session.journal_path.is_file())
        self.assertTrue(self.session.shots_dir.is_dir())
        copied = self.session.folder / journal_module.CONFIG_COPY_NAME
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), self.config_path.read_bytes())

    def test_the_first_line_says_what_the_file_is(self):
        first = self.session.records()[0]
        self.assertEqual(first["kind"], "session.start")
        self.assertEqual(first["seq"], 1)
        self.assertEqual(first["data"]["schema"], journal_module.SCHEMA)
        self.assertEqual(first["data"]["config"], str(self.config_path))
        self.assertTrue(first["at"].endswith("Z"))

    def test_every_record_has_the_same_shape_and_a_rising_seq(self):
        self.session.append("client.control", id="pb-reload")
        self.session.append("client.view", zoom_pct=250.0)
        self.session.append("note", text="the left tab is wrong", shot=1)
        records = self.session.records()
        self.assertEqual([r["seq"] for r in records], [1, 2, 3, 4])
        for record in records:
            self.assertEqual(
                set(record), {"seq", "at", "since_start_ms", "kind", "data"}
            )
            self.assertIsInstance(record["kind"], str)
            self.assertIsInstance(record["data"], dict)
            self.assertGreaterEqual(record["since_start_ms"], 0.0)

    def test_the_file_is_json_lines_and_nothing_else(self):
        self.session.append("client.control", id="pb-report")
        text = self.session.journal_path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        for line in text.splitlines():
            # Each line stands alone - that is what makes a journal readable
            # after a crash, and what a replay will consume.
            self.assertIsInstance(json.loads(line), dict)

    def test_a_credential_shaped_key_is_never_written(self):
        self.session.append(
            "client.control", token="abcd1234", nested={"api_key": "zzz"},
            id="pb-apply",
        )
        record = self.session.records()[-1]
        self.assertEqual(record["data"]["token"], journal_module.REDACTED)
        self.assertEqual(
            record["data"]["nested"]["api_key"], journal_module.REDACTED
        )
        self.assertEqual(record["data"]["id"], "pb-apply")
        self.assertNotIn("abcd1234", self.session.journal_path.read_text())

    def test_an_enormous_value_is_cut_rather_than_burying_the_line(self):
        self.session.append("client.control", label="x" * 99999,
                            points=list(range(1000)))
        data = self.session.records()[-1]["data"]
        self.assertTrue(data["label"].endswith("...[cut]"))
        self.assertLess(len(data["label"]), journal_module.MAX_STRING + 20)
        self.assertEqual(len(data["points"]), journal_module.MAX_ITEMS + 1)
        self.assertIn("cut", data["points"][-1])

    def test_something_that_is_not_json_still_gets_a_line(self):
        self.session.append("client.control", where=Path("a/b"), what=object())
        data = self.session.records()[-1]["data"]
        self.assertEqual(data["where"], str(Path("a/b")))
        self.assertIn("object", data["what"])

    def test_shot_numbers_rise_and_name_their_files(self):
        self.assertEqual(self.session.last_shot, 0)
        self.assertEqual(
            [self.session.next_shot() for _ in range(3)], [1, 2, 3]
        )
        self.assertEqual(self.session.last_shot, 3)
        self.assertEqual(
            self.session.shot_path(7, "board.svg").name, "007_board.svg"
        )

    def test_a_folder_that_already_holds_shots_never_clobbers_them(self):
        (self.session.shots_dir / "003_state.json").write_text("{}", "utf-8")
        reopened = journal_module.UiSession(self.session.folder,
                                            self.config_path)
        self.assertEqual(reopened.last_shot, 3)
        self.assertEqual(reopened.next_shot(), 4)

    def test_closing_records_the_end_once(self):
        self.session.close("Ctrl-C")
        self.session.close("again")
        ends = [r for r in self.session.records() if r["kind"] == "session.end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["data"]["reason"], "Ctrl-C")


# ---------------------------------------------------------------------------
# the raster
# ---------------------------------------------------------------------------


class RasterSizingTest(unittest.TestCase):
    """The document's own millimetres decide the dpi; the cap decides the rest."""

    def test_the_declared_size_is_read_in_millimetres(self):
        self.assertEqual(raster_module.document_size_mm(SAMPLE_SVG), (20.0, 10.0))

    def test_a_document_with_only_a_viewbox_still_has_a_size(self):
        markup = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 15"/>'
        self.assertEqual(raster_module.document_size_mm(markup), (30.0, 15.0))

    def test_a_document_with_no_size_is_refused(self):
        with self.assertRaises(ValueError):
            raster_module.document_size_mm('<svg xmlns="x"/>')

    def test_the_dpi_is_the_one_that_lands_on_the_cap(self):
        self.assertAlmostEqual(raster_module.dpi_for(25.4, 1600), 1600.0)
        self.assertAlmostEqual(raster_module.dpi_for(50.8, 800), 400.0)

    def test_a_document_with_no_extent_has_no_dpi(self):
        with self.assertRaises(ValueError):
            raster_module.dpi_for(0.0)


@unittest.skipUnless(raster_module.AVAILABLE, "no rasterizer in this interpreter")
class RasterTest(unittest.TestCase):
    """A real PNG, capped where it says it is capped."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def test_the_long_edge_lands_on_the_cap_and_the_aspect_survives(self):
        out = raster_module.rasterize(SAMPLE_SVG, self.folder / "shot.png")
        self.assertTrue(out.path.is_file())
        self.assertEqual(max(out.width_px, out.height_px),
                         raster_module.LONG_EDGE_PX)
        self.assertAlmostEqual(out.width_px / out.height_px, 2.0, places=1)

    def test_a_smaller_cap_is_honoured_exactly(self):
        out = raster_module.rasterize(SAMPLE_SVG, self.folder / "small.png",
                                      long_edge_px=240)
        self.assertLessEqual(max(out.width_px, out.height_px), 240)
        self.assertGreater(max(out.width_px, out.height_px), 200)

    def test_a_panel_sized_document_is_capped_the_same_way(self):
        markup = SAMPLE_SVG.replace('width="20.0000mm"', 'width="320.0000mm"') \
                           .replace('height="10.0000mm"', 'height="240.0000mm"') \
                           .replace('viewBox="0.000000 0.000000 20.000000 '
                                    '10.000000"',
                                    'viewBox="0 0 320 240"')
        out = raster_module.rasterize(markup, self.folder / "panel.png")
        self.assertEqual(max(out.width_px, out.height_px),
                         raster_module.LONG_EDGE_PX)

    def test_the_png_carries_no_alpha_so_it_reads_alone(self):
        from PIL import Image

        out = raster_module.rasterize(SAMPLE_SVG, self.folder / "shot.png")
        with Image.open(out.path) as image:
            self.assertEqual(image.mode, "RGB")
            # Off the diagonal the document draws: the background rect the
            # page put there, so the shot reads alone.
            self.assertEqual(image.getpixel((1500, 40)), (20, 22, 26))


class RasterMissingTest(unittest.TestCase):
    """No dev extras: the vector artifact and the state file are unaffected."""

    def test_rasterize_says_what_is_missing_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "shot.png"
            with mock.patch.object(raster_module, "AVAILABLE", False), \
                    mock.patch.object(raster_module, "UNAVAILABLE_REASON",
                                      "no rasterizer: pretend"):
                with self.assertRaises(raster_module.RasterUnavailable) as caught:
                    raster_module.rasterize(SAMPLE_SVG, out)
            self.assertIn("pretend", str(caught.exception))
            self.assertFalse(out.exists())


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------


class SnapshotTestCase(unittest.TestCase):
    """A recorded session over the stub renderer: no kicad-cli anywhere."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.recording = open_session(self.folder, config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()
        self.assertEqual(self.session.error, "")

    def tearDown(self):
        self._folder.cleanup()

    def shots(self) -> list:
        return sorted(p.name for p in self.recording.shots_dir.iterdir())

    def state_of(self, shot: int) -> dict:
        path = self.recording.shot_path(shot, snapshot_module.STATE_SUFFIX)
        return json.loads(path.read_text(encoding="utf-8"))


class SnapshotArtifactTest(SnapshotTestCase):
    """One shot number, three kinds of artifact, named for the viewport."""

    def test_a_snapshot_saves_the_vector_the_raster_and_the_state(self):
        reply = self.session.snapshot(shot_payload())
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["shot"], 1)
        self.assertIn("saved shot 001", reply["message"])
        self.assertIn("001_board.svg", self.shots())
        self.assertIn("001_state.json", self.shots())
        if raster_module.AVAILABLE:
            self.assertIn("001_board.png", self.shots())
            self.assertEqual(reply["files"]["png"], ["001_board.png"])
        self.assertEqual(reply["files"]["svg"], ["001_board.svg"])

    def test_the_svg_is_the_page_s_own_document_byte_for_byte(self):
        self.session.snapshot(shot_payload())
        saved = (self.recording.shot_path(1, "board.svg")
                 .read_text(encoding="utf-8"))
        self.assertEqual(saved, SAMPLE_SVG)

    def test_the_counter_increments_and_never_reuses_a_number(self):
        self.session.snapshot(shot_payload())
        self.session.snapshot(shot_payload())
        third = self.session.snapshot(shot_payload())
        self.assertEqual(third["shot"], 3)
        names = self.shots()
        for shot in ("001", "002", "003"):
            self.assertIn(f"{shot}_board.svg", names)
            self.assertIn(f"{shot}_state.json", names)

    def test_two_viewports_each_get_their_own_files(self):
        payload = shot_payload()
        payload["viewports"].append(
            {"name": "panel", "svg": SAMPLE_SVG, "view": dict(VIEW_STATE)}
        )
        reply = self.session.snapshot(payload)
        self.assertEqual(reply["files"]["svg"],
                         ["001_board.svg", "001_panel.svg"])
        self.assertIn("001_panel.svg", self.shots())

    def test_a_viewport_name_can_never_reach_outside_the_shots_folder(self):
        self.session.snapshot(shot_payload(name="../../etc/passwd"))
        for name in self.shots():
            self.assertNotIn("..", name)
            self.assertTrue(name.startswith("001_"))
        self.assertFalse((self.folder / "etc").exists())

    def test_the_raster_is_capped_where_the_module_says_it_is(self):
        if not raster_module.AVAILABLE:
            self.skipTest("no rasterizer in this interpreter")
        self.session.snapshot(shot_payload())
        images = self.state_of(1)["raster"]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(
            max(images[0]["width_px"], images[0]["height_px"]),
            raster_module.LONG_EDGE_PX,
        )

    def test_no_rasterizer_still_saves_the_shot_and_says_why_not(self):
        with mock.patch.object(raster_module, "AVAILABLE", False), \
                mock.patch.object(raster_module, "UNAVAILABLE_REASON",
                                  "no rasterizer: pretend"):
            reply = self.session.snapshot(shot_payload())
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["files"]["png"], [])
        self.assertIn("001_board.svg", self.shots())
        self.assertIn("001_state.json", self.shots())
        self.assertNotIn("001_board.png", self.shots())
        state = self.state_of(1)
        self.assertFalse(state["raster"]["available"])
        self.assertIn("pretend", state["raster"]["reason"])
        self.assertTrue(any("no PNG" in w for w in state["warnings"]))

    def test_a_snapshot_leaves_the_project_alone(self):
        before = (self.board_path.read_bytes(), self.config_path.read_bytes())
        self.session.snapshot(shot_payload())
        self.assertEqual(
            (self.board_path.read_bytes(), self.config_path.read_bytes()),
            before,
        )


class SnapshotStateTest(SnapshotTestCase):
    """The state file: config, results and the client's view, in one place."""

    def setUp(self):
        super().setUp()
        self.session.snapshot(shot_payload())
        self.state = self.state_of(1)

    def test_it_says_what_it_is(self):
        self.assertEqual(self.state["schema"], snapshot_module.SCHEMA)
        self.assertEqual(self.state["shot"], 1)
        self.assertTrue(self.state["recorded_at"].endswith("Z"))

    def test_it_carries_the_config_as_currently_loaded(self):
        config = self.state["config"]
        self.assertEqual(config["grid"]["columns"], 1)
        self.assertEqual(config["grid"]["rows"], 1)
        self.assertEqual(config["source_board"], "../board.kicad_pcb")
        self.assertAlmostEqual(
            config["prep"]["cutter_diameter"],
            load_config(self.config_path).prep.cutter_diameter,
        )

    def test_it_carries_the_computation_s_results(self):
        results = self.state["results"]
        self.assertTrue(results["ok"])
        self.assertEqual(results["generation"], self.session.generation)
        self.assertTrue(results["subtitle"])
        self.assertIn("Edge.Cuts", [row["canonical"] for row in results["layers"]])
        self.assertTrue(results["proposals"])
        for proposal in results["proposals"]:
            # Verdict AND geometry: the reviewer has to be able to see which
            # cut a verdict was about.
            self.assertIn("verdict", proposal)
            self.assertIn("blade", proposal)
            self.assertIn("start", proposal["blade"])

    def test_it_carries_the_check_outcomes_and_the_headline_measurements(self):
        checks = self.state["results"]["checks"]
        for name in ("scores_untouched", "guard_clear", "scores_clear",
                     "junctions_clear", "min_tab_clear", "widths_agree"):
            self.assertIsInstance(checks[name], bool)
        self.assertEqual(checks["stale_scores"], 0)
        measurements = checks["measurements"]
        self.assertAlmostEqual(
            measurements["cutter_diameter_mm"],
            load_config(self.config_path).prep.cutter_diameter,
        )
        self.assertIn("cut_length_mm", measurements)

    def test_it_carries_the_whole_prep_result_with_its_field_names(self):
        prep = self.state["results"]["prep"]
        self.assertEqual(prep["_type"], "PrepResult")
        for name in ("tabs", "stops", "bridges", "junctions", "candidates",
                     "scores", "run_lengths", "warnings"):
            self.assertIn(name, prep)

    def test_it_carries_the_run_s_identity(self):
        session = self.state["session"]
        # Resolved, because the session resolves what it was handed - the
        # short-path form Windows hands a temporary folder is not an identity.
        self.assertEqual(session["config_path"], str(self.session.config_path))
        self.assertEqual(session["board_path"],
                         str(self.board_path.resolve()))
        self.assertEqual(session["folder"], str(self.recording.folder))
        self.assertEqual(session["generation"], self.session.generation)
        self.assertTrue(session["eedesignpanels_version"])

    def test_it_carries_the_client_view_state_as_posted(self):
        client = self.state["client"]
        self.assertEqual(client["cause"], "hotkey")
        self.assertEqual(client["layout"], "side-by-side")
        self.assertEqual(client["panel"]["active"], "Edge.Cuts")
        viewport = client["viewports"][0]
        self.assertEqual(viewport["name"], "board")
        self.assertEqual(viewport["view"]["viewBox"], [0.0, 0.0, 20.0, 10.0])
        self.assertEqual(viewport["view"]["zoom_pct"], 100.0)
        self.assertEqual(viewport["view"]["layers"][1]["visible"], False)

    def test_the_document_itself_is_named_and_not_repeated(self):
        # The SVG is already a file. Repeating a megabyte of markup inside the
        # first artifact a reviewer opens would make it the one they cannot
        # read.
        viewport = self.state["client"]["viewports"][0]
        self.assertEqual(viewport["svg"], "001_board.svg")
        self.assertGreater(viewport["svg_bytes"], 100)
        self.assertNotIn("<path", json.dumps(self.state))

    def test_it_is_pretty_printed_with_a_stable_key_order(self):
        text = (self.recording.shot_path(1, snapshot_module.STATE_SUFFIX)
                .read_text(encoding="utf-8"))
        self.assertTrue(text.startswith("{\n"))
        self.assertTrue(text.endswith("}\n"))
        order = [text.index(f'"{key}"')
                 for key in ("artifacts", "client", "config", "raster",
                             "recorded_at", "results", "schema")]
        self.assertEqual(order, sorted(order))

    def test_the_artifacts_it_names_are_the_files_on_disk(self):
        artifacts = self.state["artifacts"]
        for name in artifacts["svg"] + artifacts["png"] + [artifacts["state"]]:
            self.assertTrue((self.recording.shots_dir / name).is_file(), name)


class SnapshotRefusalTest(SnapshotTestCase):
    """What a snapshot will not save, and what it leaves behind when it refuses."""

    def refuse(self, payload):
        with self.assertRaises(actions.ActionError) as caught:
            self.session.snapshot(payload)
        return str(caught.exception)

    def test_a_viewport_with_no_svg_document_is_refused(self):
        message = self.refuse({"viewports": [{"name": "board", "svg": ""}]})
        self.assertIn("no SVG document", message)

    def test_something_that_is_not_an_svg_is_refused(self):
        self.refuse({"viewports": [{"name": "board", "svg": "<html></html>"}]})

    def test_an_svg_carrying_a_script_is_refused(self):
        # A shot is opened in a browser later. It carries drawing and nothing
        # that runs, exactly like the frozen review artifact.
        message = self.refuse({"viewports": [
            {"name": "board", "svg": '<svg><script>fetch("/")</script></svg>'}
        ]})
        self.assertIn("script", message)

    def test_a_refusal_is_recorded_and_writes_no_artifact(self):
        self.refuse({"viewports": [{"name": "board", "svg": ""}]})
        self.assertEqual(
            [p for p in self.recording.shots_dir.iterdir()], []
        )
        refused = [r for r in self.recording.records()
                   if r["kind"] == "snapshot.refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["data"]["shot"], 1)

    def test_more_viewports_than_a_page_can_have_is_refused(self):
        payload = shot_payload()
        payload["viewports"] *= snapshot_module.MAX_VIEWPORTS + 1
        self.assertIn("viewports", self.refuse(payload))

    def test_a_state_only_shot_says_it_has_no_picture(self):
        reply = self.session.snapshot({"viewports": [], "cause": "button"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["files"]["svg"], [])
        self.assertTrue(any("no viewport" in w for w in reply["warnings"]))
        self.assertIn("001_state.json", self.shots())

    def test_a_session_with_no_folder_cannot_snapshot_or_note(self):
        loose = PreviewSession(self.config_path, self.folder / "loose",
                               render=stub_render)
        loose.refresh()
        with self.assertRaises(actions.ActionError):
            loose.snapshot(shot_payload())
        with self.assertRaises(actions.ActionError):
            loose.note({"text": "nowhere to keep this"})


class NoteTest(SnapshotTestCase):
    """One line of the reviewer's own words, tied to the newest shot."""

    def test_a_note_ties_to_the_latest_shot(self):
        self.session.snapshot(shot_payload())
        self.session.snapshot(shot_payload())
        reply = self.session.note({"text": "the left tab is wrong"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["shot"], 2)
        self.assertIn("002", reply["message"])
        record = self.recording.records()[-1]
        self.assertEqual(record["kind"], "note")
        self.assertEqual(record["data"]["text"], "the left tab is wrong")
        self.assertEqual(record["data"]["shot"], 2)

    def test_a_note_before_any_snapshot_is_kept_and_says_so(self):
        reply = self.session.note({"text": "starting from a clean board"})
        self.assertEqual(reply["shot"], 0)
        self.assertIn("no snapshot", reply["message"])
        self.assertEqual(self.recording.records()[-1]["data"]["shot"], 0)

    def test_an_empty_note_is_refused(self):
        for text in ("", "   ", None, 12):
            with self.subTest(text=text):
                with self.assertRaises(actions.ActionError):
                    self.session.note({"text": text})

    def test_a_very_long_note_is_kept_short(self):
        self.session.note({"text": "z" * 5000})
        kept = self.recording.records()[-1]["data"]["text"]
        self.assertEqual(len(kept), 1000)


class ClientEventTest(SnapshotTestCase):
    """A batch of interaction events, recorded and nothing else."""

    def test_a_batch_becomes_one_line_each(self):
        reply = self.session.journal_events({"events": [
            {"type": "control", "id": "pb-reload", "where": "controls"},
            {"type": "layers", "mode": 1, "hidden": ["User.8"]},
            {"type": "view", "viewport": "board", "zoom_pct": 240.0},
        ]})
        self.assertEqual(reply["logged"], 3)
        kinds = [r["kind"] for r in self.recording.records()[-3:]]
        self.assertEqual(kinds, ["client.control", "client.layers",
                                 "client.view"])
        last = self.recording.records()[-1]["data"]
        self.assertEqual(last["viewport"], "board")
        self.assertEqual(last["zoom_pct"], 240.0)

    def test_recording_an_interaction_changes_nothing(self):
        before = self.session.generation
        board = self.board_path.read_bytes()
        self.session.journal_events({"events": [{"type": "view"}]})
        self.assertEqual(self.session.generation, before)
        self.assertEqual(self.board_path.read_bytes(), board)

    def test_an_event_with_no_type_still_lands(self):
        self.session.journal_events({"events": [{"note": "unnamed"}]})
        self.assertEqual(self.recording.records()[-1]["kind"], "client.event")

    def test_rubbish_is_refused_by_name(self):
        for payload in ({}, {"events": "lots"}, {"events": ["nope"]}):
            with self.subTest(payload=payload):
                with self.assertRaises(actions.ActionError):
                    self.session.journal_events(payload)

    def test_a_batch_bigger_than_the_page_sends_is_refused(self):
        events = [{"type": "view"}] * (session_module.MAX_CLIENT_EVENTS + 1)
        with self.assertRaises(actions.ActionError) as caught:
            self.session.journal_events({"events": events})
        self.assertIn("batch", str(caught.exception))


# ---------------------------------------------------------------------------
# over the wire
# ---------------------------------------------------------------------------


class RecordedServerTest(ServerTestCase):
    """The same in-process server as :mod:`test_preview`, with a folder."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.recording = open_session(self.folder, config_path=self.config_path)
        self.session = PreviewSession(
            self.config_path, self.recording.folder,
            render=stub_render, journal=self.recording,
        )
        self.session.refresh()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def records(self, kind: str = "") -> list:
        return [r for r in self.recording.records()
                if not kind or r["kind"] == kind]


class ServerJournalTest(RecordedServerTest):
    """Every action, over a socket, with a line to show for it."""

    def test_an_action_is_journaled_with_its_endpoint_and_outcome(self):
        status, reply = self.post("reload")
        self.assertEqual(status, 200)
        actions_logged = self.records("server.action")
        self.assertTrue(actions_logged)
        last = actions_logged[-1]["data"]
        self.assertEqual(last["endpoint"], "/reload")
        self.assertTrue(last["ok"])
        self.assertEqual(last["generation"], reply["generation"])
        self.assertGreaterEqual(last["seconds"], 0.0)

    def test_a_refused_action_is_journaled_as_refused(self):
        status, _ = self.post("set_param", {"cutter_diameter": -1.0})
        self.assertEqual(status, 400)
        last = self.records("server.action")[-1]["data"]
        self.assertEqual(last["endpoint"], "/set_param")
        self.assertFalse(last["ok"])
        self.assertTrue(last["error"])

    def test_a_computation_is_journaled_with_what_it_produced(self):
        self.post("reload")
        last = self.records("compute")[-1]["data"]
        self.assertTrue(last["ok"])
        self.assertGreaterEqual(last["candidates"], 1)
        self.assertGreaterEqual(last["toolpath_items"], 1)

    def test_a_poll_is_not_an_action(self):
        before = len(self.recording.records())
        self.get("state")
        self.get("state")
        self.assertEqual(len(self.recording.records()), before)

    def test_a_batch_of_interactions_posts_and_does_not_move_the_picture(self):
        _, state = self.get("state")
        generation = json.loads(state)["generation"]
        status, reply = self.post("journal", {"events": [
            {"type": "control", "id": "pb-report"},
            {"type": "view", "viewport": "board", "cause": "wheel"},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(reply["logged"], 2)
        self.assertEqual(len(self.records("client.control")), 1)
        self.assertEqual(len(self.records("client.view")), 1)
        _, state = self.get("state")
        self.assertEqual(json.loads(state)["generation"], generation)

    def test_a_batch_is_summarised_in_the_action_line_not_repeated(self):
        self.post("journal", {"events": [{"type": "view"}] * 3})
        payload = self.records("server.action")[-1]["data"]["payload"]
        self.assertEqual(payload, {"events": 3})

    def test_a_snapshot_over_http_saves_all_three_kinds(self):
        status, reply = self.post("snapshot", shot_payload())
        self.assertEqual(status, 200)
        self.assertEqual(reply["shot"], 1)
        self.assertIn("saved shot 001", reply["message"])
        names = sorted(p.name for p in self.recording.shots_dir.iterdir())
        self.assertIn("001_board.svg", names)
        self.assertIn("001_state.json", names)
        if raster_module.AVAILABLE:
            self.assertIn("001_board.png", names)
        logged = self.records("snapshot")[-1]["data"]
        self.assertEqual(logged["shot"], 1)
        self.assertEqual(logged["svg"], ["001_board.svg"])

    def test_the_snapshot_action_line_names_the_viewports_not_their_markup(self):
        self.post("snapshot", shot_payload())
        payload = self.records("server.action")[-1]["data"]["payload"]
        self.assertEqual(payload["viewports"], ["board"])
        self.assertGreater(payload["svg_bytes"], 100)
        self.assertNotIn("<path", json.dumps(payload))

    def test_a_note_over_http_ties_to_the_shot_just_taken(self):
        self.post("snapshot", shot_payload())
        status, reply = self.post("note", {"text": "003 is the one to fix"})
        self.assertEqual(status, 200)
        self.assertEqual(reply["shot"], 1)
        self.assertEqual(self.records("note")[-1]["data"]["text"],
                         "003 is the one to fix")

    def test_an_empty_note_is_a_400_over_http_too(self):
        status, reply = self.post("note", {"text": " "})
        self.assertEqual(status, 400)
        self.assertFalse(reply["ok"])

    def test_a_snapshot_body_may_be_large_and_a_normal_body_may_not(self):
        # The snapshot route carries the page's serialization of every visible
        # layer; nothing else on this server carries more than a few numbers.
        self.assertGreater(server_module.BODY_LIMITS["/snapshot"],
                           server_module.MAX_BODY_BYTES)
        request = urllib.request.Request(
            self.url + "reload", method="POST",
            data=b"{}" + b" " * (server_module.MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


class ServedPageInstrumentationTest(RecordedServerTest):
    """What the page has to carry for any of this to be reachable."""

    def setUp(self):
        super().setUp()
        _, self.markup = self.get("")

    def test_the_report_button_is_in_the_header_and_the_note_in_its_dialog(self):
        # The header holds the report button only. The note box is in the
        # report dialog, which is the only place a note is typed - so it is
        # served AFTER the views, not in the header with the button that
        # opens it.
        header = self.markup.split("<div class=\"views\"")[0]
        self.assertIn("pb-report-btn", header)
        self.assertIn(page_served.REPORT_LABEL, header)
        self.assertNotIn("pb-note", header)
        self.assertIn("pb-note", self.markup)

    def test_the_page_states_the_hotkey(self):
        # The button is a bare name; the hotkey rides in its `title` and in
        # the one footer sentence, not as a parenthetical on the label.
        self.assertIn(
            f"Hotkey: {page_served.REPORT_KEY.upper()}", self.markup
        )
        self.assertIn(f"<b>{page_served.REPORT_KEY.upper()}</b> reports a "
                      f"problem", self.markup)
        self.assertIn(f"const REPORT_KEY = '{page_served.REPORT_KEY}'",
                      self.markup)

    def test_the_footer_shows_the_session_folder(self):
        self.assertIn(str(self.recording.folder), self.markup)
        self.assertIn("pb-shot", self.markup)

    def test_the_page_knows_the_three_instrumentation_endpoints(self):
        # The page posts a PROBLEM REPORT, not a snapshot. `/snapshot` is
        # still a server route - the report takes its shot through it - but
        # no control on the page calls it directly.
        for endpoint in ("/journal", "/bug_report", "/note"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.markup)

    def test_the_viewport_is_named_so_a_shot_can_be_named_after_it(self):
        self.assertIn(f'data-viewport="{page_served.BOARD_VIEWPORT}"',
                      self.markup)

    def test_the_page_still_references_nothing_outside_itself(self):
        from eedesignpanels.preview.icons import BOARD_ICON
        self.assertIn(BOARD_ICON, self.markup)
        markup = self.markup.replace(BOARD_ICON, "")
        for forbidden in ("<link", "<script src", "@import", "url(http"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, markup)

    def test_opening_the_page_is_journaled(self):
        self.assertTrue(self.records("server.get"))
        self.assertEqual(self.records("server.get")[-1]["data"]["endpoint"], "/")


# ---------------------------------------------------------------------------
# the frozen artifact is untouched
# ---------------------------------------------------------------------------


MINIMAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" '
    'viewBox="0 0 10 10"><path d="M0 0 L10 10"/></svg>'
)
INSTRUMENTATION = ("/journal", "/note", "/bug_report",
                   "pb-report", "pb-note", "logEvent", "serializeCard",
                   # The controls the busy state blocks, and the page that
                   # replaces this one when the server stops: both belong to
                   # the served host, and a frozen artifact has neither a
                   # control nor a server to lose.
                   "data-act", "applyBusy", "deadPage")


class FrozenArtifactTest(unittest.TestCase):
    """Served-mode only: a frozen page is still a file with nothing to talk to.

    The instrumentation lives entirely in
    :mod:`eedesignpanels.preview.page_served`, so both halves are checked - the
    served page has it (above), and the shared components plus a real written
    review page do not.
    """

    def frozen_page(self) -> str:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            svg = root / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            layers = [render.LayerSpec(
                ordinal=25, canonical_name="Edge.Cuts", layer_type="user",
                display_name="Edge.Cuts",
            )]
            out = review_page.build_page(
                out_path=root / "page.html",
                heading="Review - frozen",
                subtitle="the portable artifact",
                layers=layers,
                views=[review_page.BoardView(
                    title="board", svg_by_layer={"Edge.Cuts": svg}
                )],
                theme=theme.load_theme(),
            )
            return out.read_text(encoding="utf-8")

    def test_a_written_review_page_carries_no_instrumentation(self):
        markup = self.frozen_page()
        for token in INSTRUMENTATION:
            with self.subTest(token=token):
                self.assertNotIn(token, markup)

    def test_the_shared_components_carry_none_of_it_either(self):
        for source in (review_page.JS, review_page.CSS, layerpanel.PANEL_JS,
                       layerpanel.PANEL_CSS):
            for token in INSTRUMENTATION:
                with self.subTest(token=token):
                    self.assertNotIn(token, source)

    def test_the_served_host_is_where_it_all_lives(self):
        combined = page_served.CONTROLS_JS + page_served.CONTROLS_CSS
        for token in INSTRUMENTATION:
            with self.subTest(token=token):
                self.assertIn(token, combined)


# ---------------------------------------------------------------------------
# what the page serializes
# ---------------------------------------------------------------------------


def js_function(source: str, name: str) -> str:
    """One `function name(...) {...}` out of a script, by brace matching.

    The snapshot's fidelity lives in the browser - which layers, at which
    opacity, in which frame - and no browser runs in this suite. So the
    function is lifted out and RUN, against a DOM small enough to state
    exactly.

    An `async` function comes out async: lifting `post` without its keyword
    gives a body full of `await` and a syntax error rather than a request.
    """
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    depth, i = 0, source.index("{", start)
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1


# A viewport 1200 x 400 px showing a 20 x 10 mm box: `xMidYMid meet` fits the
# height, so 30 mm of board is on screen and the shot has to be those 30 mm.
HARNESS = r"""
%(functions)s
const PB = {generation: 7};
const LAYERS = [{canonical: 'Edge.Cuts', physical: true, top: false},
                {canonical: 'User.8', physical: false, top: false}];
globalThis.XMLSerializer = class {
  serializeToString(node) { return node.markup; }
};
globalThis.getComputedStyle = () => ({backgroundColor: 'rgb(20, 22, 26)'});
function fakeSvg(layer, opts) {
  return {
    dataset: {layer: layer, base: '0 0 20 10'},
    style: {display: opts.display || '', opacity: opts.opacity || '',
            zIndex: opts.z || ''},
    getAttribute: name => name === 'viewBox' ? '0 0 20 10' : null,
    childNodes: [{markup: opts.markup}],
  };
}
const stack = [
  fakeSvg('Edge.Cuts', {z: '99', markup: '<path d="M0 0 L20 10"/>'}),
  fakeSvg('User.8', {z: '1', opacity: '0.22',
                     markup: '<path d="M1 1 L2 2"/>'}),
  fakeSvg('Proposals.VScore', {z: '1', display: 'none',
                               markup: '<path d="M9 9 L9 0"/>'}),
];
const viewport = {
  querySelectorAll: () => stack,
  querySelector: () => stack[0],
  getBoundingClientRect: () => ({width: 1200, height: 400, left: 0, top: 0}),
};
const card = {
  dataset: {viewport: 'board'},
  querySelector: sel => sel === '.viewport' ? viewport : null,
};
console.log(JSON.stringify(serializeCard(card, 0)));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientSerializationTest(unittest.TestCase):
    """`serializeCard`, run against a stated DOM: the shot is what was on screen."""

    @classmethod
    def setUpClass(cls):
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("numbers", "escapeAttr", "cardName", "serializeCard")
        )
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "harness.mjs"
            script.write_text(HARNESS % {"functions": functions},
                              encoding="utf-8")
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), str(script)],
                capture_output=True, text=True, timeout=60,
            )
        if done.returncode != 0:
            raise AssertionError(f"the page's own script failed: {done.stderr}")
        cls.shot = json.loads(done.stdout)

    def test_the_script_parses_as_the_page_serves_it(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "controls.js"
            script.write_text(
                page_served.CONTROLS_JS.replace("__STATE__", "{}")
                .replace("__REPORT_KEY__", page_served.REPORT_KEY),
                encoding="utf-8",
            )
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), "--check", str(script)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_document_is_the_visible_extent_and_not_the_bare_viewbox(self):
        # 1200 x 400 px on a 20 x 10 mm box fits the height: 30 mm wide, and
        # centred, so the frame starts 5 mm to the left of the board.
        self.assertIn('viewBox="-5.000000 0.000000 30.000000 10.000000"',
                      self.shot["svg"])
        self.assertIn('width="30.0000mm" height="10.0000mm"', self.shot["svg"])
        self.assertEqual(self.shot["view"]["viewBox"], [-5.0, 0.0, 30.0, 10.0])
        self.assertEqual(self.shot["view"]["size_px"], [1200, 400])

    def test_it_carries_the_visible_layers_in_painting_order(self):
        svg = self.shot["svg"]
        # z-index 1 is painted under z-index 99, whatever the DOM order was.
        self.assertLess(svg.index('data-layer="User.8"'),
                        svg.index('data-layer="Edge.Cuts"'))
        self.assertIn('<g data-layer="User.8" opacity="0.22">', svg)
        self.assertIn('<path d="M1 1 L2 2"/>', svg)
        self.assertIn('<path d="M0 0 L20 10"/>', svg)

    def test_a_hidden_layer_is_not_in_the_shot_but_is_in_the_record(self):
        self.assertNotIn("Proposals.VScore", self.shot["svg"])
        hidden = [row for row in self.shot["view"]["layers"]
                  if row["canonical"] == "Proposals.VScore"]
        self.assertEqual(len(hidden), 1)
        self.assertFalse(hidden[0]["visible"])

    def test_it_paints_the_background_so_the_file_reads_alone(self):
        self.assertIn('fill="rgb(20, 22, 26)"', self.shot["svg"])
        self.assertEqual(self.shot["view"]["background"], "rgb(20, 22, 26)")

    def test_it_is_named_after_the_viewport_it_came_from(self):
        self.assertEqual(self.shot["name"], "board")
        self.assertEqual(
            snapshot_module.clean_name(self.shot["name"]), "board"
        )

    def test_the_document_it_produces_is_one_the_rasterizer_accepts(self):
        self.assertEqual(
            raster_module.document_size_mm(self.shot["svg"]), (30.0, 10.0)
        )
        if not raster_module.AVAILABLE:
            self.skipTest("no rasterizer in this interpreter")
        with tempfile.TemporaryDirectory() as folder:
            out = raster_module.rasterize(
                self.shot["svg"], Path(folder) / "shot.png"
            )
            self.assertEqual(max(out.width_px, out.height_px),
                             raster_module.LONG_EDGE_PX)


# ---------------------------------------------------------------------------
# what the page's controls do
# ---------------------------------------------------------------------------
# Same technique and the same reason as `serializeCard` above: these three
# behaviours live in the browser, the browser gate is retired, and a string
# assertion cannot tell "wired" from "wired wrongly". They live in this module
# because `js_function` and the node harness do.


def run_js(source: str) -> dict:
    """Run one harness under node and return the JSON it printed."""
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "harness.mjs"
        script.write_text(source, encoding="utf-8")
        done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
            [shutil.which("node"), str(script)],
            capture_output=True, text=True, timeout=60,
        )
    if done.returncode != 0:
        raise AssertionError(f"the page's own script failed: {done.stderr}")
    return json.loads(done.stdout)


def js_const(name: str, end: str) -> str:
    """One `const NAME = ...` declaration, lifted verbatim from the script.

    DATA, not a function: a harness that restated the three-way question's own
    wording would be testing its own copy of the page rather than the page.
    """
    source = page_served.CONTROLS_JS
    start = source.index(f"const {name} =")
    return source[start:source.index(end, start) + len(end)]


def lift(*names: str) -> str:
    return "\n".join(
        js_function(page_served.CONTROLS_JS, name) for name in names
    )


BUSY_HARNESS = """
%(functions)s
let busy = false;
// One accept for an edge that is already scored (its own state is disabled),
// one decline for the same edge, one parameter field.
const nodes = [
  {dataset: {act: 'accept', off: '1'}, disabled: false},
  {dataset: {act: 'decline', off: ''}, disabled: false},
  {dataset: {act: 'param'}, disabled: false},
];
function marker() {
  return {seen: {}, attrs: {}, textContent: '',
          classList: {toggle(name, on) { this.owner.seen[name] = on; }},
          setAttribute(name, value) { this.attrs[name] = value; }};
}
function wired() { const m = marker(); m.classList.owner = m; return m; }
const controlsEl = wired();
const veilEl = wired();
const veilWhatEl = {textContent: ''};
globalThis.document = {
  querySelectorAll: sel => sel === '[data-act]' ? nodes : [],
};
const out = {};
setBusy(true, 'accepting #2');
out.during = nodes.map(n => n.disabled);
out.busy_class = controlsEl.seen.busy;
out.aria = controlsEl.attrs['aria-busy'];
out.veil_on = veilEl.seen.on;
out.veil_hidden = veilEl.attrs['aria-hidden'];
out.veil_what = veilWhatEl.textContent;
setBusy(false);
out.after = nodes.map(n => n.disabled);
out.busy_class_after = controlsEl.seen.busy;
out.aria_after = controlsEl.attrs['aria-busy'];
out.veil_on_after = veilEl.seen.on;
out.veil_hidden_after = veilEl.attrs['aria-hidden'];
console.log(JSON.stringify(out));
"""

PARAM_HARNESS = """
%(functions)s
const PB = {params: {cutter_diameter: 0.8, route_past_score_mm: 0.5,
                     route_along_score_mm: 0, route_tangent_angle_deg: 10,
                     score_rule_half_width_mm: 0.3,
                     min_tab_mm: 0.5, eliminate_slivers: true,
                     outline_heal_mm: 0.01, pocket_cutouts: false}};
const cutterEl = {value: '%(cutter)s'};
const pastEl = {value: '%(past)s'};
const alongEl = {value: '0'};
const tangentEl = {value: '10'};
const scoreWidthEl = {value: '%(scorew)s'};
const minTabEl = {value: '%(mintab)s'};
const healEl = {value: '%(heal)s'};
const elimEl = {checked: %(eliminate)s};
const pocketEl = {checked: %(pocket)s};
console.log(JSON.stringify(changedParams()));
"""

# The save button says what is waiting, so the number is never somewhere the
# user has to go and look for it.
PENDING_HARNESS = """
%(functions)s
const PB = {};
const saveEl = {textContent: '', title: '', classes: {},
                classList: {toggle(name, on) { saveEl.classes[name] = on; }}};
const out = [];
[0, 1, 3].forEach(n => {
  drawPending(n, n ? ['a change'] : []);
  out.push({label: saveEl.textContent, dirty: saveEl.classes.dirty,
            title: saveEl.title, held: PB.pending,
            changes: PB.pendingChanges.length});
});
console.log(JSON.stringify(out));
"""

# The three-way question, and what each answer does.
ASK_HARNESS = """
%(functions)s
let stopped = false;
const PB = {pending: 2, pendingChanges: ['scored one line', 'config: x=1']};
const calls = [];
function logEvent(type, data) { calls.push({log: type, data: data}); }
function say(text) { calls.push({say: text}); }
async function save(cause) { calls.push({save: cause}); return {ok: true}; }
async function stopServer() { calls.push({stop: true}); return true; }
function node() {
  return {textContent: '', children: [],
          appendChild(kid) { this.children.push(kid); }};
}
async function reloadFromDisk() { calls.push({reload: true}); return true; }
let asking = null;
const askEl = {classes: {}, attrs: {},
               classList: {add(n) { askEl.classes[n] = true; },
                           remove(n) { askEl.classes[n] = false; }},
               setAttribute(n, v) { askEl.attrs[n] = v; }};
const askListEl = node();
const askLineEl = node();
const askSaveEl = node();
const askDiscardEl = node();
globalThis.document = {createElement: node};
askUnsaved('quit');
const shown = {on: askEl.classes.on, hidden: askEl.attrs['aria-hidden'],
               listed: askListEl.children.length,
               save: askSaveEl.textContent, discard: askDiscardEl.textContent,
               line: askLineEl.textContent};
closeAsk();
// ...and the same question, asked by Reload: the verb follows the control.
askUnsaved('reload');
const asked = {save: askSaveEl.textContent, discard: askDiscardEl.textContent,
               line: askLineEl.textContent};
closeAsk();
console.log(JSON.stringify({
  shown: shown, asked: asked, closed: askEl.classes.on,
  hidden_after: askEl.attrs['aria-hidden'], calls: calls,
}));
"""

DEAD_HARNESS = """
%(functions)s
let stopped = false;
let eventStream = {closed: false, close() { this.closed = true; }};
let pollTimer = null;
let flushTimer = null;
// `goQuiet` calls `streamAlive`, which is a no-op with no grace timer
// pending - the ordinary state for a page that was not mid-way through
// waiting out a gap in its own stream.
let streamGraceTimer = null;
// The notice flag is declared once, not shadowed: `deadPage` reads and sets
// this module-level flag.
let dead = false;
const PB = {session: 'C:/scratch/20260908_ui_session'};
function node(tag) {
  return {tag: tag, className: '', textContent: '', children: [],
          append(...kids) { this.children.push(...kids); },
          appendChild(kid) { this.children.push(kid); }};
}
globalThis.document = {
  title: 'Preview - board',
  body: node('body'),
  createElement: node,
};
document.body.textContent = 'the whole live page';
deadPage('nothing on this page is live - close this tab.');
function text(el) {
  return el.textContent + el.children.map(text).join(' ');
}
console.log(JSON.stringify({
  stopped: stopped,
  stream_closed: eventStream === null,
  title: document.title,
  body_text: document.body.textContent,
  kids: document.body.children.length,
  dead_class: document.body.children[0].className,
  words: text(document.body.children[0]),
}));
"""


@unittest.skipUnless(shutil.which("node"), "no node to parse the page's script")
class ClientScriptParsesTest(unittest.TestCase):
    """The whole script, parsed. The harnesses below only run pieces of it.

    A syntax error anywhere in `CONTROLS_JS` takes every control on the page
    with it and nothing in Python would notice: the string is served, not
    executed. `node --check` is the cheapest possible fence on that.
    """

    def test_the_served_script_is_valid_javascript(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "controls.mjs"
            script.write_text(
                page_served.CONTROLS_JS.replace("__STATE__", "{}").replace(
                    "__REPORT_KEY__", page_served.REPORT_KEY),
                encoding="utf-8",
            )
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), "--check", str(script)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientBusyTest(unittest.TestCase):
    """The busy state, run: what it disables and what it gives back."""

    def setUp(self):
        self.out = run_js(BUSY_HARNESS % {"functions": lift("applyBusy",
                                                            "setBusy")})

    def test_every_changing_control_goes_dead_for_the_action(self):
        self.assertEqual(self.out["during"], [True, True, True])
        self.assertTrue(self.out["busy_class"])
        self.assertEqual(self.out["aria"], "true")

    def test_the_display_gets_a_popup_over_it_saying_what_is_running(self):
        # A disabled control column says nothing about the picture, and the
        # picture is what is being read.
        self.assertTrue(self.out["veil_on"])
        self.assertEqual(self.out["veil_hidden"], "false")
        self.assertEqual(self.out["veil_what"], "accepting #2")

    def test_afterwards_each_control_returns_to_its_OWN_state(self):
        # The one that matters: accept for an already-scored edge must not come
        # back enabled just because the action finished.
        self.assertEqual(self.out["after"], [True, False, False])
        self.assertFalse(self.out["busy_class_after"])
        self.assertEqual(self.out["aria_after"], "false")

    def test_the_popup_goes_when_the_action_does(self):
        self.assertFalse(self.out["veil_on_after"])
        self.assertEqual(self.out["veil_hidden_after"], "true")


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientParamTest(unittest.TestCase):
    """What Enter, the button and a blur actually send."""

    def changed(self, cutter, past, mintab="0.5", eliminate="true",
                scorew="0.3", heal="0.01", pocket="false"):
        return run_js(PARAM_HARNESS % {
            "functions": lift("changedParams"), "cutter": cutter, "past": past,
            "scorew": scorew, "mintab": mintab, "eliminate": eliminate,
            "heal": heal, "pocket": pocket,
        })

    def test_only_what_the_config_does_not_already_say_is_sent(self):
        self.assertEqual(self.changed("0.8", "0.75"),
                         {"route_past_score_mm": 0.75})
        self.assertEqual(self.changed("1.2", "0.5"), {"cutter_diameter": 1.2})
        self.assertEqual(self.changed("0.8", "0.5", "0.9"), {"min_tab_mm": 0.9})

    def test_the_sliver_switch_travels_as_a_boolean_when_it_is_cleared(self):
        # A SWITCH IS NOT A NUMBER: what it sends is false, not 0, and clearing
        # it is a change even though nothing was typed anywhere.
        self.assertEqual(self.changed("0.8", "0.5", "0.5", "false"),
                         {"eliminate_slivers": False})
        self.assertEqual(self.changed("0.8", "0.5", "0.5", "true"), {})

    def test_both_travel_when_both_changed(self):
        self.assertEqual(self.changed("1.2", "0.75"),
                         {"cutter_diameter": 1.2, "route_past_score_mm": 0.75})

    def test_an_unchanged_set_is_no_recompute_at_all(self):
        self.assertEqual(self.changed("0.8", "0.5"), {})

    def test_a_field_that_is_not_a_number_sends_nothing_of_its_own(self):
        self.assertEqual(self.changed("", "0.75"), {"route_past_score_mm": 0.75})
        self.assertEqual(self.changed("nonsense", "0.5"), {})

    def test_the_pocketing_switch_travels_as_a_boolean_when_it_is_set(self):
        # A tick, not a distance, and clear is the default - so setting it is
        # the change that travels and clear sends nothing.
        self.assertEqual(self.changed("0.8", "0.5", pocket="true"),
                         {"pocket_cutouts": True})
        self.assertEqual(self.changed("0.8", "0.5", pocket="false"), {})

    def test_the_score_keepout_width_travels_from_this_page(self):
        # A prep number, typed on step 1 beside the cutter, and it posts
        # through the same endpoint the others do.
        self.assertEqual(self.changed("0.8", "0.5", scorew="0.45"),
                         {"score_rule_half_width_mm": 0.45})


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientPendingTest(unittest.TestCase):
    """The save button's own label, run: it counts what is waiting."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_js(PENDING_HARNESS % {"functions": lift("drawPending")})

    def test_nothing_waiting_is_a_plain_button(self):
        self.assertEqual(self.out[0]["label"], "Save to Board")
        self.assertFalse(self.out[0]["dirty"])

    def test_one_change_is_singular_and_more_are_not(self):
        self.assertEqual(self.out[1]["label"], "Save to Board (1 change)")
        self.assertEqual(self.out[2]["label"], "Save to Board (3 changes)")

    def test_a_waiting_button_is_marked_as_one(self):
        self.assertTrue(self.out[1]["dirty"])
        self.assertTrue(self.out[2]["dirty"])
        self.assertIn("waiting", self.out[2]["title"])

    def test_the_count_and_the_list_are_both_held(self):
        self.assertEqual([entry["held"] for entry in self.out], [0, 1, 3])
        self.assertEqual([entry["changes"] for entry in self.out], [0, 1, 1])


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientUnsavedPromptTest(unittest.TestCase):
    """The three-way unsaved-work question, run: shown, listed, and answered."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_js(ASK_HARNESS % {
            "functions": js_const("ASKS", "};") + "\n"
            + lift("askUnsaved", "closeAsk"),
        })

    def test_the_prompt_opens_and_lists_what_is_waiting(self):
        self.assertTrue(self.out["shown"]["on"])
        self.assertEqual(self.out["shown"]["hidden"], "false")
        self.assertEqual(self.out["shown"]["listed"], 2)

    def test_quitting_words_the_three_answers_as_quitting(self):
        self.assertEqual(self.out["shown"]["save"], "save and quit")
        self.assertEqual(self.out["shown"]["discard"], "discard and quit")
        self.assertIn("Quitting", self.out["shown"]["line"])

    def test_reload_words_the_same_three_answers_as_reloading(self):
        # Quit and Reload both throw held work away, so both ask - each in
        # its own words.
        self.assertEqual(self.out["asked"]["save"], "save and reload")
        self.assertEqual(self.out["asked"]["discard"], "discard and reload")
        self.assertIn("Reloading", self.out["asked"]["line"])

    def test_opening_it_is_journaled(self):
        logged = [c for c in self.out["calls"] if c.get("log") == "prompt"]
        self.assertEqual(len(logged), 2)
        self.assertEqual(logged[0]["data"]["pending"], 2)
        self.assertEqual([one["data"]["asked"] for one in logged],
                         ["quit", "reload"])

    def test_closing_it_puts_it_away_again(self):
        self.assertFalse(self.out["closed"])
        self.assertEqual(self.out["hidden_after"], "true")


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientDeadPageTest(unittest.TestCase):
    """The fallback for a browser that will not close a tab it did not open."""

    def setUp(self):
        self.out = run_js(DEAD_HARNESS % {"functions": lift(
            "goQuiet", "streamAlive", "deadPage"
        )})

    def test_the_whole_live_document_is_replaced(self):
        self.assertEqual(self.out["body_text"], "")
        self.assertEqual(self.out["kids"], 1)
        self.assertEqual(self.out["dead_class"], "dead")
        self.assertIn("the preview server has stopped", self.out["words"])
        self.assertIn("close this tab", self.out["words"])

    def test_nothing_is_left_talking_to_a_server_that_is_gone(self):
        self.assertTrue(self.out["stopped"])
        self.assertTrue(self.out["stream_closed"])
        self.assertEqual(self.out["title"], "preview server stopped")


if __name__ == "__main__":
    unittest.main()
