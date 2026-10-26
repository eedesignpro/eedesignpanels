"""A RUNTIME THAT IS THERE IS NEVER REBUILT, AND A BROKEN ONE REPAIRS ITSELF.

What the runtime does when it is present, misnamed, unpublished or damaged,
all in `plugins/runtime.py`, all fenced here.

1. THE STATUS LINE ANSWERED A DIFFERENT QUESTION. `--status`
   printed "already here    no" while every field it compares agreed and the
   runtime was complete: that line reported whether the engine is installed
   into the PYTHON RUNNING THE COMMAND, which is a different question with a
   different answer. The readout now asks the same two functions a door asks,
   so a status line and a launch can never disagree.

2. THE FOLDER NAME WAS TREATED AS THE IDENTITY. A slot is named
   after the KiCad version, and that version comes from the registry when the
   registry knows the install and from the folder name when it does not - two
   routes that answer `10.0.4` and `10.0` for one install. The stamp is the
   identity now, so a sound runtime is found wherever it sits.

3. THE LOCAL FILL. Until `eedesignpanels` is published,
   both doors would ask a server that has no such version and stop. A wheel
   built from the engine's own project in this repository is used instead, and
   the run SAYS SO in one line so nobody mistakes it for a published build.

4. A DAMAGED RUNTIME REPAIRS ITSELF. It is thrown away and refilled with no
   manual step - and the throwing away is a RENAME, because Windows refuses
   to rename a folder holding an open file. MEASURED on a Windows
   machine: `PermissionError`, Windows error 5. That refusal stops a
   rebuild under a live session from deleting what it can, filling around
   the rest and reporting success.

Nothing here raises a window, reaches the network or starts pip. The one real
file lock is taken on a throwaway folder in the test's own temporary directory.
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
PLUGIN_DIR = REPO_ROOT / "kicad_plugin"
RUNTIME_SOURCE = PLUGIN_DIR / "plugins" / "runtime.py"
RUN_ENGINE_SOURCE = PLUGIN_DIR / "launcher" / "run_engine.py"


def load_by_path(path: Path, name: str):
    """Load a module from its file, registered before it executes.

    The registration is not optional: under `from __future__ import
    annotations` a frozen dataclass resolves its annotations through
    `sys.modules[cls.__module__]`.
    """
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = load_by_path(RUNTIME_SOURCE, "eedesignpanels_runtime_under_repair")


def fake_install(root: Path, version="10.0.4", tag="cp311"):
    """A KiCadInstall pointing at a folder a test made."""
    interpreter = root / "bin" / "python.exe"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"")
    return runtime.KiCadInstall(
        version=version, root=root, interpreter=interpreter, python_tag=tag
    )


def stamp_for(install, version=None, source=None):
    """The record a finished fill writes."""
    return {
        "schema": runtime.STAMP_SCHEMA,
        "distribution": runtime.ENGINE_DISTRIBUTION,
        "version": version or runtime.ENGINE_VERSION,
        "python_tag": install.python_tag,
        "kicad_version": install.version,
        "interpreter": str(install.interpreter),
        "source": source,
    }


def build_slot(slot: Path, install, version=None, source=None, engine=True):
    """A slot that looks like a finished fill, stamp and packages and all."""
    packages = slot / runtime.PACKAGES_NAME
    packages.mkdir(parents=True, exist_ok=True)
    if engine:
        (packages / runtime.ENGINE_PACKAGE).mkdir(exist_ok=True)
        (packages / runtime.ENGINE_PACKAGE / "__init__.py").write_text(
            "", encoding="utf-8"
        )
    (slot / runtime.STAMP_NAME).write_text(
        json.dumps(stamp_for(install, version, source)), encoding="utf-8"
    )
    return slot


class TempHome:
    """Point the module's one owned folder at a throwaway directory."""

    def __enter__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self._patch = mock.patch.object(
            runtime, "runtime_home", lambda: self.home
        )
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._temp.cleanup()
        return False


def public_route():
    """The public sources, whatever the machine running the tests has set."""
    return mock.patch.dict(os.environ, {runtime.WHEEL_FOLDER_VARIABLE: ""})


def local_build(allowed: bool):
    """Set or clear the developer opt-in for the duration of a test.

    Every test that touches the fill states which side of the opt-in it is
    on, so no test depends on how the machine running it is set up.
    """
    value = "1" if allowed else ""
    return mock.patch.dict(
        os.environ, {runtime.LOCAL_BUILD_VARIABLE: value}, clear=False
    )


# ---------------------------------------------------------------------------


class WhatDamagedMeans(unittest.TestCase):
    """The one definition both doors ask, and its four plain reasons."""

    def setUp(self):
        self.home = TempHome().__enter__()
        self.addCleanup(self.home.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.install = fake_install(Path(self.temp.name))
        self.slot = runtime.runtime_slot(self.install)

    def test_a_finished_runtime_is_not_damaged(self):
        build_slot(self.slot, self.install)
        self.assertIsNone(runtime.damage(self.slot, self.install))
        self.assertTrue(runtime.runtime_is_ready(self.install))

    def test_no_packages_folder_is_a_reason(self):
        self.slot.mkdir(parents=True)
        self.assertIn("packages", runtime.damage(self.slot, self.install))

    def test_no_record_is_a_reason(self):
        (self.slot / runtime.PACKAGES_NAME).mkdir(parents=True)
        self.assertIn("record", runtime.damage(self.slot, self.install))

    def test_a_record_for_another_version_is_a_reason(self):
        build_slot(self.slot, self.install, version="0.0.1")
        self.assertIn("different", runtime.damage(self.slot, self.install))

    def test_a_tree_the_record_lies_about_is_a_reason(self):
        # The case that used to end in "delete this folder": the record says
        # the fill finished and the engine is not in the tree.
        build_slot(self.slot, self.install, engine=False)
        reason = runtime.damage(self.slot, self.install)
        self.assertIn("engine is not in it", reason)
        self.assertFalse(runtime.runtime_is_ready(self.install))

    def test_a_fill_records_whether_the_engine_landed(self):
        packages = self.slot / runtime.PACKAGES_NAME
        (packages / runtime.ENGINE_PACKAGE).mkdir(parents=True)
        (packages / runtime.ENGINE_PACKAGE / "__init__.py").write_text("x")
        self.assertTrue(runtime._write_stamp(self.slot, self.install, None)["engine"])
        self.assertIsNone(runtime.damage(self.slot, self.install))

    def test_a_record_that_says_the_engine_never_landed_is_not_damage(self):
        # A record with no such line is read as "the engine should be there",
        # which is what every real fill produces. A record that says otherwise
        # is taken at its word rather than second-guessed.
        build_slot(self.slot, self.install, engine=False)
        stamp = json.loads((self.slot / runtime.STAMP_NAME).read_text())
        self.assertNotIn("engine", stamp)
        self.assertIsNotNone(runtime.damage(self.slot, self.install))
        stamp["engine"] = False
        (self.slot / runtime.STAMP_NAME).write_text(json.dumps(stamp))
        self.assertIsNone(runtime.damage(self.slot, self.install))

    def test_every_reason_is_plain_words(self):
        for reason in (
            "the folder holding its packages is missing",
            "the record of what was installed is missing or unreadable",
        ):
            for jargon in ("stamp", "None", "Error", "_"):
                self.assertNotIn(jargon, reason)


class TheStampIsTheIdentity(unittest.TestCase):
    """Defect 2: a healthy runtime under another folder name is still it."""

    def test_the_preferred_name_is_used_when_it_is_sound(self):
        with TempHome(), tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            slot = build_slot(runtime.runtime_slot(install), install)
            self.assertEqual(runtime.ready_slot(install), slot)

    def test_a_slot_named_for_another_version_string_is_not_refilled(self):
        # The registry answers `10.0.4` and the folder name answers `10.0` for
        # one install. Both slots are the same runtime and only one is built.
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp), version="10.0")
            older_name = home.home / "runtime" / "kicad-10.0.4-cp311"
            build_slot(older_name, install)
            self.assertEqual(runtime.ready_slot(install), older_name)
            self.assertTrue(runtime.runtime_is_ready(install))

    def test_a_slot_built_for_another_python_is_not_borrowed(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp), tag="cp311")
            other = fake_install(Path(temp), tag="cp312")
            build_slot(home.home / "runtime" / other.slot_name, other)
            self.assertIsNone(runtime.ready_slot(install))

    def test_a_condemned_folder_is_never_read_as_a_runtime(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            name = install.slot_name + runtime.DISCARD_MARK + "1234-5"
            build_slot(home.home / "runtime" / name, install)
            self.assertIsNone(runtime.ready_slot(install))


class TheStatusReadout(unittest.TestCase):
    """Defect 1: every label answers the question it asks."""

    def setUp(self):
        self.home = TempHome().__enter__()
        self.addCleanup(self.home.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.install = fake_install(Path(self.temp.name))

    def _describe(self, ambient=None):
        with mock.patch.object(
            runtime, "installed_engine_version", lambda: ambient
        ), mock.patch.object(
            runtime, "kicad_installs", lambda: [self.install]
        ):
            return runtime.describe()

    def test_a_ready_runtime_reads_as_ready_though_this_python_lacks_it(self):
        build_slot(runtime.runtime_slot(self.install), self.install)
        text = self._describe(ambient=None)
        self.assertIn("runtime          ready", text)
        self.assertIn("in this python   no", text)

    def test_the_label_that_answered_another_question_is_gone(self):
        # The words survive in the comment that records why they went. What
        # must never come back is the LINE.
        self.assertNotIn("already here", self._describe())
        text = RUNTIME_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn('f"already here', text)

    def test_a_damaged_runtime_says_it_will_be_rebuilt_and_why(self):
        build_slot(runtime.runtime_slot(self.install), self.install, engine=False)
        text = self._describe()
        self.assertIn("will be rebuilt", text)
        self.assertIn("engine is not in it", text)

    def test_a_local_fill_is_named_in_the_readout(self):
        build_slot(
            runtime.runtime_slot(self.install), self.install, source="C:/work/engine"
        )
        self.assertIn("filled from      local build C:/work/engine", self._describe())

    def test_the_readout_reaches_no_network_and_starts_no_process(self):
        build_slot(runtime.runtime_slot(self.install), self.install)

        def refuse(*args, **kwargs):
            raise AssertionError("the readout reached a process or the network")

        with mock.patch("subprocess.Popen", refuse), mock.patch(
            "subprocess.run", refuse
        ), mock.patch("socket.socket", refuse):
            self.assertIn("ready", self._describe())


class ThrowingARuntimeAway(unittest.TestCase):
    """Rename first, delete second, or refuse and say who."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_slot_that_is_not_there_is_already_discarded(self):
        runtime._discard(self.root / "runtime" / "nothing")  # must not raise

    def test_a_free_slot_goes_completely(self):
        slot = self.root / "runtime" / "slot"
        (slot / runtime.PACKAGES_NAME / "pydantic").mkdir(parents=True)
        (slot / runtime.PACKAGES_NAME / "pydantic" / "x.py").write_text("x")
        runtime._discard(slot)
        self.assertFalse(slot.exists())
        self.assertEqual(list((self.root / "runtime").iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "the lock is a Windows behaviour")
    def test_a_slot_a_live_session_is_using_is_refused_and_left_alone(self):
        # MEASURED: Windows refuses to rename a folder holding an
        # open file - PermissionError, error 5. That refusal is the whole
        # guard, so it is taken for real here rather than pretended.
        slot = self.root / "runtime" / "slot"
        packages = slot / runtime.PACKAGES_NAME
        packages.mkdir(parents=True)
        held = packages / "_pydantic_core.cp311-win_amd64.pyd"
        held.write_bytes(b"x")
        handle = open(held, "rb")
        self.addCleanup(handle.close)
        with mock.patch.object(runtime, "_holders_of", lambda slot: ["python.exe (7)"]):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime._discard(slot)
        self.assertIn("python.exe (7)", caught.exception.message)
        self.assertIn("changed nothing", caught.exception.message)
        self.assertTrue(held.is_file())

    @unittest.skipUnless(os.name == "nt", "the lock is a Windows behaviour")
    def test_a_held_runtime_is_never_filled_over_the_top(self):
        # A session holds a file open, so the folder cannot be renamed. The
        # fill refuses instead of filling around what it could not delete.
        slot = self.root / "runtime" / "slot"
        (slot / runtime.PACKAGES_NAME).mkdir(parents=True)
        held = slot / runtime.PACKAGES_NAME / "held.pyd"
        held.write_bytes(b"x")
        handle = open(held, "rb")
        self.addCleanup(handle.close)
        install = fake_install(self.root / "kicad")

        def no_pip(*args, **kwargs):
            raise AssertionError("pip ran on a runtime another session holds")

        with mock.patch.object(runtime, "runtime_slot", lambda i: slot), mock.patch.object(
            runtime, "_run_pip", no_pip
        ), mock.patch.object(runtime, "_holders_of", lambda slot: []):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime.fill(install)
        self.assertIn("still using", caught.exception.message)
        self.assertTrue(held.is_file())

    def test_a_refusal_never_shows_a_traceback_or_a_path_to_paste(self):
        with mock.patch.object(Path, "rename", side_effect=PermissionError(5, "held")):
            slot = self.root / "runtime" / "slot"
            slot.mkdir(parents=True)
            with mock.patch.object(runtime, "_holders_of", lambda slot: []):
                with self.assertRaises(runtime.RuntimeProblem) as caught:
                    runtime._discard(slot)
        message = caught.exception.message
        for jargon in ("Traceback", "PermissionError", "rmtree", "--target"):
            self.assertNotIn(jargon, message)

    def test_leftovers_from_an_earlier_run_are_swept_up(self):
        home = self.root / "runtime"
        home.mkdir(parents=True)
        (home / ("slot" + runtime.DISCARD_MARK + "9-9")).mkdir()
        free = home / "slot"
        free.mkdir()
        runtime._discard(free)
        self.assertEqual(list(home.iterdir()), [])


class RepairingItself(unittest.TestCase):
    """A damaged runtime is thrown away and refilled with no manual step."""

    def setUp(self):
        self.home = TempHome().__enter__()
        self.addCleanup(self.home.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.install = fake_install(Path(self.temp.name))

    def _ensure(self, said):
        with mock.patch.object(
            runtime, "installed_engine_version", lambda: None
        ), mock.patch.object(
            runtime, "kicad_install", lambda: self.install
        ), mock.patch.object(runtime, "fill") as filled:
            filled.return_value = "rebuilt"
            answer = runtime.ensure(report=said.append)
        return answer, filled

    def test_a_damaged_runtime_is_rebuilt_and_the_reason_is_said_once(self):
        build_slot(runtime.runtime_slot(self.install), self.install, engine=False)
        said = []
        answer, filled = self._ensure(said)
        self.assertEqual(answer, "rebuilt")
        filled.assert_called_once()
        self.assertEqual(len(said), 1)
        self.assertIn("damaged", said[0])
        self.assertIn("engine is not in it", said[0])

    def test_nothing_built_yet_is_not_called_damaged(self):
        said = []
        answer, filled = self._ensure(said)
        self.assertEqual(answer, "rebuilt")
        filled.assert_called_once()
        self.assertEqual(said, [])

    def test_a_sound_runtime_is_never_rebuilt(self):
        build_slot(runtime.runtime_slot(self.install), self.install)
        said = []
        with mock.patch.object(
            runtime, "installed_engine_version", lambda: None
        ), mock.patch.object(
            runtime, "kicad_install", lambda: self.install
        ), mock.patch.object(runtime, "fill") as filled:
            result = runtime.ensure(report=said.append)
        filled.assert_not_called()
        self.assertEqual(result.state, "warm")
        self.assertEqual(said, [])

    def test_no_message_anywhere_asks_somebody_to_delete_a_folder(self):
        for source in (RUNTIME_SOURCE, RUN_ENGINE_SOURCE):
            text = source.read_text(encoding="utf-8").lower()
            self.assertNotIn("delete this folder", text)


class TheLocalFill(unittest.TestCase):
    """Defect 3: a build from this repository, named out loud every time."""

    def test_the_engine_project_is_found_by_walking_up_from_the_plugin(self):
        project = runtime.engine_project()
        self.assertIsNotNone(project)
        self.assertEqual(project, REPO_ROOT)
        self.assertTrue((project / "pyproject.toml").is_file())

    def test_no_machine_path_is_written_down_in_the_module(self):
        text = RUNTIME_SOURCE.read_text(encoding="utf-8")
        for path in ("C:\\\\KiCad", "C:/KiCad", "Users\\\\", "Users/"):
            self.assertNotIn(path, text)

    def test_a_built_wheel_is_preferred_over_the_source_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "eedesignpanels-checkout"
            (project / runtime.WHEEL_FOLDER).mkdir(parents=True)
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            wheel = (
                project
                / runtime.WHEEL_FOLDER
                / f"eedesignpanels-{runtime.ENGINE_VERSION}-py3-none-any.whl"
            )
            wheel.write_bytes(b"")
            with mock.patch.object(runtime, "engine_project", lambda: project):
                given, line = runtime.local_source()
            self.assertEqual(given, str(wheel))
            self.assertIn("NOT from the published release", line)

    def test_with_no_wheel_the_source_folder_is_given_and_named(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "eedesignpanels-checkout"
            project.mkdir(parents=True)
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            with mock.patch.object(runtime, "engine_project", lambda: project):
                given, line = runtime.local_source()
            self.assertEqual(given, str(project))
            self.assertIn(str(project), line)
            self.assertIn("NOT from the published release", line)

    def test_an_installed_copy_of_the_plugin_has_no_local_route(self):
        # Two folders above `plugins/` there is no engine project when the
        # plugin came from the package file, so a user never reaches this.
        with tempfile.TemporaryDirectory() as temp:
            elsewhere = Path(temp) / "a" / "b" / "c" / "runtime.py"
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_text("", encoding="utf-8")
            with mock.patch.object(runtime, "__file__", str(elsewhere)):
                self.assertIsNone(runtime.engine_project())
                self.assertIsNone(runtime.local_source())

    def test_the_server_is_asked_first_and_the_local_build_only_after(self):
        calls = []

        def pretend_pip(command, on_output):
            calls.append(command)
            if len(calls) == 1:
                return 1, "ERROR: No matching distribution found for x"
            target = Path(command[command.index("--target") + 1])
            (target / runtime.ENGINE_PACKAGE).mkdir(parents=True, exist_ok=True)
            (target / runtime.ENGINE_PACKAGE / "__init__.py").write_text("x")
            return 0, "Successfully installed"

        said = []
        with TempHome(), tempfile.TemporaryDirectory() as temp, local_build(True), \
                public_route():
            install = fake_install(Path(temp))
            with mock.patch.object(runtime, "_run_pip", pretend_pip), mock.patch.object(
                runtime, "local_source", lambda: ("C:/work/engine", "Installed from x")
            ):
                result = runtime.fill(install, report=said.append)
            self.assertEqual(result.state, "filled")
            stamp = runtime.read_stamp(runtime.runtime_slot(install))
        self.assertEqual(len(calls), 2)
        # The published archives first, then the local build.
        self.assertIn(
            f"{runtime.ENGINE_DISTRIBUTION} @ {runtime.ENGINE_SOURCE_URL}", calls[0]
        )
        self.assertIn(
            f"{runtime.CORE_DISTRIBUTION} @ {runtime.CORE_SOURCE_URL}", calls[0]
        )
        self.assertIn("C:/work/engine", calls[1])
        self.assertEqual(stamp["source"], "C:/work/engine")
        self.assertIsNone(stamp["sources"])
        self.assertTrue(any("does not have" in line for line in said))

    def test_a_network_failure_never_reaches_for_the_local_build(self):
        calls = []

        def pretend_pip(command, on_output):
            calls.append(command)
            return 1, "NewConnectionError: getaddrinfo failed"

        with TempHome(), tempfile.TemporaryDirectory() as temp, local_build(True), \
                public_route():
            install = fake_install(Path(temp))
            with mock.patch.object(runtime, "_run_pip", pretend_pip):
                with self.assertRaises(runtime.RuntimeProblem) as caught:
                    runtime.fill(install)
        self.assertEqual(len(calls), 1)
        self.assertIn("internet connection", caught.exception.message)

    def test_a_warm_start_off_a_local_build_says_so_every_time(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            build_slot(
                runtime.runtime_slot(install), install, source="C:/work/engine"
            )
            said = []
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(runtime, "kicad_install", lambda: install):
                result = runtime.ensure(report=said.append)
            self.assertEqual(result.state, "warm")
            self.assertEqual(len(said), 1)
            self.assertIn("local build", said[0])
            self.assertIn("C:/work/engine", said[0])
            _ = home

    def test_a_warm_start_off_the_server_says_nothing(self):
        with TempHome(), tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            build_slot(runtime.runtime_slot(install), install)
            said = []
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(runtime, "kicad_install", lambda: install):
                runtime.ensure(report=said.append)
            self.assertEqual(said, [])


class TheEngineStarterCondemnsABrokenRuntime(unittest.TestCase):
    """`run_engine.py` removes the record, so the next start repairs itself."""

    def setUp(self):
        self.engine = load_by_path(RUN_ENGINE_SOURCE, "run_engine_under_repair")

    def test_it_names_the_same_record_file_the_bootstrap_writes(self):
        self.assertEqual(self.engine.STAMP_NAME, runtime.STAMP_NAME)

    def test_the_record_goes_and_the_packages_stay(self):
        with tempfile.TemporaryDirectory() as temp:
            slot = Path(temp) / "slot"
            packages = slot / runtime.PACKAGES_NAME
            packages.mkdir(parents=True)
            (slot / runtime.STAMP_NAME).write_text("{}", encoding="utf-8")
            self.assertTrue(self.engine.condemn_runtime(str(packages)))
            self.assertFalse((slot / runtime.STAMP_NAME).exists())
            self.assertTrue(packages.is_dir())

    def test_the_next_start_reads_it_as_damaged(self):
        with TempHome(), tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            slot = build_slot(runtime.runtime_slot(install), install)
            self.engine.condemn_runtime(str(runtime.site_packages_of(slot)))
            self.assertIsNone(runtime.ready_slot(install))
            self.assertIn("record", runtime.damage(slot, install))

    def test_no_runtime_folder_leaves_nothing_to_condemn(self):
        self.assertFalse(self.engine.condemn_runtime(self.engine.NO_RUNTIME))

    def test_an_unwritable_place_is_not_a_crash(self):
        self.assertFalse(self.engine.condemn_runtime("C:/nowhere/at/all/sp"))

    def test_the_message_asks_for_nothing_and_shows_no_traceback(self):
        message = self.engine.incomplete_runtime(
            "C:/rt/sp", ImportError("no pydantic_core", name="pydantic_core")
        )
        self.assertIn("repairs itself", message)
        self.assertIn("nothing to do by hand", message)
        for banned in ("Delete", "Traceback", "console", "stderr"):
            self.assertNotIn(banned, message)


if __name__ == "__main__":
    unittest.main()
