"""One bug report: everything needed to troubleshoot somebody else's board.

The report key and the report button build a problem report, not just a
snapshot. A snapshot answers "what was on screen"; a report has to answer "why
did it do that on this machine", and the difference is the board itself, the
settings that found its footprints, and the record of what the user pressed.

One zip, written on this computer and nowhere else. The default folder is
`bug_reports/` inside the panel folder, beside the design the report describes
(:func:`report_folder`). The environment variable named by
:data:`REPORT_FOLDER_VARIABLE` names another local folder. The browser gets a
copy only when the person asks to save one somewhere else::

    <project>_bug_report_<YYYYMMDD_HHMMSS>.zip
    |-- summary.json           the short version: what failed, on what, with what
    |-- manifest.json          what went in, what was absent, what was skipped
    |-- note.txt               the one line the user typed, if any
    |-- versions.json          eedesignpanels, eedesigncore, kicad-cli, the OS
    |-- project/               the source board, its project file, the config
    |-- panel/                 the saved panel board and project, when present
    |-- session/               journal.jsonl, the config as the session started
    |-- backend/               the RUNNING session: live settings, timing, state
    |-- view/                  the shot this report took: SVG, PNG, state JSON
    |-- reports/               prep warnings, panel report, checks, comparison
    +-- kicad/                 the library tables and the path variables

The same summary is also written beside the zip, as
`<project>_bug_report_<YYYYMMDD_HHMMSS>.summary.json`, so a person or a coding
agent can read what failed without opening the zip first.

**THE ZIP CARRIES BOTH COPIES OF EVERY SETTING.** The values a user has typed
are held in memory until a save, so the file on disk alone cannot answer a
question such as whether a rail score setback moved the tooling holes. So the
settings go in twice, under names that say which is which and never require
the reader to guess:

* `project/panel.json`                      what is ON DISK, unchanged in place
* `backend/settings_live_session.json`      what the RUNNING SESSION holds

**When the two differ, that difference IS the bug report.** The manifest says
whether they differ and how many settings do (`settings` block), and
`backend/settings_difference.txt` lists each one with both values.

**THE BACKEND FOLDER IS THE REST OF THE LIVE SESSION.** The report is the only
way to debug a problem on another user's machine, so it must be complete.
Alongside the live settings it carries the
list of edits made and not saved, the reasons the panel picture is out of date,
the interaction timing table read back out of the journal
(:mod:`eedesignpanels.preview.timing`), and the whole backend state the page was
built from. The journal itself is collected COMPLETE under `session/` - every
record, never a tail - and the version facts stay at `versions.json` rather
than being written a second time here.

**The manifest is the honest part.** A machine with no KiCad installed still
produces a report; the tables are simply listed under `absent` with the reason.
A reader who finds no `kicad/fp-lib-table` must be able to tell "this machine
had none" from "the collector forgot", and that is what the manifest is for.

**What is collected, and nothing else.** The project folder, the session
folder, and the KiCad settings files named here - the global library tables,
the tables those include, and the `environment` block of `kicad_common.json`.
A library table is a short text file listing where libraries live, which is
exactly the path setup a footprint that resolved differently needs; the
libraries themselves are never read. No credential, and no environment variable
beyond the KiCad path block: :func:`eedesignpanels.preview.journal.safe`
redacts a credential-shaped key in the journal. The one environment variable
this module reads is :data:`REPORT_FOLDER_VARIABLE`, which says where to write.
"""

import json
import os
import platform
import re
import subprocess  # noqa: S404 - one fixed `kicad-cli --version`, no shell
import sys
import time
import zipfile
from pathlib import Path
from typing import NamedTuple

import eedesigncore
from eedesigncore.tables import (
    LibTable,
    LibTableEnvironment,
    default_user_settings_dir,
)

from eedesignpanels import __version__
from eedesignpanels.config import CONFIG_REVISION
from eedesignpanels.preview import journal as journal_module
from eedesignpanels.review import render

SCHEMA = "eedesignpanels.bug-report/1"

# EVERY REPORT STAYS ON THIS COMPUTER. The report is for the person who pressed
# the button, or for a coding agent they point at it, to read and debug. The
# server writes nothing to a network folder and sends nothing anywhere. This
# variable names another LOCAL folder. A network share is refused by name.
REPORT_FOLDER_VARIABLE = "EEDESIGNPANELS_BUG_REPORT_DIR"

# WHAT THE REPORT HOLDS. The dialog says it and the manifest records it,
# because the zip carries the person's own design files.
PRIVACY_LINE = (
    "This report holds your board files, your panel settings and the file "
    "paths on this machine. It holds no passwords. It stays on this computer."
)

# WHERE THE BROWSER FETCHES THE ZIP FROM. One fixed route serving the one file
# this session built last: the page never names a path, so there is nothing to
# traverse (the `/check/report` rule, same reason).
DOWNLOAD_PATH = "/bug_report/download"

REPORT_FOLDER_NAME = "bug_reports"
MANIFEST_NAME = "manifest.json"
SUMMARY_NAME = "summary.json"
SUMMARY_SCHEMA = "eedesignpanels.bug-report-summary/1"
# How many differing settings the summary names before it points at the full
# difference file inside the zip.
MAX_SUMMARY_DIFFERENCES = 40
NOTE_NAME = "note.txt"
VERSIONS_NAME = "versions.json"

# THE TWO COPIES OF THE SETTINGS, NAMED SO THE DIFFERENCE IS OBVIOUS FROM THE
# FILE NAME ALONE. The disk copy sits where the project keeps it, because a
# reader who knows the project layout already looks for it there.
DISK_SETTINGS_MEMBER = "project/panel.json"
LIVE_SETTINGS_NAME = "backend/settings_live_session.json"
SETTINGS_DIFFERENCE_NAME = "backend/settings_difference.txt"
PENDING_NAME = "backend/pending_changes.txt"
STALE_NAME = "backend/stale_reasons.txt"
BACKEND_STATE_NAME = "backend/backend_state.json"
TIMING_TABLE_NAME = "backend/timing_table.txt"
TIMING_JSON_NAME = "backend/timing_interactions.json"

# How many differing settings the difference file lists before it stops. A
# config with more than this is not two settings apart, it is a different
# config, and the two whole files are in the zip for that case.
MAX_DIFFERENCE_ROWS = 400

# Size sanity. A board is about 2 MB and a session's renders are a few MB, so
# one member over this is not one of the pieces this was built for - it is
# skipped BY NAME in the manifest rather than quietly dropped.
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# A library table is a list of paths. Anything larger is not a table.
MAX_TABLE_BYTES = 1024 * 1024
# How far a nested library table chain is followed, and how many files in all.
MAX_TABLE_DEPTH = 6
MAX_TABLE_FILES = 32
# The user's own line, kept whole up to here.
MAX_NOTE_CHARS = 1000
# How long `kicad-cli --version` is given before its answer is "it did not say".
CLI_VERSION_TIMEOUT_S = 20.0

_UNSAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


class ReportError(Exception):
    """The report cannot be built. Nothing was written."""


class Piece(NamedTuple):
    """One thing that goes in the zip: where it comes from and what it is."""

    name: str
    kind: str
    path: Path = None
    data: bytes = None


class Missing(NamedTuple):
    """One thing that did NOT go in, and why. This is the honest half."""

    name: str
    kind: str
    reason: str


class Report(NamedTuple):
    """What one report put on disk."""

    path: Path
    name: str
    size_bytes: int
    manifest: dict
    summary_path: Path = None

    @property
    def summary(self) -> str:
        """The sentence the page shows once the zip exists."""
        collected = len(self.manifest.get("collected", []))
        absent = len(self.manifest.get("absent", []))
        skipped = len(self.manifest.get("skipped", []))
        line = (
            f"bug report ready - {collected} file(s), "
            f"{self.size_bytes / 1024.0 / 1024.0:.1f} MB"
        )
        if absent:
            line += f", {absent} absent"
        if skipped:
            line += f", {skipped} skipped for size"
        return line


# ---------------------------------------------------------------------------
# where the report goes
# ---------------------------------------------------------------------------


def report_folder(config, environ=None) -> Path:
    """The local folder a report is written to.

    The default is `bug_reports/` inside the panel folder, so the report sits
    next to the design it describes. :data:`REPORT_FOLDER_VARIABLE` names
    another folder, and a relative name is taken from the panel folder. A
    network share or a URL is refused: a report holds the person's design
    files, and it never leaves this computer.
    """
    source = os.environ if environ is None else environ
    named = str(source.get(REPORT_FOLDER_VARIABLE) or "").strip()
    panel_dir = Path(config.panel_dir)
    if not named:
        return panel_dir / REPORT_FOLDER_NAME
    if named.startswith(("\\\\", "//")) or "://" in named:
        raise ReportError(
            f"{REPORT_FOLDER_VARIABLE} names {named!r}, which is not a local "
            f"folder. A report stays on this computer: name a folder on a "
            f"local drive, or clear the variable"
        )
    folder = Path(named).expanduser()
    return folder if folder.is_absolute() else panel_dir / folder


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def report_stem(board_path, stamp: str = None) -> str:
    """`<project>_bug_report_<YYYYMMDD_HHMMSS>` - the browser's default name.

    The project's own name first, because the person who opens the zip a week
    later is looking for a board, not for a timestamp.
    """
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    stem = Path(board_path).stem if board_path else "panel"
    stem = _UNSAFE_STEM.sub("_", stem).strip("_") or "panel"
    return f"{stem[:64]}_bug_report_{stamp}"


# ---------------------------------------------------------------------------
# the version facts
# ---------------------------------------------------------------------------


def kicad_cli_version(cli_path=None) -> dict:
    """What `kicad-cli --version` says here, or why it said nothing.

    A report from a machine with no KiCad is a report this has to survive, so
    an absent binary is a recorded answer rather than an exception.
    """
    path = Path(cli_path or render.DEFAULT_KICAD_CLI)
    if not path.is_file():
        return {"path": str(path), "version": "", "found": False,
                "reason": "no kicad-cli at this path"}
    try:
        done = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
            [str(path), "--version"],
            capture_output=True, text=True, timeout=CLI_VERSION_TIMEOUT_S,
            creationflags=render.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The specific recovery is the point: the report says what happened and
        # goes on being a report.
        return {"path": str(path), "version": "", "found": True,
                "reason": f"{type(exc).__name__}: {exc}"}
    text = (done.stdout or done.stderr or "").strip()
    return {"path": str(path), "version": text[:200], "found": True,
            "returncode": done.returncode, "reason": ""}


def version_facts(cli_path=None) -> dict:
    """Every version a reader needs before they believe a single other file."""
    return {
        "eedesignpanels": __version__,
        "eedesigncore": getattr(eedesigncore, "__version__", ""),
        "kicad_cli": kicad_cli_version(cli_path),
        "python": platform.python_version(),
        "interpreter": sys.executable,
        "os": platform.platform(),
        "machine": platform.machine(),
    }


# ---------------------------------------------------------------------------
# the KiCad settings
# ---------------------------------------------------------------------------


def _table_pieces(start: Path, prefix: str, kind: str, environment,
                  pieces: list, missing: list, seen: set, depth: int = 0) -> None:
    """*start* and every table it includes, appended to *pieces*.

    A row whose `type` is the literal `Table` names another table file, which
    KiCad loads into the same namespace (eedesigncore's resolver documents the
    rule against KiCad 10.0.3). Those rows are exactly the path setup a
    footprint that resolved differently on another machine needs, so the chain
    is followed - and only the chain. A library's own contents are never read.
    """
    start = Path(start)
    key = str(start).lower()
    if key in seen or depth > MAX_TABLE_DEPTH or len(seen) >= MAX_TABLE_FILES:
        return
    seen.add(key)
    # A nested table carries a running number, because two tables included from
    # different places are routinely both called `fp-lib-table` and a member
    # name that collided would silently drop the second one.
    name = (f"{prefix}.included/{len(seen):02d}_{start.name}" if depth
            else prefix)
    if not start.is_file():
        missing.append(Missing(name, kind, f"no file at {start}"))
        return
    size = start.stat().st_size
    if size > MAX_TABLE_BYTES:
        missing.append(Missing(name, kind,
                               f"{size} bytes is not a library table"))
        return
    pieces.append(Piece(name, kind, path=start))
    try:
        table = LibTable.from_file(start)
    except (OSError, ValueError) as exc:
        # A table this cannot parse is still collected above; what is lost is
        # the chain below it, and the manifest says so.
        missing.append(Missing(f"{name} (included tables)", kind,
                               f"{type(exc).__name__}: {exc}"))
        return
    for row in table.rows:
        if row.type.lower() != "table" or row.disabled:
            continue
        expanded = environment.expand(row.uri)
        nested = Path(expanded)
        if not nested.is_absolute():
            nested = start.parent / nested
        _table_pieces(nested, prefix, kind, environment,
                      pieces, missing, seen, depth + 1)


def kicad_settings_pieces(settings_dir=None, project_dir=None) -> tuple:
    """The library tables and the path variables. `(pieces, missing)`.

    THE MACHINE MAY HAVE NO KICAD. Every absence is a `Missing` with a reason,
    so the zip is still a zip and the manifest still tells the truth.
    """
    pieces, missing = [], []
    folder = Path(settings_dir) if settings_dir else default_user_settings_dir()
    if not folder.is_dir():
        return pieces, [
            Missing("kicad/fp-lib-table", "lib_table",
                    f"no KiCad settings folder at {folder}"),
            Missing("kicad/sym-lib-table", "lib_table",
                    f"no KiCad settings folder at {folder}"),
            Missing("kicad/kicad_common.environment.json", "kicad_paths",
                    f"no KiCad settings folder at {folder}"),
        ]

    try:
        environment = LibTableEnvironment.discover(
            settings_dir=folder,
            project_dir=Path(project_dir) if project_dir else None,
        )
    except (OSError, ValueError) as exc:
        environment = LibTableEnvironment(variables={})
        missing.append(Missing("kicad/environment", "kicad_paths",
                               f"{type(exc).__name__}: {exc}"))

    seen = set()
    for table_name in ("fp-lib-table", "sym-lib-table"):
        _table_pieces(folder / table_name, f"kicad/{table_name}", "lib_table",
                      environment, pieces, missing, seen)
    if project_dir:
        for table_name in ("fp-lib-table", "sym-lib-table"):
            _table_pieces(
                Path(project_dir) / table_name,
                f"kicad/project/{table_name}", "lib_table",
                environment, pieces, missing, seen,
            )

    pieces_common, missing_common = _kicad_paths_piece(folder)
    pieces.extend(pieces_common)
    missing.extend(missing_common)
    return pieces, missing


def _kicad_paths_piece(folder: Path) -> tuple:
    """ONLY the `environment` block of `kicad_common.json`, never the file.

    That file is a hundred kilobytes of window positions, recent projects and
    session state; the path variables are a few hundred bytes of it and are the
    only part that explains a library that resolved differently. Taking the
    block rather than the file is both the smaller artifact and the narrower
    collection: nothing of the environment beyond the KiCad paths is taken.
    """
    path = folder / "kicad_common.json"
    name = "kicad/kicad_common.environment.json"
    if not path.is_file():
        return [], [Missing(name, "kicad_paths", f"no file at {path}")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [Missing(name, "kicad_paths", f"{type(exc).__name__}: {exc}")]
    block = data.get("environment") if isinstance(data, dict) else None
    body = json.dumps(
        {"source": str(path), "environment": block if block is not None else {}},
        indent=2, sort_keys=True, ensure_ascii=False,
    ) + "\n"
    return [Piece(name, "kicad_paths", data=body.encode("utf-8"))], []


# ---------------------------------------------------------------------------
# the project, the session and the shot
# ---------------------------------------------------------------------------


def project_pieces(config) -> tuple:
    """The board, its project file, the panel config, and the saved panel."""
    pieces, missing = [], []
    wanted = [
        (f"project/{config.source_board_path.name}", "board",
         config.source_board_path),
        (f"project/{config.source_project_path.name}", "project",
         config.source_project_path),
        (f"project/{config.config_path.name}", "config", config.config_path),
        (f"panel/{config.output_board_path.name}", "panel_board",
         config.output_board_path),
        (f"panel/{config.output_project_path.name}", "panel_project",
         config.output_project_path),
    ]
    for name, kind, path in wanted:
        if path.is_file():
            pieces.append(Piece(name, kind, path=path))
        else:
            missing.append(Missing(name, kind, f"no file at {path}"))
    return pieces, missing


def session_pieces(journal, shot: int = 0) -> tuple:
    """The journal, the config as the session started, and the shot's files."""
    pieces, missing = [], []
    if journal is None:
        return pieces, [Missing("session/journal.jsonl", "journal",
                                "this session has no folder")]
    if journal.journal_path.is_file():
        pieces.append(Piece(f"session/{journal_module.JOURNAL_NAME}", "journal",
                            path=journal.journal_path))
    else:
        missing.append(Missing(f"session/{journal_module.JOURNAL_NAME}",
                               "journal", "the journal was never opened"))
    started = journal.folder / journal_module.CONFIG_COPY_NAME
    if started.is_file():
        pieces.append(Piece(f"session/{journal_module.CONFIG_COPY_NAME}",
                            "config_at_launch", path=started))
    else:
        missing.append(Missing(f"session/{journal_module.CONFIG_COPY_NAME}",
                               "config_at_launch",
                               "the session copied no config"))
    if not shot:
        missing.append(Missing("view/", "shot", "this report took no shot"))
        return pieces, missing
    found = sorted(journal.shots_dir.glob(f"{shot:03d}_*"))
    if not found:
        missing.append(Missing("view/", "shot",
                               f"shot {shot:03d} wrote no files"))
    for path in found:
        pieces.append(Piece(f"view/{path.name}", "shot", path=path))
    return pieces, missing


def _bullets(items) -> list:
    """One line per entry, or the word `none` - never a blank heading."""
    lines = [f"  - {one}" for one in (items or [])]
    return lines or ["  none"]


def failure_place(at) -> str:
    """The coordinate a failed board load named, in millimetres, or a reason not.

    The zip says where. A failure that named no place says so
    in words, because a blank line reads as a field nobody filled in.
    """
    if not at:
        return "no coordinate was named"
    try:
        point = [float(value) for value in at]
    except (TypeError, ValueError):
        return f"unreadable: {at!r}"
    if len(point) != 2:
        return f"unreadable: {at!r}"
    return f"{point[0]:.4f}, {point[1]:.4f} mm"


def failure_block(backend_state: dict) -> dict:
    """The manifest's answer to "was there a board on screen": yes, or why not."""
    state = backend_state if isinstance(backend_state, dict) else {}
    results = state.get("results")
    results = results if isinstance(results, dict) else {}
    at = results.get("error_at")
    return {
        "prepared": bool(results.get("ok")),
        "error": results.get("error", "") or "",
        "error_at": list(at) if at else None,
        "where": failure_place(at),
        "says": "prepared is false when the viewport carried the failure "
                "instead of the board",
    }


def report_pieces(backend_state: dict, check_report=None) -> tuple:
    """The prep and panel reports and their warnings, as text a person reads.

    The same numbers are in the shot's state file. They are repeated here in
    the shape a reader scans first, because the first question asked of a bug
    report is "what did it complain about".
    """
    pieces, missing = [], []
    state = backend_state if isinstance(backend_state, dict) else {}
    results = state.get("results") if isinstance(state.get("results"), dict) else {}
    panel = state.get("panel") if isinstance(state.get("panel"), dict) else {}

    # WHAT FAILED AND WHERE, in that order. The
    # report a stuck engineer sends is the report of a board that would not
    # prepare, so the reason and the coordinate the stage named lead this file.
    # `prepared` is stated out loud because an empty error line reads the same
    # on a board that worked and on one whose failure nothing recorded.
    lines = [
        f"prepared: {'yes' if results.get('ok') else 'no'}",
        f"message:  {results.get('message', '')}",
        f"error:    {results.get('error', '')}",
        f"where:    {failure_place(results.get('error_at'))}",
        "",
        "warnings:",
        *_bullets(results.get("warnings")),
    ]
    pieces.append(Piece("reports/prep_warnings.txt", "report",
                        data=("\n".join(lines) + "\n").encode("utf-8")))

    panel_lines = [
        f"subtitle: {panel.get('subtitle', '')}",
        "",
        "report:",
        *_bullets(panel.get("report")),
        "",
        "notes:",
        *_bullets(panel.get("notes")),
    ]
    pieces.append(Piece("reports/panel_report.txt", "report",
                        data=("\n".join(panel_lines) + "\n").encode("utf-8")))

    checks = json.dumps(results.get("checks") or {}, indent=2, sort_keys=True,
                        ensure_ascii=False) + "\n"
    pieces.append(Piece("reports/checks.json", "report",
                        data=checks.encode("utf-8")))

    if check_report is not None and Path(check_report).is_file():
        pieces.append(Piece("reports/comparison.html", "comparison",
                            path=Path(check_report)))
    else:
        missing.append(Missing("reports/comparison.html", "comparison",
                               "no comparison has been run in this session"))
    return pieces, missing


# ---------------------------------------------------------------------------
# the running session - the backend folder
# ---------------------------------------------------------------------------


class _Absent:
    """A setting one of the two copies does not carry at all."""

    def __repr__(self) -> str:
        return "(not in this copy)"


_ABSENT = _Absent()


def flatten_settings(data, prefix: str = "") -> dict:
    """One config as `{dotted name: value}`, so two of them can be compared.

    ONE SETTING IS ONE LINE: the count in the manifest has to mean "settings that
    differ", and a nested comparison that reported a whole `panel.scores`
    object as one difference would say `1` where seven numbers had moved. A
    list stays a single value, because its order is part of what it says.
    """
    flat = {}
    if isinstance(data, dict):
        for key in sorted(data, key=str):
            flat.update(flatten_settings(data[key], f"{prefix}{key}."))
        return flat
    flat[prefix.rstrip(".") or "(root)"] = data
    return flat


def settings_difference(on_disk, in_session) -> tuple:
    """`(rows, count)` - every setting whose two copies disagree.

    *rows* is one entry per setting, with the disk value and the session value;
    *count* is what the manifest states. A setting present in one copy and not
    the other IS a difference and says so by name, because a config that gained
    a section is exactly the case where the two files stop lining up.
    """
    left = flatten_settings(on_disk if isinstance(on_disk, dict) else {})
    right = flatten_settings(in_session if isinstance(in_session, dict) else {})
    rows = []
    for name in sorted(set(left) | set(right), key=str):
        there, here = left.get(name, _ABSENT), right.get(name, _ABSENT)
        if there == here and type(there) is type(here):
            continue
        rows.append((name, there, here))
    return rows, len(rows)


def _difference_text(rows, count: int) -> str:
    """The difference file: what disk says, what the running session says."""
    lines = [
        "THE SETTINGS ON DISK AGAINST THE SETTINGS THE RUNNING SESSION HOLDS.",
        "",
        f"on disk:     {DISK_SETTINGS_MEMBER}",
        f"in session:  {LIVE_SETTINGS_NAME}",
        f"differ:      {count} setting(s)",
        "",
        "A setting changed in the session and not saved appears here with the",
        "old value on disk and the value the panel was actually built from.",
        "",
    ]
    if not count:
        lines.append("  none - the session holds exactly what the file holds")
        return "\n".join(lines) + "\n"
    for name, there, here in rows[:MAX_DIFFERENCE_ROWS]:
        lines.append(f"  {name}")
        lines.append(f"      on disk:    {there!r}")
        lines.append(f"      in session: {here!r}")
    if count > MAX_DIFFERENCE_ROWS:
        lines.append("")
        lines.append(
            f"  ... and {count - MAX_DIFFERENCE_ROWS} more - both whole files "
            f"are in this zip"
        )
    return "\n".join(lines) + "\n"


def _listing_text(title: str, why: str, items, empty: str) -> str:
    """One heading, one reason, and a bulleted list - or the word for empty."""
    lines = [title, "", why, ""]
    lines.extend(f"  - {one}" for one in items)
    if not items:
        lines.append(f"  {empty}")
    return "\n".join(lines) + "\n"


def _disk_settings(config) -> tuple:
    """The config file as it stands on disk, or why it could not be read."""
    if config is None:
        return None, "this session holds no configuration"
    path = Path(config.config_path)
    if not path.is_file():
        return None, f"no file at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The specific recovery is the point: the file is collected whole under
        # `project/` either way, so what is lost here is only the comparison.
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "the file on disk is not a settings object"
    return data, ""


def _timing_pieces(journal) -> tuple:
    """The interaction timing table and its numbers, read out of the journal.

    THE JOURNAL IS ALREADY IN THE ZIP; THIS IS THE READING OF IT. A developer
    looking at somebody else's slow session should not have to run a second
    tool to see where the time went, and
    :mod:`eedesignpanels.preview.timing` is stdlib only and reads nothing but
    that one file, so running it here costs the report nothing.
    """
    from eedesignpanels.preview import timing as timing_module

    try:
        records = timing_module.read_journal(journal.folder)
        entries = timing_module.interactions(records)
        table = timing_module.table(entries, title=str(journal.folder))
        numbers = timing_module.jsonable(entries)
    except (OSError, ValueError, KeyError) as exc:
        # The specific recovery is the point: a journal this cannot summarise is
        # still collected whole under `session/`, so what is lost is the table -
        # and the manifest names it.
        return [], [
            Missing(TIMING_TABLE_NAME, "timing", f"{type(exc).__name__}: {exc}"),
            Missing(TIMING_JSON_NAME, "timing", f"{type(exc).__name__}: {exc}"),
        ]
    if not entries:
        return [], [
            Missing(TIMING_TABLE_NAME, "timing",
                    "the journal holds no completed interaction yet"),
            Missing(TIMING_JSON_NAME, "timing",
                    "the journal holds no completed interaction yet"),
        ]
    return [
        Piece(TIMING_TABLE_NAME, "timing",
              data=(table.rstrip("\n") + "\n").encode("utf-8")),
        Piece(TIMING_JSON_NAME, "timing",
              data=(json.dumps(numbers, indent=2, sort_keys=True,
                               ensure_ascii=False) + "\n").encode("utf-8")),
    ], []


def backend_pieces(config, journal, backend_state: dict, session_settings=None,
                   pending=(), stale=()) -> tuple:
    """The RUNNING SESSION, as files. `(pieces, missing, settings_block)`.

    The zip carries the settings TWICE - the file on disk and the copy the
    session holds - plus the edits made and not saved, why the picture is out of date, the timing table
    and the whole backend state. *settings_block* is what the manifest then says
    about the two copies: which member is which, whether they differ, and how
    many settings do.

    *session_settings* is the live configuration dictionary, which is the thing
    a save would write. It is TAKEN and not derived from *config*, because a
    validated model carries defaults the file never named and a comparison
    against those would report differences nobody made.

    Reads no environment variable. The KiCad path block collected under
    `kicad/` remains the whole of what this report knows about the
    environment.
    """
    pieces, missing = [], []
    disk_data, disk_reason = _disk_settings(config)

    if isinstance(session_settings, dict):
        pieces.append(Piece(
            LIVE_SETTINGS_NAME, "config_in_session",
            data=(json.dumps(session_settings, indent=2, sort_keys=True,
                             ensure_ascii=False) + "\n").encode("utf-8"),
        ))
    else:
        missing.append(Missing(
            LIVE_SETTINGS_NAME, "config_in_session",
            "the session holds no configuration dictionary, so there is no "
            "live copy of the settings to set beside the file",
        ))

    count = None
    if isinstance(session_settings, dict) and disk_data is not None:
        rows, count = settings_difference(disk_data, session_settings)
        pieces.append(Piece(
            SETTINGS_DIFFERENCE_NAME, "settings_difference",
            data=_difference_text(rows, count).encode("utf-8"),
        ))
    else:
        missing.append(Missing(
            SETTINGS_DIFFERENCE_NAME, "settings_difference",
            disk_reason or "there is only one copy of the settings to read",
        ))

    pieces.append(Piece(
        PENDING_NAME, "pending_changes",
        data=_listing_text(
            "EVERY EDIT MADE IN THIS SESSION AND NOT YET SAVED.",
            "A pending edit is WHY the settings on disk and the settings in "
            "the session differ.",
            [f"[{one[0]}] {one[1]}" if isinstance(one, (tuple, list))
             else str(one) for one in (pending or [])],
            "nothing is pending - every edit has been saved",
        ).encode("utf-8"),
    ))
    pieces.append(Piece(
        STALE_NAME, "stale_reasons",
        data=_listing_text(
            "WHY THE PANEL PICTURE IS OUT OF DATE.",
            "A stale reason means the drawing under `view/` is older than the "
            "settings beside it.",
            [str(one) for one in (stale or [])],
            "nothing is stale - the picture matches the settings",
        ).encode("utf-8"),
    ))
    # `default=repr` because this is the LAST record of the run: a field that
    # will not serialise costs its own value here and never the whole file
    # (the same ground as `snapshot.jsonable`).
    pieces.append(Piece(
        BACKEND_STATE_NAME, "backend_state",
        data=(json.dumps(backend_state if isinstance(backend_state, dict)
                         else {}, indent=2, sort_keys=True, ensure_ascii=False,
                         default=repr) + "\n").encode("utf-8"),
    ))

    if journal is not None:
        timing_pieces, timing_missing = _timing_pieces(journal)
        pieces.extend(timing_pieces)
        missing.extend(timing_missing)
    else:
        missing.append(Missing(
            TIMING_TABLE_NAME, "timing",
            "this session has no folder, so there is no journal to read",
        ))

    settings_block = {
        "on_disk": {
            "member": DISK_SETTINGS_MEMBER,
            "says": "the settings as the file on disk holds them",
            "path": str(config.config_path) if config is not None else "",
            "read": disk_data is not None,
            "reason": disk_reason,
        },
        "in_session": {
            "member": LIVE_SETTINGS_NAME,
            "says": "the settings as the RUNNING SESSION holds them - what "
                    "the panel in this report was built from",
            "read": isinstance(session_settings, dict),
        },
        "compared": count is not None,
        "differ": bool(count),
        "differing_settings": count if count is not None else -1,
        "difference": SETTINGS_DIFFERENCE_NAME,
    }
    return pieces, missing, settings_block


# ---------------------------------------------------------------------------
# the zip
# ---------------------------------------------------------------------------


def _member_bytes(piece: Piece) -> bytes:
    if piece.data is not None:
        return piece.data
    return piece.path.read_bytes()


def write_zip(destination: Path, pieces: list, missing: list, manifest: dict,
              max_member_bytes: int = MAX_MEMBER_BYTES,
              max_total_bytes: int = MAX_TOTAL_BYTES) -> Report:
    """Write the zip and the manifest inside it. Returns what landed.

    THE MANIFEST IS WRITTEN LAST AND DESCRIBES WHAT ACTUALLY WENT IN. A
    manifest built from the wish list would name a member the cap skipped,
    which is the one way a report can lie to the person reading it.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    collected, skipped, total = [], [], 0
    used = set()

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for piece in pieces:
            if piece.name in used:
                continue
            try:
                body = _member_bytes(piece)
            except OSError as exc:
                # A file that vanished between the listing and the read. Say so
                # and keep collecting: a report is worth more than its last file.
                skipped.append({"name": piece.name, "kind": piece.kind,
                                "reason": f"{type(exc).__name__}: {exc}",
                                "bytes": 0})
                continue
            size = len(body)
            if size > max_member_bytes:
                skipped.append({
                    "name": piece.name, "kind": piece.kind, "bytes": size,
                    "reason": (f"{size / 1048576.0:.1f} MB is over the "
                               f"{max_member_bytes / 1048576.0:.1f} MB cap "
                               f"for one file"),
                })
                continue
            if total + size > max_total_bytes:
                skipped.append({
                    "name": piece.name, "kind": piece.kind, "bytes": size,
                    "reason": (f"the report had already reached its "
                               f"{max_total_bytes / 1048576.0:.1f} MB budget"),
                })
                continue
            archive.writestr(piece.name, body)
            used.add(piece.name)
            total += size
            collected.append({"name": piece.name, "kind": piece.kind,
                              "bytes": size,
                              "source": str(piece.path) if piece.path else ""})

        manifest = {
            **manifest,
            "collected": collected,
            "absent": [
                {"name": one.name, "kind": one.kind, "reason": one.reason}
                for one in missing
            ],
            "skipped": skipped,
            "uncompressed_bytes": total,
        }
        archive.writestr(
            MANIFEST_NAME,
            json.dumps(manifest, indent=2, sort_keys=True,
                       ensure_ascii=False) + "\n",
        )
    return Report(destination, destination.name,
                  destination.stat().st_size, manifest)


def summary_of(config, backend_state: dict, versions: dict, note: str,
               created_at: str, report_name: str, settings_block: dict,
               session_settings=None, difference_rows=()) -> dict:
    """The short version of a report, for a person or a coding agent.

    One small file that answers the first questions: which engine, when, which
    board, which settings, and what the error said. Everything else is in the
    zip, and `read_next` names where.
    """
    state = backend_state if isinstance(backend_state, dict) else {}
    results = state.get("results")
    results = results if isinstance(results, dict) else {}
    rows = list(difference_rows)[:MAX_SUMMARY_DIFFERENCES]
    return {
        "schema": SUMMARY_SCHEMA,
        "engine_version": __version__,
        "eedesigncore_version": versions.get("eedesigncore", ""),
        "created_at": created_at,
        "created_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "report": report_name,
        "note": note,
        "board_file": str(config.source_board_path),
        "settings_file": str(config.config_path),
        "settings_revision": config.revision,
        "settings_in_session": (session_settings
                                if isinstance(session_settings, dict) else None),
        "settings_differ_from_file": bool(settings_block.get("differ")),
        "settings_differences": [
            {"setting": name, "on_disk": repr(there), "in_session": repr(here)}
            for name, there, here in rows
        ],
        "board_prepared": bool(results.get("ok")),
        "error": results.get("error", "") or "",
        "error_where": failure_place(results.get("error_at")),
        "message": results.get("message", "") or "",
        "warnings": [str(one) for one in (results.get("warnings") or [])],
        "read_next": [
            f"{NOTE_NAME}: the line the person typed",
            "reports/prep_warnings.txt: what failed and where",
            f"{SETTINGS_DIFFERENCE_NAME}: every setting changed and not saved",
            "session/journal.jsonl: every press and every value typed",
            "project/: the board, its project file and panel.json",
            f"{VERSIONS_NAME}: kicad-cli, Python and the operating system",
            f"{MANIFEST_NAME}: every file in the zip, and every file absent",
        ],
    }


def build(config, journal, backend_state: dict, note: str = "", shot: int = 0,
          check_report=None, stamp: str = None, settings_dir=None,
          cli_path=None, shot_warnings=None, session_settings=None,
          pending=(), stale=(), folder=None) -> Report:
    """Write one report into the local report folder and return where it landed.

    *folder* is where the zip and its summary go. None means
    :func:`report_folder`: `bug_reports/` in the panel folder, or the folder
    :data:`REPORT_FOLDER_VARIABLE` names. Nothing is written anywhere else.

    *config* is the session's held configuration, *journal* its recording
    folder, *backend_state* the same dictionary a shot's state file is built
    from, and *shot* the shot this report just took - its files ride along.

    *session_settings*, *pending* and *stale* are the RUNNING SESSION: the live
    configuration
    dictionary, the edits made and not saved, and the reasons the picture is
    out of date. They go under `backend/` beside the timing table, and the
    settings on disk stay where they are under `project/`. A caller that omits
    them gets a report that says under `absent` which live copy it has not got
    and why, because a report that quietly held one copy of the settings could
    not tell the reader what the session was actually running.
    """
    if journal is None:
        raise ReportError(
            "this session has no folder, so there is nowhere to build a "
            "report - start the preview through "
            "`python -m eedesignpanels.preview <config>`"
        )
    if config is None:
        raise ReportError(
            "this session holds no configuration, so there is no project to "
            "report on"
        )
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    note = (note or "").strip()[:MAX_NOTE_CHARS]

    pieces, missing = [], []
    live, live_missing, settings_block = backend_pieces(
        config, journal, backend_state, session_settings=session_settings,
        pending=pending, stale=stale,
    )
    for collect in (
        project_pieces(config),
        session_pieces(journal, shot),
        report_pieces(backend_state, check_report),
        kicad_settings_pieces(settings_dir,
                              project_dir=config.source_board_path.parent),
        (live, live_missing),
    ):
        pieces.extend(collect[0])
        missing.extend(collect[1])

    versions = version_facts(cli_path)
    pieces.append(Piece(
        VERSIONS_NAME, "versions",
        data=(json.dumps(versions, indent=2, sort_keys=True,
                         ensure_ascii=False) + "\n").encode("utf-8"),
    ))
    if note:
        pieces.append(Piece(NOTE_NAME, "note", data=(note + "\n").encode("utf-8")))
    else:
        missing.append(Missing(NOTE_NAME, "note",
                               "nobody typed a line in the report dialog"))

    stem = report_stem(config.source_board_path, stamp)
    folder = Path(folder) if folder is not None else report_folder(config)
    destination = folder / f"{stem}.zip"
    created_at = journal_module.utc_now()
    difference_rows = ()
    if isinstance(session_settings, dict):
        disk_data, _reason = _disk_settings(config)
        if disk_data is not None:
            difference_rows, _count = settings_difference(disk_data,
                                                          session_settings)
    summary_bytes = (json.dumps(
        summary_of(config, backend_state, versions, note, created_at,
                   f"{stem}.zip", settings_block,
                   session_settings=session_settings,
                   difference_rows=difference_rows),
        indent=2, sort_keys=True, ensure_ascii=False, default=repr,
    ) + "\n").encode("utf-8")
    # FIRST IN THE ZIP, so a reader who lists the members sees it at the top.
    pieces.insert(0, Piece(SUMMARY_NAME, "summary", data=summary_bytes))
    manifest = {
        "schema": SCHEMA,
        "created_at": created_at,
        "report": f"{stem}.zip",
        "note": note,
        "shot": shot,
        "shot_warnings": list(shot_warnings or []),
        "session_folder": str(journal.folder),
        "config_path": str(config.config_path),
        "board_path": str(config.source_board_path),
        "report_folder": str(folder),
        "summary": SUMMARY_NAME,
        "privacy": PRIVACY_LINE,
        "limits": {
            "member_bytes": MAX_MEMBER_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
        },
        # WHICH COPY OF THE SETTINGS IS WHICH, AND WHETHER THEY DIFFER. A
        # reader must never have to guess whether they are looking at the file
        # or at what the session was running, and when the two disagree that
        # disagreement is often the whole answer.
        "settings": settings_block,
        # WHICH REVISION OF THE SETTINGS THIS IS, beside the settings
        # themselves. A report says where its config came
        # from without anybody having to open the config: `file` is what the
        # session is running, `this_tool` is what the running build writes, and
        # `written_by` is the version line the file carries.
        "settings_revision": {
            "file": config.revision,
            "this_tool": CONFIG_REVISION,
            "written_by": config.written_by,
        },
        # WHETHER THE BOARD WAS PREPARED AT ALL, in the manifest. The one
        # report a stuck engineer sends is the one taken over a failed board
        # load, and a reader should not have to open a
        # member to learn that is what they are holding.
        "board": failure_block(backend_state),
        "versions": versions,
    }
    report = write_zip(destination, pieces, missing, manifest)
    summary_path = folder / f"{stem}.summary.json"
    summary_path.write_bytes(summary_bytes)
    return report._replace(summary_path=summary_path)
