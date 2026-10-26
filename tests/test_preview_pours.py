"""The pour phase: real KiCad copper in the panel the user is looking at.

Nothing in this project computes a copper fill, so a fill is a KiCad round
trip: add the pours in memory, write them to a temporary file, force the pour
through kicad-cli, and read the result back into memory.

So the rail pour is filled by KiCad and transplanted into the panel the session
HOLDS - which means the picture and the saved file both carry real copper. The
round trip is 9.9 s on the corpus panel, so it runs behind the picture, and what
can go wrong there is ordering rather than geometry:

* the round trip must hold NO session lock, or every edit made during it waits
  ten seconds for copper nobody asked for yet;
* an answer for a panel that has been replaced must be DROPPED, never applied -
  a fill transplanted into a superseded panel is copper for a layout the
  user has already changed;
* a save must never write bare rails silently: it waits for a fill in flight,
  and does the round trip itself when there is none;
* a fill that FAILS must say so once - on the picture and in the notes - and
  leave a valid, clearly unfilled panel.

None of that is readable off the code, so the session is driven with a filler
that can be held open inside itself, the way the content phase is tested
(`test_preview_phases`). The TRANSPLANT behind it is always the real one, so
these tests exercise the real byte gate.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eedesigncore import Board, ZoneFilledPolygon

from eedesignpanels import fplib
from eedesignpanels.config import load_config
from eedesignpanels.preview import panelbuild
from eedesignpanels.preview.session import (
    FILLING_NOTE,
    PreviewSession,
)
from eedesignpanels.stages import rail_copper, refill
from test_panel_fiducials import a_library
from test_panel_page import write_panel_project
from test_preview import stub_render
from test_preview_journal import open_session

# Long enough that a held filler is released by the test and never by a timeout;
# short enough that a wedged one fails the run rather than hanging it.
WAIT = 30.0


def pour_render(board, layers, viewbox_mm, colors, theme=None, workdir=None,
                content=False, origin=None, board_lock=None):
    """`stub_render`, in the seam's whole contract - the board lock included.

    The content phase hands the seam the session's lock for its board write, so
    a renderer that cannot be given one turns every content render into a
    failure and there is no fill phase behind it to test.
    """
    return stub_render(board, layers, viewbox_mm, colors, theme, workdir,
                       content)


def pour_project(folder: Path) -> Path:
    """The panel fixture, with the rail pour switched on in its config."""
    config_path = write_panel_project(folder)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["panel"]["rail_copper"] = {"enabled": True, "clearance_mm": 0.635}
    config_path.write_text(json.dumps(data, indent=2) + "\n",
                           encoding="utf-8")
    return config_path


def fake_taken(outcome, records: int = 1) -> refill.FillTaken:
    """What a filler would hand back: one record per copper layer per pour.

    It fakes the KiCad round trip and NOTHING else - the transplant that
    consumes this is the real one, gate included, so a wrong answer here is
    refused exactly as a wrong answer from kicad-cli would be.
    """
    zones = rail_copper.pour_zones(outcome.board)
    return refill.FillTaken(
        records={
            zone.id: [
                ZoneFilledPolygon(
                    layer=layer,
                    pts=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
                )
                for layer in (zone.layers or [])[:records]
            ]
            for zone in zones
        },
        seconds=0.25,
        cli_path=Path("stub-kicad-cli"),
    )


def wait_watching(release, cancel, filler):
    """Wait like kicad-cli does: watching the cancel token the session passes.

    The real round trip ends its KiCad when the token is set and raises
    `RefillCancelled` (`refill.take_fills`). A double that slept through the
    token would let the session wait for a fill nobody wants, and the
    latest-wins tests could not see the difference.
    """
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            filler.cancelled += 1
            raise refill.RefillCancelled(
                "the zone re-fill was stopped after a newer change",
                seconds=0.001,
            )
        if release.wait(0.01):
            return
    raise AssertionError("a held filler was never released")


class HeldFill:
    """A round trip that says when it started, and can be stopped inside itself.

    `free_while_running` is the property the whole phase rests on: the round
    trip is ten seconds long, and if it held the session lock the page would be
    dead for all of it.
    """

    def __init__(self, records: int = 1, error: BaseException = None):
        self.records = records
        self.error = error
        self.calls = 0
        self.work_dirs = []
        # WHAT WAS ON THE BOARD each time the round trip was asked for. KiCad
        # fills a BOARD and not a zone, so this is the whole question behind the
        # flooded-fiducial defect: a fill taken before a pad was
        # placed is a fill that never held off that pad.
        self.references = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = None
        self.free_while_running = None
        # How many round trips were STOPPED by a newer change.
        self.cancelled = 0

    def hold(self) -> None:
        self.release.clear()
        self.entered.clear()

    def let_go(self) -> None:
        self.release.set()

    def __call__(self, outcome, cli_path=None, work_dir=None, cancel=None):
        self.calls += 1
        self.work_dirs.append(work_dir)
        self.references.append([
            prop.value for footprint in outcome.board.footprints
            for prop in footprint.properties if prop.name == "Reference"
        ])
        if self.lock is not None:
            # `acquire(blocking=False)` fails when ANOTHER thread holds it,
            # which is exactly the question being asked.
            taken = self.lock.acquire(blocking=False)
            self.free_while_running = taken
            if taken:
                self.lock.release()
        self.entered.set()
        wait_watching(self.release, cancel, self)
        if self.error is not None:
            raise self.error
        return fake_taken(outcome, self.records)


class PourSeamTest(unittest.TestCase):
    """The two halves of the fill, and the question "does this panel owe one?"."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config = load_config(pour_project(folder))
        cls.plain = load_config(write_panel_project(folder / "plain"))
        cls.board = Board.from_file(cls.config.source_board_path)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def derived(self, config=None):
        return panelbuild.derive(
            config or self.config, self.board, fill_rails=False
        ).outcome

    def test_a_panel_with_an_empty_pour_owes_a_fill(self):
        outcome = self.derived()
        view = SimpleNamespace(outcome=outcome, fill_rails=False,
                               fill_error="")
        self.assertTrue(panelbuild.owes_fill(view))

    def test_a_panel_with_no_pour_owes_nothing(self):
        view = SimpleNamespace(outcome=self.derived(self.plain),
                               fill_rails=False, fill_error="")
        self.assertFalse(panelbuild.owes_fill(view))
        self.assertIsNone(panelbuild.pour_of(self.derived(self.plain)))

    def test_a_filled_panel_owes_nothing(self):
        view = SimpleNamespace(outcome=self.derived(), fill_rails=True,
                               fill_error="")
        self.assertFalse(panelbuild.owes_fill(view))

    def test_a_panel_whose_fill_failed_is_not_asked_again(self):
        view = SimpleNamespace(outcome=self.derived(), fill_rails=False,
                               fill_error="kicad-cli is not there")
        self.assertFalse(panelbuild.owes_fill(view))

    def test_nothing_owes_a_fill_before_a_panel_exists(self):
        self.assertFalse(panelbuild.owes_fill(None))

    def test_the_round_trip_is_not_taken_for_a_panel_with_no_pour(self):
        # No pour, no process: `take_fill` answers without looking for kicad-cli
        # at all, which is what keeps a config with no rail copper headless.
        self.assertIsNone(panelbuild.take_fill(self.derived(self.plain)))

    def test_the_transplant_puts_the_copper_on_the_held_panel(self):
        outcome = self.derived()
        filled, result = panelbuild.apply_fill(outcome, fake_taken(outcome, 2))
        zones = rail_copper.pour_zones(filled.board)
        self.assertTrue(zones)
        for zone in zones:
            self.assertEqual(len(zone.filled_polygons), 2)
        self.assertEqual(result.zones_filled, len(zones))
        self.assertTrue(result.gate_passed)

    def test_the_report_stops_saying_the_pours_are_empty(self):
        outcome = self.derived()
        before = panelbuild.report_lines(self.config, outcome)
        self.assertTrue(any("only KiCad can fill them" in line
                            for line in before))
        filled, _ = panelbuild.apply_fill(outcome, fake_taken(outcome))
        after = panelbuild.report_lines(self.config, filled)
        self.assertFalse(any("only KiCad can fill them" in line
                             for line in after))
        self.assertTrue(any("carry copper" in line for line in after))

    def test_an_answer_for_the_wrong_panel_is_refused(self):
        outcome = self.derived()
        empty = refill.FillTaken(records={}, seconds=0.0, cli_path=Path("x"))
        with self.assertRaises(refill.RefillError):
            panelbuild.apply_fill(outcome, empty)


class PourPhaseTestCase(unittest.TestCase):
    """A recorded session over the stub renderer and a filler it can hold."""

    fills = True
    records = 1
    error = None

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = pour_project(self.folder)
        self.recording = open_session(self.folder,
                                      config_path=self.config_path)
        self.fill = HeldFill(records=self.records, error=self.error)
        self.session = PreviewSession(
            self.config_path, self.recording.folder, render=pour_render,
            journal=self.recording, two_phase=True, fill=self.fill,
            fills=self.fills,
        )
        self.fill.lock = self.session._lock

    def tearDown(self):
        self.fill.let_go()
        self.session.close()
        self._folder.cleanup()

    def records_of(self, kind: str = "") -> list:
        return [r for r in self.recording.records()
                if not kind or r["kind"] == kind]

    def open_the_panel(self) -> None:
        self.session.refresh()
        self.session.start_panel()

    def pours(self) -> list:
        return rail_copper.pour_zones(self.session.panel.outcome.board)


class FillPhaseTest(PourPhaseTestCase):
    """The fill runs behind the picture and lands in the panel that is held."""

    def test_the_copper_lands_on_the_panel_the_session_holds(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertTrue(self.session.panel.fill_rails)
        zones = self.pours()
        self.assertTrue(zones)
        for zone in zones:
            self.assertTrue(zone.filled_polygons)

    def test_the_report_under_the_picture_says_what_landed(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertTrue(any("carry copper" in line
                            for line in self.session.panel.report))

    def test_the_content_renders_come_first_and_the_fill_last(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        subjects = [r["data"]["subject"] for r in self.records_of("upgrade")]
        self.assertEqual(subjects, ["board", "panel"])
        self.assertEqual(len(self.records_of("panel.fill")), 1)
        landed = self.records_of("panel.fill")[0]["data"]
        self.assertTrue(landed["applied"])
        self.assertGreater(landed["records"], 0)
        self.assertEqual(landed["zones_empty"], 0)

    def test_the_picture_is_drawn_again_with_the_copper_in_it(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        drawn = self.records_of("panel.fill.drawn")
        self.assertEqual(len(drawn), 1)
        self.assertGreater(drawn[0]["data"]["layers"], 0)
        # ...and the page is told there is a better picture of the same panel.
        self.assertEqual(self.session.upgrade_seq,
                         drawn[0]["data"]["upgrade_seq"])
        self.assertEqual(self.session.generation,
                         drawn[0]["data"]["generation"])

    def test_the_fill_does_not_move_the_generation(self):
        self.open_the_panel()
        generation = self.session.generation
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.generation, generation)

    def test_the_round_trip_runs_with_the_session_lock_free(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        # The page's own question, asked the way the page asks it: can anything
        # else read this session while KiCad is filling?
        self.assertTrue(self.session._lock.acquire(timeout=WAIT))
        self.session._lock.release()
        self.assertTrue(self.fill.free_while_running,
                        "the round trip held the session lock")
        self.assertFalse(self.session.panel.fill_rails)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))

    def test_the_sandbox_board_goes_in_this_session_s_scratch_folder(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.fill.work_dirs,
                         [self.session.workdir / "fill"])

    def test_the_corner_of_the_viewport_says_the_pour_is_being_filled(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        fragment = self.session.panel_fragment()
        self.assertEqual(fragment["content_note"], FILLING_NOTE)
        self.assertTrue(fragment["awaiting_content"])
        self.assertFalse(fragment["rail_copper_filled"])
        # ...and the PREP page says nothing about a pour it has no picture of.
        self.assertEqual(self.session.fragment()["content_note"], "")
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        after = self.session.panel_fragment()
        self.assertEqual(after["content_note"], "")
        self.assertFalse(after["awaiting_content"])
        self.assertTrue(after["rail_copper_filled"])

    def test_a_session_with_the_pour_phase_off_never_asks_for_a_fill(self):
        # No kicad-cli on the machine is this case, and the honest answer is an
        # unfilled picture that says the pours are empty.
        quiet = PreviewSession(
            self.config_path, self.folder / "quiet", render=pour_render,
            two_phase=True, fill=self.fill, fills=False,
        )
        quiet.refresh()
        quiet.start_panel()
        self.assertTrue(quiet.wait_for_upgrades(WAIT))
        self.assertEqual(self.fill.calls, 0)
        self.assertFalse(quiet.panel.fill_rails)
        self.assertEqual(quiet.panel_fragment()["content_note"], "")
        quiet.close()


class StalenessTest(PourPhaseTestCase):
    """An answer about a panel nobody is looking at is dropped, never applied."""

    def test_a_panel_edit_stops_the_fill_that_is_running(self):
        # LATEST WINS. The edit is NOT held behind
        # the round trip: it sets the token, the round trip ends inside itself,
        # and the answer that was being computed is never applied.
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        stale = self.session.generation

        self.session.set_param({"gap_x_mm": 4.0})
        self.assertGreater(self.session.generation, stale)
        self.assertEqual(self.fill.cancelled, 1)
        cancelled = self.records_of("upgrade.cancelled")
        self.assertTrue(cancelled)

        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        dropped = self.records_of("panel.fill.dropped")
        self.assertTrue(dropped)
        self.assertEqual(dropped[0]["data"]["generation"], stale)
        self.assertEqual(dropped[0]["data"]["reason"],
                         "cancelled mid round trip")
        # ...and the stop is NOT recorded as a failure, so the panel that is
        # there now still gets its copper.
        self.assertFalse(self.records_of("panel.fill.failed"))
        self.assertTrue(self.session.panel.fill_rails)

    def test_the_edit_does_not_wait_for_the_round_trip_it_stopped(self):
        # THE RESPONSIVE PATH: the window never blocks while it waits for a
        # zone refill round trip. The filler is held open
        # and never released, so an edit that waited for it would take the full
        # WAIT; this one takes about a poll interval.
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        started = time.monotonic()
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertLess(time.monotonic() - started, WAIT / 3.0)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))

    def test_a_board_edit_stops_it_too_and_says_it_was_the_edit(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.session.set_param({"cutter_diameter": 1.2})
        cancelled = [r["data"]["reason"]
                     for r in self.records_of("upgrade.cancelled")]
        self.assertIn("a new edit", cancelled)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(
            [r["data"]["reason"]
             for r in self.records_of("panel.fill.dropped")],
            ["cancelled mid round trip"],
        )

    def test_three_edits_back_to_back_leave_only_the_last_one_filled(self):
        # Latest wins: a zone-affecting change that lands before the running
        # fill is done stops that fill and starts its own. Three gaps typed in
        # a row, and the panel that ends up with copper is
        # the one the third edit made.
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        for gap in (4.0, 5.0, 6.0):
            self.fill.entered.clear()
            self.session.set_param({"gap_x_mm": gap})
            self.assertTrue(self.fill.entered.wait(WAIT))
        self.assertEqual(self.fill.cancelled, 3)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        # One fill applied, three stopped, and no failure anywhere.
        self.assertEqual(len(self.records_of("panel.fill")), 1)
        self.assertEqual(len(self.records_of("panel.fill.dropped")), 3)
        self.assertFalse(self.records_of("panel.fill.failed"))
        self.assertEqual(self.session.config.grid.gap_across_mm, 6.0)
        self.assertTrue(self.session.panel.fill_rails)

    def test_the_panel_the_edit_produced_is_the_one_that_gets_filled(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertFalse(self.session.panel.fill_rails)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        # Two round trips: the cancelled one and the one for the panel now on
        # screen. Exactly one fill was applied, and it is this panel's.
        self.assertEqual(self.fill.calls, 2)
        self.assertEqual(len(self.records_of("panel.fill")), 1)
        self.assertTrue(self.session.panel.fill_rails)
        self.assertEqual(self.records_of("panel.fill")[0]["data"]["generation"],
                         self.session.generation)

    def test_an_answer_for_a_panel_that_was_replaced_is_not_transplanted(self):
        # The panel object is replaced WITHOUT the generation moving - which is
        # what a recalculation inside one action looks like - so the guard that
        # catches this is the identity check and not the generation.
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        before = self.session.panel
        self.session.panel = self.session._compute_panel(fill_rails=False)
        self.assertIsNot(self.session.panel, before)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        dropped = [r["data"]["reason"]
                   for r in self.records_of("panel.fill.dropped")]
        self.assertIn("the panel was replaced", dropped)


class FiducialFillTest(PourPhaseTestCase):
    """The fill is asked about the FINISHED panel, fiducials included.

    A fill taken before the fiducials are placed has no clearance ring round
    them - the pour floods their copper. So the live path fills the finished
    panel, and this class fences that it stays that way.

    Two properties, and both are about ordering rather than geometry: the round
    trip is handed a board that already carries the targets, and a fiducial edit
    made while a fill is in flight throws that answer away rather than
    transplanting copper poured round a fiducial that has since moved.
    """

    def setUp(self):
        super().setUp()
        self.library = a_library(self.folder)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["panel"]["fiducials"] = {
            "enabled": True,
            "footprint": "Fiducial_1mm_Mask2mm",
            "library": "Fixture",
            "library_path": str(self.library),
            "offset_mm": 2.0,
        }
        self.config_path.write_text(json.dumps(data, indent=2) + "\n",
                                    encoding="utf-8")
        fplib.forget()
        self.session.config = load_config(self.config_path)
        self.session.data = json.loads(
            self.config_path.read_text(encoding="utf-8")
        )

    def tearDown(self):
        fplib.forget()
        super().tearDown()

    def targets(self, references) -> list:
        return [name for name in references if name.startswith("PNLFD")]

    def test_the_round_trip_is_handed_a_panel_that_already_has_the_targets(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertTrue(self.fill.references)
        self.assertTrue(
            self.targets(self.fill.references[-1]),
            "KiCad was asked to fill a panel with no fiducial on it",
        )

    def test_the_held_panel_carries_both_the_targets_and_the_copper(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertTrue(self.session.panel.fill_rails)
        board = self.session.panel.outcome.board
        references = [
            prop.value for footprint in board.footprints
            for prop in footprint.properties if prop.name == "Reference"
        ]
        self.assertTrue(self.targets(references))

    def test_a_fiducial_edit_drops_the_fill_that_is_in_flight(self):
        # THE STALENESS RULE, said for the one edit that changes what the fill
        # has to hold off. A pour filled round a target at 2 mm is the wrong
        # copper for a panel whose target is at 4 mm.
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        stale = self.session.generation

        self.session.set_param({"fiducial_offset_mm": 4.0})
        self.assertGreater(self.session.generation, stale)

        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        dropped = self.records_of("panel.fill.dropped")
        self.assertTrue(dropped)
        self.assertEqual(dropped[0]["data"]["generation"], stale)

    def test_the_fill_that_lands_is_the_one_for_the_edited_panel(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.session.set_param({"fiducial_offset_mm": 4.0})
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(len(self.records_of("panel.fill")), 1)
        self.assertEqual(self.records_of("panel.fill")[0]["data"]["generation"],
                         self.session.generation)
        self.assertTrue(self.session.panel.fill_rails)
        # ...and the answer that landed was taken about a panel with a target.
        self.assertTrue(self.targets(self.fill.references[-1]))


class FillFailureTest(PourPhaseTestCase):
    """A fill that cannot be done is said once, and the panel stays honest."""

    error = refill.RefillError("kicad-cli is not at C:/nowhere/kicad-cli.exe")

    def run_it(self):
        with mock.patch("traceback.print_exc"):
            self.open_the_panel()
            self.assertTrue(self.session.wait_for_upgrades(WAIT))

    def test_the_panel_is_still_there_and_clearly_unfilled(self):
        self.run_it()
        self.assertIsNotNone(self.session.panel)
        self.assertFalse(self.session.panel.fill_rails)
        self.assertEqual(self.session.panel_error, "")
        for zone in self.pours():
            self.assertFalse(zone.filled_polygons)

    def test_the_reason_reaches_the_picture_and_the_notes(self):
        self.run_it()
        fragment = self.session.panel_fragment()
        self.assertIn("could not be filled", fragment["content_note"])
        self.assertIn("nowhere", fragment["content_note"])
        self.assertTrue(fragment["content_error"])
        self.assertFalse(fragment["awaiting_content"])
        self.assertTrue(any("could not be filled" in note
                            for note in fragment["notes"]))

    def test_it_is_said_once_and_not_retried(self):
        self.run_it()
        failed = self.records_of("panel.fill.failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("nowhere", failed[0]["data"]["error"])
        self.assertEqual(self.fill.calls, 1)
        self.assertFalse(self.records_of("upgrade.gave_up"))
        self.assertFalse(self.records_of("panel.fill"))

    def test_the_page_still_serves(self):
        self.run_it()
        markup = self.session.panel_page()
        self.assertIn("could not be filled", markup)


class SaveWaitsForTheFillTest(PourPhaseTestCase):
    """A save never writes bare rails - it waits, or it fills them itself."""

    def written_board(self) -> Board:
        return Board.from_file(self.session.config.output_board_path)

    def saved_pours_carry_copper(self) -> None:
        zones = rail_copper.pour_zones(self.written_board())
        self.assertTrue(zones)
        for zone in zones:
            self.assertTrue(zone.filled_polygons,
                            "the saved panel has a bare rail")

    def test_a_save_waits_for_a_fill_that_is_already_running(self):
        self.fill.hold()
        self.open_the_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))

        done = threading.Event()
        replies = []

        def save():
            replies.append(self.session.save_panel())
            done.set()

        saver = threading.Thread(target=save, name="test-save", daemon=True)
        saver.start()
        # It is WAITING, not writing: the panel it would write has empty pours.
        self.assertFalse(done.wait(0.5))
        self.assertFalse(self.session.config.output_board_path.exists())

        self.fill.let_go()
        self.assertTrue(done.wait(WAIT))
        saver.join(WAIT)
        self.saved_pours_carry_copper()
        # ONE round trip: the save waited for the one in flight rather than
        # starting a second ten-second process of its own.
        self.assertEqual(self.fill.calls, 1)
        waited = self.records_of("panel.fill.waited")
        self.assertTrue(waited)
        self.assertTrue(waited[0]["data"]["finished"])
        self.assertIn("waited", replies[0]["message"])
        saved = self.records_of("panel.save")[-1]["data"]
        self.assertTrue(saved["rail_copper_filled"])
        self.assertGreater(saved["fill_waited"], 0.0)

    def test_a_save_with_the_copper_already_in_waits_for_nothing(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.session.save_panel()
        self.saved_pours_carry_copper()
        self.assertEqual(self.fill.calls, 1)
        self.assertFalse(self.records_of("panel.fill.waited"))
        saved = self.records_of("panel.save")[-1]["data"]
        self.assertEqual(saved["fill_seconds"], 0.0)
        self.assertTrue(saved["rail_copper_filled"])


class SaveFillsItselfTest(PourPhaseTestCase):
    """With no pour phase running, the save is the one that pays for the fill."""

    fills = False

    def test_the_save_does_the_round_trip_itself(self):
        self.open_the_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.fill.calls, 0)
        reply = self.session.save_panel()
        self.assertEqual(self.fill.calls, 1)
        self.assertEqual(self.fill.work_dirs,
                         [self.session.workdir / "fill_save"])
        self.assertIn("filled in", reply["message"])
        zones = rail_copper.pour_zones(
            Board.from_file(self.session.config.output_board_path)
        )
        for zone in zones:
            self.assertTrue(zone.filled_polygons)
        landed = self.records_of("panel.fill")[-1]["data"]
        self.assertTrue(landed["saving"])
        self.assertGreater(landed["records"], 0)

    def test_a_second_save_does_not_fill_twice(self):
        self.open_the_panel()
        self.session.save_panel()
        self.session.save_panel()
        self.assertEqual(self.fill.calls, 1)


class SaveFillFailureTest(PourPhaseTestCase):
    """A save whose fill fails writes nothing and says why."""

    fills = False
    error = refill.RefillError("kicad-cli is not at C:/nowhere/kicad-cli.exe")

    def test_the_save_refuses_rather_than_writing_bare_rails(self):
        from eedesignpanels.preview import actions

        self.open_the_panel()
        with self.assertRaises(actions.ActionError) as raised:
            self.session.save_panel()
        self.assertIn("nowhere", str(raised.exception))
        self.assertFalse(self.session.config.output_board_path.exists())
        self.assertTrue(self.session.panel.fill_error)
        self.assertIn("could not be filled",
                      self.session.panel_fragment()["content_note"])


class NoPourSaveTest(unittest.TestCase):
    """A config with no rail copper pays nothing for a pour it does not have."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.fill = HeldFill()
        self.session = PreviewSession(
            self.config_path, self.folder / "work", render=pour_render,
            fill=self.fill, fills=True,
        )
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_no_round_trip_is_taken_and_nothing_is_said_about_copper(self):
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        reply = self.session.save_panel()
        self.assertEqual(self.fill.calls, 0)
        self.assertEqual(self.session.panel_fragment()["content_note"], "")
        self.assertNotIn("copper", reply["message"])
        written = Board.from_file(self.session.config.output_board_path)
        self.assertEqual(rail_copper.pour_zones(written), [])


if __name__ == "__main__":
    unittest.main()
