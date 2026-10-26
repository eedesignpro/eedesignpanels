"""The preview server: a localhost HTTP host for one session, stdlib only.

Nothing here is a web framework and nothing here is a dependency. It is
`http.server` with a fixed route table, because the whole surface is one page
and six actions, and because a runtime dependency has to reach every engineer's
KiCad interpreter - which for a development-time preview would
be a large bill for a small convenience.

**127.0.0.1 only, and it means it.** The socket binds the loopback address, so
nothing off this machine can reach it; and every request's `Host` header is
checked against the address it was served on, which is what stops a page on
another site resolving a hostname to 127.0.0.1 and driving this server through
the browser that trusts it. Both, because either one alone has a hole.

**Only its own page may drive it.** Every POST must carry the session token
in the `X-Eedesignpanels-Token` header, and every request that names an
`Origin` must name this server's own. The token is made fresh for each server
and written into the page it serves (:meth:`PreviewHandler._page`). A page on
another site cannot read that page, so it cannot learn the token, and a
browser sends a custom header across sites only after a permission check this
server never answers. A request with no `Origin` is not from a browser page
(the Fetch standard makes a browser name it on every POST), so the token alone
decides it.

**One server per process.** A second one would be a second session writing to
the same board, so starting one while another is running is refused by name
rather than by a port collision.

**One server, two pages.** Step 1 prepares the board and
step 2 panelizes it; they are two routes over ONE session, so the panel is
derived from the board the prep page is editing rather than from a file, and
the layer panel is the same layer panel on both.

Routes:

=================  ======  ===================================================
`/`                GET     step 1: the board preparation page
`/panel`           GET     step 2: the panel page
`/state`           GET     `{generation, ok, error, ...}` - the cheap poll
`/fragment`        GET     what step 1 needs to update itself in place
`/panel/fragment`  GET     what step 2 needs to update itself in place
`/check/report`    GET     the last comparison report, as it was written
`/bug_report/download` GET the problem report zip this session built last
`/events?page=`    GET     server-sent events; one per change, and presence
`/accept`          POST    add the score line accepting a blade line, IN MEMORY
`/decline`         POST    take the score lines off a blade line, IN MEMORY
`/set_param`       POST    put a declared number into the held config
`/import_settings` POST    pre-populate the held config from a posted file
`/import_placement` POST   place every copy from a posted placement CSV
`/save`            POST    THE write: the board, and the config when it changed
`/build_panel`     POST    derive the panel IN MEMORY from what is held
`/save_panel`      POST    write the panel folder: board, project, config
`/check`           POST    compare the SAVED panel against its source
`/reload`          POST    re-read the board and the config from disk
`/layers`          POST    one page's layer panel state, for the other page
`/journal`         POST    record a batch of client interaction events
`/note`            POST    keep one line of the user's own words
`/snapshot`        POST    save the current view and the whole backend state
`/bug_report`      POST    collect the whole run into one zip for a developer
`/shutdown`        POST    stop the server
=================  ======  ===================================================

Every POST that CHANGES something recomputes and pushes before it answers, so
the reply and the page can never disagree about what the board now says. The
three instrumentation routes deliberately change nothing: watching the user
must not be able to move what the user is looking at.

**Only `/save` touches a file.** Accept, decline and set_param edit the session's
held board and config data; `/save` is the write, and it is one action for both
files. A session that is stopped without it changed nothing
on disk, which is what makes "discard and stop" an honest answer.

**One changing action at a time, and a duplicate is refused rather than
queued.** The five routes in `CHANGING` run under the session's action gate,
taken without blocking; a second one arriving while the first is still writing
gets `409` and a sentence, and the journal records it as a rejection. Waiting
on the lock and then applying would turn three impatient clicks into three
writes and a page that jumps twice. `/shutdown`
and the instrumentation routes are never gated, because stopping and recording
must work while something is running.

**The server stops when the LAST tab closes.** Each page holds an event stream
and names the step it is (`/events?page=panel`); dropping it starts a grace
window, and the session
ends only when no page is holding a stream and that window has run out. Closing
step 2 while step 1 is open therefore stops nothing. The server then pushes a
final `closing` event - a page that hears it replaces its document with the
stopped notice, always, rather than closing itself - and tears down, journaling
which tab went last. A reload drops the stream too, which is exactly what the
grace window is for; see :mod:`eedesignpanels.preview.lifecycle`.

**A browser hanging up is not an error.** A tab that goes away mid-request
leaves this server writing into a socket nobody owns, and Windows answers that
with `ConnectionAbortedError` or `ConnectionResetError`. That is an ordinary
disconnect: it is journaled as `server.client_gone` and it never reaches the
user's terminal as a traceback (`_send`, `_events`,
`PreviewServer.handle_error`). Nothing broader is caught, so a real fault still
surfaces with its traceback.

**Every action is journaled** when the session has a folder - endpoint,
payload, outcome and generation, one line each. Two GETs are exempt and it is
worth saying why: `/state` is a poll, not an action, and `/favicon.ico` is the
browser talking to itself. Recording either one would bury the events that
mean something under thousands that do not.
"""

import gzip
import hmac
import json
import re
import secrets
import select
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eedesignpanels.preview import (
    actions,
    bugreport,
    lifecycle,
    session as session_module,
)

_UNSAFE_FILENAME = re.compile(r'[^A-Za-z0-9._-]+')

# The header every POST from the served page carries the session token in.
TOKEN_HEADER = "X-Eedesignpanels-Token"  # noqa: S105 - a header name
# Where the page reads the token from: one tag after the character set.
TOKEN_META_ANCHOR = "<meta charset=utf-8>"  # noqa: S105 - page markup
# The loopback names a page of this server can be served under.
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def _quoted_filename(name: str) -> str:
    """A file name safe to put inside a `Content-Disposition` header.

    Rebuilt from a whitelist rather than escaped: this name is ours, and a
    header value that could carry a quote or a newline is a header value that
    could carry a second header.
    """
    return _UNSAFE_FILENAME.sub("_", str(name))[:120] or "bug_report.zip"

# The largest paint number a page may claim to hold. A session counts one per
# batch of layer documents, so a million is more edits than any session has;
# past that the number is not a page talking.
MAX_PAINT_SEQ = 1_000_000

# When a body is worth compressing, and how hard. See `PreviewHandler._send`:
# a fragment is megabytes and gzips to a fifth of that in 7 ms; a poll answer
# is a few hundred bytes and would only get bigger.
GZIP_THRESHOLD_BYTES = 64 * 1024
GZIP_LEVEL = 1

# How long a request body may be. A config number and two points; anything
# larger is not one of ours.
MAX_BODY_BYTES = 64 * 1024
# ...except a snapshot, which carries the page's serialization of every visible
# layer in every viewport. That is genuinely large and it is ours.
MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024
# ...and a batch of interaction events, which is small but not two points.
MAX_JOURNAL_BYTES = 1024 * 1024
# ...and a posted settings file, which is a whole `panel.json` in a JSON string.
# The corpus config is under a kilobyte; this is room for a hand-written one
# with every layer named, and a refusal for anything that is not a config at
# all.
MAX_IMPORT_BYTES = 256 * 1024
# ...and a posted PLACEMENT file, which is three numbers a line and one line per
# copy. `placementfile.MAX_ROWS` bounds the copies at 512, so this is room for
# a generously commented file of them and a refusal for anything that is not
# one.
MAX_PLACEMENT_BYTES = 128 * 1024
# How much of a body this handler has refused it will still read, so that the
# refusal reaches the page instead of a reset socket. One snapshot's worth: past
# that the connection goes, which is the honest end for a body nobody will read.
MAX_DRAIN_BYTES = MAX_SNAPSHOT_BYTES
DRAIN_CHUNK_BYTES = 64 * 1024
BODY_LIMITS = {
    "/snapshot": MAX_SNAPSHOT_BYTES,
    # A problem report posts the same serialization a snapshot does, because it
    # takes the shot before it builds the archive around it.
    "/bug_report": MAX_SNAPSHOT_BYTES,
    "/journal": MAX_JOURNAL_BYTES,
    "/import_settings": MAX_IMPORT_BYTES,
    "/import_placement": MAX_PLACEMENT_BYTES,
}
# A poll is not an action. See the module docstring.
#
# `/layers` is here for a different reason and it is worth stating: a layer
# change IS journaled, once, by the page whose hand was on it. A second record
# from the server would be the same change twice, and a third from the page
# that received it would be three.
UNJOURNALED = ("/state", "/favicon.ico", "/layers")
# The GETs that are journaled AFTER they are served rather than before, because
# what is worth knowing about them is what they cost and how big they were: the
# fragment is the client's whole first paint, so it is a large part of the
# interaction latency. One record each either way -
# the kind is the same `server.get`, so nothing that reads a journal has to know
# which door a GET came through.
TIMED_GETS = ("/fragment", "/panel/fragment")
# The routes that CHANGE something, and therefore the ones that go through the
# session's action gate one at a time. Everything else - the instrumentation
# three, the layer sync and the shutdown - stays reachable while an action runs.
#
# `/build_panel`, `/save_panel` and `/check` are gated for the same reason the
# rest are: they are seconds long, the page blocks its controls for exactly that
# window, and a second click must be refused rather than queued behind the
# first. `/layers` is NOT gated - switching a layer off while a panel builds is
# a reasonable thing to do and costs the server nothing.
CHANGING = ("/accept", "/decline", "/set_param", "/import_settings",
            "/generate_edge_plating", "/remove_edge_plating",
            "/import_placement", "/save",
            "/reload", "/build_panel", "/save_panel", "/check", "/check_fingerprints")
# How often an idle event stream says something, so a shutdown is never waiting
# on a socket nobody is reading.
HEARTBEAT_SECONDS = 15.0
# How long the stream loop waits between looks at its own socket. A closed tab
# drops the connection immediately, and this is how quickly that is noticed -
# waiting for the next heartbeat instead would put fifteen seconds between the
# user closing a tab and anything happening. Four wakeups a second per page
# costs nothing measurable.
EVENT_POLL_SECONDS = 0.25
# How often the watchdog asks whether a grace window has run out.
WATCH_SECONDS = 0.25
# How long the final "closing" event is given to reach the other page before
# the socket under it goes away. A local write and a browser on the same
# machine; this is generous.
CLOSING_SECONDS = 0.25
# What the journal and the terminal say when the page pressed the stop button.
STOPPED_BY_PAGE = "the page asked the server to stop"
# A BROWSER HANGING UP, and nothing else. These three are
# what a closed tab does to a socket this server is writing to - on Windows an
# abort, on a reset connection a reset, on a pipe a broken pipe. They are
# recorded and passed over; every other `OSError` is a real fault and surfaces.
DISCONNECTED = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)

_ACTIVE_LOCK = threading.Lock()
_ACTIVE = None


def _loggable_payload(path: str, body: dict) -> dict:
    """What of a request body belongs in the journal.

    Every payload but two is a handful of numbers and goes in as it stands. A
    snapshot carries megabytes of SVG and an event batch carries its own lines
    already: both are summarised, so a journal line stays a line.
    """
    if not isinstance(body, dict):
        return {}
    if path in ("/snapshot", "/bug_report"):
        posted = body.get("viewports")
        posted = posted if isinstance(posted, list) else []
        return {
            "viewports": [
                entry.get("name") for entry in posted if isinstance(entry, dict)
            ],
            "svg_bytes": sum(
                len(entry.get("svg", "") or "")
                for entry in posted if isinstance(entry, dict)
            ),
            # The one line the person typed. It is the whole point of a problem
            # report and it is two dozen characters, so it stays in the line.
            "note": str(body.get("note") or body.get("note_box") or "")[:200],
        }
    if path == "/journal":
        events = body.get("events")
        return {"events": len(events) if isinstance(events, list) else 0}
    if path in ("/import_settings", "/import_placement"):
        # The whole file rides in this body, and the session's own `import` or
        # `placement` record keeps it. Recording it here as well would be the
        # same kilobyte twice in one journal, so this line says which file and
        # how much of it arrived.
        content = body.get("content")
        return {
            "name": actions.import_name(body.get("name")),
            "content_bytes": len(content) if isinstance(content, str) else 0,
        }
    return body


class PreviewHandler(BaseHTTPRequestHandler):
    """The route table. One instance per request; the session is on the server."""

    server_version = "eedesignpanels-preview"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        """Quiet by default: the terminal belongs to the run, not to the socket."""
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _same_origin(self) -> bool:
        """Is this request addressed to the loopback host we are serving on?

        DNS rebinding is the attack this closes: a page on any site can point a
        name at 127.0.0.1 and then talk to whatever is listening. Binding the
        loopback interface does not stop that - the browser is on this machine -
        so the Host header has to be checked as well.
        """
        host = (self.headers.get("Host") or "").split("]")[-1].rsplit(":", 1)[0]
        host = host.strip("[")
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _own_origin(self) -> bool:
        """Is this request from this server's own page, or from no page at all?

        A browser names the page a request comes from in `Origin` on every
        POST, and on a cross-site GET. Only this server's own address is
        accepted there. `null` is a sandboxed or file page and is refused. No
        `Origin` at all is a client that is not a web page, and for a POST the
        token decides (:meth:`_has_token`).
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        own = {f"http://{name}:{port}" for name in LOOPBACK_NAMES}
        return origin.strip() in own

    def _has_token(self) -> bool:
        """Does this request carry this server's session token?"""
        offered = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(offered.encode("utf-8"),
                                   self.server.token.encode("utf-8"))

    def _refuse(self, reason: str) -> None:
        """Answer 403, and journal what was refused and why.

        A refused POST body is read and dropped first, bounded, for the reason
        `_body` gives: answering without reading it makes Windows reset the
        socket, and the refusal never reaches the client.
        """
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            self._drain(min(max(length, 0), MAX_DRAIN_BYTES))
            self.close_connection = True
        self._record("server.refused", path=self.path.split("?")[0],
                     method=self.command, reason=reason,
                     origin=self.headers.get("Origin") or "")
        self._json(403, {"ok": False, "error": reason})

    def _page(self, markup: str) -> None:
        """Serve one of the two pages, with this session's token written in.

        The token goes in a `<meta>` tag right after the character set, where
        the shared script reads it (`PB_TOKEN`).
        """
        tag = f'<meta name=pb-token content="{self.server.token}">'
        if TOKEN_META_ANCHOR in markup:
            markup = markup.replace(TOKEN_META_ANCHOR, TOKEN_META_ANCHOR + tag, 1)
        else:
            markup = tag + markup
        self._html(markup)

    def _accepts_gzip(self) -> bool:
        """Did the client offer to take a compressed body? Every browser does."""
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        # COMPRESSED WHEN IT IS WORTH IT, MEASURED. A panel fragment
        # is megabytes of SVG markup and the browser measured 263 ms fetching
        # 3.9 MB of it over loopback; the same bytes gzip to about a fifth at
        # level 1, for 7 ms of CPU. Level 1 rather than 6 because this is inside
        # a first paint: 6 costs three times the CPU for another 40% off a
        # transfer that is no longer the slow part.
        #
        # Small bodies are sent as they are - a poll answer is a few hundred
        # bytes and gzip would make it bigger - and a client that did not offer
        # to take gzip gets exactly what it always got.
        headers = []
        if len(body) >= GZIP_THRESHOLD_BYTES and self._accepts_gzip():
            body = gzip.compress(body, GZIP_LEVEL)
            headers.append(("Content-Encoding", "gzip"))
        self.sent_bytes = len(body)
        # A TAB THAT WENT AWAY IS NOT AN ERROR. Writing a reply to a closed
        # socket raises one of `DISCONNECTED`, and left alone that would print
        # a traceback in the user's terminal for every request in flight when
        # a tab closes. It is recorded as the ordinary
        # disconnect it is, the connection is not reused, and the route
        # continues - there is nothing to recover, and nobody to tell.
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            # Nothing here may be cached: every answer is about the board as it
            # is this second.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # What actually went over the socket, which is not what the payload
            # measures once it is compressed. Read by the fragment route so the
            # journal can say both (`_fragment`).
            self.wfile.write(body)
        except DISCONNECTED as exc:
            self.sent_bytes = 0
            self.close_connection = True
            self._record("server.client_gone", path=self.path,
                         where="reply", error=type(exc).__name__)

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code, json.dumps(payload).encode("utf-8"), "application/json"
        )

    def _html(self, markup: str, code: int = 200) -> None:
        self._send(code, markup.encode("utf-8"), "text/html; charset=utf-8")

    def _record(self, kind: str, /, **data) -> None:
        """One journal line, when this session has a folder. Never raises.

        *kind* IS POSITIONAL-ONLY, the same fence
        :meth:`session.PreviewSession.log` carries: a key named `kind` in
        `**data` then lands in the data instead of taking the slot that names
        the record.

        NO ROUTE HERE SPREADS A POSTED BODY. All three callers pass literal
        keyword names, and the one that carries a request body carries it
        whole, under `payload=`, which is what this call wants: a body is a
        value, never a set of argument names, because a posted key could
        otherwise collide with a parameter and crash the call. Keep it that
        way.
        """
        journal = getattr(self.server.session, "journal", None)
        if journal is not None:
            journal.append(kind, **data)

    def _body(self, limit: int = MAX_BODY_BYTES) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            # THE BODY IS DRAINED BEFORE THE REFUSAL. Answering without
            # reading it leaves the client still writing into a socket this
            # handler is closing, and Windows answers that with a reset - so
            # the page would see an aborted connection instead of the reason
            # it was refused. Bounded by `MAX_DRAIN_BYTES`: past that the
            # connection is dropped, which is the honest end for a body nobody
            # here will ever read.
            self._drain(min(length, MAX_DRAIN_BYTES))
            raise actions.ActionError("that request body is far too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise actions.ActionError(f"the request body is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise actions.ActionError("the request body must be a JSON object")
        return data

    def _drain(self, length: int) -> None:
        """Read and discard *length* bytes of a body this handler refused."""
        left = length
        while left > 0:
            chunk = self.rfile.read(min(left, DRAIN_CHUNK_BYTES))
            if not chunk:
                return
            left -= len(chunk)

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        if not self._same_origin():
            self._json(403, {"ok": False, "error": "not a loopback host"})
            return
        if not self._own_origin():
            self._refuse("a request from another web page is refused")
            return
        session = self.server.session
        path = self.path.split("?")[0]
        if path not in UNJOURNALED and path not in TIMED_GETS:
            self._record("server.get", endpoint=path,
                         generation=session.generation)
        if path == "/":
            self._page(session.page())
        elif path == "/panel":
            self._page(session.panel_page())
        elif path == "/state":
            # WHICH STEPS ARE OPEN rides on the cheap poll:
            # it is what a step link asks before it opens anything, so a step
            # already on screen is jumped to rather than opened twice.
            self._json(200, {**session.state(),
                             "pages": self.server.lifecycle.present()})
        elif path == "/fragment":
            self._fragment(session, session.fragment, path)
        elif path == "/panel/fragment":
            self._fragment(session, session.panel_fragment, path)
        elif path == "/check/report":
            self._report(session)
        elif path == "/fingerprints/report":
            report = session.workdir / "panel-fingerprints.json"
            if report.is_file():
                self._send(200, report.read_bytes(), "application/json")
            else:
                self._json(404, {"error": "No fingerprint check has run in this session."})
        elif path == bugreport.DOWNLOAD_PATH:
            self._bug_report(session)
        elif path == "/events":
            self._events()
        elif path == "/favicon.ico":
            self._send(204, b"", "image/png")
        else:
            self._json(404, {"ok": False, "error": f"no such path: {path}"})

    def _fragment(self, session, build, endpoint: str) -> None:
        """One fragment, served and MEASURED: what it cost and how big it was.

        THE CLIENT'S HALF OF THE LATENCY STARTS HERE. A fragment carries every
        visible layer's markup, so it is the largest thing this server sends
        and the first paint waits on all of it; a breakdown that stopped at the POST reply would be missing the
        biggest slice. So the record says how long the payload took to assemble,
        how long it took to serialize, and how many bytes went out - joined to
        the rest of its interaction by the generation the payload itself names.

        *build* is the session method, passed rather than named, so the two
        routes are one function and cannot drift apart.

        `?painted=N` is the page saying which paint it is already holding, and
        the answer then carries the layers that have moved since and no others
        (`session._changed_svgs`). Absent or unreadable means "nothing", which
        is the whole stack.
        """
        started = time.monotonic()
        payload = build(painted=self._painted())
        built = time.monotonic()
        # The lock wait comes off the payload rather than out of the timings:
        # see `session.LOCK_WAIT_KEY`. It never goes on the wire.
        waited = payload.pop(session_module.LOCK_WAIT_KEY, 0.0) or 0.0
        body = json.dumps(payload).encode("utf-8")
        dumped = time.monotonic()
        self._send(200, body, "application/json")
        done = time.monotonic()
        self._record(
            "server.get",
            endpoint=endpoint,
            generation=payload.get("generation", session.generation),
            upgrade_seq=payload.get("upgrade_seq", 0),
            paint_seq=payload.get("paint_seq", 0),
            seconds=round(done - started, 4),
            phases={
                "wait": waited,
                "build": round((built - started) * 1000.0 - waited, 1),
                "json": round((dumped - built) * 1000.0, 1),
                "write": round((done - dumped) * 1000.0, 1),
            },
            bytes=len(body),
            sent=getattr(self, "sent_bytes", len(body)),
            layers=len(payload.get("svg") or {}),
        )

    def _painted(self) -> int:
        """What paint the page says it is holding. Untrusted, so it is bounded.

        A number this cannot read is ZERO, which asks for the whole stack: the
        safe direction is always "send the picture".
        """
        query = urllib.parse.urlparse(self.path).query
        asked = urllib.parse.parse_qs(query).get("painted", ["0"])[0]
        try:
            painted = int(asked)
        except (TypeError, ValueError):
            return 0
        return painted if 0 <= painted <= MAX_PAINT_SEQ else 0

    def _report(self, session) -> None:
        """The last comparison report, served as it was written.

        The ONE file this server hands out, and the client never says which: the
        path is the one the session recorded when it ran the comparison, so
        there is no name to traverse and nothing to sanitise.
        """
        report = getattr(session, "check_report", None)
        if report is None or not report.is_file():
            self._json(404, {
                "ok": False,
                "error": "no comparison has been run in this session yet",
            })
            return
        self._send(200, report.read_bytes(), "text/html; charset=utf-8")

    def _bug_report(self, session) -> None:
        """The problem report zip, handed to the browser to be saved.

        A BROWSER OWNS THE SAVE DIALOG and a page cannot write to a chosen
        path, so the page asks for this file with an anchor that
        carries `download` and the browser does what the person has it set to
        do. The name is stated twice - here and on the anchor - because a
        browser that ignores one honours the other.

        The client never says which file: it is the one this session built
        last, the same rule the comparison report follows.
        """
        built = getattr(session, "bug_report_path", None)
        if built is None or not Path(built).is_file():
            self._json(404, {
                "ok": False,
                "error": "no problem report has been built in this session yet",
            })
            return
        body = Path(built).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{_quoted_filename(Path(built).name)}"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.sent_bytes = len(body)

    def do_POST(self):
        if not self._same_origin():
            self._json(403, {"ok": False, "error": "not a loopback host"})
            return
        if not self._own_origin():
            self._refuse("a request from another web page is refused")
            return
        if not self._has_token():
            self._refuse("this request does not carry the session token of "
                         "the page this server served")
            return
        session = self.server.session
        path = self.path.split("?")[0]
        started = time.monotonic()
        body, reply, error = {}, None, ""
        try:
            body = self._body(BODY_LIMITS.get(path, MAX_BODY_BYTES))
            if path in CHANGING:
                with session.action(path):
                    reply = self._change(session, path, body)
            elif path == "/layers":
                # Never gated, never journaled here: see the two notes above.
                reply = session.set_layers(body)
            elif path == "/journal":
                reply = session.journal_events(body)
            elif path == "/note":
                reply = session.note(body)
            elif path == "/snapshot":
                reply = session.snapshot(body)
            elif path == "/bug_report":
                # Never gated: collecting a report reads files and writes one
                # zip in the local report folder, and a person reaching for it
                # while something is running is exactly when it is needed most.
                reply = session.bug_report(body)
            elif path == "/shutdown":
                # Never gated. Stopping has to work while something is running,
                # or a stuck action takes the only way out with it.
                reply = {"ok": True, "message": "stopping",
                         "generation": session.generation}
            else:
                error = f"no such path: {path}"
                self._journal_action(path, body, None, error, started)
                self._json(404, {"ok": False, "error": error})
                return
        except actions.Busy as exc:
            # A duplicate arriving mid-action. Journaled as a REJECTION, so the
            # history shows the click and shows that nothing came of it.
            self._journal_action(path, body, None, str(exc), started,
                                 rejected=True)
            self._json(409, {"ok": False, "busy": True, "error": str(exc),
                             "generation": session.generation})
            return
        except actions.ActionError as exc:
            # The designer asked for something that cannot be done. Their
            # sentence, their status code, nothing written.
            self._journal_action(path, body, None, str(exc), started)
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._journal_action(path, body, reply, "", started)
        self._json(200, reply)
        if path == "/shutdown":
            # Answered first: the page has to receive the reply before the
            # socket it arrived on goes away.
            self.server.request_stop()

    def _change(self, session, path, body) -> dict:
        """Run the one action *path* names. Called holding the action gate."""
        if path == "/generate_edge_plating":
            return session.generate_edge_plating()
        if path == "/remove_edge_plating":
            return session.remove_edge_plating()
        if path == "/accept":
            return session.accept(body)
        if path == "/decline":
            return session.decline(body)
        if path == "/set_param":
            return session.set_param(body)
        if path == "/import_settings":
            return session.import_settings(body)
        if path == "/import_placement":
            return session.import_placement(body)
        if path == "/save":
            return session.save()
        if path == "/build_panel":
            return session.build_panel(body)
        if path == "/save_panel":
            return session.save_panel()
        if path == "/check":
            return session.check_panel(body)
        if path == "/check_fingerprints":
            return session.check_fingerprints()
        return session.reload_from_disk()

    def _journal_action(self, path, body, reply, error, started,
                        rejected=False) -> None:
        """One `server.action` line: endpoint, payload, outcome, generation."""
        if path in UNJOURNALED:
            return
        self._record(
            "server.action",
            endpoint=path,
            payload=_loggable_payload(path, body),
            ok=not error,
            rejected=rejected,
            error=error,
            message=(reply or {}).get("message", ""),
            generation=(reply or {}).get(
                "generation", self.server.session.generation
            ),
            seconds=round(time.monotonic() - started, 4),
        )

    def _which_page(self) -> str:
        """Which step this stream belongs to, as the page said on the way in.

        A page identifies itself (`/events?page=prep`) so that a RECONNECT can
        be matched to the page that went away: without it a reload of step 1
        and a close of step 1 look the same to a server counting connections.
        """
        query = urllib.parse.urlparse(self.path).query
        asked = urllib.parse.parse_qs(query).get("page", [""])[0]
        return asked.strip()

    def _client_gone(self) -> bool:
        """Has the browser dropped this connection?

        A closed tab closes its sockets, so the answer arrives as a readable
        socket that reads empty - end of stream. Asked between waits rather
        than discovered by a failed write, because the next write on an idle
        stream is a heartbeat fifteen seconds away and a close has to be
        noticed while the user is still looking at the other tab.
        """
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not ready:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _events(self):
        """One server-sent event per change, plus a heartbeat when idle.

        A message carries the generation AND the layer panel's state, because
        the two pages act on them differently: a new generation means fetch a
        fragment, a new layer sequence means repaint the stack already on
        screen. One stream, so they cannot arrive out of order.

        The stream is also the page's PRESENCE. Holding it
        is what says the tab is open; dropping it starts the grace window in
        :mod:`eedesignpanels.preview.lifecycle`, and letting that window run out
        without reconnecting is what stops the server.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        session = self.server.session
        page = self._which_page()
        tracked = self.server.stream_opened(page)
        subscriber = session.subscribe()
        beat = time.monotonic()
        try:
            while not self.server.stopping and not subscriber.closed:
                message = subscriber.wait(EVENT_POLL_SECONDS)
                if message is None:
                    if self._client_gone():
                        break
                    if time.monotonic() - beat < HEARTBEAT_SECONDS:
                        continue
                    payload = ": keep-alive\n\n"
                else:
                    payload = f"data: {json.dumps(message)}\n\n"
                beat = time.monotonic()
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except DISCONNECTED:
            # The page went away. That is how an event stream normally ends,
            # and it is the ordinary disconnect, not a fault. Deliberately
            # narrower than `OSError`: a socket error that is not a hang-up is
            # a real fault, and it surfaces.
            self._record("server.client_gone", path=self.path, where="events")
        finally:
            session.unsubscribe(subscriber)
            if tracked:
                self.server.stream_dropped(page)
        # HTTP/1.1 keep-alive cannot follow a stream of unknown length.
        self.close_connection = True


class PreviewServer(ThreadingHTTPServer):
    """A threaded loopback server bound to one :class:`PreviewSession`."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, session, host="127.0.0.1", port=0, verbose=False,
                 grace: float = lifecycle.GRACE_SECONDS):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"the preview server binds loopback only, not {host!r} - it "
                f"writes to the design, and nothing off this machine may drive it"
            )
        super().__init__((host, port), PreviewHandler)
        self.session = session
        # The session token the served page sends back with every POST. New
        # for every server, never written to disk, never in a URL.
        self.token = secrets.token_urlsafe(32)
        self.verbose = verbose
        self.stopping = False
        # Which tabs are open, and how long a dropped stream has to come back
        # before it counts as a closed one.
        self.lifecycle = lifecycle.Lifecycle(grace=grace)
        # Why this server stopped, for the journal and the terminal. Set by
        # whoever stops it; the tab watchdog names the tab.
        self.stop_reason = STOPPED_BY_PAGE
        self._thread = None
        self._watchdog = None
        # Two callers can arrive at `stop` at once - the page's shutdown POST
        # spawns one and whoever launched the server may call it too - and they
        # must not tear the same socket down together.
        self._teardown = threading.Lock()

    def handle_error(self, request, client_address) -> None:
        """What a failed request prints. A HANG-UP PRINTS NOTHING.

        `socketserver` answers any exception out of a handler with
        "Exception occurred during processing of request" and a traceback. A
        closed tab reaches here through the request line it never finished
        sending, and one closed tab would print a screenful of them. A
        disconnect is journaled instead. EVERYTHING ELSE STILL PRINTS: a fault
        in a route has to be seen.
        """
        error = sys.exc_info()[1]
        if isinstance(error, DISCONNECTED):
            self.session.log("server.client_gone", where="request",
                             error=type(error).__name__)
            return
        super().handle_error(request, client_address)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/"

    def start(self) -> str:
        """Serve on a background thread. Returns the URL. One per process."""
        global _ACTIVE
        with _ACTIVE_LOCK:
            if _ACTIVE is not None:
                raise RuntimeError(
                    "a preview server is already running in this process - one "
                    "session at a time, because two would be two writers on one "
                    "board. Stop the first one."
                )
            _ACTIVE = self
        self._thread = threading.Thread(
            target=self.serve_forever, name="eedesignpanels-preview", daemon=True
        )
        self._thread.start()
        self._watchdog = threading.Thread(
            target=self._watch_tabs, name="eedesignpanels-tabs", daemon=True
        )
        self._watchdog.start()
        return self.url

    # -- the tabs ----------------------------------------------------------

    def stream_opened(self, page: str) -> bool:
        """A page subscribed. True when it is a step whose presence counts."""
        if not self.lifecycle.tracks(page):
            return False
        self.lifecycle.opened(page, time.monotonic())
        return True

    def stream_dropped(self, page: str) -> None:
        """A page's stream ended - a close, a reload or a navigation."""
        self.lifecycle.dropped(page, time.monotonic())

    def _watch_tabs(self) -> None:
        """Stop the session when the LAST tab has been gone for the whole window.

        One open tab is enough, and the server stops cleanly when the last one
        closes. The unsaved-work guard is the browser's own prompt at tab
        close, and a close that got past it stops the server without writing
        anything.
        """
        while not self.stopping:
            time.sleep(WATCH_SECONDS)
            if self.stopping:
                return
            page = self.lifecycle.due(time.monotonic())
            if page is None:
                continue
            self.closed_by(page)
            return

    def closed_by(self, page: str) -> None:
        """Declare the LAST tab closed: journal it, say so, stop.

        Reached only when no page is holding a stream, so
        *page* is the tab that went last rather than any tab that went.
        """
        reason = f"the {page} tab was the last one open, and it was closed"
        self.session.log(
            "server.tab_closed", page=page,
            grace_seconds=self.lifecycle.grace, reason=reason,
        )
        self.request_stop(reason)

    def request_stop(self, reason: str = STOPPED_BY_PAGE) -> None:
        """Ask for a shutdown from inside a request, without deadlocking on it.

        The final "closing" event goes out FIRST and the teardown follows it, so
        the page that is still open hears why it is going rather than watching
        its stream die. Everything happens on a worker: the caller may be the
        request whose socket is about to be closed.
        """
        self.stop_reason = reason
        threading.Thread(
            target=self._announce_then_stop, args=(reason,), daemon=True
        ).start()

    def _announce_then_stop(self, reason: str) -> None:
        self.session.announce_closing(reason)
        # Long enough for a local socket write on the same machine. The pages
        # act on the event; this only keeps the socket alive while it travels.
        time.sleep(CLOSING_SECONDS)
        self.stop()

    def stop(self) -> None:
        """Stop serving and release the port. Safe to call more than once.

        `shutdown()` waits for `serve_forever` to leave its loop, and that event
        is never set on a server that was never started - so it is only asked
        for when there is a serving thread to ask. The socket is closed either
        way: a server that bound a port and never served still holds it.

        The teardown itself is serialized. A second caller waits and then finds
        the work done, rather than joining a thread the first caller has
        already cleared - which is a `None.join()` and a confusing traceback on
        the way out of a session that stopped perfectly well.
        """
        global _ACTIVE
        self.stopping = True
        self.session.close()
        with self._teardown:
            if self._thread is not None:
                self.shutdown()
                self._thread.join(timeout=5.0)
                self._thread = None
            self.server_close()
            with _ACTIVE_LOCK:
                if _ACTIVE is self:
                    _ACTIVE = None


def active_server():
    """The server running in this process, or None. For tests and for shutdown."""
    with _ACTIVE_LOCK:
        return _ACTIVE
