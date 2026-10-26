"""THE FILE DIALOG IS SHARP ON A HIGH-RESOLUTION SCREEN.

The standalone launcher opens a DPI-aware file dialog. A Windows process that
has not declared how it handles a high-resolution display is drawn at 96 dots
per inch and then stretched by Windows to fit, so the dialog and its lettering
come out soft. The declaration is one call and it has to happen BEFORE the
first window, because Windows settles a process's answer the first time it is
asked and refuses every later attempt.

The toolkit is not in question. KiCad's bundled Python 3.11.5 has no
`_tkinter`, so the Windows dialog raised through `comdlg32` is the only route
this door has.

NOT ONE TEST HERE RAISES A WINDOW. The declaration takes the two Windows
libraries as arguments, so the order it tries them in and what it does when one
is missing are read off stand-ins. One test makes the real call, which draws
nothing and only ever reports what this Windows answered.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = (
    REPO_ROOT
    / "kicad_plugin"
    / "launcher"
    / "launch_eedesignpanels.py"
)


def load_launcher():
    """Load the standalone door by path. Importing it starts nothing."""
    spec = importlib.util.spec_from_file_location(
        "eedesignpanels_launcher_under_dpi_test", str(LAUNCHER_SOURCE)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()


class FakeUser32:
    """A stand-in for `user32`, carrying only the calls asked of it."""

    def __init__(self, modern=None, older=None):
        self.calls = []
        self._modern = modern
        self._older = older

    def SetProcessDpiAwarenessContext(self, context):
        self.calls.append(("SetProcessDpiAwarenessContext", context.value))
        if self._modern is None:
            raise AttributeError("SetProcessDpiAwarenessContext")
        return self._modern

    def SetProcessDPIAware(self):
        self.calls.append(("SetProcessDPIAware", None))
        if self._older is None:
            raise AttributeError("SetProcessDPIAware")
        return self._older


class FakeShcore:
    """A stand-in for `shcore`, which is absent before Windows 8.1."""

    def __init__(self, answer=None):
        self.calls = []
        self._answer = answer

    def SetProcessDpiAwareness(self, level):
        self.calls.append(("SetProcessDpiAwareness", level))
        if self._answer is None:
            raise AttributeError("SetProcessDpiAwareness")
        return self._answer


@unittest.skipUnless(os.name == "nt", "the declaration is a Windows call")
class WhichCallAnswers(unittest.TestCase):
    """Newest first, and every older one behind it."""

    def test_the_modern_call_wins_and_asks_for_per_monitor_v2(self):
        user32 = FakeUser32(modern=1)
        shcore = FakeShcore(answer=0)
        self.assertEqual(
            launcher.declare_dpi_awareness(user32=user32, shcore=shcore),
            "per-monitor-v2",
        )
        self.assertEqual(user32.calls[0][0], "SetProcessDpiAwarenessContext")
        # The handle is the unsigned form of -4: that IS
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 as Windows takes it.
        import ctypes

        self.assertEqual(
            user32.calls[0][1],
            ctypes.c_void_p(launcher._DPI_PER_MONITOR_V2).value,
        )
        self.assertEqual(shcore.calls, [])

    def test_an_older_windows_falls_back_to_per_monitor(self):
        user32 = FakeUser32(modern=None, older=1)
        shcore = FakeShcore(answer=0)
        self.assertEqual(
            launcher.declare_dpi_awareness(user32=user32, shcore=shcore),
            "per-monitor",
        )
        self.assertEqual(shcore.calls, [("SetProcessDpiAwareness", 2)])

    def test_an_older_windows_still_declares_something(self):
        user32 = FakeUser32(modern=None, older=1)
        self.assertEqual(
            launcher.declare_dpi_awareness(user32=user32, shcore=FakeShcore()),
            "system",
        )
        self.assertEqual(user32.calls[-1][0], "SetProcessDPIAware")

    def test_a_declaration_somebody_else_made_first_is_not_a_failure(self):
        user32 = FakeUser32(modern=0, older=0)
        shcore = FakeShcore(answer=launcher._E_ACCESSDENIED)
        self.assertEqual(
            launcher.declare_dpi_awareness(user32=user32, shcore=shcore), "already"
        )

    def test_nothing_answering_is_still_not_an_error(self):
        # A blurry dialog is a defect; no dialog is a broken tool.
        self.assertEqual(
            launcher.declare_dpi_awareness(
                user32=FakeUser32(), shcore=FakeShcore()
            ),
            "none",
        )

    def test_the_real_call_on_this_machine_declares_this_process(self):
        # It draws nothing. It only reports what this Windows answered, and
        # anything but "none" means the dialog this door raises is sharp.
        self.assertIn(
            launcher.declare_dpi_awareness(),
            ("per-monitor-v2", "per-monitor", "system", "already"),
        )


class WhereItIsCalled(unittest.TestCase):
    """Before the window, never after, and never a toolkit."""

    def setUp(self):
        self.text = LAUNCHER_SOURCE.read_text(encoding="utf-8")

    def test_the_dialog_declares_before_it_raises_anything(self):
        body = self.text.split("def ask_for_board")[1]
        declared = body.index("declare_dpi_awareness()")
        raised = body.index("GetOpenFileNameW")
        self.assertLess(declared, raised)

    def test_the_message_box_declares_too(self):
        body = self.text.split("def show_problem")[1]
        self.assertLess(
            body.index("declare_dpi_awareness()"), body.index("MessageBoxW")
        )

    def test_the_dialog_is_refused_before_anything_is_declared(self):
        # A machine with no dialog gets the sentence about dragging a board
        # onto the shortcut, and no Windows call is made on the way to it.
        with mock.patch.object(launcher, "dialog_available", lambda: False):
            with mock.patch.object(launcher, "declare_dpi_awareness") as declared:
                with self.assertRaises(launcher.LauncherProblem) as caught:
                    launcher.ask_for_board()
            declared.assert_not_called()
        self.assertIn("shortcut", str(caught.exception))

    def test_no_second_toolkit_was_reached_for(self):
        # The word `_tkinter` stays in the docstring that records WHY the
        # Windows dialog was chosen. What may never appear is an import.
        for absent in ("import tkinter", "import wx", "import PyQt", "import PySide"):
            self.assertNotIn(absent, self.text)

    def test_the_reason_is_written_down(self):
        self.assertIn("HIGH-RESOLUTION SCREEN", self.text)


if __name__ == "__main__":
    unittest.main()
