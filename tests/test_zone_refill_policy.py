"""THE ZONE REFILL POLICY: every setting that moves copper asks KiCad again.

A change to any setting that affects the zones starts a refill. The refill
runs without blocking the GUI. A newer change stops a refill that is still
running and starts its own, so only the latest settings are filled.

Three obligations, and this file is the fence on all three:

* **it triggers.** A setting that moves a zone, a keep-out or the material the
  copper pours into ends in a fill. The list is enumerated here by name rather
  than reasoned about, because the failure mode is a setting nobody thought of;
* **it never blocks.** The round trip holds no session lock, and an edit that
  arrives during one does not wait for it - proven by holding a filler open and
  never releasing it;
* **latest wins.** A newer change STOPS the running round trip inside itself
  rather than queueing behind it. `refill.take_fills` ends the KiCad process and
  raises `RefillCancelled`, which is deliberately not a `RefillError`: a
  cancelled fill is not a failure and the zones still owe one.

The stopping machinery is exercised against a REAL process here - a Python
interpreter told to sleep - because the one thing a stub cannot prove is that a
process actually dies.

The per-subject orderings live beside the phases they belong to:
`test_preview_pours` for the panel's pour and `test_preview_board_copper` for
the board's own copper.
"""

import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace

from eedesignpanels.stages import refill
from test_preview_pours import WAIT, PourPhaseTestCase

# How long a sleeper is told to sleep: far longer than any test waits, so a
# process that is still running at the end of a test is a failure and never a
# race.
SLEEP_SECONDS = 120


def a_sleeping_process():
    """A real child process that will not end on its own."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({SLEEP_SECONDS})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class StopTheProcessTest(unittest.TestCase):
    """A superseded KiCad is ENDED, and the cost of ending it is measured."""

    def test_a_running_process_is_ended_and_the_stop_is_timed(self):
        process = a_sleeping_process()
        self.addCleanup(process.kill)
        seconds, killed = refill._stop(process)
        self.assertIsNotNone(process.poll())
        self.assertFalse(killed, "a sleeping python did not end on terminate")
        self.assertLess(seconds, refill.TERMINATE_GRACE_SECONDS)

    def test_the_wait_ends_the_process_when_the_token_is_set(self):
        process = a_sleeping_process()
        self.addCleanup(process.kill)
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        with self.assertRaises(refill.RefillCancelled) as caught:
            refill._wait_for(process, cancel)
        self.assertIsNotNone(process.poll())
        self.assertLess(time.monotonic() - started,
                        refill.TERMINATE_GRACE_SECONDS + 1.0)
        self.assertGreaterEqual(caught.exception.seconds, 0.0)
        self.assertIn("newer change", str(caught.exception))

    def test_a_token_set_while_it_runs_is_noticed_within_a_poll(self):
        process = a_sleeping_process()
        self.addCleanup(process.kill)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(refill.RefillCancelled):
            refill._wait_for(process, cancel)
        taken = time.monotonic() - started
        self.assertIsNotNone(process.poll())
        # The notice is one poll and the stop is one terminate. A second is an
        # enormous allowance for both, and it is still nothing beside the ten
        # seconds the round trip would otherwise have run for.
        self.assertLess(taken, 2.0)

    def test_a_caller_with_no_token_simply_waits(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        refill._wait_for(process, None)
        self.assertEqual(process.returncode, 0)

    def test_a_cancellation_is_not_a_failure(self):
        # THE DISCIPLINE, in one line: a caller that catches `RefillError` must
        # not catch this, because a failure is said once and never retried while
        # a stop leaves the zones still owing a fill.
        self.assertFalse(issubclass(refill.RefillCancelled, refill.RefillError))

    def test_a_token_already_set_costs_no_process_at_all(self):
        cancel = threading.Event()
        cancel.set()
        zone = SimpleNamespace(
            id="a-zone",
            fill=SimpleNamespace(
                island_removal_mode=refill.ISLAND_REMOVAL_NEVER
            ),
        )
        board = SimpleNamespace(
            to_file=lambda path: self.fail("the board was serialised anyway")
        )
        with self.assertRaises(refill.RefillCancelled):
            refill.take_fills(board, [zone], cancel=cancel)


class ZoneAffectingSettingsTest(PourPhaseTestCase):
    """EVERY setting that moves copper ends in a round trip. Named, not reasoned.

    The list names every setting by hand. A number that moves a zone's shape, the
    material it pours into, or anything it has to hold off is here; a number
    that measures the panel without moving it is in the test below.
    """

    def settled_panel(self) -> None:
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertTrue(self.session.panel.fill_rails)

    def asks_again(self, name, value) -> None:
        before = self.fill.calls
        self.session.set_param({name: value})
        self.settled_panel()
        self.assertEqual(self.fill.calls, before + 1,
                         f"{name} did not ask KiCad for the copper again")

    def test_the_copper_settings_each_ask_for_a_fill(self):
        self.open_the_panel()
        self.settled_panel()
        for name, value in (("rail_copper_clearance_mm", 0.8),
                            ("rail_copper_between", True)):
            with self.subTest(name=name):
                self.asks_again(name, value)

    def test_the_layout_settings_each_ask_for_a_fill(self):
        # They move the boards, so they move the rail material the pour fills.
        self.open_the_panel()
        self.settled_panel()
        for name, value in (("gap_x_mm", 4.0), ("gap_y_mm", 4.5),
                            ("columns", 3), ("rotation_deg", 90.0),
                            ("nest_across_mm", -0.5)):
            with self.subTest(name=name):
                self.asks_again(name, value)

    def test_the_carrier_settings_each_ask_for_a_fill(self):
        # The rails ARE the pour, and a tooling hole is a hole in it.
        self.open_the_panel()
        self.settled_panel()
        for name, value in (("rail_mm", 6.0), ("hole_diameter_mm", 2.5),
                            ("hole_inset_across_mm", 6.0)):
            with self.subTest(name=name):
                self.asks_again(name, value)

    def test_the_score_settings_each_ask_for_a_fill(self):
        # A panel score writes a keep-out, and a keep-out is a hole in the pour.
        # The jump distances do NOT change the keep-outs (measured) - they
        # still rebuild the panel, so the pour they
        # produce is a new one and it is filled like any other.
        self.open_the_panel()
        self.settled_panel()
        self.asks_again("scores_on", True)
        for name, value in (("score_inter_row", True),
                            ("score_rails_horizontal", True),
                            ("score_rails_vertical", True),
                            ("score_jump_horizontal_mm", 2.0),
                            ("score_jump_vertical_mm", 1.5)):
            with self.subTest(name=name):
                self.asks_again(name, value)

    def test_the_pour_switch_asks_when_it_comes_back_on(self):
        self.open_the_panel()
        self.settled_panel()
        before = self.fill.calls
        # OFF: there is no pour, so there is nothing to fill and no process.
        self.session.set_param({"rail_copper_on": False})
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.fill.calls, before)
        # ON: a new pour, empty, and it is filled.
        self.asks_again("rail_copper_on", True)

    def test_a_measurement_over_the_panel_asks_for_nothing(self):
        # The clearance warning moves no coordinate, so it
        # neither rebuilds the panel nor re-fills it. The other side of the
        # list: a setting that DOES nothing must not cost ten seconds of KiCad.
        self.open_the_panel()
        self.settled_panel()
        before = self.fill.calls
        self.session.set_param({"clearance_warning_mm": 4.0})
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.fill.calls, before)
        self.assertTrue(self.session.panel.fill_rails)


class LatestWinsTest(PourPhaseTestCase):
    """Three zone-affecting edits back to back: only the last one's fill lands."""

    def test_each_edit_stops_the_one_before_it(self):
        self.fill.hold()
        self.session.refresh()
        self.session.start_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        typed = []
        for name, value in (("rail_copper_clearance_mm", 0.8),
                            ("gap_x_mm", 4.0),
                            ("rail_mm", 6.0)):
            self.fill.entered.clear()
            started = time.monotonic()
            self.session.set_param({name: value})
            typed.append(time.monotonic() - started)
            self.assertTrue(self.fill.entered.wait(WAIT))
        # NOT SERIAL: each edit returned in about a poll interval, not in the
        # ten seconds a round trip takes.
        for name, taken in zip(("clearance", "gap", "rail"), typed):
            with self.subTest(edit=name):
                self.assertLess(taken, WAIT / 3.0)
        self.assertEqual(self.fill.cancelled, 3)
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(len(self.records_of("panel.fill")), 1)
        self.assertEqual(len(self.records_of("panel.fill.dropped")), 3)
        self.assertFalse(self.records_of("panel.fill.failed"))
        self.assertTrue(self.session.panel.fill_rails)

    def test_the_journal_says_what_each_stop_cost(self):
        self.fill.hold()
        self.session.refresh()
        self.session.start_panel()
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.fill.entered.clear()
        self.session.set_param({"gap_x_mm": 4.0})
        self.assertTrue(self.fill.entered.wait(WAIT))
        self.fill.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        stopped = self.records_of("panel.fill.dropped")[0]["data"]
        self.assertEqual(stopped["reason"], "cancelled mid round trip")
        self.assertIn("stop_seconds", stopped)
        self.assertIn("killed", stopped)


if __name__ == "__main__":
    unittest.main()
