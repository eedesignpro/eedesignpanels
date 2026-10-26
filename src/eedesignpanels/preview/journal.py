"""The session folder and its append-only journal: what happened, in order.

A reviewer reads a picture faster than anyone can describe one, and types far
slower than that. So a UI session records ITSELF: every server action, every
interaction the page can see, every snapshot the user takes and every short
note the user types land in one folder, and the folder is the deliverable a
later reader works from.

One launch, one folder, and it lives WITH THE PROJECT - `<project>/panel/logs/`,
which the KiCad project template can ignore in version control. That is where
an engineer running the tool looks for what a session did::

    <project>/panel/logs/YYYYMMDD_HHMMSS_ui_session/
    |-- journal.jsonl            one JSON object per line, append-only
    |-- config_at_launch.json    the config exactly as the session started
    |-- shots/                   NNN_<viewport>.svg / .png / NNN_state.json
    +-- render/, svg/            the session's own render scratch

**JSON Lines, and nothing cleverer.** A line is a complete record, so a reader
that stops halfway has every event up to that point - which is what a journal
being useful during a crash means. Records are SELF-DESCRIBING (`schema`,
`seq`, `at`, `kind`, `data`), append-only, and never rewritten, so the later
replay feature (DEFERRED - none of it is built here) can consume the file
forwards without knowing what these events meant when they were written.

Nothing secret goes in. The events are board geometry and config numbers, and
:func:`safe` redacts anything whose key looks like a credential anyway, because
a journal is written by a machine and read by a person much later.
"""

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# What a reader should look for on line 1 to know what it is holding. Bump the
# number when a record stops meaning what it meant.
SCHEMA = "eedesignpanels.ui-journal/1"

JOURNAL_NAME = "journal.jsonl"
SHOTS_DIRNAME = "shots"
CONFIG_COPY_NAME = "config_at_launch.json"
SESSION_SUFFIX = "_ui_session"

# Where a session folder goes inside the folder the builder owns, and the one
# environment variable that overrides it - the same name the review and prep
# pages read, spelled again here rather than imported so that this module keeps
# depending on nothing.
SESSION_DIRNAME = "logs"
SCRATCH_ENV = "EEDESIGNPANELS_SCRATCH"

# How many same-second launches one folder name will make room for before the
# launch says so instead. A guard against a spin, never a working limit.
MAX_SESSION_ATTEMPTS = 100

# A journal is machine-written and human-read, months later. Truncate rather
# than let one enormous value bury the line it is on.
MAX_STRING = 4096
MAX_ITEMS = 200
MAX_DEPTH = 6

# Redacted by KEY, not by looking at values: a token that reached here would be
# a mistake somewhere else, and this is the cheap net under it.
SECRET_KEY = re.compile(
    r"tok(en)?|secret|password|passwd|credential|api[_-]?key|authorization"
    r"|private[_-]?key|session[_-]?id",
    re.I,
)
REDACTED = "[redacted]"

_SHOT_NAME = re.compile(r"^(\d{3,})_")


def utc_now() -> str:
    """An ISO-8601 timestamp in UTC, milliseconds, `Z`-suffixed. Sorts as text."""
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def safe(value, depth: int = 0):
    """*value*, reduced to something JSON can hold and a person can read.

    Redacts credential-looking keys, truncates long strings and long lists,
    stops at :data:`MAX_DEPTH`, and falls back to a short `repr` for anything
    else - a journal line must never be the thing that fails.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN and inf are not JSON; they are also a real answer a stage can
        # produce, so they are kept as their spelling rather than dropped.
        return value if value == value and abs(value) != float("inf") else repr(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "...[cut]"
    if isinstance(value, Path):
        return str(value)
    if depth >= MAX_DEPTH:
        return f"[depth limit: {type(value).__name__}]"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:MAX_ITEMS]:
            name = str(key)
            out[name] = REDACTED if SECRET_KEY.search(name) else safe(item, depth + 1)
        if len(value) > MAX_ITEMS:
            out["...[cut]"] = len(value) - MAX_ITEMS
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [safe(item, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"...[cut {len(items) - MAX_ITEMS} more]")
        return out
    return safe(repr(value), depth + 1)


class UiSession:
    """One launch's folder, its journal, and its shot counter.

    Every append takes the lock and flushes: the server is threaded, and a
    journal that loses its tail when the process is killed is a journal nobody
    trusts. `fsync` is deliberately NOT called - flushing puts the bytes in the
    OS's hands, which survives the process dying, and a per-event fsync would
    cost more than the events are worth.
    """

    def __init__(self, folder: Path, config_path: Path = None):
        self.folder = Path(folder)
        self.config_path = Path(config_path) if config_path else None
        self.folder.mkdir(parents=True, exist_ok=True)
        self.shots_dir = self.folder / SHOTS_DIRNAME
        self.shots_dir.mkdir(exist_ok=True)
        self.journal_path = self.folder / JOURNAL_NAME
        self.journal_path.touch()
        self._lock = threading.Lock()
        self._seq = 0
        self._shot = _highest_shot(self.shots_dir)
        self._started = time.monotonic()
        self._closed = False

    # -- the journal -------------------------------------------------------

    def append(self, kind: str, **data) -> dict:
        """Write one record. Returns it, so a caller can echo what it logged."""
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "at": utc_now(),
                "since_start_ms": round(
                    (time.monotonic() - self._started) * 1000.0, 1
                ),
                "kind": str(kind),
                "data": safe(data),
            }
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            return record

    def records(self) -> list:
        """Every record written so far. For tests and for a later replay."""
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    # -- shot numbers ------------------------------------------------------

    def next_shot(self) -> int:
        """Claim the next shot number. Monotonic, and never reused."""
        with self._lock:
            self._shot += 1
            return self._shot

    @property
    def last_shot(self) -> int:
        """The newest shot number claimed, or 0 when nothing has been saved."""
        return self._shot

    def shot_path(self, shot: int, suffix: str) -> Path:
        """Where one of a shot's files goes: `shots/NNN_<suffix>`."""
        return self.shots_dir / f"{shot:03d}_{suffix}"

    # -- lifecycle ---------------------------------------------------------

    def close(self, reason: str = "") -> None:
        """Record the end of the session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.append("session.end", reason=reason, shots=self._shot,
                    events=self._seq)


def _highest_shot(shots_dir: Path) -> int:
    """The largest `NNN_` already in the shots folder, so a reused folder is safe."""
    highest = 0
    if not shots_dir.is_dir():
        return highest
    for entry in shots_dir.iterdir():
        found = _SHOT_NAME.match(entry.name)
        if found:
            highest = max(highest, int(found.group(1)))
    return highest


def session_root(config) -> Path:
    """Where session folders go: `<project>/panel/logs`, or the override.

    The session folder belongs to the PROJECT, next
    to the panel the session is about, in a folder the KiCad project template
    can ignore in version control. `EEDESIGNPANELS_SCRATCH` still wins over it,
    because an explicit answer always does - that is what a test and a CI runner
    point somewhere disposable.
    """
    override = os.environ.get(SCRATCH_ENV)
    if override:
        return Path(override)
    return Path(config.panel_dir) / SESSION_DIRNAME


def session_folder(root: Path, stamp: str = None) -> Path:
    """`<root>/YYYYMMDD_HHMMSS_ui_session`, the name a launch asks for first."""
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    return Path(root) / f"{stamp}{SESSION_SUFFIX}"


def claim_session_folder(root: Path, stamp: str = None) -> Path:
    """Create the one folder this launch owns, and no other launch can.

    **TWO LAUNCHES IN ONE SECOND GET TWO FOLDERS.** The name is a timestamp to
    the second and nothing else, so without a claim two sessions started
    together would interleave their records in one journal. The second one
    takes `..._ui_session_2`, and the next `_3`: a suffix keeps
    the name readable and keeps the folders sorted by the time they started,
    which a random string would not.

    The claim is the `mkdir` ITSELF, with `exist_ok=False`. Creating a directory
    is atomic in the filesystem, so the loser of a race gets `FileExistsError`
    and moves to the next name - where a "does it exist?" test followed by a
    `mkdir` would let both processes pass the test and share the folder.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = session_folder(root, stamp)
    for attempt in range(1, MAX_SESSION_ATTEMPTS + 1):
        folder = base if attempt == 1 else Path(f"{base}_{attempt}")
        try:
            folder.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return folder
    raise FileExistsError(
        f"{MAX_SESSION_ATTEMPTS} session folders named {base.name} already "
        f"exist in {root} - nothing was started"
    )


def open_session(config_path: Path, root: Path, stamp: str = None,
                 **header) -> UiSession:
    """Create the session folder, copy the config into it, open the journal.

    The config COPY is what makes the folder answer "what was this run asked
    to do" without trusting that the file on disk still says it - a preview
    click can rewrite `panel.json` mid-session, which is the point of having
    the copy.
    """
    session = UiSession(claim_session_folder(root, stamp), config_path)
    copied = None
    if session.config_path and session.config_path.is_file():
        copied = session.folder / CONFIG_COPY_NAME
        shutil.copyfile(session.config_path, copied)
    session.append(
        "session.start",
        schema=SCHEMA,
        folder=str(session.folder),
        journal=JOURNAL_NAME,
        shots=SHOTS_DIRNAME,
        config=str(session.config_path) if session.config_path else "",
        config_copy=CONFIG_COPY_NAME if copied else "",
        pid=os.getpid(),
        **header,
    )
    return session
