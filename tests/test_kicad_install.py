"""The engine finds `kicad-cli` and the KiCad settings folder on any machine.

The search order is the plugin's own, carried further: beside the running
Python first, then the usual install folders for the operating system, then
PATH. No KiCad version and no install folder is fixed in the source.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eedesignpanels import fplib, kicad_install
from eedesignpanels.review import render, theme
from eedesignpanels.stages import refill


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class SearchOrderTest(unittest.TestCase):

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.python_dir = self.root / "python"
        self.python_dir.mkdir()
        self.executable = str(self.python_dir / "python.exe")
        self.program_files = self.root / "Program Files"

    def tearDown(self):
        self._folder.cleanup()

    def find(self, platform="win32", environ=None):
        environ = {"ProgramFiles": str(self.program_files), "PATH": ""} \
            if environ is None else environ
        return kicad_install.find_kicad_cli(
            executable=self.executable, environ=environ, platform=platform,
            home=self.root / "home")

    def test_the_tool_beside_the_running_python_comes_first(self):
        beside = touch(self.python_dir / "kicad-cli.exe")
        touch(self.program_files / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        self.assertEqual(self.find(), beside.resolve())

    def test_the_newest_installed_version_comes_next(self):
        touch(self.program_files / "KiCad" / "9.0" / "bin" / "kicad-cli.exe")
        newest = touch(self.program_files / "KiCad" / "10.0" / "bin"
                       / "kicad-cli.exe")
        self.assertEqual(self.find(), newest)

    def test_a_version_folder_is_read_off_the_disk_not_assumed(self):
        future = touch(self.program_files / "KiCad" / "11.1" / "bin"
                       / "kicad-cli.exe")
        self.assertEqual(self.find(), future)

    def test_path_is_the_last_place_looked(self):
        on_path = touch(self.root / "tools" / "kicad-cli.exe")
        found = self.find(environ={"PATH": str(on_path.parent)})
        self.assertEqual(found.resolve(), on_path.resolve())

    def test_nothing_found_is_none(self):
        self.assertIsNone(self.find(environ={"PATH": ""}))

    def test_linux_looks_in_the_package_folders(self):
        searched = kicad_install.candidates(
            executable=self.executable, environ={"PATH": ""}, platform="linux")
        self.assertIn(Path("/usr/bin/kicad-cli"), searched)
        self.assertIn(Path("/usr/local/bin/kicad-cli"), searched)
        self.assertEqual(searched[0].name, "kicad-cli")

    def test_macos_looks_in_the_application_bundle(self):
        searched = kicad_install.candidates(
            executable=self.executable, environ={"PATH": ""},
            platform="darwin", home=self.root / "home")
        bundle = Path("KiCad/KiCad.app/Contents/MacOS/kicad-cli")
        self.assertIn(Path("/Applications") / bundle, searched)
        self.assertIn(self.root / "home" / "Applications" / bundle, searched)


class NoFixedInstallTest(unittest.TestCase):

    def test_no_engine_module_names_a_kicad_version_or_install_folder(self):
        for module in (kicad_install, render, refill, theme):
            source = Path(module.__file__).read_text(encoding="utf-8")
            with self.subTest(module=module.__name__):
                self.assertNotIn("Program Files/KiCad", source)
                self.assertNotIn("Program Files\\KiCad", source)
                self.assertIsNone(re.search(r"KiCad[/\\]+\d+\.\d+", source))

    def test_the_default_tool_is_the_search_result(self):
        found = kicad_install.find_kicad_cli()
        if found is None:
            self.assertFalse(render.DEFAULT_KICAD_CLI.is_file())
        else:
            self.assertEqual(render.DEFAULT_KICAD_CLI, found)
            self.assertEqual(refill.DEFAULT_KICAD_CLI, found)


class SettingsFolderTest(unittest.TestCase):

    def test_the_theme_and_the_footprint_tables_read_one_folder(self):
        self.assertEqual(theme.settings_dir(), fplib.settings_dir())

    def test_kicad_s_own_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"KICAD_CONFIG_HOME": folder}):
            self.assertEqual(theme.settings_dir().parent, Path(folder))


if __name__ == "__main__":
    unittest.main()
