"""Fill a panel's own zones by asking KiCad, then transplant the answer back.

Nothing in this project computes a copper fill. KiCad's filler is thousands of
lines of clipper geometry that has to agree with KiCad's DRC, its thermal
reliefs, its island rules and its own plot path, and a second implementation
that agreed with it approximately would be worse than none. So the panel's new
zones are filled by the only thing that can fill them correctly - KiCad - and
the answer is carried back into the board this build owns.

**The refill happens in a SANDBOX, and the panel is never refilled in place.**
That is the whole point of the mechanism. A panel carries the instances' copper
as flattened graphics (:mod:`eedesignpanels.stages.zones`), which no refill can
move, but it also carries whatever else the build put on it; running KiCad's
save path over the panel itself would hand every byte of the file to KiCad's
writer and quietly re-express the whole document. Instead:

1. the in-memory board is written to a scratch copy in a temporary directory,
2. `kicad-cli pcb drc --refill-zones --save-board` fills it there,
3. the fill RECORDS of the named zones are read back out, and
4. `eedesigncore.replace_fills` puts them on the zones this build holds.

Everything else in the panel is untouched by construction, and
:func:`refill_zones` proves it rather than asserting it: the board is
serialised with the target zones detached, before and after the transplant, and
the two byte strings must be equal. If they are not, the transplant is not what
it claims to be and the run says so.

**This is the one stage that needs KiCad on the machine.** The core is headless
and builds a panel in a container with no KiCad installed, and
that stays true for every config that does not ask for a filled panel zone. A
config that does ask gets a clear error naming the binary when it is missing,
rather than a panel with empty pours.

`island_removal_mode` is set to NEVER on every zone handed here, and that is not
a preference. A panel rail pour has no net, so every filled area in it is an
island by KiCad's definition; under the default "always remove" the filler
throws away all but whichever piece survives its own connectivity walk.
Measured on a test panel: a single tooling hole in the top rail
took that rail from a 43 mm pour to a 0.4 x 0.35 mm sliver, and the same panel
with `island_removal_mode` set to never kept all three pieces the hole split it
into. The caller sets it; this module refuses a zone that has not.

**THE ROUND TRIP AND THE TRANSPLANT ARE TWO CALLS**, because one
of them is ten seconds long and the other is a mutation of a board somebody
else may be reading. :func:`take_fills` writes the sandbox, runs KiCad and reads
the records back, and touches nothing the caller holds; :func:`transplant` puts
those records on the caller's zones and proves it changed nothing else.
:func:`refill_zones` is the two of them in order, which is what a headless build
wants. A live preview wants them apart: it pays the ten seconds with no lock
held and takes the lock for the millisecond the board actually changes in
(`preview.session`).

**A ROUND TRIP NOBODY WANTS ANY MORE IS STOPPED, NOT WAITED OUT.** When another
zone-affecting change lands before the first is done, the first is stopped and
the second starts. So :func:`take_fills` takes
a *cancel* token, watches it while KiCad runs, and ENDS THE PROCESS when it is
set - `terminate`, then `kill` if the process is still there
(:data:`TERMINATE_GRACE_SECONDS`). It raises :class:`RefillCancelled`, which is
deliberately NOT a :class:`RefillError`: a cancelled fill is not a failure, the
zones are still owed a fill, and a caller that recorded it as a failure would
stop asking for one. A caller with no token passes none and waits, which is
every headless build.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board, dump_board, extract_fills, replace_fills

from eedesignpanels.kicad_install import default_kicad_cli

# The absolute path, because a subprocess environment does not inherit the
# user's PATH. Found the same way as the render path's, by the
# core's own search (`eedesignpanels.kicad_install`), because the core may not
# depend on the review layer.
DEFAULT_KICAD_CLI = default_kicad_cli()
# kicad-cli is a console application. Launched from a GUI process without this
# it flashes a console window. Harmless headless.
CREATE_NO_WINDOW = 0x08000000

# KiCad's "never remove islands". A no-net pour is all islands, so this is the
# only setting under which a rail pour survives its own fill.
ISLAND_REMOVAL_NEVER = 1

# THE FILES A FILL IS DECIDED BY THAT ARE NOT IN THE BOARD (measured). A board
# file does not carry its net classes or its custom design
# rules - the project does - so a sandbox holding the board alone is filled
# against KiCad's defaults and comes back with different copper. Measured on the
# corpus board: filled beside its own project and rules, two zones came back as
# one island each; filled alone, the same two came back as three islands and two.
#
# So a caller that has the real board's path hands it over (`beside=`) and these
# are copied in next to the sandbox board, renamed to its stem, which is how
# KiCad finds a project at all. Nothing is copied back and nothing outside the
# sandbox is written.
PROJECT_SIDECARS = (".kicad_pro", ".kicad_dru")

# How often a running KiCad is checked against the cancel token. Fifty
# milliseconds is far under anything a designer perceives and far over the cost
# of asking: the wait is a `Popen.wait` with a timeout, not a spin.
POLL_SECONDS = 0.05

# How long a superseded KiCad is given to end politely before it is killed.
# `terminate` is what lets it close its own files; the kill is the guarantee
# that the next fill is not queued behind a process nobody wants.
TERMINATE_GRACE_SECONDS = 2.0


class RefillError(Exception):
    """The refill could not be done, or its result could not be trusted."""


class RefillCancelled(Exception):
    """The round trip was stopped because a newer change superseded it.

    NOT a :class:`RefillError`, and the distinction is the whole discipline: a
    failure is said once and not retried, while a cancellation
    leaves the zones still owing a fill that the newer change is about to ask
    for. `seconds` is how long the stop took and `killed` says whether the
    process had to be killed after its grace, so a journal can state both rather
    than leaving them to be assumed.
    """

    def __init__(self, message: str, seconds: float = 0.0,
                 killed: bool = False):
        super().__init__(message)
        self.seconds = seconds
        self.killed = killed


class FillTaken(NamedTuple):
    """What KiCad said, before any of it has been put on anybody's board.

    `records` is `{zone uuid: fill records}` exactly as the sandbox gave them,
    so this is an ANSWER and not yet a change: a caller may drop it - because
    the panel it was taken for is no longer the panel on screen - and nothing
    has to be undone.
    """

    records: dict
    seconds: float
    cli_path: Path


class RefillResult(NamedTuple):
    """One sandbox refill, measured. Every number here is reported."""

    zones_filled: int
    zones_empty: int
    records: int
    per_layer: tuple
    seconds: float
    # The proof: serialising the board with the target zones detached gives the
    # same bytes before and after the transplant.
    gate_passed: bool
    cli_path: Path

    @property
    def zones(self) -> int:
        return self.zones_filled + self.zones_empty


def _stop(process) -> tuple:
    """End a KiCad nobody is waiting for any more. `(seconds, killed)`.

    Politely first, because `terminate` lets it close the sandbox files it has
    open; then by force, because the whole point of the stop is that the NEXT
    fill starts now and not in ten seconds.
    """
    started = time.monotonic()
    process.terminate()
    killed = False
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        killed = True
        process.kill()
        process.wait()
    return (time.monotonic() - started, killed)


def _wait_for(process, cancel) -> None:
    """Wait for KiCad, watching *cancel*. Raises when the token is set.

    A caller with no token waits with no poll at all - the headless build's
    case, where there is nothing that could supersede this run.
    """
    if cancel is None:
        process.wait()
        return
    while True:
        if cancel.is_set():
            seconds, killed = _stop(process)
            raise RefillCancelled(
                f"the zone re-fill was stopped after a newer change: "
                f"{'killed' if killed else 'ended'} kicad-cli in "
                f"{seconds:.3f} s",
                seconds=seconds, killed=killed,
            )
        try:
            process.wait(timeout=POLL_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_cli(cli_path: Path, board_path: Path, report_path: Path,
             cancel=None):
    """Run the refill, or raise saying which part of it failed.

    `(returncode, output)`. The process's own output goes to a file in the
    sandbox rather than a pipe: it is read back only when something went wrong,
    and a pipe nobody drains is a process that blocks when it fills one.

    Raises:
        RefillError: when kicad-cli is missing or will not start.
        RefillCancelled: when *cancel* is set while KiCad is running.
    """
    if not Path(cli_path).is_file():
        raise RefillError(
            f"this panel asks for a filled copper zone, which only KiCad can "
            f"fill, and kicad-cli is not at {cli_path}. Install KiCad 10, or "
            f"remove the section that asks for the pour"
        )
    log_path = Path(board_path).with_name("refill-cli.log")
    try:
        sink = open(log_path, "wb")  # noqa: SIM115 - closed in the finally
    except OSError as exc:
        raise RefillError(f"could not write {log_path}: {exc}") from exc
    try:
        try:
            process = subprocess.Popen(
                [
                    str(cli_path), "pcb", "drc",
                    "--refill-zones", "--save-board",
                    "--output", str(report_path),
                    str(board_path),
                ],
                stdout=sink, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise RefillError(f"could not run {cli_path}: {exc}") from exc
        _wait_for(process, cancel)
    finally:
        sink.close()
    said = ""
    if log_path.is_file():
        said = log_path.read_text(encoding="utf-8", errors="replace")
    return (process.returncode, said)


def _detach(board: Board, zones: list) -> list:
    """Take *zones* off the board and return the full list, to put back."""
    saved = list(board.zones)
    targets = {id(zone) for zone in zones}
    board.zones = [zone for zone in saved if id(zone) not in targets]
    return saved


def carry_project(beside, board_path: Path) -> tuple:
    """Copy the real board's project files in beside the sandbox board.

    `(names copied)`, in the order :data:`PROJECT_SIDECARS` states them. A
    caller with no *beside* copies nothing, which is the panel's case: a panel
    is built here and has no project on disk to be filled against.
    """
    if beside is None:
        return ()
    beside = Path(beside)
    carried = []
    for suffix in PROJECT_SIDECARS:
        source = beside.with_suffix(suffix)
        if source.is_file():
            shutil.copyfile(source, Path(board_path).with_suffix(suffix))
            carried.append(source.name)
    return tuple(carried)


def checked(zones: list, require_island_keep: bool = True) -> None:
    """Refuse a zone this cannot fill, before a process is started.

    *require_island_keep* is the PANEL's rule and not a universal one. A panel
    rail pour has no net, so every filled area in it is an island and the
    default "always remove" empties it - hence the refusal. A SOURCE BOARD's
    zones are the designer's own, netted, and carry whatever island setting the
    designer chose; changing it would be this tool editing a design rule it was not asked
    about, so the board path asks for the check to be off and the fill comes
    back exactly as KiCad would compute it in the editor.

    Raises:
        ValueError: when a zone has no uuid to be matched back by, or - when the
            check is asked for - has not been set to keep its islands.
    """
    for zone in zones:
        if not zone.id:
            raise ValueError(
                "a zone handed to the refill has no uuid, so its fill could "
                "not be matched back out of the sandbox"
            )
        if not require_island_keep:
            continue
        if zone.fill.island_removal_mode != ISLAND_REMOVAL_NEVER:
            raise ValueError(
                f"zone {zone.id} would be refilled with island removal set to "
                f"{zone.fill.island_removal_mode}, and a no-net panel pour is "
                f"all islands - set island_removal_mode to "
                f"{ISLAND_REMOVAL_NEVER} or it will come back nearly empty"
            )


def cli_present(cli_path: Path = None) -> bool:
    """Is there a KiCad on this machine to fill a pour with? Asked, never assumed."""
    return Path(cli_path or DEFAULT_KICAD_CLI).is_file()


def take_fills(board: Board, zones: list,
               cli_path: Path = DEFAULT_KICAD_CLI,
               work_dir: Path = None,
               require_island_keep: bool = True,
               beside: Path = None,
               cancel=None) -> FillTaken:
    """Ask KiCad what those zones' copper is. Changes nothing at all.

    THE SLOW HALF - measured 9.9 s on a test panel, nearly all of
    it kicad-cli - and it is deliberately the half that holds no lock and mutates
    nothing: the board is SERIALISED here, not modified, so a caller may run this
    off its responsive path and discard the answer if the panel has moved on.

    *zones* are objects held by *board*, matched back by uuid, so every one of
    them needs one.

    *work_dir* is where the scratch copy goes. None means a temporary directory
    that is deleted when this returns - the panel folder gets nothing, because
    the sandbox is not output.

    *beside* is the real board's path, when there is one. Its project and custom
    rules are copied into the sandbox so KiCad fills against the design's own net
    classes rather than against its defaults - see :data:`PROJECT_SIDECARS` for
    what that measured. Nothing is ever written back to it.

    *cancel* is anything with an `is_set()`: a newer zone-affecting change sets
    it and this call STOPS - the running KiCad is ended and
    :class:`RefillCancelled` is raised - rather than finishing an answer about a
    board nobody is looking at. It is checked
    before the board is serialised as well as while KiCad runs, because that
    write is itself most of a second.

    Raises:
        RefillError: when kicad-cli is missing, fails, or loses a zone.
        RefillCancelled: when *cancel* is set before or during the round trip.
        ValueError: per :func:`checked`.
    """
    checked(zones, require_island_keep=require_island_keep)
    if not zones:
        return FillTaken(records={}, seconds=0.0, cli_path=Path(cli_path))

    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        raise RefillCancelled(
            "the zone re-fill was superseded before it started"
        )
    with _sandbox(work_dir) as sandbox:
        board_path = sandbox / "refill.kicad_pcb"
        board.to_file(board_path)
        carry_project(beside, board_path)
        code, said = _run_cli(cli_path, board_path, sandbox / "refill.rpt",
                              cancel=cancel)
        if code != 0:
            raise RefillError(
                f"kicad-cli could not refill the panel's zones (exit "
                f"{code}): {said.strip()[:800]}"
            )
        filled = Board.from_file(board_path)
    seconds = time.monotonic() - started

    by_id = {zone.id: zone for zone in filled.zones if zone.id}
    records: dict = {}
    for zone in zones:
        found = by_id.get(zone.id)
        if found is None:
            raise RefillError(
                f"zone {zone.id} did not come back from the refill, so its "
                f"fill is not available to transplant"
            )
        records[zone.id] = extract_fills(found)
    return FillTaken(records=records, seconds=seconds, cli_path=Path(cli_path))


def transplant(board: Board, zones: list, taken: FillTaken) -> RefillResult:
    """Put *taken*'s records on *zones*, and prove nothing else moved.

    THE FAST HALF, and the only half that changes the caller's board - so a
    caller with other readers of that board holds them off for exactly this
    call. It is a few milliseconds of assignment plus the gate, which is two
    serialisations of the board with the target zones detached.

    Raises:
        RefillError: when a zone has no record in *taken*, or when the
            transplant changed anything but the named zones' fills.
    """
    if not zones:
        return RefillResult(
            zones_filled=0, zones_empty=0, records=0, per_layer=(),
            seconds=taken.seconds, gate_passed=True,
            cli_path=Path(taken.cli_path),
        )
    for zone in zones:
        if zone.id not in taken.records:
            raise RefillError(
                f"zone {zone.id} has no fill in the answer this transplant was "
                f"given, so the answer is not the one this panel asked for"
            )

    saved = _detach(board, zones)
    before = dump_board(board)
    board.zones = saved

    per_layer: dict = {}
    total = empty = 0
    for zone in zones:
        got = taken.records[zone.id]
        replace_fills(zone, got)
        total += len(got)
        if not got:
            empty += 1
        for record in got:
            per_layer[record.layer] = per_layer.get(record.layer, 0) + 1

    _detach(board, zones)
    after = dump_board(board)
    board.zones = saved

    if before != after:
        raise RefillError(
            "the fill transplant changed something other than the zones it was "
            "given: the panel serialises differently with those zones detached "
            "before and after. Nothing downstream can trust the fill, so the "
            "run stops here"
        )

    return RefillResult(
        zones_filled=len(zones) - empty,
        zones_empty=empty,
        records=total,
        per_layer=tuple(sorted(per_layer.items())),
        seconds=taken.seconds,
        gate_passed=True,
        cli_path=Path(taken.cli_path),
    )


def refill_zones(board: Board, zones: list,
                 cli_path: Path = DEFAULT_KICAD_CLI,
                 work_dir: Path = None,
                 require_island_keep: bool = True,
                 beside: Path = None,
                 cancel=None) -> RefillResult:
    """Fill *zones* by running KiCad over a scratch copy, and transplant back.

    The two halves in order, which is what a build wants: it owns the board it
    is building and has nobody to hold off. A live preview calls them separately
    - see the module docstring.

    Raises:
        RefillError: when kicad-cli is missing, fails, loses a zone, or when
            the transplant changed anything but the named zones' fills.
        ValueError: when a zone has no uuid to be matched back by, or has not
            been set to keep its islands.
    """
    taken = take_fills(board, zones, cli_path=cli_path, work_dir=work_dir,
                       require_island_keep=require_island_keep, beside=beside,
                       cancel=cancel)
    return transplant(board, zones, taken)


class _sandbox:
    """A scratch directory for the refill: the caller's, or a temporary one."""

    def __init__(self, work_dir):
        self._given = work_dir
        self._temp = None

    def __enter__(self) -> Path:
        if self._given is not None:
            path = Path(self._given)
            path.mkdir(parents=True, exist_ok=True)
            return path
        self._temp = tempfile.TemporaryDirectory(prefix="eedesignpanels_refill_")
        return Path(self._temp.name)

    def __exit__(self, *exc_info) -> bool:
        if self._temp is not None:
            self._temp.cleanup()
        return False


__all__ = [
    "CREATE_NO_WINDOW",
    "DEFAULT_KICAD_CLI",
    "ISLAND_REMOVAL_NEVER",
    "POLL_SECONDS",
    "PROJECT_SIDECARS",
    "TERMINATE_GRACE_SECONDS",
    "FillTaken",
    "RefillCancelled",
    "RefillError",
    "RefillResult",
    "carry_project",
    "checked",
    "cli_present",
    "refill_zones",
    "take_fills",
    "transplant",
]
