"""Pay kicad-cli's first-call cost at plugin load, on a worker thread.

Measured: the first `kicad-cli` invocation of a KiCad session costs 5.6 s
against 0.18 s for later ones, so it is paid at load rather than by the first
thing the user clicks. `CREATE_NO_WINDOW` because a
console application launched from a GUI process flashes a console window, and a
worker thread because a synchronous call froze the editor for 3.7 s.

Launching the engine itself is `session_launch`; this module is only the
warm-up. Stdlib only: it is imported inside KiCad's interpreter.
"""

import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

# The documented install location, used when the interpreter's own folder does
# not hold a kicad-cli - inside kicad.exe it does, and deriving beats guessing.
DEFAULT_KICAD_CLI = Path(r"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")


def _creationflags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def kicad_cli_path() -> Path:
    """The kicad-cli to call, by absolute path.

    Never a bare name: KiCad's subprocess environment is the user's PATH, not
    ours. Inside kicad.exe `sys.executable` is the KiCad binary, so its folder
    is the install's bin folder and the answer needs no configuration.
    """
    candidates = []
    name = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    if sys.executable:
        candidates.append(Path(sys.executable).resolve().parent / name)
    # The Windows default only on Windows. Elsewhere a missing kicad-cli
    # leaves the warm-up undone, which is silent by design.
    if os.name == "nt":
        candidates.append(DEFAULT_KICAD_CLI)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return DEFAULT_KICAD_CLI


def warm_kicad_cli() -> None:
    """Pay the first-invocation cost at plugin load. Call on a worker thread.

    Failure is deliberately silent: this is an optimisation, and a plugin that
    refuses to load because a warm-up failed would be worse than a slow first
    click. The real invocation reports its own errors.
    """
    cli = kicad_cli_path()
    if not cli.is_file():
        return
    try:
        subprocess.run(
            [str(cli), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return
