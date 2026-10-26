"""THE BYTE GATE for in-memory editing: one save equals the old write-per-click.

The preview used to write the board on every accept and the config on every
parameter change, and then write the toolpath again on a separate commit. It now
edits objects and writes once. That is a large behavioural
change sitting on top of a file format, so it is pinned the only way worth
pinning: the OLD path is written out here, run over the same actions in the same
order, and the two files it leaves are compared byte for byte with what one save
produces.

The old path is reproduced rather than referenced, deliberately - it no longer
exists in the source, and a gate that imported the thing it is checking would be
checking nothing. It is short, and every line of it is what `actions.accept`,
`actions.decline`, `actions.set_param` and `actions.commit` used to do.

Reproducibility is the other half: two sessions given the same
actions leave the same bytes, and a save that changes no parameter leaves the
config file exactly as it was found.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from eedesigncore import Board

from eedesignpanels.config import CONFIG_REVISION, WRITTEN_BY, load_config
from eedesignpanels.preview import actions
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages import board_scores, route_toolpath
from eedesignpanels.stages.route_toolpath import outline_bbox, score_line_for
from test_preview import digest, left_blade, stub_render, write_project
from test_vscore import candidates_of


def old_accept(config, blade):
    """`actions.accept`, as it was: read the file, add one line, write it back.

    **Plus the copper keepout**, written the same way
    the old path wrote everything: read, change, write. A score decision now
    puts the copper keepout on the board beside its `User.38` line
    (`stages.board_scores`), so the old path has to do it too or this gate would
    be asking whether the new behaviour is the old behaviour, which it is not.
    """
    board = Board.from_file(config.source_board_path)
    candidate = actions.matching_candidate(blade, candidates_of(board))
    if not any(actions._accepts(item, blade)
               for item in actions.board_score_lines(board)):
        board.drawings.append(score_line_for(candidate, outline_bbox(board)))
    board_scores.sync(board, board_scores.half_width_of(config))
    board.to_file(config.source_board_path)


def old_decline(config, blade):
    """`actions.decline`, as it was: read, remove the matching lines, write."""
    board = Board.from_file(config.source_board_path)
    doomed = {
        id(item) for item in actions.board_score_lines(board)
        if actions._accepts(item, blade)
    }
    if not doomed:
        return
    board.drawings = [i for i in board.drawings if id(i) not in doomed]
    board.trailing_drawings = [
        i for i in board.trailing_drawings if id(i) not in doomed
    ]
    # ...and the keepout goes with the line, for the reason stated
    # in `old_accept`.
    board_scores.sync(board, board_scores.half_width_of(config))
    board.to_file(config.source_board_path)


def old_set_param(config, name, value):
    """`actions.set_param`, as it was: read the config file, place, write."""
    path = Path(config.config_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    actions._place(data, actions.SETTABLE[name], float(value))
    actions.validate(data, path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return load_config(path)


def old_path(config_path, script):
    """Replay *script* the way the preview replayed it before the rework."""
    config = load_config(config_path)
    for step in script:
        kind = step[0]
        if kind == "accept":
            old_accept(config, step[1](config))
        elif kind == "decline":
            old_decline(config, step[1](config))
        else:
            config = old_set_param(config, step[1], step[2])
    # ...and then the separate commit, which is what wrote the toolpath.
    route_toolpath.run(config)


def new_path(config_path, script):
    """The same script through a session, ending in ONE save."""
    with tempfile.TemporaryDirectory() as work:
        session = PreviewSession(config_path, Path(work), render=stub_render)
        session.refresh()
        for step in script:
            kind = step[0]
            if kind in ("accept", "decline"):
                blade = step[1](session.config)
                payload = {"start": list(blade.start), "end": list(blade.end)}
                getattr(session, kind)(payload)
            else:
                session.set_param({step[1]: step[2]})
        return session.save()


def blade_of(config):
    return left_blade(Board.from_file(config.source_board_path))


class SaveMatchesTheOldWritePerClickTest(unittest.TestCase):
    """Same actions, same order: one save leaves the old path's bytes."""

    #: Accept, change a number, accept the same edge again (a no-op), decline
    #: it, and accept it once more. Every branch of every edit, in one order.
    SCRIPT = (
        ("accept", blade_of),
        ("param", "cutter_diameter", 1.0),
        ("accept", blade_of),
        ("decline", blade_of),
        ("accept", blade_of),
        ("param", "route_past_score_mm", 0.75),
    )

    def project(self):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        config_path = write_project(folder)
        # Compare the historical save behavior, before optional item locking.
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["lock_board_on_save"] = False
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return config_path, load_config(config_path).source_board_path

    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self.root = self._root.name

    def tearDown(self):
        self._root.cleanup()

    def test_the_board_bytes_are_the_same_order_for_order(self):
        old_config, old_board = self.project()
        new_config, new_board = self.project()
        old_path(old_config, self.SCRIPT)
        new_path(new_config, self.SCRIPT)
        self.assertEqual(digest(new_board), digest(old_board))

    def test_the_config_bytes_are_the_same_too(self):
        old_config, _ = self.project()
        new_config, _ = self.project()
        old_path(old_config, self.SCRIPT)
        new_path(new_config, self.SCRIPT)
        self.assertEqual(digest(new_config), digest(old_config))

    def test_the_result_is_a_board_a_following_prep_still_agrees_with(self):
        # Not just the same bytes - the right bytes: one score, no stale
        # decision, the toolpath present, and prep still writing no score.
        config_path, board_path = self.project()
        new_path(config_path, self.SCRIPT)
        saved = Board.from_file(board_path)
        self.assertEqual(len(actions.board_score_lines(saved)), 1)
        result = route_toolpath.run(load_config(config_path))
        self.assertEqual(len(result.scores), 1)
        self.assertEqual(result.stale_scores, [])
        self.assertTrue(result.scores_untouched)
        self.assertGreater(result.written, 0)

    def test_two_sessions_given_the_same_actions_leave_the_same_bytes(self):
        first_config, first_board = self.project()
        second_config, second_board = self.project()
        new_path(first_config, self.SCRIPT)
        new_path(second_config, self.SCRIPT)
        self.assertEqual(digest(first_board), digest(second_board))
        self.assertEqual(digest(first_config), digest(second_config))

    def test_saving_twice_over_leaves_the_same_bytes_again(self):
        config_path, board_path = self.project()
        new_path(config_path, self.SCRIPT)
        after = digest(board_path)
        with tempfile.TemporaryDirectory() as work:
            session = PreviewSession(config_path, Path(work), render=stub_render)
            session.refresh()
            session.save()
        self.assertEqual(digest(board_path), after)


class SaveWritesOnceTest(unittest.TestCase):
    """What one save touches, and what it deliberately does not."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self._folder.cleanup()

    def blade(self):
        blade = left_blade(Board.from_file(self.board_path))
        return {"start": list(blade.start), "end": list(blade.end)}

    def test_edits_leave_both_files_byte_identical_until_the_save(self):
        before = (digest(self.board_path), digest(self.config_path))
        self.session.accept(self.blade())
        self.session.set_param({"cutter_diameter": 1.0})
        self.session.decline(self.blade())
        self.assertEqual(self.session.pending_count, 3)
        self.assertEqual(
            (digest(self.board_path), digest(self.config_path)), before
        )
        self.session.save()
        self.assertNotEqual(digest(self.board_path), before[0])

    def test_a_save_with_no_parameter_change_does_not_rewrite_the_config(self):
        before = digest(self.config_path)
        self.session.accept(self.blade())
        saved = self.session.save()
        self.assertTrue(saved["ok"])
        self.assertEqual(digest(self.config_path), before)

    def test_the_pending_count_is_cleared_by_the_save_and_not_before(self):
        self.session.accept(self.blade())
        self.assertEqual(self.session.pending_count, 1)
        self.session.save()
        self.assertEqual(self.session.pending_count, 0)
        self.assertEqual(self.session.state()["pending"], 0)

    def test_an_edit_that_changed_nothing_never_counted(self):
        self.session.accept(self.blade())
        self.session.accept(self.blade())
        self.session.set_param({"cutter_diameter":
                                self.session.config.prep.cutter_diameter})
        self.assertEqual(self.session.pending_count, 1)

    def test_the_save_bumps_the_generation_without_recomputing(self):
        # The picture does not change when it is written down, and a second
        # render to arrive at the same image is a second render.
        self.session.accept(self.blade())
        generation = self.session.generation
        computed = self.session.view
        self.session.save()
        self.assertEqual(self.session.generation, generation + 1)
        self.assertIs(self.session.view, computed)

    def test_a_board_that_cannot_be_prepared_is_never_saved(self):
        board = Board.from_file(self.board_path)
        board.drawings = board.drawings[:2]
        write_project(self.folder, board)
        before = digest(self.board_path)
        session = PreviewSession(self.config_path, self.folder / "work2",
                                 render=stub_render)
        session.refresh()
        self.assertTrue(session.error)
        with self.assertRaises(actions.ActionError):
            session.save()
        self.assertEqual(digest(self.board_path), before)

    def test_the_saved_message_names_both_files_when_both_were_written(self):
        self.session.accept(self.blade())
        self.session.set_param({"cutter_diameter": 1.0})
        saved = self.session.save()
        self.assertIn("panel.json", saved["message"])
        self.assertIn("2 changes", saved["message"])


class ConfigTextTest(unittest.TestCase):
    """The config's own text, in one place so a save and a test agree."""

    def test_it_is_two_space_json_with_a_trailing_newline(self):
        # ...and it leads with the revision and the version that wrote it,
        # whatever the caller held.
        text = actions.config_bytes({"a": 1})
        self.assertEqual(
            text,
            f'{{\n  "revision": {CONFIG_REVISION},\n'
            f'  "written_by": "{WRITTEN_BY}",\n  "a": 1\n}}\n',
        )

    def test_the_same_data_always_gives_the_same_bytes(self):
        data = {"b": 2, "a": {"c": [1, 2]}}
        self.assertEqual(
            hashlib.sha256(actions.config_bytes(data).encode()).hexdigest(),
            hashlib.sha256(actions.config_bytes(data).encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
