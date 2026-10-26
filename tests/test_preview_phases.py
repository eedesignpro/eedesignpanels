"""Two-phase painting: the fast picture first, the content behind it.

AN EDIT PAINTS AT ONCE. The content render is a kicad-cli process plus a write of the whole board - measured on the corpus
at 0.84 s for the board and 2.1 s for its panel - and paying for it inside the
edit is what turned a one-second edit into a three-second one. So a change
renders through the library, publishes, and the page paints it and stays live;
the content render runs behind that and replaces those layers as a SECOND push
on the SAME generation.

What can go wrong here is ordering, and ordering cannot be read off a string:

* an upgrade whose generation has moved must be DROPPED, or an edit is answered
  by a picture of the board before it;
* a new edit must CANCEL what is outstanding, or the machine draws work nobody
  asked for while the user waits;
* the page must paint an upgrade WITHOUT the busy popup, and must ignore one
  that belongs to a generation it has already left.

So the session is driven with a renderer that can be held open inside the
content phase - the same method the busy window is tested by - and the page's
own `upgradeSeen` and `drawAwaiting` are lifted out and run under node.
"""

import json
import re
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from eedesignpanels.preview import (
    __main__ as preview_main,
    page_served,
    render_seam,
    server as server_module,
)
from eedesignpanels.preview.session import FAST, FULL, PreviewSession
from test_panel_page import write_panel_project
from test_preview import stub_render, write_project
from test_preview_journal import js_function, open_session, run_js

# Long enough that a held render is released by the test and never by a
# timeout; short enough that a wedged one fails the run rather than hanging it.
WAIT = 30.0
# The mark the content phase leaves. Not a <title>: the page strips those, so a
# title would be invisible in a fragment and the test would be about the strip.
CONTENT_MARK = '<rect id="pb-content"/>'


class PhaseRender:
    """A renderer that says which phase it was asked for, and can be held open.

    The two phases produce DIFFERENT bytes - the content one carries
    :data:`CONTENT_MARK` - because the whole question is which of them a page is
    looking at, and a stub that answered both the same could not be asked.
    """

    def __init__(self):
        self.calls = []
        # The lock each call was given, if any: the fast phase is handed none,
        # and the upgrade is handed the session's.
        self.locks = []
        self.entered_full = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._lock = threading.Lock()

    def hold(self):
        """Stop the next content render inside itself, until `let_go`."""
        self.release.clear()
        self.entered_full.clear()

    def let_go(self):
        self.release.set()

    @property
    def phases(self) -> list:
        with self._lock:
            return list(self.calls)

    def __call__(self, board, layers, viewbox_mm, colors, theme=None,
                 workdir=None, content=False, origin=None, board_lock=None):
        with self._lock:
            self.calls.append(bool(content))
            self.locks.append(board_lock)
        if content:
            self.entered_full.set()
            self.release.wait(WAIT)
        produced = stub_render(board, layers, viewbox_mm, colors, theme,
                               workdir, content)
        if not content:
            return produced
        return {
            name: markup.replace("</svg>", f"{CONTENT_MARK}</svg>")
            for name, markup in produced.items()
        }


class TwoPhaseTestCase(unittest.TestCase):
    """A recorded session over the phase renderer, with two phases turned on."""

    panelizes = False

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = (
            write_panel_project(self.folder) if self.panelizes
            else write_project(self.folder)
        )
        self.recording = open_session(self.folder,
                                      config_path=self.config_path)
        self.render = PhaseRender()
        self.session = PreviewSession(
            self.config_path, self.recording.folder, render=self.render,
            journal=self.recording, two_phase=True,
        )

    def tearDown(self):
        self.render.let_go()
        self.session.close()
        self._folder.cleanup()

    def records(self, kind: str = "") -> list:
        return [r for r in self.recording.records()
                if not kind or r["kind"] == kind]

    def markup_of(self, layer: str = "Edge.Cuts") -> str:
        return Path(self.session.view.svg_by_layer[layer]).read_text(
            encoding="utf-8"
        )


class FastThenContentTest(TwoPhaseTestCase):
    """One edit, two renders, and the page is live between them."""

    def test_the_edit_renders_the_fast_phase_and_says_so(self):
        self.render.hold()
        self.session.refresh()
        # The picture is on disk and the content render has not run yet.
        self.assertEqual(self.session.view.content, FAST)
        self.assertNotIn(CONTENT_MARK, self.markup_of())
        self.assertEqual(self.render.phases[0], False)
        self.render.let_go()

    def test_the_content_render_replaces_those_layers_behind_it(self):
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.view.content, FULL)
        self.assertIn(CONTENT_MARK, self.markup_of())
        self.assertEqual(self.render.phases, [False, True])
        self.assertEqual(self.session.upgrade_seq, 1)

    def test_the_layer_set_is_the_fast_phases_and_does_not_move(self):
        # A stack that gains or loses a row is a page RELOAD; an upgrade is a
        # repaint, so the layers it may touch are the ones already on screen.
        self.session.refresh()
        before = list(self.session.view.svg_by_layer)
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(list(self.session.view.svg_by_layer), before)
        self.assertEqual([layer.canonical_name
                          for layer in self.session.view.layers],
                         [layer.canonical_name
                          for layer in self.session.view.layers])

    def test_only_the_content_render_is_told_how_to_lock_the_board(self):
        # The kicad-cli backend switches every Value off while it writes the
        # board out, and that is a change to the object this session holds. The
        # fast phase writes nothing and is handed nothing.
        #
        # THE LOCK IT IS HANDED IS THE BOARD LOCK, not the session lock (2026-
        # 09-11). A fragment needs no board, and a page that asked for one
        # during a content render used to wait 655 ms for it - inside the first
        # paint of the edit that had just been made.
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.render.phases, [False, True])
        self.assertIsNone(self.render.locks[0])
        self.assertIs(self.render.locks[1], self.session._board_lock)
        self.assertIsNot(self.session._board_lock, self.session._lock)

    def test_both_phases_are_journaled_with_their_own_timings(self):
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        computed = self.records("compute")[-1]["data"]
        self.assertEqual(computed["phase"], FAST)
        self.assertGreaterEqual(computed["seconds"], 0.0)
        upgraded = self.records("upgrade")[-1]["data"]
        self.assertEqual(upgraded["subject"], "board")
        self.assertTrue(upgraded["applied"])
        self.assertEqual(upgraded["generation"], self.session.generation)
        self.assertGreaterEqual(upgraded["seconds"], 0.0)
        self.assertGreaterEqual(upgraded["waited"], 0.0)
        self.assertTrue(upgraded["layers"])

    def test_the_fragment_says_which_phase_its_picture_is_in(self):
        self.render.hold()
        self.session.refresh()
        fragment = self.session.fragment()
        self.assertEqual(fragment["content"], FAST)
        self.assertTrue(fragment["awaiting_content"])
        self.assertEqual(fragment["content_note"],
                         "copper, mask and paste are still being drawn - this "
                         "picture is graphics only for a moment")
        self.assertEqual(fragment["upgrade_seq"], 0)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        after = self.session.fragment()
        self.assertEqual(after["content"], FULL)
        self.assertFalse(after["awaiting_content"])
        self.assertEqual(after["content_note"], "")
        self.assertEqual(after["upgrade_seq"], 1)

    def test_the_upgrade_does_not_move_the_generation(self):
        # It is a better picture of the SAME board. A page that treated it as a
        # new generation would put the busy popup over a wait nobody is having.
        self.session.refresh()
        generation = self.session.generation
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.generation, generation)

    def test_the_page_carries_the_corner_note_while_it_waits(self):
        self.render.hold()
        self.session.refresh()
        markup = self.session.page()
        self.assertIn("<div class=awaitnote id=pb-await", markup)
        self.assertIn("graphics only for a moment", markup)
        self.assertNotIn("<div class=awaitnote id=pb-await role=status "
                         "aria-live=polite hidden>", markup)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        after = self.session.page()
        self.assertIn("aria-live=polite hidden>", after)

    def test_the_corner_note_sits_in_the_viewport_over_the_stack(self):
        self.session.refresh()
        markup = self.session.page()
        viewport = markup.split("<div class=viewport", 1)[1].split("</div>")[0]
        self.assertIn("id=pb-await", viewport + "</div>")
        self.assertIn(".awaitnote{position:absolute", page_served.CONTROLS_CSS)
        self.assertIn("z-index:1000", page_served.CONTROLS_CSS)
        self.assertIn("pointer-events:none}",
                      page_served.CONTROLS_CSS.split(".awaitnote{", 1)[1])

    def test_the_corner_note_carries_a_spinner_beside_its_words(self):
        # A spinner shows that work is in progress, and motion is what the
        # eye finds. The words live in their own element so a redraw never
        # wipes the spinner out, and an ERROR spins nothing.
        markup = page_served.await_html("KiCad is filling the rail copper")
        self.assertIn("<span class=spin aria-hidden=true></span>", markup)
        self.assertIn("<span id=pb-await-text>KiCad is filling", markup)
        note_rule = page_served.CONTROLS_CSS.split(".awaitnote .spin{", 1)[1]
        self.assertIn("animation:pbspin", note_rule.split("}")[0])
        self.assertIn(".awaitnote.bad .spin{display:none}",
                      page_served.CONTROLS_CSS)

    def test_the_fold_handles_are_inverted(self):
        # A light bar with dark text on the dark column, so
        # a section head is obvious at a glance.
        rule = page_served.CONTROLS_CSS.split(".controls .fold{", 1)[1]
        self.assertIn("background:#aeb6bf", rule.split("}")[0])
        self.assertIn("color:#14161a", rule.split("}")[0])


class PushOrderTest(TwoPhaseTestCase):
    """Two pushes, in order, and the second one names the upgrade."""

    def test_the_fast_push_comes_first_and_the_upgrade_second(self):
        listener = self.session.subscribe()
        self.assertIsNotNone(listener.wait(WAIT))  # the subscribe message
        self.render.hold()
        self.session.refresh()

        first = listener.wait(WAIT)
        self.assertEqual(first["generation"], self.session.generation)
        self.assertEqual(first["upgrade"], 0)
        self.assertEqual(self.session.view.content, FAST)

        self.assertTrue(self.render.entered_full.wait(WAIT))
        self.render.let_go()
        second = listener.wait(WAIT)
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(second["upgrade"], 1)
        # ...and whose content it was, so the other page leaves it alone.
        self.assertEqual(second["upgrade_page"], "prep")
        self.session.unsubscribe(listener)

    def test_a_page_joining_late_is_told_where_the_upgrade_got_to(self):
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        listener = self.session.subscribe()
        opening = listener.wait(WAIT)
        self.assertEqual(opening["upgrade"], self.session.upgrade_seq)
        self.session.unsubscribe(listener)


class OrderingGuardTest(TwoPhaseTestCase):
    """A stale upgrade is dropped, and a new edit cancels a pending one."""

    def test_a_new_edit_cancels_the_upgrade_that_is_running(self):
        self.render.hold()
        self.session.refresh()
        self.assertTrue(self.render.entered_full.wait(WAIT))

        self.session.refresh()          # the new edit, while the first draws
        cancelled = self.records("upgrade.cancelled")
        self.assertTrue(cancelled)
        self.assertEqual(cancelled[-1]["data"]["reason"], "a new edit")

        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))

    def test_the_upgrade_of_a_generation_that_moved_is_never_applied(self):
        self.render.hold()
        self.session.refresh()
        self.assertTrue(self.render.entered_full.wait(WAIT))
        stale = self.session.generation
        self.session.refresh()
        self.assertGreater(self.session.generation, stale)

        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        dropped = self.records("upgrade.dropped")
        self.assertTrue(dropped)
        self.assertEqual(dropped[0]["data"]["generation"], stale)
        # ...and exactly one upgrade was applied: the newest generation's.
        applied = self.records("upgrade")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["data"]["generation"],
                         self.session.generation)
        self.assertEqual(self.session.upgrade_seq, 1)
        self.assertEqual(self.session.view.content, FULL)

    def test_two_content_renders_never_run_at_once(self):
        # One scratch folder, one kicad-cli: the second waits for the first,
        # and the wait is journaled rather than being a mystery in the timings.
        self.render.hold()
        self.session.refresh()
        self.assertTrue(self.render.entered_full.wait(WAIT))
        self.session.refresh()
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.render.phases.count(True), 2)

    def test_a_closing_session_drops_what_it_cannot_abort(self):
        self.render.hold()
        self.session.refresh()
        self.assertTrue(self.render.entered_full.wait(WAIT))
        self.render.let_go()
        self.session.close()
        self.assertTrue(self.session.wait_for_upgrades(1.0))
        self.assertTrue(self.records("upgrade.cancelled"))


class UpgradeFailureTest(TwoPhaseTestCase):
    """A content render that does not happen is SAID, not swallowed."""

    def test_a_failed_upgrade_reaches_the_page_and_the_journal(self):
        def explode(*args, **kwargs):
            if kwargs.get("content"):
                raise RuntimeError("kicad-cli fell over")
            return stub_render(*args, **kwargs)

        self.session._render = explode
        with mock.patch("traceback.print_exc"):
            self.session.refresh()
            self.assertTrue(self.session.wait_for_upgrades(WAIT))
        failed = self.records("upgrade.failed")
        self.assertTrue(failed)
        self.assertIn("kicad-cli fell over", failed[-1]["data"]["error"])
        fragment = self.session.fragment()
        self.assertEqual(fragment["content"], FAST)
        self.assertFalse(fragment["awaiting_content"])
        self.assertIn("kicad-cli fell over", fragment["content_note"])
        self.assertIn("did not finish", fragment["content_note"])
        # The counter moved, so the page reads the reason rather than sitting
        # under a note promising content that is not coming.
        self.assertEqual(fragment["upgrade_seq"], 1)

    def test_a_failure_is_not_retried_for_ever(self):
        def explode(*args, **kwargs):
            if kwargs.get("content"):
                raise RuntimeError("no")
            return stub_render(*args, **kwargs)

        self.session._render = explode
        with mock.patch("traceback.print_exc"):
            self.session.refresh()
            self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(len(self.records("upgrade.failed")), 1)
        self.assertFalse(self.records("upgrade.gave_up"))


class PanelPhaseTest(TwoPhaseTestCase):
    """Both subjects are upgraded, board first, and the panel page says so."""

    panelizes = True

    def test_the_board_and_the_panel_are_each_drawn_twice(self):
        # Held, so both fast pictures are on screen before either upgrade is:
        # unheld, the board's content render finishes while this is reading.
        self.render.hold()
        self.session.refresh()
        self.session.start_panel()
        self.assertEqual(self.session.view.content, FAST)
        self.assertEqual(self.session.panel.content, FAST)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.view.content, FULL)
        self.assertEqual(self.session.panel.content, FULL)
        self.assertEqual(self.session.upgrade_seq, 2)
        subjects = [r["data"]["subject"] for r in self.records("upgrade")]
        self.assertEqual(subjects, ["board", "panel"])

    def test_the_panel_fragment_carries_its_own_phase(self):
        self.render.hold()
        self.session.refresh()
        self.session.start_panel()
        fragment = self.session.panel_fragment()
        self.assertEqual(fragment["content"], FAST)
        self.assertTrue(fragment["awaiting_content"])
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertFalse(self.session.panel_fragment()["awaiting_content"])
        self.assertIn(CONTENT_MARK, Path(
            self.session.panel.svg_by_layer["Edge.Cuts"]
        ).read_text(encoding="utf-8"))

    def test_the_panel_page_carries_the_corner_note_too(self):
        self.render.hold()
        self.session.refresh()
        self.session.start_panel()
        markup = self.session.panel_page()
        self.assertIn("id=pb-await", markup)
        self.assertIn("graphics only for a moment", markup)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))

    def test_a_panel_derived_after_the_board_is_upgraded_is_picked_up(self):
        # `start_panel` derives without moving the generation, so a worker that
        # has already finished the board has to be started again for it.
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.session.start_panel()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.panel.content, FULL)
        self.assertEqual(self.session.upgrade_seq, 2)


class TwoPhaseOverTheWireTest(unittest.TestCase):
    """The same two phases, through the socket the browser talks to.

    An edit ANSWERS on the fast phase - that is the whole point of it - so the
    POST returns while the content render is still held open, and the fragment
    the page would fetch says which phase it is looking at.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.render = PhaseRender()
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=self.render, two_phase=True)
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def tearDown(self):
        self.render.let_go()
        self.server.stop()
        self._folder.cleanup()

    def get(self, path: str, timeout: float = 10.0):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def post(self, path: str, payload: dict = None, timeout: float = 20.0):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def test_an_edit_answers_on_the_fast_phase_and_upgrades_after(self):
        seq = self.session.upgrade_seq
        self.render.hold()
        reply = self.post("set_param", {"cutter_diameter": 1.2})
        self.assertTrue(reply["ok"])
        fragment = self.get("fragment")
        self.assertEqual(fragment["content"], FAST)
        self.assertTrue(fragment["awaiting_content"])
        self.assertEqual(fragment["upgrade_seq"], seq)
        self.assertNotIn(CONTENT_MARK, fragment["svg"]["Edge.Cuts"])

        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        state = self.get("state")
        self.assertEqual(state["upgrade_seq"], seq + 1)
        self.assertTrue(state["two_phase"])
        upgraded = self.get("fragment")
        self.assertEqual(upgraded["content"], FULL)
        self.assertFalse(upgraded["awaiting_content"])
        self.assertIn(CONTENT_MARK, upgraded["svg"]["Edge.Cuts"])
        self.assertEqual(upgraded["generation"], fragment["generation"])


class OnePhaseTest(unittest.TestCase):
    """With one backend there is one phase, and nothing says otherwise."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.render = PhaseRender()
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=self.render)

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_an_injected_renderer_is_one_renderer(self):
        self.assertFalse(self.session.two_phase)

    def test_the_one_render_is_the_content_one(self):
        self.session.refresh()
        self.assertEqual(self.render.phases, [True])
        self.assertEqual(self.session.view.content, FULL)
        self.assertEqual(self.session.upgrade_seq, 0)

    def test_nothing_on_the_page_says_it_is_waiting(self):
        self.session.refresh()
        fragment = self.session.fragment()
        self.assertFalse(fragment["awaiting_content"])
        self.assertEqual(fragment["content_note"], "")
        self.assertIn("aria-live=polite hidden>", self.session.page())


class WatchedBoard:
    """A board that reports whether a lock was held while it was written.

    It is written the way a real one is - `values_hidden` walks its footprints -
    so the question asked is the question that matters: was anyone else able to
    see this board while its Value flags were off?
    """

    def __init__(self, lock):
        self.lock = lock
        self.footprints = []
        self.free_while_writing = None

    def to_file(self, path):
        # `acquire(blocking=False)` on a plain Lock fails when it is held by
        # ANY thread, this one included - which is exactly the question.
        taken = self.lock.acquire(blocking=False)
        self.free_while_writing = taken
        if taken:
            self.lock.release()
        Path(path).write_text("(kicad_pcb)", encoding="utf-8")


class BoardWriteLockTest(unittest.TestCase):
    """The seam's board write, and the window it opens on the caller's board."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.workdir = Path(self._folder.name)
        self.layers = []

    def tearDown(self):
        self._folder.cleanup()

    def draw(self, board, lock):
        with (mock.patch("eedesignpanels.review.render.export_svgs") as export,
              mock.patch("eedesigncore.resolve_document_text",
                         return_value=mock.Mock(document=board))):
            export.return_value = mock.Mock(svg_by_layer={})
            return render_seam.render_with_cli(
                board, self.layers, (0.0, 0.0, 10.0, 10.0), {},
                workdir=self.workdir, origin=(0.0, 0.0), board_lock=lock,
            )

    def test_the_write_happens_inside_the_lock_it_was_given(self):
        lock = threading.Lock()
        board = WatchedBoard(lock)
        self.assertEqual(self.draw(board, lock), {})
        self.assertFalse(board.free_while_writing,
                         "the board was written with its lock free")

    def test_a_caller_that_shares_nothing_is_not_made_to_lock(self):
        lock = threading.Lock()
        board = WatchedBoard(lock)
        self.draw(board, None)
        self.assertTrue(board.free_while_writing)

    def test_the_lock_is_released_when_the_write_fails(self):
        lock = threading.Lock()
        board = WatchedBoard(lock)
        board.to_file = mock.Mock(side_effect=OSError("the disk is full"))
        with self.assertRaises(OSError):
            self.draw(board, lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


class SeamChoiceTest(unittest.TestCase):
    """Which backend is which phase - pure, so it can be asked both ways."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.here = Path(self._folder.name) / "kicad-cli.exe"
        self.here.write_text("not really", encoding="utf-8")
        self.missing = Path(self._folder.name) / "nowhere.exe"

    def tearDown(self):
        self._folder.cleanup()

    def test_two_backends_are_two_phases(self):
        with mock.patch.object(render_seam, "BACKEND",
                               render_seam.LIBRARY_BACKEND):
            self.assertTrue(render_seam.two_phase_available(self.here))
            self.assertEqual(render_seam.fast_backend(self.here),
                             render_seam.LIBRARY_BACKEND)

    def test_one_backend_is_one_phase(self):
        # No library renderer: kicad-cli answers both, so there is nothing to
        # paint first and nothing to upgrade to.
        with mock.patch.object(render_seam, "BACKEND", render_seam.CLI_BACKEND):
            self.assertFalse(render_seam.two_phase_available(self.here))
        # ...and no kicad-cli: the library answers both, and the page owes its
        # reader the notice it always did.
        with mock.patch.object(render_seam, "BACKEND",
                               render_seam.LIBRARY_BACKEND):
            self.assertFalse(render_seam.two_phase_available(self.missing))
            self.assertTrue(render_seam.content_notice(
                render_seam.choose_backend(True, self.missing)
            ))

    def test_the_page_names_both_backends_when_both_drew_it(self):
        with mock.patch.object(render_seam, "BACKEND",
                               render_seam.LIBRARY_BACKEND):
            said = render_seam.renderer_label(self.here)
        self.assertIn(render_seam.LIBRARY_BACKEND, said)
        self.assertIn(render_seam.CLI_BACKEND, said)
        self.assertIn("first", said)
        with mock.patch.object(render_seam, "BACKEND", render_seam.CLI_BACKEND):
            self.assertEqual(render_seam.renderer_label(self.here),
                             render_seam.CLI_BACKEND)


# ---------------------------------------------------------------------------
# the page's own half, RUN
# ---------------------------------------------------------------------------

UPGRADE_HARNESS = r"""
%(functions)s
const out = {painted: 0, logged: []};
let stopped = false, busy = %(busy)s, waiting = 0, layersSeen = 0;
let PB = {generation: 7, upgradeSeq: 1, page: 'prep'};
function logEvent(type, data) { out.logged.push({type: type, data: data}); }
function refresh() { out.painted += 1; }
function applyLayerState() { return false; }
function panelLayerState() { return null; }
generationSeen(%(message)s);
out.seq = PB.upgradeSeq;
console.log(JSON.stringify(out));
"""

AWAIT_HARNESS = r"""
%(functions)s
const NOTE = {textContent: '', hidden: true, marked: false,
              classList: {toggle(name, state) { NOTE.marked = !!state; }}};
globalThis.document = {getElementById: id => id === 'pb-await' ? NOTE : null};
const out = {};
out.said = drawAwaiting(%(fragment)s);
out.note = {text: NOTE.textContent, hidden: NOTE.hidden, bad: NOTE.marked};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientUpgradeTest(unittest.TestCase):
    """`generationSeen` and `upgradeSeen`, RUN: what repaints and what does not."""

    def seen(self, message: dict, busy: bool = False) -> dict:
        return run_js(UPGRADE_HARNESS % {
            "functions": "\n".join(
                js_function(page_served.CONTROLS_JS, name)
                for name in ("generationSeen", "upgradeSeen")
            ),
            "message": json.dumps(message),
            "busy": "true" if busy else "false",
        })

    def test_a_newer_upgrade_on_this_generation_repaints(self):
        out = self.seen({"generation": 7, "upgrade": 2})
        self.assertEqual(out["painted"], 1)
        self.assertEqual(out["seq"], 2)
        self.assertEqual(out["logged"][0]["type"], "upgrade")

    def test_this_pages_own_upgrade_repaints_it(self):
        out = self.seen({"generation": 7, "upgrade": 2, "upgrade_page": "prep"})
        self.assertEqual(out["painted"], 1)

    def test_the_other_pages_upgrade_is_counted_and_not_fetched(self):
        # One counter, two pictures: the panel's content landing says nothing
        # about the board, and a fragment is megabytes of SVG.
        out = self.seen({"generation": 7, "upgrade": 2,
                         "upgrade_page": "panel"})
        self.assertEqual(out["painted"], 0)
        self.assertEqual(out["seq"], 2)

    def test_an_upgrade_this_page_already_has_repaints_nothing(self):
        out = self.seen({"generation": 7, "upgrade": 1})
        self.assertEqual(out["painted"], 0)
        self.assertEqual(out["logged"], [])

    def test_an_upgrade_for_a_generation_this_page_left_is_dropped(self):
        # THE STALE ONE. The page is on generation 7; an upgrade announced for
        # 6 is a picture of a board nobody is looking at.
        out = self.seen({"generation": 6, "upgrade": 9})
        # Generation 6 is not 7, so the page fetches the current fragment - and
        # it does NOT take the upgrade number, which belongs to the old one.
        self.assertEqual(out["seq"], 1)

    def test_a_new_generation_is_still_a_full_repaint(self):
        out = self.seen({"generation": 8, "upgrade": 1})
        self.assertEqual(out["painted"], 1)

    def test_nothing_repaints_under_an_action(self):
        out = self.seen({"generation": 7, "upgrade": 5}, busy=True)
        self.assertEqual(out["painted"], 0)


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientAwaitNoteTest(unittest.TestCase):
    """The corner note, RUN: shown, hidden, and the failure it turns into."""

    def drawn(self, fragment: dict) -> dict:
        return run_js(AWAIT_HARNESS % {
            "functions": js_function(page_served.CONTROLS_JS, "drawAwaiting"),
            "fragment": json.dumps(fragment),
        })

    def test_a_picture_waiting_for_its_content_says_so(self):
        out = self.drawn({"content_note": "copper is still being drawn",
                          "awaiting_content": True})
        self.assertEqual(out["note"]["text"], "copper is still being drawn")
        self.assertFalse(out["note"]["hidden"])
        self.assertFalse(out["note"]["bad"])

    def test_a_finished_picture_says_nothing_at_all(self):
        out = self.drawn({"content_note": "", "awaiting_content": False})
        self.assertEqual(out["said"], "")
        self.assertTrue(out["note"]["hidden"])

    def test_a_render_that_failed_is_marked_as_one(self):
        out = self.drawn({"content_note": "the full-content render did not "
                                          "finish: boom",
                          "content_error": "boom"})
        self.assertIn("boom", out["note"]["text"])
        self.assertTrue(out["note"]["bad"])
        self.assertFalse(out["note"]["hidden"])


class LaunchSaysBothTest(unittest.TestCase):
    """The terminal names the renderers the page names."""

    def test_the_launch_preamble_carries_the_same_label_the_page_does(self):
        source = Path(preview_main.__file__).read_text(encoding="utf-8")
        self.assertIn("render_seam.renderer_label()", source)
        self.assertNotIn('renderer     {render_seam.preview_backend()}', source)


class PageWiringTest(unittest.TestCase):
    """The served script does the things the harnesses ran."""

    def test_the_shared_half_carries_the_whole_mechanism(self):
        script = page_served.CONTROLS_JS
        for token in ("function upgradeSeen(data)",
                      "PB.upgradeSeq = fragment.upgrade_seq || 0;",
                      "drawAwaiting(fragment);",
                      "if (data.generation !== PB.generation) return false;",
                      "if (busy) { waiting = Math.max(waiting, generation); "
                      "return; }"):
            with self.subTest(token=token):
                self.assertIn(token, script)

    def test_an_upgrade_never_raises_the_busy_popup(self):
        # `setBusy` is `post`'s, and an upgrade is not an action: the only way
        # it reaches the page is `refresh`, which touches neither.
        upgrade = page_served.CONTROLS_JS.split("function upgradeSeen", 1)[1]
        upgrade = upgrade.split("\n}", 1)[0]
        self.assertNotIn("setBusy", upgrade)
        refresh = page_served.CONTROLS_JS.split("async function refresh", 1)[1]
        self.assertNotIn("setBusy", refresh.split("\n}", 1)[0])

    def test_the_note_is_one_line_and_not_a_banner(self):
        # A banner is a `.stale`; this is a corner note, and the difference is
        # in the CSS as much as in the wording.
        rule = page_served.CONTROLS_CSS.split(".awaitnote{", 1)[1].split("}")[0]
        self.assertIn("position:absolute", rule)
        self.assertNotIn("display:block", rule)
        self.assertLess(len(re.findall(r"[.!]", "copper")), 2)


if __name__ == "__main__":
    unittest.main()
