"""Save replaces the person's own board file, so it keeps a backup and writes safely.

The step-1 page edits the design and `Save` writes it back over the design
file. Three things are pinned here:

* **The previous bytes are kept.** Before the design file is replaced, its
  bytes are copied to `<board file>-bak` beside it.
* **A new file that will not load never replaces the old one.** The new text
  is parsed before it is written, and the write goes through a temporary file
  in the same folder. A failure leaves the design file exactly as it was.
* **Nothing is left behind.** No temporary file stays in the design folder.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eedesigncore import Board, GuardedSaveError, GuardedSaveResult

from eedesignpanels.config import load_config
from eedesignpanels.preview import actions
from test_preview import write_project


class SafeSaveTest(unittest.TestCase):
    """`actions.save` over a real design file in a scratch folder."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.board_path = self.config.source_board_path
        self.board = Board.from_file(self.board_path)
        self.before = self.board_path.read_bytes()
        # One change, so the saved file differs from the file on disk.
        self.data = {"lock_board_on_save": True}

    def tearDown(self):
        self._folder.cleanup()

    def save(self, **extra):
        return actions.save(self.board, self.board_path, self.data,
                            self.config_path, **extra)

    def test_the_previous_design_is_kept_beside_it(self):
        saved = self.save()
        backup = actions.backup_path_for(self.board_path)
        self.assertEqual(saved.backup_path, backup)
        self.assertEqual(backup.parent, self.board_path.parent)
        self.assertEqual(backup.name, self.board_path.name + "-bak")
        self.assertEqual(backup.read_bytes(), self.before)
        self.assertIn(backup.name, saved.message)

    def test_the_saved_design_loads(self):
        self.save()
        self.assertIsInstance(Board.from_file(self.board_path), Board)

    def test_a_second_save_keeps_the_first_save_as_the_backup(self):
        self.save()
        first = self.board_path.read_bytes()
        self.save()
        self.assertEqual(
            actions.backup_path_for(self.board_path).read_bytes(), first)

    def test_no_temporary_file_is_left_in_the_design_folder(self):
        names_before = {path.name for path in self.board_path.parent.iterdir()}
        self.save(write_config=True)
        names_after = {path.name for path in self.board_path.parent.iterdir()}
        self.assertEqual(names_after - names_before,
                         {self.board_path.name + "-bak"})
        panel_names = {path.name for path in self.config_path.parent.iterdir()}
        self.assertFalse([name for name in panel_names
                          if name.endswith(".tmp")])

    def test_text_that_will_not_load_never_replaces_the_design(self):
        # The board serializes to text the parser refuses. The guarded save
        # parses the new text before it writes anything.
        with mock.patch.object(Board, "to_string",
                               return_value="(kicad_pcb (version"):
            with self.assertRaises(actions.ActionError) as caught:
                self.save()
        self.assertIn("unchanged", str(caught.exception))
        self.assertEqual(self.board_path.read_bytes(), self.before)

    def test_a_failed_replace_leaves_the_design_as_it_was(self):
        refused = GuardedSaveError(
            "the disk is full", GuardedSaveResult((), "rejected"))
        with mock.patch.object(actions, "save_guarded_documents",
                               side_effect=refused):
            with self.assertRaises(actions.ActionError) as caught:
                self.save()
        self.assertIn("the disk is full", str(caught.exception))
        self.assertEqual(self.board_path.read_bytes(), self.before)
        self.assertEqual(
            actions.backup_path_for(self.board_path).read_bytes(), self.before)

    def test_a_missing_design_file_writes_nothing(self):
        self.board_path.unlink()
        with self.assertRaises(actions.ActionError):
            self.save()
        self.assertFalse(self.board_path.exists())
        self.assertFalse(actions.backup_path_for(self.board_path).exists())

    def test_the_config_bytes_are_what_text_mode_wrote_before(self):
        self.save(write_config=True)
        expected = actions.config_bytes(self.data).replace("\n", os.linesep)
        self.assertEqual(self.config_path.read_bytes(),
                         expected.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
