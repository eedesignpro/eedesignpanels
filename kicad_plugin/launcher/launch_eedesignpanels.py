"""The standalone door: double-click a shortcut, get the panel builder.

WHO THIS IS FOR. An engineer with KiCad on their machine, no Python of their
own, and no patience for a setup page. Windows only: the shortcut is a batch
file and the board dialog is the Windows one. They want to look at a
board. Nothing is configured by hand - no path to paste, no environment
variable, no install step. Everything this needs, it finds or builds.

WHERE IT LIVES, and why it is not in `plugins/`. Every file under `plugins/`
ships to every engineer through the plugin catalogue, and this is a SEPARATE
DOOR - it is for people who are not in KiCad at all. It sits in
`launcher/` beside the plugin package so `plugins/runtime.py` is one relative
path away and needs no install to reach, and because the packaging allowlist in
`tools/build_repo.py` names `plugins`, `resources` and the library folders, a
folder called `launcher` is excluded from the shipped archive by simply not
being on that list.

THE FOUR STEPS, in order, and each one can only fail in a sentence:

1. RUN ON THE RIGHT PYTHON. `eedesignpanels.cmd` reads the registry for a KiCad
   with a bundled `python.exe` and starts this file with it. That lookup is
   deliberately dumb - it takes whichever KiCad it meets first - so this file
   asks `runtime` for the NEWEST install and re-runs itself on that one when
   they differ. All version-choosing logic stays in Python, where it can be
   read and tested, and the batch file stays four lines.
2. FIND THE BOARD. An argument, which is what a board file DROPPED ON THE
   SHORTCUT becomes, or a file dialog. The dialog is the Windows one, called
   through `ctypes`, because MEASURED: KiCad's bundled Python
   3.11.5 has no `_tkinter`, so importing `tkinter` raises
   `ModuleNotFoundError`. There is no toolkit in that one to raise a window
   with, and `comdlg32.GetOpenFileNameW` is in every Windows, needs no
   package, and is the dialog the engineer already knows.
3. GUARANTEE THE ENGINE. `plugins/runtime.py` does all of it. The first run
   builds a per-user runtime and SAYS SO before it starts, because eight
   seconds of silence after a double-click reads as a broken shortcut.
4. OPEN IT. The same launch the user types by hand -
   `python -m eedesignpanels.preview <config> --open` - as a child process that
   inherits this console, so its own plain-words output is what the engineer
   reads and there is no second layer of messages to keep honest.

WHAT IT DOES NOT DO. It does not import the engine to start it, it writes no
settings logic of its own, and it never modifies a board. It reaches for
`eedesignpanels.config` in exactly one case, named at :func:`seed_config`.

Run it directly when there is already a Python that can:

    <python> launch_eedesignpanels.py [board.kicad_pcb]
    <python> launch_eedesignpanels.py --board board.kicad_pcb --no-open
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# The bootstrap this door stands on. Loaded BY FILE PATH from the sibling
# `plugins/` folder, not imported as `plugins.runtime`, because
# that package's `__init__.py` imports `pcbnew` and `wx` and would drag the
# whole KiCad door into a process that has no KiCad session.
LAUNCHER_DIR = Path(__file__).resolve().parent
RUNTIME_SOURCE = LAUNCHER_DIR.parent / "plugins" / "runtime.py"

# What the engine is started as. ONE launch shape, identical to the command the
# user types by hand, so the shortcut runs the same program that gets tested.
# It goes through `run_engine.py` because KiCad's bundled Python ignores
# `PYTHONPATH` - see that file, and see :func:`preview_command`.
PREVIEW_MODULE = "eedesignpanels.preview"
RUN_ENGINE_SOURCE = LAUNCHER_DIR / "run_engine.py"
NO_RUNTIME = "-"

BOARD_SUFFIX = ".kicad_pcb"

# Where the last board came from, so the dialog opens somewhere useful. Nothing
# here has to be set for the tool to work, and a missing or broken file costs
# one dialog that opens on the Windows default. It lives beside the runtime
# because that folder is already this tool's own and already survives a KiCad
# upgrade.
MEMORY_NAME = "launcher.json"
MEMORY_KEY = "last_board_folder"

# Exit codes. The shortcut pauses on anything non-zero, so a number here is
# what decides whether a sentence stays on screen long enough to be read.
EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_BOARD = 2


# ---------------------------------------------------------------------------
# loading the shared bootstrap
# ---------------------------------------------------------------------------


def load_runtime(source: Path = RUNTIME_SOURCE):
    """Load `plugins/runtime.py` from its file, under its own module name.

    It registers itself in `sys.modules` before it executes, and that is not
    optional: under `from __future__ import annotations` a frozen
    dataclass resolves its annotations through `sys.modules[cls.__module__]`,
    and a module that is not there fails with an `AttributeError` about
    `NoneType` that says nothing about the real cause.
    """
    import importlib.util

    if not source.is_file():
        raise LauncherProblem(
            "The panel builder is missing a file it needs to start:\n\n"
            f"    {source}\n\n"
            "The shortcut was probably copied out of the folder it belongs "
            "in. Run it from where it was installed, or install a fresh copy."
        )
    spec = importlib.util.spec_from_file_location(
        "eedesignpanels_runtime", str(source)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LauncherProblem(Exception):
    """A launch that could not happen, said in plain words.

    The argument is the whole user-facing answer, exactly as
    `runtime.RuntimeProblem` works. An engineer is shown this rather than a
    traceback, because there is nothing in one they can act on.
    """


# ---------------------------------------------------------------------------
# the file dialog, and why it is this one
# ---------------------------------------------------------------------------

_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_EXPLORER = 0x00080000

# The dialog's type list. Two NULs end it, one NUL separates every part. Boards
# first so the folder shows only what can be opened, and "All files" second so
# nobody is trapped by a board saved under another name.
_FILE_FILTER = (
    "KiCad board files (*.kicad_pcb)\0*.kicad_pcb\0All files (*.*)\0*.*\0\0"
)

_DIALOG_TITLE = "Choose the board to panelize"

# Big enough for any path Windows will hand back, and the API needs the size
# stated rather than discovered.
_PATH_BUFFER_CHARS = 4096


# WHAT A HIGH-RESOLUTION SCREEN DOES TO A PROCESS THAT SAYS NOTHING. Windows
# draws a process that has not declared itself at 96 dots
# per inch and then stretches the result to fit the screen, so the file dialog
# and every letter in it come out soft. Declaring costs one call, and it has to
# happen BEFORE the first window: Windows fixes a process's answer the first
# time it is asked for, and every later call is refused.
_DPI_PER_MONITOR_V2 = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_DPI_PER_MONITOR = 2  # PROCESS_PER_MONITOR_DPI_AWARE
_E_ACCESSDENIED = -2147024891  # already declared, which is not a failure


def _windows_library(name: str):
    """One Windows library, or None when this Windows does not carry it."""
    import ctypes

    try:
        return getattr(ctypes.windll, name)
    except (AttributeError, OSError):
        return None


def declare_dpi_awareness(user32=None, shcore=None) -> str:
    """Declare this process sharp on a high-resolution screen. Never raises.

    Three routes, newest first, and it returns which one answered so a test can
    read it without a window ever being drawn. The modern call comes first with
    the older ones behind it, so a call that is absent on an older Windows
    cannot stop the file dialog from opening - a blurry dialog is a defect, and
    no dialog at all is a broken tool.

    * `per-monitor-v2` - `SetProcessDpiAwarenessContext`, Windows 10 1703 and
      later, and the only one that follows a window dragged between screens.
    * `per-monitor` - `SetProcessDpiAwareness`, Windows 8.1 and later.
    * `system` - `SetProcessDPIAware`, on anything older.
    * `already` - something declared it first, so this process is already sharp.
    * `none` - not Windows, or no route answered. The dialog still opens.
    """
    if os.name != "nt":
        return "none"
    import ctypes

    # Each library is loaded on its own. `shcore` is absent before Windows 8.1,
    # and a failure to load it must not stop the newest call - which lives in
    # `user32` - from being tried at all.
    user32 = _windows_library("user32") if user32 is None else user32
    shcore = _windows_library("shcore") if shcore is None else shcore
    try:
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_PER_MONITOR_V2)
        ):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        answer = shcore.SetProcessDpiAwareness(_DPI_PER_MONITOR)
        if answer == 0:
            return "per-monitor"
        if answer == _E_ACCESSDENIED:
            return "already"
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "none"


def dialog_available() -> bool:
    """True when this machine can raise the Windows file dialog."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        _ = ctypes.windll.comdlg32.GetOpenFileNameW
    except (ImportError, AttributeError, OSError):
        return False
    return True


def _open_file_name_struct():
    """The `OPENFILENAMEW` layout, built once per call.

    Declared here rather than at import time, so a machine
    that is not Windows can still import this file and run its tests. Every
    field is present and in order even where it is never set - the API reads
    `lStructSize` and trusts the rest of the block to match it.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    return OPENFILENAMEW


def ask_for_board(start_dir: Path | None = None) -> Path | None:
    """Raise the Windows file dialog. Returns the chosen board, or None.

    None means CANCELLED, and cancelling is not a failure: somebody who changes
    their mind gets no error and no paused console.
    """
    if not dialog_available():
        raise LauncherProblem(
            "The panel builder needs to be told which board to open, and it "
            "could not raise a file dialog on this computer.\n\n"
            "Drag a .kicad_pcb file onto the eedesignpanels shortcut instead. "
            "That opens it straight away."
        )
    # BEFORE THE DIALOG. Windows settles a process's display handling at its
    # first window, so this is the last moment it can be said and the dialog
    # is the first window this door draws.
    declare_dpi_awareness()
    import ctypes
    import ctypes.wintypes as wintypes

    structure = _open_file_name_struct()
    path_buffer = ctypes.create_unicode_buffer(_PATH_BUFFER_CHARS)
    # The filter is held in its own buffer and passed as a pointer. A plain
    # Python string would be handed over as `LPCWSTR` and
    # cut off at its first NUL, which is three characters in - the dialog would
    # come up with an empty type list and show nothing.
    filter_buffer = ctypes.create_unicode_buffer(_FILE_FILTER)
    block = structure()
    block.lStructSize = ctypes.sizeof(structure)
    block.lpstrFilter = ctypes.cast(filter_buffer, wintypes.LPCWSTR)
    block.nFilterIndex = 1
    block.lpstrFile = ctypes.cast(path_buffer, wintypes.LPWSTR)
    block.nMaxFile = _PATH_BUFFER_CHARS
    block.lpstrTitle = _DIALOG_TITLE
    block.lpstrDefExt = BOARD_SUFFIX.lstrip(".")
    if start_dir is not None and Path(start_dir).is_dir():
        block.lpstrInitialDir = str(start_dir)
    # OFN_NOCHANGEDIR is deliberate. The dialog would otherwise leave this
    # process sitting in whatever folder was browsed to, and the child process
    # inherits that - a relative path in a settings file would then resolve
    # against the wrong folder.
    block.Flags = (
        _OFN_EXPLORER
        | _OFN_FILEMUSTEXIST
        | _OFN_PATHMUSTEXIST
        | _OFN_HIDEREADONLY
        | _OFN_NOCHANGEDIR
    )
    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(block)):
        # Zero is cancel OR an error, and only `CommDlgExtendedError` tells
        # them apart. Ask, because reporting a real failure as "you
        # cancelled" would send somebody looking in the wrong place.
        if ctypes.windll.comdlg32.CommDlgExtendedError():
            raise LauncherProblem(
                "Windows could not show the file dialog, so the panel builder "
                "does not know which board to open.\n\n"
                "Drag a .kicad_pcb file onto the eedesignpanels shortcut "
                "instead."
            )
        return None
    return Path(path_buffer.value)


# ---------------------------------------------------------------------------
# remembering where the boards are
# ---------------------------------------------------------------------------


def memory_path(runtime) -> Path:
    """The little file holding the folder the last board came from."""
    return runtime.runtime_home() / MEMORY_NAME


def remembered_folder(runtime) -> Path | None:
    """Where the last board was chosen from, or None.

    Anything unreadable counts as nothing remembered. This is a convenience,
    so it is never a reason for the tool to refuse to start.
    """
    try:
        raw = memory_path(runtime).read_text(encoding="utf-8")
        folder = Path(json.loads(raw)[MEMORY_KEY])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return folder if folder.is_dir() else None


def remember_folder(runtime, folder: Path) -> None:
    """Keep the folder a board was chosen from. Never raises."""
    try:
        path = memory_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({MEMORY_KEY: str(folder)}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


# ---------------------------------------------------------------------------
# the board, and the settings file beside it
# ---------------------------------------------------------------------------


def board_problem(path: Path) -> str | None:
    """Why this file is not a board this tool can open, or None when it is.

    Only what is CHEAP and CERTAIN is checked here - it exists, it is a file,
    it carries the board suffix, it is not empty. Whether the board
    actually loads is the engine's question, it already answers it in plain
    words, and asking it twice would mean two messages to keep saying the same
    thing.
    """
    if not path.exists():
        return (
            f"There is no file at:\n\n    {path}\n\n"
            "Check the name, or drag the board onto the eedesignpanels "
            "shortcut from the folder it is really in."
        )
    if path.is_dir():
        return (
            f"That is a folder, not a board:\n\n    {path}\n\n"
            "Open the folder and choose the .kicad_pcb file inside it."
        )
    if path.suffix.lower() != BOARD_SUFFIX:
        return (
            f"That is not a KiCad board file:\n\n    {path}\n\n"
            f"The panel builder opens a board, which is the file ending in "
            f"{BOARD_SUFFIX}. A KiCad project has one beside it."
        )
    try:
        empty = path.stat().st_size == 0
    except OSError as error:
        return (
            f"The panel builder could not read:\n\n    {path}\n\n"
            f"Windows said: {getattr(error, 'strerror', None) or error}"
        )
    if empty:
        return (
            f"That board file is empty, so there is nothing to panelize:\n\n"
            f"    {path}"
        )
    return None


def config_path_for(board: Path) -> Path:
    """The settings file for a board: `panel/panel.json` beside it.

    Computed, never asked for. The layout is fixed by the
    project - the panel folder sits beside the board and holds everything the
    panel builder owns - so a person who has a board has, by that fact, named
    their settings file too.
    """
    return board.parent / "panel" / "panel.json"


def boards_beside(board: Path) -> list:
    """Every board file in the same folder, in a defined order."""
    try:
        found = [
            path
            for path in board.parent.glob(f"*{BOARD_SUFFIX}")
            if path.is_file()
        ]
    except OSError:
        return [board]
    return sorted(found)


def needs_seeding(board: Path) -> bool:
    """True when the launcher must write the settings file itself.

    The engine's `config.load_or_create` writes the starting file, and this
    launcher does NOT own a second copy of that logic. But that
    function finds the board by looking for the ONE board beside the panel
    folder, and it refuses when there are several - which would turn a board
    the engineer had just picked by hand into a refusal. So the launcher steps
    in for exactly one case: no settings file yet, and more than one board in
    the folder. In every other case the engine does it.
    """
    if config_path_for(board).exists():
        return False
    return len(boards_beside(board)) != 1


def seed_config(board: Path, runtime_result) -> Path:
    """Write the starting settings file, naming the board that was chosen.

    THE ENGINE'S OWN DEFAULTS, not a second set. This calls
    `eedesignpanels.config.default_config_data` and `config_text` - the two
    functions `create_config` itself calls - so the file this writes is the
    same file the engine would have written, with the board named rather than
    guessed. It is the only place the launcher imports the engine, and it is
    reached only when :func:`needs_seeding` says the engine cannot answer.
    """
    if runtime_result.site_packages is not None:
        entry = str(runtime_result.site_packages)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from eedesignpanels.config import config_text, default_config_data

    config_path = config_path_for(board)
    data = default_config_data(f"../{board.name}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_text(data), encoding="utf-8")
    return config_path


def named_board(config_path: Path) -> str | None:
    """The board a settings file names, read as one field and nothing more.

    A plain JSON read, NOT the config loader. The only use is a sentence
    warning that the file points somewhere else, and a warning should not stop
    a launch - so an unreadable file returns None and the launch goes on to let
    the engine give the real answer.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("source_board") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def board_mismatch(board: Path, config_path: Path) -> str | None:
    """A sentence when the settings file names a different board, else None.

    THE FILE WINS, and the person is told. Rewriting somebody's settings file
    because they double-clicked a neighbouring board would destroy settings
    they built up, and opening the other board in silence would confuse them,
    so it is said out loud and the sentence names the file to edit.
    """
    if not config_path.exists():
        return None
    named = named_board(config_path)
    if not named:
        return None
    try:
        resolved = (config_path.parent / named).resolve()
    except OSError:
        return None
    if resolved == board.resolve():
        return None
    return (
        f"Note: the settings in {config_path} are for {resolved.name}, not "
        f"{board.name}, so that is the board being opened. Change the "
        f"'source_board' line in that file to use the other one."
    )


# ---------------------------------------------------------------------------
# running the right Python
# ---------------------------------------------------------------------------


def newest_install(runtime):
    """The newest KiCad on this machine, whichever one is running this code.

    This differs on purpose from what the KiCad door does.
    `runtime.kicad_installs` puts the RUNNING KiCad first, which is right for a
    plugin - it must build a runtime for the KiCad that loaded it. This door is
    loaded by nobody: the shortcut takes whichever KiCad python the registry
    hands it first, which alphabetically can be an old one, so the newest is
    chosen here instead.
    """
    installs = runtime.kicad_installs()
    if not installs:
        # Let the bootstrap raise its own "KiCad is not installed" message,
        # so both doors show the same wording from one place.
        runtime.kicad_install()
    return sorted(
        installs, key=lambda i: runtime.version_sort_key(i.version), reverse=True
    )[0]


def should_switch_interpreter(current: str, wanted: Path) -> bool:
    """True when this process is not on the interpreter that should run it."""
    if not current:
        return True
    try:
        return Path(current).resolve() != Path(wanted).resolve()
    except OSError:
        return str(current) != str(wanted)


def switch_command(interpreter: Path, board: Path, args) -> list:
    """This same file again, on the right Python, with the board already known.

    `--no-switch` is always passed. It makes one hop the most that can ever
    happen - without it, two installs
    that each thought the other was newer would hand the launch back and
    forth until the machine ran out of processes.
    """
    command = [
        str(interpreter),
        str(Path(__file__).resolve()),
        "--board",
        str(board),
        "--no-switch",
    ]
    if args.no_open:
        command.append("--no-open")
    return command


# ---------------------------------------------------------------------------
# starting the engine
# ---------------------------------------------------------------------------


def preview_command(
    interpreter, config_path: Path, open_browser: bool, site_packages=None
) -> list:
    """The command that starts the panel builder on a settings file.

    IT GOES THROUGH `run_engine.py` RATHER THAN `-m`. KiCad 10.0.4's bundled
    `python.exe` ignores `PYTHONPATH` completely - the folder never reaches
    `sys.path` - so `-m eedesignpanels.preview` with the runtime named in the
    environment dies with `No module named 'eedesignpanels'`. The same
    variable on an ordinary Python works. So the runtime folder is passed as
    an ARGUMENT and put on the path from inside the child.

    Everything after that argument is the engine's own argument list,
    unchanged, so the shortcut runs the same program as the command the user
    types.
    """
    command = [
        str(interpreter),
        str(RUN_ENGINE_SOURCE),
        NO_RUNTIME if site_packages is None else str(site_packages),
        str(config_path),
    ]
    if open_browser:
        command.append("--open")
    return command


def launch_environment(runtime, runtime_result) -> dict:
    """The child's environment: UTF-8 on, and the runtime named for any Python.

    `PYTHONUTF8` because the child prints file paths, and a board stored under
    a name holding an accented character should not stop a launch.

    `PYTHONPATH` is still set, even though the interpreter this door usually
    starts ignores it. It costs nothing, it is correct on
    every other Python, and the argument route in `run_engine.py` is what
    actually carries the runtime - so this is the belt, not the braces.
    """
    env = runtime.launch_environment(runtime_result)
    env["PYTHONUTF8"] = "1"
    return env


class SetupProgress:
    """What the first run looks like: one sentence, then a dot per step.

    Eight seconds of silence reads as a broken shortcut. The bootstrap already
    says what it is doing before it starts, but the minute after that is a
    download with nothing on screen, so every line the
    installer produces becomes one dot. A dot is proof of life that a
    non-programmer does not have to read, where the installer's own lines are
    a wall of text that looks like something went wrong.

    It also owns the newline. The dots and the bootstrap's closing sentence go
    to the same stream, so somebody has to break the line between them, and
    the thing printing the dots is the only one that knows there are any.
    """

    def __init__(self, write=print):
        self._write = write
        self.dots = 0

    def say(self, message: str) -> None:
        """A milestone from the bootstrap, on a line of its own."""
        self.finish()
        self._write(message)

    def tick(self, line: str) -> None:
        """One step of the install. The line itself is deliberately dropped."""
        self.dots += 1
        self._write(".", end="", flush=True)

    def finish(self) -> None:
        """End the dot line, if one was started."""
        if self.dots:
            self._write("")
            self.dots = 0


# The most of the engine's own words a failure message will ever carry. A
# message stands alone on screen, which means it carries the REASON, not a
# pointer at output somebody may not be able to see. Three lines is what the
# engine's refusals actually are, and more than that stops being a message and
# starts being a log.
REASON_LINES = 3

_TRACEBACK_MARKER = "Traceback (most recent call last)"


def engine_reason(lines) -> str:
    """The engine's own last plain words, with any traceback thrown away.

    No traceback, not even quoted inside a sentence. The engine's refusals are
    already written for a person - "cannot start: there is no board to
    panelize in ..." - and those are worth repeating. A stack
    trace is not, so everything from the marker onwards is dropped and an
    output that held nothing else returns empty, which makes the caller fall
    back to a message that stands on its own.
    """
    kept = []
    for line in lines:
        if _TRACEBACK_MARKER in line:
            break
        text = line.strip()
        if text:
            kept.append(text)
    return "\n".join(f"    {line}" for line in kept[-REASON_LINES:])


def echo_now(line: str) -> None:
    """Print one line of the engine's output and push it out at once.

    FLUSHED, and it is not a nicety. Measured: without it a caller
    reading this launcher's output through a pipe saw NOTHING for the whole
    run - Python block-buffers a piped stdout, so every line sat in an 8 KB
    buffer while the server ran below. The engine flushes its own last line for
    exactly this reason, and re-printing it unflushed here put the fault back.
    """
    print(line, flush=True)


def run_and_watch(command, env, cwd, echo=echo_now) -> tuple:
    """Run the engine, show its output live, and keep the last of it.

    Both, and neither one alone. Streaming is what makes the session feel like
    the command the user types by hand. Keeping the tail is what lets a failure
    message carry the real reason instead of telling somebody to go and read a
    console.
    """
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    seen = []
    if process.stdout is not None:
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            seen.append(line)
            echo(line)
        process.stdout.close()
    return process.wait(), seen


def has_console() -> bool:
    """True when there is a console window for a printed sentence to land in."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (ImportError, AttributeError, OSError):
        return True


def show_problem(message: str, console=None) -> None:
    """Put a failure where the person will see it, and never a traceback.

    Printed always, because the shortcut opens a console and
    holds it open on a failure. Also shown as a message box when there is NO
    console - somebody who started this another way would otherwise get a
    window that flashes and is gone.
    """
    print(message, file=sys.stderr)
    if console is None:
        console = has_console()
    if console or os.name != "nt":
        return
    try:
        import ctypes

        # The same declaration the file dialog makes. A message box is a
        # window too, and a soft one reads as a broken tool at
        # the exact moment somebody is already being told something failed.
        declare_dpi_awareness()
        ctypes.windll.user32.MessageBoxW(
            None, message, "eedesignpanels", 0x10  # MB_ICONERROR
        )
    except (ImportError, AttributeError, OSError):
        return


# ---------------------------------------------------------------------------
# runnable
# ---------------------------------------------------------------------------


def parse_arguments(argv: list):
    """The two things this takes: a board, and whether to open a browser."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="launch_eedesignpanels",
        description=(
            "Open the panel builder on a board. With no board it asks for one."
        ),
    )
    parser.add_argument(
        "board",
        nargs="?",
        help=(
            "the .kicad_pcb to panelize; this is also what a board DROPPED "
            "ON the shortcut arrives as"
        ),
    )
    parser.add_argument(
        "--board", dest="board_option", help="the same thing, named"
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="start the session but do not open a browser",
    )
    parser.add_argument(
        "--no-switch",
        action="store_true",
        help="stay on this interpreter even if a newer KiCad has one",
    )
    return parser.parse_args(argv)


def resolve_board(args, runtime, asker) -> Path | None:
    """The board to open: the argument, or the dialog. None means cancelled."""
    given = args.board_option or args.board
    if given:
        return Path(given).expanduser()
    board = asker(remembered_folder(runtime))
    if board is not None:
        remember_folder(runtime, board.parent)
    return board


def main(argv: list = None, asker=None, runner=None) -> int:
    """Open the panel builder on a board. Every failure is one sentence.

    *asker* and *runner* are the two seams a test drives - the file dialog and
    the child process, so a test raises no window on a desktop and starts no
    server. *runner* answers `(exit code, the lines it printed)`.
    """
    # EVERY LINE LEAVES AS IT IS WRITTEN. Python block-buffers a stdout that
    # is a pipe, so a launcher started by anything other than a console says
    # nothing at all until it ends - and this one does not end, it serves.
    # Measured: with the buffer in place a caller reading this output saw zero
    # lines through a four-minute run.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError, ValueError):
        pass
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    asker = ask_for_board if asker is None else asker
    runner = run_and_watch if runner is None else runner
    try:
        runtime = load_runtime()
    except LauncherProblem as problem:
        show_problem(str(problem))
        return EXIT_PROBLEM

    try:
        board = resolve_board(args, runtime, asker)
        if board is None:
            print("No board chosen. Nothing was opened.")
            return EXIT_OK
        problem = board_problem(board)
        if problem is not None:
            show_problem(problem)
            return EXIT_BOARD
        board = board.resolve()

        if not args.no_switch:
            install = newest_install(runtime)
            if should_switch_interpreter(sys.executable, install.interpreter):
                # Said out loud in one line: a second Python starting costs
                # two seconds, and a silent re-run looks like a hang.
                print(
                    f"Switching to the Python that comes with KiCad "
                    f"{install.version}."
                )
                # The second run owns every message from here on, including
                # its own failures, so this one just hands back its answer.
                code, _ = runner(
                    switch_command(install.interpreter, board, args),
                    env=None,
                    cwd=None,
                )
                return code

        # THE ONE SENTENCE BEFORE THE SILENCE. `ensure`
        # says it is setting up before pip runs, and the dots are what proves
        # the minute that follows is work rather than a hung shortcut.
        progress = SetupProgress()
        result = runtime.ensure(report=progress.say, on_output=progress.tick)
        progress.finish()

        config_path = config_path_for(board)
        if needs_seeding(board):
            seed_config(board, result)
            print(f"Settings written for {board.name}: {config_path}")
        mismatch = board_mismatch(board, config_path)
        if mismatch:
            print(mismatch)

        command = preview_command(
            result.interpreter,
            config_path,
            not args.no_open,
            result.site_packages,
        )
        environment = launch_environment(runtime, result)
    except runtime.RuntimeProblem as problem:
        show_problem(problem.message)
        return EXIT_PROBLEM
    except LauncherProblem as problem:
        show_problem(str(problem))
        return EXIT_PROBLEM

    code, output = runner(command, env=environment, cwd=str(board.parent))
    if code == EXIT_OK:
        return EXIT_OK
    show_problem(engine_failure(code, board, config_path, output))
    return EXIT_BOARD if code == EXIT_BOARD else EXIT_PROBLEM


def engine_failure(code: int, board: Path, config_path: Path, output) -> str:
    """One self-contained message for a run of the engine that did not finish.

    IT CARRIES THE REASON. A message that says "see the
    output above" is useless to somebody whose console has already gone, and
    somebody reading it in a message box has no "above" at all. So the engine's
    own last plain words are quoted inside the message, and when it left
    nothing but a traceback the message says what to do instead of showing it.
    """
    reason = engine_reason(output)
    said = f"\n\nWhat it said:\n\n{reason}" if reason else ""
    if code == EXIT_BOARD:
        return (
            f"The panel builder could not open this board:\n\n"
            f"    {board}\n\n"
            f"Its settings are in:\n\n    {config_path}"
            f"{said}\n\n"
            f"Nothing was changed - not the board, and not the settings."
        )
    return (
        f"The panel builder stopped before it finished, opening:\n\n"
        f"    {board}"
        f"{said}\n\n"
        f"Try once more. If it stops again, keep this message. A problem "
        f"report needs it."
    )


if __name__ == "__main__":
    sys.exit(main())
