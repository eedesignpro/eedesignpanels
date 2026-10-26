"""The tab lifecycle: one tab is a whole session, and the LAST one to close
ends it.

**The server stops when the LAST tab closes, and closing one of several
leaves it running.** The event stream is the presence signal, so the hard
part is that a RELOAD drops it exactly the way a close does. The answer is a
grace window (:mod:`eedesignpanels.preview.lifecycle`): a page that comes back
inside it was reloading, and a page that does not had its tab closed - and
`due()` only answers once NO step is holding a stream at all. Exercised
against a real server on 127.0.0.1 with two clients - closing one leaves the
session running with nothing sent to the other, and closing the second stops
it - and the window itself is unit-tested against an injected clock, so
nothing here waits five seconds to find out.

**Step 1 opens at start; step 2 opens from step 1's own control.** `--open`
opens step 1 only, so no second tab appears unasked. Both addresses are still
printed whether or not `--open` was passed.

**The step links jump, never spawn.** The links are aimed at a stable window
NAME, each page adopts its own name on load, and the click handler asks the
server whether the other step is open before it opens anything. The handler is
run under node against a stated window registry, because "a named window is reused"
is a browser behaviour and a string assertion cannot tell reuse from a third
tab.

**The layout toggle is gone from the served pages and stays on the frozen one.**
Fenced in both directions on emitted bytes.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from eedesignpanels.preview import (
    __main__ as preview_main,
    lifecycle,
    page_served,
    server as server_module,
)
from eedesignpanels.review import layerpanel, page as review_page, theme
from eedesignpanels.review.render import LayerSpec
from test_preview_journal import js_function, run_js
from test_preview_linked import WAIT_SECONDS, EventClient, TwoTabTestCase

# Short enough that a close is declared while a test is still watching, long
# enough that it is not tripped by the loopback round trip that opens a stream.
TEST_GRACE = 0.4


def wait_until(predicate, timeout: float = WAIT_SECONDS) -> bool:
    """Poll *predicate* until it is true or the bounded wait runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# the grace window, on an injected clock
# ---------------------------------------------------------------------------


class LifecycleTest(unittest.TestCase):
    """A reload and a close look identical. This is what tells them apart."""

    def setUp(self):
        self.tabs = lifecycle.Lifecycle(grace=5.0)

    def test_a_page_that_never_opened_can_never_close(self):
        self.assertFalse(self.tabs.seen("panel"))
        self.assertIsNone(self.tabs.due(1000.0))
        self.assertEqual(self.tabs.present(), {"prep": False, "panel": False})

    def test_an_open_page_is_present_and_not_due(self):
        self.tabs.opened("prep", 0.0)
        self.assertEqual(self.tabs.present()["prep"], True)
        self.assertIsNone(self.tabs.due(1000.0))

    def test_a_drop_inside_the_grace_window_is_not_a_close(self):
        self.tabs.opened("prep", 0.0)
        self.tabs.dropped("prep", 10.0)
        self.assertIsNone(self.tabs.due(14.9))

    def test_a_page_that_comes_back_was_reloading(self):
        self.tabs.opened("prep", 0.0)
        self.tabs.dropped("prep", 10.0)
        self.tabs.opened("prep", 10.4)
        # Long past the window, and nothing is due: the reconnect cancelled it.
        self.assertIsNone(self.tabs.due(1000.0))
        self.assertTrue(self.tabs.present()["prep"])

    def test_a_page_that_does_not_come_back_was_closed(self):
        self.tabs.opened("panel", 0.0)
        self.tabs.dropped("panel", 10.0)
        self.assertEqual(self.tabs.due(15.0), "panel")

    def test_the_close_is_declared_once_and_once_only(self):
        # The watchdog asks four times a second; the shutdown it triggers
        # happens once.
        self.tabs.opened("panel", 0.0)
        self.tabs.dropped("panel", 10.0)
        self.assertEqual(self.tabs.due(20.0), "panel")
        self.assertIsNone(self.tabs.due(21.0))
        self.assertEqual(self.tabs.closed, "panel")

    def test_a_second_stream_for_one_step_keeps_it_open(self):
        # A duplicate tab, or the moment in a reload when the new page has
        # connected and the old socket has not been reaped yet.
        self.tabs.opened("prep", 0.0)
        self.tabs.opened("prep", 1.0)
        self.tabs.dropped("prep", 2.0)
        self.assertTrue(self.tabs.present()["prep"])
        self.assertIsNone(self.tabs.due(1000.0))
        self.tabs.dropped("prep", 3.0)
        self.assertEqual(self.tabs.due(1000.0), "prep")

    def test_presence_goes_false_the_moment_the_last_stream_drops(self):
        # What a step link reads: the grace window is about declaring a CLOSE,
        # not about pretending a tab is still there.
        self.tabs.opened("panel", 0.0)
        self.tabs.dropped("panel", 1.0)
        self.assertFalse(self.tabs.present()["panel"])

    def test_a_client_that_is_not_one_of_the_steps_is_not_tracked(self):
        self.assertFalse(self.tabs.tracks("curl"))
        self.assertFalse(self.tabs.opened("curl", 0.0))
        self.assertFalse(self.tabs.dropped("curl", 1.0))
        self.assertIsNone(self.tabs.due(1000.0))

    def test_the_default_window_is_five_seconds(self):
        # Stated, because it is a judgement call: a reload reconnects in a few
        # hundred milliseconds, and this is short enough that a user
        # watches the server stop rather than wondering whether it will.
        self.assertEqual(lifecycle.GRACE_SECONDS, 5.0)
        self.assertEqual(lifecycle.Lifecycle().grace, 5.0)
        self.assertEqual(lifecycle.STEP_PAGES, ("prep", "panel"))


# ---------------------------------------------------------------------------
# the same grace window, over a socket
# ---------------------------------------------------------------------------


class TabCloseTestCase(TwoTabTestCase):
    """A real server with a short grace window, and two clients on it."""

    def setUp(self):
        super().setUp()
        self.server.lifecycle.grace = TEST_GRACE

    def assert_stopped(self, why: str = ""):
        self.assertTrue(
            wait_until(lambda: server_module.active_server() is None),
            f"the server never stopped after {why or 'the close'}",
        )

    def assert_still_running(self, seconds: float):
        time.sleep(seconds)
        self.assertIs(server_module.active_server(), self.server)


class TabCloseStopsTheServerTest(TabCloseTestCase):
    """ONE OPEN TAB IS ENOUGH - closing the LAST one stops the session;
    closing one of two leaves it running.

    Step 2 is opened from step 1 when it is wanted, so a session with one tab
    open is a whole session and only running out of tabs entirely ends it.
    """

    def test_both_open_tabs_are_visible_to_the_server(self):
        self.open_tab("prep")
        self.open_tab("panel")
        self.assertTrue(wait_until(
            lambda: json.loads(self.get("state"))["pages"]
            == {"prep": True, "panel": True}
        ))

    def test_closing_one_of_two_open_tabs_leaves_the_session_running(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        self.assertTrue(wait_until(
            lambda: json.loads(self.get("state"))["pages"]["prep"]
        ))
        prep.close()
        self.assert_still_running(TEST_GRACE * 3)
        # Nobody was told anything: the session did not end, so there is no
        # closing event for the surviving tab to receive.
        self.assertIsNone(panel.wait_for(lambda m: m.get("closing"), 0.1))

    def test_closing_step_one_then_step_two_stops_it_on_the_second_close(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        prep.close()
        self.assert_still_running(TEST_GRACE * 3)
        panel.close()
        self.assert_stopped("the last tab closed")
        self.assertIn("panel", self.server.stop_reason)
        self.assertIn("last one open", self.server.stop_reason)

    def test_closing_step_two_then_step_one_stops_it_just_the_same(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        panel.close()
        self.assert_still_running(TEST_GRACE * 3)
        prep.close()
        self.assert_stopped("the last tab closed")
        self.assertIn("prep", self.server.stop_reason)
        self.assertIn("last one open", self.server.stop_reason)

    def test_a_close_is_noticed_at_once_and_not_at_the_next_heartbeat(self):
        # A dropped socket is END OF STREAM, and the stream loop looks for it
        # between waits. The alternative - finding out when the next heartbeat
        # write fails - is fifteen seconds of a user watching a tab they
        # closed, and this bound fails if anything regresses to it.
        panel = self.open_tab("panel")
        at = time.monotonic()
        panel.close()
        self.assert_stopped()
        self.assertLess(time.monotonic() - at,
                        server_module.HEARTBEAT_SECONDS / 2)

    def test_the_reason_is_journaled(self):
        panel = self.open_tab("panel")
        panel.close()
        self.assert_stopped()
        closed = self.records("server.tab_closed")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["data"]["page"], "panel")
        self.assertEqual(closed[0]["data"]["grace_seconds"], TEST_GRACE)
        self.assertIn("closed", closed[0]["data"]["reason"])

    def test_a_close_writes_nothing(self):
        # The unsaved-work guard is unchanged: the browser's own prompt at tab
        # close is the guard, and a close past it stops the server without
        # writing.
        before = self.board_path.read_bytes()
        prep = self.open_tab("prep")
        prep.close()
        self.assert_stopped()
        self.assertEqual(self.board_path.read_bytes(), before)

    def test_the_stop_button_tells_the_other_tab_too(self):
        # The button's own behaviour is unchanged - it answers, then stops -
        # but the tab that did not press it is no longer left holding a page
        # whose server has quietly gone.
        self.open_tab("prep")
        panel = self.open_tab("panel")
        status, reply = self.post("shutdown")
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        final = panel.wait_for(lambda message: message.get("closing"))
        self.assertIsNotNone(final)
        self.assertEqual(final["reason"], server_module.STOPPED_BY_PAGE)
        self.assert_stopped("the stop button")


class ReloadIsNotACloseTest(TabCloseTestCase):
    """The defect this whole mechanism exists to avoid."""

    def test_a_page_that_reconnects_inside_the_window_keeps_the_session(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        prep.close()
        # ...and comes straight back, the way a reloaded page does.
        self.open_tab("prep")
        self.assert_still_running(TEST_GRACE * 3)
        self.assertIsNone(panel.wait_for(lambda m: m.get("closing"), 0.1))

    def test_a_duplicate_tab_of_one_step_does_not_close_it(self):
        first = self.open_tab("prep")
        self.open_tab("prep")
        self.open_tab("panel")
        first.close()
        self.assert_still_running(TEST_GRACE * 3)

    def test_a_client_that_never_named_a_step_stops_nothing(self):
        # Whatever this is - a probe, a script, another tool - it is not one of
        # the user's tabs and it may not end the session by leaving.
        self.open_tab("prep")
        self.open_tab("panel")
        stray = EventClient(self.url, "stray")
        self.clients.append(stray)
        stray.close()
        self.assert_still_running(TEST_GRACE * 3)

    def test_a_page_that_never_opened_never_closes_the_session(self):
        # Step 2 was never opened in this session: step 1 alone is still a
        # session, right up until step 1 goes.
        prep = self.open_tab("prep")
        self.assert_still_running(TEST_GRACE * 2)
        prep.close()
        self.assert_stopped("the only open tab closed")


# ---------------------------------------------------------------------------
# step 1 opens at start; both addresses are still printed
# ---------------------------------------------------------------------------


class LaunchOpensBothTest(unittest.TestCase):
    """`--open` opens STEP 1 ONLY; the run still prints
    both addresses, because one only reachable by clicking a link is one
    nobody can hand to anyone.
    """

    URL = "http://127.0.0.1:52111/"

    def test_the_addresses_are_both_pages_in_step_order(self):
        addresses = preview_main.page_addresses(self.URL)
        self.assertEqual([step.page for step, _ in addresses],
                         ["prep", "panel"])
        self.assertEqual([where for _, where in addresses],
                         [self.URL, self.URL + "panel"])

    def test_open_asks_the_browser_for_step_one_only(self):
        asked = []
        opened = preview_main.open_pages(self.URL, opener=asked.append)
        self.assertEqual(asked, [self.URL])
        self.assertEqual(opened, asked)

    def test_a_url_without_its_trailing_slash_still_opens_step_one(self):
        addresses = preview_main.open_pages(
            "http://127.0.0.1:52111", opener=lambda where: None
        )
        self.assertEqual(addresses, [self.URL])


class LaunchOutputTest(TwoTabTestCase):
    """What the terminal says, read off the real thing rather than a format."""

    def test_the_running_server_answers_on_both_printed_addresses(self):
        for _, address in preview_main.page_addresses(self.url):
            with self.subTest(address=address):
                with urllib.request.urlopen(address, timeout=WAIT_SECONDS) as r:
                    self.assertEqual(r.status, 200)


# ---------------------------------------------------------------------------
# the layout toggle: gone from the served pages, kept on the frozen one
# ---------------------------------------------------------------------------


MINIMAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" '
    'viewBox="0 0 10 10"><path d="M0 0 L10 10"/></svg>'
)
# Everything the toggle is made of. A served page may carry none of it.
LAYOUT_TOKENS = ("id=layout", "side by side", "'stacked'", "setLayout",
                 "pb-stacked", "side by side / stacked")


def frozen_review_page() -> str:
    """A real written review page - two viewports, and a layout to arrange."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        svg = root / "edge.svg"
        svg.write_text(MINIMAL_SVG, encoding="utf-8")
        layers = [LayerSpec(ordinal=25, canonical_name="Edge.Cuts",
                            layer_type="user", display_name="Edge.Cuts")]
        out = review_page.build_page(
            out_path=root / "page.html",
            heading="Review - frozen",
            subtitle="two viewports",
            layers=layers,
            views=[
                review_page.BoardView(title="source",
                                      svg_by_layer={"Edge.Cuts": svg}),
                review_page.BoardView(title="panel",
                                      svg_by_layer={"Edge.Cuts": svg}),
            ],
            theme=theme.load_theme(),
        )
        return out.read_text(encoding="utf-8")


class LayoutToggleFenceTest(TwoTabTestCase):
    """No layout toggle on the served pages, one on the frozen page.

    Fenced BOTH ways on the bytes that are emitted.
    """

    def served_pages(self) -> dict:
        self.session.build_panel({})
        return {"prep": self.session.page(), "panel": self.session.panel_page()}

    def test_no_served_page_carries_the_toggle(self):
        for name, markup in self.served_pages().items():
            for token in LAYOUT_TOKENS:
                with self.subTest(page=name, token=token):
                    self.assertNotIn(token, markup)

    def test_no_served_page_offers_the_key_that_drove_it(self):
        for name, markup in self.served_pages().items():
            with self.subTest(page=name):
                self.assertNotIn("<kbd>L</kbd>", markup)
                self.assertIn("<kbd>H</kbd>", markup)

    def test_the_frozen_review_page_keeps_every_bit_of_it(self):
        markup = frozen_review_page()
        for token in LAYOUT_TOKENS:
            with self.subTest(token=token):
                self.assertIn(token, markup)
        self.assertIn("<kbd>L</kbd> side by side / stacked", markup)
        self.assertEqual(markup.count("class=card"), 2)

    def test_the_shared_script_holds_none_of_it(self):
        # The pan/zoom and the layer wiring are shared literally; the toggle is
        # not, because only one of the two hosts has a layout.
        for token in LAYOUT_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, review_page.JS)
                self.assertNotIn(token, page_served.CONTROLS_JS)
                self.assertNotIn(token, layerpanel.ONE_VIEW_PANEL.legend)
        self.assertIn("setLayout", review_page.LAYOUT_JS)
        self.assertIn("<kbd>L</kbd>", layerpanel.VIEWER_PANEL.legend)


# ---------------------------------------------------------------------------
# the step links, in markup and under node
# ---------------------------------------------------------------------------


class StepMarkupTest(TwoTabTestCase):
    """The names are on the page, because nothing else can put them there."""

    def pages(self) -> dict:
        return {"prep": self.get(""), "panel": self.get("panel")}

    def test_each_page_adopts_its_own_step_name(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn(f'"stepWindow": "pb-step-{name}"', markup)
                self.assertIn("window.name = PB.stepWindow", markup)

    def test_each_link_targets_the_other_steps_window_name(self):
        prep, panel = self.get(""), self.get("panel")
        self.assertIn('<a href="/panel" target="pb-step-panel"', prep)
        self.assertIn('data-step-window="pb-step-panel"', prep)
        self.assertIn('<a href="/" target="pb-step-prep"', panel)
        self.assertIn('data-step-window="pb-step-prep"', panel)

    def test_no_link_forces_a_fresh_window(self):
        for name, markup in self.pages().items():
            for link in re.findall(r"<a href=\"/(?:panel)?\"[^>]*>", markup):
                with self.subTest(page=name, link=link):
                    self.assertNotIn("_blank", link)
                    self.assertNotIn("noopener", link)

    def test_the_stream_says_which_step_is_holding_it(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn("'/events?page=' + encodeURIComponent", markup)
                self.assertIn(f'"page": "{name}"', markup)


REGISTRY_HARNESS = r"""
%(functions)s
let stopped = false;
const stepWindows = {};
const out = {opened: [], focused: 0, said: [], logged: [], asked: 0};
function say(text) { out.said.push(text); }
function logEvent(type, data) { out.logged.push(type); }

// THE BROWSER'S OWN RULE, stated: `window.open(url, name)` addressed to a name
// that is already open in this browsing context REUSES that window - it is
// re-aimed and raised, never duplicated. A name nothing holds makes one.
const REGISTRY = %(registry)s;
globalThis.window = {
  open(path, name) {
    const held = REGISTRY[name];
    if (held && !held.closed) {
      held.navigated = path;
      out.opened.push({name: name, reused: true});
      return held;
    }
    const made = {name: name, navigated: path, closed: false,
                  focus() { out.focused += 1; }};
    REGISTRY[name] = made;
    out.opened.push({name: name, reused: false});
    return made;
  },
};
globalThis.fetch = async () => ({
  json: async () => { out.asked += 1; return {pages: %(pages)s}; },
});
%(held)s
const step = {window: 'pb-step-panel', path: '/panel', page: 'panel',
              label: 'Step 2 - panelize'};
out.first = await jumpToStep(step);
out.second = await jumpToStep(step);
out.made = out.opened.filter(entry => !entry.reused).length;
out.windows = Object.keys(REGISTRY);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientStepJumpTest(unittest.TestCase):
    """`jumpToStep`, run: the link that focuses instead of spawning."""

    def jump(self, pages: dict, registry: str = "{}", held: str = "") -> dict:
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("stepIsOpen", "focusStep", "jumpToStep")
        )
        return run_js(REGISTRY_HARNESS % {
            "functions": functions, "registry": registry,
            "pages": json.dumps(pages), "held": held,
        })

    def test_the_first_click_opens_the_step_with_its_name(self):
        out = self.jump({"prep": True, "panel": False})
        self.assertEqual(out["first"], "opened")
        self.assertEqual(out["windows"], ["pb-step-panel"])
        self.assertEqual(out["made"], 1)

    def test_the_second_click_focuses_the_tab_the_first_one_opened(self):
        out = self.jump({"prep": True, "panel": False})
        self.assertEqual(out["second"], "focused")
        # THE WHOLE POINT: two clicks, one tab.
        self.assertEqual(out["made"], 1)
        self.assertEqual(out["focused"], 1)

    def test_a_step_the_server_says_is_open_is_never_opened_again(self):
        # The honest case. A tab the operating system's browser opened is in
        # its own browsing context, so this page cannot reach its name and
        # cannot raise it - but it CAN refuse to make a third one, and say
        # where the second is.
        out = self.jump({"prep": True, "panel": True})
        self.assertEqual(out["first"], "already-open")
        self.assertEqual(out["made"], 0)
        self.assertEqual(out["windows"], [])
        self.assertTrue(out["said"])
        self.assertIn("already open", out["said"][0])
        self.assertIn("step.already_open", out["logged"])

    def test_a_named_window_this_page_can_reach_is_reused_not_asked_about(self):
        # The browser CAN find the name: the server is never consulted, and no
        # window is made.
        out = self.jump(
            {"prep": True, "panel": True},
            registry="{'pb-step-panel': {name: 'pb-step-panel', closed: false,"
                     " focus() { out.focused += 1; }}}",
            held="stepWindows['pb-step-panel'] = REGISTRY['pb-step-panel'];",
        )
        self.assertEqual(out["first"], "focused")
        self.assertEqual(out["made"], 0)
        self.assertEqual(out["asked"], 0)
        self.assertEqual(out["focused"], 2)

    def test_a_handle_to_a_closed_tab_is_not_a_tab(self):
        out = self.jump(
            {"prep": True, "panel": False},
            held="stepWindows['pb-step-panel'] = {closed: true};",
        )
        self.assertEqual(out["first"], "opened")
        self.assertEqual(out["made"], 1)

    def test_a_stopped_page_jumps_nowhere(self):
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("stepIsOpen", "focusStep", "jumpToStep")
        )
        out = run_js(
            functions
            + "\nlet stopped = true;\nconst stepWindows = {};\n"
            + "const out = {opened: 0};\n"
            + "function say() {}\nfunction logEvent() {}\n"
            + "globalThis.window = {open() { out.opened += 1; return null; }};\n"
            + "out.answer = await jumpToStep({window: 'pb-step-panel',"
            + " path: '/panel', page: 'panel', label: 'Step 2'});\n"
            + "console.log(JSON.stringify(out));"
        )
        self.assertEqual(out["answer"], "")
        self.assertEqual(out["opened"], 0)


CLOSING_HARNESS = r"""
%(functions)s
let stopped = false;
let eventStream = {closed: false, close() { this.closed = true; }};
let pollTimer = 11;
let flushTimer = 22;
// No grace timer pending when the close happens - `streamAlive` (called from
// `goQuiet`) is then a no-op, exactly as it is on a page that was not mid-way
// through waiting out one of its own stream's gaps.
let streamGraceTimer = null;
// The notice flag is declared once, not shadowed: `deadPage` reads and sets
// this module-level flag, so it is here rather than inside any one function.
let dead = false;
const out = {logged: [], flushed: 0, closes: 0, cleared: []};
function logEvent(type, data) { out.logged.push([type, data]); }
function flushEvents(keepalive) { out.flushed += 1; out.keepalive = keepalive; }
globalThis.clearInterval = () => out.cleared.push('interval');
globalThis.clearTimeout = () => out.cleared.push('timeout');
globalThis.setTimeout = fn => { out.deferred = fn; return 0; };
const PB = {session: 'C:/scratch/20260909_ui_session'};
function element(tag) {
  return {tag: tag, children: [], textContent: '', className: '',
          append(...kids) { this.children.push(...kids); },
          appendChild(kid) { this.children.push(kid); }};
}
const body = {
  children: ['titlebar', 'viewport', 'controls', 'stop button'],
  _text: '',
  get textContent() { return this._text; },
  set textContent(value) {
    this._text = value;
    if (value === '') this.children = [];
  },
  appendChild(kid) { this.children.push(kid); },
};
globalThis.document = {title: 'Preview - board', body: body,
                       createElement: element};
globalThis.window = {close() { out.closes += 1; }};
serverClosing(%(reason)s);
out.stopped = stopped;
out.stream = eventStream;
out.left = body.children.map(kid => typeof kid === 'string' ? kid : kid.tag);
out.title = document.title;
out.text = JSON.stringify(body.children);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientClosingTest(unittest.TestCase):
    """`serverClosing`, run: it goes quiet and draws the dead page.

    No auto-close: the surviving tab is not told to close itself - it shows
    the dead-page notice and leaves the tab for the user to deal with.
    `window.close()` stays on the QUIT BUTTON's own path (:func:`stopServer`),
    tested where that button is.
    """

    def closing(self, reason="'the prep tab was closed'") -> dict:
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("goQuiet", "streamAlive", "deadPage", "serverClosing")
        )
        return run_js(CLOSING_HARNESS % {"functions": functions,
                                         "reason": reason})

    def test_it_never_tries_to_close_the_tab_itself(self):
        out = self.closing()
        self.assertEqual(out["closes"], 0)

    def test_it_stops_talking_to_the_server_first(self):
        out = self.closing()
        self.assertTrue(out["stopped"])
        self.assertIsNone(out["stream"])
        self.assertEqual(sorted(out["cleared"]), ["interval", "timeout"])

    def test_the_reason_is_recorded_and_flushed_before_the_page_goes(self):
        out = self.closing()
        self.assertEqual(out["logged"][0][0], "server.closing")
        self.assertEqual(out["logged"][0][1]["reason"],
                         "the prep tab was closed")
        self.assertEqual(out["flushed"], 1)
        self.assertTrue(out["keepalive"])

    def test_the_dead_page_leaves_every_control_gone(self):
        out = self.closing()
        self.assertEqual(out["left"], ["div"])
        self.assertEqual(out["title"], "preview server stopped")
        self.assertIn("the preview server has stopped", out["text"])
        self.assertIn("the prep tab was closed", out["text"])

    def test_a_closing_event_with_no_reason_still_ends_the_page(self):
        out = self.closing(reason="''")
        self.assertEqual(out["closes"], 0)
        self.assertIn("Nothing on this page is live", out["text"])


@unittest.skipUnless(shutil.which("node"), "no node to parse the page's script")
class StepScriptParsesTest(unittest.TestCase):
    """Everything added here is inside the shared script and still parses."""

    def test_the_shared_script_parses(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.mjs"
            path.write_text(
                page_served.page_script(page_served.PREP_JS, {"page": "prep"}),
                encoding="utf-8",
            )
            done = subprocess.run(  # noqa: S603 - a fixed node run, no shell
                [shutil.which("node"), "--check", str(path)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
