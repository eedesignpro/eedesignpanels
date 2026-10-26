"""Which footprint library a name lives in, answered from KiCad's own settings.

**LIBRARIES RESOLVE THROUGH KICAD'S OWN SETTINGS.** A footprint the panel
places is a footprint an engineer
picked in KiCad, so the list of candidates comes from the same place KiCad reads
it - the global `fp-lib-table` under the user settings directory, the nested
tables it includes, and the path variables `kicad_common.json` defines. Nothing
here carries a library path that somebody typed into this project.

**The resolution itself is `eedesigncore`'s.** The board library owns KiCad
file reading, so it is not reimplemented here.
`eedesigncore.tables.resolve_lib_table`
discovers the tables, follows `type "Table"` rows, expands `${VAR}` against the
same sources KiCad expands against, and merges the nickname namespace in
KiCad's own order. This module is the panel builder's policy ON TOP of that, and
it is three decisions:

1. **WHICH ROOTS MAY BE READ.** The library confines every file it opens to the
   KiCad install and the user settings directory, and a
   user's own footprints are often on neither: a global table can include
   `${MY_LIBRARIES}/fp-lib-table`, which expands to a folder elsewhere. A path
   the user configured in KiCad's own Configure Paths dialog is a path KiCad
   reads, so :func:`settings_roots` widens the confinement to exactly the
   directories `kicad_common.json` names - and to nothing else. A table row
   pointing somewhere no KiCad setting mentions is still refused.
2. **WHAT A FAILURE SAYS.** Resolution runs `strict=True`, so a table that
   cannot be read raises instead of quietly resolving to a shorter list, and
   :class:`Resolution` carries the reason as a sentence naming the table file or
   the variable that did not expand. A dropdown with nothing in it has to say
   why.
3. **THE ESCAPE HATCH.** A config may pin an explicit `.pretty` directory
   (`library_path`). That is for a machine with no KiCad settings at all - a CI
   runner - and it is read INSTEAD of the tables, never as a fallback: a
   silent fallback would let two machines resolve the same config differently.

**WITH NO KICAD SETTINGS PRESENT** - the CI container, which has no KiCad -
`resolve_lib_table` finds no `fp-lib-table` and the namespace is empty, so a
config naming a library by nickname gets :class:`Resolution` with a reason
naming the settings directory it looked in, and the fiducials stage refuses and
writes nothing. A config carrying `library_path` works with no KiCad and no
settings, which is how a headless build places fiducials.

What the KiCad 10.0.3 source says, cited because the behaviour is not guessable:

* the settings directory is `KICAD_CONFIG_HOME` or
  `wxStandardPaths::GetUserConfigDir()` + `kicad`, plus the major.minor
  subdirectory - `%APPDATA%\\kicad\\10.0` on Windows
  (`common/paths.cpp:645-668`, `libs/kiplatform/os/windows/environment.cpp:109`,
  `CMakeLists.txt:344`);
* a `type "Table"` row is loaded recursively, its URI expanded and then made
  absolute against the PARENT table's directory, with already-seen tables
  refused (`common/libraries/library_manager.cpp:118-165`);
* a nested table's rows are spliced into the parent namespace in place, first
  nickname sighting winning (`library_manager.cpp:625-705`);
* `${VAR}` expands against project text variables first, then the process
  environment, then versioned `KICAD*_` fallbacks
  (`common/common.cpp:360-470`, `library_manager.cpp:845-851`) - and KiCad puts
  `kicad_common.json`'s `environment.vars` INTO the process environment at
  startup (`common/pgm_base.cpp:777-790`), which is why a custom variable such
  as `MY_LIBRARIES` resolves at all.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Union

from eedesigncore import Footprint, LibTableResolutionError
from eedesigncore.tables import default_user_settings_dir, resolve_lib_table

# The footprint library extension KiCad writes. A footprint library is a
# DIRECTORY of `.kicad_mod` files, which is why an explicit `library_path` is a
# folder and not a file.
LIBRARY_SUFFIX = ".pretty"
FOOTPRINT_SUFFIX = ".kicad_mod"

# The library the fiducial footprints come from by default: `Fiducial`, the
# footprint library KiCad installs with itself. It is a DEFAULT and not a
# constant: a config may name any nickname the tables carry, and
# `library_path` bypasses the tables entirely.
DEFAULT_FIDUCIAL_LIBRARY = "Fiducial"

# WHAT MAKES A FOOTPRINT A FIDUCIAL: every footprint in the library whose NAME
# contains "fiducial", case-insensitively, wherever in the name it falls. Not a
# curated list, not a prefix: the library is the authority on what it holds,
# and a generated variant such as `Fiducial_0.4mm_Mask0.6mm_Variant_A` is as
# selectable as any other.
FIDUCIAL_MARKER = "fiducial"

# A footprint name is a FILE NAME, and this config is hand-editable, so its
# content is untrusted input. Anything outside this shape is refused by name
# rather than opened:
# no separators, no drive letters, no `..`.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")

# An unexpanded variable is left verbatim by the library, so a `${` surviving
# expansion is the one reliable signal that a path variable is missing.
UNEXPANDED = re.compile(r"\$\{([A-Za-z0-9_:]+)\}")

# How many footprints one library may offer the dropdown. A directory with more
# entries than this is not a footprint library anybody is scrolling through, and
# the cap bounds what a page paint reads off disk.
MAX_LIBRARY_ENTRIES = 4096


class Resolution(NamedTuple):
    """One library, found or not found, and in the second case why not.

    `reason` is empty exactly when `directory` is set. A caller shows the
    reason; it never has to compose one, because the sentence naming the table
    file or the variable is only available here.
    """

    library: str
    directory: Union[Path, None] = None
    footprints: tuple = ()
    # Which table file the winning row came from, and which tier. Shown on the
    # page so the user can see WHERE the list came from.
    source_table: Union[Path, None] = None
    scope: str = ""
    raw_uri: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.directory is not None

    @property
    def fiducials(self) -> tuple:
        """Every footprint whose name says it is a fiducial, in library order.

        One rule, applied here so the page and the stage cannot
        disagree about what the dropdown offered: case-insensitive
        ``"fiducial"`` anywhere in the name.
        """
        return tuple(
            name for name in self.footprints
            if FIDUCIAL_MARKER in name.lower()
        )


class LibraryError(ValueError):
    """The named library, or the named footprint in it, could not be read.

    A `ValueError` on purpose. The two things that raise it are a library that
    is not a library and a name that is not a name, and both are BAD VALUES -
    which means a pydantic validator may raise it directly and the config loader
    turns it into the same clear message every other schema failure gets. A
    separate exception hierarchy would have to be translated at that boundary,
    and a translation is a place for the reason to be lost.
    """


def settings_dir() -> Path:
    """KiCad's user settings directory, by KiCad's own rule.

    Delegated to `eedesigncore`, which implements `common/paths.cpp:645-668`
    including the `KICAD_CONFIG_HOME` override. Named here because a failure has
    to be able to say which directory it looked in.
    """
    return default_user_settings_dir()


def settings_roots(directory: Union[Path, None] = None) -> tuple:
    """Every directory KiCad's own settings name, for the confinement to allow.

    Reads `kicad_common.json`'s `environment.vars` - the object behind
    Preferences -> Configure Paths - and returns the values that are directories
    on this machine. That is the whole widening: a table row that expands
    somewhere no KiCad setting mentions is still outside the roots and is still
    refused, which keeps a table file from naming an arbitrary path to read.

    An absent or unreadable `kicad_common.json` is not an error here. It means
    this machine configures no extra paths, which is the CI case, and the
    resolution that follows reports its own empty namespace.
    """
    base = Path(directory) if directory is not None else settings_dir()
    common = base / "kicad_common.json"
    try:
        raw = common.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    variables = data.get("environment", {}).get("vars", {})
    if not isinstance(variables, dict):
        return ()
    roots = []
    for value in variables.values():
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return tuple(roots)


def _entries(directory: Path) -> tuple:
    """Every footprint name in a `.pretty` directory, sorted.

    Sorted and not directory order: a dropdown's option list is part of the
    page's bytes, and a file system that enumerates differently on two machines
    would make two different pages out of one library.
    """
    names = sorted(
        path.stem for path in directory.glob(f"*{FOOTPRINT_SUFFIX}")
        if path.is_file()
    )
    if len(names) > MAX_LIBRARY_ENTRIES:
        raise LibraryError(
            f"{directory} holds {len(names)} footprints, more than the "
            f"{MAX_LIBRARY_ENTRIES} this reader takes - that is not a "
            f"footprint library, it is a folder of files"
        )
    return tuple(names)


def _pinned(library: str, library_path: Union[Path, str]) -> Resolution:
    """The explicit-path escape hatch: read this directory, ask no table."""
    directory = Path(library_path)
    if not directory.is_dir():
        return Resolution(
            library=library, raw_uri=str(directory),
            reason=(
                f"the config pins the footprint library to {directory}, and "
                f"that is not a directory on this machine - a footprint "
                f"library is a folder named <name>{LIBRARY_SUFFIX}"
            ),
        )
    return Resolution(
        library=library, directory=directory, footprints=_entries(directory),
        scope="pinned", raw_uri=str(directory),
    )


def resolve(library: str = DEFAULT_FIDUCIAL_LIBRARY,
            library_path: Union[Path, str, None] = None,
            *, settings: Union[Path, str, None] = None,
            project_dir: Union[Path, str, None] = None) -> Resolution:
    """Where *library* is, through KiCad's settings - or why it could not be found.

    *library_path* pins the directory and skips the tables entirely, which is
    the headless escape hatch. Otherwise the global and project `fp-lib-table`
    are discovered, nested tables followed, `${VAR}` expanded, and the nickname
    looked up in the merged namespace.

    Never raises for a resolution failure: a missing table, a missing variable
    and a nickname nobody declared are all things the page has to be able to
    SHOW, so each comes back as a :class:`Resolution` carrying the sentence.
    :class:`LibraryError` is reserved for a directory that is not a library.
    """
    if library_path is not None:
        return _pinned(library, library_path)

    base = Path(settings) if settings is not None else settings_dir()
    try:
        table = resolve_lib_table(
            "fp",
            settings_dir=base,
            project_dir=project_dir,
            strict=True,
            allowed_roots=list(settings_roots(base)),
        )
    except LibTableResolutionError as exc:
        return Resolution(
            library=library,
            reason=(
                f"KiCad's footprint library tables under {base} could not be "
                f"read: {exc}. Either the table is not where KiCad keeps it, "
                f"or it includes one this run may not open - add the path to "
                f"Preferences, Configure Paths, or pin the library directory "
                f"in the config"
            ),
        )
    except OSError as exc:
        return Resolution(
            library=library,
            reason=(
                f"KiCad's footprint library table under {base} could not be "
                f"read: {exc}"
            ),
        )

    row = next(
        (entry for entry in table.libraries if entry.nickname == library), None
    )
    if row is None:
        return Resolution(
            library=library,
            reason=(
                f"no footprint library is nicknamed {library!r} in KiCad's "
                f"tables under {base} - {len(table.nicknames)} librar"
                f"{'y is' if len(table.nicknames) == 1 else 'ies are'} "
                f"declared there. Add it in Preferences, Manage Footprint "
                f"Libraries, or pin the library directory in the config"
            ),
        )

    missing = UNEXPANDED.search(str(row.uri))
    if missing is not None:
        return Resolution(
            library=library, source_table=Path(str(row.source_table)),
            scope=str(row.scope), raw_uri=row.raw_uri,
            reason=(
                f"the path variable ${{{missing.group(1)}}} in "
                f"{library}'s library path {row.raw_uri!r} is not defined, so "
                f"the library's location is unknown. {row.source_table} "
                f"declares the row; set the variable in Preferences, Configure "
                f"Paths"
            ),
        )

    directory = Path(str(row.uri))
    if not directory.is_dir():
        return Resolution(
            library=library, source_table=Path(str(row.source_table)),
            scope=str(row.scope), raw_uri=row.raw_uri,
            reason=(
                f"{library} resolves to {directory}, which is not a directory "
                f"on this machine - {row.source_table} declares it as "
                f"{row.raw_uri!r}"
            ),
        )
    return Resolution(
        library=library, directory=directory, footprints=_entries(directory),
        source_table=Path(str(row.source_table)), scope=str(row.scope),
        raw_uri=row.raw_uri,
    )


@lru_cache(maxsize=16)
def _cached(library: str, library_path, settings, project_dir) -> Resolution:
    return resolve(library, library_path, settings=settings,
                   project_dir=project_dir)


def resolve_cached(library: str = DEFAULT_FIDUCIAL_LIBRARY,
                   library_path=None, *, settings=None,
                   project_dir=None) -> Resolution:
    """:func:`resolve`, remembered.

    The page reads the option list on every paint and the stage reads the same
    answer on every build, and both would otherwise walk the tables and glob a
    directory each time. Cleared by :func:`forget`, which the tests call - a
    process that outlives a library change is the preview server, and it is
    restarted when a library changes.
    """
    return _cached(
        library,
        None if library_path is None else str(library_path),
        None if settings is None else str(settings),
        None if project_dir is None else str(project_dir),
    )


def forget() -> None:
    """Drop the remembered resolutions. For tests, and for a settings change."""
    _cached.cache_clear()


def checked_name(name: str) -> str:
    """*name* if it is a footprint name, else :class:`LibraryError` saying why.

    A footprint name reaches the file system, and this config is hand-edited,
    so its content is untrusted input. So the name is checked for SHAPE here,
    before a path is
    built from it, rather than trusted and then normalised afterwards.
    """
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise LibraryError(
            f"{name!r} is not a footprint name: a footprint name is a file "
            f"name - letters, digits, dot, dash, underscore and plus - and "
            f"carries no path of its own"
        )
    return name


def load(resolution: Resolution, name: str) -> Footprint:
    """The named footprint of *resolution*'s library, as a `Footprint`.

    Raises:
        LibraryError: when the library did not resolve, when the name is not a
            footprint name, or when the library holds no such footprint. Each
            message names what is missing and where it was looked for, because
            a stage that cannot place a footprint has to say which one.
    """
    if not resolution.ok:
        raise LibraryError(resolution.reason)
    checked_name(name)
    path = resolution.directory / f"{name}{FOOTPRINT_SUFFIX}"
    if not path.is_file():
        raise LibraryError(
            f"{resolution.library} holds no footprint named {name!r} - "
            f"{path} does not exist. The library has "
            f"{len(resolution.footprints)} footprint(s)"
        )
    return Footprint.from_file(path)


__all__ = [
    "DEFAULT_FIDUCIAL_LIBRARY",
    "FIDUCIAL_MARKER",
    "FOOTPRINT_SUFFIX",
    "LIBRARY_SUFFIX",
    "MAX_LIBRARY_ENTRIES",
    "LibraryError",
    "Resolution",
    "checked_name",
    "forget",
    "load",
    "resolve",
    "resolve_cached",
    "settings_dir",
    "settings_roots",
]
