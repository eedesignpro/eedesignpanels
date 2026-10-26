"""eedesignpanels - registers the plugin's toolbar actions.

This module is the **entry point and nothing else**. It reports the open
board's file path, saves it on request, and hands both to the door. It reads no
geometry, creates no items, and the engine it opens is a separate program that
never sees `pcbnew` at all, because KiCad's Python carries none of the engine's
packages.

Importing this module defines classes and registers nothing. Registration is
guarded so the module can be imported and inspected outside
`kicad.exe` without KiCad's `register_action` assertion putting a wxWidgets
dialog on someone's screen.
"""

import os
import sys
import threading

import pcbnew
import wx

from .kicad_door import run_eedesignpanels
from .runner import warm_kicad_cli

__version__ = "0.1.0"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class EedesignpanelsPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "eedesignpanels: build a panel"
        self.category = "Fabrication"
        self.description = "Builds a fabrication panel from the open board."
        # Without both of these the action appears only under
        # Tools > External Plugins, with the default puzzle-piece icon.
        # icon_file_name must be an absolute path.
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(_PLUGIN_DIR, "icon.png")

    def Run(self):
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox(
                "No board is open.", "eedesignpanels",
                wx.OK | wx.ICON_ERROR)
            return

        board_path = board.GetFileName()
        if not board_path:
            wx.MessageBox(
                "This board has never been saved, so there is no file for the "
                "panel builder to read.\n\nSave it and run the plugin again.",
                "eedesignpanels", wx.OK | wx.ICON_ERROR)
            return

        parent = wx.FindWindowByName("PcbFrame") or wx.GetActiveWindow()
        run_eedesignpanels(
            parent, board_path, save_board=_make_saver(board, board_path)
        )


def _make_saver(board, board_path):
    """A callable that writes the open board to its own file.

    The panel builder reads the file on disk, so the editor's in-memory edits
    have to reach it first. `aSkipSettings=True` keeps this to the .kicad_pcb:
    the core reads nothing else, and rewriting the project file from a plugin
    would be a side effect nobody asked for.

    Verified against the KiCad 10.0 source: `SaveBoard` is the only save the
    SWIG API exposes, and it does not clear the editor's modified flag - KiCad
    will still offer to save on close. There is no way to ask KiCad whether the
    board is dirty, so the dialog offers this on every run.
    """

    def save():
        if not pcbnew.SaveBoard(board_path, board, True):
            raise RuntimeError(f"pcbnew.SaveBoard refused to write {board_path}")

    return save


def inside_kicad() -> bool:
    """Allow startup registration before KiCad assigns the PCB frame pointer.

    Plain Python imports stay inert. KiCad loads plugins inside its own
    executable, including when the manager opens the PCB editor.
    """
    return os.path.basename(sys.executable).lower() in {
        "kicad.exe", "pcbnew.exe", "kicad", "pcbnew",
    }


def register() -> None:
    """Register the action and warm kicad-cli. Only meaningful inside KiCad."""
    # The first kicad-cli invocation of a KiCad session costs 5.6 s against
    # 0.18 s for later ones, so it is paid here rather than by the first thing
    # the user clicks. Daemon, so the warm-up never holds KiCad's startup.
    threading.Thread(target=warm_kicad_cli, daemon=True).start()
    EedesignpanelsPlugin().register()


if inside_kicad():
    register()
