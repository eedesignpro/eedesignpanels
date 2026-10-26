"""Step 2: the panel page, the in-memory build under it, and the layer sync.

Five subjects, and each one is a thing that would be expensive to get wrong.

**The in-memory build IS the command line's build.** `build_from` is the call
`run` makes after loading the file, so a panel derived from a board whose file
bytes match must be byte for byte the panel a build writes - and that is
asserted on hashes of two written files, not on the shape of the code. When the
board carries unsaved decisions the panel reflects THEM, and the page says so.

**The first panel build waits to be asked.** After it, a change recalculates
the panel on entry; when the picture is out of date, the reasons are what the
banner says.

**The layer panel belongs to the session, not to a tab.** What is visible, what
is active and how the inactive layers draw travel through the server, and one
change is journaled once - not again by the page that receives it.

**The captions are controls.** A proposable candidate's caption is a click
target that posts the same two actions the buttons post; a REJECTED one is not
drawn as a target at all. The handler is lifted out of the served page and RUN
under node against a stated DOM, because a string assertion cannot tell "wired"
from "wired wrongly".

**The comparison needs files.** It is refused with a reason until a panel has
been saved, and the report it writes is served back by the one route that hands
out a file - the path the session recorded, never one a client named.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from eedesigncore import Board

from eedesignpanels import prep
from eedesignpanels.config import (
    CONFIG_REVISION,
    WRITTEN_BY,
    ConfigError,
    load_config,
)
from eedesignpanels.preview import (
    actions,
    checkrun,
    labels as labels_module,
    naming,
    page_panel,
    page_served,
    panelbuild,
    server as server_module,
)
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.review import overlay, page
from eedesignpanels.stages import step_and_repeat
from test_preview import stub_render
from test_preview_journal import js_function, run_js
from test_vscore import CUTTER, scoreable_board

GRID = {"columns": 2, "rows": 1, "gap_mm": 3.0}
MARGIN = 5.0


def write_panel_project(folder: Path, grid: dict = None,
                        margin: float = MARGIN, stage: str = "build_panel",
                        panelize: bool = True, prepped: bool = True,
                        rails=None, legacy: bool = False) -> Path:
    """A prepped board and a config that panelizes it, laid out as the core expects.

    The board is PREPPED and written before the config is read, because a panel
    is derived from the toolpath: the file on disk therefore says exactly what
    the session's in-memory board says, which is the condition the byte
    comparison is stated under.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    board_path = folder / "board.kicad_pcb"
    scoreable_board().to_file(board_path)
    # A design is a project, so the fixture is one: the panel gets its own
    # `.kicad_pro` and both halves of the write are compared.
    board_path.with_suffix(".kicad_pro").write_text(
        json.dumps({
            "meta": {"filename": "board.kicad_pro", "version": 3},
            "text_variables": {"PART_ID": "100-200-3000"},
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    panel = folder / "panel"
    panel.mkdir(exist_ok=True)
    config_path = panel / "panel.json"
    data = {
        # A file this tool wrote: the revision and the version that wrote it,
        # first and in this order. A fixture that
        # said less than a real `panel.json` would let a save write bytes no
        # test had ever compared.
        "revision": CONFIG_REVISION,
        "written_by": WRITTEN_BY,
        "source_board": "../board.kicad_pcb",
        "stage": stage,
        "grid": dict(grid or GRID),
        "min_tab_mm": 0.5,
        "prep": {
            "cutter_diameter": CUTTER,
            "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
        },
    }
    if panelize:
        # Two spellings of one setting, the linked number and the four sides.
        # `legacy` writes the old `panel.sheet` key, which the loader refuses.
        data["panel"] = (
            {"sheet": {"margin_mm": margin}} if legacy
            else {"rails": margin if rails is None else rails}
        )
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # Prep once, in memory, and put the result on disk under the board's own
    # name. Prep is idempotent, so the session's first compute over this file
    # produces the identical object.
    #
    # *prepped* False leaves the toolpath OFF the file, which is the ordinary
    # state of a board nobody has pressed Save to Board on - and the case where
    # the panel legitimately reflects something the file does not carry.
    if prepped:
        config = load_config(config_path)
        prep.compute(config).board.to_file(board_path)
    return config_path


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def a_session(folder: Path, config_path: Path, compare=None) -> PreviewSession:
    session = PreviewSession(config_path, folder / "work", render=stub_render,
                             compare=compare)
    session.refresh()
    return session


# ---------------------------------------------------------------------------
# the in-memory build seam
# ---------------------------------------------------------------------------


class InMemoryBuildTest(unittest.TestCase):
    """One build, two callers: the command line's, and the page's."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.config = load_config(self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def test_the_in_memory_panel_is_the_command_lines_panel_byte_for_byte(self):
        # THE GATE. The page derives from an object; the command line derives
        # from a file. Same board, same config, same bytes - or a panel the
        # user approved on screen is not the panel a CI runner writes.
        first = step_and_repeat.run(self.config)
        cli = digest(first.output_path)
        cli_project = digest(first.project_path)

        source = Board.from_file(self.config.source_board_path)
        from eedesignpanels import fingerprints
        evidence = fingerprints.capture(self.config.source_board_path, self.config_path)
        derived = panelbuild.derive(self.config, source)
        written = step_and_repeat.write_panel(self.config, derived.outcome)
        # The low-level writer never establishes a generation record itself.
        fingerprints.stamp_saved(written.output_path, evidence)
        self.assertEqual(digest(written.output_path), cli)
        self.assertEqual(written.project_sha256, cli_project)
        # ...and the board it derived from was the file, to the byte.
        self.assertEqual(
            derived.text,
            self.config.source_board_path.read_text(encoding="utf-8"),
        )

    def test_it_derives_from_the_board_as_the_FILE_will_hold_it(self):
        # MEASURED: a coordinate prep has just computed carries more
        # precision than the file keeps, and deriving the panel from the
        # computed one lands the channel geometry picometres away from the
        # panel a build writes. So the round trip is taken deliberately.
        source = Board.from_file(self.config.source_board_path)
        exact = panelbuild.as_the_file_holds_it(source)
        self.assertEqual(exact.to_string(), source.to_string())
        # Stable: a second round trip changes nothing, so this is a fixed point
        # and not a slow drift.
        self.assertEqual(
            panelbuild.as_the_file_holds_it(exact).to_string(), exact.to_string()
        )

    def test_the_board_it_derived_from_is_not_modified(self):
        on_disk = digest(self.config.source_board_path)
        source = Board.from_file(self.config.source_board_path)
        before = source.model_dump_json()
        panelbuild.derive(self.config, source)
        self.assertEqual(source.model_dump_json(), before)
        # ...and the file it came from is untouched.
        self.assertEqual(digest(self.config.source_board_path), on_disk)

    def test_a_config_that_does_not_build_a_panel_is_refused_by_name(self):
        path = write_panel_project(self.folder / "plain", stage="passthrough")
        with self.assertRaises(ValueError) as raised:
            panelbuild.derive(
                load_config(path),
                Board.from_file(load_config(path).source_board_path),
            )
        self.assertIn("passthrough", str(raised.exception))
        self.assertIn("build_panel", str(raised.exception))

    def test_writing_creates_the_panel_folder_when_it_is_missing(self):
        shutil.rmtree(self.config.panel_dir / "sub", ignore_errors=True)
        source = Board.from_file(self.config.source_board_path)
        derived = panelbuild.derive(self.config, source)
        written = step_and_repeat.write_panel(self.config, derived.outcome)
        self.assertTrue(written.output_path.is_file())
        self.assertTrue(written.project_path is None
                        or written.project_path.is_file())


class RailSpellingTest(unittest.TestCase):
    """One setting, two spellings, and only one of them changes the panel.

    `panel.rails: 5` and a config that says four fives build the SAME PANEL -
    to the byte, on the files written. `panel.sheet.margin_mm`, the old name
    for the same setting, is not translated: it is refused like any unknown
    key.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def built(self, name: str, **kwargs):
        config = load_config(
            write_panel_project(self.folder / name, **kwargs)
        )
        return config, step_and_repeat.run(config)

    def test_the_retired_key_is_refused_rather_than_translated(self):
        with self.assertRaises(ConfigError) as raised:
            self.built("legacy", legacy=True)
        self.assertIn("panel.sheet", str(raised.exception))

    def test_four_equal_rails_build_the_same_bytes_as_one(self):
        _, one = self.built("one")
        _, four = self.built("four", rails={
            "top": MARGIN, "bottom": MARGIN, "left": MARGIN, "right": MARGIN
        })
        self.assertEqual(digest(one.output_path), digest(four.output_path))

    def test_a_config_on_the_current_key_says_nothing_about_a_rename(self):
        _, result = self.built("quiet")
        self.assertFalse(any("panel.sheet" in note for note in result.warnings))

    def test_per_side_rails_move_the_edges_they_name_and_no_others(self):
        _, one = self.built("even")
        _, sided = self.built("sided", rails={
            "top": 1.0, "bottom": MARGIN, "left": MARGIN, "right": MARGIN
        })
        even = one.panelize.blank
        uneven = sided.panelize.blank
        # Only the top edge moved, by exactly what was taken off it.
        self.assertAlmostEqual(uneven.min_y - even.min_y, MARGIN - 1.0)
        self.assertAlmostEqual(uneven.min_x, even.min_x)
        self.assertAlmostEqual(uneven.max_x, even.max_x)
        self.assertAlmostEqual(uneven.max_y, even.max_y)
        self.assertNotEqual(digest(one.output_path), digest(sided.output_path))

    def test_a_per_side_panel_builds_the_same_bytes_twice(self):
        # On the arrangement that is new: same input, same
        # output, whichever way the rails were written.
        rails = {"top": 1.0, "bottom": 8.0, "left": 3.0, "right": 0.0}
        _, first = self.built("twice", rails=rails)
        second = step_and_repeat.run(
            load_config(self.folder / "twice" / "panel" / "panel.json")
        )
        self.assertEqual(first.output_sha256, second.output_sha256)


class GapPerAxisTest(unittest.TestCase):
    """Gap across and gap down are two numbers, and one by default."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def geometry(self, grid: dict, name: str = "grid"):
        path = write_panel_project(self.folder / name, grid=grid)
        config = load_config(path)
        source = Board.from_file(config.source_board_path)
        return step_and_repeat.panel_geometry(
            source, config.grid.columns, config.grid.rows,
            config.grid.gap_across_mm, config.exclude_layers,
            config.grid.gap_down_mm,
        )

    def test_one_gap_still_means_both(self):
        extent = self.geometry(
            {"columns": 2, "rows": 2, "gap_mm": 3.0}, "one"
        ).extent
        self.assertAlmostEqual(extent.step_x_mm - extent.step_y_mm, 0.0)

    def test_each_axis_takes_its_own_when_it_is_given(self):
        extent = self.geometry(
            {"columns": 2, "rows": 2, "gap_mm": 3.0, "gap_x_mm": 1.0,
             "gap_y_mm": 7.0}, "two",
        ).extent
        # A 10 mm board, so the pitch is the board plus that axis' gap.
        self.assertAlmostEqual(extent.step_x_mm, 11.0)
        self.assertAlmostEqual(extent.step_y_mm, 17.0)

    def test_the_grid_sentence_names_two_gaps_only_when_they_differ(self):
        config = load_config(write_panel_project(
            self.folder / "same", grid={"columns": 2, "rows": 3, "gap_mm": 3.0}
        ))
        self.assertEqual(config.grid.summary,
                         "2 across x 3 down, 3 mm between boards")
        config = load_config(write_panel_project(
            self.folder / "split",
            grid={"columns": 2, "rows": 3, "gap_mm": 3.0, "gap_y_mm": 5.0},
        ))
        self.assertEqual(
            config.grid.summary,
            "2 across x 3 down, 3 mm across and 5 mm down between boards",
        )


# ---------------------------------------------------------------------------
# the session: what tab 2 holds, and when it says it is out of date
# ---------------------------------------------------------------------------


class PanelSessionTest(unittest.TestCase):
    """The panel half of the session, driven through its own methods."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = a_session(self.folder, self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def blade(self):
        from eedesignpanels.stages.route_toolpath import outline_bbox
        from eedesignpanels.toolpath import blade_extent

        board = self.session.board
        bbox = outline_bbox(board)
        for proposal in self.session.view.result.proposals:
            if proposal.proposable and not proposal.accepted:
                start, end = blade_extent(proposal.candidate, bbox)
                return {"start": list(start), "end": list(end)}
        raise AssertionError("the fixture has no proposable candidate")

    def test_nothing_is_built_until_it_is_asked_for(self):
        self.assertIsNone(self.session.panel)
        self.assertEqual(self.session.panel_stale, [panelbuild.NEVER_BUILT])
        fragment = self.session.panel_fragment()
        self.assertFalse(fragment["ok"])
        self.assertIn(panelbuild.NEVER_BUILT, fragment["stale"])

    def test_a_rebuild_moves_the_frame_and_leaves_the_layer_set_alone(self):
        # WHY THE BUILD CAN REFRESH IN PLACE. A wider panel
        # is a new frame and the same stack: the frame is handed to the
        # viewport and the contents are swapped, and `paintSvgs` - which
        # reloads only when the layer SET changes - has nothing to reload for.
        self.session.build_panel({})
        first = self.session.panel_fragment()
        self.session.set_param({"columns": 3})
        self.session.build_panel({})
        second = self.session.panel_fragment()
        self.assertEqual(first["layers"], second["layers"])
        self.assertNotEqual(first["base"], second["base"])
        self.assertNotEqual(first["extent"], second["extent"])
        self.assertEqual(second["base"],
                         page.padded_box(*self.session.panel.window))
        # ...and the stack the page was SERVED with is that same set, so the
        # very first build is a paint too.
        served = re.findall(r'<svg[^>]*data-layer="([^"]+)"',
                            self.session.panel_page())
        self.assertEqual(served, second["layers"])

    def test_building_gives_a_panel_a_report_and_no_stale_reason(self):
        self.session.build_panel({})
        self.assertIsNotNone(self.session.panel)
        self.assertEqual(self.session.panel_stale, [])
        report = self.session.panel.report
        self.assertTrue(any("2 boards" in line for line in report))
        self.assertTrue(any("The panel is" in line for line in report))
        self.assertTrue(any("panel edge is" in line for line in report))

    def test_the_panel_is_derived_from_memory_and_says_what_is_unsaved(self):
        # Tab 2 builds from tab 1's in-memory state, and the report says when
        # that is not what the files carry.
        before = digest(load_config(self.config_path).source_board_path)
        self.session.accept(self.blade())
        self.session.build_panel({})
        self.assertEqual(self.session.panel.unsaved_board, 1)
        self.assertIn("1 unsaved board change", self.session.panel.subtitle)
        self.assertIn("1 unsaved board change",
                      self.session.panel_fragment()["unsaved"])
        # ...and the design on disk is still untouched by any of it.
        self.assertEqual(
            digest(load_config(self.config_path).source_board_path), before
        )

    def test_a_saved_board_says_nothing_about_unsaved_work(self):
        self.session.save()
        self.session.build_panel({})
        self.assertEqual(self.session.panel.unsaved_board, 0)
        self.assertFalse(self.session.panel.board_differs)
        self.assertEqual(self.session.panel_fragment()["unsaved"], "")

    def test_a_toolpath_the_file_does_not_carry_is_said_out_loud(self):
        # MEASURED ON THE CORPUS, and no count can see it: prep owns
        # the toolpath layer and rewrites it on every computation, so a board
        # file whose toolpath an older run wrote differs from the board in
        # memory with nobody having edited anything. The panel is derived from
        # the new toolpath - the right answer, said out loud.
        folder = self.folder / "unprepped"
        session = a_session(
            folder, write_panel_project(folder, prepped=False)
        )
        session.build_panel({})
        self.assertEqual(session.pending_count, 0)
        self.assertTrue(session.panel.board_differs)
        said = session.panel_fragment()["unsaved"]
        self.assertIn("a prepared board the design file does not carry", said)
        self.assertIn("Save to Board", said)
        # ...and once it IS on the file, the sentence goes.
        session.save()
        session.build_panel({})
        self.assertFalse(session.panel.board_differs)
        self.assertEqual(session.panel_fragment()["unsaved"], "")

    def test_once_the_board_is_saved_the_panel_is_the_command_lines_panel(self):
        # The gate, stated the way a user meets it: prepare, save the
        # board, build the panel, save the panel - and a CI runner building
        # from those same files writes the same bytes.
        folder = self.folder / "roundtrip"
        session = a_session(folder, write_panel_project(folder, prepped=False))
        session.save()
        session.build_panel({})
        session.save_panel()
        mine = digest(session.panel_saved.output_path)
        expected = step_and_repeat.run(
            load_config(session.config_path)
        )
        self.assertEqual(digest(expected.output_path), mine)

    def test_a_score_decision_recalculates_the_panel(self):
        # A responsive change recalculates on entry. A score decision moves the
        # channels and the saw passes, so the panel that follows it is a panel
        # of that decision - not a picture with a banner over it.
        self.session.build_panel({})
        before = self.session.panel
        self.session.accept(self.blade())
        self.assertEqual(self.session.panel_stale, [])
        self.assertIsNot(self.session.panel, before)
        self.assertTrue(self.session.panel_fragment()["ok"])

    def test_a_panel_number_recalculates_the_panel_and_not_the_board(self):
        self.session.build_panel({})
        held = self.session.view
        panel = self.session.panel
        generation = self.session.generation
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertEqual(self.session.panel_stale, [])
        # The PREP picture is drawn from nothing this number touches, so it was
        # not recomputed: the same object is still on the session.
        self.assertIs(self.session.view, held)
        # The panel is, though, and it is the panel these numbers describe.
        self.assertIsNot(self.session.panel, panel)
        self.assertAlmostEqual(
            self.session.panel.outcome.geometry.extent.step_x_mm
            - panel.outcome.geometry.extent.step_x_mm,
            1.0,
        )
        self.assertGreater(self.session.generation, generation)
        self.assertEqual(self.session.pending_config, 1)
        self.assertEqual(self.session.pending_board, 0)

    def test_a_prep_number_recomputes_the_board_and_then_the_panel(self):
        self.session.build_panel({})
        held = self.session.view
        panel = self.session.panel
        self.session.set_param({"cutter_diameter": CUTTER + 0.1})
        self.assertIsNot(self.session.view, held)
        self.assertEqual(self.session.panel_stale, [])
        self.assertIsNot(self.session.panel, panel)
        # ORDER MATTERS: prep owns the toolpath and rewrites it, so the panel
        # has to be derived from the NEW one. The cutter is wider, so the
        # channels are wider, and the panel says so.
        self.assertGreater(
            self.session.panel.outcome.panelize.channel_length_mm, 0.0
        )

    def test_a_manual_build_still_clears_a_stale_reason(self):
        # Build Panel is the manual re-derive, and it is what starts a session
        # that has never built one.
        self.assertEqual(self.session.panel_stale, [panelbuild.NEVER_BUILT])
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertEqual(self.session.panel_stale, [panelbuild.NEVER_BUILT])
        self.session.build_panel({})
        self.assertEqual(self.session.panel_stale, [])

    def test_before_the_first_build_a_change_only_notes_itself(self):
        # Nothing is on screen to keep true, so nothing pays for a build: one
        # reason, however many edits it took.
        self.session.set_param({"gap_x_mm": 4.0})
        self.session.set_param({"gap_y_mm": 4.0})
        self.assertEqual(self.session.panel_stale, [panelbuild.NEVER_BUILT])
        self.assertIsNone(self.session.panel)

    def test_a_recalculation_that_fails_leaves_the_reason_and_retries(self):
        # The one case the banner still exists for. The failure is stated, the
        # picture is withdrawn rather than left lying, and the NEXT edit tries
        # again rather than needing the button.
        self.session.build_panel({})
        broken = ValueError("the outline does not close")

        def fail(*args, **kwargs):
            raise broken

        good = self.session._compute_panel
        self.session._compute_panel = fail
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertIsNone(self.session.panel)
        self.assertEqual(self.session.panel_stale,
                         [panelbuild.CONFIG_CHANGED])
        self.assertIn("does not close", self.session.panel_error)
        self.assertIn("does not close",
                      " ".join(self.session.panel_fragment()["notes"]))

        self.session._compute_panel = good
        self.session.set_param({"gap_x_mm": 5.0})
        self.assertIsNotNone(self.session.panel)
        self.assertEqual(self.session.panel_stale, [])

    def test_a_grid_change_is_in_the_panel_that_follows_it(self):
        self.session.build_panel({})
        first = self.session.panel.outcome.panelize.blank.width_mm
        self.session.set_param({"columns": 3})
        self.session.build_panel({})
        second = self.session.panel.outcome.panelize.blank.width_mm
        self.assertGreater(second, first)
        self.assertEqual(self.session.config.grid.columns, 3)

    def test_a_count_of_boards_takes_no_fraction(self):
        with self.assertRaises(actions.ActionError) as raised:
            self.session.set_param({"columns": 2.5})
        self.assertIn("whole number", str(raised.exception))

    def test_saving_the_panel_writes_what_the_command_line_writes(self):
        # THE OTHER HALF OF THE GATE: the page's own save, compared with a
        # build, on the bytes of both.
        self.session.save()
        expected = step_and_repeat.run(load_config(self.config_path))
        cli_board = digest(expected.output_path)
        cli_project = (
            digest(expected.project_path) if expected.project_path else ""
        )
        self.session.build_panel({})
        self.session.save_panel()
        saved = self.session.panel_saved
        self.assertEqual(digest(saved.output_path), cli_board)
        if cli_project:
            self.assertEqual(digest(saved.project_path), cli_project)

    def test_saving_the_panel_writes_the_config_only_when_a_number_moved(self):
        before = digest(self.config_path)
        self.session.build_panel({})
        self.session.save_panel()
        self.assertEqual(digest(self.config_path), before)

        self.session.set_param({"gap_x_mm": 2.5})
        self.session.build_panel({})
        self.session.save_panel()
        self.assertNotEqual(digest(self.config_path), before)
        self.assertEqual(json.loads(self.config_path.read_text())["grid"]
                         ["gap_x_mm"], 2.5)
        self.assertEqual(self.session.pending_config, 0)

    def test_saving_the_panel_does_not_save_the_board(self):
        # The two writes are two writes: derived output is not a decision.
        board_path = self.session.config.source_board_path
        before = digest(board_path)
        self.session.accept(self.blade())
        self.session.build_panel({})
        self.session.save_panel()
        self.assertEqual(digest(board_path), before)
        self.assertEqual(self.session.pending_board, 1)

    def test_there_is_nothing_to_save_before_there_is_a_panel(self):
        with self.assertRaises(actions.ActionError) as raised:
            self.session.save_panel()
        self.assertIn("Build Panel", str(raised.exception))

    def test_a_panel_is_not_built_over_a_board_that_cannot_be_prepared(self):
        self.session.view = None
        self.session.error = "the outline does not close"
        with self.assertRaises(actions.ActionError) as raised:
            self.session.build_panel({})
        self.assertIn("does not close", str(raised.exception))

    def test_the_state_file_carries_the_panel_as_well(self):
        self.session.build_panel({})
        state = self.session.backend_state()
        self.assertIn("panel", state)
        self.assertTrue(state["panel"]["panel_ok"])
        self.assertEqual(state["panel"]["params"]["columns"], 2)
        self.assertTrue(state["panel"]["report"])


class PanelCheckTest(unittest.TestCase):
    """The comparison: refused with a reason until there are files to compare."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.ran = []
        self.session = a_session(self.folder, self.config_path,
                                 compare=self.fake_compare)

    def tearDown(self):
        self._folder.cleanup()

    def fake_compare(self, config, out_dir):
        """The comparison's contract, without the rasterizing backend."""
        self.ran.append((config.config_path, Path(out_dir)))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        report = Path(out_dir) / "GERBER_DIFF_REVIEW.html"
        report.write_text("<!doctype html><h1>diff</h1>", encoding="utf-8")
        return checkrun.CheckResult(report_path=report, summary="compared",
                                    seconds=0.1)

    def test_it_is_dead_with_a_reason_until_a_panel_is_saved(self):
        reason = self.session.check_reason()
        self.assertIn("no saved panel", reason)
        with self.assertRaises(actions.ActionError) as raised:
            self.session.check_panel({})
        self.assertIn("Save Panel Files", str(raised.exception))
        self.assertEqual(self.ran, [])

    def test_a_config_with_the_comparison_switched_off_says_so(self):
        self.session.config = self.session.config.model_copy(
            update={"verify": self.session.config.verify.model_copy(
                update={"enabled": False})}
        )
        self.assertIn("verify.enabled", self.session.check_reason())

    def test_once_saved_it_runs_and_keeps_the_report(self):
        self.session.build_panel({})
        self.session.save_panel()
        self.assertEqual(self.session.check_reason(), "")
        reply = self.session.check_panel({})
        self.assertTrue(reply["ok"])
        self.assertEqual(len(self.ran), 1)
        self.assertTrue(Path(reply["report"]).is_file())
        self.assertEqual(self.session.panel_state()["check_report"],
                         str(self.session.check_report))

    def test_it_compares_the_saved_files_and_not_the_picture(self):
        # Stated, because it is the one action that reads the disk: what the
        # comparison judges is the panel folder.
        self.session.build_panel({})
        self.session.save_panel()
        self.session.set_param({"gap_x_mm": 9.0})
        self.assertEqual(self.session.check_reason(), "")
        self.session.check_panel({})
        self.assertEqual(self.ran[0][0].resolve(),
                         self.config_path.resolve())


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


class PanelPageTest(unittest.TestCase):
    """What the panel page holds, read off the bytes it serves."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.waiting = cls.session.panel_page()
        cls.session.build_panel({})
        cls.markup = cls.session.panel_page()

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_dimensions_layer_starts_visible_on_panel_page(self):
        from eedesigncore import Board, BoardLayer
        with tempfile.TemporaryDirectory() as folder:
            config_path = write_panel_project(Path(folder))
            source = Path(folder) / 'board.kicad_pcb'
            board = Board.from_file(source)
            board.layers.append(BoardLayer(ordinal=69, canonical_name='User.16', layer_type='user'))
            board.to_file(source)
            session = a_session(Path(folder), config_path)
            try:
                session.build_panel({})
                markup = session.panel_page()
                layers = json.loads(re.search(r'const LAYERS = (\[.*?\]);', markup).group(1))
                dimensions = next(layer for layer in layers if layer['canonical'] == 'User.16')
                self.assertTrue(dimensions['start'])
            finally:
                session.close()

    def test_it_names_both_steps_and_links_to_the_other_one(self):
        for markup in (self.waiting, self.markup):
            self.assertIn("Step 1 - prepare the board", markup)
            self.assertIn("Step 2 - panelize", markup)
            self.assertIn('<a href="/" target="pb-step-prep"', markup)
            self.assertIn('<span class=here aria-current=page>Step 2', markup)

    def test_the_prep_page_links_here(self):
        prep_markup = self.session.page()
        self.assertIn('<a href="/panel" target="pb-step-panel"', prep_markup)
        self.assertIn('<span class=here aria-current=page>Step 1', prep_markup)

    def test_the_step_links_are_aimed_at_a_name_and_not_at_a_new_window(self):
        # The links jump to the other step's window. The target is that
        # step's stable window NAME, so a browser that can reach that window
        # reuses it; `_blank` and `rel=noopener` both force a fresh one, which
        # leaves a duplicate tab.
        for markup in (self.markup, self.session.page()):
            with self.subTest(page=markup[:60]):
                links = re.findall(r"<a href=\"/(?:panel)?\"[^>]*>", markup)
                self.assertTrue(links)
                for link in links:
                    self.assertNotIn('target="_blank"', link)
                    self.assertNotIn("noopener", link)
                    self.assertRegex(link, r'target="pb-step-(prep|panel)"')
                    self.assertRegex(
                        link, r'data-step-window="pb-step-(prep|panel)"'
                    )
                    self.assertRegex(link, r'data-step-page="(prep|panel)"')
                self.assertIn("brought forward if it is already open", markup)

    def test_there_is_one_viewport_and_it_is_the_panel(self):
        self.assertEqual(self.markup.count("class=card"), 1)
        self.assertIn('data-viewport="panel"', self.markup)
        self.assertNotIn("THE BOARD", self.markup)
        self.assertIn("The panel - edge, cutter path and saw lines",
                      self.markup)

    def test_it_carries_the_same_viewer_machinery(self):
        for token in ('id="lp-viewer"', "function layerPanel(",
                      "class=viewport",
                      "preserveAspectRatio=\"xMidYMid meet\"",
                      "viewport.addEventListener('wheel'", 'class="btn fit"'):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_controls_are_the_flattened_groups_and_the_write_actions(self):
        # The setting groups and the write actions.
        for token in ("id=pb-columns", "id=pb-rows", "id=pb-gapx", "id=pb-gapy",
                      "id=pb-rail", "id=pb-rail-unlock", "id=pb-build",
                      "id=pb-save", "id=pb-check", "Build Panel",
                      "Save Panel Files", "compare with the source"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)
        # The groups, in order, read off their fold headings. The write
        # actions are not a group: they sit in the footer, which does not
        # scroll away.
        headings = re.findall(
            r"<h3 class=fold[^>]*><span class=nm>([^<]+)</span>", self.markup
        )
        # Import settings leads: it pre-populates every group under it, so it
        # reads first and opens folded. Rails, Tooling Holes, Fiducials,
        # Copper Fill and V-Scores stand at the same level, each one
        # collapsed, with no wrapper group around them. The headings use
        # title case throughout.
        #
        # Dimensions sits after the carrier groups and before the checks:
        # the panel's dimensions are the last thing the build draws, and the
        # checks are not a setting at all. It holds the one drawing setting
        # the panel page has - the lettering height.
        self.assertEqual(
            headings[:10],
            ["Import settings", "Layout", "Rails", "Tooling Holes",
             "Fiducials", "Copper Fill", "V-Scores", "Dimensions", "Text", "Checks"],
        )
        self.assertNotIn(">Carrier</span>", self.markup)
        # The grid settings live in Layout; there is no separate Grid group.
        self.assertNotIn(">Grid</span>", self.markup)

    def test_every_number_on_it_is_millimetres_and_nothing_else(self):
        # Every number is in millimetres and there is no unit selector. The
        # page does have dropdowns, so the test reads what they OFFER: every
        # option on the page is a named choice, and none of them is a unit.
        self.assertIn("Gap across (mm)", self.markup)
        self.assertIn("Gap down (mm)", self.markup)
        self.assertIn("Rail width (mm)", self.markup)
        for absent in ("inch", " mils", "unit"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, self.markup)
        options = re.findall(r"<option value=\"([^\"]+)\">([^<]+)</option>",
                             self.markup)
        self.assertTrue(options)
        for value, label in options:
            with self.subTest(option=value):
                self.assertNotRegex(
                    f"{value} {label}",
                    r"(?i)\b(mm|millimet\w*|inch\w*|mil|mils|thou|cm|unit)\b",
                )

    def test_the_stale_banner_is_there_and_starts_empty(self):
        self.assertIn("<div class=stale id=pb-stale></div>", self.markup)
        self.assertIn("This picture is not the panel these numbers describe: ",
                      self.markup)
        self.assertIn("Press Build Panel.", self.markup)

    def test_a_page_with_no_panel_yet_still_carries_every_control(self):
        self.assertIn("no panel has been built in this session yet",
                      self.waiting)
        for token in ("id=pb-columns", "id=pb-build", "id=pb-status",
                      "id=pb-veil", "id=pb-ask", "id=pb-note"):
            with self.subTest(token=token):
                self.assertIn(token, self.waiting)

    def test_a_page_with_no_panel_yet_has_the_viewport_the_build_will_fill(self):
        # A build refreshes IN PLACE, so the page it refreshes
        # has to have the same SHAPE before and after. The stack is there and
        # empty, one element per layer, in the frame the board is drawn in.
        self.assertIn("class=card", self.waiting)
        self.assertIn('id="lp-viewer"', self.waiting)
        self.assertIn("function layerPanel(", self.waiting)
        self.assertIn("no panel built yet", self.waiting)
        empty = re.findall(r'<svg[^>]*data-layer="([^"]+)"[^>]*></svg>',
                           self.waiting)
        self.assertTrue(empty)
        # ...and it is exactly the stack the built page carries, so the first
        # build swaps contents and never reloads.
        built = re.findall(r'<svg[^>]*data-layer="([^"]+)"', self.markup)
        self.assertEqual(empty, built)

    def test_the_empty_stack_is_framed_where_the_board_is_drawn(self):
        base = re.search(r'data-base="([^"]+)"', self.waiting).group(1)
        self.assertEqual(
            base, page.padded_box(*self.session.view.window)
        )

    def test_a_finished_build_refreshes_in_place_and_never_reloads(self):
        # A `location.reload()` here would trip the unsaved-work guard and ask
        # the user to confirm leaving the site on every build.
        self.assertNotIn("location.reload", page_panel.PANEL_JS)
        self.assertIn(
            "return post('/build_panel', {cause: cause || 'button'}",
            page_panel.PANEL_JS,
        )
        # What it does instead: the fragment carries the frame and the extents,
        # and the viewports take them.
        self.assertIn("function rebaseViews(base)", self.markup)
        self.assertIn("rebaseViews(fragment.base)", self.markup)
        self.assertIn("drawExtents(fragment.extent)", self.markup)

    def test_the_fragment_carries_the_frame_and_the_extents(self):
        fragment = self.session.panel_fragment()
        self.assertEqual(fragment["base"],
                         page.padded_box(*self.session.panel.window))
        self.assertEqual(fragment["extent"],
                         page_served.extent_text(self.session.panel))
        self.assertRegex(fragment["extent"], r"^[\d.]+ x [\d.]+ mm$")

    def test_the_report_reaches_the_page_as_the_state_it_draws_from(self):
        self.assertIn('"report":', self.markup)
        self.assertIn("2 boards, 2 across x 1 down", self.markup)

    def test_it_says_what_writes_and_what_does_not(self):
        self.assertIn("Build Panel derives the panel in memory", self.markup)
        self.assertIn("Save Panel Files writes the panel folder", self.markup)
        self.assertIn("every number is held in memory", self.markup)

    def test_it_shows_no_bare_layer_name(self):
        # No bare layer name, checked on the bytes that are served.
        self.assertEqual(naming.violations(self.markup), [])
        self.assertEqual(naming.violations(self.waiting), [])

    def test_it_is_self_contained_like_every_other_page_here(self):
        from eedesignpanels.preview.icons import PANEL_ICON
        self.assertIn(PANEL_ICON, self.markup)
        markup = self.markup.replace(PANEL_ICON, "")
        for token in ("<link ", "<script src", "@import", "url(", "XMLHttp",
                      "WebSocket"):
            with self.subTest(token=token):
                self.assertNotIn(token, markup)

    def test_the_check_button_carries_its_reason(self):
        self.assertIn("there is no saved panel to compare yet", self.markup)

    def test_the_panel_fragment_is_what_the_page_asks_for(self):
        fragment = self.session.panel_fragment()
        self.assertTrue(fragment["ok"])
        self.assertIn("pending_config", fragment)
        self.assertIn("params", fragment)
        self.assertIn("report", fragment)
        self.assertIn("stale", fragment)
        self.assertIn("check_reason", fragment)
        self.assertTrue(fragment["svg"])
        self.assertEqual(fragment["params"]["rail_mm"], MARGIN)
        self.assertEqual(fragment["params"]["rail_top_mm"], MARGIN)
        self.assertTrue(fragment["params"]["rails_linked"])
        self.assertIn(prep.route_toolpath.OUTLINE_LAYER, fragment["layers"])


class NoCarrierPageTest(unittest.TestCase):
    """A config with no carrier still serves the rail field, greyed, with a
    reason above it.

    All five carrier groups are always on tab 2: the field is not replaced, it
    is served alongside the sentence saying why nothing below it is live yet.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder, panelize=False)
        self.session = a_session(self.folder, self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def test_the_rail_field_stays_with_the_reason_above_it(self):
        markup = self.session.panel_page()
        self.assertIn("id=pb-rail ", markup)
        self.assertIn("This config has no carrier", markup)

    def test_a_rail_cannot_be_typed_into_a_config_that_has_no_panel(self):
        with self.assertRaises(actions.ActionError) as raised:
            self.session.set_param({"rail_mm": 5.0})
        self.assertIn("'panel' section", str(raised.exception))


# ---------------------------------------------------------------------------
# the routes, over a socket
# ---------------------------------------------------------------------------


class PanelRouteTest(unittest.TestCase):
    """One server, both pages - answered over a socket, not through a call."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = a_session(self.folder, self.config_path,
                                 compare=self.fake_compare)
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def tearDown(self):
        self.server.stop()
        self._folder.cleanup()

    def fake_compare(self, config, out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        report = Path(out_dir) / "diff.html"
        report.write_text("<!doctype html><h1>the diff</h1>", encoding="utf-8")
        return checkrun.CheckResult(report_path=report, summary="compared",
                                    seconds=0.0)

    def get(self, path: str, timeout: float = 20.0):
        try:
            with urllib.request.urlopen(self.url + path, timeout=timeout) as reply:
                return reply.status, reply.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode("utf-8")

    def post(self, path: str, payload: dict = None, timeout: float = 60.0):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload or {}).encode("utf-8"),
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

    def test_both_pages_are_served_by_the_one_server(self):
        status, body = self.get("")
        self.assertEqual(status, 200)
        self.assertIn("pb-candidates", body)
        status, body = self.get("panel")
        self.assertEqual(status, 200)
        self.assertIn("pb-build", body)

    def test_the_panel_fragment_has_its_own_route(self):
        status, body = self.get("panel/fragment")
        self.assertEqual(status, 200)
        fragment = json.loads(body)
        self.assertFalse(fragment["ok"])
        self.assertIn(panelbuild.NEVER_BUILT, fragment["stale"])

    def test_building_then_saving_then_checking_works_over_the_wire(self):
        status, reply = self.post("build_panel")
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertIn("panel built in memory", reply["message"])

        status, body = self.get("panel/fragment")
        self.assertTrue(json.loads(body)["ok"])

        status, reply = self.post("save_panel")
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertIn("panel.kicad_pcb", reply["message"])

        status, reply = self.post("check")
        self.assertEqual((status, reply["ok"]), (200, True))
        status, body = self.get("check/report")
        self.assertEqual(status, 200)
        self.assertIn("the diff", body)

    def test_the_report_route_is_a_404_until_a_comparison_has_run(self):
        status, body = self.get("check/report")
        self.assertEqual(status, 404)
        self.assertIn("no comparison", body)

    def test_the_check_is_refused_with_its_reason_before_a_save(self):
        status, reply = self.post("check")
        self.assertEqual(status, 400)
        self.assertIn("Save Panel Files", reply["error"])

    def test_a_panel_number_travels_on_the_one_parameter_route(self):
        status, reply = self.post("set_param", {"rows": 2})
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertEqual(self.session.config.grid.rows, 2)
        self.assertEqual(reply["pending"], 1)

    def test_a_layout_change_recalculates_over_the_wire(self):
        # Recalculate on entry, end to end: the number goes in on one route
        # and the PANEL that comes back on the next fragment is the panel it
        # describes. No second request, no button.
        self.post("build_panel")
        before = json.loads(self.get("panel/fragment")[1])
        status, reply = self.post("set_param", {"rows": 2})
        self.assertEqual((status, reply["ok"]), (200, True))
        after = json.loads(self.get("panel/fragment")[1])
        self.assertTrue(after["ok"])
        self.assertEqual(after["stale"], [])
        self.assertNotEqual(after["extent"], before["extent"])
        self.assertIn("2 across x 2 down", after["subtitle"])

    def test_a_rail_change_recalculates_over_the_wire(self):
        self.post("build_panel")
        before = json.loads(self.get("panel/fragment")[1])
        status, reply = self.post("set_param", {"rail_mm": MARGIN + 4.0})
        self.assertEqual((status, reply["ok"]), (200, True))
        after = json.loads(self.get("panel/fragment")[1])
        self.assertEqual(after["stale"], [])
        self.assertEqual(after["params"]["rail_mm"], MARGIN + 4.0)
        self.assertNotEqual(after["extent"], before["extent"])

    def test_unlocking_the_rails_travels_as_four_numbers(self):
        self.post("build_panel")
        status, reply = self.post("set_param", {
            "rail_top_mm": 1.0, "rail_bottom_mm": 8.0,
            "rail_left_mm": 3.0, "rail_right_mm": 0.0,
        })
        self.assertEqual((status, reply["ok"]), (200, True))
        fragment = json.loads(self.get("panel/fragment")[1])
        self.assertFalse(fragment["params"]["rails_linked"])
        self.assertEqual(fragment["params"]["rail_bottom_mm"], 8.0)
        # The linked view of four rails is the widest of them.
        self.assertEqual(fragment["params"]["rail_mm"], 8.0)
        self.assertEqual(fragment["stale"], [])
        self.assertEqual(
            self.session.config.panel.rail_widths, (1.0, 8.0, 3.0, 0.0)
        )

    def test_the_layer_state_goes_round_through_the_server(self):
        state = {"page": "prep", "mode": 1, "active": "Edge.Cuts",
                 "known": ["Edge.Cuts", "User.36"],
                 "visible": ["Edge.Cuts"]}
        status, reply = self.post("layers", state)
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertEqual(reply["layers_seq"], 1)

        status, body = self.get("state")
        served = json.loads(body)
        self.assertEqual(served["layers_seq"], 1)
        self.assertEqual(served["layers"]["visible"], ["Edge.Cuts"])
        self.assertEqual(served["layers"]["mode"], 1)
        # Both fragments carry it, so a page that missed the push catches up
        # from whichever one it was going to fetch anyway.
        for path in ("fragment", "panel/fragment"):
            with self.subTest(path=path):
                self.assertEqual(
                    json.loads(self.get(path)[1])["layers_seq"], 1
                )

    def test_last_change_wins(self):
        self.post("layers", {"page": "prep", "mode": 0, "active": "Edge.Cuts",
                              "known": ["Edge.Cuts"], "visible": ["Edge.Cuts"]})
        self.post("layers", {"page": "panel", "mode": 2, "active": "User.36",
                              "known": ["Edge.Cuts", "User.36"],
                              "visible": []})
        state = json.loads(self.get("state")[1])
        self.assertEqual(state["layers_seq"], 2)
        self.assertEqual(state["layers"]["page"], "panel")
        self.assertEqual(state["layers"]["visible"], [])

    def test_a_layer_change_does_not_move_the_generation(self):
        # Switching a layer off is not changing the board, so nothing
        # recomputes and no fragment is invalidated.
        before = json.loads(self.get("state")[1])["generation"]
        self.post("layers", {"page": "prep", "mode": 0, "active": "Edge.Cuts",
                              "known": ["Edge.Cuts"], "visible": []})
        self.assertEqual(json.loads(self.get("state")[1])["generation"], before)

    def test_a_layer_state_that_is_not_one_is_refused(self):
        for bad in ({"visible": "Edge.Cuts"}, {"visible": [], "mode": 7},
                    {"visible": [], "mode": "dimmed"},
                    {"visible": ["a"] * 200}):
            with self.subTest(bad=bad):
                status, reply = self.post("layers", bad)
                self.assertEqual(status, 400)
                self.assertFalse(reply["ok"])

    def test_the_event_stream_carries_the_layers_with_the_generation(self):
        request = urllib.request.Request(self.url + "events")
        with urllib.request.urlopen(request, timeout=10.0) as stream:
            first = ""
            while not first.startswith("data:"):
                first = stream.readline().decode("utf-8")
            opening = json.loads(first[len("data:"):])
            self.assertIn("generation", opening)
            self.assertIn("layers_seq", opening)

            self.post("layers", {"page": "panel", "mode": 1,
                                  "active": "Edge.Cuts",
                                  "known": ["Edge.Cuts"], "visible": []})
            line = ""
            while not line.startswith("data:"):
                line = stream.readline().decode("utf-8")
            message = json.loads(line[len("data:"):])
        self.assertEqual(message["layers_seq"], 1)
        self.assertEqual(message["layers"]["mode"], 1)
        self.assertEqual(message["generation"], opening["generation"])


class LayerSyncJournalTest(unittest.TestCase):
    """One layer change, one journal record - not two, and not three."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        from eedesignpanels.preview import journal as journal_module

        self.recording = journal_module.open_session(
            self.config_path, self.folder / "sessions", stamp="20260909_120000"
        )
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render,
                                      journal=self.recording)
        self.session.refresh()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def tearDown(self):
        self.server.stop()
        self._folder.cleanup()

    def kinds(self) -> list:
        return [record["kind"] for record in self.recording.records()]

    def test_the_server_does_not_journal_the_sync_itself(self):
        request = urllib.request.Request(
            self.url + "/layers",
            data=json.dumps({"page": "prep", "mode": 0, "active": "Edge.Cuts",
                             "known": ["Edge.Cuts"], "visible": []}
                            ).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10.0) as reply:
            self.assertEqual(reply.status, 200)
        # The page journals its own change - `client.layers`, posted to
        # /journal - so a `server.action` record here would be the same change
        # a second time.
        self.assertNotIn("server.action", self.kinds()[-1:])
        actions_logged = [
            record for record in self.recording.records()
            if record["kind"] == "server.action"
            and record.get("endpoint") == "/layers"
        ]
        self.assertEqual(actions_logged, [])

    def test_the_pages_own_record_is_the_one_that_is_kept(self):
        self.session.journal_events({"events": [
            {"type": "layers", "cause": "panel", "page": "prep",
             "mode": 0, "active": "Edge.Cuts", "hidden": [], "shown": 4},
        ]})
        layers = [record for record in self.recording.records()
                  if record["kind"] == "client.layers"]
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["data"]["page"], "prep")


# ---------------------------------------------------------------------------
# the clickable captions
# ---------------------------------------------------------------------------


def proposal(accepted=False, proposable=True):
    """One proposal, as the label module reads it."""
    candidate = SimpleNamespace(
        start=(0.0, 0.0), end=(0.0, 10.0), midpoint=(0.0, 5.0),
        unit=(0.0, 1.0), normal=(1.0, 0.0), spans=(), length_mm=10.0,
        extent_mm=10.0,
    )
    return SimpleNamespace(id="0123456789abcdef", verdict="scored",
                           accepted=accepted, proposable=proposable,
                           candidate=candidate, summary="", reason="")


class LabelHitTest(unittest.TestCase):
    """What is drawn round a caption, and what is not drawn at all."""

    def box(self):
        return overlay.ViewBox(-5.0, -5.0, 20.0, 20.0)

    def render(self, entries):
        marked = labels_module.labelled([e for e in entries], (0.0, 0.0, 10.0, 10.0))
        box = self.box()
        return overlay.proposal_svg(
            [entry.proposal for entry in marked], (box.x, box.y), box, 1.4,
            captions=labels_module.captions(marked),
            hits=labels_module.hits(marked),
        ), marked

    def test_a_proposed_caption_becomes_a_click_target(self):
        markup, marked = self.render([proposal()])
        self.assertIn('<g class="pb-hit"', markup)
        self.assertIn('data-action="accept"', markup)
        self.assertIn('data-number="1"', markup)
        self.assertIn('data-verdict="PROPOSED"', markup)
        self.assertIn('class="pb-hitbox"', markup)
        self.assertIn("click to make it SCORED", markup)

    def test_a_scored_caption_toggles_the_other_way(self):
        markup, _ = self.render([proposal(accepted=True)])
        self.assertIn('data-action="decline"', markup)
        self.assertIn('data-verdict="SCORED"', markup)
        self.assertIn("click to make it PROPOSED", markup)

    def test_a_rejected_caption_is_not_a_target_at_all(self):
        markup, _ = self.render([proposal(proposable=False)])
        self.assertNotIn("pb-hit", markup)
        self.assertNotIn("data-action", markup)
        # ...and it is still drawn and still says what it is.
        self.assertIn("REJECTED", markup)
        self.assertIn('class="pb-label"', markup)

    def test_the_action_a_click_takes_comes_from_the_two_booleans(self):
        self.assertEqual(labels_module.click_action(proposal()), "accept")
        self.assertEqual(
            labels_module.click_action(proposal(accepted=True)), "decline"
        )
        self.assertEqual(
            labels_module.click_action(proposal(proposable=False)), ""
        )
        self.assertEqual(
            labels_module.click_action(
                proposal(accepted=True, proposable=False)
            ),
            "",
        )
        self.assertIsNone(labels_module.hit(
            labels_module.Labelled(1, "#1", "REJECTED", proposal(proposable=False))
        ))

    def test_the_hit_box_is_the_caption_with_room_round_it(self):
        # Glyphs are a mean thing to aim at, so the target is the caption's own
        # box grown by half a character on every side.
        size, chars = 2.0, 11
        box = overlay.caption_box((10.0, 20.0), "start", size, chars)
        pad = size * overlay.HIT_PAD_FRACTION
        width = chars * overlay.LABEL_CHAR_WIDTH * size
        height = size * overlay.LABEL_CAP_FRACTION
        self.assertAlmostEqual(box[0], 10.0 - pad)
        self.assertAlmostEqual(box[1], 20.0 - height - pad)
        self.assertAlmostEqual(box[2], width + 2 * pad)
        self.assertAlmostEqual(box[3], height + 2 * pad)

    def test_the_box_follows_the_anchoring_so_it_covers_the_words(self):
        for place, left in (("start", 0.0), ("end", -12.0), ("middle", -6.0)):
            with self.subTest(place=place):
                box = overlay.caption_box((0.0, 0.0), place, 2.0, 10,
                                          pad_fraction=0.0)
                self.assertAlmostEqual(box[0], left)
                self.assertAlmostEqual(box[2], 12.0)

    def test_the_frozen_artifact_carries_no_targets_and_no_handler(self):
        # A page with nothing to talk to has no controls on it. Asserted on the
        # overlay called the way the frozen page calls it.
        box = self.box()
        markup = overlay.proposal_svg(
            [proposal()], (box.x, box.y), box, 1.4, along_line=True
        )
        for token in ("pb-hit", "data-action", "pointer-events"):
            with self.subTest(token=token):
                self.assertNotIn(token, markup)

    def test_a_turned_caption_takes_its_box_with_it(self):
        box = self.box()
        marked = labels_module.labelled([proposal()], (0.0, 0.0, 10.0, 10.0))
        markup = overlay.proposal_svg(
            [entry.proposal for entry in marked], (box.x, box.y), box, 1.4,
            captions=["#1 PROPOSED"], along_line=True,
            hits=labels_module.hits(marked),
        )
        hitbox = markup.split('class="pb-hitbox"')[1].split("/>")[0]
        self.assertIn("transform=\"rotate(", hitbox)


class LabelsOnThePageTest(unittest.TestCase):
    """The wiring, on the bytes the prep page serves."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.markup = cls.session.page()

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_the_targets_reach_the_page(self):
        self.assertIn('<g class="pb-hit"', self.markup)
        self.assertIn('data-action="', self.markup)

    def test_only_the_captions_take_pointer_events_back(self):
        # Panning must stay dead everywhere else: the stack is
        # pointer-events:none and exactly one selector undoes it.
        self.assertIn(".viewport svg{", self.markup)
        self.assertIn("pointer-events:none", self.markup)
        self.assertIn(".viewport svg .pb-hit{pointer-events:all;cursor:pointer}",
                      self.markup)
        self.assertEqual(self.markup.count("pointer-events:all"), 1)

    def test_hovering_a_caption_shows_it_is_a_control(self):
        self.assertIn(".viewport svg .pb-hit:hover .pb-hitbox{fill:#ffffff26}",
                      self.markup)
        self.assertIn(".viewport svg .pb-hit:hover .pb-label{fill:#ffffff}",
                      self.markup)

    def test_the_handler_is_wired_to_the_viewports_and_nowhere_else(self):
        for token in ("function clickLabel(number, cause)",
                      "function labelAction(p)",
                      "function wireLabels(root)",
                      "wireLabels(document.getElementById('views'))",
                      "event.target.closest('.pb-hit')",
                      "if (travelPx(press, event) > CLICK_SLOP_PX) return null;"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_caption_is_read_at_the_press_and_the_toggle_taken_at_release(self):
        # Both in the CAPTURE phase, because a captured pointer sends the click
        # to the viewport and there is nothing to read off it by then.
        self.assertIn("root.addEventListener('pointerdown', pressLabel, true);",
                      self.markup)
        self.assertIn("root.addEventListener('pointerup', event => {",
                      self.markup)
        self.assertIn("if (releaseLabel(event, 'label')) clickTaken = true;",
                      self.markup)

    def test_a_click_takes_the_same_path_the_buttons_take(self):
        # One action path: the caption posts /accept and /decline with the
        # candidate's own blade line, which is what the button sends.
        self.assertIn("return post('/' + act, p.blade,", self.markup)
        self.assertIn("post('/accept', p.blade,", self.markup)

    def test_a_click_is_journaled_the_way_a_button_press_is(self):
        self.assertIn("logEvent('control', {", self.markup)
        self.assertIn("where: 'label'", self.markup)
        self.assertIn("candidate: p.displayId", self.markup)


# WHY THE DOM BELOW CAPTURES THE POINTER. The viewport takes POINTER CAPTURE
# on pointerdown - it must, or a press that lands on an earlier selection
# starts a native drag and panning locks up - and a captured pointer retargets
# every later event of that gesture, `click` included, to the CAPTURE ELEMENT.
# A handler that calls `closest('.pb-hit')` on the event target then searches
# upwards from `.viewport`, finds nothing, and the toggle does not happen. A
# harness in which the click arrives at the caption passes such a handler
# while it does nothing in a real browser.
#
# So the DOM below dispatches through a real tree, in both phases, and honours
# capture. Checked both ways: with the capture line removed, a target-based
# handler posted both actions, and with it in place it posted nothing.
LABEL_DOM = r"""
const captured = {at: null};
function node(name, parent, extra) {
  const el = Object.assign({
    name: name, parent: parent, children: [], classes: new Set(),
    handlers: {capture: {}, bubble: {}},
    addEventListener(kind, fn, useCapture) {
      (useCapture ? this.handlers.capture : this.handlers.bubble)[kind] = fn;
    },
    closest(sel) {
      let at = this;
      while (at) {
        if (at.classes.has(sel.replace('.', ''))) return at;
        at = at.parent;
      }
      return null;
    },
    setPointerCapture() { captured.at = this; },
    releasePointerCapture() { captured.at = null; },
  }, extra || {});
  if (parent) parent.children.push(el);
  return el;
}
function chain(target) {
  const out = [];
  for (let at = target; at; at = at.parent) out.unshift(at);
  return out;
}
function dispatch(kind, target, event) {
  event.target = target;
  const path = chain(target);
  path.forEach(el => { if (el.handlers.capture[kind]) el.handlers.capture[kind](event); });
  path.slice().reverse().forEach(
    el => { if (el.handlers.bubble[kind]) el.handlers.bubble[kind](event); });
}
function pointerEvent(x, y, button) {
  return {clientX: x, clientY: y, button: button || 0, pointerId: 1,
          preventDefault() { this.prevented = true; }};
}
// One whole gesture, the way a browser runs it: down, an optional move, up,
// and the click the browser synthesises after them.
function gesture(target, from, to, button) {
  dispatch('pointerdown', target, pointerEvent(from[0], from[1], button));
  if (to[0] !== from[0] || to[1] !== from[1]) {
    dispatch('pointermove', captured.at || target,
             pointerEvent(to[0], to[1], button));
  }
  dispatch('pointerup', captured.at || target, pointerEvent(to[0], to[1], button));
  const clickTarget = captured.at || target;
  captured.at = null;
  if (!button) dispatch('click', clickTarget, pointerEvent(to[0], to[1], button));
}
"""

LABEL_HARNESS = """
%(dom)s
%(functions)s
let busy = false, stopped = false;
const PB = {proposals: [
  {number: 1, displayId: '#1', verdict: 'PROPOSED', accepted: false,
   proposable: true, blade: {start: [0, 0], end: [0, 10]}},
  {number: 2, displayId: '#2', verdict: 'SCORED', accepted: true,
   proposable: true, blade: {start: [5, 0], end: [5, 10]}},
  {number: 3, displayId: '#3', verdict: 'REJECTED', accepted: false,
   proposable: false, blade: {start: [9, 0], end: [9, 10]}},
]};
const out = {posted: [], logged: []};
function logEvent(type, data) { out.logged.push({type: type, data: data}); }
async function post(path, body, what) {
  out.posted.push({path: path, body: body, what: what});
  return {ok: true};
}

const views = node('views', null);
views.classes.add('views');
const viewport = node('viewport', views);
viewport.classes.add('viewport');
// The viewport's own pan handler, as `review.page` writes it.
viewport.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  event.preventDefault();
  viewport.setPointerCapture(event.pointerId);
});
function caption(number) {
  const group = node('hit' + number, viewport,
                     {dataset: {number: String(number)}});
  group.classes.add('pb-hit');
  const box = node('hitbox' + number, group, {dataset: {}});
  box.classes.add('pb-hitbox');
  return box;              // the rect is what a press actually lands on
}
wireLabels(views);

const one = caption(1), two = caption(2), three = caption(3);
out.actions = [labelAction(PB.proposals[0]), labelAction(PB.proposals[1]),
               labelAction(PB.proposals[2]), labelAction(null)];
gesture(one, [100, 100], [100, 100]);        // down and up, same point
gesture(two, [100, 100], [102, 100]);        // down, 2 px of jitter, up
gesture(three, [100, 100], [100, 100]);      // REJECTED: no target at all
gesture(one, [100, 100], [110, 100]);        // down, 10 px, up - a pan
gesture(two, [100, 100], [100, 100], 2);     // the right button toggles nothing
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientLabelClickTest(unittest.TestCase):
    """The caption click path, RUN against a DOM that captures the pointer.

    A regression test: the captions must do something when pressed, not only
    highlight on hover.
    """

    @classmethod
    def setUpClass(cls):
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("labelAction", "proposalNumbered", "clickLabel",
                         "hitUnder", "travelPx", "pressLabel", "releaseLabel",
                         "wireLabels")
        )
        cls.out = run_js(
            "const CLICK_SLOP_PX = 4;\nlet pressAt = null;\n"
            "let clickTaken = false;\n"
            + (LABEL_HARNESS % {"dom": LABEL_DOM, "functions": functions})
        )

    def test_the_action_is_read_off_the_candidates_two_booleans(self):
        # PROPOSED accepts, SCORED declines, REJECTED does nothing, and
        # nothing at all does nothing.
        self.assertEqual(self.out["actions"], ["accept", "decline", "", ""])

    def test_a_press_and_a_release_on_a_caption_toggles_it(self):
        # The case pointer capture breaks: a handler that searches from the
        # event target posts nothing here.
        posted = self.out["posted"]
        self.assertEqual(posted[0]["path"], "/accept")
        self.assertEqual(posted[0]["body"], {"start": [0, 0], "end": [0, 10]})
        self.assertIn("accepting #1", posted[0]["what"])

    def test_a_scored_caption_declines(self):
        self.assertEqual(self.out["posted"][1]["path"], "/decline")
        self.assertIn("declining #2", self.out["posted"][1]["what"])

    def test_two_pixels_of_jitter_is_still_a_click(self):
        # A pointer at devicePixelRatio 1.5 does not land on whole CSS pixels.
        self.assertEqual(len(self.out["posted"]), 2)

    def test_a_press_that_travelled_is_a_pan_and_not_a_click(self):
        # Ten pixels: a pan, and no third post.
        self.assertEqual([entry["path"] for entry in self.out["posted"]],
                         ["/accept", "/decline"])

    def test_a_rejected_caption_and_a_right_click_post_nothing(self):
        self.assertEqual(len(self.out["posted"]), 2)

    def test_one_gesture_toggles_once_and_not_twice(self):
        # pointerup takes it; the click the browser fires afterwards must not
        # take it again.
        self.assertEqual(len(self.out["posted"]), 2)
        self.assertEqual(
            len([e for e in self.out["logged"] if e["type"] == "control"]), 2
        )

    def test_every_click_is_journaled_with_the_candidate_it_named(self):
        logged = [entry for entry in self.out["logged"]
                  if entry["type"] == "control"]
        self.assertEqual(len(logged), 2)
        self.assertEqual(logged[0]["data"]["candidate"], "#1")
        self.assertEqual(logged[0]["data"]["where"], "label")
        self.assertEqual(logged[0]["data"]["action"], "accept")
        self.assertEqual(logged[1]["data"]["candidate"], "#2")

    def test_the_viewport_really_does_capture_the_pointer(self):
        # The harness models what `review.page` actually does. If this stops
        # being true the model above is stale and the test is worth nothing.
        self.assertIn("viewport.setPointerCapture(event.pointerId);", page.JS)
        self.assertIn("viewport.addEventListener('pointerdown'", page.JS)


# The refresh path, run: the frame, the contents, the readout, and the guard
# that must NOT fire while a build refreshes the page under the user.
REFRESH_HARNESS = """
%(functions)s
let stopped = false, reloading = false;
const PB = {generation: 4, pending: 2};
const out = {rebased: [], reloaded: 0, flushed: 0, logged: [], guard: []};
function logEvent(type, data) { out.logged.push(type); }
function flushEvents() { out.flushed += 1; }
const location = {reload() { out.reloaded += 1; }};
// One viewport, as `review.page` registers it.
const viewFrames = [{
  box: () => [0, 0, 10, 10],
  rebase: next => { out.rebased.push(next); return true; },
}];
const stack = %(stack)s;
const hud = [{textContent: ''}];
globalThis.document = {
  querySelectorAll(selector) {
    // The stack carries an OVERLAY element - the clearance marking's - which
    // is not a layer. `paintSvgs` asks for the layers only, and that is
    // exactly what this harness has to model or the fence proves nothing.
    if (selector === '.viewport svg') return stack;
    if (selector === '.viewport svg:not([data-overlay])') {
      return stack.filter(el => !el.dataset.overlay);
    }
    if (selector === '.hud b.ext') return hud;
    return [];
  },
  querySelector(selector) {
    const want = selector.match(/data-layer="(.*)"\\]$/);
    return stack.find(el => el.dataset.layer === want[1]) || null;
  },
};
globalThis.CSS = {escape: text => text};

out.painted = paintSvgs(%(svg)s, %(layers)s);
out.rebasedAny = rebaseViews(%(base)s);
drawExtents('61.00 x 41.50 mm');
out.extent = hud[0].textContent;
out.contents = stack.map(el => [el.dataset.layer, el.innerHTML]);

// THE GUARD. A build has just refreshed the page in place with two edits
// waiting: nothing left the tab, so nothing may be asked.
function ask() {
  const event = {prevented: false, returnValue: null,
                 preventDefault() { this.prevented = true; }};
  const answer = unloadGuard(event);
  out.guard.push({prevented: event.prevented, answer: answer === undefined
                  ? null : answer});
  return event;
}
ask();                      // the user really is closing the tab
reloading = true; ask();    // ...and this one is the page reloading itself
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class InPlaceRefreshTest(unittest.TestCase):
    """A build refreshes IN PLACE, and never raises the browser's prompt."""

    LAYERS = ["Edge.Cuts", "User.8"]

    def refresh(self, layers=None, base="0 0 61 41.5") -> dict:
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("paintSvgs", "rebaseViews", "drawExtents",
                         "selfReload", "unloadGuard")
        )
        stack = [{"dataset": {"layer": name}, "innerHTML": "old " + name}
                 for name in self.LAYERS]
        # THE CLEARANCE MARKING's element, last in the stack and NOT a layer.
        # It must neither be swapped nor counted, or every
        # refresh would decide the layer set had changed and reload the page.
        stack.append({"dataset": {"overlay": "1"}, "innerHTML": "marks"})
        return run_js(REFRESH_HARNESS % {
            "functions": functions,
            "stack": json.dumps(stack),
            "svg": json.dumps({name: "<path d='M0 0'/>" for name in self.LAYERS}),
            "layers": json.dumps(self.LAYERS if layers is None else layers),
            "base": json.dumps(base),
        })

    def test_the_same_layer_set_is_repainted_in_place(self):
        out = self.refresh()
        self.assertTrue(out["painted"])
        self.assertEqual(out["reloaded"], 0)
        # Every LAYER is swapped and the OVERLAY is left exactly as it was: its
        # contents are the page's own annotation, not a plot to replace.
        self.assertEqual([entry[1] for entry in out["contents"]],
                         ["<path d='M0 0'/>", "<path d='M0 0'/>", "marks"])

    def test_a_new_frame_reaches_every_viewport(self):
        out = self.refresh()
        self.assertTrue(out["rebasedAny"])
        self.assertEqual(out["rebased"], [[0, 0, 61, 41.5]])

    def test_the_readout_follows_the_picture(self):
        self.assertEqual(self.refresh()["extent"], "61.00 x 41.50 mm")

    def test_the_unsaved_guard_still_stops_a_real_tab_close(self):
        guard = self.refresh()["guard"]
        self.assertTrue(guard[0]["prevented"])
        self.assertEqual(guard[0]["answer"], "")

    def test_a_refresh_the_page_asked_for_never_raises_the_prompt(self):
        # A build refreshes the page itself, so the unsaved-work guard must
        # not fire the browser's "leave site?" box.
        guard = self.refresh()["guard"]
        self.assertFalse(guard[1]["prevented"])
        self.assertIsNone(guard[1]["answer"])

    def test_a_changed_layer_set_reloads_and_says_so_first(self):
        # The one case that cannot be done in place. It goes through
        # `selfReload`, so it does not raise the prompt either.
        out = self.refresh(layers=[*self.LAYERS, "User.9"])
        self.assertFalse(out["painted"])
        self.assertEqual(out["reloaded"], 1)
        self.assertIn("page.reload", out["logged"])

    def test_a_frame_that_did_not_move_is_not_a_refit(self):
        # Asserted on the viewport's own `rebase`, which answers whether
        # anything happened - a refit on every refresh would throw away the
        # zoom the user set.
        self.assertIn("if (next.every((n, i) => n === base[i])) return false;",
                      page.JS)
        self.assertIn("viewFrames.push({", page.JS)


LAYER_SYNC_HARNESS = """
%(functions)s
let stopped = false, lastLayers = '';
const PB = {page: 'panel'};
const LAYERS = [{canonical: 'Edge.Cuts'}, {canonical: 'User.36'},
                {canonical: 'User.38'}];
const sent = [];
function send(path, body) { sent.push({path: path, body: body}); }
function layerState() { return {mode: viewer.mode, active: 'x', hidden: []}; }
const viewer = {
  active: 0, mode: 0, visible: new Set(['Edge.Cuts', 'User.36', 'User.38']),
  painted: 0, paint() { this.painted += 1; },
};
const out = {};
out.applied = applyLayerState(%(incoming)s);
out.visible = LAYERS.map(l => l.canonical).filter(n => viewer.visible.has(n));
out.active = viewer.active;
out.mode = viewer.mode;
out.painted = viewer.painted;
out.sent = sent;
// The echo of our own state must not be applied again.
out.echo = applyLayerState(panelLayerState());
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientLayerSyncTest(unittest.TestCase):
    """`applyLayerState`, run: the other page's layers, applied to this one."""

    def apply(self, incoming: dict) -> dict:
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("panelLayerState", "sameLayerState", "applyLayerState",
                         "pushLayers")
        )
        return run_js(LAYER_SYNC_HARNESS % {
            "functions": functions, "incoming": json.dumps(incoming),
        })

    def test_the_other_pages_visibility_is_applied(self):
        out = self.apply({
            "page": "prep", "mode": 1, "active": "User.36",
            "known": ["Edge.Cuts", "User.36", "User.38"],
            "visible": ["Edge.Cuts"],
        })
        self.assertTrue(out["applied"])
        self.assertEqual(out["visible"], ["Edge.Cuts"])
        self.assertEqual(out["active"], 1)
        self.assertEqual(out["mode"], 1)
        self.assertEqual(out["painted"], 1)

    def test_a_layer_the_other_page_does_not_have_is_left_alone(self):
        # The prep page carries the candidate overlay and the panel page does
        # not, so neither may switch the other's off by omission.
        out = self.apply({
            "page": "prep", "mode": 0, "active": "Edge.Cuts",
            "known": ["Edge.Cuts", "User.36"], "visible": ["Edge.Cuts"],
        })
        self.assertEqual(out["visible"], ["Edge.Cuts", "User.38"])

    def test_applying_a_state_does_not_send_it_back(self):
        out = self.apply({
            "page": "prep", "mode": 0, "active": "Edge.Cuts",
            "known": ["Edge.Cuts"], "visible": [],
        })
        self.assertEqual(out["sent"], [])

    def test_our_own_state_arriving_back_changes_nothing(self):
        out = self.apply({
            "page": "prep", "mode": 1, "active": "User.36",
            "known": ["Edge.Cuts", "User.36", "User.38"],
            "visible": ["Edge.Cuts"],
        })
        self.assertFalse(out["echo"])
        self.assertEqual(out["painted"], 1)

    def test_a_page_with_no_layer_panel_says_nothing_about_layers(self):
        # The panel page before its first build has no viewer at all.
        out = run_js(
            "\n".join(
                js_function(page_served.CONTROLS_JS, name)
                for name in ("panelLayerState", "sameLayerState",
                             "applyLayerState")
            )
            + "\nconst PB = {page: 'panel'};\n"
            + "console.log(JSON.stringify({"
            + "state: panelLayerState(),"
            + "applied: applyLayerState({visible: [], known: []})}));"
        )
        self.assertIsNone(out["state"])
        self.assertFalse(out["applied"])


# ---------------------------------------------------------------------------
# the fences: what every field wears, and what no page may say
# ---------------------------------------------------------------------------

CONTROLS_BLOCK = re.compile(r"<aside class=controls.*?</aside>", re.S)
FIELD_BLOCK = re.compile(r'<label class="field[^"]*">(.*?)</label>', re.S)
# Every kind of control the two pages use. A dropdown and a list box are entry
# fields too, so the fence counts them the way it counts an `<input>`.
ANY_INPUT = re.compile(r"<(?:input|select|textarea)\b[^>]*>")


def controls_of(markup: str) -> str:
    """The third column, on its own. The fence is about entry FIELDS."""
    found = CONTROLS_BLOCK.search(markup)
    assert found, "the page has no controls column"
    return found.group(0)


class FieldFenceTest(unittest.TestCase):
    """Every entry field's title and helper, read off the served bytes of BOTH
    pages.

    Every entry field shows a bold title and one smaller helper line under it.
    Checked on the markup rather than on the builder, because what matters is
    what the user reads: a field added by hand, in either page, has to wear
    the same clothes as one built through `page_served.number_field`.

    The header's note box is deliberately out of scope - it is a session tool,
    not a setting, and it lives in the title bar where a
    two-line field would not fit.
    """

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = write_panel_project(folder)
        cls.session = a_session(folder, cls.config_path)
        cls.session.build_panel({})
        cls.pages = {
            "prep": cls.session.page(),
            "panel": cls.session.panel_page(),
            "panel-unlocked": None,
        }
        cls.session.set_param({
            "rail_top_mm": 1.0, "rail_bottom_mm": 8.0,
            "rail_left_mm": 3.0, "rail_right_mm": 0.0,
        })
        cls.pages["panel-unlocked"] = cls.session.panel_page()

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_every_field_carries_a_title_and_one_helper_line(self):
        for name, markup in self.pages.items():
            controls = controls_of(markup)
            fields = FIELD_BLOCK.findall(controls)
            with self.subTest(page=name):
                self.assertTrue(fields)
                # Every input in the column is inside one of those fields.
                self.assertEqual(
                    len(ANY_INPUT.findall(controls)),
                    sum(len(ANY_INPUT.findall(body)) for body in fields),
                )
                for body in fields:
                    title = re.search(r"<span class=t>([^<]+)</span>", body)
                    helper = re.search(r"<span class=h>([^<]+)</span>", body)
                    self.assertIsNotNone(title, body)
                    self.assertIsNotNone(helper, body)
                    self.assertTrue(title.group(1).strip())
                    self.assertTrue(helper.group(1).strip())

    def test_a_helper_is_one_short_line(self):
        # A helper is brief and fits one line, so it is measured rather than
        # hoped for: one sentence-ish, no line break, and no essay.
        for name, markup in self.pages.items():
            for body in FIELD_BLOCK.findall(controls_of(markup)):
                helper = re.search(r"<span class=h>([^<]+)</span>", body)
                with self.subTest(page=name, helper=helper.group(1)):
                    self.assertLessEqual(len(helper.group(1)), 72)
                    self.assertNotIn("\n", helper.group(1))

    def test_the_helper_is_also_what_a_hover_says(self):
        # One sentence, two ways of meeting it: the tooltip never disagrees
        # with the line printed under the field.
        for name, markup in self.pages.items():
            for body in FIELD_BLOCK.findall(controls_of(markup)):
                helper = re.search(r"<span class=h>([^<]+)</span>", body)
                title = re.search(r'title="([^"]*)"', body)
                with self.subTest(page=name, helper=helper.group(1)):
                    self.assertEqual(title.group(1), helper.group(1))

    def test_no_page_says_the_retired_word(self):
        # In KiCad, "sheet" means the drawing sheet. The material round the
        # boards is the RAIL, so the pages do not call it a sheet.
        for name, markup in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual(naming.retired_words(markup), [])
                self.assertEqual(naming.violations(markup), [])

    def test_the_fence_reads_the_state_the_page_draws_from_as_well(self):
        # The report, the notes and the subtitle are STATE, not markup, and
        # they are exactly where a stage's own wording would slip through.
        for name, markup in self.pages.items():
            state = json.loads(
                re.search(r"let PB = (\{.*?\});\n", markup, re.S).group(1)
            )
            with self.subTest(page=name):
                self.assertEqual(naming.retired_words("", state), [])

    def test_the_fence_would_catch_it(self):
        # A fence nobody has seen fail is a fence nobody has tested.
        self.assertEqual(
            naming.retired_words("<div>the sheet margin</div>"), ["sheet"]
        )
        self.assertEqual(
            naming.retired_words("", {"subtitle": "a 20 x 40 mm Sheet"}),
            ["sheet"],
        )
        # A word that merely contains it is not it.
        self.assertEqual(naming.retired_words("<div>sheets of ice</div>"), [])


# ---------------------------------------------------------------------------
# the rails and the recalculation, RUN against a stated DOM
# ---------------------------------------------------------------------------


def js_slice(source: str, first: str, last: str) -> str:
    """The page's own declarations, lifted verbatim between two markers.

    The field tables are DATA, not functions, and a harness that restated them
    would be testing its own copy of the page rather than the page.
    """
    start = source.index(first)
    end = source.index(last, start) + len(last)
    return source[start:end]


RAIL_HARNESS = r"""
%(tables)s
%(functions)s
let busy = false, stopped = false;
const out = {posted: [], said: [], logged: []};
const NODES = {};
// An <input> keeps a STRING, whatever is assigned to it - which is why the
// page parses on the way out. Modelled, or the harness would be kinder to the
// code than a browser is.
function node(id, options) {
  let held = '';
  // `dataset`, `disabled` and `options` are modelled because the page uses all
  // three: `data-off` is the shared half's intrinsic-disabled idiom, and a
  // numeric dropdown is written by matching its own option values.
  // `querySelectorAll` is modelled empty because `setDependent` calls it to
  // disable a block's own controls, and this harness reads `classList`
  // directly instead.
  const el = {id: id, checked: false, disabled: false,
              dataset: {}, options: (options || []).map(v => ({value: v})),
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
 'pb-rail-bottom', 'pb-rail-left', 'pb-rail-right',
 'pb-holes-on', 'pb-hole-d', 'pb-hole-tl', 'pb-hole-tr', 'pb-hole-br',
 'pb-hole-bl', 'pb-hole-place', 'pb-hole-x', 'pb-hole-y', 'pb-hole-insets',
 'pb-copper-on', 'pb-copper-clear', 'pb-copper-between',
 'pb-scores-on', 'pb-score-rails-h', 'pb-score-rails-v',
 'pb-holes-body', 'pb-copper-body', 'pb-scores-body',
 // THE PLACEMENT SUITE's own controls.
 'pb-nestx', 'pb-nesty', 'pb-clearance', 'pb-alternate',
 'pb-placement', 'pb-placed', 'pb-placed-clear'].forEach(id => node(id));
node('pb-rotation', ['0', '90', '180', '270']);
node('pb-rail-unlock');
node('pb-persides');
node('pb-rail-linked');
globalThis.document = {
  getElementById: id => NODES[id] || null,
  activeElement: null,
};
const unlockEl = NODES['pb-rail-unlock'];
const persidesEl = NODES['pb-persides'];
const linkedEl = NODES['pb-rail-linked'];
let PB = {params: {}};
function say(text, kind) { out.said.push(text); }
function logEvent(type, data) { out.logged.push({type: type, data: data}); }
// The real `post` refreshes, and the refresh redraws the fields from what the
// server now holds. Modelled, because without it every field stays "changed"
// and the second entry posts the first one all over again.
async function post(path, body, what) {
  out.posted.push({path: path, body: body, what: what});
  Object.assign(PB.params, body);
  if ('rail_mm' in body) {
    PB.params.rails_linked = true;
    SIDE_RAILS.forEach(pair => { PB.params[pair[1]] = body.rail_mm; });
  } else if (SIDE_RAILS.some(pair => pair[1] in body)) {
    PB.params.rails_linked = false;
    PB.params.rail_mm = Math.max.apply(
      null, SIDE_RAILS.map(pair => PB.params[pair[1]])
    );
  }
  drawPanelParams(PB.params);
  return {ok: true};
}
// The live half of the rail spelling wears `classList` `off` (greyed), not
// `.hidden`.
function mode() {
  return {checked: unlockEl.checked,
          persides: persidesEl.classList.contains('off'),
          linked: linkedEl.classList.contains('off')};
}

// -- LINKED, which is what a config saying one number gives ----------------
drawPanelParams(%(linked)s);
out.linkedFilled = ['pb-columns', 'pb-gapx', 'pb-rail', 'pb-rail-top']
  .map(id => NODES[id].value);
out.linkedMode = mode();

// A layout number, typed and entered.
NODES['pb-gapx'].value = '4';
await applyPanelParams('enter');

// The per-side fields are HIDDEN in linked mode, so nothing reads them.
NODES['pb-rail-top'].value = '99';
await applyPanelParams('enter');

// The rail itself, typed into the one field that is on screen.
NODES['pb-rail-top'].value = '5';
NODES['pb-rail'].value = '7';
await applyPanelParams('enter');

// -- the unlock ------------------------------------------------------------
NODES['pb-rail'].value = '5';
await setRailMode(true, 'unlock');

drawPanelParams(%(unlocked)s);
out.unlockedFilled = ['pb-rail', 'pb-rail-top', 'pb-rail-right']
  .map(id => NODES[id].value);
out.unlockedMode = mode();

// One side, typed. The linked field is hidden now, so it is not read.
NODES['pb-rail-left'].value = '4';
NODES['pb-rail'].value = '123';
await applyPanelParams('enter');

// -- and back to one number ------------------------------------------------
await setRailMode(false, 'unlock');

// Nothing changed at all: an unchanged value costs nothing.
drawPanelParams(%(linked)s);
out.saidBefore = out.said.length;
await applyPanelParams('enter');
out.quiet = out.said.slice(out.saidBefore);
console.log(JSON.stringify(out));
"""

LINKED_PARAMS = {
    "columns": 2, "rows": 1, "gap_x_mm": 3.0, "gap_y_mm": 3.0,
    "rail_mm": 5.0, "rail_top_mm": 5.0, "rail_bottom_mm": 5.0,
    "rail_left_mm": 5.0, "rail_right_mm": 5.0, "rails_linked": True,
}
UNLOCKED_PARAMS = {
    "columns": 2, "rows": 1, "gap_x_mm": 3.0, "gap_y_mm": 3.0,
    "rail_mm": 8.0, "rail_top_mm": 1.0, "rail_bottom_mm": 8.0,
    "rail_left_mm": 3.0, "rail_right_mm": 0.0, "rails_linked": False,
}


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientRailAndRecalcTest(unittest.TestCase):
    """The panel page's own wiring, RUN: what a field entry posts, and when.

    Two behaviours, and neither can be read off a string. First, entering a
    number posts it, which is what makes the panel recalculate. Second, the
    rails are ONE setting with two spellings, and the page reads whichever one
    the config carries - so a hidden field cannot post a number and change the
    shape of `panel.rails` behind the user.
    """

    @classmethod
    def setUpClass(cls):
        cls.out = run_js(RAIL_HARNESS % {
            "tables": js_slice(
                page_panel.PANEL_JS, "const LAYOUT_FIELDS = [",
                "COPPER_FIELDS, SCORE_FIELDS, DIMENSION_FIELDS, TEXT_FIELDS\n);",
            ),
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
                                 "changedPanelParams", "applyPanelParams",
                                 "setRailMode")
                ]
                # `setDependent` greys a dependent block; it is the shared
                # half's function, used on both pages.
                + [js_function(page_served.CONTROLS_JS, "setDependent")]
            ),
            "linked": json.dumps(LINKED_PARAMS),
            "unlocked": json.dumps(UNLOCKED_PARAMS),
        })

    def posted(self):
        return self.out["posted"]

    def test_a_linked_config_shows_one_rail_field_and_hides_the_four(self):
        self.assertEqual(self.out["linkedFilled"], ["2", "3", "5", "5"])
        self.assertEqual(self.out["linkedMode"],
                         {"checked": False, "persides": True, "linked": False})

    def test_entering_a_layout_number_posts_it_and_says_it_recalculates(self):
        first = self.posted()[0]
        self.assertEqual(first["path"], "/set_param")
        self.assertEqual(first["body"], {"gap_x_mm": 4.0})
        self.assertIn("recalculating", first["what"])

    def test_a_hidden_per_side_field_is_never_read(self):
        # THE TRAP this design avoids: a per-side number left over in the DOM
        # would migrate the config to four rails on the next Enter.
        for entry in self.posted():
            self.assertNotIn("rail_top_mm=99", str(entry["body"]))
        self.assertFalse(any(entry["body"].get("rail_top_mm") == 99.0
                             for entry in self.posted()))

    def test_the_linked_rail_posts_the_one_number(self):
        self.assertEqual(self.posted()[1]["body"], {"rail_mm": 7.0})

    def test_unlocking_posts_the_four_sides_the_one_number_meant(self):
        body = self.posted()[2]["body"]
        self.assertEqual(body, {"rail_top_mm": 5.0, "rail_bottom_mm": 5.0,
                                "rail_left_mm": 5.0, "rail_right_mm": 5.0})
        self.assertIn("unlocking", self.posted()[2]["what"])

    def test_an_unlocked_config_shows_the_four_and_hides_the_one(self):
        self.assertEqual(self.out["unlockedFilled"], ["8", "1", "0"])
        self.assertEqual(self.out["unlockedMode"],
                         {"checked": True, "persides": False, "linked": True})

    def test_one_side_posts_only_that_side(self):
        self.assertEqual(self.posted()[3]["body"], {"rail_left_mm": 4.0})

    def test_relinking_posts_the_widest_of_the_four(self):
        # Collapsing four rails into one must not quietly delete material.
        self.assertEqual(self.posted()[4]["body"], {"rail_mm": 8.0})
        self.assertIn("linking", self.posted()[4]["what"])

    def test_an_unchanged_value_posts_nothing_and_says_so(self):
        self.assertEqual(len(self.posted()), 5)
        self.assertEqual(len(self.out["quiet"]), 1)
        self.assertIn("already what panel.json says", self.out["quiet"][0])

    def test_every_entry_is_journaled_as_a_parameter(self):
        kinds = [entry["type"] for entry in self.out["logged"]]
        self.assertEqual(kinds, ["param"] * 5)
        self.assertEqual(self.out["logged"][0]["data"]["page"], "panel")
        self.assertEqual(self.out["logged"][2]["data"]["cause"], "unlock")


class LauncherStdoutTest(unittest.TestCase):
    """The launcher's contract: the URL line arrives, on a PIPE, while it runs.

    Python block-buffers a piped stdout, so without an explicit flush the whole
    preamble sits in an 8 KB buffer while the server blocks below it - and a
    caller waiting for the URL waits for ever. The
    KiCad plugin is exactly that caller, so this is driven the way the plugin
    will drive it: a real process, a real pipe, and a read that must not hang.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self._folder.cleanup()

    def test_the_url_reaches_a_reader_before_the_server_blocks(self):
        self.proc = subprocess.Popen(  # noqa: S603 - this interpreter, no shell
            [sys.executable, "-m", "eedesignpanels.preview",
             str(self.config_path), "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "EEDESIGNPANELS_SCRATCH": str(self.folder / "s")},
        )
        deadline = time.monotonic() + 120.0
        url = ""
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if re.match(r"^http://127\.0\.0\.1:\d+/$", line.strip()):
                url = line.strip()
                break
        self.assertTrue(url, "the preview never printed its URL to the pipe")
        # ...and it is a server, not just a print: it answers on that address.
        with urllib.request.urlopen(url + "state", timeout=30.0) as reply:
            self.assertTrue(json.loads(reply.read().decode("utf-8"))["ok"])
        with urllib.request.urlopen(url + "panel", timeout=30.0) as reply:
            markup = reply.read().decode("utf-8")
        self.assertIn("pb-build", markup)
        # The server runs in another process, so the token is read off the
        # page it served, exactly as the page's own script reads it.
        token = re.search(r'<meta name=pb-token content="([^"]+)">',
                          markup).group(1)
        request = urllib.request.Request(
            url + "shutdown", data=b"{}",
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30.0) as reply:
            self.assertEqual(reply.status, 200)
        self.proc.wait(timeout=60)


@unittest.skipUnless(shutil.which("node"), "no node to parse the page's script")
class PanelScriptParsesTest(unittest.TestCase):
    """The panel page's whole script, parsed. A syntax error takes every control."""

    def parses(self, script: str, name: str) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"{name}.mjs"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), "--check", str(path)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_served_script_is_valid_javascript(self):
        self.parses(page_served.page_script(page_panel.PANEL_JS,
                                            {"page": "panel"}), "panel")

    def test_every_script_on_every_served_page_parses(self):
        # THREE pages and every block in each: the viewer, the layer panel and
        # the controls. A syntax error in any one of them takes the page, and
        # the panel page is now served in two states rather than one.
        with tempfile.TemporaryDirectory() as folder:
            config_path = write_panel_project(Path(folder))
            session = a_session(Path(folder), config_path)
            pages = {"prep": session.page(),
                     "panel_waiting": session.panel_page()}
            session.build_panel({})
            pages["panel_built"] = session.panel_page()
        for name, markup in pages.items():
            blocks = re.findall(r"<script>(.*?)</script>", markup, re.S)
            self.assertTrue(blocks, f"{name} carries no script at all")
            for index, block in enumerate(blocks):
                with self.subTest(page=name, block=index):
                    self.parses(block, f"{name}_{index}")

    def test_both_pages_share_the_same_two_halves(self):
        prep_script = page_served.page_script(page_served.PREP_JS, {})
        panel_script = page_served.page_script(page_panel.PANEL_JS, {})
        for token in ("function setBusy(", "function drawPending(",
                      "function refresh(cause)", "function applyLayerState(",
                      "function reportProblem(",
                      "function stopServer("):
            with self.subTest(token=token):
                self.assertIn(token, prep_script)
                self.assertIn(token, panel_script)
        # ...and each page defines exactly one `applyFragment`.
        self.assertEqual(prep_script.count("function applyFragment("), 1)
        self.assertEqual(panel_script.count("function applyFragment("), 1)

    def test_neither_page_carries_the_others_controls(self):
        prep_script = page_served.page_script(page_served.PREP_JS, {})
        panel_script = page_served.page_script(page_panel.PANEL_JS, {})
        self.assertIn("function drawCandidates(", prep_script)
        self.assertNotIn("function drawCandidates(", panel_script)
        self.assertIn("function buildPanel(", panel_script)
        self.assertNotIn("function buildPanel(", prep_script)


if __name__ == "__main__":
    unittest.main()
