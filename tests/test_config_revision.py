"""The settings file says which tool wrote it, and the tool says what it reads.

The settings file carries a revision number. Four cases, and each one is
here: the current revision, an older one, a newer one,
and a file that carries no revision at all. The last three take one path -
standard settings, a plain sentence on the page, and the user's file
untouched - so what each test pins is the SENTENCE, because the sentence is
the diagnosis once engineers hold configs of their own.

No migration is tested because the tool has none: a file at another revision
is left untouched and standard settings are used.
"""

import json
import tempfile
import unittest
from pathlib import Path

from eedesignpanels import __version__
from eedesignpanels.config import (
    CONFIG_REVISION,
    REVISION_KEY,
    WRITTEN_BY,
    WRITTEN_BY_KEY,
    ConfigError,
    ConfigRevisionError,
    config_text,
    default_config_data,
    load_config,
    load_or_create,
    revision_sentence,
    stamped,
)

BOARD_NAME = "board.kicad_pcb"

VALID = {
    "revision": CONFIG_REVISION,
    "source_board": f"../{BOARD_NAME}",
    "grid": {"columns": 2, "rows": 3, "gap_mm": 3.0},
}


class RevisionProjectTestCase(unittest.TestCase):
    """A project folder with one board in it and a panel folder beside it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.panel_dir = self.project / "panel"
        self.panel_dir.mkdir()
        (self.project / BOARD_NAME).write_text("(kicad_pcb)", "utf-8")
        self.addCleanup(self._tmp.cleanup)

    def write(self, data) -> Path:
        path = self.panel_dir / "panel.json"
        path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return path


class CurrentRevisionTest(RevisionProjectTestCase):
    """A file at the current revision loads."""

    def test_a_file_at_this_revision_loads(self):
        config = load_config(self.write(VALID))
        self.assertEqual(config.revision, CONFIG_REVISION)

    def test_the_revision_is_the_first_thing_in_a_written_file(self):
        keys = list(json.loads(config_text(default_config_data("../b.kicad_pcb"))))
        self.assertEqual(keys[:2], [REVISION_KEY, WRITTEN_BY_KEY])

    def test_a_written_file_says_which_version_wrote_it(self):
        data = json.loads(config_text(default_config_data("../b.kicad_pcb")))
        self.assertEqual(data[REVISION_KEY], CONFIG_REVISION)
        self.assertEqual(data[WRITTEN_BY_KEY], WRITTEN_BY)
        self.assertIn(__version__, data[WRITTEN_BY_KEY])

    def test_the_version_line_is_never_compared(self):
        # The version line is information only: a file that names another
        # version of this tool still loads, because the REVISION is what
        # decides.
        config = load_config(self.write(
            dict(VALID, written_by="eedesignpanels 0.0.1-from-a-colleague")
        ))
        self.assertEqual(config.revision, CONFIG_REVISION)

    def test_a_new_project_is_written_at_this_revision(self):
        startup = load_or_create(self.panel_dir / "panel.json")
        self.assertTrue(startup.created)
        self.assertEqual(startup.problem, "")
        self.assertEqual(startup.data[REVISION_KEY], CONFIG_REVISION)

    def test_stamping_puts_the_two_keys_first_and_makes_them_current(self):
        stale = {"source_board": "../b.kicad_pcb", "revision": 99,
                 "written_by": "eedesignpanels 0.0.1"}
        self.assertEqual(
            list(stamped(stale)),
            [REVISION_KEY, WRITTEN_BY_KEY, "source_board"],
        )
        self.assertEqual(stamped(stale)[REVISION_KEY], CONFIG_REVISION)
        self.assertEqual(stamped(stale)[WRITTEN_BY_KEY], WRITTEN_BY)


class RevisionSentenceTest(unittest.TestCase):
    """One sentence per case, naming the file's revision and this tool's."""

    def test_this_revision_has_nothing_to_say(self):
        self.assertEqual(revision_sentence(CONFIG_REVISION), "")

    def test_an_older_file_is_called_older(self):
        sentence = revision_sentence(CONFIG_REVISION - 1)
        self.assertIn("older version", sentence)
        self.assertIn(str(CONFIG_REVISION - 1), sentence)
        self.assertIn(str(CONFIG_REVISION), sentence)

    def test_a_newer_file_is_called_newer_and_never_broken(self):
        sentence = revision_sentence(CONFIG_REVISION + 1)
        self.assertIn("newer version", sentence)
        self.assertIn(str(CONFIG_REVISION + 1), sentence)
        self.assertIn(str(CONFIG_REVISION), sentence)
        for word in ("invalid", "corrupt", "error"):
            self.assertNotIn(word, sentence)

    def test_no_revision_at_all_is_said_plainly(self):
        sentence = revision_sentence(None)
        self.assertIn("no revision number", sentence)
        self.assertIn(str(CONFIG_REVISION), sentence)

    def test_a_revision_that_is_not_a_whole_number_is_named(self):
        for odd in ("1", 1.5, True, [1]):
            with self.subTest(revision=odd):
                self.assertIn("whole number", revision_sentence(odd))


class OtherRevisionLoadTest(RevisionProjectTestCase):
    """`load_config` says OLD or NEW, and never complains about a field."""

    def refused(self, data) -> ConfigRevisionError:
        with self.assertRaises(ConfigRevisionError) as caught:
            load_config(self.write(data))
        return caught.exception

    def test_an_older_file_is_refused_as_older(self):
        exc = self.refused(dict(VALID, revision=CONFIG_REVISION - 1))
        self.assertIn("older version", exc.reason)
        self.assertIsInstance(exc, ConfigError)

    def test_a_newer_file_is_refused_as_newer(self):
        exc = self.refused(dict(VALID, revision=CONFIG_REVISION + 1))
        self.assertIn("newer version", exc.reason)

    def test_a_file_with_no_revision_is_refused_as_older(self):
        data = {key: value for key, value in VALID.items() if key != "revision"}
        self.assertIn("no revision number", self.refused(data).reason)

    def test_an_old_file_is_told_it_is_old_and_not_told_about_a_field(self):
        # An old config carries keys this tool no longer knows. It is named
        # as OLD rather than as a file with a bad field, because the age is
        # the real cause.
        exc = self.refused(dict(VALID, revision=CONFIG_REVISION - 1,
                                sheet={"margin_mm": 5.0}))
        self.assertIn("older version", exc.reason)
        self.assertNotIn("sheet", exc.reason)


class OtherRevisionStartupTest(RevisionProjectTestCase):
    """The launch goes on, says why, and does not touch the file."""

    def startup(self, data):
        path = self.write(data)
        before = path.read_bytes()
        startup = load_or_create(path)
        self.assertEqual(path.read_bytes(), before, "the file was written to")
        return startup

    def test_an_older_file_starts_on_standard_settings(self):
        startup = self.startup(dict(VALID, revision=CONFIG_REVISION - 1))
        self.assertFalse(startup.created)
        self.assertIn("older version", startup.problem)
        self.assertIn("standard settings", startup.problem)
        self.assertIn("NOT been changed", startup.problem)
        self.assertEqual(startup.data[REVISION_KEY], CONFIG_REVISION)
        self.assertEqual(startup.config.source_board, f"../{BOARD_NAME}")

    def test_a_newer_file_says_so_and_does_not_read_as_broken(self):
        startup = self.startup(dict(VALID, revision=CONFIG_REVISION + 1))
        self.assertIn("newer version", startup.problem)
        self.assertIn("different version of eedesignpanels", startup.problem)
        self.assertIn("different version", startup.banner)

    def test_a_file_with_no_revision_says_so(self):
        data = {key: value for key, value in VALID.items() if key != "revision"}
        startup = self.startup(data)
        self.assertIn("no revision number", startup.problem)
        self.assertIn("standard settings", startup.banner)

    def test_a_file_at_this_revision_says_nothing(self):
        startup = self.startup(VALID)
        self.assertEqual(startup.problem, "")
        self.assertEqual(startup.banner, "")
        self.assertEqual(startup.data[WRITTEN_BY_KEY], WRITTEN_BY)


if __name__ == "__main__":
    unittest.main()
