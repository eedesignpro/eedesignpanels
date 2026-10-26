"""THE PUBLISHED PATH FILLS A RUNTIME ON ITS OWN, WITH NO LOCAL BUILD BEHIND IT.

An earlier release named a wrong package source, and nobody saw it before a
user did. The first run ended with pip's own sentence: "Could not find a
version that satisfies the requirement".

WHY NOBODY CAUGHT IT, which matters more than the address. The local-build
fallback stood in for the missing package on both machines the release was
tested on, because both have this repository on disk. It announced itself in
one line and that line was not read as a warning. A FALLBACK THAT HIDES THE
FAILURE OF THE REAL PATH IS WORSE THAN NO FALLBACK.

So this file does two things, and the second one is the point:

1. It fences the fallback as DEVELOPER OPT-IN. Nothing a user installs from
   the package file can reach it, and a machine that has not asked for it
   gets a plain failure naming the sources that were asked.

2. It fills a THROWAWAY runtime with the fallback FORBIDDEN, so the published
   path is the only path available, and asserts it succeeds. Measured, not
   mocked: KiCad's own bundled Python and real pip, into a temporary folder
   that is deleted afterwards. The path is the one this machine is set up for:
   the two public release archives, or the offline wheel folder when
   `EEDESIGNPANELS_WHEEL_FOLDER` names one. A wrong source in that module
   fails this test in seconds instead of on somebody's desk.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runner_tags import runs_real_kicad  # noqa: E402  (after the path insert)
from test_runtime_repair import (  # noqa: E402  (after the path insert)
    TempHome,
    fake_install,
    local_build,
    runtime,
)


def public_route():
    """The public sources, whatever the machine running the tests has set."""
    return mock.patch.dict(os.environ, {runtime.WHEEL_FOLDER_VARIABLE: ""})

# What a filled runtime has to carry. The engine alone is not
# proof - the engine imports the board reader on its first line of real work,
# and the board reader needs compiled wheels that come from a different source
# than the two archives. All four, or the fill did not do its job.
EXPECTED_PACKAGES = ("eedesignpanels", "eedesigncore", "pydantic", "zstandard")


class TheLocalBuildIsOptIn(unittest.TestCase):
    """The local build is used only when a developer turns it on by name."""

    def test_it_is_off_unless_a_developer_sets_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(runtime.local_build_allowed())
        for absent in ("", "   ", "0", "no", "off", "false", "maybe"):
            self.assertFalse(
                runtime.local_build_allowed({runtime.LOCAL_BUILD_VARIABLE: absent}),
                f"{absent!r} was read as a yes",
            )

    def test_a_developer_can_ask_for_it_by_name(self):
        # A developer turns it on per machine, by setting the variable.
        for yes in ("1", "true", "TRUE", "yes", "On"):
            self.assertTrue(
                runtime.local_build_allowed({runtime.LOCAL_BUILD_VARIABLE: yes}),
                f"{yes!r} was read as a no",
            )

    def test_the_variable_is_the_only_way_in(self):
        # Only an environment variable turns it on. A config
        # key would put the choice inside a file a board author can hand over,
        # and a `fill` parameter would let a caller turn it on by accident.
        import inspect

        source = Path(runtime.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("LOCAL_BUILD_VARIABLE = "), 1)
        # The variable is read in exactly one place, and that place is the one
        # function both the fill and the status readout ask.
        self.assertEqual(source.count("env.get(LOCAL_BUILD_VARIABLE"), 1)
        self.assertIn(
            "LOCAL_BUILD_VARIABLE",
            inspect.getsource(runtime.local_build_allowed),
        )
        for function in (runtime.fill, runtime.ensure):
            self.assertNotIn(
                "local", inspect.signature(function).parameters,
                f"{function.__name__} takes the choice as an argument",
            )

    def test_a_machine_that_has_not_asked_never_reaches_the_local_build(self):
        """THE FENCE. The published path failing is a failure, not a detour."""
        calls = []

        def pretend_pip(command, on_output):
            calls.append(command)
            return 1, "ERROR: Could not find a version that satisfies the requirement"

        def never(*args, **kwargs):
            raise AssertionError("the local build ran without being asked for")

        with TempHome(), tempfile.TemporaryDirectory() as temp, \
                local_build(False), public_route():
            install = fake_install(Path(temp))
            with mock.patch.object(
                runtime, "_run_pip", pretend_pip
            ), mock.patch.object(runtime, "local_source", never):
                with self.assertRaises(runtime.RuntimeProblem) as caught:
                    runtime.fill(install)
            self.assertFalse(runtime.runtime_slot(install).exists())
        for source in runtime.PUBLIC_SOURCES:
            self.assertIn(source, caught.exception.message)
        self.assertEqual(len(calls), 1)
        self.assertIn(
            f"{runtime.ENGINE_DISTRIBUTION} @ {runtime.ENGINE_SOURCE_URL}", calls[0]
        )

    def test_the_plain_failure_names_every_source_and_what_to_do(self):
        """What the blocked user should have been told, and now is."""
        with public_route():
            problem = runtime._plain_words_failure(
                "ERROR: Could not find a version that satisfies the requirement "
                "eedesignpanels==0.1.0 (from versions: none)",
                None,
            )
        for url in runtime.PUBLIC_SOURCES:
            self.assertIn(url, problem.message)
        # The one thing the user can check: a wheel folder must hold a wheel
        # for each part, built for KiCad's own Python.
        self.assertIn("built for the Python that comes with KiCad", problem.message)
        self.assertIn(runtime.ENGINE_VERSION, problem.message)
        self.assertIn(runtime.CORE_VERSION, problem.message)
        self.assertNotIn("Traceback", problem.message)

    def test_the_plain_failure_names_the_wheel_folder_when_one_is_set(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            os.environ, {runtime.WHEEL_FOLDER_VARIABLE: folder}
        ):
            problem = runtime._plain_words_failure(
                "ERROR: No matching distribution found for eedesigncore==0.38.0",
                None,
            )
        self.assertIn(f"the wheel folder {folder}", problem.message)
        for url in runtime.PUBLIC_SOURCES:
            self.assertNotIn(url, problem.message)

    def test_an_explicit_source_still_works_for_a_developer(self):
        # `--from <path>` is explicit by definition, so it needs no variable:
        # somebody typed the path. It is the other door into the same route.
        with tempfile.TemporaryDirectory() as temp:
            command = runtime._pip_command(
                fake_install(Path(temp)), Path("C:/slot"), "C:/work/engine"
            )
        self.assertEqual(command[-1], "C:/work/engine")

    def test_the_status_readout_says_which_side_of_the_opt_in_it_is_on(self):
        with local_build(True):
            self.assertIn("local build", runtime.describe())
            self.assertIn(runtime.LOCAL_BUILD_VARIABLE, runtime.describe())
        with local_build(False):
            self.assertIn("not allowed", runtime.describe())

    def test_the_readout_names_every_source_it_fills_from(self):
        with public_route():
            readout = runtime.describe()
        for url in runtime.PUBLIC_SOURCES:
            self.assertIn(url, readout)

    def test_the_readout_says_when_the_wheel_folder_is_in_use(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            os.environ, {runtime.WHEEL_FOLDER_VARIABLE: folder}
        ):
            readout = runtime.describe()
        self.assertIn(f"the wheel folder {folder}", readout)
        self.assertIn(f"offline, set by {runtime.WHEEL_FOLDER_VARIABLE}", readout)
        for url in runtime.PUBLIC_SOURCES:
            self.assertNotIn(url, readout)


@runs_real_kicad(
    "KiCad's own bundled Python runs pip against the published sources, or "
    "the offline wheel folder, and fills a throwaway runtime, with the local "
    "build forbidden"
)
class ThePublishedPathFillsARuntimeByItself(unittest.TestCase):
    """THE ONE THAT MATTERS. Measured. No mock anywhere in it.

    The fallback is forbidden two ways over: the opt-in variable is cleared,
    AND `local_source` is replaced with a function that fails the test if it is
    ever called. So the only way this can pass is for the sources to serve
    what the fill asks them for, at the addresses the module carries.

    Which sources: the ones this machine is set up for. With
    `EEDESIGNPANELS_WHEEL_FOLDER` set, the folder and nothing else, and no
    network is reached. Without it, the two public release archives and the
    package index pip is set up to use.

    With no network this SKIPS, because "no answer" says nothing about the
    addresses. A source that answers and has no such package is a FAILURE -
    that is precisely the defect being guarded, and pip's answer for it is
    "could not find a version", never a connection error.
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("KiCad's bundled Python is resolved through Windows")
        try:
            self.install = runtime.kicad_install()
        except runtime.RuntimeProblem as problem:
            self.skipTest(f"no KiCad on this machine: {problem.message[:60]}")

    def test_it_fills_from_the_published_path_with_no_local_build_available(self):
        said = []

        def never(*args, **kwargs):
            raise AssertionError(
                "the local build was reached; this test proves the PUBLISHED "
                "path works and a local build standing in for it is the "
                "failure it is here to catch"
            )

        started = time.monotonic()
        with TempHome() as home, local_build(False):
            with mock.patch.object(runtime, "local_source", never):
                try:
                    result = runtime.fill(self.install, report=said.append)
                except runtime.RuntimeProblem as problem:
                    self._explain(problem, home)
                    raise
            seconds = time.monotonic() - started
            slot = runtime.runtime_slot(self.install)
            packages = runtime.site_packages_of(slot)

            self.assertEqual(result.state, "filled")
            self.assertIsNone(runtime.damage(slot, self.install))
            for name in EXPECTED_PACKAGES:
                self.assertTrue(
                    runtime._package_present(packages, name),
                    f"{name} is not in the runtime the published path filled",
                )
            stamp = runtime.read_stamp(slot)
            self.assertIsNone(stamp["source"], "this came from a local build")
            self.assertEqual(stamp["sources"], list(runtime.fill_sources()))
            self.assertEqual(stamp["version"], runtime.ENGINE_VERSION)
            self.assertEqual(
                runtime._version_in(packages, runtime.ENGINE_DISTRIBUTION),
                runtime.ENGINE_VERSION,
            )
            self.assertEqual(
                runtime._version_in(packages, runtime.CORE_DISTRIBUTION),
                runtime.CORE_VERSION,
            )
            self.assertGreater(stamp["bytes"], 1_000_000)
            # Measured, printed rather than asserted: a number to compare the
            # next run against, never a threshold that fails a slow network.
            print(
                f"\n  published-path fill: {seconds:.0f} s, {stamp['files']} "
                f"files, {stamp['bytes'] / 1_000_000:.1f} MB, from "
                f"{'; '.join(stamp['sources'])}",
                file=sys.stderr,
            )
            self.assertTrue(any("is set up" in line for line in said))

    def _explain(self, problem, home):
        """Skip when the network was the answer; otherwise let the fill fail."""
        log = home.home / runtime.FILL_LOG_NAME
        transcript = log.read_text(encoding="utf-8") if log.is_file() else ""
        if (
            "internet connection" in problem.message
            and not runtime._server_has_no_such_version(transcript)
        ):
            self.skipTest(
                "the published sources did not answer - this machine has no "
                "network, which says nothing about the addresses"
            )


if __name__ == "__main__":
    unittest.main()
