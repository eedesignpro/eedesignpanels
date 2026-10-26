"""Guarantee a working Python runtime for the panel builder engine.

THE PROBLEM. The plugin catalogue installs a plain file tree and resolves no
dependencies. KiCad's bundled Python ships none of the packages the engine
needs: `eedesigncore`, `pydantic` and `zstandard` are all absent from
`C:\\Program Files\\KiCad\\10.0\\bin\\python.exe`. Installing into
`C:\\Program Files` needs admin rights, and a KiCad upgrade wipes it. A user may
have no Python of their own at all.

THE APPROACH: the runtime is built with KICAD'S OWN BUNDLED PYTHON, because it
is the only interpreter certain to exist on a machine that can open a board.
That interpreter ships pip (KiCad 10.0: Python 3.11.5), so it can fill the
runtime itself with no second install and no admin rights.

Three consequences follow:

* The runtime is per-user, under the user's local application data, so no
  install needs admin rights and no KiCad upgrade can wipe it.
* The runtime is keyed to the KiCad install that built it and to that
  interpreter's Python ABI, because `pydantic` and `zstandard` ship compiled
  wheels. A KiCad upgrade that moves Python gets a fresh runtime rather than a
  tree of wheels the new interpreter cannot load.
* The interpreter is RESOLVED, never configured and never hardcoded. Windows is
  asked where KiCad is: the running process first - inside `kicad.exe` the
  bundled `python.exe` is its own neighbour - then the registry's uninstall
  entries. No version number and no install folder is written down here.

WHAT IT PROMISES ITS CALLERS. `ensure()` returns only when the engine can be
run, and raises `RuntimeProblem` with a message a non-programmer reads once
otherwise. A traceback is never the user-facing answer. A fill that fails takes
its half-built runtime with it, so the next run starts clean rather than
inheriting something that half works.

WHAT IT DOES NOT IMPORT. It imports neither `wx` nor `pcbnew` - it runs before
either door is open - and it never imports the engine, not even to ask whether
the engine is installed. `importlib.metadata` answers that from package
metadata without executing a line of it.

WHERE THE PARTS COME FROM. Public sources only, each pinned to an exact tag: the engine from the source archive
of this repository's release tag, the board library `eedesigncore` from the
source archive of its own release tag, and the third-party wheels they need
from the Python package index pip is set up to use. A user with no network, or
a test, names a local folder of wheel files instead - see
`WHEEL_FOLDER_VARIABLE`. The developer build from a clone is asked for by name
or does not happen - see `LOCAL_BUILD_VARIABLE`.

BOTH DOORS CALL THIS. The standalone launcher and the KiCad plugin both stand
on it, which is why it is stdlib only and works the same from inside KiCad's
interpreter and from a plain shell.

Run it directly to see or manage the runtime:

    <python> runtime.py --status
    <python> runtime.py --fill
    <python> runtime.py --remove
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The engine, named as a pip requirement. ONE requirement is named here and
# the engine's own metadata pulls the rest, because a second list of
# dependencies in a second place goes stale silently. `tests/test_runtime_bootstrap.py` asserts this version against
# `pyproject.toml` at the repository root so the two cannot drift.
ENGINE_DISTRIBUTION = "eedesignpanels"
ENGINE_VERSION = "0.1.0"

# The board library, pinned exactly. It must equal the engine's own pin in
# `pyproject.toml` at the repository root: pip refuses the fill when the two disagree,
# so a drift is a loud failure and never a silent mix.
CORE_DISTRIBUTION = "eedesigncore"
CORE_VERSION = "0.38.0"

# What the engine pulls in, for the error message only, so a failure names
# what is missing. The engine's name alone tells a user nothing. Not used to
# resolve anything.
ENGINE_NEEDS = (CORE_DISTRIBUTION, "pydantic", "zstandard")

# THE RELEASE THIS PLUGIN BELONGS TO. The engine and this plugin are released
# together from one repository with one version number, so the plugin installs
# the engine from the source archive of its own release tag. The tag is "v"
# plus the plugin version in `metadata.json`, and the engine version above is
# the same number; `tools/build_package_file.py` refuses to build a package
# file when any of them disagree.
RELEASE_TAG = "v0.1.0"

# The public sources, as pip direct references. Two fixed archives at fixed
# tags, so a plugin release names exactly what it installs. The engine is the
# Python package at the root of its repository, so the archive needs no
# subfolder. Nothing else is named: the third-party wheels (`pydantic`, `zstandard` and
# the rest) come from the package index pip is set up to use, PyPI by default.
ENGINE_REPOSITORY = "https://github.com/eedesignpro/eedesignpanels"
CORE_REPOSITORY = "https://github.com/eedesignpro/eedesigncore"
ENGINE_SOURCE_URL = f"{ENGINE_REPOSITORY}/archive/refs/tags/{RELEASE_TAG}.zip"
CORE_SOURCE_URL = f"{CORE_REPOSITORY}/archive/refs/tags/v{CORE_VERSION}.zip"
PUBLIC_SOURCES = (CORE_SOURCE_URL, ENGINE_SOURCE_URL)

# THE OFFLINE ROUTE. A folder of wheel files, named by this variable, replaces
# every network source: pip is given `--no-index --find-links <folder>`, so
# nothing is downloaded. It is for a computer with no network, for an
# air-gapped site, and for testing a release before it is published. The
# folder must hold a wheel for the engine, for `eedesigncore`, and for every
# package they need, built for KiCad's own Python. Absent or blank means the
# public sources above.
WHEEL_FOLDER_VARIABLE = "EEDESIGNPANELS_WHEEL_FOLDER"

# Where the runtime lives, under the user's local application data:
#     %LOCALAPPDATA%\\eedesignpro\\eedesignpanels\\runtime\\<slot>\\site-packages
# Local application data, because it is per-user, needs no admin rights, is not roamed onto the network (these are compiled wheels, tens
# of megabytes, and they are machine-specific), and no KiCad upgrade touches it.
VENDOR_FOLDER = "eedesignpro"
PRODUCT_FOLDER = "eedesignpanels"

# The stamp file. It is written LAST and it is the only proof a fill finished, so a half-built runtime is indistinguishable from no runtime
# and gets replaced rather than trusted.
STAMP_NAME = "runtime.json"
STAMP_SCHEMA = "eedesignpanels-runtime/1"
PACKAGES_NAME = "site-packages"
FILL_LOG_NAME = "last-fill.log"

# The engine's own import name, checked inside the tree. The stamp says a fill
# FINISHED, and that is not the same as the engine being there - a tree somebody pruned, or a fill
# that wrote around files another session held open, stamps as complete and
# then fails at the import. One cheap look inside turns that into a repair
# instead of a message about a folder to delete by hand.
ENGINE_PACKAGE = "eedesignpanels"

# Where a condemned runtime waits to be deleted. A slot is RENAMED out of the
# way before anything is deleted, so a name carrying this mark is never read
# as a runtime.
DISCARD_MARK = ".discarded-"

# The engine's build folder, for the local fill. Found relative to this file,
# so it only resolves in a clone of the repository, which is where an
# unpublished build is tested. A catalogue install has no such folder.
WHEEL_FOLDER = "dist"

# THE LOCAL BUILD IS DEVELOPER OPT-IN. A local build that stood in for a
# missing published package would hide the very defect a release test needs
# to expose, on any machine that has a clone on disk. So it happens only when
# a developer asks for it by name. On any other machine, a missing package is
# a plain failure naming the sources that were asked. Nothing a catalogue
# install sets this.
LOCAL_BUILD_VARIABLE = "EEDESIGNPANELS_LOCAL_BUILD"
LOCAL_BUILD_YES = ("1", "true", "yes", "on")

# THE RUNTIME FOLDER CAN BE REDIRECTED, FOR A CHECK OR A TEST. On a real run the
# shell's answer for local application data is used, not %LOCALAPPDATA% (see
# `local_app_data`), so setting %LOCALAPPDATA% does not move the runtime. This
# variable is how a check proves a release on a machine that already has a
# runtime: without it, the check would read the existing warm runtime and prove
# nothing. It works like `EEDESIGNPANELS_SCRATCH`, and nothing the plugin
# catalogue installs sets it. The status readout says when it is set, so a
# redirected run is not mistaken for a real one. Absent or blank means the
# shell answers.
RUNTIME_HOME_VARIABLE = "EEDESIGNPANELS_RUNTIME_HOME"

# How many compiled modules are probed when a rebuild is blocked. Enough to
# name the holder, few enough that a refusal answers in
# about a second. Only ever reached on the failure path.
HOLDER_PROBE_MODULES = 3
HOLDER_PROBE_SECONDS = 10

# A console application launched from a GUI process flashes a console window
# without this flag. pip is a console application and the KiCad door is a GUI
# process.
CREATE_NO_WINDOW = 0x08000000

# Short, so a machine with no network is told so in seconds rather than after pip's default minutes of retrying.
PIP_TIMEOUT_SECONDS = "15"
PIP_RETRIES = "2"
FILL_DEADLINE_SECONDS = 600

_LOCAL_APP_DATA_GUID = "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"


class RuntimeProblem(Exception):
    """A runtime that could not be guaranteed, said in plain words.

    The argument is the whole user-facing answer. Both doors show
    `str(problem)` and nothing else: no traceback and no pip transcript. `detail_path` names a file where the transcript was kept, when
    there is one, so the message can point at it instead of containing it.
    """

    def __init__(self, message: str, detail_path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail_path = detail_path


@dataclass(frozen=True)
class KiCadInstall:
    """One KiCad installation Windows knows about."""

    version: str
    root: Path
    interpreter: Path
    python_tag: str

    @property
    def slot_name(self) -> str:
        """The runtime folder name for this install.

        The KiCad version AND the Python ABI. The ABI decides whether a
        compiled wheel loads. The KiCad version is what a person can read off
        the path.
        """
        return f"kicad-{self.version}-{self.python_tag}"


@dataclass(frozen=True)
class Runtime:
    """A runtime the engine can be run from."""

    interpreter: Path
    site_packages: Path | None
    version: str
    state: str  # "ambient" | "warm" | "filled"
    slot: Path | None = None

    @property
    def ambient(self) -> bool:
        """True when the calling interpreter already had the engine."""
        return self.state == "ambient"


# ---------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------


def _known_folder(guid: str) -> Path | None:
    """The shell's answer for a known folder, or None off Windows.

    Ask the shell rather than read %LOCALAPPDATA%: a redirected folder makes
    the environment variable and the real folder two different places, and
    writing to the wrong one fails silently.
    """
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.wintypes.DWORD),
            ("Data2", ctypes.wintypes.WORD),
            ("Data3", ctypes.wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    import uuid as _uuid

    raw = _uuid.UUID(guid).bytes_le
    handle = GUID(
        int.from_bytes(raw[0:4], "little"),
        int.from_bytes(raw[4:6], "little"),
        int.from_bytes(raw[6:8], "little"),
        (ctypes.c_ubyte * 8)(*raw[8:16]),
    )
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(handle), 0, None, ctypes.byref(out)
    ):
        return None
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def local_app_data() -> Path:
    """The user's local application data folder.

    The shell's answer is used for a real run, and %LOCALAPPDATA% is only the
    fallback, because a redirected folder makes the two disagree and the
    wrong one fails silently. A check or a test that needs the runtime
    somewhere else sets `RUNTIME_HOME_VARIABLE`, which `runtime_home()` reads.
    """
    folder = _known_folder(_LOCAL_APP_DATA_GUID)
    if folder is not None:
        return folder
    # The environment variable is the fallback. Reached only when the shell
    # call fails or the platform is not Windows.
    raw = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


def runtime_home_override() -> Path | None:
    """The folder a check or a test named, or None for a real run.

    Read in exactly one place, so there is one answer to
    "is this run redirected" and the status readout cannot disagree with what a
    door does a second later. A blank value is an absent value - an emptied
    variable is how a caller turns the redirection off.
    """
    named = str(os.environ.get(RUNTIME_HOME_VARIABLE, "")).strip()
    return Path(named) if named else None


def runtime_home() -> Path:
    """The one folder this module owns. Discover it from here, never guess it.

    Every caller asks this module rather than rebuilding the path, because a
    second copy of the path arithmetic is a second thing to get wrong when the
    layout moves. The redirection is applied here and nowhere else, so a
    redirected run cannot half happen.
    """
    named = runtime_home_override()
    if named is not None:
        return named
    return local_app_data() / VENDOR_FOLDER / PRODUCT_FOLDER


def runtime_slot(install: KiCadInstall) -> Path:
    """The runtime folder for one KiCad install."""
    return runtime_home() / "runtime" / install.slot_name


def site_packages_of(slot: Path) -> Path:
    """The folder that goes on `sys.path`.

    The packages sit one level down from the stamp, so the
    stamp is never inside the tree pip owns and pip can never overwrite it.
    """
    return slot / PACKAGES_NAME


def fill_log_path() -> Path:
    """Where the last fill's transcript was kept.

    Outside the slot, because a failed fill deletes its slot
    and the transcript is exactly what survives to be read afterwards.
    """
    return runtime_home() / FILL_LOG_NAME


# ---------------------------------------------------------------------------
# resolving KiCad's interpreter
# ---------------------------------------------------------------------------


def _python_tag(bin_dir: Path) -> str | None:
    """The CPython ABI tag of a KiCad bin folder, read off its Python DLL.

    Read the DLL name rather than launch the interpreter, because the warm
    path runs on every button press and a subprocess is too slow for it. A
    bin folder holding `python311.dll` is a cp311 runtime; that is exactly the
    tag its wheels carry.
    """
    for dll in sorted(bin_dir.glob("python3*.dll")):
        digits = dll.stem[len("python") :]
        if digits.isdigit() and len(digits) >= 2:
            return f"cp{digits}"
    return None


def _install_at(root: Path, version: str) -> KiCadInstall | None:
    """A KiCadInstall for *root*, or None when it carries no usable Python."""
    bin_dir = root / "bin"
    interpreter = bin_dir / ("python.exe" if os.name == "nt" else "python")
    if not interpreter.is_file():
        return None
    tag = _python_tag(bin_dir)
    if tag is None:
        return None
    return KiCadInstall(
        version=version, root=root, interpreter=interpreter, python_tag=tag
    )


def _install_from_running_process() -> KiCadInstall | None:
    """KiCad's install, deduced from the process this code is running in.

    This route is first because it is both free and exact.
    Inside `kicad.exe`, `sys.executable` IS the KiCad binary, so the bundled
    `python.exe` is its own neighbour and no registry read can disagree with
    it. It also catches a plain shell already running KiCad's Python.
    """
    if not sys.executable:
        return None
    bin_dir = Path(sys.executable).resolve().parent
    # The fence that makes this a KiCad bin folder rather than any folder with
    # a python.exe in it. kicad-cli is the marker, because this project already
    # depends on it being there.
    marker = bin_dir / ("kicad-cli.exe" if os.name == "nt" else "kicad-cli")
    if not marker.is_file():
        return None
    return _install_at(bin_dir.parent, _version_of_root(bin_dir.parent))


def _version_of_root(root: Path) -> str:
    """The KiCad version for an install root, from the registry when it knows.

    Falls back to the install folder's own name, which KiCad sets to its major
    version. A name is better than "unknown" in a path a person may have to
    read, and the ABI tag beside it is what
    actually guards wheel compatibility.
    """
    for version, candidate in _registry_installs():
        if candidate == root:
            return version
    return root.name


def _registry_installs() -> list[tuple[str, Path]]:
    """Every (version, root) pair Windows lists for a KiCad install.

    The registry's uninstall entries are the supported way to ask Windows
    where a program is. For example, the entry `KiCad 10.0` under HKLM carries
    `InstallLocation` = `C:\\Program Files\\KiCad\\10.0` and `DisplayVersion` =
    `10.0.4`, which is the pair this needs.
    """
    if os.name != "nt":
        return []
    import winreg

    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    ]
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for hive, view in roots:
        try:
            key = winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                if not name.lower().startswith("kicad"):
                    continue
                pair = _registry_entry(winreg, key, name)
                if pair is None or pair[1] in seen:
                    continue
                seen.add(pair[1])
                found.append(pair)
    return found


def _registry_entry(winreg, parent, name: str) -> tuple[str, Path] | None:
    """(DisplayVersion, InstallLocation) for one uninstall entry."""
    try:
        with winreg.OpenKey(parent, name) as entry:
            location = winreg.QueryValueEx(entry, "InstallLocation")[0]
            try:
                version = winreg.QueryValueEx(entry, "DisplayVersion")[0]
            except OSError:
                version = name.split(" ", 1)[-1]
    except OSError:
        return None
    if not location:
        return None
    return str(version), Path(location)


def version_sort_key(version: str) -> tuple:
    """Sort a dotted version newest-last, tolerating anything non-numeric."""
    parts = []
    for chunk in str(version).replace("-", ".").split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, 0))
    return tuple(parts)


def kicad_installs() -> list[KiCadInstall]:
    """Every KiCad install with a usable bundled Python, newest first.

    The running process, when it is a KiCad one, always leads: a plugin runs
    against the KiCad it was loaded by, and a different install's interpreter
    would build a runtime for the wrong one.
    """
    ordered: list[KiCadInstall] = []
    running = _install_from_running_process()
    if running is not None:
        ordered.append(running)
    from_registry = []
    for version, root in _registry_installs():
        install = _install_at(root, version)
        if install is None:
            continue
        if any(other.root == install.root for other in ordered):
            continue
        from_registry.append(install)
    from_registry.sort(key=lambda i: version_sort_key(i.version), reverse=True)
    return ordered + from_registry


def kicad_install() -> KiCadInstall:
    """The KiCad install whose Python builds and owns the runtime."""
    installs = kicad_installs()
    if installs:
        return installs[0]
    if os.name != "nt":
        # FAIL SOFT OFF WINDOWS. Windows is the tested platform: the search
        # above reads KiCad's bundled Python and the Windows registry, and on
        # Linux or macOS it finds neither. Say so plainly instead of guessing
        # at an interpreter that may not be the one KiCad runs.
        raise RuntimeProblem(
            "The panel builder sets itself up automatically on Windows only, "
            "which is the platform it is tested on. On this system it did not "
            "find the Python that comes with KiCad, so it has installed "
            "nothing.\n\nThe engine can still run from a command line with "
            "Python 3.11 or later. The README tells how."
        )
    raise RuntimeProblem(
        "The panel builder could not find KiCad on this computer, so it has "
        "no Python to set itself up with. It looked at the program it is "
        "running inside and at the list of installed programs Windows keeps, "
        "and neither one named a KiCad installation. Install KiCad 10 and try "
        "again."
    )


# ---------------------------------------------------------------------------
# the stamp: is the runtime there, and is it the right one
# ---------------------------------------------------------------------------


def read_stamp(slot: Path) -> dict | None:
    """The stamp in *slot*, or None when it is absent or unreadable.

    Unreadable counts as absent. A stamp we cannot parse
    tells us nothing about the tree beside it, and refilling costs one minute
    once where trusting it could cost a wrong panel.
    """
    path = slot / STAMP_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        stamp = json.loads(text)
    except ValueError:
        return None
    return stamp if isinstance(stamp, dict) else None


def stamp_matches(stamp: dict | None, install: KiCadInstall) -> bool:
    """True when *stamp* describes the runtime this version of the code wants.

    The version is checked as well as the presence, so the runtime is
    replaced when the engine's version moves. Every field is compared; a stamp missing one fails.
    """
    if not stamp:
        return False
    return (
        stamp.get("schema") == STAMP_SCHEMA
        and stamp.get("distribution") == ENGINE_DISTRIBUTION
        and stamp.get("version") == ENGINE_VERSION
        and stamp.get("python_tag") == install.python_tag
        and stamp.get("interpreter") == str(install.interpreter)
    )


def _package_present(packages: Path, name: str) -> bool:
    """True when *packages* holds an importable package or module *name*."""
    return (packages / name).is_dir() or (packages / f"{name}.py").is_file()


def damage(slot: Path, install: KiCadInstall) -> str | None:
    """Why the runtime in *slot* cannot be trusted, or None when it is sound.

    This is the one definition of "damaged". Every door asks it rather than
    deciding for itself, so the launcher and the KiCad door cannot disagree
    about a runtime they share. What it returns is
    printed to a person, so it is a plain reason and never a code.

    The four answers, cheapest first: no packages folder, no readable record,
    a record describing a different KiCad or a different engine version, and a
    record that claims a complete tree the engine is not actually in.
    """
    packages = site_packages_of(slot)
    if not packages.is_dir():
        return "the folder holding its packages is missing"
    stamp = read_stamp(slot)
    if stamp is None:
        return "the record of what was installed is missing or unreadable"
    if not stamp_matches(stamp, install):
        return (
            "it was built for a different KiCad, or for a different version "
            "of the panel builder"
        )
    # THE TREE AGAINST ITS OWN RECORD. The record says what the fill put there, so a tree that has lost the
    # engine since is damaged and gets rebuilt. A record written before this
    # field existed is taken at its word, because the alternative is throwing
    # away a runtime that works for want of one line in a file.
    if stamp.get("engine", True) and not _package_present(packages, ENGINE_PACKAGE):
        return "the panel builder engine is not in it, though its record says it is"
    return None


def ready_slot(install: KiCadInstall) -> Path | None:
    """The folder holding a sound runtime for *install*, or None.

    THE STAMP IS THE IDENTITY, THE FOLDER NAME IS A LABEL.
    The name carries the KiCad version so a person can read it, and that
    version comes from the registry when the registry knows the install and
    from the folder name when it does not - two routes that can answer
    `10.0.4` and `10.0` for one install and send a healthy runtime to be built
    a second time. So the preferred name is tried first and then every other
    slot is offered to the same check, and a runtime whose record says it was
    built by THIS interpreter for THIS engine version is used wherever it sits.
    """
    preferred = runtime_slot(install)
    if damage(preferred, install) is None:
        return preferred
    home = runtime_home() / "runtime"
    if not home.is_dir():
        return None
    for slot in sorted(home.iterdir()):
        if slot == preferred or DISCARD_MARK in slot.name or not slot.is_dir():
            continue
        if damage(slot, install) is None:
            return slot
    return None


def runtime_is_ready(install: KiCadInstall) -> bool:
    """True when a finished, current, undamaged runtime exists for *install*."""
    return ready_slot(install) is not None


def _holders_of(slot: Path) -> list[str]:
    """The programs holding a file in *slot* open, as `name (pid)`.

    A refusal names what is in the way, because "close the thing using it" is
    not an instruction anybody can follow. Windows'
    own task list is asked which processes have one of the runtime's compiled
    modules loaded - those are the files that cannot be replaced while a
    session is running, so they are the ones worth asking about.
    """
    # Windows only, and it fails soft: off Windows, or with no task list
    # program where Windows keeps it, the refusal still happens and simply
    # names no holder.
    if os.name != "nt":
        return []
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    tasklist = system_root / "System32" / "tasklist.exe"
    if not tasklist.is_file():
        return []
    import csv

    modules = sorted(p.name for p in slot.rglob("*.pyd"))[:HOLDER_PROBE_MODULES]
    found: set[str] = set()
    for module in modules:
        try:
            answer = subprocess.run(
                [str(tasklist), "/FI", "MODULES eq " + module, "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=HOLDER_PROBE_SECONDS,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return sorted(found)
        for row in csv.reader(answer.stdout.splitlines()):
            if len(row) >= 2 and row[1].strip().isdigit():
                found.add(f"{row[0]} ({row[1]})")
    return sorted(found)


def _discard(slot: Path) -> None:
    """Throw one runtime away, or refuse and say what is holding it.

    RENAME FIRST, DELETE SECOND, so a slot is not half deleted around files
    that will not go. Windows refuses to rename a folder
    holding an open file or a loaded compiled module, so the rename IS the
    test for "a session is still using this" - and it is the same operation as
    the answer, so nothing can change between asking and acting.

    The case this guards: a rebuild ran while a session held
    `pydantic`'s compiled module open, the delete skipped what it could not
    remove, pip filled around the survivors, and the stamp was written anyway
    - a runtime reported as fresh that was half old. It is idempotent: a slot
    that is not there is already discarded.
    """
    if not slot.exists():
        return
    mark = f"{DISCARD_MARK}{os.getpid()}-{int(time.time())}"
    condemned = slot.with_name(slot.name + mark)
    try:
        slot.rename(condemned)
    except OSError:
        holders = _holders_of(slot)
        named = (
            "Still using it: " + ", ".join(holders) + "."
            if holders
            else "Another program on this computer still has it open."
        )
        raise RuntimeProblem(
            "The panel builder has to rebuild the copy of itself it keeps on "
            "this computer, and something is still using the old one, so it "
            f"has changed nothing.\n\n{named}\n\nClose it and start the panel "
            f"builder again.\n\nThe copy it needs to replace is:\n\n    {slot}"
        ) from None
    shutil.rmtree(condemned, ignore_errors=True)
    _sweep_discarded(slot.parent)


def _sweep_discarded(home: Path) -> None:
    """Delete condemned folders an earlier run could not finish removing."""
    if not home.is_dir():
        return
    for path in home.iterdir():
        if DISCARD_MARK in path.name and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def measure_tree(folder: Path) -> tuple[int, int]:
    """(file count, total bytes) under *folder*."""
    files = 0
    total = 0
    for path in folder.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    return files, total


def _write_stamp(slot: Path, install: KiCadInstall, source: str | None) -> dict:
    """Write the stamp. Called last, once the tree is complete.

    The stamp records the time it was filled. Output files stay reproducible,
    because no byte of this file reaches a board. The time is here so a
    support question about a stale runtime has an answer.
    """
    packages = site_packages_of(slot)
    files, total = measure_tree(packages)
    stamp = {
        "schema": STAMP_SCHEMA,
        "distribution": ENGINE_DISTRIBUTION,
        "version": ENGINE_VERSION,
        "python_tag": install.python_tag,
        "kicad_version": install.version,
        "interpreter": str(install.interpreter),
        # EVERY SOURCE THE FILL WAS GIVEN, so a question about where a runtime
        # came from is answered by reading its record. None for the developer
        # build, which `source` names instead.
        "sources": list(fill_sources()) if source is None else None,
        "source": source,
        "filled_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "bytes": total,
        # What the tree is checked against later. The record states what this
        # fill actually put there, so "the record lies" is a
        # comparison and never a guess.
        "engine": _package_present(packages, ENGINE_PACKAGE),
    }
    (slot / STAMP_NAME).write_text(
        json.dumps(stamp, indent=2) + "\n", encoding="utf-8"
    )
    return stamp


# ---------------------------------------------------------------------------
# is the engine already here
# ---------------------------------------------------------------------------


def installed_engine_version() -> str | None:
    """The engine's version as the CALLING interpreter already sees it.

    Reads package metadata and does not import the engine. Importing it would
    drag in `pydantic` and the whole board reader just to answer a
    yes-or-no question, and inside KiCad it would raise before the runtime this
    module exists to build is on the path. `importlib.metadata` reads the
    distribution's METADATA file and executes none of its code.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(ENGINE_DISTRIBUTION)
    except PackageNotFoundError:
        return None


# ---------------------------------------------------------------------------
# filling the runtime
# ---------------------------------------------------------------------------


def wheel_folder() -> Path | None:
    """The offline wheel folder a user named, or None for the public sources.

    Read in exactly one place, as `runtime_home_override` is, so the status
    readout and the fill can never disagree about where the parts come from.
    A blank value is an absent value.
    """
    named = str(os.environ.get(WHEEL_FOLDER_VARIABLE, "")).strip()
    return Path(named).expanduser() if named else None


def fill_sources() -> tuple[str, ...]:
    """Where a fill takes the engine and the board library from, in words."""
    folder = wheel_folder()
    if folder is not None:
        return (f"the wheel folder {folder}",)
    return PUBLIC_SOURCES


def _pip_command(
    install: KiCadInstall, slot: Path, source: str | None
) -> list[str]:
    """The pip call that fills the runtime."""
    command = [
        str(install.interpreter),
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages_of(slot)),
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--retries",
        PIP_RETRIES,
        "--timeout",
        PIP_TIMEOUT_SECONDS,
    ]
    if source is not None:
        # The development route only, reached by `--from` or by the opt-in
        # local build. Neither door passes this on its own. It exists so the
        # launcher and the KiCad door can be run before a release.
        command.append(source)
        return command
    # Ready-built packages only for everything third-party. A user's machine
    # may have no C compiler, so a source build would spend a minute failing.
    # A missing wheel is reported as missing.
    command += ["--only-binary", ":all:"]
    folder = wheel_folder()
    if folder is not None:
        # OFFLINE: the folder is the only source. `--no-index` turns every
        # package index off, so nothing on this route reaches a network.
        command += ["--no-index", "--find-links", str(folder)]
        command += [
            f"{CORE_DISTRIBUTION}=={CORE_VERSION}",
            f"{ENGINE_DISTRIBUTION}=={ENGINE_VERSION}",
        ]
        return command
    # PUBLIC: the two pure-Python packages come as source archives at fixed
    # tags, so they are let out of the wheels-only rule by name and built
    # here. Both are pure Python, so the build needs no compiler. Both are
    # named on the command line so the engine's own pin on the board library
    # is met by the pinned archive and never looked up on an index.
    command += ["--no-binary", f"{CORE_DISTRIBUTION},{ENGINE_DISTRIBUTION}"]
    command += [
        f"{CORE_DISTRIBUTION} @ {CORE_SOURCE_URL}",
        f"{ENGINE_DISTRIBUTION} @ {ENGINE_SOURCE_URL}",
    ]
    return command


# Markers in pip's own output that mean "the network is the problem". The
# failure is classified rather than forwarded, because the one
# answer a user can act on is "check the connection, or use the offline wheel
# folder" and pip never says that.
_NETWORK_MARKERS = (
    "newconnectionerror",
    "failed to establish a new connection",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "connection refused",
    "connection reset",
    "read timed out",
    "connecttimeout",
    "proxyerror",
    "sslerror",
    "certificate verify failed",
    "there was a problem confirming the ssl certificate",
)

_MISSING_MARKERS = (
    "no matching distribution found",
    "could not find a version that satisfies the requirement",
)


def _server_has_no_such_version(transcript: str) -> bool:
    """True when pip's answer was "the index does not have that"."""
    lowered = transcript.lower()
    return any(marker in lowered for marker in _MISSING_MARKERS)


def engine_project() -> Path | None:
    """The engine's source project in this repository, or None.

    Found by walking up from THIS file. This file is
    `kicad_plugin/plugins/runtime.py`, and the engine's project is the
    repository root two folders above `plugins/`. An installation from the
    plugin catalogue has no such neighbour, so this answers None there and the
    local route does not exist.
    """
    here = Path(__file__).resolve()
    if len(here.parents) < 3:
        return None
    project = here.parents[2]
    return project if (project / "pyproject.toml").is_file() else None


def local_build_allowed(environment: dict | None = None) -> bool:
    """True when a developer has asked for the local build BY NAME.

    An environment variable, because it is set per machine by the person who
    wants it and cannot be reached from a board file, a config, or anything
    the plugin catalogue installs. An absent or unrecognised value is "no". On
    a user's machine the published sources are the only path, so their
    failure is visible.
    """
    env = os.environ if environment is None else environment
    return str(env.get(LOCAL_BUILD_VARIABLE, "")).strip().lower() in LOCAL_BUILD_YES


def local_source() -> tuple[str, str] | None:
    """What pip should be given locally, and one plain line naming it.

    This is how an unreleased build is tested on a real machine through the
    real doors. A built wheel is preferred over the source folder because it
    is the same artifact a release carries.

    `fill` asks `local_build_allowed()` first, so this answers what the local
    build WOULD be and does not decide that it happens.
    """
    project = engine_project()
    if project is None:
        return None
    wheels = sorted((project / WHEEL_FOLDER).glob(f"*{ENGINE_VERSION}*.whl"))
    if wheels:
        newest = max(wheels, key=lambda path: path.stat().st_mtime)
        return str(newest), (
            f"Installed from a local build on this computer, NOT from the "
            f"published release: {newest}"
        )
    return str(project), (
        f"Installed from the engine's source folder on this computer, NOT "
        f"from the published release: {project}"
    )

def _plain_words_failure(transcript: str, log: Path | None) -> RuntimeProblem:
    """Turn a pip failure into one paragraph, and never a traceback."""
    lowered = transcript.lower()
    needs = ", ".join(ENGINE_NEEDS)
    where = f"\n\nThe full log is in {log}." if log is not None else ""
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return RuntimeProblem(
            "The panel builder could not download the parts it needs and has "
            "not been set up. It tried to reach github.com and the Python "
            "package index and got no answer.\n\n"
            "Check the internet connection and start the panel builder again. "
            "This download happens once; after it has worked, the panel "
            "builder starts without the network.\n\n"
            "For a computer with no internet connection, put the wheel files "
            f"in a folder and name that folder in {WHEEL_FOLDER_VARIABLE}. "
            "The README tells how.\n\n"
            f"What is missing: the panel builder engine and {needs}."
            f"{where}",
            detail_path=log,
        )
    if any(marker in lowered for marker in _MISSING_MARKERS):
        # NAME THE SOURCES. They are the answer to "why did it not find it",
        # so they are in the message and not only in the log.
        asked = "\n".join(f"    {item}" for item in fill_sources())
        return RuntimeProblem(
            "The package sources answered, but one of them does not have a "
            "part at the version this plugin expects "
            f"({ENGINE_DISTRIBUTION} {ENGINE_VERSION}, "
            f"{CORE_DISTRIBUTION} {CORE_VERSION}), or has no ready-built "
            "package for KiCad's own Python. Nothing was installed and "
            "nothing was left half-finished.\n\n"
            f"The sources it asked:\n\n{asked}\n\n"
            "A wheel folder must hold a wheel for the engine, for "
            f"{CORE_DISTRIBUTION}, and for every package they need, built for "
            "the Python that comes with KiCad."
            f"{where}",
            detail_path=log,
        )
    return RuntimeProblem(
        "The panel builder could not finish setting itself up, and it has "
        "removed what it had started so the next attempt begins clean.\n\n"
        "Try again once. If it fails a second time, keep the log named below. "
        "A problem report needs it."
        f"{where}",
        detail_path=log,
    )


def _version_in(packages: Path, distribution: str) -> str | None:
    """The version of *distribution* that pip put in *packages*, or None.

    Read from the `.dist-info` folder name, which pip writes as
    `<name>-<version>.dist-info` with the name normalized to underscores.
    Executes none of the package's code.
    """
    wanted = distribution.replace("-", "_").lower()
    for info in packages.glob("*.dist-info"):
        name, _, version = info.name[: -len(".dist-info")].rpartition("-")
        if name.replace("-", "_").lower() == wanted:
            return version
    return None


def _run_pip(command: list[str], on_output) -> tuple[int, str]:
    """Run pip, stream its lines, return (returncode, transcript)."""
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    # A fixed argument list and no shell. The only value that varies is a path
    # this module computed, so there is nothing here for a
    # board file or a config to reach.
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )
    lines: list[str] = []
    if process.stdout is None:  # pragma: no cover - stdout was asked for
        raise RuntimeProblem(
            "The panel builder could not read what the installer was doing, "
            "so it stopped rather than guess. Try again."
        )
    for raw in process.stdout:
        line = raw.rstrip("\r\n")
        lines.append(line)
        if on_output is not None:
            on_output(line)
    process.stdout.close()
    return process.wait(timeout=FILL_DEADLINE_SECONDS), "\n".join(lines)


def fill(
    install: KiCadInstall,
    report=None,
    on_output=None,
    source: str | None = None,
) -> Runtime:
    """Build the runtime from scratch. Raises RuntimeProblem when it cannot.

    A fill that fails deletes its slot. A tree with some of
    the packages in it and no stamp would be read as "not ready" and refilled
    anyway, but leaving it there costs disk and invites somebody to put it on
    `sys.path` by hand.
    """
    slot = runtime_slot(install)
    _discard(slot)
    site_packages_of(slot).mkdir(parents=True, exist_ok=True)
    _say(
        report,
        "Setting up the panel builder. This happens once, takes about a "
        "minute, and needs an internet connection."
        if wheel_folder() is None or source is not None
        else "Setting up the panel builder from the wheel folder "
        f"{wheel_folder()}. This happens once.",
    )
    started = time.monotonic()
    command = _pip_command(install, slot, source)
    try:
        code, transcript = _run_pip(command, on_output)
        if (
            code != 0
            and source is None
            and _server_has_no_such_version(transcript)
            and local_build_allowed()
        ):
            # THE LOCAL FILL, DEVELOPER OPT-IN (see `LOCAL_BUILD_VARIABLE`).
            # It is tried second, so a published package always wins, and the
            # run says which source it used.
            local = local_source()
            if local is not None:
                source, line = local
                _say(
                    report,
                    f"The published release does not have "
                    f"{ENGINE_DISTRIBUTION} {ENGINE_VERSION}. {line}",
                )
                command = _pip_command(install, slot, source)
                code, transcript = _run_pip(command, on_output)
    except subprocess.TimeoutExpired:
        code, transcript = 1, "the download did not finish in time (read timed out)"
    except OSError as error:
        shutil.rmtree(slot, ignore_errors=True)
        raise RuntimeProblem(
            "The panel builder could not start KiCad's own Python to set "
            f"itself up. It tried to run:\n\n    {install.interpreter}\n\n"
            "That file is where Windows says KiCad is installed. If KiCad was "
            f"moved or partly uninstalled, reinstall it.\n\nWindows said: "
            f"{error.strerror or error}"
        ) from None
    log = _keep_transcript(command, transcript)
    if code != 0:
        shutil.rmtree(slot, ignore_errors=True)
        raise _plain_words_failure(transcript, log)
    if source is None:
        # THE PIN, CHECKED AFTER THE FACT. The public route names the engine
        # by its release archive, not by a version, so the version that
        # archive holds is compared here with the one this plugin release
        # names. A mismatch is a wrong release tag, and it is refused rather
        # than stamped as the version this plugin expects. An absent record
        # is left to the engine check in `damage`, which looks at the tree.
        got = _version_in(site_packages_of(slot), ENGINE_DISTRIBUTION)
        if got is not None and got != ENGINE_VERSION:
            shutil.rmtree(slot, ignore_errors=True)
            raise RuntimeProblem(
                "The panel builder downloaded its engine, but the engine is "
                f"version {got} and this plugin expects version "
                f"{ENGINE_VERSION}. Nothing was kept.\n\nThe source it used:"
                f"\n\n    {fill_sources()[-1]}",
                detail_path=log,
            )
    stamp = _write_stamp(slot, install, source)
    seconds = time.monotonic() - started
    _say(
        report,
        f"The panel builder is set up: {stamp['files']} files, "
        f"{stamp['bytes'] / 1_000_000:.1f} MB, {seconds:.0f} seconds. "
        "It will start straight away from now on.",
    )
    return Runtime(
        interpreter=install.interpreter,
        site_packages=site_packages_of(slot),
        version=ENGINE_VERSION,
        state="filled",
        slot=slot,
    )


def _keep_transcript(command: list[str], transcript: str) -> Path | None:
    """Write pip's output to a file so a message can name it, not contain it."""
    log = fill_log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "\n".join([" ".join(command), "", transcript, ""]), encoding="utf-8"
        )
    except OSError:
        return None
    return log


def _say(report, message: str) -> None:
    if report is not None:
        report(message)


# ---------------------------------------------------------------------------
# the one call both doors make
# ---------------------------------------------------------------------------


def ensure(report=None, on_output=None, source: str | None = None) -> Runtime:
    """Guarantee the engine can be run, and say where from.

    Three answers, cheapest first:

    * "ambient" - the calling interpreter already has the engine installed.
      That is the development venv and the CI container. An interpreter
      someone deliberately installed the engine into is used as it stands, at
      whatever version.
    * "warm" - the per-user runtime is present, finished and current. Costs a
      registry read and two file checks. It reaches no network, starts no
      process, and is what every run after the first one does.
    * "filled" - there was no current runtime, so one was built.

    Raises `RuntimeProblem`, whose text is the whole user-facing answer.
    """
    ambient = installed_engine_version()
    if ambient is not None:
        if ambient != ENGINE_VERSION:
            _say(
                report,
                f"Using the panel builder already installed here (version "
                f"{ambient}); this plugin was built against {ENGINE_VERSION}.",
            )
        return Runtime(
            interpreter=Path(sys.executable),
            site_packages=None,
            version=ambient,
            state="ambient",
        )
    install = kicad_install()
    slot = None if source is not None else ready_slot(install)
    if slot is not None:
        # A warm start is silent EXCEPT when the runtime was built from a local
        # copy, so an unpublished build is not mistaken for the released one. One line, every time it is used.
        built_from = (read_stamp(slot) or {}).get("source")
        if built_from:
            _say(report, f"Running a local build of the panel builder engine: {built_from}")
        return Runtime(
            interpreter=install.interpreter,
            site_packages=site_packages_of(slot),
            version=ENGINE_VERSION,
            state="warm",
            slot=slot,
        )
    if source is None:
        # REPAIR. A runtime that is present and broken is thrown away and
        # rebuilt, and the reason is said in one line so the extra minute is
        # not a mystery.
        hurt = damage(runtime_slot(install), install)
        if hurt is not None and runtime_slot(install).exists():
            _say(
                report,
                f"The panel builder's setup on this computer is damaged - "
                f"{hurt}. Rebuilding it now.",
            )
    return fill(install, report=report, on_output=on_output, source=source)


def activate(report=None, on_output=None) -> Runtime:
    """`ensure`, then put the runtime on this interpreter's import path.

    Only the KiCad door needs this - it imports nothing of the engine itself,
    but the code it launches in-process does. The standalone launcher spawns a
    subprocess instead and uses `launch_environment`.
    """
    runtime = ensure(report=report, on_output=on_output)
    if runtime.site_packages is None:
        return runtime
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    slot_tag = (read_stamp(runtime.slot) or {}).get("python_tag")
    if slot_tag and slot_tag != tag:
        # Refuse rather than import. These are compiled
        # wheels; a cp311 tree on a cp312 interpreter fails deep inside
        # pydantic with a message nobody can act on.
        raise RuntimeProblem(
            "The panel builder was set up for the Python that comes with "
            f"KiCad ({slot_tag.replace('cp', 'version ')}), and this is a "
            f"different one ({tag.replace('cp', 'version ')}). Run the panel "
            "builder from KiCad, or from KiCad's own Python:\n\n"
            f"    {runtime.interpreter}"
        )
    entry = str(runtime.site_packages)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return runtime


def launch_environment(runtime: Runtime, base: dict | None = None) -> dict:
    """Environment for a subprocess that must be able to import the engine.

    PYTHONPATH rather than a command-line flag, because the
    launcher runs `python -m eedesignpanels ...` and the module search has to be
    in place before the interpreter looks for it.
    """
    env = dict(os.environ if base is None else base)
    if runtime.site_packages is None:
        return env
    entry = str(runtime.site_packages)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = entry + (os.pathsep + existing if existing else "")
    return env


def remove() -> list[Path]:
    """Delete every runtime this module built. Returns what went."""
    home = runtime_home() / "runtime"
    if not home.is_dir():
        return []
    gone = []
    for slot in sorted(home.iterdir()):
        if slot.is_dir():
            shutil.rmtree(slot, ignore_errors=True)
            gone.append(slot)
    return gone


# ---------------------------------------------------------------------------
# runnable
# ---------------------------------------------------------------------------


def describe() -> str:
    """What the runtime is, in lines a person reads. Reaches no network."""
    lines = [f"engine wanted    {ENGINE_DISTRIBUTION} {ENGINE_VERSION}"]
    # This line reports whether the engine is installed into the Python
    # running this command, which is a different question from whether the
    # runtime is built. The runtime's own answer is below, from the same
    # function the doors use.
    ambient = installed_engine_version()
    lines.append(f"in this python   {ambient or 'no'}")
    lines.append(f"runtime folder   {runtime_home()}")
    # SAY WHEN THE FOLDER IS REDIRECTED. A redirected run is a check or a
    # test, and a readout that hides the redirection could be read as proof
    # of a real install.
    if runtime_home_override() is not None:
        lines.append(f"{'':16} redirected by {RUNTIME_HOME_VARIABLE}")
    for item in fill_sources():
        lines.append(f"{'fills from':16} {item}")
    if wheel_folder() is not None:
        lines.append(f"{'':16} offline, set by {WHEEL_FOLDER_VARIABLE}")
    # The readout says whether the local build is permitted, so a machine
    # where it is on does not pass for one that uses the published sources.
    allowed = local_build_allowed()
    lines.append(
        f"{'local build':16} "
        + (
            f"allowed ({LOCAL_BUILD_VARIABLE} is set)"
            if allowed
            else f"not allowed (set {LOCAL_BUILD_VARIABLE}=1 for development)"
        )
    )
    try:
        installs = kicad_installs()
    except RuntimeProblem as problem:
        lines.append(f"kicad            {problem.message.splitlines()[0]}")
        return "\n".join(lines)
    if not installs:
        lines.append("kicad            not found")
        return "\n".join(lines)
    for index, install in enumerate(installs):
        label = "kicad" if index == 0 else "kicad (also)"
        lines.append(f"{label:16} {install.version}  {install.interpreter}")
        # The SAME two calls `ensure` makes, so a status readout can never
        # disagree with what a door does one second later.
        sound = ready_slot(install)
        slot = sound or runtime_slot(install)
        if sound is not None:
            lines.append(f"{'runtime':16} ready  {slot}")
        else:
            hurt = damage(slot, install) if slot.exists() else "it is not built"
            lines.append(f"{'runtime':16} will be rebuilt: {hurt}")
            lines.append(f"{'':16} {slot}")
        stamp = read_stamp(slot)
        if stamp:
            lines.append(
                f"{'stamp':16} {stamp.get('version')}  "
                f"{stamp.get('files')} files  "
                f"{(stamp.get('bytes') or 0) / 1_000_000:.1f} MB  "
                f"filled {stamp.get('filled_utc')}"
            )
            source = stamp.get("source")
            if source:
                lines.append(f"{'filled from':16} local build {source}")
            else:
                # EVERY SOURCE: a readout that names one source cannot show
                # that another one is wrong.
                recorded = stamp.get("sources") or ["a source not recorded"]
                lines.append(f"{'filled from':16} {recorded[0]}")
                for item in recorded[1:]:
                    lines.append(f"{'':16} {item}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Show, build or delete the runtime. No traceback ever reaches stdout."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="show what exists")
    parser.add_argument("--fill", action="store_true", help="build it now")
    parser.add_argument("--remove", action="store_true", help="delete it")
    parser.add_argument(
        "--from",
        dest="source",
        metavar="PATH",
        help="development only: fill from a local wheel or project folder",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="milestones only, no pip output"
    )
    args = parser.parse_args(argv)
    if args.remove:
        gone = remove()
        for path in gone:
            print(f"removed {path}")
        if not gone:
            print(f"nothing to remove under {runtime_home()}")
        return 0
    if args.status or not (args.fill or args.source):
        print(describe())
        return 0
    output = None if args.quiet else lambda line: print(f"  {line}")
    try:
        runtime = ensure(report=print, on_output=output, source=args.source)
    except RuntimeProblem as problem:
        print(problem.message, file=sys.stderr)
        return 1
    print(f"state            {runtime.state}")
    print(f"interpreter      {runtime.interpreter}")
    print(f"import path      {runtime.site_packages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
