"""Where KiCad's command line tool is on this machine. Asked, never assumed.

The engine calls `kicad-cli` to draw a board and to fill copper. It used to
call one fixed Windows path, so a KiCad on another drive, in another version
folder, on Linux or on macOS was not found. This module searches in the order
the plugin's own launcher uses (`plugins/runner.py`), and then further:

1. Beside the running Python. Inside KiCad that is KiCad's own `bin` folder, so
   the tool found is the one that belongs to the running KiCad.
2. The usual install folders. On Windows that is every version folder under
   `KiCad` in each Program Files folder, newest first. On macOS it is the
   application bundle. On Linux it is `/usr/bin` and `/usr/local/bin`.
3. `PATH`.

No KiCad version and no install folder is written here: the version folders
are read off the disk, and the Program Files folders come from the
environment.

The KiCad settings folder comes from eedesigncore's
`default_user_settings_dir`, which follows KiCad's own rule, so the engine does
not keep a second copy of it.
"""

import os
import re
import shutil
import sys
from pathlib import Path

# The Windows environment variables that name a Program Files folder.
PROGRAM_FILES_VARIABLES = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
# Where a macOS install keeps the tool, inside the application bundle.
MAC_BUNDLE_TOOL = Path("KiCad") / "KiCad.app" / "Contents" / "MacOS"
# Where a Linux package puts it.
LINUX_FOLDERS = (Path("/usr/bin"), Path("/usr/local/bin"))


def tool_name(platform: str = None) -> str:
    """The file name of the tool on this operating system."""
    platform = sys.platform if platform is None else platform
    return "kicad-cli.exe" if platform.startswith("win") else "kicad-cli"


def _version_key(folder: Path) -> tuple:
    """A version folder name as numbers, so 10.0 sorts after 9.0."""
    return tuple(int(part) for part in re.findall(r"\d+", folder.name))


def candidates(executable: str = None, environ=None, platform: str = None,
               home: Path = None) -> list:
    """Every place the tool may be, in the order they are tried."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    name = tool_name(platform)
    executable = sys.executable if executable is None else executable
    found = []
    if executable:
        found.append(Path(executable).resolve().parent / name)
    if platform.startswith("win"):
        roots = [environ.get(key) for key in PROGRAM_FILES_VARIABLES]
        for root in dict.fromkeys(root for root in roots if root):
            base = Path(root) / "KiCad"
            if not base.is_dir():
                continue
            versions = sorted((child for child in base.iterdir()
                               if child.is_dir()),
                              key=_version_key, reverse=True)
            found.extend(version / "bin" / name for version in versions)
    elif platform == "darwin":
        home = Path.home() if home is None else Path(home)
        found.append(Path("/Applications") / MAC_BUNDLE_TOOL / name)
        found.append(home / "Applications" / MAC_BUNDLE_TOOL / name)
    else:
        found.extend(folder / name for folder in LINUX_FOLDERS)
    on_path = shutil.which(name, path=environ.get("PATH"))
    if on_path:
        found.append(Path(on_path))
    return list(dict.fromkeys(found))


def find_kicad_cli(executable: str = None, environ=None, platform: str = None,
                   home: Path = None):
    """The first `kicad-cli` that exists, by absolute path, or None."""
    for candidate in candidates(executable, environ, platform, home):
        if candidate.is_file():
            return candidate
    return None


def default_kicad_cli() -> Path:
    """The tool to call when a caller names none.

    When no tool is found this is the first place searched, beside the running
    Python. It does not exist, so every caller's own "is it there" check says
    no, and a message that names it says where the search started.
    """
    found = find_kicad_cli()
    if found is not None:
        return found
    searched = candidates()
    return searched[0] if searched else Path(tool_name())


# What "not found" means in a message: the whole search, in plain words.
SEARCH_TEXT = ("beside this Python, in the KiCad install folders, and on "
               "PATH")
