"""Start the panel builder engine on an interpreter that ignores PYTHONPATH.

WHY THIS FILE EXISTS. Measured on KiCad 10.0.4: the bundled
`python.exe` does not honour `PYTHONPATH`. A folder named in that variable
never reaches `sys.path`, while the same variable on a stock Python 3.14 puts
it there at once. KiCad ships its own `_kipython` and builds its path itself,
so the ordinary way of handing a package folder to a child process silently
does nothing and the child dies with `No module named 'eedesignpanels'`.

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe"   PYTHONPATH ignored
    <any ordinary python>                              PYTHONPATH honoured

So the folder is passed as an ARGUMENT and put on the path from inside the
child, where nothing can ignore it. It is a real file rather than a
`python -c` string so that it can be read, linted and tested.

WHAT IT IS NOT. It is not a second entry point and it adds no behaviour. Every
argument after the runtime folder is handed to `eedesignpanels.preview` exactly
as `python -m eedesignpanels.preview` would hand it over, and the engine's own
exit code comes straight back out, so the shortcut runs the same program the
tests cover.

    <python> run_engine.py <runtime-folder|-> <config.json> [--open]
"""

from __future__ import annotations

import runpy
import sys

# The engine's entry, named once: the same module string the user types by
# hand, so the shortcut and the typed command are one program.
PREVIEW_MODULE = "eedesignpanels.preview"

# What the launcher passes when the engine is already importable - the
# development venv and the CI container both reach this. A marker rather than
# an empty argument, because an empty string on a command line is
# indistinguishable from a missing one.
NO_RUNTIME = "-"


def main(argv: list = None) -> int:
    """Put the runtime on the path, then run the engine as if by `-m`."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        print(
            "run_engine.py needs the runtime folder and then the engine's own "
            f"arguments, or {NO_RUNTIME!r} when there is no runtime folder.",
            file=sys.stderr,
        )
        return 2
    runtime_folder, rest = argv[0], argv[1:]
    if runtime_folder != NO_RUNTIME and runtime_folder not in sys.path:
        # FIRST, not appended: this runtime is the one the bootstrap built
        # and stamped for this interpreter, and it has to win over anything an
        # engineer happens to have installed elsewhere.
        sys.path.insert(0, runtime_folder)
    # The engine reads its own arguments, so it has to see the list it would
    # have seen under `-m`. `alter_sys` then replaces the first entry with the
    # module's file, exactly as `-m` does.
    sys.argv = [PREVIEW_MODULE] + rest
    try:
        runpy.run_module(PREVIEW_MODULE, run_name="__main__", alter_sys=True)
    except ImportError as missing:
        # A runtime that is present but incomplete raises here, deep inside a
        # package nobody has heard of, and a traceback tells an engineer
        # nothing they can act on, so a plain message is printed instead.
        #
        # The runtime's record is removed. The bootstrap reads that record to
        # decide a runtime is sound, so the next start throws the broken copy
        # away and builds a fresh one with nothing to do by hand.
        condemn_runtime(runtime_folder)
        print(incomplete_runtime(runtime_folder, missing), file=sys.stderr)
        return 1
    return 0


# The bootstrap's record of a finished fill, named here rather than imported.
# This file does not import the bootstrap: it runs INSIDE the engine's process
# after the path is set, and a second module loaded here would be a second copy
# of it. `tests/test_runtime_repair.py` fences the two names
# against each other so they cannot drift apart.
STAMP_NAME = "runtime.json"


def condemn_runtime(runtime_folder: str) -> bool:
    """Mark the runtime as damaged, so the next start rebuilds it. Never raises.

    The record sits one level above the packages folder, and it is one small
    file no session ever holds open - which is why it, and not the tree, is
    what gets removed here.
    """
    if runtime_folder == NO_RUNTIME:
        return False
    import pathlib

    stamp = pathlib.Path(runtime_folder).parent / STAMP_NAME
    try:
        stamp.unlink()
    except OSError:
        return False
    return True


def incomplete_runtime(runtime_folder: str, missing) -> str:
    """One self-contained message for a runtime that will not import.

    It stands alone: it says what happened, names the copy that will be
    replaced, and asks for NOTHING. The folder is named because a support
    question needs it. The message does not ask anybody to delete it, because
    the next start replaces it.
    """
    where = (
        ""
        if runtime_folder == NO_RUNTIME
        else f"\n\nThe copy it will replace:\n\n    {runtime_folder}"
    )
    return (
        "The panel builder is only partly set up on this computer, so it "
        f"could not start. What it could not load: {missing.name or missing}."
        "\n\nIt repairs itself: start the panel builder again and it throws "
        "the broken copy away and builds a fresh one, which takes about a "
        "minute and needs an internet connection or the offline wheel folder. "
        "There is nothing to do by hand."
        f"{where}\n\n"
        "If it fails a second time, keep this message. A problem report "
        "needs it."
    )


if __name__ == "__main__":
    sys.exit(main())
