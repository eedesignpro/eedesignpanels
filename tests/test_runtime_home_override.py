"""THE RUNTIME FOLDER CAN BE REDIRECTED BY ONE NAMED VARIABLE, FOR A CHECK.

The runtime module asks the Windows shell for the local application data
folder instead of reading %LOCALAPPDATA%, because a redirected folder makes
the variable and the real folder two different places, and writing to the
wrong one fails silently. So a check that points %LOCALAPPDATA% at a
throwaway folder does not move the runtime: it reads the real runtime of
whoever runs it and proves nothing.

WHAT THIS FILE FENCES. One named variable, `EEDESIGNPANELS_RUNTIME_HOME`, in
the same spirit as `EEDESIGNPANELS_SCRATCH`: a check or a test says where the
runtime goes, by name, and nothing the plugin catalogue installs sets it. Both
halves are asserted here, and the second half is the one that matters:

* WITH the variable, every path the module owns moves together, and the status
  readout SAYS the run was redirected.
* WITHOUT it, the shell answers. Rewriting %LOCALAPPDATA% on Windows changes
  nothing.

Nothing here reaches the network, starts pip or raises a window.
"""

import os
import subprocess  # noqa: S404 - one fixed interpreter call, no shell
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_runtime_repair import (  # noqa: E402  (after the path insert)
    RUNTIME_SOURCE,
    fake_install,
    runtime,
)

PLUGIN_DIR = RUNTIME_SOURCE.parents[1]


def redirected(folder):
    """Set the override for the duration of a test, as a check does."""
    return mock.patch.dict(
        os.environ, {runtime.RUNTIME_HOME_VARIABLE: str(folder)}, clear=False
    )


def not_redirected():
    """Clear the override, so the shell answers - a real run's state."""
    return mock.patch.dict(
        os.environ, {runtime.RUNTIME_HOME_VARIABLE: ""}, clear=False
    )


class TheOverrideMovesEveryPathTogether(unittest.TestCase):
    """One function answers, so a redirected run cannot half happen."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "throwaway"

    def test_the_runtime_folder_is_exactly_what_was_named(self):
        with redirected(self.folder):
            self.assertEqual(runtime.runtime_home(), self.folder)
            self.assertEqual(runtime.runtime_home_override(), self.folder)

    def test_the_slot_the_stamp_and_the_log_all_follow_it(self):
        install = fake_install(Path(self.temp.name) / "kicad")
        with redirected(self.folder):
            slot = runtime.runtime_slot(install)
            self.assertTrue(
                str(slot).startswith(str(self.folder)), f"{slot} left the folder"
            )
            self.assertTrue(
                str(runtime.site_packages_of(slot)).startswith(str(self.folder))
            )
            self.assertEqual(
                runtime.fill_log_path(), self.folder / runtime.FILL_LOG_NAME
            )

    def test_a_blank_value_is_an_absent_value(self):
        # Emptying the variable is how a caller turns the redirection off, so
        # a blank value reads as no override, not as a folder named "".
        for blank in ("", "   ", "\t"):
            with mock.patch.dict(
                os.environ, {runtime.RUNTIME_HOME_VARIABLE: blank}, clear=False
            ):
                self.assertIsNone(runtime.runtime_home_override())
                self.assertNotEqual(runtime.runtime_home(), Path(""))

    def test_it_is_read_in_exactly_one_place(self):
        # One reader, so the status readout can never
        # disagree with what a door does a second later.
        source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(source.count("RUNTIME_HOME_VARIABLE = "), 1)
        self.assertEqual(source.count("os.environ.get(RUNTIME_HOME_VARIABLE"), 1)


class ARealRunStillResolvesThroughTheShell(unittest.TestCase):
    """With no override, the folder comes from the Windows shell."""

    def test_with_no_override_the_folder_is_the_shell_answer(self):
        with not_redirected():
            self.assertIsNone(runtime.runtime_home_override())
            self.assertEqual(
                runtime.runtime_home(),
                runtime.local_app_data()
                / runtime.VENDOR_FOLDER
                / runtime.PRODUCT_FOLDER,
            )

    @unittest.skipUnless(os.name == "nt", "the shell call is Windows only")
    def test_rewriting_local_app_data_changes_nothing_on_windows(self):
        # Rewriting %LOCALAPPDATA% does not move the runtime. The named
        # variable is the only way to redirect it.
        with tempfile.TemporaryDirectory() as temp:
            with not_redirected(), mock.patch.dict(
                os.environ, {"LOCALAPPDATA": temp}, clear=False
            ):
                self.assertFalse(
                    str(runtime.runtime_home()).startswith(temp),
                    "%LOCALAPPDATA% overrode the shell's answer",
                )

    def test_the_environment_variable_is_still_the_fallback_off_windows(self):
        # The variable is the fallback when the shell call fails or the
        # platform is not Windows, which today means a test host.
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(runtime, "_known_folder", lambda guid: None):
                with mock.patch.dict(
                    os.environ, {"LOCALAPPDATA": temp}, clear=False
                ):
                    self.assertEqual(runtime.local_app_data(), Path(temp))


class TheReadoutSaysWhenItIsRedirected(unittest.TestCase):
    """The status readout says when the runtime folder is redirected."""

    def test_it_names_the_variable_and_the_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            with redirected(temp):
                readout = runtime.describe()
        self.assertIn(temp, readout)
        self.assertIn("redirected by", readout)
        self.assertIn(runtime.RUNTIME_HOME_VARIABLE, readout)

    def test_a_real_run_says_nothing_about_redirection(self):
        with not_redirected():
            self.assertNotIn("redirected by", runtime.describe())


class NothingAnEngineerInstallsSetsIt(unittest.TestCase):
    """Only a check or a test sets the override. No shipped file does."""

    def test_no_shipped_file_assigns_the_variable(self):
        assignment = f'"{runtime.RUNTIME_HOME_VARIABLE}"'
        for path in sorted((PLUGIN_DIR / "plugins").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if assignment not in text:
                continue
            self.assertEqual(
                path.name,
                "runtime.py",
                f"{path.name} carries the runtime folder override",
            )
        for path in sorted((PLUGIN_DIR / "launcher").rglob("*.py")):
            self.assertNotIn(
                runtime.RUNTIME_HOME_VARIABLE,
                path.read_text(encoding="utf-8"),
                f"{path.name} carries the runtime folder override",
            )


RELEASE_CHECK = PLUGIN_DIR / "tools" / "check_published_plugin.py"


@unittest.skipUnless(
    RELEASE_CHECK.is_file(),
    "the release check for the internal plugin catalogue is not in this copy",
)
class TheCheckUsesTheDoorItDocuments(unittest.TestCase):
    """The release check and the module have to agree on the name."""

    def test_the_release_check_names_the_same_variable(self):
        check = RELEASE_CHECK.read_text(encoding="utf-8")
        self.assertIn(f'"{runtime.RUNTIME_HOME_VARIABLE}"', check)
        # Rewriting %LOCALAPPDATA% does not move the runtime on Windows, so
        # a check that did it would test the wrong folder.
        self.assertNotIn('"LOCALAPPDATA"', check)

    def test_the_check_proves_three_things(self):
        check = RELEASE_CHECK.read_text(encoding="utf-8")
        for proof in (
            "archive_matches_its_fingerprint",
            "fills_from_the_servers_alone",
            "the_fallback_is_fenced",
            "redirection_confirmed",
        ):
            self.assertIn(f"def {proof}", check)


class TheRedirectionSurvivesASubprocess(unittest.TestCase):
    """As the release check uses it: another interpreter, its own environment.

    The check reads this status line to decide whether it is
    safe to run a fill, so the line is part of the contract and is measured
    here rather than assumed. `--status` reaches no network and starts no fill.
    """

    def test_the_status_readout_reports_the_folder_it_was_given(self):
        with tempfile.TemporaryDirectory() as temp:
            environment = dict(os.environ)
            environment[runtime.RUNTIME_HOME_VARIABLE] = temp
            environment.pop(runtime.LOCAL_BUILD_VARIABLE, None)
            finished = subprocess.run(  # noqa: S603 - fixed interpreter, no shell
                [sys.executable, str(RUNTIME_SOURCE), "--status"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            folder = None
            for line in finished.stdout.splitlines():
                if line.startswith("runtime folder"):
                    folder = line.split(maxsplit=2)[-1].strip()
            self.assertIsNotNone(folder, finished.stdout)
            self.assertEqual(Path(folder), Path(temp))
            self.assertIn(runtime.RUNTIME_HOME_VARIABLE, finished.stdout)


if __name__ == "__main__":
    unittest.main()
