"""Start the panel builder as a separate program and find the address it prints.

This is the mechanical half of the KiCad door. It knows how to name the
settings file beside a board, how to spawn the engine so that it outlives
KiCad, and how to wait for the line that says where the browser should go. It
holds no `wx` and no `pcbnew`, so the tests drive it with neither installed.

The engine runs in its own process and is never imported here. KiCad's
Python carries none of the engine's three packages; the runtime bootstrap puts
them on a subprocess's path instead. A separate process also keeps the engine
free of any dependency on KiCad.

The child's output goes to a file, not to a pipe. The session has to survive
KiCad closing, and a child holding a pipe whose reader has gone dies on its
next `print`. A file also leaves a record a user can be asked for after the
fact.
"""

import os
import re
import subprocess
import time
from pathlib import Path

# Where the settings file lives, relative to the board. The door derives this
# path and nothing more. Creating the file, repairing it and judging its
# revision belong to the engine's own loader, which owns the settings shape.
PANEL_DIRNAME = "panel"
CONFIG_FILENAME = "panel.json"
LOGS_DIRNAME = "logs"
LAUNCH_LOG_SUFFIX = "_kicad_launch.log"

# A console application launched from a GUI process flashes a console window
# without this flag. The engine is a console application and KiCad is a GUI
# process.
CREATE_NO_WINDOW = 0x08000000

# Its own process group, so a console signal aimed at KiCad does not reach the
# panel builder, and quitting one does not disturb the other.
CREATE_NEW_PROCESS_GROUP = 0x00000200

# The line the engine prints last before it starts serving: the address, alone
# on the line. Matched exactly so a mention of the address inside a sentence is
# never taken for the address itself.
ADDRESS_LINE = re.compile(r"^(http://127\.0\.0\.1:\d+/?)$")

# Five minutes. The engine reads the board and derives the
# panel before it serves, which is seconds on the reference board and can be
# much longer on a heavy one; a wait that gives up early would look like a
# failure that never happened.
ADDRESS_DEADLINE_SECONDS = 300


class LaunchProblem(Exception):
    """A launch that did not happen, said in one plain sentence.

    The argument is the whole user-facing answer, as in the runtime bootstrap.
    The door shows `str(problem)` and nothing else: no traceback and no raw
    transcript. `detail_path` names the
    file holding the engine's own words when there is one.
    """

    def __init__(self, message: str, detail_path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail_path = detail_path


class Session:
    """A running panel builder: its process, its address and its paperwork."""

    def __init__(self, process, address: str, config_path: Path,
                 log_path: Path) -> None:
        self.process = process
        self.address = address
        self.config_path = config_path
        self.log_path = log_path


def config_path_for(board_path) -> Path:
    """The settings file that belongs to a board: `panel/panel.json` beside it."""
    return Path(board_path).resolve().parent / PANEL_DIRNAME / CONFIG_FILENAME


def launch_log_path(config_path, stamp: str | None = None) -> Path:
    """Where this launch's output is kept.

    `panel/logs/`, the same folder the engine already puts a session in, and
    stamped with the time so two launches on one board do not write over each
    other's record.
    """
    when = stamp or time.strftime("%Y%m%d_%H%M%S")
    return Path(config_path).parent / LOGS_DIRNAME / f"{when}{LAUNCH_LOG_SUFFIX}"


# Measured, and it is why this is not a plain `-m` call: KiCad's bundled
# `python.exe` IGNORES `PYTHONPATH`. It carries no `._pth` file and
# `sys.flags.ignore_environment` is 0, yet a folder named in `PYTHONPATH` never
# reaches `sys.path` - KiCad builds the path configuration itself. Command:
#     PYTHONPATH=<runtime> "C:\Program Files\KiCad\10.0\bin\python.exe" -c
#     "import sys; print(sys.path)"
# The runtime folder is therefore put on the path by the code being run, which
# no interpreter can ignore. The first argument is the folder
# and it is removed before the engine sees its own arguments.
PATH_THEN_MODULE = (
    "import runpy,sys;"
    "sys.path.insert(0, sys.argv.pop(1));"
    "runpy.run_module('eedesignpanels.preview', run_name='__main__',"
    " alter_sys=True)"
)


def preview_command(interpreter, config_path, site_packages=None) -> list:
    """The engine's preview entry, run against one settings file.

    No `--open`. The door opens the page itself once it has read the address,
    so there is one place that knows whether a tab was asked for.

    `-u` because the output is going to a file: the engine flushes the address
    line on purpose, but every line before it would otherwise sit in a buffer,
    and those lines are what the waiting window shows.

    *site_packages* is the runtime folder, and naming it switches the call to
    the bootstrap above. It is None only when the interpreter already has the
    engine installed in it, which is the development venv and CI.
    """
    if site_packages is None:
        return [
            str(interpreter), "-u", "-m", "eedesignpanels.preview",
            str(config_path),
        ]
    return [
        str(interpreter), "-u", "-c", PATH_THEN_MODULE,
        str(site_packages), str(config_path),
    ]


# ---------------------------------------------------------------------------
# what a failure says
#
# A failure message stands alone on screen. The person reading it is inside
# KiCad, sees no console, and has nothing "above" to look at. So the message
# itself carries what was being done, the reason in the words the engine
# actually produced, what has changed (nothing), and the one next step.
# ---------------------------------------------------------------------------

TELL_THE_TEAM = "Keep that file. A problem report needs it."


def _named(subject) -> str:
    """The board as a person names it, or a plain fallback."""
    name = Path(subject).name if subject else ""
    return name or "this board"


def cannot_start_message(executable, windows_said: str) -> str:
    """Windows would not run the interpreter at all."""
    return (
        "The panel builder could not be started.\n\n"
        "Windows tried to run:\n\n"
        f"    {executable}\n\n"
        f"and said: {windows_said}\n\n"
        "That file is the Python that comes with KiCad. If KiCad was moved, "
        "or only partly uninstalled, install KiCad 10 again. Nothing on your "
        "board has been changed."
    )


def stopped_message(subject, said: str, log_path) -> str:
    """The engine started and then stopped before it was ready."""
    if said:
        reason = (
            "It started, and then stopped, saying:\n\n"
            f'    "{said}"\n\n'
            "That sentence is the reason: a settings file it cannot read, a "
            "board file that is not where the settings say it is, and a board "
            "it cannot load all say so there."
        )
        closing = (
            "Everything it did is written down in:\n\n"
            f"    {log_path}\n\n"
            "If that sentence does not say what to fix, keep that file. A "
            "problem report needs it."
        )
    else:
        reason = "It started, and then stopped without saying anything at all."
        closing = (
            "What it did before it stopped is written down in:\n\n"
            f"    {log_path}\n\n" + TELL_THE_TEAM
        )
    return (
        f"The panel builder could not open {_named(subject)}.\n\n"
        f"{reason}\n\nYour board has not been changed.\n\n{closing}"
    )


def still_starting_message(subject, said: str, log_path,
                           deadline_seconds: float) -> str:
    """The engine is alive and has taken longer than the door will wait."""
    minutes = max(1, round(deadline_seconds / 60))
    counted = "1 minute " if minutes == 1 else f"{minutes} minutes "
    heard = (
        f'The last thing it said was:\n\n    "{said}"\n\n' if said
        else "It has said nothing yet.\n\n"
    )
    return (
        f"The panel builder has been opening {_named(subject)} for "
        f"{counted}and has not said it is ready.\n\n"
        f"{heard}"
        "It is still running and your board has not been changed. When it "
        "does finish, it writes the address of its page as the last line of:"
        f"\n\n    {log_path}\n\n"
        "That address can be pasted into a browser. If it never finishes, "
        "keep that file. A problem report needs it."
    )


def engine_interpreter(chosen, kicad_python) -> Path:
    """The interpreter that may be handed `-m eedesignpanels.preview`.

    Inside KiCad, `sys.executable` is `kicad.exe`, not a
    Python. The runtime bootstrap answers "ambient" with the calling
    interpreter when the engine is already installed beside it - which is right
    for a shell and wrong here, because KiCad's `python.exe` and `kicad.exe`
    share one site-packages folder. Running `kicad.exe -m ...` would open a
    second KiCad. Anything not named `python` is replaced by the bundled
    `python.exe` of the same install.
    """
    chosen = Path(chosen)
    if chosen.name.lower().startswith("python"):
        return chosen
    return Path(kicad_python)


def creation_flags() -> int:
    """The Windows process flags this launch needs; nothing off Windows."""
    if os.name != "nt":
        return 0
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def spawn(command: list, environment: dict, log_path, cwd=None,
          popen=subprocess.Popen):
    """Start *command* with its output going to *log_path*. Returns the process.

    No pipe and no inherited console. The parent closes its
    own copy of the log handle straight away - the child holds its own - so
    nothing of this launch is still attached to KiCad once it returns.

    *popen* is the seam the tests drive; nothing else passes it.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "w", encoding="utf-8", errors="replace")
    try:
        return popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=None if cwd is None else str(cwd),
            env=environment,
            creationflags=creation_flags(),
            close_fds=True,
        )
    except OSError as error:
        raise LaunchProblem(
            cannot_start_message(
                command[0], getattr(error, "strerror", None) or str(error)
            )
        ) from None
    finally:
        handle.close()


def address_in(text: str) -> str | None:
    """The address the engine printed, or None while it has not printed one."""
    for line in reversed(text.splitlines()):
        found = ADDRESS_LINE.match(line.strip())
        if found:
            return found.group(1)
    return None


def _read(log_path) -> str:
    try:
        return Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_words(text: str) -> str:
    """The last thing the engine said that was not blank."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def wait_for_address(log_path, process, on_line=None, subject=None,
                     deadline_seconds: float = ADDRESS_DEADLINE_SECONDS,
                     sleep=time.sleep, now=time.monotonic) -> str:
    """Watch the log until the address appears. Raises LaunchProblem otherwise.

    Two ways it ends badly, and each gets its own message: the engine stopped
    (its last line is quoted, because that line is usually the whole diagnosis),
    or it is still running and has taken too long. *subject* is the board, so
    the message names the job the reader asked for.
    """
    log_path = Path(log_path)
    started = now()
    seen = 0
    while True:
        text = _read(log_path)
        if on_line is not None:
            lines = text.splitlines()
            for line in lines[seen:]:
                on_line(line)
            seen = len(lines)
        address = address_in(text)
        if address:
            return address
        if process.poll() is not None:
            raise LaunchProblem(
                stopped_message(subject, _last_words(text), log_path),
                detail_path=log_path,
            )
        if now() - started >= deadline_seconds:
            raise LaunchProblem(
                still_starting_message(
                    subject, _last_words(text), log_path, deadline_seconds
                ),
                detail_path=log_path,
            )
        sleep(0.2)


def start(interpreter, environment: dict, board_path, site_packages=None,
          on_line=None, stamp: str | None = None, popen=subprocess.Popen,
          sleep=time.sleep, now=time.monotonic,
          deadline_seconds: float = ADDRESS_DEADLINE_SECONDS) -> Session:
    """Launch the panel builder on *board_path* and wait for its address.

    The board's own project folder is the working folder, so
    a relative path in the settings file means what it says. KiCad's working
    folder is wherever KiCad was started from, which is nobody's project.
    """
    config_path = config_path_for(board_path)
    log_path = launch_log_path(config_path, stamp=stamp)
    command = preview_command(interpreter, config_path,
                              site_packages=site_packages)
    process = spawn(
        command, environment, log_path,
        cwd=Path(board_path).resolve().parent, popen=popen,
    )
    address = wait_for_address(
        log_path, process, on_line=on_line, subject=board_path, sleep=sleep,
        now=now, deadline_seconds=deadline_seconds,
    )
    return Session(process, address, config_path, log_path)
