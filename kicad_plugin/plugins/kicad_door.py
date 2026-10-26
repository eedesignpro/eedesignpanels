"""The KiCad door: what happens between the menu entry and the open browser tab.

Six things, in order:

1. offer to save the open board, because the engine reads the FILE and unsaved
   work in the editor would otherwise be invisible to it;
2. guarantee the runtime, which on the very first run builds it and says so;
3. leave the settings file to the engine - the door only names its path;
4. start the engine as a SEPARATE PROGRAM with KiCad's own Python and the
   runtime on its path, on a worker thread and with no console window;
5. open step 1 in the browser when the address appears, and leave the session
   running;
6. say every failure in one sentence a non-programmer reads once.

`wx` lives here and nowhere below: `session_launch` is the mechanism and has
none, so it can be tested with neither KiCad nor a display.
"""

import threading
import webbrowser

import wx

from . import runtime, session_launch

TITLE = "eedesignpanels"

# The save is offered, not assumed. The SWIG API wraps no unsaved-changes
# accessor: `IsContentModified` is not exposed and `BOARD.IsModified()` is an
# edit flag, not document state. So the door asks on every run rather than
# saying something it cannot know.
SAVE_QUESTION = (
    "The panel builder reads the board file on disk, not the window you are "
    "looking at.\n\nSave the board now so it sees your latest work?"
)

WAITING_FIRST_LINE = "Checking that the panel builder is set up."
LAUNCH_LINE = (
    "Starting the panel builder. It reads the board and works out the panel "
    "before it opens, which takes a few seconds."
)

# The waiting window is sized once, from the longest thing it can say, so the
# box does not resize under the reader's eyes at every step of the work. These
# are the messages the dialog can show: the two above, and the ones
# `runtime.ensure` reports, with their variable parts written at a plausible
# worst case.
SIZING_MESSAGES = (
    WAITING_FIRST_LINE,
    LAUNCH_LINE,
    # runtime.fill, before the download
    "Setting up the panel builder. This happens once, takes about a minute, "
    "and needs an internet connection.",
    # runtime.fill, after the download
    "The panel builder is set up: 8888 files, 88.8 MB, 888 seconds. It will "
    "start straight away from now on.",
    # runtime.ensure, when a damaged runtime is rebuilt
    "The panel builder's setup on this computer is damaged - the folder it "
    "was installed into is missing several of its files. Rebuilding it now.",
    # runtime.fill, when the server has no such version and a local build is in
    "The published release does not have eedesignpanels 0.0.0. Using the "
    "local build in C:/work/eedesignpanels-checkout/dist instead.",
)


def sizing_message(messages=SIZING_MESSAGES) -> str:
    """The longest message the waiting window can show. It sets the width."""
    return max(messages, key=len)


def fitted(text: str, budget: int = 0) -> str:
    """Shorten *text* to the sizing message's length, so the box never grows.

    The engine's own log lines reach `say` as well, and one
    is as long as whatever path it names. A line longer than the window was
    sized for is cut rather than allowed to resize the window.
    """
    budget = budget or len(sizing_message())
    text = " ".join(text.split())
    if len(text) <= budget:
        return text
    return text[: budget - 3].rstrip() + "..."


class WaitingDialog(wx.Dialog):
    """A window that says what is happening while the door works.

    A moving bar and a line of text, because the first run builds the runtime
    over the network and a still window is read as a hang. The bar pulses
    rather than filling, because the work has no percentage to show.
    """

    WRAP_WIDTH = 460

    def __init__(self, parent, first_line: str) -> None:
        super().__init__(parent, title=TITLE,
                         style=wx.CAPTION | wx.RESIZE_BORDER)
        self.outcome = None
        # Built holding the LONGEST message, so the window is fitted to that
        # and not to the first line. The height that fit
        # gives is then held as a floor, and the text control keeps its own
        # size, so every later line changes the words and nothing else.
        self.said = wx.StaticText(self, label=sizing_message(),
                                  style=wx.ST_NO_AUTORESIZE)
        self.said.Wrap(self.WRAP_WIDTH)
        self.said.SetMinSize(
            (self.WRAP_WIDTH, self.said.GetBestSize().GetHeight())
        )
        self.bar = wx.Gauge(self, range=100, size=(self.WRAP_WIDTH, 14))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.said, 0, wx.ALL | wx.EXPAND, 14)
        sizer.Add(self.bar, 0, wx.ALL | wx.EXPAND, 14)
        self.SetSizerAndFit(sizer)
        self.SetMinSize(self.GetSize())
        self.say(first_line)
        self.CentreOnParent()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _event: self.bar.Pulse(), self.timer)
        self.timer.Start(120)

    def say(self, text: str) -> None:
        """Replace the line of text. Main thread only - use `wx.CallAfter`."""
        if not text.strip():
            return
        # No `Fit` here: fitting on every line makes the window jump. The size
        # set at construction stands for the run.
        self.said.SetLabel(fitted(text))
        self.said.Wrap(self.WRAP_WIDTH)
        self.Layout()

    def finish(self, outcome) -> None:
        """End the wait. *outcome* is a Session or the exception that stopped it."""
        self.outcome = outcome
        self.timer.Stop()
        if self.IsModal():
            self.EndModal(wx.ID_OK)


def offer_save(parent, save_board) -> bool:
    """Ask whether to save the board. False means the user backed out."""
    answer = wx.MessageBox(
        SAVE_QUESTION, TITLE,
        wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION | wx.YES_DEFAULT, parent,
    )
    if answer == wx.CANCEL:
        return False
    if answer == wx.YES:
        save_board()
    return True


def run_eedesignpanels(parent, board_path, save_board) -> None:
    """The whole door, from the menu entry to the open tab."""
    if not offer_save(parent, save_board):
        return

    dialog = WaitingDialog(parent, WAITING_FIRST_LINE)

    def say(text):
        wx.CallAfter(dialog.say, text)

    def job():
        # Every failure the two halves raise carries its own finished sentence,
        # so the worker hands the exception back whole and the main thread
        # decides how to show it. Anything else is handed back too and shown
        # on the main thread, rather than dying quietly on a worker.
        try:
            guaranteed = runtime.ensure(report=say)
            say(LAUNCH_LINE)
            interpreter = session_launch.engine_interpreter(
                guaranteed.interpreter, _kicad_python(guaranteed)
            )
            outcome = session_launch.start(
                interpreter,
                runtime.launch_environment(guaranteed),
                board_path,
                # Both routes, on purpose: the environment carries the runtime
                # folder for an interpreter that reads `PYTHONPATH`, and the
                # folder is named again for KiCad's own Python, which does not
                # (measured, see `session_launch`).
                site_packages=guaranteed.site_packages,
                on_line=say,
            )
        except BaseException as problem:  # noqa: BLE001 - re-raised below
            outcome = problem
        wx.CallAfter(dialog.finish, outcome)

    # The worker starts INSIDE the modal loop: a job that
    # finished before `ShowModal` began would call `EndModal` on a dialog that
    # is not modal yet, and the window would stay on screen for ever.
    wx.CallAfter(lambda: threading.Thread(target=job, daemon=True).start())
    dialog.ShowModal()
    outcome = dialog.outcome
    dialog.Destroy()

    if isinstance(outcome, (runtime.RuntimeProblem,
                            session_launch.LaunchProblem)):
        wx.MessageBox(str(outcome), TITLE, wx.OK | wx.ICON_ERROR, parent)
        return
    if isinstance(outcome, BaseException):
        wx.MessageBox(
            unexpected_message(outcome, keep_the_detail(outcome)),
            TITLE, wx.OK | wx.ICON_ERROR, parent,
        )
        return
    if outcome is None:
        return
    open_page(parent, outcome.address)


def unexpected_message(error: BaseException, detail_path=None) -> str:
    """What a failure nobody planned for says on screen.

    The reader is inside KiCad with no console, so the message carries the
    error's own words on one line, with no traceback and no class name, plus
    the file the traceback went to and the one next step.
    """
    said = " ".join(str(error).split()) or error.__class__.__name__
    written = (
        f"What happened is written down in:\n\n    {detail_path}\n\n"
        f"{session_launch.TELL_THE_TEAM}"
        if detail_path else
        "Keep this message. A problem report needs it."
    )
    return (
        "The panel builder stopped on a problem it did not expect.\n\n"
        "It said:\n\n"
        f"    {said}\n\n"
        "Your board has not been changed.\n\n" + written
    )


def keep_the_detail(error: BaseException):
    """Write the traceback where it can be asked for. Returns the path or None.

    Kept beside the runtime, next to pip's own transcript,
    because that is the one folder this tool owns on every machine and it is
    not inside a project a user might send to a fabricator.
    """
    import traceback

    try:
        path = runtime.runtime_home() / "last-door-error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                traceback.format_exception(
                    type(error), error, error.__traceback__
                )
            ),
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


def open_page(parent, address: str, opener=webbrowser.open) -> None:
    """Open step 1, and say the address only when the browser would not open.

    Nothing is shown on success. The page appearing is the confirmation, and a
    dialog that has to be dismissed first would cost the user a step for no
    information.
    """
    if opener(address):
        return
    wx.MessageBox(
        "The panel builder is running, but no browser would open.\n\nOpen "
        f"this address by hand:\n\n    {address}",
        TITLE, wx.OK | wx.ICON_INFORMATION, parent,
    )


def _kicad_python(guaranteed) -> str:
    """KiCad's bundled `python.exe`, for the case where the runtime named none.

    Only reached when the engine is already installed beside the calling
    interpreter, which inside KiCad is `kicad.exe`. One registry read.
    """
    if guaranteed.slot is not None:
        return str(guaranteed.interpreter)
    return str(runtime.kicad_install().interpreter)
