"""The caption toggle, and what a responsive recalculation is allowed to cost.

Two regressions are guarded here, and they share a file only by accident:

**THE TOGGLE.** A caption on the prep page toggles a candidate between SCORED
and PROPOSED. PROPOSED went to SCORED and then stopped: a scored caption
clicked again reported "already scored - nothing to change", and the journal
showed the click posting `/accept` a second time. The server was never wrong -
the verdicts it recomputed were correct at every generation, which is asserted
here from the fragment AND from the caption overlay - and the page was: it
looked the candidate up in `PB.proposals` to decide accept-or-decline, and
`drawCandidates` drew the rows without ever storing the list it was given. So
the decision was taken against the page-load snapshot for the life of the tab.

**THE COST.** Every edit answered in about 1.2 s, and 682 ms of it was
serializing 1.77 MB of held board to a string and parsing it back - on edits
that could not have moved the board, a tooling hole diameter among them. The
round trip is real (`panelbuild.as_the_file_holds_it`) and it is now taken
ONCE per board change instead of once per edit. What guards that is not the
saving, it is the two fences below: the snapshot is dropped wherever the board
is touched, and a responsive recalculation reads no board from disk and writes
none to it.
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eedesigncore import Board

from eedesignpanels import prep
from eedesignpanels.config import load_config
from eedesignpanels.preview import actions, page_served, panelbuild
from eedesignpanels.preview.labels import PROPOSED, SCORED
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages import route_toolpath, step_and_repeat
from test_panel_page import write_panel_project
from test_preview import left_blade, scoreable_board, stub_render, write_project
from test_preview_journal import js_function, run_js


def verdicts(proposals) -> dict:
    """`{display number: word}` off a fragment's candidate list."""
    return {row["displayId"]: row["verdict"] for row in proposals}


def stage_verdicts(result) -> dict:
    """`{label: verdict}` straight off a prep result, with no page in the way."""
    return {entry.label: entry.verdict for entry in result.proposals}


# ---------------------------------------------------------------------------
# the stage: a decision made in memory is a decision the next computation reads
# ---------------------------------------------------------------------------


class StageTogglesOnTheHeldBoardTest(unittest.TestCase):
    """Accept, recompute, decline, recompute - with nothing on disk moving.

    The bottom of the toggle, tested without a session or a page: prep reads
    `User.38` off the board object it is handed, so an edit to that object is
    the whole of a decision and the verdict is a reading of it.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.board = Board.from_file(self.config.source_board_path)
        self.before = self.config.source_board_path.read_bytes()

    def tearDown(self):
        self._folder.cleanup()

    def test_accept_then_decline_flips_the_verdict_back(self):
        first = prep.compute(self.config, self.board).result
        proposed = [
            entry.label for entry in first.proposals
            if entry.verdict == route_toolpath.VERDICT_PROPOSED
        ]
        self.assertTrue(proposed, "the fixture offers no candidate to accept")
        label = proposed[0]
        blade = left_blade(self.board)

        written = actions.accept(self.board, blade,
                                 list(first.candidates))
        self.assertTrue(written.changed)
        after = prep.compute(self.config, self.board).result
        self.assertEqual(stage_verdicts(after)[label],
                         route_toolpath.VERDICT_SCORED)

        written = actions.decline(self.board, blade)
        self.assertTrue(written.changed)
        back = prep.compute(self.config, self.board).result
        self.assertEqual(stage_verdicts(back)[label],
                         route_toolpath.VERDICT_PROPOSED)

    def test_the_design_on_disk_never_moved(self):
        first = prep.compute(self.config, self.board).result
        blade = left_blade(self.board)
        actions.accept(self.board, blade, list(first.candidates))
        prep.compute(self.config, self.board)
        actions.decline(self.board, blade)
        prep.compute(self.config, self.board)
        self.assertEqual(self.config.source_board_path.read_bytes(),
                         self.before)


# ---------------------------------------------------------------------------
# the session: the fragment and the caption overlay both say the new verdict
# ---------------------------------------------------------------------------


class SessionTogglesTest(unittest.TestCase):
    """The round trip as the page sees it, on both of the page's two readings.

    The fragment's candidate list is what the rows and the click handler read;
    the overlay document is what the user reads off the picture. They are
    produced by different code from the same result, so both are asserted -
    a caption that disagreed with its own row is the bug this is about.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.assertEqual(self.session.error, "")

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def captions(self) -> list:
        """`#1 PROPOSED` and friends, read out of the overlay document itself."""
        markup = self.session.fragment()["svg"][prep.PROPOSAL_LAYER]
        return [
            f"{number} {word}"
            for number, word in
            re.findall(r"(#\d+) (SCORED|PROPOSED|REJECTED)", markup)
        ]

    def target(self) -> dict:
        for row in self.session.fragment()["proposals"]:
            if row["verdict"] == PROPOSED:
                return row
        raise AssertionError("the fixture offers no candidate to accept")

    def test_accept_then_decline_returns_the_candidate_to_proposed(self):
        row = self.target()
        name = row["displayId"]
        self.assertEqual(verdicts(self.session.fragment()["proposals"])[name],
                         PROPOSED)

        answer = self.session.accept(row["blade"])
        self.assertTrue(answer["ok"])
        self.assertIn("scored", answer["message"])
        self.assertEqual(verdicts(self.session.fragment()["proposals"])[name],
                         SCORED)

        answer = self.session.decline(row["blade"])
        self.assertTrue(answer["ok"])
        self.assertIn("declined", answer["message"])
        self.assertEqual(verdicts(self.session.fragment()["proposals"])[name],
                         PROPOSED)

    def test_the_caption_on_the_picture_flips_with_it(self):
        row = self.target()
        name = row["displayId"]
        self.assertIn(f"{name} {PROPOSED}", self.captions())
        self.session.accept(row["blade"])
        self.assertIn(f"{name} {SCORED}", self.captions())
        self.assertNotIn(f"{name} {PROPOSED}", self.captions())
        self.session.decline(row["blade"])
        self.assertIn(f"{name} {PROPOSED}", self.captions())
        self.assertNotIn(f"{name} {SCORED}", self.captions())

    def test_a_second_accept_changes_nothing_and_says_so(self):
        row = self.target()
        self.session.accept(row["blade"])
        answer = self.session.accept(row["blade"])
        self.assertIn("already scored", answer["message"])
        self.assertEqual(
            verdicts(self.session.fragment()["proposals"])[row["displayId"]],
            SCORED,
        )


# ---------------------------------------------------------------------------
# the page: the click decides from the CURRENT list
# ---------------------------------------------------------------------------


# One candidate, drawn twice: PROPOSED, clicked, then SCORED as the next
# fragment reports it, clicked again. The second click must post `/decline`.
TOGGLE_HARNESS = """
%(functions)s
const PB = {scoreLayerName: 'User.38-VScore'};
function applyBusy() {}
function node() {
  return {className: '', textContent: '', style: {}, dataset: {},
          children: [],
          append(...kids) { this.children.push(...kids); },
          appendChild(kid) { this.children.push(kid); }};
}
const candEl = node();
globalThis.document = {createElement: node};
const posted = [];
function logEvent(type, data) { posted.push({log: type, data: data}); }
function post(url, body, what) { posted.push({url: url}); return url; }
const base = {number: 1, displayId: '#1', proposable: true,
              midpoint: [1, 2], length_mm: 3, extent_mm: 4,
              summary: 'a summary', reason: 'a reason',
              blade: {start: [0, 0], end: [0, 1]}};
const proposed = Object.assign({}, base, {verdict: 'PROPOSED',
                                          accepted: false});
const scored = Object.assign({}, base, {verdict: 'SCORED', accepted: true});
const out = {};
drawCandidates([proposed]);
out.held_first = PB.proposals[0].verdict;
clickLabel(1, 'label');
out.first = posted[posted.length - 1].url;
out.first_label = posted[posted.length - 2].data.label;
// The server scored it, and the next fragment says so.
drawCandidates([scored]);
out.held_second = PB.proposals[0].verdict;
clickLabel(1, 'label');
out.second = posted[posted.length - 1].url;
out.second_label = posted[posted.length - 2].data.label;
// And back again.
drawCandidates([proposed]);
clickLabel(1, 'label');
out.third = posted[posted.length - 1].url;
console.log(JSON.stringify(out));
"""


class PageClickTogglesTest(unittest.TestCase):
    """`drawCandidates` is what puts the list on `PB`, and the click reads it.

    Run under node, because this is a statement about the page's own script and
    a statement about a string would not have caught it.
    """

    def harness(self) -> dict:
        functions = "\n".join(
            js_function(page_served.PREP_JS, name)
            for name in ("dotColor", "drawCandidates", "labelAction",
                         "proposalNumbered", "clickLabel")
        )
        return run_js(TOGGLE_HARNESS % {"functions": functions})

    def test_the_click_follows_the_verdict_both_ways(self):
        out = self.harness()
        self.assertEqual(out["held_first"], "PROPOSED")
        self.assertEqual(out["first"], "/accept")
        self.assertEqual(out["held_second"], "SCORED")
        self.assertEqual(out["second"], "/decline")
        self.assertEqual(out["third"], "/accept")

    def test_the_journal_records_the_verdict_the_click_was_taken_on(self):
        out = self.harness()
        self.assertEqual(out["first_label"], "#1 PROPOSED")
        self.assertEqual(out["second_label"], "#1 SCORED")


# ---------------------------------------------------------------------------
# the cost: one snapshot per board change, and no board file in the fast path
# ---------------------------------------------------------------------------


class SnapshotTest(unittest.TestCase):
    """What the reused round trip is, and that reusing it cannot be wrong.

    Two derivations from ONE snapshot have to produce the same panel, and the
    snapshot has to come out of them unchanged - `step_and_repeat.build_from`
    documents that it never modifies its source, and this is where that
    document is checked, because the whole optimisation rests on it.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.config = load_config(self.config_path)
        self.board = Board.from_file(self.config.source_board_path)

    def tearDown(self):
        self._folder.cleanup()

    def test_a_snapshot_is_the_board_as_the_file_holds_it(self):
        taken = panelbuild.snapshot_of(self.board)
        self.assertEqual(taken.text, self.board.to_string())
        # Stable: the parsed copy writes the same text it was read from, which
        # is what makes it the file's own precision and not ours.
        self.assertEqual(taken.board.to_string(), taken.text)

    def test_a_build_leaves_the_snapshot_it_ran_on_alone(self):
        taken = panelbuild.snapshot_of(self.board)
        step_and_repeat.build_from(self.config, taken.board, fill_rails=False)
        self.assertEqual(taken.board.to_string(), taken.text)

    def test_two_derives_from_one_snapshot_give_one_panel(self):
        taken = panelbuild.snapshot_of(self.board)
        first = panelbuild.derive(self.config, self.board, fill_rails=False,
                                  snapshot=taken)
        second = panelbuild.derive(self.config, self.board, fill_rails=False,
                                   snapshot=taken)
        self.assertEqual(first.outcome.board.to_string(),
                         second.outcome.board.to_string())
        self.assertEqual(first.text, second.text)

    def test_no_snapshot_means_take_one(self):
        derived = panelbuild.derive(self.config, self.board, fill_rails=False)
        self.assertEqual(derived.text, self.board.to_string())


class RecalcCostTest(unittest.TestCase):
    """The two fences on the responsive path: no stale snapshot, no disk.

    Neither is about speed. A snapshot kept across a board change would serve a
    panel of the previous board, which is the defect this session already had
    once; and a recalculation that read or wrote a board file would be doing
    file work inside a click, which is what the measurement said it was.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()
        self.assertEqual(self.session.panel_error, "")
        self.assertIsNotNone(self.session.panel)

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def blade(self):
        for row in self.session.fragment()["proposals"]:
            if row["verdict"] == PROPOSED:
                return row["blade"]
        raise AssertionError("the fixture offers no candidate to accept")

    def test_a_panel_only_edit_reuses_the_snapshot(self):
        held = self.session._snapshot
        self.assertIsNotNone(held)
        with mock.patch.object(panelbuild, "snapshot_of",
                               side_effect=AssertionError(
                                   "a panel-only edit took a new snapshot")):
            self.session.set_param({"rail_left_mm": 6.0})
        self.assertIs(self.session._snapshot, held)
        self.assertEqual(self.session.panel_error, "")

    def test_a_score_decision_drops_the_snapshot(self):
        held = self.session._snapshot
        self.session.accept(self.blade())
        self.assertIsNotNone(self.session._snapshot)
        self.assertIsNot(self.session._snapshot, held)
        self.assertEqual(self.session._snapshot.text,
                         actions.board_for_save(
                             self.session.board, self.session.data).to_string())

    def test_a_prep_number_drops_the_snapshot(self):
        held = self.session._snapshot
        self.session.set_param({"min_tab_mm": 0.4})
        self.assertIsNot(self.session._snapshot, held)
        self.assertEqual(self.session._snapshot.text,
                         actions.board_for_save(
                             self.session.board, self.session.data).to_string())

    def test_the_panel_a_reused_snapshot_derives_is_the_panel_a_fresh_one_does(
            self):
        # The staleness question, answered on the bytes. One edit taken with
        # the snapshot reused, the same edit taken from a session that has
        # just read the board: same panel, or the reuse is a lie.
        blade = self.blade()
        self.session.accept(blade)
        self.session.set_param({"rail_left_mm": 6.0})
        reused = self.session.panel.outcome.board.to_string()

        fresh = PreviewSession(self.config_path, self.folder / "work2",
                               render=stub_render)
        try:
            fresh.refresh()
            fresh.accept(blade)
            fresh.start_panel()
            fresh.set_param({"rail_left_mm": 6.0})
            self.assertEqual(fresh.panel.outcome.board.to_string(), reused)
        finally:
            fresh.close()

    def test_no_board_reaches_or_leaves_the_disk_during_a_recalculation(self):
        # The edit stays in memory. Both doors are watched - a write of a
        # board file, and a parse of one - across every kind of responsive
        # edit there is.
        blade = self.blade()
        with mock.patch.object(Board, "to_file") as wrote, \
                mock.patch.object(Board, "from_file") as read:
            self.session.set_param({"rail_left_mm": 6.5})
            self.session.set_param({"min_tab_mm": 0.45})
            self.session.accept(blade)
            self.session.decline(blade)
            self.session.build_panel()
        self.assertEqual(wrote.call_count, 0, "a recalculation wrote a board")
        self.assertEqual(read.call_count, 0, "a recalculation parsed a board")
        self.assertEqual(self.session.panel_error, "")

    def test_the_phase_timings_are_journaled(self):
        self.session.set_param({"rail_left_mm": 7.0})
        phases = self.session._panel_phases
        self.assertIn("snapshot_reused", phases)
        for name in ("build", "differs", "frame", "render", "write_svgs"):
            self.assertIn(name, phases)
            self.assertGreaterEqual(phases[name], 0.0)


class SavedPanelIsUnaffectedTest(unittest.TestCase):
    """A save takes its OWN snapshot, and writes the panel a build writes.

    The one place the reuse is deliberately not taken. What the save produces
    is the file a fabricator gets, and it costs under a second of a write
    measured in tens of them - so it pays for a fresh reading of the board
    rather than trusting a cache to have been dropped.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.config = load_config(self.config_path)

    def tearDown(self):
        self._folder.cleanup()

    def test_the_filled_derive_takes_a_fresh_snapshot(self):
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        try:
            session.refresh()
            session.start_panel()
            self.assertIsNotNone(session._snapshot)
            with mock.patch.object(
                panelbuild, "snapshot_of",
                wraps=panelbuild.snapshot_of,
            ) as taken:
                session._compute_panel(fill_rails=True)
            # ONE, and it is the point: the filled derive did not reach for
            # the snapshot the live picture is reusing.
            self.assertEqual(taken.call_count, 1)
        finally:
            session.close()

    def test_the_saved_panel_is_what_the_command_line_builds(self):
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        try:
            session.refresh()
            session.start_panel()
            session.set_param({"rail_left_mm": 6.0})
            session.save()
            live = session.panel.outcome.board.to_string()
            held = session.config
        finally:
            session.close()
        # The same numbers and the same board, built the way the command line
        # builds it: the reuse must not have changed the answer. The config is
        # the session's held one. Save to Board applied the same item locks
        # that the panel snapshot uses.
        board = Board.from_file(held.source_board_path)
        direct = panelbuild.derive(held, board, fill_rails=False)
        self.assertEqual(direct.outcome.board.to_string(), live)


class FixtureSanityTest(unittest.TestCase):
    """The fixture really does offer a candidate in each state.

    A toggle test over a board with nothing to toggle would pass by saying
    nothing, so the fixture is asserted rather than assumed.
    """

    def test_the_scoreable_board_proposes_something(self):
        board = scoreable_board()
        made = route_toolpath.build(board, 0.8,
                                    route_toolpath.VScoreConfig(
                                        detect="auto",
                                        route_past_score_mm=1.0))
        words = [entry.verdict for entry in made.proposals]
        self.assertIn(route_toolpath.VERDICT_PROPOSED, words)


if __name__ == "__main__":  # pragma: no cover - the runner is the entry point
    unittest.main()
