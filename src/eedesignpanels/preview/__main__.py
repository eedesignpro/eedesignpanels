"""Entry point: ``python -m eedesignpanels.preview <config.json>``.

Starts the live preview on loopback and prints the URL. **Opening the browser is
the caller's job** - a test never does it, and a user pastes the line. A tool
that opens a window by itself cannot be run from a test or from CI without side
effects.

`--open` is that caller asking, and it opens STEP 1 ONLY: the prep page is
where the job starts, and its `Step 2` control opens the panel tab when it is
pressed. Both addresses are printed either way, so either page can be pasted.
The pages adopt stable window names when they load, because a tab opened
through `webbrowser` carries none - which is what lets each page's step link
find the other tab instead of opening a second copy of it.

**Both tabs open READY.** The launch computes the board and then derives the
panel in memory - the ordinary unfilled build, about a second - before a
browser is pointed at anything, so step 2 opens showing the panel instead of an
empty viewport and the name of a button, whenever it is opened. A config whose
panel cannot be built opens stating the reason; it is never a silent empty page.

**ONE OPEN TAB IS ENOUGH.** The session lives while at least one of its pages
is open, and closing the LAST one stops it, so closing a spare tab is safe. A
page that is still open when the session ends is told, and says so in plain
words rather than vanishing. Nothing is written on the way out: every edit is
held in memory, and the browser's own prompt at tab close is the guard on
losing it.

**Lock files are ignored.** The preview starts and saves whether or not KiCad
is holding the board: editing is in memory, nothing touches the file until
"Save to Board", and whether that save is safe under an open editor is the
user's call rather than this tool's.

Every launch opens a SESSION FOLDER - `<project>/panel/logs/`, named
`YYYYMMDD_HHMMSS_ui_session` - holding the journal, the shots the user saves, a
copy of the config as it was at launch, and the session's own render scratch.
Its path is printed here and shown in the page footer, so whatever comes out
of a session is one path to hand over. Two launches in one second do NOT share
it: the second takes a numbered suffix (`journal.claim_session_folder`).

Stdout contract, matching the other entries: the **last** line before the server
blocks is the URL.
"""

import argparse
import sys
import time
import webbrowser

from eedesignpanels import __version__
from eedesignpanels.config import ConfigError, load_or_create
from eedesignpanels.preview import (
    journal as journal_module,
    page_served,
    raster,
    render_seam,
    server as server_module,
)
from eedesignpanels.preview.session import PreviewSession


def page_addresses(url: str) -> list:
    """Both pages, in step order: step 1 first, then step 2.

    BOTH ARE PRINTED even though `--open` opens only the first: an address
    that is only reachable by clicking a link is an address nobody can hand to
    anyone.
    """
    base = url if url.endswith("/") else url + "/"
    return [
        (step, base + step.path.lstrip("/")) for step in page_served.STEPS
    ]


def open_pages(url: str, opener=webbrowser.open) -> list:
    """Open STEP 1 in the browser. Returns the addresses it asked for.

    ONE TAB AT LAUNCH: the panel page is opened by the `Step 2` control on the
    prep page, when the user wants it. Both addresses are still printed by
    :func:`main`.

    *opener* is the seam a test drives; nothing else passes it. A tab opened
    this way carries no window name, so the pages adopt their own on load
    (`page_served`) and the step links ask the server before opening anything.
    """
    addresses = [address for _, address in page_addresses(url)][:1]
    for address in addresses:
        opener(address)
    return addresses


def main(argv: list = None) -> int:
    """Run the preview server until Ctrl-C or a POST to `/shutdown`."""
    parser = argparse.ArgumentParser(
        prog="eedesignpanels.preview",
        description="Serve the live prep preview on 127.0.0.1.",
    )
    parser.add_argument("config", help="path to panel/panel.json")
    parser.add_argument(
        "--port", type=int, default=0,
        help="port to bind; 0 (the default) takes an ephemeral one",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="open the page in a browser once the server is up",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log every request"
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    # A FRESH PROJECT STARTS. No settings file is
    # not an error - one is written. A settings file that will not load is not
    # an error either - the launch goes on with standard settings and says so,
    # and the file on disk is left exactly as it was found. The only refusal
    # left is not knowing which board was meant, and it names the folder.
    try:
        startup = load_or_create(args.config)
    except ConfigError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    config = startup.config
    if config.prep is None:
        print(
            f"config error: {config.config_path} has no 'prep' section, so "
            f"there is no cutter diameter to preview with. A cutter diameter "
            f"is declared, never inferred.",
            file=sys.stderr,
        )
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    recording = journal_module.open_session(
        config.config_path,
        journal_module.session_root(config),
        stamp=stamp,
        board=str(config.source_board_path),
        backend=render_seam.preview_backend(),
        rasterizer=raster.AVAILABLE,
        eedesignpanels_version=__version__,
    )
    # The session folder IS the working folder: one launch, one place, so a
    # shot, the render it came from and the journal that explains it are
    # handed over as one path.
    workdir = recording.folder

    print(f"eedesignpanels.preview {__version__}")
    print(f"  config       {config.config_path}")
    # THE COMMAND LINE STAYS HONEST. One line when the file was written for
    # the user, and one line when it could not be read - because a banner that
    # printed a path and nothing else would let a run on standard settings look
    # like a run on the user's own.
    if startup.banner:
        print(f"  settings     {startup.banner}")
    print(f"  board        {config.source_board_path}")
    # BOTH backends, when both of them draw: an edit paints
    # through the library and the content render replaces those layers behind
    # it, so a line naming one of the two would be half the answer. The same
    # sentence the page's own footer carries.
    print(f"  renderer     {render_seam.renderer_label()}")
    print(f"  session      {recording.folder}")
    print(f"  journal      {recording.journal_path.name}")
    print(f"  shots        {recording.shots_dir}")
    if not raster.AVAILABLE:
        print(f"  no PNG       {raster.UNAVAILABLE_REASON}")

    if startup.problem:
        recording.append("config.unreadable", reason=startup.reason,
                         path=str(config.config_path))
    elif startup.created:
        recording.append("config.created", path=str(config.config_path))

    session = PreviewSession(config.config_path, workdir, journal=recording,
                             startup=startup)
    session.refresh()
    if session.error:
        print(f"  NOT SHOWN    {session.error}")
    # THE PANEL IS DERIVED BEFORE THE BROWSER OPENS, so step 2 opens showing
    # the panel rather than an empty viewport and the name of a button. Said
    # out loud, because it is the second or so between the board being
    # computed and the pages being served.
    print("  panel        deriving the panel in memory, so its tab opens "
          "showing one", flush=True)
    session.start_panel()
    if session.panel_error:
        print(f"  NO PANEL     {session.panel_error}")
    elif session.panel is not None:
        print(f"  panel        {session.panel.subtitle}")

    live = server_module.PreviewServer(session, port=args.port,
                                       verbose=args.verbose)
    url = live.start()
    recording.append("server.listening", url=url, port=live.server_address[1])
    print("  writes       nothing until 'Save to Board' - every edit is held "
          "in memory")
    # Two pages, one server, and BOTH of them are named here: --open opens the
    # first, the page's own Step 2 control opens the second, and either line
    # can be pasted instead.
    for step, address in page_addresses(url):
        print(f"  {step.label.split(' - ')[0].lower():<12} {address} - "
              f"{step.label.split(' - ')[-1]}")
    print(f"  report       press {page_served.REPORT_KEY.upper()} on the page, "
          f"or the header button - one zip for a developer")
    # ONE OPEN TAB IS ENOUGH: closing the last one stops the session, closing a
    # spare one does not.
    print("  quit         Ctrl-C, the 'Quit Panelizer' button, or closing the "
          "last open tab")
    # FLUSHED, and it matters. A launcher reads this line
    # off a PIPE, and Python block-buffers a piped stdout - so without the flush
    # the whole preamble sits in an 8 KB buffer while the server blocks below,
    # and a caller waiting for the URL waits for ever. The KiCad plugin is
    # exactly that caller.
    print(url, flush=True)

    if args.open:
        open_pages(url)

    try:
        while server_module.active_server() is live:
            time.sleep(0.2)
        # Whoever stopped it says why: the stop button, or the last tab to close.
        reason = live.stop_reason
    except KeyboardInterrupt:
        reason = "Ctrl-C"
        print("\nstopping")
        live.stop()
    recording.close(reason)
    print(f"session recorded in {recording.folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
