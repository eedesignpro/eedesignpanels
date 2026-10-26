"""THE STEPPED PANEL, KEPT: what it may be reused for, and what it may not.

MEASURED on the corpus: a carrier-only edit - a tooling hole diameter, for
example - cost 547 ms to first paint without the kept panel, of which 404 ms
was `inject_board` deep-copying six instances that the edit could not move.
Keeping the stepped panel between edits takes that edit to 149 ms.

**Every test here is about staleness, not about speed.** A cache fails by
serving a stale panel, so the question each test asks is the same one: is the
panel this produced the panel a FROM-SCRATCH build produces, byte for byte? A
cache that answers no is worse than the third of a second it saves, so the
fence is drawn round every kind of edit there is - carrier settings, which
reuse; the layout, which must not; and the board, which must not.

The other half of the fence is that a reused stepped panel must come out of a
build UNCHANGED. `finish` puts the carrier features on a cheap copy
(`step_and_repeat.cheap_copy`), so a second edit against one kept panel cannot
find the first edit's holes already on it - which is asserted here rather than
read off the code, because it is the one way this shape can fail.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eedesigncore import Board

from eedesignpanels import channels, prep
from eedesignpanels.channels import ChannelError
from eedesignpanels.config import load_config
from eedesignpanels.preview import actions, panelbuild
from eedesignpanels.preview.labels import PROPOSED
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.stages import panelize, step_and_repeat
from test_panel_page import write_panel_project
from test_preview import stub_render

# The carrier settings a test moves, as they are written in `panel.json`. Named
# here rather than through the page's parameter names, because those belong to
# `preview.actions` and this file is about the build under them.
HOLES_WIDER = {"tooling_holes": {"diameter_mm": 3.4}}
SCORES_ON = {"scores": {"enabled": True, "inter_column": True,
                        "inter_row": False, "rails_horizontal": False,
                        "rails_vertical": False}}
DIMENSIONS_OFF = {"dimensions": {"enabled": False}}


def config_with(folder: Path, panel: dict = None, **top) -> object:
    """The panel fixture's config with *panel* merged into its panel section."""
    config_path = write_panel_project(folder)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["panel"].update(panel or {})
    data.update(top)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return load_config(config_path)


def prepped_board(config):
    """The design as the panel is derived from it: prepped, at file precision."""
    board = Board.from_file(config.source_board_path)
    prep.compute(config, board)
    return panelbuild.snapshot_of(board)


def from_scratch(config, snapshot) -> str:
    """The panel a plain build writes for this config and this board.

    THE ORACLE for every comparison in this file. It is `build_from` on a board
    of its own, so nothing it produces can have been touched by the cache under
    test.
    """
    board = Board.from_string(snapshot.text)
    return step_and_repeat.build_from(
        config, board, fill_rails=False
    ).board.to_string()


class PanelizeSplitTest(unittest.TestCase):
    """`stepped` then `carrier` is `apply`, and `carrier` may run twice."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config = config_with(self.folder, panel=dict(SCORES_ON))
        self.snapshot = prepped_board(self.config)

    def tearDown(self):
        self._folder.cleanup()

    def stepped_only(self):
        """A panel stepped and panelized, with no carrier feature on it."""
        return step_and_repeat.stepped_from(self.config, self.snapshot.board)

    def test_the_two_halves_are_the_whole_stage(self):
        holes, scores, rail_copper = self.config.carrier_features
        dimensions = self.config.panel.dimension_settings
        one = self.stepped_only()
        whole = panelize.apply(
            one.board, self.config.rail_widths, self.config.min_tab_mm,
            tooling_holes=holes, scores=scores, rail_copper=rail_copper,
            dimensions=dimensions,
            instance_boxes=one.geometry.instance_boxes, fill_rails=False,
        )
        two = self.stepped_only()
        base = panelize.stepped(two.board, self.config.rail_widths,
                                self.config.min_tab_mm)
        split = panelize.carrier(
            two.board, base, tooling_holes=holes, scores=scores,
            rail_copper=rail_copper, dimensions=dimensions,
            instance_boxes=two.geometry.instance_boxes, fill_rails=False,
        )
        # The panel, and then everything the panel says about itself.
        self.assertEqual(two.board.to_string(), one.board.to_string())
        self.assertEqual(split.warnings, whole.warnings)
        self.assertEqual(split.blank, whole.blank)
        self.assertEqual(split.scores.merged_count, whole.scores.merged_count)
        self.assertEqual(split.holes.count, whole.holes.count)
        self.assertEqual(split.panel_scores.count, whole.panel_scores.count)
        self.assertEqual(split.dimensions.count, whole.dimensions.count)

    def test_the_stepped_half_carries_no_carrier_feature(self):
        # What the cache holds. A stepped panel with a hole already in it would
        # give the next edit two holes, and nothing else here would notice.
        built = self.stepped_only()
        self.assertIsNotNone(built.stepped)
        text = built.board.to_string()
        self.assertNotIn("PanelToolingHole", text)
        self.assertNotIn("panel_score", text)

    def test_the_warnings_of_one_pass_read_as_one_list(self):
        # `carrier` returns the first half's warnings first and its own after,
        # in the order the panel produced them - so a report reads the same
        # whichever entry the caller used.
        built = self.stepped_only()
        base = panelize.stepped(built.board, self.config.rail_widths,
                                self.config.min_tab_mm)
        holes, scores, rail_copper = self.config.carrier_features
        made = panelize.carrier(
            built.board, base, tooling_holes=holes, scores=scores,
            rail_copper=rail_copper,
            dimensions=self.config.panel.dimension_settings,
            instance_boxes=built.geometry.instance_boxes, fill_rails=False,
        )
        self.assertEqual(made.warnings[:len(base.warnings)],
                         list(base.warnings))


class CheapCopyTest(unittest.TestCase):
    """The copy carrier features are put on: own lists, shared items."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config = config_with(self.folder)
        self.snapshot = prepped_board(self.config)
        self.built = step_and_repeat.stepped_from(self.config,
                                                  self.snapshot.board)

    def tearDown(self):
        self._folder.cleanup()

    def test_a_copy_writes_the_same_board(self):
        copy = step_and_repeat.cheap_copy(self.built.board)
        self.assertEqual(copy.to_string(), self.built.board.to_string())

    def test_a_copy_owns_its_lists(self):
        copy = step_and_repeat.cheap_copy(self.built.board)
        for name in ("drawings", "footprints", "zones", "tracks",
                     "trailing_drawings", "groups"):
            self.assertIsNot(getattr(copy, name),
                             getattr(self.built.board, name), name)

    def test_a_copy_shares_its_items(self):
        # The point of the copy: the items are not duplicated, which is why it
        # costs under a millisecond where a deep copy costs 130.
        copy = step_and_repeat.cheap_copy(self.built.board)
        for index, item in enumerate(self.built.board.drawings):
            self.assertIs(copy.drawings[index], item)

    def test_appending_to_a_copy_leaves_the_original_alone(self):
        before = len(self.built.board.drawings)
        copy = step_and_repeat.cheap_copy(self.built.board)
        copy.drawings.append(copy.drawings[0])
        self.assertEqual(len(self.built.board.drawings), before)


class FinishIsNonDestructiveTest(unittest.TestCase):
    """One stepped build, finished as many times as a designer types.

    `finish` puts the carrier features on a cheap copy rather than on the panel
    it was given, which is what lets a pre-carrier panel be kept. These tests
    are about that - a build reused must be a build unchanged.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config = config_with(self.folder)
        self.snapshot = prepped_board(self.config)

    def tearDown(self):
        self._folder.cleanup()

    def test_a_finish_leaves_the_stepped_build_alone(self):
        built = step_and_repeat.stepped_from(self.config, self.snapshot.board)
        before = built.board.to_string()
        step_and_repeat.finish_from(self.config, built, fill_rails=False)
        self.assertEqual(built.board.to_string(), before)

    def test_two_finishes_of_one_build_are_two_from_scratch_builds(self):
        # THE STALENESS QUESTION at the stage level, on the bytes: one kept
        # build, two different carrier settings, each compared with the panel a
        # plain build writes for that setting.
        built = step_and_repeat.stepped_from(self.config, self.snapshot.board)
        wider = config_with(self.folder / "wider", panel=dict(HOLES_WIDER))
        scored = config_with(self.folder / "scored", panel=dict(SCORES_ON))
        for config in (wider, scored, wider):
            reused = step_and_repeat.finish_from(
                config, built, fill_rails=False
            ).board.to_string()
            self.assertEqual(reused, from_scratch(config, self.snapshot))

    def test_the_outcome_carries_the_pre_carrier_panel(self):
        outcome = step_and_repeat.build_from(
            self.config, self.snapshot.board, fill_rails=False
        )
        self.assertIsNotNone(outcome.stepped)
        self.assertIsNot(outcome.stepped.board, outcome.board)
        self.assertNotIn("PanelToolingHole", outcome.stepped.board.to_string())
        self.assertIn("PanelToolingHole", outcome.board.to_string())

    def test_a_build_is_still_byte_identical_twice(self):
        # The standing gate, restated through the split: same input, same
        # output, byte for byte.
        first = from_scratch(self.config, self.snapshot)
        second = from_scratch(self.config, self.snapshot)
        self.assertEqual(first, second)


class FingerprintTest(unittest.TestCase):
    """WHAT INVALIDATES THE CACHE, stated as the key and tested as the key.

    An allowlist of what may change - the four carrier sections - and everything
    else in the config counts as layout. That direction is deliberate: a key
    nobody has written yet invalidates the cache instead of being reused
    against, so a new config setting is safe before anybody remembers it exists.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config = config_with(self.folder)
        self.base = panelbuild.layout_fingerprint(self.config)

    def tearDown(self):
        self._folder.cleanup()

    def fingerprint_of(self, name: str, panel: dict = None, **top) -> str:
        return panelbuild.layout_fingerprint(
            config_with(self.folder / name, panel=panel, **top)
        )

    def test_a_carrier_section_does_not_move_it(self):
        # `panel.scores` is NOT in this list: it carries the rail setbacks,
        # which grow the blank, so the section counts as layout - see
        # `test_the_layout_moves_it`.
        for name, panel in (("holes", HOLES_WIDER),
                            ("dimensions", DIMENSIONS_OFF),
                            ("pour", {"rail_copper": {"clearance_mm": 0.7}})):
            self.assertEqual(self.fingerprint_of(name, panel=dict(panel)),
                             self.base, name)

    def test_the_layout_moves_it(self):
        moved = {
            "rails": ({"rails": 6.0}, {}),
            # `panel.scores` carries the rail setbacks, which grow the
            # blank, so the whole section is layout.
            "scores": (dict(SCORES_ON), {}),
            "columns": (None, {"grid": {"columns": 3, "rows": 1,
                                        "gap_mm": 3.0}}),
            "rows": (None, {"grid": {"columns": 2, "rows": 2,
                                     "gap_mm": 3.0}}),
            "gap": (None, {"grid": {"columns": 2, "rows": 1,
                                    "gap_mm": 4.0}}),
            "min_tab": (None, {"min_tab_mm": 0.6}),
            "excluded": (None, {"exclude_layers": ["Dwgs.User"]}),
        }
        for name, (panel, top) in moved.items():
            self.assertNotEqual(self.fingerprint_of(name, panel=panel, **top),
                                self.base, name)

    def test_a_prep_number_moves_it_too(self):
        # Conservative, and it costs nothing: a prep number changes the BOARD,
        # which drops the snapshot and invalidates the cache anyway. Two fences.
        self.assertNotEqual(
            self.fingerprint_of("cutter", prep={
                "cutter_diameter": 0.8,
                "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
            }),
            self.base,
        )


class DeriveWithCacheTest(unittest.TestCase):
    """`panelbuild.derive`: reuse where it is true, rebuild where it is not."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config = config_with(self.folder)
        self.snapshot = prepped_board(self.config)

    def tearDown(self):
        self._folder.cleanup()

    def derive(self, config, cache=None):
        return panelbuild.derive(config, self.snapshot.board,
                                 fill_rails=False, snapshot=self.snapshot,
                                 cache=cache)

    def test_a_first_derive_hands_back_a_cache(self):
        derived = self.derive(self.config)
        self.assertIsNotNone(derived.cache)
        self.assertIs(derived.cache.snapshot, self.snapshot)
        self.assertEqual([name for name, _ in derived.phases], ["build_full"])

    def test_a_carrier_edit_reuses_it_and_gives_the_same_bytes(self):
        first = self.derive(self.config)
        wider = config_with(self.folder / "wider", panel=dict(HOLES_WIDER))
        again = self.derive(wider, cache=first.cache)
        self.assertIs(again.cache, first.cache)
        self.assertEqual(dict(again.phases).keys(), {"carrier_only"})
        self.assertEqual(again.outcome.board.to_string(),
                         from_scratch(wider, self.snapshot))

    def test_a_layout_edit_rebuilds_and_gives_the_same_bytes(self):
        first = self.derive(self.config)
        moved = config_with(self.folder / "moved", panel={"rails": 6.5})
        again = self.derive(moved, cache=first.cache)
        self.assertIsNot(again.cache, first.cache)
        self.assertIsNot(again.cache.built, first.cache.built)
        self.assertEqual(again.outcome.board.to_string(),
                         from_scratch(moved, self.snapshot))

    def test_a_cache_from_another_snapshot_is_not_used(self):
        # The board fence, by identity. A cache is only ever true for the
        # snapshot it was built from, so a second snapshot - even of the same
        # board - rebuilds.
        first = self.derive(self.config)
        other = panelbuild.snapshot_of(self.snapshot.board)
        derived = panelbuild.derive(self.config, self.snapshot.board,
                                    fill_rails=False, snapshot=other,
                                    cache=first.cache)
        self.assertIsNot(derived.cache, first.cache)
        self.assertEqual(derived.outcome.board.to_string(),
                         from_scratch(self.config, self.snapshot))

    def test_a_reused_cache_never_accumulates_features(self):
        # Six carrier edits against ONE kept panel. If the copy were not a copy
        # the sixth would carry six sets of holes, and the byte comparison is
        # what says it does not.
        cache = self.derive(self.config).cache
        for diameter in (3.0, 3.2, 3.4, 3.6, 3.8, 4.0):
            config = config_with(
                self.folder / f"d{diameter}",
                panel={"tooling_holes": {"diameter_mm": diameter}},
            )
            derived = self.derive(config, cache=cache)
            cache = derived.cache
            self.assertEqual(derived.outcome.board.to_string(),
                             from_scratch(config, self.snapshot))

    def test_a_filled_derive_neither_reads_nor_writes_the_cache(self):
        # The save path, untouched. It steps the board itself, and the cache it
        # was handed comes back exactly as it arrived.
        cache = self.derive(self.config).cache
        derived = panelbuild.derive(self.config, self.snapshot.board,
                                    fill_rails=True, snapshot=self.snapshot,
                                    cache=cache)
        self.assertIs(derived.cache, cache)
        self.assertEqual(derived.phases, ())

    def test_no_cache_handed_in_means_a_full_build(self):
        derived = self.derive(self.config)
        self.assertEqual(derived.outcome.board.to_string(),
                         from_scratch(self.config, self.snapshot))


class SessionCacheTest(unittest.TestCase):
    """The live path: what each kind of edit costs, and what it must equal.

    Every edit is followed by the same question - is the panel the session now
    holds the panel a from-scratch build writes for what the session now holds?
    - because that is the only form of "not stale" that means anything.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()
        self.assertEqual(self.session.panel_error, "")

    def tearDown(self):
        self.session.close()
        self._folder.cleanup()

    def blade(self):
        for row in self.session.fragment()["proposals"]:
            if row["verdict"] == PROPOSED:
                return row["blade"]
        raise AssertionError("the fixture offers no candidate to accept")

    def assert_not_stale(self, what: str):
        """The held panel is the panel a from-scratch build writes. Bytes."""
        self.assertEqual(self.session.panel_error, "", what)
        board = Board.from_string(actions.board_for_save(
            self.session.board, self.session.data).to_string())
        fresh = step_and_repeat.build_from(
            self.session.config, board, fill_rails=False
        ).board.to_string()
        self.assertEqual(self.session.panel.outcome.board.to_string(), fresh,
                         f"the panel after {what} is not what a build writes")

    def injections(self):
        """A counter round `inject_board`: how many instances were stepped."""
        return mock.patch.object(step_and_repeat, "inject_board",
                                 wraps=step_and_repeat.inject_board)

    def test_a_carrier_edit_injects_nothing(self):
        with self.injections() as injected:
            self.session.set_param({"hole_diameter_mm": 3.4})
        self.assertEqual(injected.call_count, 0)
        self.assert_not_stale("a tooling hole diameter")

    def test_a_carrier_edit_says_so_in_the_phase_journal(self):
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.assertIn("carrier_only", self.session._panel_phases)
        self.assertNotIn("build_full", self.session._panel_phases)

    def test_a_score_switch_now_steps_the_boards_again(self):
        # `panel.scores` also carries the rail setbacks, which grow the blank,
        # so the whole section counts as layout and any edit inside it - the
        # switch included - re-steps the boards rather than redrawing the
        # carrier alone.
        count = (self.session.config.grid.columns
                * self.session.config.grid.rows)
        with self.injections() as injected:
            self.session.set_param({"scores_on": True})
        self.assertEqual(injected.call_count, count)
        self.assert_not_stale("switching the panel scores on")
        with self.injections() as injected:
            self.session.set_param({"score_inter_column": True})
        self.assertEqual(injected.call_count, count)
        self.assert_not_stale("a score switch")

    def test_a_rail_edit_steps_the_boards_again(self):
        # NO SAVING HERE, and it is not a shortfall: a rail moves the blank, so
        # the panel round the boards is a different panel.
        with self.injections() as injected:
            self.session.set_param({"rail_left_mm": 6.0})
        self.assertEqual(injected.call_count,
                         self.session.config.grid.columns
                         * self.session.config.grid.rows)
        self.assertIn("build_full", self.session._panel_phases)
        self.assert_not_stale("a rail width")

    def test_a_rail_typed_to_zero_rebuilds_and_removes_that_side(self):
        # Unlock the rails, type a zero, and the panel must follow - the
        # blank's edge lands on the boards and the picture is what a
        # from-scratch build writes.
        self.session.set_param({"rail_right_mm": 0.0})
        self.assert_not_stale("a rail removed")
        made = self.session.panel.outcome.panelize
        self.assertAlmostEqual(made.blank.max_x, made.instance_bbox[2],
                               places=9)
        self.assertEqual(made.rails.right, 0.0)

    def test_the_zero_rail_declines_its_two_corners_and_says_so(self):
        self.session.set_param({"rail_right_mm": 0.0})
        made = self.session.panel.outcome.panelize
        self.assertEqual([skip.corner for skip in made.holes.skipped],
                         ["top_right", "bottom_right"])
        self.assertTrue(any("no rail material" in note
                            for note in self.session.panel.notes),
                        self.session.panel.notes)

    def test_putting_the_rail_back_gives_the_panel_that_was_there(self):
        before = self.session.panel.outcome.board.to_string()
        self.session.set_param({"rail_right_mm": 0.0})
        self.session.set_param({"rail_right_mm": 5.0})
        self.assert_not_stale("a rail removed and restored")
        self.assertEqual(self.session.panel.outcome.board.to_string(), before)

    def test_an_outline_that_will_not_close_is_a_banner_and_not_a_crash(self):
        # If the recalculation raised out of the request, the page would keep
        # the picture of the panel BEFORE the edit, which reads as an edit that
        # was ignored. A build this stage cannot make is a sentence on the
        # page, like every other one.
        def refuse(*args, **kwargs):
            raise ChannelError("this outline does not close", (1.0, 2.0))

        with mock.patch.object(channels, "region_boundary", refuse):
            answered = self.session.set_param({"rail_right_mm": 0.0})
        self.assertTrue(answered["ok"])
        self.assertIsNone(self.session.panel)
        self.assertIn("does not close", self.session.panel_error)
        # ...and the next edit tries again and gets its panel back.
        self.session.set_param({"rail_right_mm": 5.0})
        self.assertEqual(self.session.panel_error, "")
        self.assert_not_stale("an edit after a failed recalculation")

    def test_a_gap_edit_steps_the_boards_again(self):
        with self.injections() as injected:
            self.session.set_param({"gap_x_mm": 4.0})
        self.assertGreater(injected.call_count, 0)
        self.assert_not_stale("a gap")

    def test_a_grid_edit_steps_the_boards_again(self):
        self.session.set_param({"rows": 2})
        self.assertIn("build_full", self.session._panel_phases)
        self.assert_not_stale("a row count")

    def test_a_board_edit_drops_the_cache(self):
        blade = self.blade()
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.assertIsNotNone(self.session._panel_cache)
        with self.injections() as injected:
            self.session.accept(blade)
        self.assertGreater(injected.call_count, 0)
        self.assert_not_stale("accepting a score")
        with self.injections() as injected:
            self.session.decline(blade)
        self.assertGreater(injected.call_count, 0)
        self.assert_not_stale("declining it again")

    def test_a_prep_number_drops_the_cache(self):
        self.session.set_param({"hole_diameter_mm": 3.4})
        held = self.session._panel_cache
        self.session.set_param({"min_tab_mm": 0.45})
        self.assertIsNot(self.session._panel_cache, held)
        self.assert_not_stale("a minimum tab")

    def test_a_reload_drops_the_cache(self):
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.session.save()
        self.session.reload_from_disk()
        self.assert_not_stale("re-reading the files")

    def test_a_mixed_sequence_of_edits_never_serves_a_stale_panel(self):
        # The drive, in one test: carrier, layout, board, carrier again. Each
        # step is compared with a from-scratch build of what is held.
        blade = self.blade()
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.assert_not_stale("a hole diameter")
        self.session.set_param({"scores_on": True})
        self.assert_not_stale("the scores switched on")
        self.session.set_param({"rail_left_mm": 6.0})
        self.assert_not_stale("a rail width")
        self.session.accept(blade)
        self.assert_not_stale("a score decision")
        self.session.set_param({"hole_diameter_mm": 3.6})
        self.assert_not_stale("a hole diameter after the decision")
        self.session.set_param({"gap_y_mm": 4.0})
        self.assert_not_stale("a gap down")
        self.session.set_param({"hole_top_left": False})
        self.assert_not_stale("a hole switched off")

    def test_the_same_edit_twice_gives_the_same_bytes(self):
        self.session.set_param({"hole_diameter_mm": 3.4})
        first = self.session.panel.outcome.board.to_string()
        self.session.set_param({"hole_diameter_mm": 3.0})
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.assertEqual(self.session.panel.outcome.board.to_string(), first)

    def test_the_kept_panel_stays_the_pre_carrier_panel(self):
        # Where a stale panel would come from, looked at directly: the cache
        # must never hold a panel that has been through the carrier stages.
        for diameter in (3.0, 3.2, 3.4):
            self.session.set_param({"hole_diameter_mm": diameter})
            kept = self.session._panel_cache.built.board.to_string()
            self.assertNotIn("PanelToolingHole", kept)

    def test_the_saved_panel_is_what_the_command_line_builds(self):
        # THE SAVE PATH IS UNAFFECTED. The files a fabricator gets are compared
        # with a plain build of the same held board and held config.
        self.session.set_param({"hole_diameter_mm": 3.4})
        self.session.save()
        self.session.save_panel()
        written = Path(self.session.panel_saved.output_path).read_text(
            encoding="utf-8"
        )
        board = Board.from_file(self.session.config.source_board_path)
        fresh = step_and_repeat.build_from(
            self.session.config, board, fill_rails=False
        ).board.to_string()
        self.assertEqual(written, fresh)


if __name__ == "__main__":  # pragma: no cover - the runner is the entry point
    unittest.main()
