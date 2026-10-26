"""Two tabs at once, and the settings that are linked between them.

Three behaviours of the two-tab preview, all asserted here against a REAL
server on 127.0.0.1 rather than against the shape of the code.

**A caption click reaches the endpoint.** The label handler was exercised under
node against a stated DOM and passed while doing nothing whatever in a real
browser, because the DOM it was stated against left out pointer
capture (see `test_panel_page.ClientLabelClickTest`). A string assertion could
not have caught that and neither could a stub: so the page's OWN `post` is
lifted out, given node's real `fetch`, pointed at a real preview server, and
driven by a synthetic press and release on a caption. What is asserted is what
the SESSION then holds - a POST-level effect, from a synthesized gesture.

**Both pages run at once.** Step 2 opens in a separate browser tab, so two
pages hold two event streams over one session. Both are
pushed to, layer state set on either one converges on the other, and the
journal says which page did what.

**Linked settings are a backend, driven server-side.**
`PreviewSession.set_layer_visibility` is the API; `/layers` is one caller of
it. A value changed with no browser involved reaches every connected page
through the same push, last change wins, and the state that arrives is the
state a page applies - asserted by feeding the server's own message to the
page's own `applyLayerState` under node.
"""

import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from eedesignpanels.config import load_config
from eedesignpanels.preview import (
    journal as journal_module,
    page_served,
    server as server_module,
)
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages.route_toolpath import SCORE_LAYER
from test_panel_page import LABEL_DOM, write_panel_project
from test_preview import stub_render
from test_preview_journal import js_function, run_js

# Every wait in this file is bounded. A preview test that spins on a condition
# it never reaches takes the suite down with it.
WAIT_SECONDS = 20.0


class EventClient:
    """One page's event stream, read on its own thread.

    A subscriber COALESCES - a listener that has not read yet keeps only the
    newest message - so what is asserted is always the state that ARRIVED in
    the end, never an exact sequence of intermediates.
    """

    def __init__(self, url: str, name: str, page: str = ""):
        self.name = name
        self.page = page
        self.messages = []
        self.error = None
        # A REAL page says which step it is - that is what lets the server
        # tell a reload from a close. An empty *page* is a client that is not
        # one of the user's tabs and is never tracked.
        where = f"events?page={page}" if page else "events"
        self._stream = urllib.request.urlopen(url + where, timeout=WAIT_SECONDS)
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name=f"events-{name}")
        self._thread.start()

    def _read(self) -> None:
        try:
            for raw in self._stream:
                line = raw.decode("utf-8").strip()
                if line.startswith("data:"):
                    self.messages.append(json.loads(line[5:]))
        except (OSError, ValueError) as exc:
            # The stream was closed under us, which is how one of these ends.
            self.error = exc

    def wait_for(self, predicate, timeout: float = WAIT_SECONDS):
        """The first message this client holds that satisfies *predicate*."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.messages):
                if predicate(message):
                    return message
            time.sleep(0.02)
        return None

    def close(self) -> None:
        """End the stream the way a CLOSED TAB ends it: the socket goes.

        `urlopen(...).close()` alone is not that. With a reader thread parked in
        a read on the same file object, the underlying socket stays open long
        enough that the server only finds out at its next heartbeat - fifteen
        seconds of a test waiting for something a browser does instantly.
        Shutting the socket down first sends the FIN a closing tab sends, which
        is what the server's own end-of-stream check is looking for.
        """
        raw = getattr(getattr(self._stream, "fp", None), "raw", None)
        under = getattr(raw, "_sock", None)
        if under is not None:
            try:
                under.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._stream.close()
        self._thread.join(timeout=5.0)


class TwoTabTestCase(unittest.TestCase):
    """A recorded session, a real server, and two pages holding two streams."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.recording = journal_module.open_session(
            self.config_path, self.folder / "scratch",
            stamp="20260909_120000", backend="test",
        )
        self.session = PreviewSession(
            self.config_path, self.recording.folder, render=stub_render,
            journal=self.recording,
        )
        self.session.refresh()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()
        self.clients = []

    def tearDown(self):
        # THE SERVER FIRST. Stopping it wakes every listener and ends every
        # stream, so a reader thread comes back on its own; closing the socket
        # under a thread blocked in a read leaves it there until the next
        # heartbeat, which is fifteen seconds per client of nothing at all.
        self.server.stop()
        for client in self.clients:
            client.close()
        self._folder.cleanup()

    def open_tab(self, name: str) -> EventClient:
        client = EventClient(self.url, name, page=name)
        self.clients.append(client)
        # A page fetches its own document too, which is what the journal
        # attributes and what proves the route answers while the other tab is
        # open.
        self.get("" if name == "prep" else "panel")
        return client

    def get(self, path: str) -> str:
        with urllib.request.urlopen(self.url + path,
                                    timeout=WAIT_SECONDS) as reply:
            return reply.read().decode("utf-8")

    def post(self, path: str, payload: dict = None):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=WAIT_SECONDS) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def records(self, kind: str = "") -> list:
        return [r for r in self.recording.records()
                if not kind or r["kind"] == kind]


# ---------------------------------------------------------------------------
# two tabs, one session
# ---------------------------------------------------------------------------


class TwoTabsTest(TwoTabTestCase):
    """Step 1 and step 2 open at the same time, over one server."""

    def test_both_pages_are_served_while_both_are_open(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        self.assertIn("pb-candidates", self.get(""))
        self.assertIn("pb-build", self.get("panel"))
        self.assertEqual(len(self.session._subscribers), 2)
        self.assertIsNone(prep.error)
        self.assertIsNone(panel.error)

    def test_one_change_is_pushed_to_every_open_page(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        before = self.session.generation
        self.session.refresh()
        after = self.session.generation
        self.assertGreater(after, before)
        for client in (prep, panel):
            with self.subTest(page=client.name):
                seen = client.wait_for(
                    lambda message: message.get("generation") == after
                )
                self.assertIsNotNone(
                    seen, f"{client.name} never saw generation {after}"
                )

    def test_an_action_on_one_page_reaches_the_other(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        status, reply = self.post("build_panel")
        self.assertEqual(status, 200)
        for client in (prep, panel):
            with self.subTest(page=client.name):
                self.assertIsNotNone(client.wait_for(
                    lambda message: (message.get("generation") or 0)
                    >= reply["generation"]
                ))

    def test_the_journal_says_which_page_did_what(self):
        self.open_tab("prep")
        self.open_tab("panel")
        for page in ("prep", "panel"):
            status, _ = self.post("journal", {"events": [
                {"type": "control", "id": "pb-fit", "page": page},
            ]})
            self.assertEqual(status, 200)
        pages = [r["data"].get("page") for r in self.records("client.control")]
        self.assertEqual(pages, ["prep", "panel"])
        # ...and both documents were served, each recorded as its own GET.
        served = [r["data"]["endpoint"] for r in self.records("server.get")]
        self.assertIn("/", served)
        self.assertIn("/panel", served)


# ---------------------------------------------------------------------------
# the linked-settings backend
# ---------------------------------------------------------------------------


class LayerBackendTest(TwoTabTestCase):
    """`set_layer_visibility`: the API, driven server-side, pushed to all."""

    LAYERS = ["Edge.Cuts", "User.8", SCORE_LAYER]

    def test_a_server_side_change_reaches_every_open_page(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        reply = self.session.set_layer_visibility(
            visible=["Edge.Cuts"], known=self.LAYERS, active="User.8", mode=1,
        )
        self.assertTrue(reply["ok"])
        for client in (prep, panel):
            with self.subTest(page=client.name):
                seen = client.wait_for(
                    lambda message:
                    message.get("layers_seq") == reply["layers_seq"]
                )
                self.assertIsNotNone(seen, f"{client.name} was not told")
                # ...and what arrived is what a page needs to apply it.
                self.assertEqual(seen["layers"]["visible"], ["Edge.Cuts"])
                self.assertEqual(seen["layers"]["known"], self.LAYERS)
                self.assertEqual(seen["layers"]["active"], "User.8")
                self.assertEqual(seen["layers"]["mode"], 1)
                self.assertEqual(seen["layers"]["page"], "server")

    def test_a_layer_change_never_moves_the_generation(self):
        # Changing what is VISIBLE is not changing the board: nothing
        # recomputes and no picture is redrawn from the server.
        before = self.session.generation
        self.session.set_layer_visibility(visible=["Edge.Cuts"])
        self.assertEqual(self.session.generation, before)
        self.assertEqual(self.session.layers_seq, 1)

    def test_an_omitted_argument_keeps_what_the_session_holds(self):
        self.session.set_layer_visibility(
            visible=["Edge.Cuts", "User.8"], known=self.LAYERS,
            active="Edge.Cuts", mode=0,
        )
        self.session.set_layer_visibility(mode=2)
        held = self.session.layers
        self.assertEqual(held["mode"], 2)
        self.assertEqual(held["visible"], ["Edge.Cuts", "User.8"])
        self.assertEqual(held["known"], self.LAYERS)
        self.assertEqual(held["active"], "Edge.Cuts")

    def test_the_route_funnels_into_the_same_setter(self):
        status, reply = self.post("layers", {
            "page": "prep", "mode": 1, "active": "User.8",
            "known": self.LAYERS, "visible": ["Edge.Cuts", "User.8"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.session.layers["page"], "prep")
        self.assertEqual(self.session.layers_seq, reply["layers_seq"])

    def test_a_page_change_is_journaled_once_and_a_server_change_is_too(self):
        # One change, one record. A page writes its own line (the `/layers`
        # route is deliberately unjournaled), and a server-side change has no
        # page to speak for it, so the setter writes that one.
        self.post("layers", {"page": "prep", "visible": ["Edge.Cuts"],
                             "known": self.LAYERS, "mode": 0})
        self.assertEqual(self.records("layers.set"), [])
        self.session.set_layer_visibility(visible=[], mode=2)
        written = self.records("layers.set")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["data"]["page"], "server")
        self.assertEqual(written[0]["data"]["mode"], 2)

    def test_page_a_sets_it_and_page_b_converges(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        status, reply = self.post("layers", {
            "page": "prep", "mode": 2, "active": SCORE_LAYER,
            "known": self.LAYERS, "visible": ["Edge.Cuts"],
        })
        self.assertEqual(status, 200)
        seen = panel.wait_for(
            lambda message: message.get("layers_seq") == reply["layers_seq"]
        )
        self.assertIsNotNone(seen, "the other tab was never told")
        self.assertEqual(seen["layers"]["visible"], ["Edge.Cuts"])
        # The page that SENT it is told too - the server does not exempt it,
        # and the page's own change detector is what stops the echo (see
        # `applyLayerState`).
        self.assertIsNotNone(prep.wait_for(
            lambda message: message.get("layers_seq") == reply["layers_seq"]
        ))

    def test_last_change_wins_under_interleaving(self):
        prep, panel = self.open_tab("prep"), self.open_tab("panel")
        self.post("layers", {"page": "prep", "visible": ["Edge.Cuts"],
                             "known": self.LAYERS, "mode": 0})
        self.session.set_layer_visibility(visible=self.LAYERS, mode=1)
        self.post("layers", {"page": "panel", "visible": ["User.8"],
                             "known": self.LAYERS, "mode": 2,
                             "active": "User.8"})
        last = self.session.set_layer_visibility(
            visible=[SCORE_LAYER], known=self.LAYERS, active=SCORE_LAYER,
            mode=0,
        )
        self.assertEqual(self.session.layers_seq, 4)
        self.assertEqual(last["layers_seq"], 4)
        self.assertEqual(self.session.layers["visible"], [SCORE_LAYER])
        # Both pages end on the LAST state, whichever intermediates they
        # coalesced away.
        for client in (prep, panel):
            with self.subTest(page=client.name):
                seen = client.wait_for(
                    lambda message: message.get("layers_seq") == 4
                )
                self.assertIsNotNone(seen)
                self.assertEqual(seen["layers"]["visible"], [SCORE_LAYER])

    def test_the_sequence_number_only_ever_goes_up(self):
        seen = [self.session.set_layer_visibility(mode=n % 3)["layers_seq"]
                for n in range(5)]
        self.assertEqual(seen, [1, 2, 3, 4, 5])

    def test_a_state_worth_refusing_is_refused_by_name(self):
        for bad, says in (
            ({"visible": "Edge.Cuts"}, "lists of canonical layer names"),
            ({"mode": 7}, "0, 1 or 2"),
            ({"mode": True}, "0, 1 or 2"),
            ({"visible": ["x"] * 200}, "more layers than any board has"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception) as raised:
                    self.session.set_layer_visibility(**bad)
                self.assertIn(says, str(raised.exception))
        # ...and nothing was recorded and nothing pushed.
        self.assertEqual(self.session.layers_seq, 0)

    def test_the_cheap_poll_carries_it_too(self):
        # A page that missed a push catches up without one.
        self.session.set_layer_visibility(visible=["Edge.Cuts"],
                                          known=self.LAYERS, mode=1)
        state = json.loads(self.get("state"))
        self.assertEqual(state["layers_seq"], 1)
        self.assertEqual(state["layers"]["visible"], ["Edge.Cuts"])


APPLY_HARNESS = """
%(functions)s
let stopped = false, lastLayers = '';
const PB = {page: 'panel'};
const LAYERS = %(layers)s;
const sent = [];
function send(path, body) { sent.push({path: path, body: body}); }
function layerState() { return {mode: viewer.mode, active: 'x', hidden: []}; }
const viewer = {
  active: 0, mode: 0, visible: new Set(LAYERS.map(l => l.canonical)),
  painted: 0, paint() { this.painted += 1; },
};
const out = {};
// The message the SERVER pushed, applied by the page's own function.
out.applied = applyLayerState(%(incoming)s);
out.visible = LAYERS.map(l => l.canonical).filter(n => viewer.visible.has(n));
out.active = LAYERS[viewer.active].canonical;
out.mode = viewer.mode;
out.painted = viewer.painted;
out.sent = sent;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ServerStateIsAppliedTest(TwoTabTestCase):
    """The message a SERVER-SIDE change pushes, applied by the page's own code.

    "Received" is not "applied", and the two live in different languages. So
    the message this session actually pushed is taken off a live event stream
    and fed to the page's own `applyLayerState` under node.
    """

    LAYERS = [{"canonical": "Edge.Cuts"}, {"canonical": "User.8"},
              {"canonical": SCORE_LAYER}]

    def apply(self, incoming: dict) -> dict:
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("panelLayerState", "sameLayerState", "applyLayerState",
                         "pushLayers")
        )
        return run_js(APPLY_HARNESS % {
            "functions": functions,
            "layers": json.dumps(self.LAYERS),
            "incoming": json.dumps(incoming),
        })

    def test_a_page_applies_exactly_what_the_server_pushed(self):
        panel = self.open_tab("panel")
        reply = self.session.set_layer_visibility(
            visible=["Edge.Cuts"], known=[row["canonical"] for row in self.LAYERS],
            active="User.8", mode=1,
        )
        pushed = panel.wait_for(
            lambda message: message.get("layers_seq") == reply["layers_seq"]
        )
        self.assertIsNotNone(pushed)
        out = self.apply(pushed["layers"])
        self.assertTrue(out["applied"])
        self.assertEqual(out["visible"], ["Edge.Cuts"])
        self.assertEqual(out["active"], "User.8")
        self.assertEqual(out["mode"], 1)
        self.assertEqual(out["painted"], 1)
        # Applying a change never sends it back round.
        self.assertEqual(out["sent"], [])


# ---------------------------------------------------------------------------
# a synthesized click, through the page's own code, into the server
# ---------------------------------------------------------------------------


CLICK_TO_SERVER = """
%(dom)s
%(functions)s
const ORIGIN = %(origin)s;
let busy = false, stopped = false, waiting = 0;
const PB = {generation: %(generation)d, proposals: %(proposals)s};
const out = {fetched: [], said: [], refreshed: 0};

// Every stub here is a display concern. `post` and `fetch` are REAL: the
// defect was in the wiring between a gesture and a request, so the request has
// to be a request.
function say(text, kind) { out.said.push([text, kind || '']); }
function setBusy() {}
function drawPending(count) { out.pending = count; }
function logEvent(type, data) { }
async function refresh() { out.refreshed += 1; }

const realFetch = globalThis.fetch;
globalThis.fetch = (path, init) => {
  out.fetched.push(String(path));
  return realFetch(ORIGIN + String(path).replace(/^\\//, ''), init);
};

const views = node('views', null);
views.classes.add('views');
const viewport = node('viewport', views);
viewport.classes.add('viewport');
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
  return box;
}
wireLabels(views);

// A press and a release on candidate #%(number)d, at one point.
gesture(caption(%(number)d), [420, 260], [420, 260]);

// The request is in flight; wait for the SESSION to have taken it, and never
// longer than this.
async function settled(ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    const state = await (await realFetch(ORIGIN + 'state')).json();
    if (state.pending > 0) return state;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return null;
}
out.state = await settled(%(timeout)d);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class LabelClickReachesTheServerTest(TwoTabTestCase):
    """The whole click path, from a synthetic press to what the session holds.

    Node alone cannot answer this. `post` was never reached, so a harness that
    stubs `post` asserts the half of the path that was working; what had to be
    proved is that a press and a release on a caption end as a POST the server
    acts on.
    """

    def proposable(self) -> dict:
        for row in page_served.proposals_json(self.session.view):
            if row["proposable"] and not row["accepted"]:
                return row
        raise AssertionError("the fixture has no proposable candidate")

    def test_a_synthesized_press_and_release_accepts_the_candidate(self):
        self.open_tab("prep")
        row = self.proposable()
        before = len(self.session.pending)
        functions = "\n".join(
            js_function(page_served.CONTROLS_JS, name)
            for name in ("labelAction", "proposalNumbered", "clickLabel",
                         "hitUnder", "travelPx", "pressLabel", "releaseLabel",
                         "wireLabels", "post")
        )
        # What the page reads off its own `<meta name=pb-token>`: the headers
        # every POST carries, with this server's session token.
        headers = json.dumps({"Content-Type": "application/json",
                              server_module.TOKEN_HEADER: self.server.token})
        out = run_js(
            f"const PB_POST_HEADERS = {headers};\n"
            "const CLICK_SLOP_PX = 4;\nlet pressAt = null;\n"
            "let clickTaken = false;\n"
            + CLICK_TO_SERVER % {
                "dom": LABEL_DOM,
                "functions": functions,
                "origin": json.dumps(self.url),
                "generation": self.session.generation,
                "proposals": json.dumps(
                    page_served.proposals_json(self.session.view)
                ),
                "number": row["number"],
                "timeout": int(WAIT_SECONDS * 1000),
            }
        )
        # THE ENDPOINT WAS CALLED, with the candidate's own blade line.
        self.assertIn("/accept", out["fetched"])
        # ...and the server acted on it: the session holds the decision, in
        # memory, and the score line is on the held board.
        self.assertIsNotNone(out["state"], "the session never took the click")
        self.assertEqual(len(self.session.pending), before + 1)
        self.assertEqual(self.session.pending[-1].kind, "accept")
        self.assertTrue(
            [item for item in self.session.board.drawings
             if getattr(item, "layer", "") == SCORE_LAYER],
            "no score line was written to the held board",
        )
        # ...and nothing was written to disk: an accept is an in-memory edit.
        self.assertEqual(out["state"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
