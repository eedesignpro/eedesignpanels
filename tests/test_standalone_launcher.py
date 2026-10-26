"""THE STANDALONE DOOR: what a double-click promises, and what it may never do.

The subject is `launcher/launch_eedesignpanels.py` in the plugin package - the
shortcut an engineer with no Python and no setup uses to open a board. Its
failures are the ones nobody reports, because a shortcut that does nothing
looks like a shortcut that was never installed.

WHAT IS FENCED HERE, and why each one:

* No window is ever raised and no process is ever started. The file dialog and
  the child process are the two seams `main` takes, and every test drives them
  with a function. A test that opened a dialog would hang a CI runner for ever.
* The board a person picked is the board that gets opened, including the case
  the engine cannot answer on its own - a folder holding several boards.
* Every refusal is a sentence with no traceback in it, and cancelling the
  dialog is not a refusal at all.
* The settings file is COMPUTED from the board, never asked for.

The module is loaded BY FILE PATH for the same reason the bootstrap's own tests
load it that way: it lives outside the installed package, beside a `plugins/`
folder whose `__init__.py` imports `pcbnew` and `wx`.

ONLY A HUMAN CAN PROVE the last step: that the Windows file dialog actually
appears, that a board dropped on the shortcut opens, and that the browser comes
up on step 1. Those need a desktop session, and nothing here pretends to cover
them.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "kicad_plugin"
LAUNCHER_SOURCE = PLUGIN_ROOT / "launcher" / "launch_eedesignpanels.py"
SHORTCUT_SOURCE = PLUGIN_ROOT / "launcher" / "eedesignpanels.cmd"
RUNTIME_SOURCE = PLUGIN_ROOT / "plugins" / "runtime.py"


def load_launcher():
    """Load the launcher from its file, under its own module name."""
    spec = importlib.util.spec_from_file_location(
        "eedesignpanels_launcher_under_test", LAUNCHER_SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()

_RUN_ENGINE = None


def load_run_engine():
    """Load `run_engine.py` by path. Importing it runs nothing on its own."""
    global _RUN_ENGINE
    if _RUN_ENGINE is None:
        spec = importlib.util.spec_from_file_location(
            "eedesignpanels_run_engine_under_test", launcher.RUN_ENGINE_SOURCE
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _RUN_ENGINE = module
    return _RUN_ENGINE


class FakeInstall:
    """A KiCad install, as much of one as the launcher ever reads."""

    def __init__(self, version, interpreter):
        self.version = version
        self.interpreter = Path(interpreter)


class FakeRuntime:
    """The bootstrap, replaced by something that reaches nothing.

    Every test that gets this far would otherwise read the registry, touch the
    user's real application data folder, or run pip.
    """

    def __init__(self, home, installs=(), result=None, problem=None):
        self.RuntimeProblem = _load_real_runtime().RuntimeProblem
        self._home = Path(home)
        self._installs = list(installs)
        self._result = result
        self._problem = problem
        self.ensure_calls = []

    def runtime_home(self):
        return self._home

    def kicad_installs(self):
        return list(self._installs)

    def kicad_install(self):
        if self._installs:
            return self._installs[0]
        raise self.RuntimeProblem("could not find KiCad on this computer")

    def version_sort_key(self, version):
        return _load_real_runtime().version_sort_key(version)

    def ensure(self, report=None, on_output=None, source=None):
        self.ensure_calls.append(True)
        if self._problem is not None:
            raise self._problem
        return self._result

    def launch_environment(self, result, base=None):
        return {"PYTHONPATH": str(getattr(result, "site_packages", ""))}


class FakeResult:
    """What `ensure` hands back."""

    def __init__(self, interpreter, site_packages=None, state="warm"):
        self.interpreter = Path(interpreter)
        self.site_packages = site_packages
        self.state = state


_REAL_RUNTIME = None


def _load_real_runtime():
    """The real bootstrap, for the two pieces the fake borrows from it."""
    global _REAL_RUNTIME
    if _REAL_RUNTIME is None:
        spec = importlib.util.spec_from_file_location(
            "eedesignpanels_runtime_for_launcher_tests", RUNTIME_SOURCE
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _REAL_RUNTIME = module
    return _REAL_RUNTIME


class Recorder:
    """A stand-in for the child process. Records, never starts anything.

    It answers what `run_and_watch` answers - the exit code and the lines
    the child printed - so a test can hand back the engine's own refusal
    and check that it reaches the message somebody reads.
    """

    def __init__(self, code=0, output=()):
        self.code = code
        self.output = list(output)
        self.calls = []

    def __call__(self, command, env=None, cwd=None):
        self.calls.append({"command": list(command), "env": env, "cwd": cwd})
        return self.code, list(self.output)


def board_file(folder: Path, name="example.kicad_pcb") -> Path:
    """A file that passes every cheap check the launcher makes."""
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    return path


class TheFilesExist(unittest.TestCase):
    """The door is two files, and one of them is what gets double-clicked."""

    def test_the_shortcut_and_the_launcher_are_both_there(self):
        self.assertTrue(LAUNCHER_SOURCE.is_file(), LAUNCHER_SOURCE)
        self.assertTrue(SHORTCUT_SOURCE.is_file(), SHORTCUT_SOURCE)

    def test_it_lives_outside_the_folder_that_ships(self):
        """Everything under `plugins/` ships through the plugin catalogue, and
        the launcher is a separate entry point, so it lives elsewhere."""
        parts = LAUNCHER_SOURCE.relative_to(PLUGIN_ROOT).parts
        self.assertNotEqual(parts[0], "plugins")
        self.assertEqual(parts[0], "launcher")

    def test_the_packaging_allowlist_does_not_carry_it(self):
        """The real guard, read off the packaging tool rather than assumed.

        The package file is built from an allowlist, so a
        folder stays out of the shipped archive by not being on it. The
        public build tool is `kicad_plugin/tools/build_package_file.py`, and
        its list is `MEMBERS`. This is the assertion that catches somebody
        adding `launcher` to that list.
        """
        builder = (PLUGIN_ROOT / "tools" / "build_package_file.py").read_text(
            encoding="utf-8"
        )
        allowlist = builder.split("\nMEMBERS = ", 1)[1].split(")", 1)[0]
        self.assertNotIn("launcher", allowlist)
        self.assertIn('"plugins"', allowlist)

    def test_the_shortcut_hardcodes_no_kicad_path_or_version(self):
        """The shortcut finds KiCad at run time: no install path or version."""
        text = SHORTCUT_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("Program Files", text)
        self.assertNotIn("10.0", text)
        self.assertIn("reg query", text)

    def test_the_shortcut_runs_the_launcher_beside_it(self):
        """`%~dp0` is the shortcut's own folder, so a copy of the pair works
        anywhere and a copy of the shortcut alone fails with a sentence."""
        text = SHORTCUT_SOURCE.read_text(encoding="utf-8")
        self.assertIn('"%~dp0launch_eedesignpanels.py"', text)

    def test_the_shortcut_forwards_a_dropped_board(self):
        """A board dropped on the shortcut arrives as the first argument."""
        self.assertIn('launch_eedesignpanels.py" %*', SHORTCUT_SOURCE.read_text(
            encoding="utf-8"))


class FindingTheSettingsFile(unittest.TestCase):
    """The settings file is computed from the board, never asked for."""

    def test_it_sits_in_the_panel_folder_beside_the_board(self):
        board = Path("C:/projects/widget/widget.kicad_pcb")
        self.assertEqual(
            launcher.config_path_for(board),
            Path("C:/projects/widget/panel/panel.json"),
        )

    def test_one_board_in_the_folder_needs_no_help_from_the_launcher(self):
        """The engine's own `load_or_create` gets this case right, so the
        launcher does not write a second version of that logic."""
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder))
            self.assertFalse(launcher.needs_seeding(board))

    def test_several_boards_and_no_settings_needs_the_launcher(self):
        """The engine refuses when it cannot tell which board was meant, and
        the launcher was told which one by the person who picked it."""
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder), "one.kicad_pcb")
            board_file(Path(folder), "two.kicad_pcb")
            self.assertTrue(launcher.needs_seeding(board))

    def test_an_existing_settings_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder), "one.kicad_pcb")
            board_file(Path(folder), "two.kicad_pcb")
            config = launcher.config_path_for(board)
            config.parent.mkdir(parents=True)
            config.write_text("{}", encoding="utf-8")
            self.assertFalse(launcher.needs_seeding(board))


class ABoardThatWillNotOpen(unittest.TestCase):
    """Every refusal is a sentence, and it names the file."""

    def test_a_missing_file_says_so(self):
        message = launcher.board_problem(Path("C:/nowhere/absent.kicad_pcb"))
        self.assertIsNotNone(message)
        self.assertIn("absent.kicad_pcb", message)
        self.assertNotIn("Traceback", message)

    def test_a_folder_is_not_a_board(self):
        with tempfile.TemporaryDirectory() as folder:
            message = launcher.board_problem(Path(folder))
            self.assertIn("folder", message)

    def test_the_wrong_kind_of_file_says_which_kind_is_wanted(self):
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "notes.txt"
            other.write_text("hello", encoding="utf-8")
            message = launcher.board_problem(other)
            self.assertIn(".kicad_pcb", message)

    def test_an_empty_board_file_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / "empty.kicad_pcb"
            empty.write_bytes(b"")
            self.assertIn("empty", launcher.board_problem(empty))

    def test_a_real_board_has_no_problem(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(launcher.board_problem(board_file(Path(folder))))

    def test_every_message_is_plain_prose(self):
        """No exception class names, no module paths, no tracebacks."""
        messages = [
            launcher.board_problem(Path("C:/nowhere/absent.kicad_pcb")),
            launcher.board_problem(Path(tempfile.gettempdir())),
        ]
        for message in messages:
            self.assertNotIn("Error", message)
            self.assertNotIn("Exception", message)
            self.assertNotIn("Traceback", message)


class WhenTheSettingsNameAnotherBoard(unittest.TestCase):
    """The file wins, and the person is told which board opened."""

    def test_a_matching_settings_file_says_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder))
            config = launcher.config_path_for(board)
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"source_board": "../example.kicad_pcb"}),
                encoding="utf-8",
            )
            self.assertIsNone(launcher.board_mismatch(board, config))

    def test_a_different_board_is_named_out_loud(self):
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder), "one.kicad_pcb")
            board_file(Path(folder), "two.kicad_pcb")
            config = launcher.config_path_for(board)
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"source_board": "../two.kicad_pcb"}),
                encoding="utf-8",
            )
            message = launcher.board_mismatch(board, config)
            self.assertIn("two.kicad_pcb", message)
            self.assertIn("one.kicad_pcb", message)

    def test_an_unreadable_settings_file_never_stops_the_launch(self):
        """A warning may never be the thing that refuses. The engine gives the
        real answer about a file it cannot read."""
        with tempfile.TemporaryDirectory() as folder:
            board = board_file(Path(folder))
            config = launcher.config_path_for(board)
            config.parent.mkdir(parents=True)
            config.write_text("not json at all", encoding="utf-8")
            self.assertIsNone(launcher.board_mismatch(board, config))
            self.assertIsNone(launcher.named_board(config))


class TheCommandItRuns(unittest.TestCase):
    """One launch shape, identical to what the user types by hand."""

    def test_it_goes_through_run_engine_and_not_dash_m(self):
        """MEASURED: KiCad 10.0.4's bundled python.exe ignores
        PYTHONPATH, so `-m eedesignpanels.preview` never finds the engine.
        The runtime folder is passed as an argument instead."""
        command = launcher.preview_command(
            Path("C:/py/python.exe"),
            Path("C:/p/panel/panel.json"),
            True,
            "C:/rt/sp",
        )
        self.assertNotIn("-m", command)
        self.assertEqual(command[1], str(launcher.RUN_ENGINE_SOURCE))
        self.assertEqual(command[2], "C:/rt/sp")
        self.assertEqual(command[3], str(Path("C:/p/panel/panel.json")))
        self.assertIn("--open", command)

    def test_run_engine_sits_beside_the_launcher(self):
        self.assertTrue(launcher.RUN_ENGINE_SOURCE.is_file())
        self.assertEqual(
            launcher.RUN_ENGINE_SOURCE.parent, LAUNCHER_SOURCE.parent
        )

    def test_an_engine_already_installed_needs_no_runtime_folder(self):
        """The development venv and the CI container both reach this."""
        command = launcher.preview_command(
            Path("C:/py/python.exe"),
            Path("C:/p/panel/panel.json"),
            False,
            None,
        )
        self.assertEqual(command[2], launcher.NO_RUNTIME)

    def test_no_open_leaves_the_browser_alone(self):
        command = launcher.preview_command(
            Path("C:/py/python.exe"), Path("C:/p/panel/panel.json"), False
        )
        self.assertNotIn("--open", command)

    def test_the_child_is_told_to_spell_board_paths_in_utf8(self):
        """A board stored under an accented name may not be the thing
        that stops a launch."""
        runtime = FakeRuntime(home=Path(tempfile.gettempdir()))
        result = FakeResult("C:/py/python.exe", site_packages="C:/rt/sp")
        env = launcher.launch_environment(runtime, result)
        self.assertEqual(env["PYTHONUTF8"], "1")


class ChoosingTheNewestKiCad(unittest.TestCase):
    """The shortcut takes any KiCad; the launcher takes the newest."""

    def test_the_newest_version_wins_over_the_order_it_was_found_in(self):
        runtime = FakeRuntime(
            home=Path(tempfile.gettempdir()),
            installs=[
                FakeInstall("9.0.2", "C:/K9/bin/python.exe"),
                FakeInstall("10.0.4", "C:/K10/bin/python.exe"),
            ],
        )
        self.assertEqual(launcher.newest_install(runtime).version, "10.0.4")

    def test_no_kicad_at_all_is_the_bootstrap_s_own_sentence(self):
        runtime = FakeRuntime(home=Path(tempfile.gettempdir()), installs=[])
        with self.assertRaises(runtime.RuntimeProblem) as caught:
            launcher.newest_install(runtime)
        self.assertIn("KiCad", str(caught.exception))

    def test_the_same_interpreter_is_not_switched_to(self):
        self.assertFalse(
            launcher.should_switch_interpreter(
                sys.executable, Path(sys.executable)
            )
        )

    def test_a_different_interpreter_is_switched_to(self):
        self.assertTrue(
            launcher.should_switch_interpreter(
                sys.executable, Path("C:/other/python.exe")
            )
        )

    def test_a_switch_can_only_ever_happen_once(self):
        """`--no-switch` on the second run is what stops two installs handing
        the launch back and forth for ever."""
        args = launcher.parse_arguments(["--no-open"])
        command = launcher.switch_command(
            Path("C:/K10/bin/python.exe"), Path("C:/p/b.kicad_pcb"), args
        )
        self.assertIn("--no-switch", command)
        self.assertIn("--no-open", command)
        self.assertIn("--board", command)


class TheFirstRunSaysSomething(unittest.TestCase):
    """Eight seconds of silence reads as a broken shortcut."""

    def setUp(self):
        self.written = []
        self.progress = launcher.SetupProgress(write=self._write)

    def _write(self, text="", end="\n", flush=False):
        self.written.append(text)

    def test_a_milestone_is_printed_whole(self):
        self.progress.say("Setting up the panel builder.")
        self.assertIn("Setting up the panel builder.", self.written)

    def test_every_installer_line_becomes_one_dot(self):
        for _ in range(5):
            self.progress.tick("Collecting pydantic")
        self.assertEqual(self.written.count("."), 5)

    def test_the_installer_s_own_words_are_never_shown(self):
        self.progress.tick("ERROR: Could not build wheels for zstandard")
        self.assertNotIn(
            "ERROR: Could not build wheels for zstandard", self.written
        )

    def test_the_dot_line_is_closed_before_the_next_sentence(self):
        self.progress.tick("one")
        self.progress.say("The panel builder is set up.")
        self.assertEqual(self.written, [".", "", "The panel builder is set up."])

    def test_finishing_twice_adds_no_stray_line(self):
        self.progress.tick("one")
        self.progress.finish()
        self.progress.finish()
        self.assertEqual(self.written, [".", ""])


class ADoubleClickEndToEnd(unittest.TestCase):
    """`main`, with the dialog and the child process both replaced."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        self.home = self.root / "appdata"
        # RESOLVED, because the launcher resolves the board it was given and
        # a Windows temp folder arrives as an 8.3 short name. Comparing against
        # what the launcher will actually pass tests the behaviour, not a
        # spelling.
        self.board = board_file(
            self.root / "project", "widget.kicad_pcb"
        ).resolve()
        self.runner = Recorder()

    def tearDown(self):
        self.folder.cleanup()

    def _runtime(self, **kwargs):
        kwargs.setdefault(
            "result", FakeResult(sys.executable, site_packages=None)
        )
        kwargs.setdefault(
            "installs", [FakeInstall("10.0.4", sys.executable)]
        )
        return FakeRuntime(home=self.home, **kwargs)

    def _run(self, argv, runtime=None, asker=None):
        runtime = self._runtime() if runtime is None else runtime
        with mock.patch.object(launcher, "load_runtime", lambda *a: runtime):
            return launcher.main(argv, asker=asker, runner=self.runner)

    def test_a_board_on_the_command_line_opens_it(self):
        code = self._run([str(self.board)])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.runner.calls), 1)
        command = self.runner.calls[0]["command"]
        self.assertIn(str(launcher.RUN_ENGINE_SOURCE), command)
        self.assertIn(str(launcher.config_path_for(self.board)), command)

    def test_it_opens_step_one_in_the_browser(self):
        self._run([str(self.board)])
        self.assertIn("--open", self.runner.calls[0]["command"])

    def test_the_child_runs_in_the_board_s_own_folder(self):
        """A settings file names its board by a relative path, so the folder
        the child starts in decides what that path means."""
        self._run([str(self.board)])
        self.assertEqual(self.runner.calls[0]["cwd"], str(self.board.parent))

    def test_no_argument_asks_and_a_cancel_is_not_a_failure(self):
        code = self._run([], asker=lambda start: None)
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.calls, [])

    def test_no_argument_opens_what_the_dialog_returned(self):
        code = self._run([], asker=lambda start: self.board)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.runner.calls), 1)

    def test_the_folder_the_board_came_from_is_remembered(self):
        self._run([], asker=lambda start: self.board)
        runtime = self._runtime()
        self.assertEqual(
            launcher.remembered_folder(runtime), self.board.parent
        )

    def test_the_remembered_folder_is_offered_back_to_the_dialog(self):
        self._run([], asker=lambda start: self.board)
        seen = []

        def asker(start):
            seen.append(start)
            return self.board

        self._run([], asker=asker)
        self.assertEqual(seen, [self.board.parent])

    def test_a_bad_board_never_starts_anything(self):
        bad = self.root / "project" / "notes.txt"
        bad.write_text("hello", encoding="utf-8")
        with mock.patch.object(launcher, "show_problem") as shown:
            code = self._run([str(bad)])
        self.assertEqual(code, launcher.EXIT_BOARD)
        self.assertEqual(self.runner.calls, [])
        self.assertIn(".kicad_pcb", shown.call_args[0][0])

    def test_no_network_on_the_first_run_is_one_paragraph(self):
        problem = _load_real_runtime().RuntimeProblem(
            "The panel builder could not download the parts it needs."
        )
        runtime = self._runtime(problem=problem)
        with mock.patch.object(launcher, "show_problem") as shown:
            code = self._run([str(self.board)], runtime=runtime)
        self.assertEqual(code, launcher.EXIT_PROBLEM)
        self.assertEqual(self.runner.calls, [])
        self.assertIn("could not download", shown.call_args[0][0])
        self.assertNotIn("Traceback", shown.call_args[0][0])

    def test_no_kicad_installed_is_one_paragraph(self):
        runtime = self._runtime(installs=[])
        with mock.patch.object(launcher, "show_problem") as shown:
            code = self._run([str(self.board)], runtime=runtime)
        self.assertEqual(code, launcher.EXIT_PROBLEM)
        self.assertIn("KiCad", shown.call_args[0][0])

    def test_a_board_the_engine_refuses_carries_its_own_reason(self):
        """The message stands alone: it names the board, the settings
        file, and what the engine actually said - never a console the
        reader may no longer have."""
        self.runner = Recorder(
            code=launcher.EXIT_BOARD,
            output=["cannot start: there is no board to panelize in C:/x"],
        )
        with mock.patch.object(launcher, "show_problem") as shown:
            code = self._run([str(self.board)])
        message = shown.call_args[0][0]
        self.assertEqual(code, launcher.EXIT_BOARD)
        self.assertIn("no board to panelize", message)
        self.assertIn(str(self.board), message)
        self.assertIn("Nothing was changed", message)

    def test_any_other_crash_becomes_a_sentence(self):
        self.runner = Recorder(code=99, output=["something gave way"])
        with mock.patch.object(launcher, "show_problem") as shown:
            code = self._run([str(self.board)])
        message = shown.call_args[0][0]
        self.assertEqual(code, launcher.EXIT_PROBLEM)
        self.assertNotIn("99", message)
        self.assertIn("something gave way", message)

    def test_a_missing_bootstrap_file_is_a_sentence(self):
        with self.assertRaises(launcher.LauncherProblem) as caught:
            launcher.load_runtime(self.root / "gone" / "runtime.py")
        self.assertIn("missing a file", str(caught.exception))

    def test_it_never_touches_the_board_file(self):
        before = self.board.read_bytes()
        self._run([str(self.board)])
        self.assertEqual(self.board.read_bytes(), before)

    def test_the_older_kicad_is_left_and_the_newest_is_switched_to(self):
        runtime = self._runtime(
            installs=[
                FakeInstall("10.0.4", self.root / "K10" / "python.exe"),
                FakeInstall("9.0.2", sys.executable),
            ]
        )
        code = self._run([str(self.board)], runtime=runtime)
        self.assertEqual(code, 0)
        command = self.runner.calls[0]["command"]
        self.assertIn("--no-switch", command)
        self.assertNotIn(str(launcher.RUN_ENGINE_SOURCE), command)
        self.assertEqual(runtime.ensure_calls, [])


class EveryMessageStandsAlone(unittest.TestCase):
    """Every message the launcher shows carries its reason in plain words.

    No message sends the reader to output they cannot see - no "printed
    above", no console, no traceback - because a console window that has
    closed, or a message box with no console behind it, leaves that reader
    with nothing.
    """

    BANNED = (
        "printed above",
        "above",
        "see the console",
        "the output",
        "Traceback",
        "stdout",
        "stderr",
    )

    def messages(self):
        """Every message this launcher can put in front of a person."""
        board = Path("C:/projects/widget/widget.kicad_pcb")
        config = launcher.config_path_for(board)
        found = [
            launcher.engine_failure(
                launcher.EXIT_BOARD, board, config, ["cannot start: no prep"]
            ),
            launcher.engine_failure(launcher.EXIT_BOARD, board, config, []),
            launcher.engine_failure(1, board, config, ["it gave way"]),
            launcher.engine_failure(1, board, config, []),
            launcher.board_problem(Path("C:/nowhere/absent.kicad_pcb")),
            launcher.board_problem(Path(tempfile.gettempdir())),
        ]
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "notes.txt"
            other.write_text("x", encoding="utf-8")
            found.append(launcher.board_problem(other))
            try:
                launcher.load_runtime(Path(folder) / "gone.py")
            except launcher.LauncherProblem as problem:
                found.append(str(problem))
        return [message for message in found if message]

    def test_none_of_them_points_at_output_the_reader_cannot_see(self):
        for message in self.messages():
            for banned in self.BANNED:
                self.assertNotIn(
                    banned, message, f"{banned!r} in: {message}"
                )

    def test_each_one_names_the_file_it_is_about(self):
        for message in self.messages():
            self.assertTrue(
                any(mark in message for mark in (".kicad_pcb", ".json", ".py")),
                message,
            )

    def test_a_refused_board_message_carries_the_engine_s_words(self):
        board = Path("C:/projects/widget/widget.kicad_pcb")
        message = launcher.engine_failure(
            launcher.EXIT_BOARD,
            board,
            launcher.config_path_for(board),
            ["cannot start: there is no board to panelize in C:/projects"],
        )
        self.assertIn("no board to panelize", message)
        self.assertIn("widget.kicad_pcb", message)
        self.assertIn("Nothing was changed", message)

    def test_it_still_stands_alone_when_the_engine_said_nothing(self):
        board = Path("C:/projects/widget/widget.kicad_pcb")
        message = launcher.engine_failure(
            launcher.EXIT_BOARD, board, launcher.config_path_for(board), []
        )
        self.assertIn("widget.kicad_pcb", message)
        self.assertNotIn("What it said", message)


class QuotingTheEngineWithoutQuotingATraceback(unittest.TestCase):
    """What gets repeated back, and what is thrown away."""

    def test_the_last_plain_lines_are_kept_and_the_preamble_is_dropped(self):
        reason = launcher.engine_reason(
            [
                "eedesignpanels.preview 0.1.0",
                "",
                "  config       C:/p/panel/panel.json",
                "  board        C:/p/widget.kicad_pcb",
                "config error: no cutter diameter",
            ]
        )
        self.assertIn("config error: no cutter diameter", reason)
        self.assertNotIn("eedesignpanels.preview 0.1.0", reason)
        self.assertNotIn("", reason.splitlines())

    def test_a_traceback_is_thrown_away_whole(self):
        reason = launcher.engine_reason(
            [
                "cannot start: the board would not load",
                "Traceback (most recent call last):",
                '  File "x.py", line 1, in <module>',
                "ValueError: bad token",
            ]
        )
        self.assertIn("would not load", reason)
        self.assertNotIn("Traceback", reason)
        self.assertNotIn("ValueError", reason)
        self.assertNotIn("x.py", reason)

    def test_output_that_was_only_a_traceback_leaves_nothing(self):
        self.assertEqual(
            launcher.engine_reason(
                ["Traceback (most recent call last):", "ValueError: bad"]
            ),
            "",
        )

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(launcher.engine_reason([]), "")
        self.assertEqual(launcher.engine_reason(["", "   "]), "")

    def test_it_never_grows_into_a_log(self):
        many = [f"line {n}" for n in range(50)]
        self.assertEqual(
            len(launcher.engine_reason(many).splitlines()),
            launcher.REASON_LINES,
        )


class TheFileThatCarriesTheRuntime(unittest.TestCase):
    """`run_engine.py`, which exists because of one measurement.

    MEASURED: KiCad 10.0.4's bundled `python.exe`
    puts nothing from `PYTHONPATH` on `sys.path`, while an ordinary Python
    does. Without this file the child dies with `No module named
    'eedesignpanels'` and the shortcut looks broken.
    """

    def setUp(self):
        self.source = launcher.RUN_ENGINE_SOURCE
        self.text = self.source.read_text(encoding="utf-8")

    def test_it_exists_beside_the_launcher(self):
        self.assertTrue(self.source.is_file(), self.source)

    def test_it_runs_the_same_module_the_user_types(self):
        self.assertIn('PREVIEW_MODULE = "eedesignpanels.preview"', self.text)

    def test_it_puts_the_runtime_first_on_the_path(self):
        """Ahead of anything an engineer happens to have installed."""
        self.assertIn("sys.path.insert(0, runtime_folder)", self.text)

    def test_it_decides_nothing_about_the_engine_s_arguments(self):
        """Every argument after the runtime folder is handed straight on."""
        self.assertIn("sys.argv = [PREVIEW_MODULE] + rest", self.text)

    def test_it_records_the_measurement_that_made_it_necessary(self):
        self.assertIn("PYTHONPATH", self.text)
        self.assertIn("KiCad 10.0.4", self.text)

    def test_a_half_built_runtime_is_a_sentence_not_a_traceback(self):
        """The message stands alone, names the folder to throw away, and
        gives one instruction."""
        engine = load_run_engine()
        message = engine.incomplete_runtime(
            "C:/rt/sp", ImportError("no pydantic_core", name="pydantic_core")
        )
        self.assertIn("pydantic_core", message)
        self.assertIn("C:/rt/sp", message)
        self.assertIn("start the panel builder again", message)
        for banned in ("Traceback", "above", "console", "stderr"):
            self.assertNotIn(banned, message)

    def test_with_no_runtime_folder_it_names_none_to_delete(self):
        engine = load_run_engine()
        message = engine.incomplete_runtime(
            engine.NO_RUNTIME, ImportError("gone", name="gone")
        )
        self.assertNotIn("Delete this folder", message)
        self.assertIn("gone", message)

    def test_it_refuses_an_empty_argument_list_in_plain_words(self):
        engine = load_run_engine()
        self.assertEqual(engine.main([]), 2)


class WhatItMayNeverDo(unittest.TestCase):
    """Rules the file has to keep, read off the source."""

    def setUp(self):
        self.text = LAUNCHER_SOURCE.read_text(encoding="utf-8")

    def test_it_imports_neither_wx_nor_pcbnew(self):
        """The launcher runs with no KiCad session, so it imports neither."""
        self.assertNotIn("import wx", self.text)
        self.assertNotIn("import pcbnew", self.text)

    def test_it_hardcodes_no_machine_path(self):
        self.assertNotIn("Program Files", self.text)
        self.assertNotIn("C:\\Users", self.text)

    def test_it_names_no_token_or_credential(self):
        for word in ("GITLAB_TOKEN", "PRIVATE-TOKEN", "password", "secret"):
            self.assertNotIn(word, self.text)

    def test_the_only_engine_import_is_the_settings_seed(self):
        """The launcher starts the engine, it does not run it. One import,
        in `seed_config`, of the two functions the engine's
        own `create_config` calls."""
        lines = [
            line.strip()
            for line in self.text.splitlines()
            if "eedesignpanels.config" in line and "import" in line
        ]
        self.assertEqual(
            lines,
            ["from eedesignpanels.config import config_text, default_config_data"],
        )

    def test_tkinter_is_not_reached_for(self):
        """MEASURED: KiCad's bundled Python 3.11.5 has no
        `_tkinter`, so any tkinter dialog would raise on every machine. The
        module docstring says so, which is why this looks for the import
        rather than for the word."""
        self.assertNotIn("import tkinter", self.text)
        self.assertNotIn("from tkinter", self.text)
        self.assertIn("comdlg32", self.text)

    def test_the_dialog_filter_ends_in_two_nuls(self):
        """The API reads the type list until it meets an empty entry."""
        self.assertTrue(launcher._FILE_FILTER.endswith("\0\0"))
        self.assertIn("*.kicad_pcb", launcher._FILE_FILTER)


class TheDialogOnThisMachine(unittest.TestCase):
    """The one part only a desktop session can finish proving."""

    @unittest.skipUnless(os.name == "nt", "the Windows dialog needs Windows")
    def test_windows_can_raise_it(self):
        """The entry point resolves. That it APPEARS is a manual test."""
        self.assertTrue(launcher.dialog_available())

    @unittest.skipUnless(os.name == "nt", "the Windows dialog needs Windows")
    def test_the_request_block_is_the_size_windows_expects(self):
        """A wrong `lStructSize` is how this fails silently: the dialog simply
        never appears and nothing says why."""
        import ctypes

        structure = launcher._open_file_name_struct()
        block = structure()
        block.lStructSize = ctypes.sizeof(structure)
        self.assertEqual(block.lStructSize, ctypes.sizeof(structure))
        self.assertGreaterEqual(block.lStructSize, 140)

    @unittest.skipIf(os.name == "nt", "off Windows there is no dialog")
    def test_off_windows_it_says_to_drop_a_board_instead(self):
        with self.assertRaises(launcher.LauncherProblem) as caught:
            launcher.ask_for_board()
        self.assertIn("Drag", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
