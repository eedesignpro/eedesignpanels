"""A session journal, read back as a timing table: where an interaction went.

**WHERE THE INTERACTION LATENCY GOES.** A session already records every phase
of every interaction (:mod:`eedesignpanels.preview.journal`); this is the reader
that turns one `journal.jsonl` into a table a person can act on, and it runs on
ANY session folder, including one a user drove by hand an hour ago::

    python -m eedesignpanels.preview.timing <session folder>
    python -m eedesignpanels.preview.timing <session folder> --html out.html

**ONE INTERACTION IS ONE GENERATION.** Every action ends in one publish, so the
generation is the join key for the whole chain - the request's own record, the
board computation, the panel recalculation, the tiers that land behind the first
paint, the fragment the page then fetched, and the page's own measurement of
what it cost to paint. Records from both sides of the wire carry it, so nothing
here has to guess which edit a number belongs to.

**WHAT THE KIND IS DERIVED FROM, not declared.** A `/set_param` may be four
different amounts of work depending on which number moved, and the honest
statement of which one it was is what the run actually did:

* a board computation happened  -> a BOARD edit (prep is drawn from that number)
* the panel was built in full   -> a LAYOUT edit (the six copies moved)
* only the carrier stages ran   -> a CARRIER edit (the stepped panel was kept)
* neither                       -> a VIEW-ONLY edit (nothing moved; see
  `panelbuild.VIEW_ONLY_KEYS`)

So the table cannot claim a carrier edit was cheap when the cache missed and it
rebuilt everything - the classification comes from the same records as the
timings.

Nothing here is a gate and nothing here reads the board: it is a report over a
file, stdlib only, and a journal missing a field prints `-` rather than failing.
"""

import argparse
import html
import json
import statistics
import sys
from pathlib import Path

from eedesignpanels.preview.journal import JOURNAL_NAME

# What one interaction's records are joined on. See the module docstring.
GENERATION = "generation"

# The endpoints that are one interaction each, and what to call them. A name is
# what the user did - a reader looks for "carrier edit", not "/set_param".
KIND_BY_ENDPOINT = {
    "/accept": "score accept",
    "/decline": "score decline",
    "/import_settings": "import settings",
    "/import_placement": "import placement",
    "/save": "save board",
    "/build_panel": "build panel",
    "/save_panel": "save panel files",
    "/check": "comparison",
    "/reload": "reload",
}
# The one endpoint whose kind depends on what it did - see the module docstring.
PARAM_ENDPOINT = "/set_param"
# THE ENDPOINTS THAT ARE AN INTERACTION. Everything else a page posts -
# `/journal`, `/note`, `/snapshot`, `/layers` - changes nothing and produces no
# generation of its own, so it lands in whatever generation is current and must
# not be allowed to name it: a batch of interaction events arriving while a
# carrier edit was painting would otherwise label that edit `/journal`. They
# are counted instead.
OWNING_ENDPOINTS = frozenset({*KIND_BY_ENDPOINT, PARAM_ENDPOINT})
BOARD_EDIT = "board edit"
LAYOUT_EDIT = "layout edit"
CARRIER_EDIT = "carrier edit"
VIEW_ONLY_EDIT = "view-only edit"
LAUNCH = "launch"

# The phase chain, in the order it happens, with the journal key each phase is
# read out of. A phase nothing reported is left out of the row rather than
# printed as a zero: "the panel was not recalculated" and "it took no time" are
# different statements.
#
# `request` is the remainder: the whole request minus the phases below it, which
# is the body read, the action gate, the config validation and the reply. Kept
# as a remainder rather than measured, so the parts always add up to the total a
# caller actually waited for.
SERVER_PHASES = (
    ("request", "the body, the gate and the reply"),
    ("board.load", "reading the design files - a launch only"),
    ("board.prep", "prep computation - toolpath, candidates, checks"),
    ("board.frame", "layers, theme, colours, window"),
    ("board.render", "the board's fast render"),
    ("board.write_svgs", "the board's layer documents"),
    ("board.overlay", "the candidate captions"),
    ("panel.snapshot", "the board as the file holds it"),
    ("panel.build", "step and repeat, and the carrier stages"),
    ("panel.differs", "does the panel match the design file"),
    ("panel.frame", "layers, theme, colours, window"),
    ("panel.render", "the panel's fast render"),
    ("panel.write_svgs", "the panel's layer documents"),
)
# What lands BEHIND the first paint. Not part of the wait, and part of the
# picture: a reader has to see both or the second tier looks free.
BEHIND_PHASES = (
    ("lettering", "text and dimensions, swapped in behind the graphics"),
    ("content", "the kicad-cli content render"),
    ("fill", "KiCad's own rail-copper pour"),
)
# The client's half, as the page measured it (`page_served`'s `refresh`).
CLIENT_PHASES = (
    ("fetch_ms", "the fragment request, to the first byte"),
    ("read_ms", "reading the body off the socket"),
    ("parse_ms", "JSON.parse of the fragment"),
    ("words_ms", "the header, the pending count, the frame"),
    ("svg_ms", "swapping every layer's markup into the document"),
    ("apply_ms", "the controls column and the layer paint"),
)


def read_journal(where) -> list:
    """Every record of a session, in order. *where* is the folder or the file.

    A journal is append-only JSON Lines, so a half-written last line is a
    crashed session and not a broken file: it is dropped and the rest is read.
    """
    path = Path(where)
    if path.is_dir():
        path = path / JOURNAL_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no journal at {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _ms(seconds) -> float:
    """Seconds as milliseconds, one decimal. `None` stays `None`."""
    if seconds is None:
        return None
    return round(float(seconds) * 1000.0, 1)


class Interaction:
    """One edit: its kind, the chain it went through, and what the page paid.

    Built from every record carrying one generation. Nothing is inferred that
    the records do not say - a missing half is missing, not zero.
    """

    def __init__(self, generation: int):
        self.generation = generation
        self.endpoint = ""
        self.payload = {}
        self.total_ms = None
        self.server = {}
        self.behind = {}
        # The page's own half, per page, and the slowest of them (see
        # :meth:`_slowest_client`). `clicked` is the one page that was clicked,
        # when a hand was on it at all.
        self.clients = {}
        self.client = {}
        self.clicked = {}
        # The repaints that landed BEHIND the first paint, per page: one per
        # tier. Not part of the wait; part of what the wire carried.
        self.tiers = {}
        self.fragment = {}
        self.ok = True
        self.error = ""
        self.rejected = False
        # Clicks that were answered with a reason and changed nothing. They
        # produced no generation of their own, so they are counted here against
        # whatever was current (see :meth:`take`).
        self.refused = []
        # The requests that happened DURING this interaction and are not it:
        # journal flushes, notes, layer toggles.
        self.alongside = []
        self.counts = {}
        self.kinds = set()

    # -- what it was -------------------------------------------------------

    @property
    def kind(self) -> str:
        """What to call this interaction. Derived - see the module docstring."""
        if self.endpoint in KIND_BY_ENDPOINT:
            return KIND_BY_ENDPOINT[self.endpoint]
        if self.endpoint == PARAM_ENDPOINT:
            return self._did() or VIEW_ONLY_EDIT
        if not self.endpoint:
            # NO REQUEST AT ALL: the launch, or a session driven straight
            # through its own methods (a script, a test). A computation with
            # nothing asking for it is the launch; a panel recalculation with
            # nothing asking for it is named by what it built.
            if "compute" in self.kinds:
                return LAUNCH
            return self._did() or "other"
        return self.endpoint

    def _did(self) -> str:
        """The kind this interaction's own work implies, or `""`."""
        if "compute" in self.kinds:
            return BOARD_EDIT
        if "panel.build_full" in self.kinds:
            return LAYOUT_EDIT
        if "panel.carrier_only" in self.kinds:
            return CARRIER_EDIT
        return ""

    @property
    def named(self) -> str:
        """The parameter names this interaction moved, for a row's detail."""
        return ", ".join(sorted(str(key) for key in self.payload)) if isinstance(
            self.payload, dict
        ) else ""

    # -- the chain ---------------------------------------------------------

    def take(self, record: dict) -> None:
        """Fold one journal record into this interaction."""
        kind, data = record.get("kind", ""), record.get("data") or {}
        self.kinds.add(kind)
        if kind == "server.action":
            if not data.get("ok", True):
                # A REFUSAL OWNS NOTHING. It produced no generation, so it
                # lands in whatever generation was current - and letting it
                # name that interaction would label a launch a `/set_param`.
                # Counted instead, because a refused click is still a click
                # the user made.
                self.refused.append({
                    "endpoint": data.get("endpoint", ""),
                    "error": data.get("error", ""),
                    "rejected": bool(data.get("rejected")),
                    "ms": _ms(data.get("seconds")),
                })
                return
            if data.get("endpoint") not in OWNING_ENDPOINTS:
                # Instrumentation, a layer toggle, a note: a request that owns
                # no generation (see :data:`OWNING_ENDPOINTS`).
                self.alongside.append({
                    "endpoint": data.get("endpoint", ""),
                    "ms": _ms(data.get("seconds")),
                })
                return
            self.endpoint = data.get("endpoint", "")
            self.payload = data.get("payload") or {}
            self.total_ms = _ms(data.get("seconds"))
            self.ok = True
            self.error = ""
            self.rejected = False
        elif kind == "compute":
            self._phases("board", data.get("phases"))
            self.counts.update(
                {key: data[key] for key in ("candidates", "scores", "tabs")
                 if key in data}
            )
            if self.total_ms is None:
                self.total_ms = _ms(data.get("seconds"))
        elif kind in ("panel.recalc", "panel.build", "panel.start"):
            self._phases("panel", data.get("phases"))
            if self.total_ms is None:
                self.total_ms = _ms(data.get("seconds"))
        elif kind == "letter":
            self.behind["lettering"] = _ms(data.get("seconds"))
        elif kind == "upgrade":
            self.behind["content"] = round(
                (self.behind.get("content") or 0.0) + (_ms(data.get("seconds")) or 0.0), 1
            )
        elif kind == "panel.fill":
            self.behind["fill"] = _ms(data.get("seconds"))
        elif kind == "server.get":
            self.fragment = {
                "endpoint": data.get("endpoint", ""),
                "ms": _ms(data.get("seconds")),
                "bytes": data.get("bytes"),
                "layers": data.get("layers"),
                "build_ms": (data.get("phases") or {}).get("build"),
                "json_ms": (data.get("phases") or {}).get("json"),
            }
        elif kind == "client.paint":
            painted = {
                key: data.get(key) for key in
                (*(name for name, _ in CLIENT_PHASES), "total_ms", "bytes",
                 "layers", "reason", "cause")
            }
            page = data.get("page") or "?"
            # THE FIRST PAINT IS NOT THE TIER REPAINTS. A page repaints once
            # for the edit and once more for each tier that lands behind it
            # (`cause` says which), and counting a tier repaint as the first
            # paint would report the wait as the whole chain - which is the
            # opposite of what the tiers are for.
            if painted.get("cause") == "upgrade":
                self.tiers.setdefault(page, []).append(painted)
            else:
                self.clients[page] = painted
                self._slowest_client()
        elif kind == "client.action":
            self.clicked = {
                "page": data.get("page") or "?",
                "post_ms": data.get("post_ms"),
                "click_to_paint_ms": data.get("total_ms"),
            }

    def _slowest_client(self) -> None:
        """The page that paid MOST for this interaction becomes `client`.

        BOTH TABS REPAINT on a new generation, and the screen is not finished
        until the slower of them is - so the slower one is the honest answer to
        "how long until the picture was right". Each page's own numbers stay in
        `clients`, because "the prep page re-fetched a megabyte for an edit that
        did not change its picture" is exactly the kind of thing this report
        exists to show.
        """
        best, found = None, {}
        for painted in self.clients.values():
            total = painted.get("total_ms")
            if total is not None and (best is None or total > best):
                best, found = total, painted
        self.client = found or self.client

    def _phases(self, prefix: str, phases) -> None:
        """One computation's own named phases, under this subject's prefix.

        The snapshot has two spellings - taken, or reused - and the DIFFERENCE
        is the point of the cache, so both land under one name and the reuse is
        remembered as a kind so a row can say which happened.
        """
        if not isinstance(phases, dict):
            return
        for name, value in phases.items():
            if name in ("snapshot", "snapshot_reused"):
                self.server[f"{prefix}.snapshot"] = value
                self.kinds.add(f"{prefix}.{name}")
                continue
            if name in ("build_full", "carrier_only"):
                # The build's INNER name: kept as a kind, because it is what
                # says whether the stepped panel was reused, and its span is
                # already reported as `build`.
                self.kinds.add(f"{prefix}.{name}")
                continue
            self.server[f"{prefix}.{name}"] = value

    @property
    def measured_ms(self) -> float:
        """Every named server phase added up."""
        return round(sum(v for v in self.server.values() if v), 1)

    def chain(self) -> dict:
        """The server chain, in order, with `request` as the remainder."""
        out = {}
        for name, _ in SERVER_PHASES:
            if name == "request":
                continue
            if self.server.get(name) is not None:
                out[name] = self.server[name]
        if self.total_ms is not None:
            rest = round(self.total_ms - self.measured_ms, 1)
            out = {"request": max(rest, 0.0), **out}
        return out

    @property
    def first_bytes(self) -> int:
        """What this interaction's FIRST paint cost on the wire, both tabs."""
        painted = [
            one.get("bytes") for one in self.clients.values() if one.get("bytes")
        ]
        return sum(painted) if painted else None

    @property
    def wire_bytes(self) -> int:
        """Every byte of fragment this interaction cost, tier repaints included.

        The first paint is what the user waits for; this is what the wire
        carried for the whole edit, which is the number a diff protocol is
        judged on. None when no page reported a paint.
        """
        painted = [
            *(one.get("bytes") for one in self.clients.values()),
            *(one.get("bytes") for found in self.tiers.values()
              for one in found),
        ]
        painted = [value for value in painted if value]
        return sum(painted) if painted else None

    @property
    def first_paint_ms(self) -> float:
        """Edit to painted: the whole wait, both halves of the wire.

        A CLICKED action has the answer directly - the page timed it from the
        press to the paint. An edit driven any other way (a scripted drive, the
        other tab's push) is the reply plus the paint behind it, which is how
        the two costs actually land: the push goes out as the reply does, so
        the page starts fetching as the answer arrives.

        None when no page reported a paint at all - a session driven with no
        browser in it measures the server's half honestly and says nothing
        about the other one, rather than inventing a number for it.
        """
        clicked = self.clicked.get("click_to_paint_ms")
        if clicked is not None:
            return float(clicked)
        painted = self.client.get("total_ms")
        if painted is None:
            return None
        return round((self.total_ms or 0.0) + float(painted), 1)


def interactions(records) -> list:
    """Every interaction in a journal, in order, joined by generation."""
    found = {}
    order = []
    for record in records:
        data = record.get("data") or {}
        generation = data.get(GENERATION)
        if not isinstance(generation, int) or generation <= 0:
            continue
        if generation not in found:
            found[generation] = Interaction(generation)
            order.append(generation)
        found[generation].take(record)
    return [found[generation] for generation in order]


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


def _median(values):
    values = [value for value in values if value is not None]
    return round(statistics.median(values), 1) if values else None


def _worst(values):
    values = [value for value in values if value is not None]
    return round(max(values), 1) if values else None


class KindSummary:
    """One interaction kind, summarised over every one of it in the session."""

    def __init__(self, kind: str, entries: list):
        self.kind = kind
        self.entries = entries

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def reply_median(self):
        return _median([entry.total_ms for entry in self.entries])

    @property
    def reply_worst(self):
        return _worst([entry.total_ms for entry in self.entries])

    @property
    def paint_median(self):
        return _median([entry.first_paint_ms for entry in self.entries])

    @property
    def paint_worst(self):
        return _worst([entry.first_paint_ms for entry in self.entries])

    @property
    def fragment_bytes(self):
        """What the FIRST paint cost on the wire - both tabs added up.

        Both pages repaint on a new generation, so the first paint of an edit
        is two fragments and the honest number is their sum. Falls back to the
        server's own record of the request when no page reported one.
        """
        return _median([
            entry.first_bytes for entry in self.entries
        ]) or _median([
            entry.fragment.get("bytes") for entry in self.entries
        ])

    @property
    def wire_bytes(self):
        """Every fragment byte of the whole interaction, tiers included."""
        return _median([entry.wire_bytes for entry in self.entries])

    @property
    def fragment_layers(self):
        return _median([entry.fragment.get("layers") for entry in self.entries])

    def phase_median(self, name: str):
        return _median([entry.chain().get(name) for entry in self.entries])

    def phases(self) -> dict:
        """The median of every server phase this kind ever reported."""
        out = {}
        for name, _ in SERVER_PHASES:
            value = self.phase_median(name)
            if value is not None:
                out[name] = value
        return out

    def behind(self) -> dict:
        out = {}
        for name, _ in BEHIND_PHASES:
            value = _median([entry.behind.get(name) for entry in self.entries])
            if value is not None:
                out[name] = value
        return out

    def client(self) -> dict:
        out = {}
        for name, _ in CLIENT_PHASES:
            value = _median([entry.client.get(name) for entry in self.entries])
            if value is not None:
                out[name] = value
        return out


def summarise(entries) -> list:
    """One :class:`KindSummary` per kind, heaviest median reply first."""
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.kind, []).append(entry)
    out = [KindSummary(kind, found) for kind, found in grouped.items()]
    out.sort(key=lambda one: -(one.reply_median or 0.0))
    return out


def _cell(value, width: int = 8, suffix: str = "") -> str:
    if value is None:
        return "-".rjust(width)
    return f"{value:,.0f}{suffix}".rjust(width)


def table(entries, title: str = "") -> str:
    """The whole report as text: the summary, then the phase split per kind."""
    lines = []
    if title:
        lines += [title, "=" * len(title), ""]
    lines += [
        "interaction kind        n   reply ms      first paint ms   "
        "first KB  all KB",
        "                            median worst  median worst     "
        "median    median",
    ]
    for one in summarise(entries):
        size, whole = one.fragment_bytes, one.wire_bytes
        lines.append(
            f"{one.kind:<22}{one.count:>3}  "
            f"{_cell(one.reply_median, 6)}{_cell(one.reply_worst, 6)}  "
            f"{_cell(one.paint_median, 7)}{_cell(one.paint_worst, 6)}   "
            f"{_cell(None if size is None else size / 1024.0, 8)}"
            f"{_cell(None if whole is None else whole / 1024.0, 10)}"
        )
    lines.append("")
    lines.append("PHASE SPLIT - median milliseconds per phase, in order")
    for one in summarise(entries):
        lines.append("")
        lines.append(f"  {one.kind}  (n={one.count})")
        phases = one.phases()
        widest = max(phases.values()) if phases else 0.0
        for name, _ in SERVER_PHASES:
            if name not in phases:
                continue
            value = phases[name]
            bar = "#" * int(round(28.0 * value / widest)) if widest else ""
            lines.append(f"    {name:<20}{value:>8.1f}  {bar}")
        behind = one.behind()
        if behind:
            lines.append("    -- behind the first paint (nobody waits) --")
            for name, _ in BEHIND_PHASES:
                if name in behind:
                    lines.append(f"    {name:<20}{behind[name]:>8.1f}")
        client = one.client()
        if client:
            lines.append("    -- the page's own half --")
            for name, _ in CLIENT_PHASES:
                if name in client:
                    lines.append(f"    {name:<20}{client[name]:>8.1f}")
    return "\n".join(lines) + "\n"


def jsonable(entries) -> dict:
    """The whole report as data, for a page or another tool to draw."""
    return {
        "interactions": [
            {
                "generation": entry.generation,
                "kind": entry.kind,
                "endpoint": entry.endpoint,
                "named": entry.named,
                "ok": entry.ok,
                "error": entry.error,
                "refused": entry.refused,
                "alongside": entry.alongside,
                "total_ms": entry.total_ms,
                "chain": entry.chain(),
                "behind": entry.behind,
                "client": entry.client,
                "clients": entry.clients,
                "tiers": entry.tiers,
                "clicked": entry.clicked,
                "wire_bytes": entry.wire_bytes,
                "first_paint_ms": entry.first_paint_ms,
                "fragment": entry.fragment,
            }
            for entry in entries
        ],
        "kinds": [
            {
                "kind": one.kind,
                "count": one.count,
                "reply_median": one.reply_median,
                "reply_worst": one.reply_worst,
                "paint_median": one.paint_median,
                "paint_worst": one.paint_worst,
                "fragment_bytes": one.fragment_bytes,
                "wire_bytes": one.wire_bytes,
                "fragment_layers": one.fragment_layers,
                "phases": one.phases(),
                "behind": one.behind(),
                "client": one.client(),
            }
            for one in summarise(entries)
        ],
    }


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

PAGE_CSS = """
body{background:#14161a;color:#e6e6e6;font:14px/1.45 "Segoe UI",system-ui,
     sans-serif;margin:0;padding:1.2rem 1.6rem}
h1{font-size:18px;margin:0 0 .2rem}
h2{font-size:15px;margin:1.6rem 0 .5rem;color:#cfe3ff}
h3{font-size:13px;margin:1rem 0 .3rem;color:#9aa4ae;font-weight:600}
code,pre{font-family:Consolas,"Courier New",monospace}
pre.cmd{background:#0e1013;border:1px solid #2b3240;border-radius:6px;
        padding:.6rem .8rem;overflow-x:auto;color:#cfe3ff}
table{border-collapse:collapse;margin:.3rem 0 .8rem;font-size:13px}
th,td{border:1px solid #2b3240;padding:.25rem .55rem;text-align:right}
th{background:#1b1f27;color:#9aa4ae;font-weight:600}
td.k,th.k{text-align:left}
tr.better td.d{color:#7ee787}
tr.worse td.d{color:#ff7b72}
.bar{height:10px;background:#79b8ff;border-radius:2px;display:inline-block;
     vertical-align:middle}
.bar.b2{background:#7ee787}
.note{color:#9aa4ae;font-size:12px;margin:.2rem 0 .8rem}
.legend{color:#9aa4ae;font-size:12px}
"""


def _num(value, digits: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def _bar(value, widest, css: str = "bar") -> str:
    """One bar, scaled to the widest phase of its table and CLAMPED to it.

    The clamp is for the rows below the chain - the tiers behind the first
    paint are seconds where the phases are milliseconds, and an unclamped bar
    for a ten-second fill would be three thousand pixels of nothing.
    """
    if not value or not widest:
        return ""
    width = min(240, max(2, int(round(240.0 * value / widest))))
    return f'<span class="{css}" style="width:{width}px"></span>'


def _kind_rows(reports) -> list:
    """Every kind seen in any report, in the first report's order."""
    order, seen = [], set()
    for _, summaries in reports:
        for one in summaries:
            if one.kind not in seen:
                seen.add(one.kind)
                order.append(one.kind)
    return order


def page(reports, title: str = "panel builder - interaction latency",
         command: str = "") -> str:
    """One self-contained page comparing one or more labelled reports.

    *reports* is `[(label, [KindSummary, ...]), ...]`. Two of them is the
    before-and-after a reviewer reads; one is what the CLI writes for a
    session. No external reference of any kind: one file, openable from
    anywhere, mailable.
    """
    by_label = [
        (label, {one.kind: one for one in summaries}) for label, summaries in reports
    ]
    out = [
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>",
        f"<title>{html.escape(title)}</title>",
        f"<style>{PAGE_CSS}</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        '<p class=note>Every number is measured, from one session journal '
        "each. <b>reply</b> is what the server took to answer the click; "
        "<b>first paint</b> is click to painted as the page itself measured "
        "it; the phase split is the median of each named phase.</p>",
    ]
    if command:
        out.append("<h3>run this on any session folder</h3>")
        out.append(f"<pre class=cmd>{html.escape(command)}</pre>")

    out.append("<h2>reply, first paint and fragment size</h2>")
    head = ["<table><tr><th class=k>interaction kind</th>"]
    for label, _ in by_label:
        head.append(
            f"<th>{html.escape(label)}<br>n</th>"
            f"<th>{html.escape(label)}<br>reply ms</th>"
            f"<th>{html.escape(label)}<br>worst ms</th>"
            f"<th>{html.escape(label)}<br>paint ms</th>"
            f"<th>{html.escape(label)}<br>first KB</th>"
            f"<th>{html.escape(label)}<br>all KB</th>"
        )
    head.append("</tr>")
    out.append("".join(head))
    for kind in _kind_rows(reports):
        cells = [f"<tr><td class=k>{html.escape(kind)}</td>"]
        for _, found in by_label:
            one = found.get(kind)
            if one is None:
                cells.append("<td>-</td>" * 6)
                continue
            size, whole = one.fragment_bytes, one.wire_bytes
            cells.append(
                f"<td>{one.count}</td>"
                f"<td class=d>{_num(one.reply_median)}</td>"
                f"<td>{_num(one.reply_worst)}</td>"
                f"<td class=d>{_num(one.paint_median)}</td>"
                f"<td class=d>{_num(None if size is None else size / 1024.0)}</td>"
                f"<td>{_num(None if whole is None else whole / 1024.0)}</td>"
            )
        cells.append("</tr>")
        out.append("".join(cells))
    out.append("</table>")

    out.append("<h2>phase split</h2>")
    described = dict(SERVER_PHASES)
    for kind in _kind_rows(reports):
        out.append(f"<h3>{html.escape(kind)}</h3>")
        rows = ["<table><tr><th class=k>phase</th>"]
        for label, _ in by_label:
            rows.append(f"<th>{html.escape(label)} ms</th><th></th>")
        rows.append("<th class=k>what it is</th></tr>")
        widest = 0.0
        for _, found in by_label:
            one = found.get(kind)
            if one is not None and one.phases():
                widest = max(widest, max(one.phases().values()))
        for name, _ in SERVER_PHASES:
            values = [
                (found.get(kind).phases().get(name)
                 if found.get(kind) is not None else None)
                for _, found in by_label
            ]
            if all(value is None for value in values):
                continue
            rows.append(f"<tr><td class=k>{html.escape(name)}</td>")
            for index, value in enumerate(values):
                css = "bar" if index == 0 else "bar b2"
                rows.append(
                    f"<td>{_num(value, 1)}</td><td>{_bar(value, widest, css)}</td>"
                )
            rows.append(
                f'<td class="k legend">{html.escape(described.get(name, ""))}</td></tr>'
            )
        # ...and the two halves nobody waits for, stated so the trade is visible.
        for group, names in (("behind the first paint", BEHIND_PHASES),
                             ("the page's own half", CLIENT_PHASES)):
            shown = False
            for name, said in names:
                reader = "behind" if group.startswith("behind") else "client"
                values = []
                for _, found in by_label:
                    one = found.get(kind)
                    table_of = (one.behind() if reader == "behind"
                                else one.client()) if one is not None else {}
                    values.append(table_of.get(name))
                if all(value is None for value in values):
                    continue
                if not shown:
                    shown = True
                    span = 1 + 2 * len(by_label) + 1
                    rows.append(
                        f'<tr><td class=k colspan={span}><i>{html.escape(group)}'
                        f"</i></td></tr>"
                    )
                rows.append(f"<tr><td class=k>{html.escape(name)}</td>")
                for index, value in enumerate(values):
                    css = "bar" if index == 0 else "bar b2"
                    rows.append(
                        f"<td>{_num(value, 1)}</td>"
                        f"<td>{_bar(value, widest, css)}</td>"
                    )
                rows.append(
                    f'<td class="k legend">{html.escape(said)}</td></tr>'
                )
        rows.append("</table>")
        out.append("".join(rows))
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

USAGE = (
    'python -m eedesignpanels.preview.timing "<session folder>"'
)


def main(argv: list = None) -> int:
    """Print the timing table for one session folder."""
    parser = argparse.ArgumentParser(
        prog="eedesignpanels.preview.timing",
        description="Where an interaction's time went, from a session journal.",
    )
    parser.add_argument("session", help="a session folder, or its journal.jsonl")
    parser.add_argument("--html", default="",
                        help="also write the report as one self-contained page")
    parser.add_argument("--json", default="",
                        help="also write the report as data")
    parser.add_argument("--label", default="measured",
                        help="what to call this session's column in the page")
    args = parser.parse_args(argv)

    try:
        records = read_journal(args.session)
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    entries = interactions(records)
    if not entries:
        print(f"no interactions in {args.session} - nothing was driven in it",
              file=sys.stderr)
        return 1
    print(table(entries, title=f"{Path(args.session).name} - "
                               f"{len(entries)} interaction(s)"))
    if args.json:
        Path(args.json).write_text(
            json.dumps(jsonable(entries), indent=2), encoding="utf-8"
        )
        print(f"data   {args.json}")
    if args.html:
        Path(args.html).write_text(
            page([(args.label, summarise(entries))],
                 command=f'{USAGE.replace("<session folder>", str(args.session))}'),
            encoding="utf-8",
        )
        print(f"page   {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
