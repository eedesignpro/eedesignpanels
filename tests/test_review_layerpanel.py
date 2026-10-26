"""The shared layer panel, and the marks the diff page puts on its rows.

Both review artifacts render their layers from this one component, so what is
pinned here is the part a wrong answer would make invisible: board order, the
key that ties a row to its comparison, and the third status - "not compared",
which must never render as a pass.
"""

import unittest

from eedesignpanels.review import layerpanel
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.review.theme import Theme
from eedesignpanels.verify.report import status_marks
from eedesignpanels.verify.results import CompareResult, LayerDiff
from eedesignpanels.verify.regions import Bounds, Canvas

LAYERS = [
    LayerSpec(0, "F.Cu", "signal", "F.Cu"),
    LayerSpec(5, "F.SilkS", "user", "F.Silkscreen"),
    LayerSpec(25, "Edge.Cuts", "user", "Edge.Cuts"),
]
THEME = Theme("t", {"copper": {"f": "#f00"}, "edge_cuts": "#ff0"}, "#000")


def layer_diff(name, differing) -> LayerDiff:
    return LayerDiff(
        layer=name, present_in_a=True, present_in_b=True,
        total_pixels=100, pixel_area_mm2=0.000025,
        ink_pixels_a=10, ink_pixels_b=10,
        pixels_differing=differing, pixels_only_in_a=differing,
        pixels_only_in_b=0,
    )


def result_for(*layers) -> CompareResult:
    return CompareResult(
        canvas=Canvas.over(Bounds(0.0, 0.0, 1.0, 1.0), 5.0),
        um_per_pixel=5.0, threshold=0, region_cell_mm=1.0,
        layers=tuple(layers), dir_a="a", dir_b="b",
    )


class LayersJsonTest(unittest.TestCase):
    def test_the_key_is_the_name_kicad_cli_gives_the_export(self):
        data = layerpanel.layers_json(LAYERS)
        self.assertIn('"key": "F_Silkscreen"', data)
        self.assertIn('"canonical": "F.SilkS"', data)

    def test_board_order_is_preserved(self):
        data = layerpanel.layers_json(LAYERS)
        self.assertLess(data.index("F.Cu"), data.index("Edge.Cuts"))

    def test_the_script_carries_this_boards_table(self):
        script = layerpanel.panel_script(LAYERS)
        self.assertNotIn("__LAYERS__", script)
        self.assertIn("Edge.Cuts", script)


class PhysicalLayerTest(unittest.TestCase):
    """Which layers start visible, decided by rule and not by a name list.

    The rule has to keep working for a board nobody has seen yet, so what is
    pinned is the *kind* of answer - including for layers this project does
    not have today.
    """

    def physical(self, canonical, display=None):
        return layerpanel.is_physical_layer(
            LayerSpec(0, canonical, "user", display or canonical)
        )

    def test_everything_that_becomes_board_starts_visible(self):
        for canonical in ("F.Cu", "B.Cu", "In1.Cu", "In30.Cu", "F.Mask",
                          "B.Mask", "F.SilkS", "B.SilkS", "F.Paste",
                          "Edge.Cuts"):
            self.assertTrue(self.physical(canonical), canonical)

    def test_documentation_layers_start_hidden(self):
        for canonical in ("F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd", "Margin",
                          "Dwgs.User", "Cmts.User", "Eco1.User"):
            self.assertFalse(self.physical(canonical), canonical)

    def test_a_route_or_score_layer_is_found_by_its_display_name(self):
        # These live on User.N layers, so only the display name identifies
        # them - and it is what an engineer reads off the Appearance panel.
        self.assertTrue(self.physical("User.36", "User.36-RouteToolPath"))
        self.assertTrue(self.physical("User.38", "User.38-VScore"))
        self.assertTrue(self.physical("User.39", "User.39-EdgePlating"))

    def test_the_match_ignores_separators_and_case(self):
        self.assertTrue(self.physical("User.38", "V.Score"))
        self.assertTrue(self.physical("User.38", "vscore"))
        self.assertTrue(self.physical("User.36", "Route_Tool_Path"))

    def test_a_plain_user_layer_is_not_physical(self):
        self.assertFalse(self.physical("User.40", "User.40-QrCode"))
        self.assertFalse(self.physical("User.41", "User.41-DwgCommon"))

    def test_the_flag_reaches_the_component(self):
        self.assertIn('"physical": true', layerpanel.layers_json(LAYERS))
        with_fab = LAYERS + [LayerSpec(30, "F.Fab", "user", "F.Fab")]
        self.assertIn('"physical": false', layerpanel.layers_json(with_fab))


class RowsTest(unittest.TestCase):
    def test_a_viewer_row_carries_the_theme_swatch_and_an_eye(self):
        rows = layerpanel.rows_html(LAYERS, theme=THEME)
        self.assertIn("lp-eye", rows)
        self.assertIn("#f00", rows)
        self.assertNotIn("lp-status", rows)

    def test_a_diff_row_carries_a_status_mark_and_no_theme_colour(self):
        rows = layerpanel.rows_html(
            LAYERS, status_by_layer={"F.Cu": layerpanel.STATUS_BAD}
        )
        self.assertIn('class="lp-status bad"', rows)
        self.assertNotIn("#f00", rows)
        self.assertNotIn("lp-eye", rows)

    def test_the_tooltip_is_not_double_escaped(self):
        # It is a `title` attribute, escaped once on the way out. Building it
        # from HTML entities escaped those too, and the tooltip read
        # "F.Cu &middot; signal" on screen.
        rows = layerpanel.rows_html(
            LAYERS[:1],
            status_by_layer={"F.Cu": layerpanel.STATUS_OK},
            counts_by_layer={"F.Cu": 12},
        )
        self.assertNotIn("&amp;", rows)
        self.assertIn(
            'title="F.Cu - signal - matches - 12 differing pixels"', rows
        )

    def test_an_unlisted_layer_is_marked_not_compared(self):
        rows = layerpanel.rows_html(LAYERS, status_by_layer={})
        self.assertEqual(rows.count('class="lp-status none"'), len(LAYERS))

    def test_a_row_marks_one_thing_or_the_other_never_both_or_neither(self):
        with self.assertRaises(ValueError):
            layerpanel.rows_html(LAYERS)
        with self.assertRaises(ValueError):
            layerpanel.rows_html(LAYERS, theme=THEME, status_by_layer={})


class StatusMarksTest(unittest.TestCase):
    def test_a_clean_layer_passes_and_a_differing_one_fails(self):
        result = result_for(layer_diff("F_Cu", 0), layer_diff("Edge_Cuts", 7))
        marks = status_marks(result, LAYERS)
        self.assertEqual(marks["F.Cu"], layerpanel.STATUS_OK)
        self.assertEqual(marks["Edge.Cuts"], layerpanel.STATUS_BAD)

    def test_a_layer_the_comparison_never_covered_is_not_a_pass(self):
        # "not measured" and "no difference" are different answers, and the
        # panel must not show the first as the second.
        marks = status_marks(result_for(layer_diff("F_Cu", 0)), LAYERS)
        self.assertEqual(marks["F.SilkS"], layerpanel.STATUS_NONE)
        self.assertEqual(marks["Edge.Cuts"], layerpanel.STATUS_NONE)


if __name__ == "__main__":
    unittest.main()
