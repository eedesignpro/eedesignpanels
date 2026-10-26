"""The board's copper, re-filled behind the picture after a score decision.

When v-score lines are added to the source single board, a rule area is also
applied and the copper is re-filled. This makes sure the source board zones are
filled and reviewed before they are frozen into polygons and moved to the panel.

The file half of that is pinned in `test_board_scores.py` and the KiCad half in
`test_board_refill.py`. What is pinned HERE is the ORDERING, which is the part
no reading of the code settles:

* the round trip runs off the responsive path and holds NO session lock;
* a newer decision cancels a pending answer, and a stale answer is dropped
  rather than transplanted into a board that has moved on;
* the picture says, in the corner, that the copper is being re-filled;
* a Save to Board that catches a fill in flight WAITS for it, and one with no
  fill in flight does the round trip itself - a design is never saved with
  copper pouring across its own score;
* a failure is said once and not retried, and the board stays valid.

Driven with a filler the test can hold open inside itself, exactly as the
panel's pour phase is (`test_preview_pours`). The TRANSPLANT behind it is always
the real one, so the real byte gate runs.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    Zone,
    ZoneFill,
    ZoneFilledPolygon,
    ZoneOutlinePolygon,
)

from eedesignpanels.preview.session import (
    BOARD_FILLING_NOTE,
    PreviewSession,
)
from eedesignpanels.stages import board_copper, board_scores, refill
from test_preview import left_blade, stub_render, write_project
from test_preview_pours import wait_watching
from test_preview_journal import open_session
from test_vscore import scoreable_board

# Long enough that a held filler is released by the test and never by a timeout;
# short enough that a wedged one fails the run rather than hanging it.
WAIT = 30.0

POUR_UUID = "00000000-0000-0000-0000-0000000000c1"


def board_with_copper() -> Board:
    """The scoreable fixture, with one copper zone on it to be re-filled."""
    board = scoreable_board()
    board.zones.append(Zone(
        net=0, net_name="", layers=["F.Cu"], id=POUR_UUID, name="board_pour",
        hatch_style="edge", hatch_pitch=0.508, min_thickness=0.25,
        fill=ZoneFill(filled=True, thermal_gap=0.5, thermal_bridge_width=0.5,
                      island_removal_mode=refill.ISLAND_REMOVAL_NEVER),
        polygons=[ZoneOutlinePolygon(
            pts=[(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)]
        )],
    ))
    return board


def fake_taken(board: Board, points: int = 3) -> refill.FillTaken:
    """What a filler would hand back for this board's copper zones.

    It fakes the KiCad round trip and NOTHING else: the transplant that consumes
    it is the real one, gate included.
    """
    return refill.FillTaken(
        records={
            zone.id: [ZoneFilledPolygon(
                layer=(zone.layers or ["F.Cu"])[0],
                pts=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)][:points],
            )]
            for zone in board_copper.copper_zones(board)
        },
        seconds=0.25,
        cli_path=Path("stub-kicad-cli"),
    )


class HeldBoardFill:
    """A board round trip that says when it started and can be stopped inside."""

    def __init__(self, error: BaseException = None):
        self.error = error
        self.calls = 0
        self.work_dirs = []
        self.besides = []
        # How many score keepouts were on the board each time KiCad was asked.
        self.keepouts = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = None
        self.free_while_running = None
        # How many round trips were STOPPED by a newer decision.
        self.cancelled = 0

    def hold(self) -> None:
        self.release.clear()
        self.entered.clear()

    def let_go(self) -> None:
        self.release.set()

    def __call__(self, board, beside=None, cli_path=None, work_dir=None,
                 cancel=None):
        self.calls += 1
        self.work_dirs.append(work_dir)
        self.besides.append(beside)
        self.keepouts.append(len(board_scores.keepouts_on(board)))
        if self.lock is not None:
            taken = self.lock.acquire(blocking=False)
            self.free_while_running = taken
            if taken:
                self.lock.release()
        self.entered.set()
        wait_watching(self.release, cancel, self)
        if self.error is not None:
            raise self.error
        return fake_taken(board)


class BoardFillPhaseTestCase(unittest.TestCase):
    """A recorded session over the stub renderer and a filler it can hold."""

    error = None

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder, board_with_copper())
        self.recording = open_session(self.folder,
                                      config_path=self.config_path)
        self.fill = HeldBoardFill(error=self.error)
        self.session = PreviewSession(
            self.config_path, self.recording.folder, render=stub_render,
            journal=self.recording, board_fill=self.fill,
        )
        self.fill.lock = self.session._lock
        self.session.refresh()

    def tearDown(self):
        self.fill.let_go()
        # The worker writes layer documents into the session folder, so it is
        # let finish before the folder goes away underneath it.
        self.session.wait_for_upgrades(WAIT)
        self.session.close()
        self._folder.cleanup()

    def blade(self):
        board = Board.from_file(self.session.config.source_board_path)
        line = left_blade(board)
        return {"start": list(line.start), "end": list(line.end)}

    def records_of(self, kind: str = "") -> list:
        return [r for r in self.recording.records()
                if not kind or r["kind"] == kind]

    def settle(self, timeout: float = WAIT) -> bool:
        return self.session.wait_for_upgrades(timeout)


class BoardFillRunsTest(BoardFillPhaseTestCase):
    """A decision costs a re-fill, and the re-fill lands on the held board."""

    def test_a_board_with_no_decision_owes_no_fill(self):
        self.assertFalse(self.session._board_fill_owed())
        self.assertEqual(self.fill.calls, 0)

    def test_an_accept_asks_kicad_for_the_copper(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 1)

    def test_the_fill_sees_the_keepout_the_decision_wrote(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.keepouts, [1])

    def test_the_answer_lands_on_the_board_the_session_holds(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        zone = board_copper.copper_zones(self.session.board)[0]
        self.assertEqual(len(zone.filled_polygons), 1)

    def test_the_design_file_is_not_touched_by_a_fill(self):
        before = self.session.config.source_board_path.read_bytes()
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(
            self.session.config.source_board_path.read_bytes(), before
        )

    def test_the_sandbox_is_this_session_s_own_folder(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.work_dirs[0],
                         self.session.workdir / "board_fill")

    def test_the_real_board_s_path_goes_with_it(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.besides[0],
                         self.session.config.source_board_path)

    def test_a_decline_asks_for_the_copper_too(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.session.decline(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 2)
        self.assertEqual(self.fill.keepouts, [1, 0])

    def test_a_decision_that_changed_nothing_costs_no_process(self):
        self.session.decline(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 0)

    def test_the_generation_moves_when_the_copper_lands(self):
        self.session.accept(self.blade())
        after_accept = self.session.generation
        self.assertTrue(self.settle())
        self.assertGreater(self.session.generation, after_accept)

    def test_it_is_journaled_with_what_it_did(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        record = self.records_of("board.fill")[0]["data"]
        self.assertTrue(record["applied"])
        self.assertEqual(record["zones"], 1)
        self.assertEqual(record["records"], 1)

    def test_the_panel_is_derived_from_the_copper_that_landed(self):
        # The board changed under the panel, so what was derived from it is
        # dropped - both the panel cache and the snapshot.
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertIsNone(self.session._panel_cache)
        self.assertIsNone(self.session._snapshot)


class BoardFillOrderingTest(BoardFillPhaseTestCase):
    """What must be true while KiCad is running, and what must be dropped."""

    def test_the_round_trip_holds_no_session_lock(self):
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.fill.let_go()
        self.assertTrue(self.settle())
        self.assertTrue(self.fill.free_while_running)

    def test_the_corner_says_the_copper_is_being_refilled(self):
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        state = self.session.content_state(self.session.view)
        self.assertEqual(state["content_note"], BOARD_FILLING_NOTE)
        self.assertTrue(state["awaiting_content"])
        self.fill.let_go()
        self.assertTrue(self.settle())

    def test_the_corner_goes_quiet_once_the_copper_lands(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(
            self.session.content_state(self.session.view)["content_note"], ""
        )

    def test_a_newer_decision_stops_the_answer_in_flight(self):
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        # A second decision while KiCad is still working: the answer coming
        # back is for a board that no longer exists, so the round trip is ENDED
        # rather than waited out.
        self.session.decline(self.blade())
        self.assertEqual(self.fill.cancelled, 1)
        self.fill.let_go()
        self.assertTrue(self.settle())
        dropped = self.records_of("board.fill.dropped")
        self.assertTrue(dropped)
        self.assertEqual(dropped[0]["data"]["reason"],
                         "cancelled mid round trip")
        self.assertEqual(len(self.records_of("board.fill")), 1)
        self.assertEqual(self.fill.keepouts, [1, 0])
        # A stop is not a failure, so the board still got its copper.
        self.assertFalse(self.records_of("board.fill.failed"))
        self.assertEqual(self.session.board_fill_error, "")

    def test_the_decision_does_not_wait_for_the_round_trip_it_stopped(self):
        # The window does not block while a zone refill round trip runs. The
        # filler is held open and never released, so a decision that waited
        # for it would take the whole WAIT.
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        started = time.monotonic()
        self.session.decline(self.blade())
        self.assertLess(time.monotonic() - started, WAIT / 3.0)
        self.fill.let_go()
        self.assertTrue(self.settle())

    def test_three_decisions_back_to_back_leave_only_the_last_one_filled(self):
        # Latest wins, on the board side. Accept, decline, accept - each
        # landing while the last round trip is still inside KiCad.
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        for act in (self.session.decline, self.session.accept):
            self.fill.entered.clear()
            act(self.blade())
            self.assertTrue(self.fill.entered.wait(WAIT))
        self.assertEqual(self.fill.cancelled, 2)
        self.fill.let_go()
        self.assertTrue(self.settle())
        self.assertEqual(len(self.records_of("board.fill")), 1)
        self.assertEqual(len(self.records_of("board.fill.dropped")), 2)
        self.assertFalse(self.records_of("board.fill.failed"))
        # The board on screen carries the last decision, filled against it.
        self.assertEqual(len(board_scores.keepouts_on(self.session.board)), 1)
        self.assertEqual(self.fill.keepouts[-1], 1)

    def test_the_keepout_width_stops_a_fill_the_same_way(self):
        # The width is a BOARD setting, on the prep page: moving it
        # redraws every keep-out, so the copper is owed a fill and any fill
        # already running is for the old strips.
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.fill.entered.clear()
        self.session.set_param({"score_rule_half_width_mm": 1.5})
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.assertEqual(self.fill.cancelled, 1)
        self.fill.let_go()
        self.assertTrue(self.settle())
        self.assertEqual(len(self.records_of("board.fill")), 1)
        self.assertFalse(self.records_of("board.fill.failed"))


class BoardFillSaveTest(BoardFillPhaseTestCase):
    """A save never writes copper from before the decision."""

    def test_a_save_waits_for_a_fill_in_flight(self):
        self.fill.hold()
        self.session.accept(self.blade())
        self.assertTrue(self.fill.entered.wait(WAIT))

        done = threading.Event()
        answer = {}

        def saving():
            answer["said"] = self.session.save()
            done.set()

        worker = threading.Thread(target=saving, daemon=True)
        worker.start()
        # The save is waiting, not writing: the board still has no keepout in it.
        self.assertFalse(done.wait(0.3))
        self.fill.let_go()
        self.assertTrue(done.wait(WAIT))
        worker.join(WAIT)

        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(len(board_scores.keepouts_on(saved)), 1)
        self.assertEqual(
            len(board_copper.copper_zones(saved)[0].filled_polygons), 1
        )
        self.assertTrue(self.records_of("board.fill.waited"))

    def test_a_save_with_nothing_in_flight_does_the_round_trip_itself(self):
        # The backstop: the trailing phase is cancelled and never redone, and
        # the save is the last thing standing between a stale fill and the file.
        self.session.accept(self.blade())
        self.session._cancel_upgrades("the test says so")
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 0)

        self.session.save()
        self.assertEqual(self.fill.calls, 1)
        self.assertEqual(self.fill.work_dirs[0],
                         self.session.workdir / "board_fill_save")
        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(
            len(board_copper.copper_zones(saved)[0].filled_polygons), 1
        )

    def test_a_save_with_the_copper_already_in_costs_no_process(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.session.save()
        self.assertEqual(self.fill.calls, 1)

    def test_the_save_writes_the_line_and_the_keepout(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        said = self.session.save()
        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(len(board_scores.score_lines_on(saved)), 1)
        self.assertEqual(len(board_scores.keepouts_on(saved)), 1)
        self.assertIn("score keepout", said["message"])


class BoardFillFailureTest(BoardFillPhaseTestCase):
    """A failure is said once, and the board stays valid and clearly unfilled."""

    error = refill.RefillError("kicad-cli is not there")

    def test_the_reason_reaches_the_corner_of_the_picture(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        state = self.session.content_state(self.session.view)
        self.assertIn("kicad-cli is not there", state["content_note"])
        self.assertIn("still pours across the score", state["content_note"])

    def test_it_is_not_asked_again(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 1)
        self.assertFalse(self.session._board_fill_owed())

    def test_a_new_decision_is_a_new_question(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.session.decline(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(self.fill.calls, 2)

    def test_the_board_still_saves_and_still_carries_the_decision(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.session.save()
        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(len(board_scores.keepouts_on(saved)), 1)

    def test_it_is_journaled_once(self):
        self.session.accept(self.blade())
        self.assertTrue(self.settle())
        self.assertEqual(len(self.records_of("board.fill.failed")), 1)


class NoKiCadTest(unittest.TestCase):
    """A session with no filler asks for nothing and says nothing."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        folder = Path(self._folder.name)
        self.config_path = write_project(folder, board_with_copper())
        self.work = folder / "work"
        self.work.mkdir()
        self.session = PreviewSession(self.config_path, self.work,
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_an_injected_renderer_gets_no_kicad_process_it_did_not_ask_for(self):
        self.assertFalse(self.session.board_fills_live)

    def blade(self) -> dict:
        board = Board.from_file(self.session.config.source_board_path)
        line = left_blade(board)
        return {"start": list(line.start), "end": list(line.end)}

    def test_a_decision_still_writes_both_and_saves_both(self):
        self.session.accept(self.blade())
        self.assertFalse(self.session._board_fill_owed())
        self.session.save()
        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(len(board_scores.score_lines_on(saved)), 1)
        self.assertEqual(len(board_scores.keepouts_on(saved)), 1)

    def test_accepting_and_declining_leaves_the_design_it_started_from(self):
        # THE TOGGLE ROUND TRIP, to the byte. The baseline is a save with NO
        # decision on it, because prep owns the toolpath layer and writes it
        # whether anything was decided or not - so this asks about the score
        # decision and not about the toolpath. The copper is not
        # re-filled in this session, so this is the file half alone; the half
        # with KiCad's own filler in it is fenced in `test_board_refill.py`.
        self.session.save()
        before = self.session.config.source_board_path.read_bytes()
        self.session.accept(self.blade())
        self.session.decline(self.blade())
        self.session.save()
        self.assertEqual(
            self.session.config.source_board_path.read_bytes(), before
        )

    def test_accepting_twice_writes_one_line_and_one_keepout(self):
        self.session.accept(self.blade())
        self.session.accept(self.blade())
        self.session.save()
        saved = Board.from_file(self.session.config.source_board_path)
        self.assertEqual(len(board_scores.score_lines_on(saved)), 1)
        self.assertEqual(len(board_scores.keepouts_on(saved)), 1)


if __name__ == "__main__":
    unittest.main()
