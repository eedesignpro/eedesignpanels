"""A config error must be a clear message and no output, so it is checked."""

import json
import tempfile
import unittest
from pathlib import Path

from eedesignpanels.config import (
    CONFIG_REVISION,
    ConfigError,
    load_config,
)

VALID = {
    "revision": CONFIG_REVISION,
    "source_board": "../board.kicad_pcb",
    "grid": {"columns": 2, "rows": 3, "gap_mm": 3.0},
    "review": {"layers": "all"},
}


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.panel_dir = self.project / "panel"
        self.panel_dir.mkdir()
        (self.project / "board.kicad_pcb").write_text("(kicad_pcb)", "utf-8")
        self.addCleanup(self._tmp.cleanup)

    def write(self, data, text=None):
        path = self.panel_dir / "panel.json"
        path.write_text(text if text is not None else json.dumps(data), "utf-8")
        return path

    def test_valid_config_resolves_its_paths(self):
        config = load_config(self.write(VALID))
        self.assertEqual(config.panel_dir, self.panel_dir.resolve())
        self.assertEqual(
            config.source_board_path, (self.project / "board.kicad_pcb").resolve()
        )
        self.assertEqual(
            config.output_board_path, self.panel_dir.resolve() / "panel.kicad_pcb"
        )
        self.assertEqual(config.grid.columns, 2)
        self.assertEqual(config.review.layers, "all")

    def test_review_defaults_to_all_layers(self):
        data = {k: v for k, v in VALID.items() if k != "review"}
        self.assertEqual(load_config(self.write(data)).review.layers, "all")

    def test_explicit_layer_list_is_kept_in_order(self):
        data = dict(VALID, review={"layers": ["Edge.Cuts", "F.Cu"]})
        self.assertEqual(
            load_config(self.write(data)).review.layers, ["Edge.Cuts", "F.Cu"]
        )

    def test_missing_config_file(self):
        with self.assertRaises(ConfigError):
            load_config(self.panel_dir / "absent.json")

    def test_malformed_json(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(None, text="{not json"))

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, colums=2)))

    def test_out_of_range_grid_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(
                dict(VALID, grid={"columns": 0, "rows": 1, "gap_mm": 1.0})
            ))
        with self.assertRaises(ConfigError):
            load_config(self.write(
                dict(VALID, grid={"columns": 1, "rows": 1, "gap_mm": -1.0})
            ))

    def test_stage_defaults_to_passthrough(self):
        self.assertEqual(load_config(self.write(VALID)).stage, "passthrough")

    def test_known_stage_is_accepted(self):
        data = dict(VALID, stage="build_panel")
        self.assertEqual(load_config(self.write(data)).stage, "build_panel")

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, stage="panelise_everything")))

    def test_a_quarter_turn_is_allowed(self):
        for turn in (90.0, 180.0, 270.0):
            grid = {"columns": 2, "rows": 3, "gap_mm": 3.0,
                    "rotation_deg": turn}
            config = load_config(self.write(dict(VALID, grid=grid)))
            self.assertEqual(config.grid.rotation_deg, turn)

    def test_a_turn_that_is_not_a_quarter_turn_is_rejected_with_a_reason(self):
        grid = {"columns": 2, "rows": 3, "gap_mm": 3.0, "rotation_deg": 45.0}
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(dict(VALID, grid=grid)))
        self.assertIn("rotation_deg", str(caught.exception))

    def test_zero_rotation_is_allowed(self):
        grid = {"columns": 2, "rows": 3, "gap_mm": 3.0, "rotation_deg": 0.0}
        self.assertEqual(load_config(self.write(dict(VALID, grid=grid))).grid.rows, 3)

    def test_mirror_is_rejected_with_a_reason(self):
        grid = {"columns": 2, "rows": 3, "gap_mm": 3.0, "mirror": True}
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(dict(VALID, grid=grid)))
        self.assertIn("mirror", str(caught.exception))

    def test_mirror_false_is_allowed(self):
        grid = {"columns": 2, "rows": 3, "gap_mm": 3.0, "mirror": False}
        self.assertFalse(load_config(self.write(dict(VALID, grid=grid))).grid.mirror)

    def test_no_panel_section_means_the_build_stops_at_the_grid(self):
        self.assertIsNone(load_config(self.write(VALID)).panel)

    def test_one_rail_number_means_all_four_sides(self):
        config = load_config(self.write(dict(VALID, panel={"rails": 5.0})))
        self.assertEqual(config.panel.rail_widths, (5.0, 5.0, 5.0, 5.0))
        self.assertEqual(config.rail_widths, (5.0, 5.0, 5.0, 5.0))
        # A number is the LINKED view, and that is what the page's unlock reads.
        self.assertTrue(config.panel.rails_linked)

    def test_four_rails_are_read_side_by_side(self):
        config = load_config(self.write(dict(VALID, panel={
            "rails": {"top": 8.0, "bottom": 6.0, "left": 3.0, "right": 0.0}
        })))
        self.assertEqual(config.panel.rail_widths, (8.0, 6.0, 3.0, 0.0))
        self.assertFalse(config.panel.rails_linked)

    def test_four_equal_rails_are_still_unlocked(self):
        # The FILE says per side, so the page shows per side, even when the
        # four numbers happen to agree.
        config = load_config(self.write(dict(VALID, panel={
            "rails": {"top": 5.0, "bottom": 5.0, "left": 5.0, "right": 5.0}
        })))
        self.assertFalse(config.panel.rails_linked)
        self.assertEqual(config.panel.rail_widths, (5.0, 5.0, 5.0, 5.0))

    def test_a_panel_section_with_no_rail_at_all_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(dict(VALID, panel={})))
        self.assertIn("rail width", str(caught.exception))

    def test_a_negative_rail_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, panel={"rails": -1.0})))
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, panel={
                "rails": {"top": 1.0, "bottom": 1.0, "left": -1.0, "right": 1.0}
            })))

    def test_a_rail_object_missing_a_side_is_rejected(self):
        # A side left unwritten would have to mean "as wide as the others" or
        # "none", and a fabrication number nobody typed is the wrong default.
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, panel={
                "rails": {"top": 1.0, "bottom": 1.0, "left": 1.0}
            })))

    def test_a_misspelled_rail_side_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, panel={
                "rails": {"top": 1.0, "bottom": 1.0, "left": 1.0, "rigth": 1.0}
            })))

    # -- the retired key -----------------------------------------------
    # The loader converts no old config. `panel.sheet.margin_mm` - the old
    # name of `panel.rails` - is an unrecognised key, refused like any other
    # typo, with no migration and no deprecation message.

    def test_the_old_sheet_margin_key_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(
                self.write(dict(VALID, panel={"sheet": {"margin_mm": 5.0}}))
            )
        self.assertIn("panel.sheet", str(caught.exception))

    def test_a_negative_old_margin_is_still_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(
                self.write(dict(VALID, panel={"sheet": {"margin_mm": -1.0}}))
            )

    def test_a_misspelled_old_key_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, panel={"sheet": {"margin": 5.0}})))

    # -- panel.scores: the renamed switches --------------------------------
    # `vertical`/`horizontal`/`rails` and the even older `vertical_mm`/
    # `horizontal_mm` position lists are not keys of this section, so every
    # old spelling is refused exactly like a typo, and the current names load
    # with no deprecation reported anywhere.

    def scores(self, section):
        return load_config(
            self.write(dict(VALID, panel={"rails": 5.0, "scores": section}))
        )

    def test_the_current_switches_load_as_stated(self):
        config = self.scores({"rails_horizontal": True, "rails_vertical": True,
                              "inter_row": True, "inter_column": True})
        scores = config.panel.scores
        self.assertTrue(scores.rails_horizontal)
        self.assertTrue(scores.rails_vertical)
        self.assertTrue(scores.inter_row)
        self.assertTrue(scores.inter_column)

    def test_the_old_switch_names_are_refused(self):
        for retired in ({"vertical": True}, {"horizontal": True},
                        {"rails": True}):
            with self.assertRaises(ConfigError):
                self.scores(retired)

    def test_the_old_position_lists_are_refused(self):
        for retired in ({"vertical_mm": [22.0]}, {"horizontal_mm": []}):
            with self.assertRaises(ConfigError):
                self.scores(retired)

    def test_a_switch_that_is_not_a_switch_is_refused(self):
        with self.assertRaises(ConfigError):
            self.scores({"rails_horizontal": [22.0]})

    def test_a_misspelled_switch_is_refused_rather_than_ignored(self):
        with self.assertRaises(ConfigError):
            self.scores({"rails_horizontal_scores": True})

    def test_every_setting_is_always_written_back(self):
        # The section is explicit - every field, every time - and
        # `model_dump` is what the preview's group switch puts in panel.json.
        section = self.scores({"rails_horizontal": True}).panel.scores.model_dump()
        self.assertEqual(
            sorted(section),
            ["enabled", "inter_column", "inter_row",
             "jump_horizontal_mm", "jump_vertical_mm",
             "rail_setback_horizontal_mm", "rail_setback_vertical_mm",
             "rails_horizontal", "rails_vertical",
             "shared_line_is_rail_score_horizontal",
             "shared_line_is_rail_score_vertical"],
        )

    def test_prep_without_a_vscore_section_still_looks_for_scores(self):
        # Saying nothing about v-scores is not the same as saying no: a board
        # whose edge a saw could take has to be looked at, and the run then says
        # what it found.
        config = load_config(self.write(dict(VALID, prep={"cutter_diameter": 0.889})))
        self.assertEqual(config.prep.vscore.detect, "auto")
        self.assertIsNone(config.prep.vscore.route_past_score_mm)

    def test_the_declared_overshoot_is_read(self):
        config = load_config(self.write(dict(VALID, prep={
            "cutter_diameter": 0.889,
            "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
        })))
        self.assertAlmostEqual(config.prep.vscore.route_past_score_mm, 1.0)

    def test_an_unknown_detect_mode_is_rejected_by_name(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(dict(VALID, prep={
                "cutter_diameter": 0.889, "vscore": {"detect": "clever"},
            })))
        self.assertIn("detect", str(caught.exception))

    def test_a_negative_overshoot_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, prep={
                "cutter_diameter": 0.889,
                "vscore": {"route_past_score_mm": -0.5},
            })))

    def test_a_misspelled_vscore_key_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, prep={
                "cutter_diameter": 0.889,
                "vscore": {"route_past_mm": 1.0},
            })))

    def test_missing_source_board_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, source_board="../absent.kicad_pcb")))

    def test_source_board_inside_the_panel_folder_is_rejected(self):
        (self.panel_dir / "in_panel.kicad_pcb").write_text("(kicad_pcb)", "utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, source_board="in_panel.kicad_pcb")))

    def test_wrong_extension_is_rejected(self):
        (self.project / "board.kicad_pro").write_text("{}", "utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.write(dict(VALID, source_board="../board.kicad_pro")))


if __name__ == "__main__":
    unittest.main()
