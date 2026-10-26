"""The review entry point resolves its output folder without being told."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from eedesignpanels.config import (
    CONFIG_REVISION,
    ConfigError,
    load_config,
)
from eedesignpanels.review import __main__ as main
from eedesignpanels.review.__main__ import SCRATCH_ENV, scratch_root

# The PCB editor's hole colours, as KiCad's built-in default theme carries
# them: `plated_hole` is the NON-plated hole (color_settings.cpp:136) and
# `via_hole` is the plated one (pcb_painter.cpp:1238, :295).
NON_PLATED = "rgb(26, 196, 210)"
PLATED = "rgb(227, 183, 46)"


def _config(root: Path, **extra) -> object:
    """A minimal valid config on disk, with a board file that exists."""
    board = root / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    panel = root / "panel"
    panel.mkdir()
    config_path = panel / "panel.json"
    config_path.write_text(json.dumps({
        "revision": CONFIG_REVISION,
        "source_board": "../board.kicad_pcb",
        "stage": "passthrough",
        "grid": {"columns": 1, "rows": 1, "gap_mm": 0.0},
        **extra,
    }), encoding="utf-8")
    return load_config(config_path)


class ScratchRootTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(SCRATCH_ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(SCRATCH_ENV, None)
        else:
            os.environ[SCRATCH_ENV] = self._saved

    def test_the_environment_variable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            os.environ[SCRATCH_ENV] = str(Path(tmp) / "elsewhere")
            self.assertEqual(scratch_root(config), Path(tmp) / "elsewhere")

    def test_it_finds_the_plugin_projects_scratch_folder_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            root = scratch_root(config)
        # Running from the repository checkout, which is where reviews belong.
        self.assertEqual(root.name, "scratch")
        self.assertEqual(root.parent.name, "kicad_plugin")

    def test_without_a_checkout_it_stays_inside_the_panel_folder(self):
        # The fallback an installed wheel takes: the folder the builder already
        # owns, so it never writes somewhere the caller did not expect. Named
        # after a project that cannot be found, which is what a wheel sees.
        original = main.PLUGIN_PROJECT
        main.PLUGIN_PROJECT = "no.such.plugin.project"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = _config(Path(tmp))
                self.assertEqual(scratch_root(config), config.panel_dir / "review")
        finally:
            main.PLUGIN_PROJECT = original


class PanelGeometryTest(unittest.TestCase):
    def test_a_stage_that_does_not_step_and_repeat_has_no_grid(self):
        # passthrough copies the board, so there is no panel extent to predict,
        # no placements to stamp, and the board is never even looked at.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertIsNone(main.geometry_of(config, source=None))


class ExpectedExtentTest(unittest.TestCase):
    """What the render is checked against: the grid, or the sheet it was cut
    from. One derivation, shared with the stage, so that a panel cannot be
    grown by one rule and predicted by another."""

    class Geometry:
        outline_extent_mm = (34.0, 37.5)
        inked_extent_mm = (34.05, 37.55)

    def test_without_a_panel_section_the_grid_is_what_is_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            outline, inked = main.expected_extents(config, self.Geometry())
            self.assertEqual(outline, (34.0, 37.5))
            self.assertEqual(inked, (34.05, 37.55))

    def test_the_retired_sheet_margin_key_is_refused(self):
        # `panel.sheet.margin_mm` is an old name for `panel.rails`. The config
        # reader has no migration for it, so it is refused exactly like a
        # typo rather than read as a margin.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as raised:
                _config(Path(tmp), panel={"sheet": {"margin_mm": 5.0}})
            self.assertIn("panel.sheet", str(raised.exception))


class PanelOwnedLayersTest(unittest.TestCase):
    """Which layers the comparison reports rather than judges.

    The dimensions layer is in every plotted output, which puts it into the
    comparison as carrier ink. The panel states its own size; the source board
    stamped at every placement states nothing, so a verdict off that reference
    would be false. It is not in `panelize.PANEL_OWNED_LAYERS` because the
    config moves it, which is the whole reason the answer is computed here.
    """

    PANEL = {"rails": 10.0}

    def test_a_panelized_config_owns_its_dimensions_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp), stage="build_panel", panel=self.PANEL)
            owned = main.panel_owned_layers(config)
        self.assertEqual(owned, {"Edge.Cuts", "User.38", "User.16"})

    def test_the_panels_derived_layers_are_still_in_it(self):
        from eedesignpanels.stages import panelize

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp), stage="build_panel", panel=self.PANEL)
            owned = main.panel_owned_layers(config)
        self.assertTrue(panelize.PANEL_OWNED_LAYERS <= owned)

    def test_it_follows_the_config_when_the_dimensions_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                Path(tmp), stage="build_panel",
                panel={**self.PANEL, "dimensions": {"layer": "Dwgs.User"}},
            )
            owned = main.panel_owned_layers(config)
        self.assertIn("Dwgs.User", owned)
        self.assertNotIn("User.16", owned)

    def test_dimensions_declined_owns_only_what_the_panel_derives(self):
        from eedesignpanels.stages import panelize

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                Path(tmp), stage="build_panel",
                panel={**self.PANEL, "dimensions": {"enabled": False}},
            )
            owned = main.panel_owned_layers(config)
        self.assertEqual(owned, frozenset(panelize.PANEL_OWNED_LAYERS))

    def test_a_config_that_does_not_panelize_owns_nothing(self):
        # Its instances' own outlines ARE judged, exactly as they always were.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertEqual(main.panel_owned_layers(config), frozenset())

    def test_the_layer_it_owns_is_the_one_the_page_opens_showing(self):
        # One answer, two consumers: the dimensions layer opens visible on the
        # review page and is unjudged in the comparison. A change to either
        # must move both, so they are pinned to each other here.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp), stage="build_panel", panel=self.PANEL)
            self.assertTrue(
                set(main.dimension_layers(config))
                <= main.panel_owned_layers(config)
            )


class HoleMarksTest(unittest.TestCase):
    """The review's own step: lift the drill marks, or say why it could not.

    The exports are real kicad-cli output (see `test_review_holes`); what is
    tested here is the decision the entry point takes on top of them - a file
    written and no sentence, or no file and a sentence for the page.
    """

    class Export:
        def __init__(self, svg_by_layer):
            self.svg_by_layer = svg_by_layer

    def setUp(self):
        from eedesignpanels.review.render import LayerSpec
        from eedesignpanels.review.theme import Theme

        self.data = Path(__file__).parent / "data"
        self.layers = [LayerSpec(0, "F.Cu", "signal", "F.Cu")]
        self.theme = Theme("t", {"copper": {"f": "rgb(200, 52, 52)"}}, "#000")

    def test_the_marks_are_written_beside_the_render(self):
        export = self.Export({"F.Cu": self.data / "holes_export_F_Cu.svg"})
        with tempfile.TemporaryDirectory() as tmp:
            path, notice = main.hole_marks(
                self.layers, export, self.theme, Path(tmp), "panel",
                log=lambda *_: None,
            )
            self.assertEqual(notice, "")
            self.assertEqual(path, Path(tmp) / "svg_panel" / "holes.svg")
            self.assertIn("<circle", path.read_text(encoding="utf-8"))

    def test_an_unreadable_export_yields_a_notice_and_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.svg"
            broken.write_text("<svg><g></g></svg>", encoding="utf-8")
            path, notice = main.hole_marks(
                self.layers, self.Export({"F.Cu": broken}), self.theme,
                Path(tmp), "panel", log=lambda *_: None,
            )
            self.assertIsNone(path)
            self.assertIn("could not be shown", notice)
            self.assertFalse((Path(tmp) / "svg_panel").exists())

    def test_the_marks_take_the_editors_hole_colours_from_the_theme(self):
        # The marks use the PCB editor's colours, not the plot colours. The
        # theme key `plated_hole` is LAYER_NON_PLATEDHOLES
        # (color_settings.cpp:136).
        from eedesignpanels.review.theme import Theme

        active = Theme("t", {"copper": {"f": "rgb(200, 52, 52)"},
                             "plated_hole": NON_PLATED, "via_hole": PLATED},
                       "rgb(0, 16, 35)")
        export = self.Export({"F.Cu": self.data / "holes_export_F_Cu.svg"})
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = main.hole_marks(self.layers, export, active, Path(tmp),
                                      "panel", log=lambda *_: None)
            written = path.read_text(encoding="utf-8")
        self.assertIn(f"fill:{NON_PLATED};", written)
        self.assertIn(f"fill:{PLATED};", written)
        self.assertNotIn("fill:#FFFFFF", written)
        self.assertNotIn("fill:#000000", written)

    def test_a_board_with_no_holes_yields_neither(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.svg"
            plain.write_text(
                '<svg viewBox="0 0 1 1" width="1mm" height="1mm">'
                '<g transform="translate(0 0)" style="fill:#000000;"></g>'
                '<g style="fill:none; stroke:#8D8E8B;"><path d="M0 0"/></g>'
                "</svg>",
                encoding="utf-8",
            )
            path, notice = main.hole_marks(
                self.layers, self.Export({"F.Cu": plain}), self.theme,
                Path(tmp), "source", log=lambda *_: None,
            )
            self.assertIsNone(path)
            self.assertEqual(notice, "")


class PanelReviewPageHolesTest(unittest.TestCase):
    """The holes on the PANEL REVIEW page, read back off the written file.

    The dual-viewport page is the review's own host. It shares the mode
    branches with the prep toolpath page, and both are fenced on their own
    emitted bytes so a change for one cannot pass unnoticed in the other.
    """

    def build(self) -> str:
        from eedesignpanels.review import holes, page
        from eedesignpanels.review.render import LayerSpec
        from eedesignpanels.review.theme import Theme

        data = Path(__file__).parent / "data"
        front = data / "holes_export_F_Cu.svg"
        active = Theme("t", {"copper": {"f": "rgb(200, 52, 52)"},
                             "plated_hole": NON_PLATED, "via_hole": PLATED},
                       "rgb(0, 16, 35)")
        layers = [LayerSpec(0, "F.Cu", "signal", "F.Cu")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marks, _ = holes.write_beside(
                [("F.Cu", front)], {}, root / "holes.svg",
                palette=holes.editor_palette(active),
            )
            stack = {"F.Cu": front, holes.HOLES_LAYER: marks}
            out = page.build_page(
                out_path=root / "page.html",
                heading="Panel review - test",
                subtitle="test",
                layers=[*layers, holes.holes_layer(layers)],
                views=[page.BoardView(title="source", svg_by_layer=stack),
                       page.BoardView(title="panel", svg_by_layer=stack)],
                theme=holes.with_swatch(active),
                start_visible=(holes.HOLES_LAYER,),
                on_top=(holes.HOLES_LAYER,),
            )
            return out.read_text(encoding="utf-8")

    def test_it_stays_a_file_with_nothing_to_talk_to(self):
        # THE FROZEN ARTIFACT'S FENCE. The served preview and this page are
        # built from the same components, so every change made for the server
        # is checked here as well: a frozen page has no origin, no controls and
        # no server, and a single `fetch` in a shared part would take its
        # portability with it. A review page is one self-contained file that
        # opens with no server.
        markup = self.build()
        for token in ("<link ", "<script src", "@import", "url(", "fetch(",
                      "XMLHttp", "WebSocket", "EventSource", "location.reload",
                      "/fragment", "pb-hit", "data-act"):
            with self.subTest(token=token):
                self.assertNotIn(token, markup)

    def test_the_viewport_hook_the_served_page_uses_is_inert_here(self):
        # `viewFrames` is how the LIVE preview re-frames a viewport in place.
        # It is registered on this page too - one component, two hosts - and
        # nothing here ever calls it.
        markup = self.build()
        self.assertIn("viewFrames.push({", markup)
        self.assertNotIn("rebaseViews", markup)

    def test_H_dims_and_hides_the_holes_with_every_other_layer(self):
        markup = self.build()
        self.assertIn(
            "if (on && !isActive && panel.mode === 1) opacity *= 0.22;", markup
        )
        self.assertIn(
            "if (on && !isActive && panel.mode === 2) shown = false;", markup
        )
        self.assertNotIn("!layer.top && panel.mode", markup)

    def test_the_holes_keep_their_top_stacking_order_in_both_viewports(self):
        markup = self.build()
        self.assertIn("zIndex = layer.top ? 999 : (isActive ? 99 : 1)", markup)
        from eedesignpanels.review import holes

        self.assertEqual(
            markup.count(f'data-layer="{holes.HOLES_LAYER}"'), 2
        )

    def test_both_viewports_are_painted_the_editors_hole_colours(self):
        from eedesignpanels.review import holes

        markup = self.build()
        stacks = markup.split(f'data-layer="{holes.HOLES_LAYER}"')[1:]
        self.assertEqual(len(stacks), 2)
        for stack in stacks:
            overlay = stack[:stack.index("</svg>")]
            self.assertIn(f"fill:{NON_PLATED};", overlay)
            self.assertIn(f"fill:{PLATED};", overlay)
            self.assertNotIn("fill:#FFFFFF", overlay)

    def test_the_row_still_opens_visible_with_its_own_eye(self):
        from eedesignpanels.review import holes

        table = json.loads(
            re.search(r"const LAYERS = (\[.*?\]);", self.build(), re.S).group(1)
        )
        entry = next(row for row in table
                     if row["canonical"] == holes.HOLES_LAYER)
        self.assertTrue(entry["start"])
        self.assertTrue(entry["top"])
        self.assertFalse(entry["physical"])


if __name__ == "__main__":
    unittest.main()
