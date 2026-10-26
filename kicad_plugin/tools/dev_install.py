"""Install this plugin into KiCad for development. Close KiCad, run, reopen.

The PCM route is far too slow to iterate against. This copies plugins/ straight
into KiCad's user scripting folder, which KiCad scans at startup:

    <Documents>\\KiCad\\10.0\\scripting\\plugins\\com_github_eedesignpro_eedesignpanels

The folder name has dots rewritten to underscores because KiCad imports the
folder BY NAME - a dotted name is an import of a package "com" and fails.

Usage:
    python tools/dev_install.py            install or update, then restart KiCad
    python tools/dev_install.py --remove   uninstall
    python tools/dev_install.py --list     show what is installed where
    python tools/dev_install.py --watch    reinstall on every source change

Notes:
  - Tools -> External Plugins -> Refresh Plugins re-runs discovery, but Python
    keeps already-imported modules cached, so edits to an imported module are
    not reliably picked up. Restarting KiCad is the honest loop.
  - A PCM install of the same identifier lives in
    %APPDATA%\\kicad\\10.0\\3rdparty\\plugins and registers separately, so you
    would get two toolbar buttons. --list warns when both are present.
  - __pycache__ is never copied; a stale .pyc is a confusing way to lose an hour.
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
# The package identifier, read from metadata.json. The folder is named
# kicad_plugin, so its name is not the identifier.
IDENTIFIER = json.loads(
    (PROJECT / "metadata.json").read_text(encoding="utf-8")
)["identifier"]
SOURCE = PROJECT / "plugins"
KICAD_VERSION = "10.0"


def documents_dir():
    """The Documents folder KiCad itself resolves.

    KiCad calls wxStandardPaths::GetDocumentsDir(), which on Windows asks the
    shell for the Documents known folder. That is NOT %USERPROFILE%\\Documents
    whenever the folder is redirected - OneDrive and similar sync tools do this,
    and guessing the profile path silently installs where KiCad never looks.
    KICAD_DOCUMENTS_HOME overrides it, matching PATHS::getUserDocumentPath.
    """
    override = os.environ.get("KICAD_DOCUMENTS_HOME")
    if override:
        return Path(override)
    if os.name != "nt":
        # Windows is the tested platform. Elsewhere the Documents folder is
        # not asked of the shell, so the caller names it.
        sys.exit(
            "dev_install.py finds KiCad's folder on Windows only. Elsewhere, "
            "set KICAD_DOCUMENTS_HOME to the folder that holds KiCad/10.0."
        )

    # FOLDERID_Documents, the same known folder the shell hands wxWidgets.
    folder_id = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.wintypes.DWORD),
            ("Data2", ctypes.wintypes.WORD),
            ("Data3", ctypes.wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    raw = folder_id.bytes_le
    guid = GUID(
        int.from_bytes(raw[0:4], "little"),
        int.from_bytes(raw[4:6], "little"),
        int.from_bytes(raw[6:8], "little"),
        (ctypes.c_ubyte * 8)(*raw[8:16]),
    )
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(out)
    ):
        sys.exit("could not resolve the Documents folder from the shell")
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


KICAD_USER = documents_dir() / "KiCad" / KICAD_VERSION
USER_PLUGINS = KICAD_USER / "scripting" / "plugins"
# The folder name IS the Python module name: KiCad appends the plugins folder
# to sys.path and calls importlib.import_module(<folder name>) (verified in
# scripting/kicadplugins.i, KiCad 10.0). The dotted identifier
# "com.github.eedesignpro.eedesignpanels" makes that an import of a
# package "com", which fails - the plugin is listed as not
# loaded and no action ever appears. Rewriting dots to underscores is exactly
# what the PCM does when it unpacks a package, and for the same reason.
MODULE_NAME = IDENTIFIER.replace(".", "_")
TARGET = USER_PLUGINS / MODULE_NAME
LEGACY_TARGET = USER_PLUGINS / IDENTIFIER
PCM_INSTALL = KICAD_USER / "3rdparty" / "plugins" / MODULE_NAME

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "Thumbs.db")


def install():
    if not SOURCE.is_dir():
        sys.exit(f"no source at {SOURCE}")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    if LEGACY_TARGET.exists():
        shutil.rmtree(LEGACY_TARGET)
        print(f"removed the un-importable dotted install at {LEGACY_TARGET}")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, TARGET, ignore=IGNORE)
    files = sorted(p for p in TARGET.rglob("*") if p.is_file())
    print(f"installed {len(files)} file(s) -> {TARGET}")
    for f in files:
        print(f"    {f.relative_to(TARGET)}")
    if PCM_INSTALL.exists():
        print(
            f"\nWARNING: a PCM install also exists at {PCM_INSTALL}\n"
            f"         It has the same module name ({MODULE_NAME}) and KiCad\n"
            "         searches scripting/plugins first, so this dev install\n"
            "         wins and the PCM copy is shadowed. Uninstall it in the\n"
            "         PCM to keep the two from being confused."
        )
    print("\nRestart KiCad to pick this up.")


def remove():
    removed = [path for path in (TARGET, LEGACY_TARGET) if path.exists()]
    for path in removed:
        shutil.rmtree(path)
        print(f"removed {path}")
    if removed:
        print("Restart KiCad.")
    else:
        print(f"nothing installed at {TARGET}")


def show():
    for label, path in (("dev install", TARGET),
                        ("dotted (dead)", LEGACY_TARGET),
                        ("PCM install", PCM_INSTALL)):
        state = "present" if path.exists() else "absent"
        print(f"{label:12} {state:8} {path}")
    others = [
        p for p in USER_PLUGINS.iterdir()
        if p.is_dir() and p.name not in (MODULE_NAME, IDENTIFIER)
    ] if USER_PLUGINS.is_dir() else []
    if others:
        print("\nother dev plugins in the same folder:")
        for p in others:
            print(f"    {p.name}")


def watch():
    print(f"watching {SOURCE} - Ctrl+C to stop")
    print("NOTE: KiCad still needs a restart to load a change.")
    last = None
    while True:
        stamp = sorted(
            (p, p.stat().st_mtime) for p in SOURCE.rglob("*.py")
            if "__pycache__" not in p.parts
        )
        if stamp != last:
            if last is not None:
                print(f"\n[{time.strftime('%H:%M:%S')}] change detected")
            install()
            last = stamp
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remove", action="store_true", help="uninstall")
    ap.add_argument("--list", action="store_true", help="show install state")
    ap.add_argument("--watch", action="store_true", help="reinstall on change")
    args = ap.parse_args()
    if args.remove:
        remove()
    elif args.list:
        show()
    elif args.watch:
        watch()
    else:
        install()


if __name__ == "__main__":
    main()
