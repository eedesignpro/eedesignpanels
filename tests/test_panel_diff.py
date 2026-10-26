"""The panel comparison's decisions, without running one.

What is checked here is everything that happens *before* a pixel is rendered:
which layers the reference is stamped from, and how a skip is reported. A skip
is the outcome most likely to go unnoticed, so its banner is pinned too - a
comparison that did not happen must never read like one that found nothing.
"""

import unittest

from eedesignpanels.review.layerpanel import is_physical_layer
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.verify.panel_diff import (
    Skipped,
    gerber_key_of,
    judged_keys,
    panel_owned_keys,
    replicated_specs,
)
from eedesignpanels.verify.regions import Bounds, Canvas, Region
from eedesignpanels.verify.results import CompareResult, LayerDiff

LAYERS = [
    LayerSpec(0, "F.Cu", "signal", "F.Cu"),
    LayerSpec(5, "F.SilkS", "user", "F.Silkscreen"),
    LayerSpec(25, "Edge.Cuts", "user", "Edge.Cuts"),
]


class ReplicatedLayersTest(unittest.TestCase):
    def test_only_the_named_layers_are_stamped_and_board_order_holds(self):
        chosen = replicated_specs(LAYERS, {"Edge.Cuts", "F.Cu"})
        self.assertEqual([layer.canonical_name for layer in chosen],
                         ["F.Cu", "Edge.Cuts"])

    def test_a_stage_naming_a_layer_the_board_lacks_is_an_error(self):
        # Silence here would build a reference missing a layer the stage
        # believes it replicated, and the comparison would pass by omission.
        with self.assertRaises(ValueError):
            replicated_specs(LAYERS, {"No.Such"})

    def test_replicating_nothing_is_allowed_and_stamps_nothing(self):
        self.assertEqual(replicated_specs(LAYERS, set()), [])

    def test_the_gerber_key_is_the_display_name_with_underscores(self):
        self.assertEqual(gerber_key_of(LAYERS[1]), "F_Silkscreen")


class JudgedLayersTest(unittest.TestCase):
    """Which layers may set a verdict - `verify.compared_layers`.

    The default reuses the review page's own default-visibility rule, so both
    answers come from one place. Everything else is still measured; what is
    withheld is the judgment.
    """

    BOARD = [
        LayerSpec(0, "F.Cu", "signal", "F.Cu"),
        LayerSpec(1, "F.Mask", "user", "F.Mask"),
        LayerSpec(2, "F.SilkS", "user", "F.Silkscreen"),
        LayerSpec(3, "F.Fab", "user", "F.Fab"),
        LayerSpec(4, "F.CrtYd", "user", "F.Courtyard"),
        LayerSpec(5, "Edge.Cuts", "user", "Edge.Cuts"),
        LayerSpec(6, "User.38", "user", "User.38-VScore"),
    ]

    def test_the_default_judges_what_builds_the_bare_board(self):
        self.assertEqual(
            judged_keys(self.BOARD, "physical"),
            {"F_Cu", "F_Mask", "F_Silkscreen", "Edge_Cuts", "User_38-VScore"},
        )

    def test_paste_layers_are_judged(self):
        # Paste is judged by decision. It is a stencil rather than
        # bare board, so it is the one layer kind where "physical" could
        # reasonably have gone the other way - pinned here so that nothing
        # drops it quietly while tidying the rule.
        board = self.BOARD + [
            LayerSpec(7, "F.Paste", "user", "F.Paste"),
            LayerSpec(8, "B.Paste", "user", "B.Paste"),
        ]
        judged = judged_keys(board, "physical")
        self.assertIn("F_Paste", judged)
        self.assertIn("B_Paste", judged)

    def test_documentation_layers_are_not_judged_by_default(self):
        judged = judged_keys(self.BOARD, "physical")
        self.assertNotIn("F_Fab", judged)
        self.assertNotIn("F_Courtyard", judged)

    def test_the_default_agrees_with_the_viewer_rule(self):
        # One rule, two consumers. If these ever disagree, a layer is shown by
        # one standard and judged by another.
        judged = judged_keys(self.BOARD, "physical")
        for spec in self.BOARD:
            self.assertEqual(
                spec.svg_suffix in judged, is_physical_layer(spec),
                spec.canonical_name,
            )

    def test_none_means_the_default(self):
        self.assertEqual(
            judged_keys(self.BOARD, None), judged_keys(self.BOARD, "physical")
        )

    def test_all_judges_everything(self):
        self.assertEqual(
            judged_keys(self.BOARD, "all"),
            {spec.svg_suffix for spec in self.BOARD},
        )

    def test_an_explicit_list_judges_exactly_those(self):
        self.assertEqual(
            judged_keys(self.BOARD, ["Edge.Cuts", "F.Fab"]),
            {"Edge_Cuts", "F_Fab"},
        )

    def test_an_explicit_list_naming_an_absent_layer_is_an_error(self):
        # Silence here would judge nothing and look like a clean run.
        with self.assertRaises(ValueError):
            judged_keys(self.BOARD, ["No.Such"])

    def test_an_empty_list_judges_nothing(self):
        self.assertEqual(judged_keys(self.BOARD, []), set())


class UnjudgedLayerTest(unittest.TestCase):
    """A layer that is not judged reports everything and decides nothing."""

    def layer(self, judged, differing=5000):
        return LayerDiff(
            layer="F_Fab", present_in_a=True, present_in_b=True,
            total_pixels=1000, pixel_area_mm2=0.000025,
            ink_pixels_a=100, ink_pixels_b=100,
            pixels_differing=differing, pixels_only_in_a=differing,
            pixels_only_in_b=0,
            displacement_threshold_mm=0.01,
            judged=judged,
            regions=(Region(0.0, 0.0, 1.0, 1.0, pixels=5000, area_mm2=0.5,
                            perimeter_mm=1.0, widest_mm=0.3),),
        )

    def test_an_unjudged_layer_never_goes_over_threshold(self):
        loud = self.layer(judged=False)
        # It measured a millimetre of displacement and still says nothing.
        self.assertGreater(loud.worst_displacement_mm, 0.01)
        self.assertFalse(loud.over_threshold)

    def test_the_same_numbers_judged_do_go_over(self):
        self.assertTrue(self.layer(judged=True).over_threshold)

    def test_the_measurements_are_all_still_there(self):
        payload = self.layer(judged=False).to_dict()
        self.assertIs(payload["judged"], False)
        self.assertGreater(payload["worst_displacement_mm"], 0.01)
        self.assertGreater(payload["pixels_differing"], 0)
        self.assertEqual(len(payload["regions"]), 1)

    def test_an_unjudged_layer_keeps_a_run_clean(self):
        result = CompareResult(
            canvas=Canvas.over(Bounds(0.0, 0.0, 1.0, 1.0), 5.0),
            um_per_pixel=5.0, threshold=0, region_cell_mm=1.0,
            layers=(self.layer(judged=False),), dir_a="a", dir_b="b",
        )
        self.assertTrue(result.clean)
        self.assertEqual(result.layers_over_threshold, [])


class PanelOwnedTest(unittest.TestCase):
    """Layers the PANEL derives are measured and reported, never judged.

    Once the outline is the sheet minus the routed channels, stamping the board
    at every placement is not what the panel should look like - so a verdict
    off that reference would be a false one. What must not happen is
    the layer quietly disappearing: it stays in the table, with its numbers and
    with a sentence saying why nothing was decided.
    """

    BOARD = [
        LayerSpec(0, "F.Cu", "signal", "F.Cu"),
        LayerSpec(5, "Edge.Cuts", "user", "Edge.Cuts"),
        LayerSpec(6, "User.38", "user", "User.38-VScore"),
    ]

    def test_a_panel_owned_layer_is_taken_out_of_the_judged_set(self):
        judged = judged_keys(self.BOARD, "physical")
        owned = panel_owned_keys(self.BOARD, {"Edge.Cuts", "User.38"})
        self.assertIn("Edge_Cuts", judged)
        self.assertEqual(judged - set(owned), {"F_Cu"})

    def test_each_one_carries_the_reason_it_is_not_judged(self):
        owned = panel_owned_keys(self.BOARD, {"Edge.Cuts"})
        self.assertEqual(set(owned), {"Edge_Cuts"})
        self.assertIn("derived for the panel as a whole", owned["Edge_Cuts"])

    def test_naming_nothing_owns_nothing(self):
        self.assertEqual(panel_owned_keys(self.BOARD, ()), {})
        self.assertEqual(panel_owned_keys(self.BOARD, None), {})

    def test_a_layer_the_board_does_not_have_owns_nothing(self):
        self.assertEqual(panel_owned_keys(self.BOARD, {"User.36"}), {})

    def test_the_dimensions_layer_is_named_reported_never_judged(self):
        # `User.16-Dimensions` is plotted, which
        # puts it in the comparison as CARRIER ink - the panel states its own
        # size and the source board stamped six times states nothing. Measured
        # and named, never judged.
        board = self.BOARD + [
            LayerSpec(7, "User.16", "user", "User.16-Dimensions"),
        ]
        owned = panel_owned_keys(board, {"Edge.Cuts", "User.38", "User.16"})
        self.assertIn("User_16-Dimensions", owned)
        self.assertIn("never judged", owned["User_16-Dimensions"])

    def test_it_holds_even_when_the_config_judges_everything(self):
        # `verify.compared_layers = "all"` bypasses the physical-layer rule
        # that spares a numbered user layer by default. `panel_owned` is
        # subtracted afterwards, so it is the fence that always holds.
        board = self.BOARD + [
            LayerSpec(7, "User.16", "user", "User.16-Dimensions"),
        ]
        judged_all = judged_keys(board, "all")
        self.assertIn("User_16-Dimensions", judged_all)
        owned = panel_owned_keys(board, {"Edge.Cuts", "User.38", "User.16"})
        self.assertNotIn("User_16-Dimensions", judged_all - set(owned))

    def test_the_reason_rides_on_the_measurement(self):
        measured = LayerDiff(
            layer="Edge_Cuts", present_in_a=True, present_in_b=True,
            total_pixels=1000, pixel_area_mm2=0.000025,
            ink_pixels_a=100, ink_pixels_b=200,
            pixels_differing=500, pixels_only_in_a=100, pixels_only_in_b=400,
            displacement_threshold_mm=0.01, judged=False,
            unjudged_reason="Edge.Cuts is derived for the panel as a whole",
        )
        self.assertFalse(measured.over_threshold)
        self.assertEqual(
            measured.to_dict()["unjudged_reason"],
            "Edge.Cuts is derived for the panel as a whole",
        )


class SkippedTest(unittest.TestCase):
    def test_the_banner_says_the_panel_was_not_verified(self):
        banner = Skipped("placements miss whole pixels", ("cell (0, 1)",)).banner()
        self.assertIn("SKIPPED", banner)
        self.assertIn("NOT verified", banner)
        self.assertIn("placements miss whole pixels", banner)
        self.assertIn("cell (0, 1)", banner)

    def test_it_says_the_build_still_stands(self):
        # A check never blocks, so the banner has to say what did and
        # did not happen or it reads as a failed build.
        self.assertIn("stands", Skipped("no instances").banner())


if __name__ == "__main__":
    unittest.main()
