"""The interaction latency work: the breakdown, the tiers, and the wire.

Each interaction records a timing breakdown, and three more mechanisms keep
the interaction latency down. This file is the fence round each:

1. **THE BREAKDOWN.** Every interaction records a complete, named phase chain on
   both sides of the wire, joined by the generation, and
   :mod:`eedesignpanels.preview.timing` reads a session folder back as a table.
   What can go wrong is the JOIN: a refusal or a journal flush that owns a
   generation labels somebody else's edit, which is exactly what happened while
   this was being built (both cases are tests below).
2. **THE GRAPHICS TIER.** The fast render leaves the text and the dimensions
   out - half the render and half the markup, measured - and a second tier swaps
   them in behind the first paint. What can go wrong is ORDERING: a library
   render drawn over kicad-cli's own picture would take the copper out of it.
3. **THE FRAGMENT.** The markup is kept in memory rather than read back off the
   documents just written (345 ms against 9 ms warm, once per edit), and a page
   says which paint it is holding so the answer carries what has moved.
4. **THE BOARD LOCK.** A fragment needs no board, so it no longer waits for a
   content render's board write - which was 655 ms inside a first paint.

Every number quoted above was measured on the corpus panel.
"""

import gzip
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from eedesigncore import PcbText

from eedesignpanels.preview import (
    page_served,
    render_seam,
    server as server_module,
    timing,
)
from eedesignpanels.preview.journal import JOURNAL_NAME
from eedesignpanels.preview.session import (
    FAST,
    FULL,
    GRAPHICS,
    LETTERED,
    PreviewSession,
)
from test_panel_page import write_panel_project
from test_preview import stub_render, write_project
from test_preview_journal import js_function, open_session, run_js
from test_vscore import scoreable_board

WAIT = 30.0


# ---------------------------------------------------------------------------
# a renderer with two tiers in it
# ---------------------------------------------------------------------------


class TierRender:
    """A stub that records which TIER and which PHASE it was asked for.

    The graphics tier and the lettering tier have to produce different bytes,
    because the whole question is which of them a page is looking at.
    """

    LETTERING = '<text id="pb-lettering">REF**</text>'
    CONTENT = '<rect id="pb-content"/>'

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._lock = threading.Lock()

    def hold(self):
        """Stop the next LETTERING render inside itself, until `let_go`."""
        self.release.clear()
        self.entered.clear()

    def let_go(self):
        self.release.set()

    @property
    def tiers(self) -> list:
        with self._lock:
            return list(self.calls)

    def __call__(self, board, layers, viewbox_mm, colors, theme=None,
                 workdir=None, content=False, origin=None, board_lock=None,
                 lettering=True):
        with self._lock:
            self.calls.append((bool(content), bool(lettering)))
        if lettering and not content:
            self.entered.set()
            self.release.wait(WAIT)
        produced = stub_render(board, layers, viewbox_mm, colors, theme,
                               workdir, content)
        mark = ""
        if lettering and not content:
            mark = self.LETTERING
        elif content:
            mark = self.CONTENT
        if not mark:
            return produced
        return {
            name: markup.replace("</svg>", f"{mark}</svg>")
            for name, markup in produced.items()
        }


class TieredTestCase(unittest.TestCase):
    """A recorded session with all three tiers switched on."""

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
        self.render = TierRender()
        self.session = PreviewSession(
            self.config_path, self.recording.folder, render=self.render,
            journal=self.recording, two_phase=True, tiered=True,
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


# ---------------------------------------------------------------------------
# 2. the graphics tier
# ---------------------------------------------------------------------------


class GraphicsFirstTest(TieredTestCase):
    """Graphics at once, lettering behind them, content behind that."""

    def test_the_first_render_leaves_the_lettering_out(self):
        self.render.hold()
        self.session.refresh()
        self.assertEqual(self.session.view.lettering, GRAPHICS)
        self.assertEqual(self.session.view.content, FAST)
        # (content, lettering) - the first call is neither.
        self.assertEqual(self.render.tiers[0], (False, False))
        self.assertNotIn(TierRender.LETTERING, self.markup_of())
        self.render.let_go()

    def test_the_lettering_lands_behind_the_first_paint(self):
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.view.lettering, LETTERED)
        self.assertEqual(self.session.view.content, FULL)
        self.assertEqual(self.render.tiers,
                         [(False, False), (False, True), (True, True)])

    def test_the_tier_is_journaled_with_its_own_timings(self):
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        lettered = self.records("letter")[-1]["data"]
        self.assertEqual(lettered["subject"], "board")
        self.assertTrue(lettered["applied"])
        self.assertEqual(lettered["generation"], self.session.generation)
        self.assertGreaterEqual(lettered["seconds"], 0.0)
        self.assertGreaterEqual(lettered["waited"], 0.0)
        self.assertGreater(lettered["layers"], 0)
        self.assertGreater(lettered["changed"], 0)

    def test_the_tier_moves_the_upgrade_counter_and_not_the_generation(self):
        self.render.hold()
        self.session.refresh()
        generation, seq = self.session.generation, self.session.upgrade_seq
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.generation, generation)
        # Two pushes on one generation: the lettering, then the content.
        self.assertEqual(self.session.upgrade_seq, seq + 2)

    def test_the_fragment_says_which_tier_its_picture_is(self):
        self.render.hold()
        self.session.refresh()
        self.assertEqual(self.session.fragment()["lettering"], GRAPHICS)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.fragment()["lettering"], LETTERED)

    def test_a_session_with_one_renderer_has_one_tier(self):
        # A caller that injected a renderer supplied one function, so the
        # default is one tier for it - the tier is a library-renderer feature.
        plain = PreviewSession(self.config_path, self.folder / "plain",
                               render=stub_render)
        self.assertFalse(plain.tiered)
        plain.refresh()
        self.assertEqual(plain.view.lettering, LETTERED)
        plain.close()


class TierOrderingTest(TieredTestCase):
    """The three orderings, and none of them may draw over a better picture."""

    def test_a_content_render_is_never_re_lettered(self):
        # THE FENCE. kicad-cli draws the lettering itself, so a view that has
        # had its content render is LETTERED - and a library render drawn over
        # it would take the copper out of the picture.
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.view.content, FULL)
        self.assertEqual(self.session.view.lettering, LETTERED)
        self.assertEqual(self.session._unlettered_views(), [])
        markup = self.markup_of()
        self.assertIn(TierRender.CONTENT, markup)
        self.assertNotIn(TierRender.LETTERING, markup)
        # ...and asking the worker again draws nothing more.
        tiers = len(self.render.tiers)
        self.session._schedule_upgrades(self.session.generation)
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(len(self.render.tiers), tiers)

    def test_a_new_edit_drops_the_tier_that_was_waiting(self):
        self.render.hold()
        self.session.refresh()
        self.assertTrue(self.render.entered.wait(WAIT))
        stale = self.session.generation
        self.session.refresh()          # the new edit, while the tier draws
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        dropped = self.records("letter.dropped")
        self.assertTrue(dropped)
        self.assertEqual(dropped[0]["data"]["generation"], stale)
        self.assertEqual(self.session.view.lettering, LETTERED)

    def test_a_tier_that_fails_is_said_and_not_retried(self):
        held = threading.Event()
        go = threading.Event()

        def explode(*args, **kwargs):
            if kwargs.get("lettering", True) and not kwargs.get("content"):
                raise RuntimeError("the font is not there")
            if kwargs.get("content"):
                held.set()
                go.wait(WAIT)
            return stub_render(*args, **{
                key: value for key, value in kwargs.items()
                if key in ("theme", "workdir", "content")
            })

        self.session._render = explode
        with mock.patch("traceback.print_exc"):
            self.session.refresh()
            self.assertTrue(held.wait(WAIT))
            # A FAILURE OUTRANKS A WAIT: the corner says the text is missing
            # rather than promising content, because until the content render
            # lands this picture looks like a board with no text on it.
            note = self.session.fragment()["content_note"]
            self.assertIn("text and dimensions could not be drawn", note)
            self.assertEqual(self.session.view.lettering, GRAPHICS)
            go.set()
            self.assertTrue(self.session.wait_for_upgrades(WAIT))
        failed = self.records("letter.failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("the font is not there", failed[0]["data"]["error"])
        # ...and the content render draws that text itself, so the sentence
        # goes with the picture it was about.
        self.assertEqual(self.session.view.lettering, LETTERED)
        self.assertEqual(self.session.fragment()["content_note"], "")

    def test_the_tier_waits_for_the_first_paint_to_go_out(self):
        # The tier holds the board lock while it draws. Starting it before the
        # picture had been handed to anybody would put the page's own fragment
        # request behind the render it was supposed to arrive ahead of.
        self.session.refresh()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        waited = [r["data"]["waited"] for r in self.records("letter")]
        self.assertTrue(waited)
        # Nothing fetched, so every tier waited out its whole grace.
        self.assertGreater(max(waited), 0.1)

    def test_a_page_that_fetches_releases_the_tier_at_once(self):
        listener = self.session.subscribe()
        self.assertIsNotNone(listener.wait(WAIT))
        started = time.monotonic()
        thread = threading.Thread(target=self.session.refresh)
        thread.start()
        # Fetch as a page would, as soon as the generation is published.
        self.assertIsNotNone(listener.wait(WAIT))
        self.session.fragment()
        thread.join(WAIT)
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertLess(time.monotonic() - started, WAIT)
        self.assertLess(self.records("letter")[0]["data"]["waited"], 0.3)
        self.session.unsubscribe(listener)


class PanelTierTest(TieredTestCase):
    """Both subjects get both tiers, and each page hears about its own."""

    panelizes = True

    def test_the_panel_is_lettered_behind_its_own_first_paint(self):
        self.render.hold()
        self.session.refresh()
        self.session.start_panel()
        self.assertEqual(self.session.panel.lettering, GRAPHICS)
        self.render.let_go()
        self.assertTrue(self.session.wait_for_upgrades(WAIT))
        self.assertEqual(self.session.panel.lettering, LETTERED)
        subjects = [r["data"]["subject"] for r in self.records("letter")]
        self.assertEqual(subjects, ["board", "panel"])

    def test_each_page_is_served_its_own_grace(self):
        # The two tabs fetch about 90 ms apart. A tier released by the OTHER
        # page's fetch landed on this one before it had asked for anything,
        # and its first paint was then the lettered picture (measured).
        self.session.refresh()
        self.session.start_panel()
        self.session.fragment()
        self.assertEqual(
            self.session._served_generation["prep"], self.session.generation
        )
        self.assertEqual(self.session._served_generation["panel"], 0)


# ---------------------------------------------------------------------------
# the seam's own half of the tier
# ---------------------------------------------------------------------------


class LetteringPolicyTest(unittest.TestCase):
    """The graphics tier is expressed where the library says it may be."""

    def setUp(self):
        self.board = scoreable_board()
        self.board.drawings.append(PcbText(
            content="PANEL", position=(2.0, 2.0), layer="F.Cu",
        ))
        from eedesignpanels.review.render import layers_of

        self.layers = [
            layer for layer in layers_of(self.board)
            if layer.canonical_name in ("F.Cu", "Edge.Cuts")
        ]
        self.box = (-5.0, -5.0, 40.0, 40.0)
        self.colors = {"F.Cu": "#ffffff", "Edge.Cuts": "#d0d000"}

    def draw(self, lettering: bool) -> dict:
        return render_seam.render_layers(
            self.board, self.layers, self.box, self.colors,
            lettering=lettering,
        )

    def test_the_graphics_tier_draws_no_text_and_the_lettered_one_does(self):
        graphics, lettered = self.draw(False), self.draw(True)
        self.assertLess(len(graphics["F.Cu"]), len(lettered["F.Cu"]))
        self.assertFalse(render_seam.has_drawing(graphics["F.Cu"]))
        self.assertTrue(render_seam.has_drawing(lettered["F.Cu"]))
        # ...and a layer with no lettering on it is IDENTICAL in both tiers,
        # which is what lets the second tier be a swap of a few layers.
        self.assertEqual(graphics["Edge.Cuts"], lettered["Edge.Cuts"])

    def test_the_policy_drops_by_item_kind_and_keeps_everything_else(self):
        base = mock.Mock()
        base.style_for.return_value = "styled"
        policy = render_seam.LetteringHidden(base=base)
        for kind in render_seam.LETTERING_ITEMS:
            with self.subTest(kind=kind):
                item = mock.Mock()
                item.item = type(kind, (), {})()
                self.assertIsNone(policy.style_for(item))
        kept = mock.Mock()
        kept.item = type("PcbLine", (), {})()
        self.assertEqual(policy.style_for(kept), "styled")

    def test_two_tiers_need_the_library_renderer(self):
        with mock.patch.object(render_seam, "BACKEND",
                               render_seam.LIBRARY_BACKEND):
            self.assertTrue(render_seam.tiered_available())
        with mock.patch.object(render_seam, "BACKEND",
                               render_seam.CLI_BACKEND):
            self.assertFalse(render_seam.tiered_available())


# ---------------------------------------------------------------------------
# 3. the fragment: kept in memory, and sent by difference
# ---------------------------------------------------------------------------


class KeptMarkupTest(unittest.TestCase):
    """A fragment is served from memory, not from the documents on disk."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_the_fragment_needs_none_of_the_files_it_wrote(self):
        held = self.session.fragment()["svg"]
        # The sharpest statement of "from memory": take the documents away.
        for path in list(self.session.view.svg_by_layer.values()):
            Path(path).unlink()
        self.assertEqual(self.session.fragment()["svg"], held)

    def test_the_documents_are_still_written_for_the_session_folder(self):
        for name, path in self.session.view.svg_by_layer.items():
            with self.subTest(layer=name):
                self.assertTrue(Path(path).is_file())

    def test_memory_and_disk_say_the_same_thing_after_a_repaint(self):
        produced = stub_render(
            self.session.view.plan.board, self.session.view.layers,
            self.session.view.window, {}, content=True,
        )
        produced = {
            name: markup.replace("</svg>", '<rect id="new"/></svg>')
            for name, markup in produced.items()
        }
        drawn, changed = self.session._repaint(self.session.view, produced)
        self.assertGreater(changed, 0)
        self.assertEqual(drawn, len(self.session.view.svg_by_layer))
        for name, markup in self.session.fragment()["svg"].items():
            with self.subTest(layer=name):
                on_disk = page_served.inner_svg(
                    self.session.view.svg_by_layer[name]
                )
                self.assertEqual(markup, on_disk)

    def test_a_repaint_that_changes_nothing_says_so(self):
        produced = {
            layer.canonical_name: Path(
                self.session.view.svg_by_layer[layer.canonical_name]
            ).read_text(encoding="utf-8")
            for layer in self.session.view.layers
            if layer.canonical_name in self.session.view.svg_by_layer
        }
        drawn, changed = self.session._repaint(self.session.view, produced)
        self.assertGreater(drawn, 0)
        self.assertEqual(changed, 0)


class FragmentDiffTest(unittest.TestCase):
    """A page says which paint it holds; the answer carries what has moved."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_a_page_with_nothing_is_sent_the_whole_stack(self):
        fragment = self.session.fragment(painted=0)
        self.assertEqual(len(fragment["svg"]),
                         len(self.session.view.svg_by_layer))
        self.assertGreater(fragment["paint_seq"], 0)

    def test_a_page_that_is_up_to_date_is_sent_no_markup_at_all(self):
        painted = self.session.fragment()["paint_seq"]
        again = self.session.fragment(painted=painted)
        self.assertEqual(again["svg"], {})
        # ...and it still carries everything else the page updates itself from.
        self.assertEqual(again["layers"],
                         [layer.canonical_name
                          for layer in self.session.view.layers])
        self.assertEqual(again["paint_seq"], painted)

    def test_only_the_layers_that_moved_are_sent(self):
        painted = self.session.fragment()["paint_seq"]
        view = self.session.view
        name = next(iter(view.svg_by_layer))
        produced = {name: '<svg viewBox="0 0 1 1"><rect id="moved"/></svg>'}
        self.session._repaint(view, produced)
        fragment = self.session.fragment(painted=painted)
        self.assertEqual(list(fragment["svg"]), [name])
        self.assertIn("moved", fragment["svg"][name])
        self.assertGreater(fragment["paint_seq"], painted)

    def test_an_unchanged_layer_keeps_the_paint_it_was_drawn_at(self):
        first = dict(self.session.view.svg_stamp)
        # A recomputation that produces the same pictures: the stamps must not
        # move, or every edit would re-send every layer.
        self.session.refresh()
        after = dict(self.session.view.svg_stamp)
        self.assertEqual(set(first), set(after))
        self.assertEqual(
            [after[name] for name in first if name != "User.9"],
            [first[name] for name in first if name != "User.9"],
        )

    def test_a_panel_only_edit_sends_the_board_page_nothing(self):
        # THE POINT OF THE DIFF. A carrier edit does not touch the board, and
        # the prep page used to re-fetch its whole stack for it anyway.
        painted = self.session.fragment()["paint_seq"]
        self.session.set_param({"rail_mm": 12.0})
        fragment = self.session.fragment(painted=painted)
        self.assertEqual(fragment["svg"], {})

    def test_the_panel_fragment_diffs_on_its_own_paint(self):
        painted = self.session.panel_fragment()["paint_seq"]
        self.assertGreater(painted, 0)
        self.assertEqual(self.session.panel_fragment(painted=painted)["svg"],
                         {})

    def test_the_served_page_carries_the_paint_its_stack_is(self):
        markup = self.session.page()
        self.assertIn(
            f'"paintSeq": {self.session._painted_at(self.session.view)}',
            markup,
        )


class FragmentRouteTest(unittest.TestCase):
    """`?painted=` over the socket, and the compression under it."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def tearDown(self):
        self.server.stop()
        self._folder.cleanup()

    def get(self, path: str, gzipped: bool = False):
        request = urllib.request.Request(self.url + path.lstrip("/"))
        request.add_header("Accept-Encoding", "gzip" if gzipped else "identity")
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.headers, reply.read()

    def test_the_route_reads_the_paint_a_page_says_it_holds(self):
        _, body = self.get("/fragment")
        painted = json.loads(body)["paint_seq"]
        _, again = self.get(f"/fragment?painted={painted}")
        self.assertEqual(json.loads(again)["svg"], {})

    def test_a_paint_number_that_is_not_one_asks_for_everything(self):
        for asked in ("", "abc", "-4", "99999999999", "1e9"):
            with self.subTest(asked=asked):
                _, body = self.get(f"/fragment?painted={asked}")
                self.assertTrue(json.loads(body)["svg"])

    def test_the_journal_says_what_a_fragment_cost_and_how_big_it_was(self):
        recording = open_session(self.folder / "recorded",
                                 config_path=self.config_path)
        self.session.journal = recording
        self.get("/fragment", gzipped=True)
        served = [
            r["data"] for r in recording.records()
            if r["kind"] == "server.get"
            and r["data"].get("endpoint") == "/fragment"
        ]
        self.assertEqual(len(served), 1)
        record = served[0]
        self.assertGreater(record["bytes"], 0)
        # What the payload measures and what went down the socket are two
        # numbers once the body is compressed - and the same number for a
        # fragment as small as this fixture's, which is never compressed.
        self.assertLessEqual(record["sent"], record["bytes"])
        self.assertEqual(record["generation"], self.session.generation)
        self.assertGreater(record["layers"], 0)
        for phase in ("wait", "build", "json", "write"):
            with self.subTest(phase=phase):
                self.assertGreaterEqual(record["phases"][phase], 0.0)

    def test_a_client_that_takes_gzip_gets_gzip(self):
        big = "x" * (server_module.GZIP_THRESHOLD_BYTES + 1)
        with mock.patch.object(self.session, "fragment",
                               return_value={"svg": {"F.Cu": big}}):
            headers, body = self.get("/fragment", gzipped=True)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(body))["svg"]["F.Cu"], big)
        self.assertLess(len(body), len(big) / 4)

    def test_a_client_that_did_not_offer_gets_exactly_what_it_asked_for(self):
        big = "x" * (server_module.GZIP_THRESHOLD_BYTES + 1)
        with mock.patch.object(self.session, "fragment",
                               return_value={"svg": {"F.Cu": big}}):
            headers, body = self.get("/fragment", gzipped=False)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(json.loads(body)["svg"]["F.Cu"], big)

    def test_a_small_answer_is_never_compressed(self):
        headers, body = self.get("/state", gzipped=True)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertTrue(json.loads(body)["ok"])


# ---------------------------------------------------------------------------
# 4. the board lock: a fragment does not wait for a render
# ---------------------------------------------------------------------------


class BoardLockTest(unittest.TestCase):
    """A content render holds the BOARD lock, and a fragment needs no board."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def test_the_two_locks_are_two_locks(self):
        self.assertIsNot(self.session._lock, self.session._board_lock)

    def test_a_fragment_is_served_while_the_board_lock_is_held(self):
        # Measured: this wait was 655 ms on the corpus panel, inside
        # the first paint of the edit that had just been made.
        held = threading.Event()
        answered = []

        def hold():
            with self.session._board_lock:
                held.set()
                time.sleep(0.5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(WAIT))
        started = time.monotonic()
        answered.append(self.session.fragment())
        self.assertLess(time.monotonic() - started, 0.4)
        thread.join(WAIT)
        self.assertTrue(answered[0]["ok"])

    def test_a_computation_does_take_the_board_lock(self):
        # The other half of the lock split: prep rewrites the held board and the
        # render reads it, so a computation may not run inside a content
        # render's Value-hiding write.
        taken = []
        original = self.session._compute

        def watched(held=None):
            taken.append(self.session._board_lock.acquire(blocking=False))
            if taken[-1]:
                self.session._board_lock.release()
            return original(held)

        self.session._compute = watched
        self.session.refresh()
        self.assertTrue(taken and taken[-1])


# ---------------------------------------------------------------------------
# 1. the breakdown, and the tool that reads it
# ---------------------------------------------------------------------------


def journal_of(folder: Path, records: list) -> Path:
    """A session folder holding exactly *records*, for the reader to read."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / JOURNAL_NAME
    path.write_text(
        "".join(
            json.dumps({"seq": index + 1, "at": "2026-09-11T00:00:00.000Z",
                        "kind": kind, "data": data}) + "\n"
            for index, (kind, data) in enumerate(records)
        ),
        encoding="utf-8",
    )
    return path


def edit(generation: int, endpoint: str = "/set_param", seconds: float = 0.25,
         payload: dict = None, compute: dict = None, recalc: dict = None,
         ok: bool = True) -> list:
    """One interaction's records, in the order a session writes them."""
    out = []
    if recalc is not None:
        out.append(("panel.recalc", {"generation": generation, "ok": True,
                                     "seconds": 0.2, "phases": recalc}))
    if compute is not None:
        out.append(("compute", {"generation": generation, "ok": True,
                                "seconds": 0.2, "phases": compute}))
    out.append(("server.action", {
        "endpoint": endpoint, "generation": generation, "ok": ok,
        "error": "" if ok else "no", "rejected": False,
        "seconds": seconds, "payload": payload or {"rail_mm": 10.0},
    }))
    return out


class TimingReaderTest(unittest.TestCase):
    """The journal read back: what belongs to which interaction, and its kind."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def read(self, records: list) -> list:
        journal_of(self.folder, records)
        return timing.interactions(timing.read_journal(self.folder))

    def test_one_generation_is_one_interaction(self):
        found = self.read([
            *edit(2, compute={"prep": 100.0, "render": 40.0}),
            *edit(3, recalc={"build": 300.0, "carrier_only": 300.0}),
        ])
        self.assertEqual([one.generation for one in found], [2, 3])
        self.assertEqual(found[0].total_ms, 250.0)

    def test_the_kind_is_derived_from_what_the_run_actually_did(self):
        cases = (
            (timing.BOARD_EDIT, {"compute": {"prep": 10.0}}),
            (timing.LAYOUT_EDIT, {"recalc": {"build": 10.0,
                                             "build_full": 10.0}}),
            (timing.CARRIER_EDIT, {"recalc": {"build": 10.0,
                                              "carrier_only": 10.0}}),
            (timing.VIEW_ONLY_EDIT, {}),
        )
        for expected, how in cases:
            with self.subTest(kind=expected):
                found = self.read(edit(2, **how))
                self.assertEqual(found[0].kind, expected)

    def test_a_named_endpoint_is_called_what_the_user_calls_it(self):
        found = self.read(edit(2, endpoint="/build_panel"))
        self.assertEqual(found[0].kind, "build panel")

    def test_a_refusal_never_names_the_interaction_it_landed_in(self):
        # Measured while this was being built: a `/set_param` refused for an
        # unknown key labelled the LAUNCH a board edit, because a refusal
        # carries whatever generation was current.
        found = self.read([
            ("compute", {"generation": 1, "ok": True, "seconds": 0.4,
                         "phases": {"load": 200.0, "prep": 100.0}}),
            *edit(1, ok=False),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, timing.LAUNCH)
        self.assertEqual(len(found[0].refused), 1)

    def test_a_journal_flush_never_names_the_interaction_either(self):
        found = self.read([
            *edit(2, recalc={"build": 10.0, "carrier_only": 10.0}),
            ("server.action", {"endpoint": "/journal", "generation": 2,
                               "ok": True, "seconds": 0.01, "payload": {}}),
        ])
        self.assertEqual(found[0].kind, timing.CARRIER_EDIT)
        self.assertEqual(len(found[0].alongside), 1)

    def test_the_request_phase_is_the_remainder_so_the_parts_add_up(self):
        found = self.read(edit(2, seconds=0.5,
                               compute={"prep": 300.0, "render": 100.0}))
        chain = found[0].chain()
        self.assertEqual(chain["board.prep"], 300.0)
        self.assertEqual(chain["request"], 100.0)
        self.assertEqual(round(sum(chain.values()), 1), found[0].total_ms)

    def test_the_two_halves_of_the_wire_are_joined_by_the_generation(self):
        found = self.read([
            *edit(2, recalc={"build": 10.0, "carrier_only": 10.0}),
            ("server.get", {"endpoint": "/panel/fragment", "generation": 2,
                            "seconds": 0.06, "bytes": 2031335, "sent": 648448,
                            "layers": 51, "paint_seq": 7,
                            "phases": {"wait": 0.0, "build": 0.4, "json": 3.5,
                                       "write": 54.3}}),
            ("client.paint", {"generation": 2, "page": "panel",
                              "cause": "generation", "bytes": 2031335,
                              "fetch_ms": 44, "read_ms": 1, "parse_ms": 1,
                              "svg_ms": 30, "apply_ms": 8, "total_ms": 93}),
            ("client.paint", {"generation": 2, "page": "panel",
                              "cause": "upgrade", "bytes": 1800000,
                              "total_ms": 200}),
            ("letter", {"generation": 2, "subject": "panel", "seconds": 0.285,
                        "layers": 51, "changed": 3}),
        ])
        one = found[0]
        self.assertEqual(one.fragment["bytes"], 2031335)
        self.assertEqual(one.client["total_ms"], 93)
        self.assertEqual(one.behind["lettering"], 285.0)
        # The first paint is the reply plus the paint behind it, and the TIER
        # repaint is not the first paint.
        self.assertEqual(one.first_paint_ms, 343.0)
        self.assertEqual(one.wire_bytes, 2031335 + 1800000)

    def test_a_clicked_action_is_timed_by_the_page_that_clicked_it(self):
        found = self.read([
            *edit(2, recalc={"build": 10.0, "carrier_only": 10.0}),
            ("client.action", {"generation": 2, "page": "panel",
                               "endpoint": "/set_param", "ok": True,
                               "post_ms": 260, "total_ms": 410}),
        ])
        self.assertEqual(found[0].first_paint_ms, 410.0)

    def test_a_half_written_last_line_is_a_crash_and_not_a_broken_file(self):
        path = journal_of(self.folder, edit(2))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 99, "kind": "compu')
        self.assertEqual(len(timing.interactions(
            timing.read_journal(self.folder)
        )), 1)

    def test_the_table_and_the_page_come_out_of_the_same_numbers(self):
        found = self.read([
            *edit(2, recalc={"build": 10.0, "carrier_only": 10.0}),
            *edit(3, recalc={"build": 12.0, "carrier_only": 12.0}),
        ])
        text = timing.table(found, title="drive")
        self.assertIn("carrier edit", text)
        self.assertIn("PHASE SPLIT", text)
        page = timing.page([("after", timing.summarise(found))],
                           command=timing.USAGE)
        self.assertIn("carrier edit", page)
        # The usage line is on the page, escaped as page text is.
        self.assertIn("python -m eedesignpanels.preview.timing", page)
        # The portability rule every page of this project keeps: one
        # self-contained file that loads nothing from outside itself.
        for forbidden in ("<link", "<script", "@import", "url(http"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, page)

    def test_the_summary_is_a_median_and_a_worst_case(self):
        found = self.read([
            *edit(2, seconds=0.1, recalc={"build": 1.0, "carrier_only": 1.0}),
            *edit(3, seconds=0.2, recalc={"build": 1.0, "carrier_only": 1.0}),
            *edit(4, seconds=0.9, recalc={"build": 1.0, "carrier_only": 1.0}),
        ])
        one = timing.summarise(found)[0]
        self.assertEqual(one.count, 3)
        self.assertEqual(one.reply_median, 200.0)
        self.assertEqual(one.reply_worst, 900.0)

    def test_a_folder_with_no_journal_says_so(self):
        with self.assertRaises(FileNotFoundError):
            timing.read_journal(self.folder / "nowhere")

    def test_the_command_runs_on_a_session_folder(self):
        journal_of(self.folder, edit(2, recalc={"build": 1.0,
                                                "carrier_only": 1.0}))
        page = self.folder / "out.html"
        data = self.folder / "out.json"
        self.assertEqual(timing.main([
            str(self.folder), "--html", str(page), "--json", str(data),
        ]), 0)
        self.assertIn("carrier edit", page.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(data.read_text(encoding="utf-8"))["kinds"][0]["kind"],
            timing.CARRIER_EDIT,
        )

    def test_a_session_nobody_drove_is_not_a_table(self):
        journal_of(self.folder, [("session.start", {"schema": "x"})])
        self.assertEqual(timing.main([str(self.folder)]), 1)


class PhaseChainTest(unittest.TestCase):
    """A real session's own records carry the whole chain, named."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.recording = open_session(self.folder,
                                      config_path=self.config_path)
        self.session = PreviewSession(self.config_path, self.recording.folder,
                                      render=stub_render,
                                      journal=self.recording)
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def records(self, kind: str) -> list:
        return [r for r in self.recording.records() if r["kind"] == kind]

    def test_the_board_computation_names_every_phase_of_itself(self):
        phases = self.records("compute")[-1]["data"]["phases"]
        for name in ("load", "prep", "frame", "render", "write_svgs",
                     "overlay"):
            with self.subTest(phase=name):
                self.assertIn(name, phases)
                self.assertGreaterEqual(phases[name], 0.0)

    def test_the_panel_recalculation_names_the_generation_it_will_be(self):
        # It happens BEFORE the generation is bumped, so it states the number
        # it is about to be published as - which is what joins it to its own
        # interaction.
        self.session.set_param({"rail_mm": 11.0})
        recalc = self.records("panel.recalc")[-1]["data"]
        self.assertEqual(recalc["generation"], self.session.generation)
        self.assertIn("build", recalc["phases"])

    def test_the_whole_chain_reads_back_as_one_interaction(self):
        # A CARRIER setting, which is what keeps the stepped panel: the rails
        # are the blank's own size and rebuild it, a carrier STAGE does not
        # (`panelbuild.CARRIER_SECTIONS`).
        self.session.set_param({"rail_copper_on": True})
        self.session.fragment()
        found = {
            one.generation: one for one in
            timing.interactions(timing.read_journal(self.recording.folder))
        }
        one = found[self.session.generation]
        self.assertEqual(one.kind, timing.CARRIER_EDIT)
        self.assertIn("panel.build", one.chain())
        self.assertIn("panel.render", one.chain())


# ---------------------------------------------------------------------------
# the page's own half, RUN
# ---------------------------------------------------------------------------

URL_HARNESS = r"""
%(functions)s
let PB = %(state)s;
console.log(JSON.stringify({url: fragmentUrl(%(painted)s)}));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientDiffTest(unittest.TestCase):
    """`fragmentUrl`, RUN: what a page asks for and how it asks."""

    def asked(self, painted, state: dict = None) -> str:
        out = run_js(URL_HARNESS % {
            "functions": js_function(page_served.CONTROLS_JS, "fragmentUrl"),
            "state": json.dumps(state or {}),
            "painted": json.dumps(painted),
        })
        return out["url"]

    def test_a_page_with_nothing_asks_for_everything(self):
        self.assertEqual(self.asked(0), "/fragment")

    def test_a_page_says_which_paint_it_holds(self):
        self.assertEqual(self.asked(7), "/fragment?painted=7")

    def test_the_panel_page_asks_its_own_route(self):
        self.assertEqual(
            self.asked(7, {"fragmentUrl": "/panel/fragment"}),
            "/panel/fragment?painted=7",
        )

    def test_a_route_that_already_has_a_query_is_appended_to(self):
        self.assertEqual(
            self.asked(7, {"fragmentUrl": "/fragment?x=1"}),
            "/fragment?x=1&painted=7",
        )


class ClientTimingWiringTest(unittest.TestCase):
    """The served script measures its own half and reports it."""

    def test_the_paint_is_timed_phase_by_phase(self):
        script = page_served.CONTROLS_JS
        for token in ("paintedIn.fetch_ms", "paintedIn.read_ms",
                      "paintedIn.parse_ms", "paintedIn.svg_ms",
                      "paintedIn.apply_ms", "logEvent('paint'",
                      "page: PB.page || 'prep'"):
            with self.subTest(token=token):
                self.assertIn(token, script)

    def test_the_action_is_timed_from_the_click_to_the_paint(self):
        script = page_served.CONTROLS_JS
        self.assertIn("const clicked = (performance && performance.now)",
                      script)
        self.assertIn("post_ms: replied, total_ms: since()", script)
        self.assertIn("await refresh('action');", script)

    def test_the_paint_number_is_kept_only_once_the_markup_is_in(self):
        # Set before the swap it would be a claim about a picture this page
        # has not got, and the next fetch would skip a layer that never came.
        refresh = page_served.CONTROLS_JS.split("async function refresh", 1)[1]
        before, after = refresh.split("PB.paintSeq = fragment.paint_seq;", 1)
        self.assertIn("paintSvgs(fragment.svg, fragment.layers)", before)
        self.assertIn("notePaint('painted')", after)


if __name__ == "__main__":
    unittest.main()
