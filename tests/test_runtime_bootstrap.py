"""THE SHARED RUNTIME BOOTSTRAP: what it promises, and what it may never do.

The subject is `plugins/runtime.py` in the plugin package - the module that
guarantees KiCad's own Python can import the engine. Both doors stand on it, so
the properties fenced here are the ones whose failure an engineer would meet as
"the plugin does nothing" with no way to find out why.

Nothing here touches the network, and two tests PROVE that rather than assume
it: `socket` and `subprocess` are replaced with functions that raise, and the
warm path has to succeed anyway. That is the same fence the module's own
measured warm cost was taken behind.

The module is loaded BY FILE PATH, because it lives in `plugins/`, whose
`__init__.py` imports `pcbnew` and `wx` and therefore cannot be imported
outside KiCad. It must register itself in `sys.modules` before it executes:
under `from __future__ import annotations`, `dataclasses` resolves a frozen
class's annotations through `sys.modules[cls.__module__]` and fails with an
`AttributeError` about `NoneType` when the module is not there - which is worth
knowing, because it is exactly what the KiCad door would hit if it loaded this
the same way.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = REPO_ROOT / "kicad_plugin" / "plugins" / "runtime.py"
ENGINE_PYPROJECT = REPO_ROOT / "pyproject.toml"


def load_runtime():
    """Load the bootstrap module from its file, under its own module name."""
    spec = importlib.util.spec_from_file_location(
        "eedesignpanels_runtime_under_test", RUNTIME_SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = load_runtime()


def fake_install(root: Path, version="10.0.4", tag="cp311"):
    """A KiCadInstall pointing at a folder a test made."""
    interpreter = root / "bin" / "python.exe"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"")
    return runtime.KiCadInstall(
        version=version, root=root, interpreter=interpreter, python_tag=tag
    )


def make_ready_runtime(home: Path, install, version=None):
    """Build a slot that looks like a finished fill, stamp and all."""
    slot = home / "runtime" / install.slot_name
    packages = slot / runtime.PACKAGES_NAME
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "eedesignpanels").mkdir(exist_ok=True)
    (packages / "eedesignpanels" / "__init__.py").write_text("", encoding="utf-8")
    stamp = {
        "schema": runtime.STAMP_SCHEMA,
        "distribution": runtime.ENGINE_DISTRIBUTION,
        "version": version or runtime.ENGINE_VERSION,
        "python_tag": install.python_tag,
        "kicad_version": install.version,
        "interpreter": str(install.interpreter),
    }
    (slot / runtime.STAMP_NAME).write_text(json.dumps(stamp), encoding="utf-8")
    return slot


class NoNetwork:
    """Replaces every route to a socket or a child process with a refusal.

    The one way to show the warm path is free is to take the network away and
    watch it work anyway.
    """

    def __init__(self, case):
        self.case = case
        self.patches = []

    def __enter__(self):
        def refuse(*args, **kwargs):
            raise AssertionError("this path reached the network or a process")

        for target in (
            "socket.socket",
            "socket.create_connection",
            "socket.getaddrinfo",
            "subprocess.Popen",
            "subprocess.run",
            "subprocess.check_output",
        ):
            patch = mock.patch(target, refuse)
            patch.start()
            self.patches.append(patch)
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


def public_route():
    """The public sources, whatever the machine running the tests has set.

    A blank wheel folder variable is an absent one, so this turns the offline
    route off for the duration and puts the variable back afterwards.
    """
    return mock.patch.dict(os.environ, {runtime.WHEEL_FOLDER_VARIABLE: ""})


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


# ---------------------------------------------------------------------------


class ModuleContractTest(unittest.TestCase):
    """The four things this module is not allowed to be."""

    @classmethod
    def setUpClass(cls):
        import ast

        cls.source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.imported = set()
        for node in ast.walk(cls.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    cls.imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                cls.imported.add(node.module.split(".")[0])

    def test_it_imports_neither_gui_nor_pcbnew_nor_the_engine(self):
        # The whole point: it runs before either door is open, and its job is
        # to make the engine importable - so it cannot need the engine to run.
        for forbidden in ("wx", "pcbnew", "eedesignpanels", "kicad_file_io",
                          "eedesigncore"):
            self.assertNotIn(forbidden, self.imported)

    def test_the_engine_is_never_imported_anywhere_in_the_source(self):
        # Not even inside a function. `importlib.metadata` answers the
        # "is it installed" question from metadata instead.
        self.assertNotIn("import eedesignpanels", self.source)
        self.assertIn("importlib.metadata", self.source)

    def test_it_is_standard_library_only(self):
        for name in self.imported:
            self.assertIn(
                name,
                sys.stdlib_module_names,
                f"{name} is not in the standard library",
            )

    def test_no_credential_is_read_or_named(self):
        # Both public sources allow anonymous download, so the module needs
        # no token and names none.
        for pattern in ("GITLAB_TOKEN", "PRIVATE-TOKEN", "PRIVATE_TOKEN"):
            self.assertNotIn(pattern, self.source)


class DeclaredVersionTest(unittest.TestCase):
    """The bootstrap's idea of the engine's version, against the engine's own.

    The module ships to engineers without the engine's `pyproject.toml` beside
    it, so the version has to be a literal there. This is the fence that keeps
    the literal true.
    """

    def setUp(self):
        self.pyproject = ENGINE_PYPROJECT.read_text(encoding="utf-8")

    def _field(self, key):
        for line in self.pyproject.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key} ="):
                return stripped.split("=", 1)[1].strip().strip('"')
        self.fail(f"no {key} in {ENGINE_PYPROJECT}")

    def test_the_distribution_name_matches_the_engine(self):
        self.assertEqual(runtime.ENGINE_DISTRIBUTION, self._field("name"))

    def test_the_version_matches_the_engine(self):
        self.assertEqual(runtime.ENGINE_VERSION, self._field("version"))

    def test_the_named_dependencies_are_the_engine_s_own(self):
        # ENGINE_NEEDS exists only to name what is missing in an error message,
        # so it has to name the real thing.
        for need in runtime.ENGINE_NEEDS:
            if need in (runtime.CORE_DISTRIBUTION, "pydantic"):
                self.assertIn(f'"{need}==', self.pyproject)

    def test_the_board_library_pin_is_the_engine_s_own(self):
        # pip refuses a fill whose two pins disagree, so this is caught at the
        # latest on an engineer's machine. Here it is caught first.
        self.assertIn(
            f'"{runtime.CORE_DISTRIBUTION}=={runtime.CORE_VERSION}"',
            self.pyproject,
        )

    def test_the_release_tag_is_v_plus_the_engine_version(self):
        # One repository, one version: the plugin installs the engine from the
        # tag of its own release, so the tag and the engine version are one
        # number.
        self.assertEqual(runtime.RELEASE_TAG, f"v{runtime.ENGINE_VERSION}")
        self.assertEqual(runtime.RELEASE_TAG, f"v{self._field('version')}")

    def test_the_declared_library_pin_is_the_one_the_suite_needs(self):
        """The pin the bootstrap's fill will resolve, against the real floor.

        The bootstrap names ONE requirement and lets the engine's metadata pull
        the rest, so this pin IS what an engineer's runtime gets. Measured:
        the old pin (0.11.2) filled a runtime that imported and
        then raised `ImportError: cannot import name 'inject_board'`. A failure
        that far downstream is invisible here unless the two numbers are held
        against each other.
        """
        from test_library_016 import MINIMUM_LIBRARY, version_tuple

        pinned = None
        for line in self.pyproject.splitlines():
            stripped = line.strip().strip(",").strip('"')
            if stripped.startswith("eedesigncore=="):
                pinned = stripped.split("==", 1)[1]
        self.assertIsNotNone(pinned, "the engine declares no eedesigncore pin")
        self.assertGreaterEqual(version_tuple(pinned), MINIMUM_LIBRARY)


class SlotTest(unittest.TestCase):
    """Where a runtime lands, and why that folder name."""

    def test_the_abi_tag_is_read_off_the_python_dll(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_dir = Path(temp)
            (bin_dir / "python311.dll").write_bytes(b"")
            self.assertEqual(runtime._python_tag(bin_dir), "cp311")

    def test_a_bare_python3_dll_is_not_an_abi_tag(self):
        # KiCad's bin folder holds both python3.dll and python311.dll; only the
        # second one carries the ABI, and the first must never be mistaken for
        # a tag of its own.
        with tempfile.TemporaryDirectory() as temp:
            bin_dir = Path(temp)
            (bin_dir / "python3.dll").write_bytes(b"")
            self.assertIsNone(runtime._python_tag(bin_dir))
            (bin_dir / "python311.dll").write_bytes(b"")
            self.assertEqual(runtime._python_tag(bin_dir), "cp311")

    def test_no_python_dll_means_no_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "bin" / "python.exe").write_bytes(b"")
            self.assertIsNone(runtime._install_at(root, "10.0.4"))

    def test_no_interpreter_means_no_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "bin" / "python311.dll").write_bytes(b"")
            self.assertIsNone(runtime._install_at(root, "10.0.4"))

    def test_the_slot_name_carries_the_kicad_version_and_the_abi(self):
        with tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            self.assertEqual(install.slot_name, "kicad-10.0.4-cp311")

    def test_the_stamp_sits_outside_the_tree_pip_owns(self):
        # pip owns site-packages, so the stamp cannot live in it or a
        # refill could overwrite the one thing that proves a fill finished.
        slot = Path("C:/anywhere/kicad-10.0.4-cp311")
        packages = runtime.site_packages_of(slot)
        stamp = slot / runtime.STAMP_NAME
        self.assertEqual(packages.parent, slot)
        self.assertEqual(stamp.parent, slot)
        self.assertNotIn(packages, stamp.parents)

    def test_versions_sort_newest_first(self):
        ordered = sorted(
            ["9.0.1", "10.0.4", "10.0.3", "10.0.10"],
            key=runtime.version_sort_key,
            reverse=True,
        )
        self.assertEqual(ordered, ["10.0.10", "10.0.4", "10.0.3", "9.0.1"])

    def test_a_non_numeric_version_does_not_raise(self):
        runtime.version_sort_key("10.0-rc1")
        runtime.version_sort_key("nightly")


class StampTest(unittest.TestCase):
    """The stamp is the only proof a fill finished."""

    def test_a_written_stamp_reads_back_and_matches(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp, \
                public_route():
            install = fake_install(Path(temp))
            slot = runtime.runtime_slot(install)
            packages = runtime.site_packages_of(slot)
            packages.mkdir(parents=True)
            (packages / "a.py").write_text("x", encoding="utf-8")
            written = runtime._write_stamp(slot, install, None)
            self.assertEqual(written["files"], 1)
            self.assertEqual(written["bytes"], 1)
            # EVERY SOURCE IS RECORDED. One of two is half an
            # answer to "where did this runtime come from". A fill from the
            # public sources names both archives and no local build.
            self.assertEqual(written["sources"], list(runtime.PUBLIC_SOURCES))
            self.assertIsNone(written["source"])
            self.assertNotIn("index_url", written)
            self.assertTrue(runtime.stamp_matches(runtime.read_stamp(slot), install))
            self.assertTrue(runtime.runtime_is_ready(install))
            _ = home

    def test_every_field_is_a_reason_to_refill(self):
        with tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            good = {
                "schema": runtime.STAMP_SCHEMA,
                "distribution": runtime.ENGINE_DISTRIBUTION,
                "version": runtime.ENGINE_VERSION,
                "python_tag": install.python_tag,
                "interpreter": str(install.interpreter),
            }
            self.assertTrue(runtime.stamp_matches(good, install))
            for key, wrong in (
                ("schema", "something-else/9"),
                ("distribution", "not-the-engine"),
                ("version", "99.0.0"),
                ("python_tag", "cp312"),
                ("interpreter", "C:/elsewhere/python.exe"),
            ):
                spoiled = dict(good)
                spoiled[key] = wrong
                self.assertFalse(
                    runtime.stamp_matches(spoiled, install), f"{key} was ignored"
                )
                missing = dict(good)
                del missing[key]
                self.assertFalse(
                    runtime.stamp_matches(missing, install), f"{key} may be absent"
                )

    def test_nothing_and_rubbish_both_count_as_no_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            self.assertFalse(runtime.stamp_matches(None, install))
            self.assertFalse(runtime.stamp_matches({}, install))
            slot = Path(temp) / "slot"
            slot.mkdir()
            self.assertIsNone(runtime.read_stamp(slot))
            (slot / runtime.STAMP_NAME).write_text("{not json", encoding="utf-8")
            self.assertIsNone(runtime.read_stamp(slot))
            (slot / runtime.STAMP_NAME).write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(runtime.read_stamp(slot))

    def test_a_half_built_runtime_is_not_ready(self):
        # Packages present, stamp absent: the fill died partway. It has to read
        # as "not built", never as "built".
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            runtime.site_packages_of(runtime.runtime_slot(install)).mkdir(
                parents=True
            )
            self.assertFalse(runtime.runtime_is_ready(install))
            _ = home

    def test_a_stamp_with_no_packages_is_not_ready(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            slot = runtime.runtime_slot(install)
            slot.mkdir(parents=True)
            (slot / runtime.STAMP_NAME).write_text(
                json.dumps(
                    {
                        "schema": runtime.STAMP_SCHEMA,
                        "distribution": runtime.ENGINE_DISTRIBUTION,
                        "version": runtime.ENGINE_VERSION,
                        "python_tag": install.python_tag,
                        "interpreter": str(install.interpreter),
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(runtime.runtime_is_ready(install))
            _ = home


class WarmPathTest(unittest.TestCase):
    """The path every run after the first one takes. It must cost nothing."""

    def test_the_warm_path_reaches_no_network_and_starts_no_process(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            make_ready_runtime(home.home, install)
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(runtime, "kicad_install", lambda: install):
                with NoNetwork(self):
                    result = runtime.ensure()
            self.assertEqual(result.state, "warm")
            self.assertEqual(
                result.site_packages,
                runtime.site_packages_of(runtime.runtime_slot(install)),
            )
            self.assertEqual(result.version, runtime.ENGINE_VERSION)

    def test_a_moved_engine_version_replaces_the_runtime(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            make_ready_runtime(home.home, install, version="0.0.1")
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(
                runtime, "kicad_install", lambda: install
            ), mock.patch.object(runtime, "fill") as filled:
                filled.return_value = "refilled"
                self.assertEqual(runtime.ensure(), "refilled")
            filled.assert_called_once()

    def test_no_runtime_at_all_fills(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(
                runtime, "kicad_install", lambda: install
            ), mock.patch.object(runtime, "fill") as filled:
                runtime.ensure()
            filled.assert_called_once()
            _ = home


class AmbientTest(unittest.TestCase):
    """An interpreter that already has the engine is used as it stands."""

    def test_an_installed_engine_short_circuits_everything(self):
        def no_kicad():
            raise AssertionError("KiCad was resolved when it was not needed")

        with mock.patch.object(
            runtime, "installed_engine_version", lambda: runtime.ENGINE_VERSION
        ), mock.patch.object(runtime, "kicad_install", no_kicad):
            with NoNetwork(self):
                result = runtime.ensure()
        self.assertEqual(result.state, "ambient")
        self.assertTrue(result.ambient)
        self.assertIsNone(result.site_packages)

    def test_a_different_ambient_version_is_used_and_said_out_loud(self):
        said = []
        with mock.patch.object(
            runtime, "installed_engine_version", lambda: "9.9.9"
        ):
            result = runtime.ensure(report=said.append)
        self.assertEqual(result.state, "ambient")
        self.assertEqual(result.version, "9.9.9")
        self.assertEqual(len(said), 1)
        self.assertIn("9.9.9", said[0])
        self.assertIn(runtime.ENGINE_VERSION, said[0])


class PipCommandTest(unittest.TestCase):
    """The one network call this module ever makes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.install = fake_install(Path(self.temp.name))
        self.slot = Path(self.temp.name) / "slot"
        route = public_route()
        route.start()
        self.addCleanup(route.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_it_runs_kicad_s_own_python_against_the_public_sources(self):
        command = runtime._pip_command(self.install, self.slot, None)
        self.assertEqual(command[0], str(self.install.interpreter))
        self.assertEqual(command[1:4], ["-m", "pip", "install"])
        self.assertEqual(
            command[command.index("--target") + 1],
            str(runtime.site_packages_of(self.slot)),
        )
        # The two pinned archives, the board library first, each named by
        # its distribution so pip checks the name it builds.
        self.assertEqual(
            command[-2:],
            [
                f"{runtime.CORE_DISTRIBUTION} @ {runtime.CORE_SOURCE_URL}",
                f"{runtime.ENGINE_DISTRIBUTION} @ {runtime.ENGINE_SOURCE_URL}",
            ],
        )

    def test_ready_built_packages_only(self):
        # An engineer's machine has no C compiler, so a source build would
        # spend a minute failing and then pull in a build backend nobody asked
        # for. A missing wheel has to be said, not worked around.
        command = runtime._pip_command(self.install, self.slot, None)
        self.assertEqual(
            command[command.index("--only-binary") + 1], ":all:"
        )

    def test_both_sources_are_pinned_tags_and_neither_carries_a_token(self):
        """Each source is one archive at one fixed release tag.

        A plugin release names exactly what it installs: the board library at
        its own tag, and the engine at the tag of this release. The engine is
        the Python package at the root of its repository, so the archive needs
        no subfolder. No address carries a user name or a token.
        """
        from urllib.parse import urlparse

        self.assertEqual(
            runtime.PUBLIC_SOURCES,
            (runtime.CORE_SOURCE_URL, runtime.ENGINE_SOURCE_URL),
        )
        self.assertEqual(
            runtime.ENGINE_SOURCE_URL,
            "https://github.com/eedesignpro/eedesignpanels/archive/refs/tags/"
            f"{runtime.RELEASE_TAG}.zip",
        )
        self.assertEqual(
            runtime.CORE_SOURCE_URL,
            "https://github.com/eedesignpro/eedesigncore/archive/refs/tags/"
            f"v{runtime.CORE_VERSION}.zip",
        )
        for url in runtime.PUBLIC_SOURCES:
            parts = urlparse(url)
            self.assertEqual(parts.scheme, "https")
            self.assertIsNone(parts.username)
            self.assertIsNone(parts.password)
            self.assertEqual(parts.fragment, "")
        command = runtime._pip_command(self.install, self.slot, None)
        self.assertNotIn("--trusted-host", command)
        self.assertNotIn("--find-links", command)
        # The two pure-Python packages are built from their archives; every
        # other package stays a ready-built wheel.
        self.assertEqual(
            command[command.index("--no-binary") + 1],
            f"{runtime.CORE_DISTRIBUTION},{runtime.ENGINE_DISTRIBUTION}",
        )

    def test_no_package_index_is_named_so_pip_keeps_its_own_setting(self):
        # The third-party wheels come from the index pip is set up to use,
        # PyPI by default, so a user's own mirror setting is kept. The fill
        # names no index and turns none off.
        command = runtime._pip_command(self.install, self.slot, None)
        for flag in ("--index-url", "--extra-index-url", "--no-index", "-i"):
            self.assertNotIn(flag, command)

    def test_a_wheel_folder_replaces_every_network_source(self):
        # The offline route: one folder, no index, both packages pinned by
        # version because a folder holds wheels and not archives.
        folder = Path(self.temp.name) / "wheels"
        with mock.patch.dict(
            os.environ, {runtime.WHEEL_FOLDER_VARIABLE: str(folder)}
        ):
            command = runtime._pip_command(self.install, self.slot, None)
            self.assertEqual(runtime.fill_sources(), (f"the wheel folder {folder}",))
        self.assertIn("--no-index", command)
        self.assertEqual(command[command.index("--find-links") + 1], str(folder))
        self.assertEqual(
            command[-2:],
            [
                f"{runtime.CORE_DISTRIBUTION}=={runtime.CORE_VERSION}",
                f"{runtime.ENGINE_DISTRIBUTION}=={runtime.ENGINE_VERSION}",
            ],
        )
        for source in runtime.PUBLIC_SOURCES:
            self.assertNotIn(source, " ".join(command))

    def test_it_never_waits_minutes_to_find_out_the_network_is_off(self):
        command = runtime._pip_command(self.install, self.slot, None)
        self.assertEqual(
            command[command.index("--timeout") + 1], runtime.PIP_TIMEOUT_SECONDS
        )
        self.assertEqual(
            command[command.index("--retries") + 1], runtime.PIP_RETRIES
        )
        self.assertIn("--no-input", command)

    def test_a_local_source_replaces_the_requirement_and_allows_a_build(self):
        # The development route. Neither door passes it, so nothing an
        # engineer installs has anything to configure.
        command = runtime._pip_command(self.install, self.slot, "C:/work/engine")
        self.assertEqual(command[-1], "C:/work/engine")
        self.assertNotIn("--only-binary", command)
        self.assertNotIn(
            f"{runtime.ENGINE_DISTRIBUTION}=={runtime.ENGINE_VERSION}", command
        )


class FailurePathTest(unittest.TestCase):
    """What a fill that fails says, and what it leaves behind."""

    def setUp(self):
        # The home outlives each test so the assertions can read the log file
        # the failure left behind.
        self.home = TempHome().__enter__()
        self.addCleanup(self.home.__exit__, None, None, None)
        route = public_route()
        route.start()
        self.addCleanup(route.stop)

    def _fill_with(self, code, transcript):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = fake_install(Path(temp.name))
        with mock.patch.object(
            runtime, "_run_pip", lambda *a, **k: (code, transcript)
        ):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime.fill(install)
        return caught.exception, runtime.runtime_slot(install), self.home.home

    def test_no_network_names_the_connection_the_wheel_folder_and_what_is_missing(self):
        problem, slot, _ = self._fill_with(
            1,
            "WARNING: Retrying ... NewConnectionError: Failed to establish a "
            "new connection: [Errno 11001] getaddrinfo failed",
        )
        self.assertIn("internet connection", problem.message)
        self.assertIn("github.com", problem.message)
        self.assertIn(runtime.WHEEL_FOLDER_VARIABLE, problem.message)
        for need in runtime.ENGINE_NEEDS:
            self.assertIn(need, problem.message)
        self.assertFalse(slot.exists(), "a failed fill left its slot behind")

    def test_a_read_timeout_is_also_a_network_answer(self):
        problem, _, _ = self._fill_with(1, "ERROR: ... Read timed out.")
        self.assertIn("internet connection", problem.message)

    def test_a_missing_package_is_not_blamed_on_the_network(self):
        problem, slot, _ = self._fill_with(
            1,
            "ERROR: Could not find a version that satisfies the requirement "
            "eedesignpanels==0.1.0\nERROR: No matching distribution "
            "found for eedesignpanels==0.1.0",
        )
        self.assertNotIn("internet connection", problem.message)
        self.assertIn(runtime.ENGINE_VERSION, problem.message)
        self.assertIn(runtime.CORE_VERSION, problem.message)
        # The sources are the answer to "why did it not find it", so the
        # message names every one of them.
        for source in runtime.PUBLIC_SOURCES:
            self.assertIn(source, problem.message)
        self.assertFalse(slot.exists())

    def test_an_unexplained_failure_points_at_the_log(self):
        problem, slot, home = self._fill_with(1, "ERROR: something odd")
        self.assertIn(str(home / runtime.FILL_LOG_NAME), problem.message)
        self.assertTrue((home / runtime.FILL_LOG_NAME).is_file())
        self.assertFalse(slot.exists())

    def test_the_transcript_is_kept_in_a_file_and_not_in_the_message(self):
        problem, _, home = self._fill_with(
            1, "ERROR: something odd\nline two\nline three"
        )
        self.assertNotIn("line three", problem.message)
        kept = (home / runtime.FILL_LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("line three", kept)

    def test_no_message_is_ever_a_traceback(self):
        for transcript in (
            "NewConnectionError",
            "No matching distribution found",
            "ERROR: something odd",
        ):
            problem, _, _ = self._fill_with(1, transcript)
            for jargon in ("Traceback", "Exception", "stderr", "--target"):
                self.assertNotIn(jargon, problem.message)

    def test_a_missing_interpreter_names_the_path_windows_gave(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = fake_install(Path(temp.name))

        def cannot_start(*args, **kwargs):
            raise OSError(2, "The system cannot find the file specified")

        with mock.patch.object(runtime, "_run_pip", cannot_start):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime.fill(install)
        self.assertFalse(runtime.runtime_slot(install).exists())
        self.assertIn(str(install.interpreter), caught.exception.message)
        self.assertNotIn("Traceback", caught.exception.message)

    def test_a_fill_that_never_returns_is_a_network_answer(self):
        def too_slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="pip", timeout=1)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = fake_install(Path(temp.name))
        with mock.patch.object(runtime, "_run_pip", too_slow):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime.fill(install)
        self.assertFalse(runtime.runtime_slot(install).exists())
        self.assertIn("internet connection", caught.exception.message)


class SuccessfulFillTest(unittest.TestCase):
    """A fill that works, without pip."""

    def test_it_stamps_last_and_reports_the_size(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = fake_install(Path(temp.name))
        said = []

        def pretend_pip(command, on_output):
            target = Path(command[command.index("--target") + 1])
            (target / "eedesignpanels").mkdir(parents=True, exist_ok=True)
            (target / "eedesignpanels" / "__init__.py").write_text(
                "x" * 100, encoding="utf-8"
            )
            return 0, "Successfully installed"

        with TempHome() as home, public_route():
            with mock.patch.object(runtime, "_run_pip", pretend_pip):
                result = runtime.fill(install, report=said.append)
            self.assertEqual(result.state, "filled")
            self.assertTrue(runtime.runtime_is_ready(install))
            stamp = runtime.read_stamp(runtime.runtime_slot(install))
            self.assertEqual(stamp["files"], 1)
            self.assertEqual(stamp["bytes"], 100)
            self.assertEqual(stamp["kicad_version"], install.version)
            _ = home
        self.assertEqual(len(said), 2)
        self.assertIn("internet connection", said[0])
        self.assertIn("MB", said[1])

    def test_a_refill_starts_from_nothing(self):
        # A stale tree must not be merged into: pip --target leaves whatever
        # was already there, and a package the new pin drops would survive.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = fake_install(Path(temp.name))

        def pretend_pip(command, on_output):
            target = Path(command[command.index("--target") + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "new.py").write_text("x", encoding="utf-8")
            return 0, ""

        with TempHome() as home:
            stale = runtime.site_packages_of(runtime.runtime_slot(install))
            stale.mkdir(parents=True)
            (stale / "left_over.py").write_text("x", encoding="utf-8")
            with mock.patch.object(runtime, "_run_pip", pretend_pip):
                runtime.fill(install)
            self.assertFalse((stale / "left_over.py").exists())
            self.assertTrue((stale / "new.py").exists())
            _ = home


class ActivateTest(unittest.TestCase):
    """Putting the runtime on this interpreter's import path."""

    def setUp(self):
        self.saved_path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.saved_path

    def test_it_goes_on_the_front_of_the_path_once(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
            install = fake_install(Path(temp), tag=tag)
            make_ready_runtime(home.home, install)
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(runtime, "kicad_install", lambda: install):
                with NoNetwork(self):
                    first = runtime.activate()
                    runtime.activate()
            entry = str(runtime.site_packages_of(runtime.runtime_slot(install)))
            self.assertEqual(sys.path[0], entry)
            self.assertEqual(sys.path.count(entry), 1)
            self.assertEqual(first.state, "warm")

    def test_a_runtime_for_another_python_is_refused_in_plain_words(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp), tag="cp399")
            make_ready_runtime(home.home, install)
            with mock.patch.object(
                runtime, "installed_engine_version", lambda: None
            ), mock.patch.object(runtime, "kicad_install", lambda: install):
                with self.assertRaises(runtime.RuntimeProblem) as caught:
                    runtime.activate()
            self.assertEqual(sys.path, self.saved_path)
            self.assertIn(str(install.interpreter), caught.exception.message)
            self.assertNotIn("cp399", caught.exception.message)
            self.assertNotIn("Traceback", caught.exception.message)

    def test_an_ambient_engine_leaves_the_path_alone(self):
        with mock.patch.object(
            runtime, "installed_engine_version", lambda: runtime.ENGINE_VERSION
        ):
            runtime.activate()
        self.assertEqual(sys.path, self.saved_path)


class LaunchEnvironmentTest(unittest.TestCase):
    """What the standalone launcher hands a subprocess."""

    def test_the_runtime_goes_on_the_front_of_pythonpath(self):
        result = runtime.Runtime(
            interpreter=Path("C:/k/python.exe"),
            site_packages=Path("C:/runtime/site-packages"),
            version=runtime.ENGINE_VERSION,
            state="warm",
        )
        env = runtime.launch_environment(result, base={"PYTHONPATH": "C:/other"})
        self.assertEqual(
            env["PYTHONPATH"],
            str(Path("C:/runtime/site-packages")) + os.pathsep + "C:/other",
        )

    def test_an_empty_pythonpath_gains_no_separator(self):
        result = runtime.Runtime(
            interpreter=Path("C:/k/python.exe"),
            site_packages=Path("C:/runtime/site-packages"),
            version=runtime.ENGINE_VERSION,
            state="warm",
        )
        env = runtime.launch_environment(result, base={})
        self.assertEqual(env["PYTHONPATH"], str(Path("C:/runtime/site-packages")))

    def test_an_ambient_engine_adds_nothing(self):
        result = runtime.Runtime(
            interpreter=Path(sys.executable),
            site_packages=None,
            version=runtime.ENGINE_VERSION,
            state="ambient",
        )
        env = runtime.launch_environment(result, base={"PATH": "x"})
        self.assertNotIn("PYTHONPATH", env)


class RemoveAndDescribeTest(unittest.TestCase):
    """The two things a person runs by hand."""

    def test_remove_takes_every_slot_and_names_them(self):
        with TempHome() as home, tempfile.TemporaryDirectory() as temp:
            install = fake_install(Path(temp))
            slot = make_ready_runtime(home.home, install)
            gone = runtime.remove()
            self.assertEqual(gone, [slot])
            self.assertFalse(slot.exists())
            self.assertEqual(runtime.remove(), [])

    def test_describe_names_the_folder_and_reaches_no_network(self):
        with TempHome() as home:
            with NoNetwork(self):
                text = runtime.describe()
        self.assertIn(str(home.home), text)
        self.assertIn(runtime.ENGINE_DISTRIBUTION, text)

    def test_status_is_the_default_and_says_nothing_on_stderr(self):
        import io
        import contextlib

        out = io.StringIO()
        with TempHome(), NoNetwork(self):
            with contextlib.redirect_stdout(out):
                code = runtime.main([])
        self.assertEqual(code, 0)
        self.assertIn("runtime folder", out.getvalue())

    def test_a_problem_is_one_paragraph_on_stderr_and_a_failure_code(self):
        import io
        import contextlib

        def refuse(*args, **kwargs):
            raise runtime.RuntimeProblem("Check the internet connection.")

        err = io.StringIO()
        with mock.patch.object(runtime, "ensure", refuse):
            with contextlib.redirect_stderr(err):
                code = runtime.main(["--fill"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().strip(), "Check the internet connection.")


class ResolutionTest(unittest.TestCase):
    """Finding KiCad: the running process, then what Windows lists."""

    def test_a_folder_with_no_kicad_cli_is_not_a_kicad_bin_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "bin" / "python.exe"
            fake.parent.mkdir(parents=True)
            fake.write_bytes(b"")
            with mock.patch.object(runtime.sys, "executable", str(fake)):
                self.assertIsNone(runtime._install_from_running_process())

    def test_the_running_process_wins_over_the_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            here = fake_install(Path(temp) / "here", version="10.0.4")
            (here.root / "bin" / "python311.dll").write_bytes(b"")
            other = Path(temp) / "other"
            (other / "bin").mkdir(parents=True)
            (other / "bin" / "python.exe").write_bytes(b"")
            (other / "bin" / "python311.dll").write_bytes(b"")
            with mock.patch.object(
                runtime, "_install_from_running_process", lambda: here
            ), mock.patch.object(
                runtime, "_registry_installs", lambda: [("99.9.9", other)]
            ):
                installs = runtime.kicad_installs()
            self.assertEqual(installs[0].root, here.root)
            self.assertEqual(len(installs), 2)

    def test_no_kicad_anywhere_is_said_in_plain_words(self):
        with mock.patch.object(runtime, "kicad_installs", lambda: []):
            with self.assertRaises(runtime.RuntimeProblem) as caught:
                runtime.kicad_install()
        message = caught.exception.message
        self.assertIn("KiCad", message)
        self.assertNotIn("registry", message.lower())
        self.assertNotIn("Traceback", message)

    @unittest.skipUnless(os.name == "nt", "the registry is a Windows thing")
    def test_windows_really_does_know_where_kicad_is(self):
        # Measured, not asserted against a number: this machine has KiCad
        # installed, so the registry route has to find at least one install and
        # every one it finds has to have a real interpreter beside it.
        installs = runtime.kicad_installs()
        self.assertTrue(installs, "no KiCad install was resolved on this machine")
        for install in installs:
            self.assertTrue(install.interpreter.is_file())
            self.assertTrue(install.python_tag.startswith("cp"))
            self.assertNotIn("unknown", install.slot_name)

    @unittest.skipUnless(os.name == "nt", "the registry is a Windows thing")
    def test_the_runtime_folder_is_under_local_application_data(self):
        home = runtime.runtime_home()
        self.assertEqual(home.name, runtime.PRODUCT_FOLDER)
        self.assertEqual(home.parent.name, runtime.VENDOR_FOLDER)
        self.assertIn("Local", str(home))
        # Never in the project, never in the plugin folder, never in
        # Program Files.
        self.assertNotIn("Program Files", str(home))
        self.assertNotIn("KiCad", str(home))


class NoNetworkFenceTest(unittest.TestCase):
    """The fence the other tests lean on has to actually bite."""

    def test_the_no_network_guard_refuses_a_socket(self):
        with NoNetwork(self):
            with self.assertRaises(AssertionError):
                socket.socket()
            with self.assertRaises(AssertionError):
                subprocess.Popen(["cmd"])


if __name__ == "__main__":
    unittest.main()
