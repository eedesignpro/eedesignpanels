"""THE KICAD DOOR: what it promises without KiCad anywhere near it.

The subject is `plugins/kicad_door.py` and `plugins/session_launch.py` in the
plugin package - the menu entry that opens the panel builder from the PCB
editor. Everything here runs with no KiCad, no display and no network: `pcbnew`
and `wx` are stand-ins this file defines, and every process is a stand-in too.

Four properties are fenced, because each one has already cost this project time
or would cost an engineer a silent failure:

* importing the plugin outside the PCB editor registers NOTHING;
* the plugin imports no part of the engine into KiCad's interpreter;
* the command is `python -m eedesignpanels.preview <settings file>`, spawned with
  no console window and its output going to a file rather than a pipe, so the
  session outlives KiCad;
* `kicad.exe` is never handed `-m`, which is the one way this door could open a
  second KiCad instead of the panel builder.

The modules are loaded BY FILE PATH under a package name of this file's
choosing, because the real package name has a dot in it and `plugins/__init__.py`
imports `pcbnew` and `wx`.
"""

import importlib
import importlib.util
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = REPO_ROOT / "kicad_plugin" / "plugins"
PACKAGE_NAME = "eedesignpanels_plugin_under_test"


# ---------------------------------------------------------------------------
# the stand-ins for what only exists inside KiCad
# ---------------------------------------------------------------------------


class FakeActionPlugin:
    """`pcbnew.ActionPlugin`, recording every registration it is asked for."""

    registered = []

    def __init__(self):
        self.defaults()

    def register(self):
        FakeActionPlugin.registered.append(self)


def fake_pcbnew(user_units: int) -> types.ModuleType:
    """A `pcbnew` whose `GetUserUnits` answers -1 outside the PCB editor."""
    module = types.ModuleType("pcbnew")
    module.ActionPlugin = FakeActionPlugin
    module.GetUserUnits = lambda: user_units
    module.GetBoard = lambda: None
    module.SaveBoard = lambda *args: True
    return module


def fake_wx() -> types.ModuleType:
    """A `wx` with the names the door touches, and a recording MessageBox."""
    module = types.ModuleType("wx")
    for index, name in enumerate(
        [
            "OK", "CANCEL", "YES", "NO", "YES_NO", "YES_DEFAULT", "ICON_ERROR",
            "ICON_QUESTION", "ICON_INFORMATION", "CAPTION", "RESIZE_BORDER",
            "ALL", "EXPAND", "VERTICAL", "ID_OK", "GA_HORIZONTAL",
            "ST_NO_AUTORESIZE",
        ]
    ):
        setattr(module, name, 1 << index)
    module.Dialog = type("Dialog", (), {})
    module.StaticText = type("StaticText", (), {})
    module.Gauge = type("Gauge", (), {})
    module.Timer = type("Timer", (), {})
    module.BoxSizer = type("BoxSizer", (), {})
    module.EVT_TIMER = object()
    module.said = []
    module.answer = None
    module.MessageBox = lambda *args, **kwargs: (
        module.said.append(args[0]), module.answer
    )[1]
    module.CallAfter = lambda call, *args: call(*args)
    module.FindWindowByName = lambda name: None
    module.GetActiveWindow = lambda: None
    return module


WARMED = []


def stub_warm_up() -> types.ModuleType:
    """A `runner` whose kicad-cli warm-up records instead of running.

    The real one launches kicad-cli. A test may not start a program on the
    machine it runs on, and the property worth fencing is that registration
    ASKS for the warm-up, not what kicad-cli does when it is asked.
    """
    module = types.ModuleType(PACKAGE_NAME + ".runner")
    module.warm_kicad_cli = lambda: WARMED.append(True)
    return module


def load_plugin_package(user_units: int = -1):
    """Load `plugins/__init__.py` as a package, with the stand-ins in place.

    Returns (package, pcbnew stand-in, wx stand-in). The caller unloads with
    :func:`unload_plugin_package`, so one test's stand-ins never reach another.
    """
    pcbnew = fake_pcbnew(user_units)
    wx = fake_wx()
    sys.modules["pcbnew"] = pcbnew
    sys.modules["wx"] = wx
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_PACKAGE / "__init__.py",
        submodule_search_locations=[str(PLUGIN_PACKAGE)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    sys.modules[PACKAGE_NAME + ".runner"] = stub_warm_up()
    with mock.patch.object(sys, "executable", "pcbnew.exe" if user_units >= 0 else "python.exe"):
        spec.loader.exec_module(package)
    return package, pcbnew, wx


def unload_plugin_package() -> None:
    for name in list(sys.modules):
        if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
            del sys.modules[name]
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("wx", None)


def load_session_launch():
    """`session_launch.py` on its own: stdlib only, no package, no stand-ins."""
    spec = importlib.util.spec_from_file_location(
        "eedesignpanels_session_launch_under_test",
        PLUGIN_PACKAGE / "session_launch.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launch = load_session_launch()


class FakeProcess:
    """A child process that is alive until a test says it stopped."""

    def __init__(self, code=None):
        self.code = code
        self.kwargs = None

    def poll(self):
        return self.code


def recording_popen(process, writes=()):
    """A `Popen` stand-in that records its arguments and can write the log."""

    def popen(command, **kwargs):
        process.command = command
        process.kwargs = kwargs
        for line in writes:
            kwargs["stdout"].write(line + "\n")
        kwargs["stdout"].flush()
        return process

    return popen


# ---------------------------------------------------------------------------
# registering nothing, and importing nothing of the engine
# ---------------------------------------------------------------------------


class ImportingOutsideKiCad(unittest.TestCase):
    """The plugin module is inert anywhere but a PCB editor."""

    def setUp(self):
        FakeActionPlugin.registered = []
        WARMED.clear()
        self.addCleanup(unload_plugin_package)

    def test_registers_nothing_outside_the_pcb_editor(self):
        package, _pcbnew, _wx = load_plugin_package(user_units=-1)
        with mock.patch.object(sys, "executable", "python.exe"):
            self.assertFalse(package.inside_kicad())
        self.assertEqual(FakeActionPlugin.registered, [])

    def test_registers_once_inside_the_pcb_editor(self):
        package, _pcbnew, _wx = load_plugin_package(user_units=0)
        with mock.patch.object(sys, "executable", "pcbnew.exe"):
            self.assertTrue(package.inside_kicad())
        self.assertEqual(len(FakeActionPlugin.registered), 1)
        self.assertEqual(
            FakeActionPlugin.registered[0].name, "eedesignpanels: build a panel"
        )
        # The first kicad-cli call of a session costs 5.6 s against 0.18 s for
        # later ones, so loading pays it rather than the user's first click.
        for _ in range(100):
            if WARMED:
                break
            time.sleep(0.02)
        self.assertEqual(WARMED, [True])

    def test_no_part_of_the_engine_is_imported_into_kicads_python(self):
        # The whole architecture in one assertion: KiCad's interpreter has none
        # of the engine's packages, so a door that imported the engine would
        # fail on an engineer's machine and nowhere else.
        # MEASURED AS A DIFFERENCE, NOT AS A TOTAL. The suite's other tests
        # import the engine into this same interpreter, so a test that asked
        # whether ANY engine module is loaded would pass alone and fail in the
        # suite. What the design promises is that THE DOOR imports none, which
        # is the difference across its load.
        before = {
            name for name in sys.modules
            if name == "eedesignpanels" or name.startswith("eedesignpanels.")
        }
        load_plugin_package(user_units=0)
        after = {
            name for name in sys.modules
            if name == "eedesignpanels" or name.startswith("eedesignpanels.")
        }
        self.assertEqual(sorted(after - before), [])

    def test_the_door_is_reachable_from_the_entry_point(self):
        package, _pcbnew, _wx = load_plugin_package(user_units=-1)
        self.assertTrue(callable(package.run_eedesignpanels))


# ---------------------------------------------------------------------------
# where things are, and what is run
# ---------------------------------------------------------------------------


class NamingThePaths(unittest.TestCase):
    def test_settings_file_sits_in_the_panel_folder_beside_the_board(self):
        board = Path(tempfile.gettempdir()) / "job" / "board.kicad_pcb"
        self.assertEqual(
            launch.config_path_for(board),
            board.resolve().parent / "panel" / "panel.json",
        )

    def test_the_launch_log_is_stamped_and_lives_under_panel_logs(self):
        config = Path(tempfile.gettempdir()) / "job" / "panel" / "panel.json"
        log = launch.launch_log_path(config, stamp="20260915_101500")
        self.assertEqual(log.parent, config.parent / "logs")
        self.assertEqual(log.name, "20260915_101500_kicad_launch.log")

    def test_two_launches_do_not_share_one_log(self):
        config = Path(tempfile.gettempdir()) / "job" / "panel" / "panel.json"
        first = launch.launch_log_path(config, stamp="20260915_101500")
        second = launch.launch_log_path(config, stamp="20260915_101501")
        self.assertNotEqual(first, second)


class TheCommand(unittest.TestCase):
    def test_it_is_the_preview_entry_and_the_settings_path(self):
        command = launch.preview_command(r"C:\KiCad\bin\python.exe", "p.json")
        self.assertEqual(
            command,
            [r"C:\KiCad\bin\python.exe", "-u", "-m",
             "eedesignpanels.preview", "p.json"],
        )

    def test_the_runtime_folder_is_put_on_the_path_by_the_code_run(self):
        # Measured: KiCad's bundled python.exe ignores PYTHONPATH,
        # so naming the runtime folder in the environment is not enough. The
        # folder becomes the first argument instead, and the engine never
        # sees it.
        command = launch.preview_command(
            "python.exe", "p.json", site_packages="D:/runtime/packages"
        )
        self.assertEqual(command[:3], ["python.exe", "-u", "-c"])
        self.assertIn("eedesignpanels.preview", command[3])
        self.assertEqual(command[4:], ["D:/runtime/packages", "p.json"])

    def test_the_bootstrap_hides_that_folder_from_the_engine(self):
        code = launch.PATH_THEN_MODULE
        self.assertIn("sys.path.insert(0, sys.argv.pop(1))", code)
        self.assertIn("run_name='__main__'", code)

    def test_an_interpreter_that_already_has_the_engine_gets_dash_m(self):
        command = launch.preview_command("python.exe", "p.json")
        self.assertIn("-m", command)
        self.assertNotIn("-c", command)

    def test_the_door_opens_the_browser_so_the_engine_does_not(self):
        # `--open` would have the engine open a tab as well, and the door would
        # no longer be the one place that knows whether a page was asked for.
        self.assertNotIn(
            "--open", launch.preview_command("python.exe", "p.json")
        )

    def test_kicad_exe_is_never_handed_a_module_to_run(self):
        kicad = r"C:\Program Files\KiCad\10.0\bin\kicad.exe"
        python = r"C:\Program Files\KiCad\10.0\bin\python.exe"
        self.assertEqual(launch.engine_interpreter(kicad, python),
                         Path(python))

    def test_a_python_the_runtime_named_is_used_as_it_stands(self):
        python = r"C:\venv\Scripts\python.exe"
        self.assertEqual(launch.engine_interpreter(python, "other.exe"),
                         Path(python))

    def test_no_console_window_flashes(self):
        if sys.platform != "win32":
            self.skipTest("Windows process flags")
        self.assertTrue(launch.creation_flags() & launch.CREATE_NO_WINDOW)
        self.assertTrue(
            launch.creation_flags() & launch.CREATE_NEW_PROCESS_GROUP
        )


class SpawningTheSession(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.log = self.folder / "logs" / "launch.log"

    def test_the_output_goes_to_a_file_and_not_to_a_pipe(self):
        # A child holding a pipe whose reader has gone dies on its next print.
        # The session has to survive KiCad closing, so the log is a file.
        process = FakeProcess()
        launch.spawn(["python.exe"], {"PYTHONPATH": "x"}, self.log,
                     popen=recording_popen(process, ["hello"]))
        self.assertNotEqual(process.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(process.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(process.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "hello\n")

    def test_the_runtime_environment_reaches_the_child(self):
        process = FakeProcess()
        launch.spawn(["python.exe"], {"PYTHONPATH": "runtime"}, self.log,
                     popen=recording_popen(process))
        self.assertEqual(process.kwargs["env"], {"PYTHONPATH": "runtime"})

    def test_the_flags_are_on_the_call(self):
        process = FakeProcess()
        launch.spawn(["python.exe"], {}, self.log,
                     popen=recording_popen(process))
        self.assertEqual(process.kwargs["creationflags"],
                         launch.creation_flags())

    def test_a_missing_interpreter_is_one_plain_sentence(self):
        def refuse(command, **kwargs):
            raise OSError(2, "The system cannot find the file specified")

        with self.assertRaises(launch.LaunchProblem) as raised:
            launch.spawn(["nowhere.exe"], {}, self.log, popen=refuse)
        message = str(raised.exception)
        self.assertIn("could not be started", message)
        self.assertIn("nowhere.exe", message)
        self.assertNotIn("Traceback", message)


# ---------------------------------------------------------------------------
# waiting for the address
# ---------------------------------------------------------------------------


class FindingTheAddress(unittest.TestCase):
    def test_the_address_is_a_line_of_its_own(self):
        text = "  panel   deriving\nhttp://127.0.0.1:52341/\n"
        self.assertEqual(launch.address_in(text), "http://127.0.0.1:52341/")

    def test_an_address_inside_a_sentence_is_not_the_address(self):
        text = "  step 1       http://127.0.0.1:52341/ - prepare the board\n"
        self.assertIsNone(launch.address_in(text))

    def test_nothing_yet_is_not_an_answer(self):
        self.assertIsNone(launch.address_in("starting\n"))


class WaitingForTheAddress(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.log = self.folder / "launch.log"
        self.log.write_text("", encoding="utf-8")
        self.clock = [0.0]

    def tick(self, _seconds=0.0):
        self.clock[0] += 0.2

    def test_it_returns_the_address_the_engine_printed(self):
        process = FakeProcess()
        appended = []

        def sleep(_seconds):
            self.tick()
            if not appended:
                self.log.write_text(
                    "  board   x\nhttp://127.0.0.1:49999/\n", encoding="utf-8"
                )
                appended.append(True)

        address = launch.wait_for_address(
            self.log, process, sleep=sleep, now=lambda: self.clock[0]
        )
        self.assertEqual(address, "http://127.0.0.1:49999/")

    def test_the_engines_lines_are_reported_as_they_appear(self):
        process = FakeProcess()
        seen = []
        self.log.write_text(
            "  board   x\n  panel   deriving\nhttp://127.0.0.1:1/\n",
            encoding="utf-8",
        )
        launch.wait_for_address(
            self.log, process, on_line=seen.append,
            sleep=self.tick, now=lambda: self.clock[0],
        )
        self.assertIn("  panel   deriving", seen)

    def test_a_board_that_will_not_load_is_quoted_not_traced(self):
        process = FakeProcess(code=2)
        self.log.write_text(
            "cannot start: no board file at ../board.kicad_pcb\n",
            encoding="utf-8",
        )
        with self.assertRaises(launch.LaunchProblem) as raised:
            launch.wait_for_address(
                self.log, process, subject="job/plate.kicad_pcb",
                sleep=self.tick, now=lambda: self.clock[0],
            )
        message = str(raised.exception)
        self.assertIn("plate.kicad_pcb", message)
        self.assertIn("no board file", message)
        self.assertIn(str(self.log), message)
        self.assertNotIn("Traceback", message)

    def test_a_silent_death_still_says_something(self):
        process = FakeProcess(code=1)
        with self.assertRaises(launch.LaunchProblem) as raised:
            launch.wait_for_address(
                self.log, process, sleep=self.tick, now=lambda: self.clock[0]
            )
        self.assertIn("without saying anything at all",
                      str(raised.exception))

    def test_taking_too_long_is_not_reported_as_a_crash(self):
        process = FakeProcess()
        with self.assertRaises(launch.LaunchProblem) as raised:
            launch.wait_for_address(
                self.log, process, deadline_seconds=1.0,
                sleep=self.tick, now=lambda: self.clock[0],
            )
        message = str(raised.exception)
        self.assertIn("still running", message)
        self.assertIn("has not been changed", message)
        self.assertIn(str(self.log), message)


class StartingEndToEnd(unittest.TestCase):
    def test_one_call_spawns_waits_and_hands_back_the_address(self):
        # Resolved, because Windows hands out a short 8.3 temp path and the
        # door resolves the board before it derives anything from it.
        folder = Path(tempfile.mkdtemp()).resolve()
        board = folder / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        process = FakeProcess()
        session = launch.start(
            r"C:\KiCad\bin\python.exe",
            {"PYTHONPATH": "runtime"},
            board,
            stamp="20260915_120000",
            popen=recording_popen(process, ["  board   x",
                                            "http://127.0.0.1:53000/"]),
            sleep=lambda _s: None,
        )
        self.assertEqual(session.address, "http://127.0.0.1:53000/")
        self.assertEqual(session.config_path,
                         folder / "panel" / "panel.json")
        self.assertEqual(session.log_path.parent, folder / "panel" / "logs")
        self.assertEqual(
            process.command[1:],
            ["-u", "-m", "eedesignpanels.preview",
             str(folder / "panel" / "panel.json")],
        )
        # The board's own project folder, so a relative path in the settings
        # file means what it says.
        self.assertEqual(process.kwargs["cwd"], str(folder))

    def test_the_door_never_writes_the_settings_file_itself(self):
        # Creating, repairing and judging the settings file is the engine's
        # loader; the door only names the path.
        folder = Path(tempfile.mkdtemp()).resolve()
        board = folder / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        process = FakeProcess()
        launch.start(
            "python.exe", {}, board, stamp="20260915_120000",
            popen=recording_popen(process, ["http://127.0.0.1:1/"]),
            sleep=lambda _s: None,
        )
        self.assertFalse((folder / "panel" / "panel.json").exists())


# ---------------------------------------------------------------------------
# what the user is asked and told
# ---------------------------------------------------------------------------


class TheDialogs(unittest.TestCase):
    def setUp(self):
        self.addCleanup(unload_plugin_package)
        package, _pcbnew, self.wx = load_plugin_package(user_units=-1)
        self.door = importlib.import_module(PACKAGE_NAME + ".kicad_door")

    def test_the_save_offer_says_why_it_matters(self):
        self.assertIn("board file on disk", self.door.SAVE_QUESTION)

    def test_yes_saves_the_board(self):
        saved = []
        self.wx.answer = self.wx.YES
        self.assertTrue(
            self.door.offer_save(None, lambda: saved.append(True))
        )
        self.assertEqual(saved, [True])

    def test_no_goes_on_without_saving(self):
        saved = []
        self.wx.answer = self.wx.NO
        self.assertTrue(
            self.door.offer_save(None, lambda: saved.append(True))
        )
        self.assertEqual(saved, [])

    def test_cancel_stops_the_run(self):
        self.wx.answer = self.wx.CANCEL
        self.assertFalse(self.door.offer_save(None, lambda: None))

    def test_an_unexpected_failure_says_what_it_said_and_where_it_went(self):
        # Built, never shown: no window is put on anyone's screen by a test.
        message = self.door.unexpected_message(
            ValueError("the settings file names no board"), "C:/detail.log"
        )
        self.assertIn("the settings file names no board", message)
        self.assertIn("C:/detail.log", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("ValueError", message)

    def test_an_unexpected_failure_with_no_file_still_says_what_to_do(self):
        message = self.door.unexpected_message(ValueError("odd"), None)
        # With no file to point at, the message itself is the evidence.
        self.assertIn("Keep this message. A problem report needs it.", message)
        self.assertIn("has not been changed", message)

    def test_a_page_that_opened_says_nothing(self):
        self.door.open_page(None, "http://127.0.0.1:1/", opener=lambda a: True)
        self.assertEqual(self.wx.said, [])

    def test_a_page_that_would_not_open_hands_over_the_address(self):
        self.door.open_page(None, "http://127.0.0.1:1/",
                            opener=lambda a: False)
        self.assertEqual(len(self.wx.said), 1)
        self.assertIn("http://127.0.0.1:1/", self.wx.said[0])


class TheWaitingWindowHoldsItsSize(unittest.TestCase):
    """The box is sized from the longest line, once.

    A waiting window fitted to whatever line is on screen would resize at
    every step of the work. NO WINDOW IS
    RAISED HERE - the strings are measured, not the pixels, which is the same
    property: the width comes from the longest message the dialog can show,
    and no message reaching it is longer than that.
    """

    def setUp(self):
        self.addCleanup(unload_plugin_package)
        load_plugin_package(user_units=-1)
        self.door = importlib.import_module(PACKAGE_NAME + ".kicad_door")

    def test_the_size_comes_from_the_longest_message_not_the_first(self):
        longest = self.door.sizing_message()
        for message in self.door.SIZING_MESSAGES:
            self.assertLessEqual(len(message), len(longest))
        self.assertNotEqual(longest, self.door.WAITING_FIRST_LINE)

    def test_every_message_the_door_itself_shows_is_in_the_sizing_set(self):
        self.assertIn(self.door.WAITING_FIRST_LINE, self.door.SIZING_MESSAGES)
        self.assertIn(self.door.LAUNCH_LINE, self.door.SIZING_MESSAGES)

    def test_the_setup_lines_the_runtime_reports_are_sized_against(self):
        joined = " ".join(self.door.SIZING_MESSAGES)
        for phrase in [
            "Setting up the panel builder",
            "The panel builder is set up",
            "is damaged",
            "does not have",
        ]:
            self.assertIn(phrase, joined)

    def test_a_known_message_is_shown_whole(self):
        for message in self.door.SIZING_MESSAGES:
            self.assertEqual(self.door.fitted(message), message)

    def test_a_longer_line_is_cut_rather_than_growing_the_window(self):
        budget = len(self.door.sizing_message())
        cut = self.door.fitted("C:/very/long/path/" + "x" * 500)
        self.assertEqual(len(cut), budget)
        self.assertTrue(cut.endswith("..."))

    def test_a_line_with_newlines_stays_one_line(self):
        self.assertEqual(
            self.door.fitted("one" + chr(10) + "  two" + chr(10)), "one two"
        )


class TheMessagesStandAlone(unittest.TestCase):
    """A failure message carries the reason itself.

    The reader is inside KiCad. There is no console and nothing "above" a
    popup. Every message is built by a function these tests call directly -
    never by the code that puts a window on a screen.
    """

    def test_a_stopped_engine_names_the_board_the_reason_and_the_record(self):
        message = launch.stopped_message(
            "C:/job/plate.kicad_pcb",
            "cannot start: no board file at ../plate.kicad_pcb",
            "C:/job/panel/logs/launch.log",
        )
        self.assertIn("plate.kicad_pcb", message)
        self.assertIn("no board file", message)
        self.assertIn("has not been changed", message)
        self.assertIn("C:/job/panel/logs/launch.log", message)
        self.assertNotIn("above", message)

    def test_a_stopped_engine_that_said_nothing_still_gives_a_next_step(self):
        message = launch.stopped_message("plate.kicad_pcb", "", "L.log")
        self.assertIn("without saying anything at all", message)
        # The next step: keep the log, because a problem report needs it.
        self.assertIn("L.log", message)
        self.assertIn("A problem report needs it.", message)

    def test_a_slow_start_says_where_the_address_will_appear(self):
        message = launch.still_starting_message(
            "plate.kicad_pcb", "  panel   deriving", "L.log", 300
        )
        self.assertIn("5 minutes", message)
        self.assertIn("deriving", message)
        self.assertIn("pasted into a browser", message)
        self.assertIn("L.log", message)

    def test_one_minute_is_not_called_one_minutes(self):
        message = launch.still_starting_message("b", "", "L.log", 60)
        self.assertIn("1 minute ", message)
        self.assertNotIn("1 minutes", message)

    def test_an_interpreter_windows_refused_says_what_windows_said(self):
        message = launch.cannot_start_message(
            "C:/KiCad/bin/python.exe", "The system cannot find the file"
        )
        self.assertIn("C:/KiCad/bin/python.exe", message)
        self.assertIn("The system cannot find the file", message)
        self.assertIn("install KiCad 10 again", message)


if __name__ == "__main__":
    unittest.main()
