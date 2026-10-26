"""The review layer: naming rules, SVG inlining, and theme colour lookup.

Nothing here calls kicad-cli - these are the parts that decide what its output
is called and how it is embedded.
"""

import tempfile
import unittest
from pathlib import Path

from eedesignpanels.review.page import VIEWBOX_PAD_FRACTION, inline_svg
from eedesignpanels.review.render import LayerSpec, select_layers
from eedesignpanels.review.theme import (
    FALLBACK_BACKGROUND,
    FALLBACK_LAYER_COLOR,
    Theme,
    active_theme_name,
    load_theme,
)

LAYERS = [
    LayerSpec(0, "F.Cu", "signal", "F.Cu"),
    LayerSpec(5, "F.SilkS", "user", "F.Silkscreen"),
    LayerSpec(25, "Edge.Cuts", "user", "Edge.Cuts"),
    LayerSpec(45, "User.4", "front", "User.4-F.FpRef"),
]

SVG = (
    '<?xml version="1.0" standalone="no"?>\n'
    '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd">\n'
    '<svg xmlns="http://www.w3.org/2000/svg"\n'
    '  width="15.0000mm" height="10.0000mm" '
    'viewBox="0.0000 0.0000 15.0000 10.0000">\n'
    "<title>SVG Image created as board-Edge_Cuts.svg date 2026-09-02 </title>\n"
    '<g><path d="M0 0"/></g>\n'
    "</svg>\n"
)


class LayerNamingTest(unittest.TestCase):
    def test_svg_suffix_uses_the_display_name_with_underscores(self):
        # kicad-cli names files after the display name, not the canonical one.
        self.assertEqual(LAYERS[1].svg_suffix, "F_Silkscreen")
        self.assertEqual(LAYERS[3].svg_suffix, "User_4-F_FpRef")
        self.assertEqual(LAYERS[0].svg_suffix, "F_Cu")


class SelectLayersTest(unittest.TestCase):
    def test_all_keeps_board_order(self):
        self.assertEqual(select_layers(LAYERS, "all"), LAYERS)

    def test_explicit_list_keeps_the_requested_order(self):
        chosen = select_layers(LAYERS, ["Edge.Cuts", "F.Cu"])
        self.assertEqual([layer.canonical_name for layer in chosen],
                         ["Edge.Cuts", "F.Cu"])

    def test_unknown_layer_is_rejected(self):
        with self.assertRaises(ValueError):
            select_layers(LAYERS, ["No.Such"])


class InlineSvgTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "board-Edge_Cuts.svg"
        self.path.write_text(SVG, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_wrapper_is_adapted_and_geometry_is_untouched(self):
        result = inline_svg(self.path, "svg-0-25", "Edge.Cuts")
        self.assertEqual((result.width_mm, result.height_mm), (15.0, 10.0))
        self.assertIn('<path d="M0 0"/>', result.markup)
        self.assertNotIn("<?xml", result.markup)
        self.assertNotIn("<!DOCTYPE", result.markup)
        self.assertNotIn("<title>", result.markup)
        self.assertNotIn('width="15.0000mm"', result.markup)
        self.assertIn('id="svg-0-25"', result.markup)
        self.assertIn('data-layer="Edge.Cuts"', result.markup)

    def test_viewbox_is_padded_so_the_outline_is_not_clipped(self):
        pad = 15.0 * VIEWBOX_PAD_FRACTION
        result = inline_svg(self.path, "svg-0-25", "Edge.Cuts")
        expected = (f'viewBox="{-pad:.4f} {-pad:.4f} '
                    f'{15.0 + 2 * pad:.4f} {10.0 + 2 * pad:.4f}"')
        self.assertIn(expected, result.markup)
        self.assertIn("data-base=", result.markup)

    def test_a_file_without_a_viewbox_is_an_error(self):
        self.path.write_text("<svg></svg>", encoding="utf-8")
        with self.assertRaises(ValueError):
            inline_svg(self.path, "svg-0-0", "Edge.Cuts")

    def test_a_millimetre_pad_raises_the_fraction_when_it_has_to(self):
        # For a page carrying annotation OUTSIDE the board - the v-score
        # captions, held a router diameter off the outline. That distance is
        # millimetres and the fraction is not, so on a small board the fraction
        # alone would clip them.
        result = inline_svg(self.path, "svg-0-25", "Edge.Cuts", pad_mm=2.0)
        self.assertIn('viewBox="-2.0000 -2.0000 19.0000 14.0000"',
                      result.markup)

    def test_the_pad_is_a_floor_and_never_a_ceiling(self):
        # A pad smaller than the fraction changes nothing: the outline still
        # needs its half stroke.
        plain = inline_svg(self.path, "svg-0-25", "Edge.Cuts")
        small = inline_svg(self.path, "svg-0-25", "Edge.Cuts", pad_mm=0.01)
        self.assertEqual(plain.markup, small.markup)


class ViewerGuardsTest(unittest.TestCase):
    """The board viewer must not let a stray selection freeze panning.

    Pressing into the image while a selection is live starts a native
    drag-and-drop, which swallows pointermove; panning then looks broken until
    you click somewhere else. Each guard below is load-bearing.
    """

    def test_the_whole_card_is_unselectable(self):
        from eedesignpanels.review.page import CSS
        card = CSS[CSS.index(".card{"):CSS.index(".head{")]
        self.assertIn("user-select:none", card)

    def test_the_plots_do_not_take_the_pointer(self):
        from eedesignpanels.review.page import CSS
        self.assertIn("pointer-events:none", CSS)

    def test_the_browsers_own_drag_is_suppressed(self):
        from eedesignpanels.review.page import JS
        self.assertIn("dragstart", JS)


class ThemeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self._tmp.name)
        (self.settings / "colors").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write_theme(self, name, body):
        import json
        (self.settings / "colors" / f"{name}.json").write_text(
            json.dumps({"board": body}), encoding="utf-8"
        )

    def write_pcbnew(self, name):
        import json
        (self.settings / "pcbnew.json").write_text(
            json.dumps({"appearance": {"color_theme": name}}), encoding="utf-8"
        )

    def test_copper_lookup_uses_the_copper_sub_object(self):
        theme = Theme("t", {"copper": {"f": "#f00", "in1": "#0f0"}}, "#000")
        self.assertEqual(theme.layer_color("F.Cu"), "#f00")
        self.assertEqual(theme.layer_color("In1.Cu"), "#0f0")
        self.assertEqual(theme.layer_color("In9.Cu"), FALLBACK_LAYER_COLOR)

    def test_other_layers_are_keyed_lowercase_with_underscores(self):
        theme = Theme("t", {"f_silks": "#fff", "user_4": "#abc"}, "#000")
        self.assertEqual(theme.layer_color("F.SilkS"), "#fff")
        self.assertEqual(theme.layer_color("User.4"), "#abc")
        self.assertEqual(theme.layer_color("Margin"), FALLBACK_LAYER_COLOR)

    def test_active_theme_comes_from_pcbnew_json(self):
        self.write_theme("user", {"background": "#111"})
        self.write_theme("cicd", {"background": "#222"})
        self.write_pcbnew("cicd")
        self.assertEqual(active_theme_name(self.settings), "cicd")
        self.assertEqual(load_theme(settings=self.settings).background, "#222")

    def test_unknown_active_theme_falls_back_to_a_theme_file(self):
        self.write_theme("user", {"background": "#111"})
        self.write_pcbnew("deleted-theme")
        self.assertEqual(active_theme_name(self.settings), "user")

    def test_missing_settings_do_not_break_a_render(self):
        theme = load_theme(settings=self.settings / "absent")
        self.assertEqual(theme.background, FALLBACK_BACKGROUND)
        self.assertEqual(theme.layer_color("Edge.Cuts"), FALLBACK_LAYER_COLOR)


if __name__ == "__main__":
    unittest.main()
