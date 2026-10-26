"""The result types are arithmetic over counts, so plain values check them.

These run without the rasterization backend installed - which is the reason
`results.py` is a module of its own.
"""

import unittest
from pathlib import Path

from eedesignpanels.verify.regions import Bounds, Canvas, PixelBox
from eedesignpanels.verify.results import CompareResult, LayerDiff


def make_layer(**overrides) -> LayerDiff:
    """A layer with round numbers: 1 mm2 pixels make every area readable."""
    base = dict(
        layer="F_Cu",
        present_in_a=True,
        present_in_b=True,
        total_pixels=1000,
        pixel_area_mm2=1.0,
        ink_pixels_a=100,
        ink_pixels_b=110,
        pixels_differing=30,
        pixels_only_in_a=10,
        pixels_only_in_b=20,
    )
    base.update(overrides)
    return LayerDiff(**base)


class LayerDiffStatsTest(unittest.TestCase):
    def test_areas_are_counts_times_the_pixel_area(self):
        layer = make_layer(pixel_area_mm2=0.25)
        self.assertAlmostEqual(layer.canvas_area_mm2, 250.0)
        self.assertAlmostEqual(layer.ink_area_a_mm2, 25.0)
        self.assertAlmostEqual(layer.ink_area_b_mm2, 27.5)
        self.assertAlmostEqual(layer.area_differing_mm2, 7.5)
        self.assertAlmostEqual(layer.area_only_in_a_mm2, 2.5)
        self.assertAlmostEqual(layer.area_only_in_b_mm2, 5.0)

    def test_share_of_canvas_and_share_of_ink_differ(self):
        layer = make_layer()
        self.assertAlmostEqual(layer.share_of_canvas, 0.03)
        # Ink in either side: 100 + 110 - 90 both = 120.
        self.assertAlmostEqual(layer.share_of_ink, 30 / 120)

    def test_share_of_ink_on_two_blank_layers_is_zero_not_an_error(self):
        blank = make_layer(
            ink_pixels_a=0,
            ink_pixels_b=0,
            pixels_differing=0,
            pixels_only_in_a=0,
            pixels_only_in_b=0,
        )
        self.assertEqual(blank.share_of_ink, 0.0)
        self.assertEqual(blank.share_of_canvas, 0.0)

    def test_only_in_a_plus_only_in_b_is_the_whole_difference(self):
        layer = make_layer()
        self.assertEqual(
            layer.pixels_only_in_a + layer.pixels_only_in_b,
            layer.pixels_differing,
        )


class ThresholdIsPresentationTest(unittest.TestCase):
    """The verdict is derived from the counts; it never replaces them."""

    def test_default_threshold_reports_any_difference(self):
        self.assertTrue(make_layer(pixels_differing=1).over_threshold)
        self.assertFalse(make_layer(pixels_differing=0).over_threshold)

    def test_a_raised_threshold_only_changes_the_verdict(self):
        layer = make_layer(pixels_differing=30, threshold=30)
        self.assertFalse(layer.over_threshold)
        self.assertEqual(layer.pixels_differing, 30)
        self.assertAlmostEqual(layer.area_differing_mm2, 30.0)
        self.assertTrue(make_layer(pixels_differing=31, threshold=30).over_threshold)


class RegionStatsTest(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas.over(Bounds(0.0, 0.0, 25.4, 12.7), 254.0)

    def test_region_area_is_its_pixel_count_not_its_bounding_box(self):
        region = self.canvas.pixel_box_to_mm(PixelBox(0, 0, 9, 9, 20))
        self.assertAlmostEqual(region.area_mm2, 20 * 0.254 * 0.254)
        self.assertAlmostEqual(region.bbox_area_mm2, (10 * 0.254) ** 2)
        self.assertLess(region.area_mm2, region.bbox_area_mm2)

    def test_largest_region_is_the_one_with_the_most_pixels(self):
        small = self.canvas.pixel_box_to_mm(PixelBox(0, 0, 1, 1, 3))
        large = self.canvas.pixel_box_to_mm(PixelBox(50, 50, 60, 60, 40))
        layer = make_layer(regions=(small, large))
        self.assertEqual(layer.region_count, 2)
        self.assertIs(layer.largest_region, large)

    def test_a_clean_layer_has_no_largest_region(self):
        self.assertIsNone(make_layer(pixels_differing=0).largest_region)


class ExtensibilityTest(unittest.TestCase):
    """The model must grow without breaking callers."""

    def test_construction_is_keyword_only(self):
        with self.assertRaises(TypeError):
            LayerDiff("F_Cu", True, True, 1, 1.0, 0, 0, 0, 0, 0)

    def test_metrics_carries_a_statistic_that_has_no_field_yet(self):
        layer = make_layer(metrics={"hausdorff_mm": 0.21})
        self.assertEqual(layer.metrics["hausdorff_mm"], 0.21)
        self.assertEqual(layer.to_dict()["metrics"], {"hausdorff_mm": 0.21})

    def test_metrics_default_is_not_shared_between_instances(self):
        first, second = make_layer(), make_layer()
        first.metrics["scratch"] = 1
        self.assertEqual(second.metrics, {})

    def test_to_dict_carries_every_measurement(self):
        data = make_layer().to_dict()
        for key in (
            "pixels_differing",
            "area_differing_mm2",
            "ink_area_a_mm2",
            "ink_area_b_mm2",
            "pixels_only_in_a",
            "pixels_only_in_b",
            "share_of_ink",
            "region_count",
            "regions",
            "threshold",
            "over_threshold",
        ):
            self.assertIn(key, data)


class CompareResultTest(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas.over(Bounds(0.0, 0.0, 25.4, 12.7), 254.0)

    def result(self, layers) -> CompareResult:
        return CompareResult(
            canvas=self.canvas,
            um_per_pixel=254.0,
            threshold=0,
            region_cell_mm=1.0,
            layers=tuple(layers),
            dir_a=Path("a"),
            dir_b=Path("b"),
        )

    def test_totals_sum_over_layers(self):
        result = self.result(
            [
                make_layer(layer="F_Cu", pixels_differing=30, pixel_area_mm2=0.5),
                make_layer(layer="B_Cu", pixels_differing=12, pixel_area_mm2=0.5),
            ]
        )
        self.assertEqual(result.pixels_differing, 42)
        self.assertAlmostEqual(result.area_differing_mm2, 21.0)

    def test_clean_means_nothing_over_threshold_not_nothing_measured(self):
        clean = self.result([make_layer(pixels_differing=0)])
        self.assertTrue(clean.clean)
        self.assertEqual(clean.layers_over_threshold, [])

        tolerated = self.result([make_layer(pixels_differing=5, threshold=10)])
        self.assertTrue(tolerated.clean)
        self.assertEqual(tolerated.pixels_differing, 5)

    def test_layers_over_threshold_names_the_offenders(self):
        result = self.result(
            [
                make_layer(layer="F_Cu", pixels_differing=0),
                make_layer(layer="B_Cu", pixels_differing=7),
            ]
        )
        self.assertFalse(result.clean)
        self.assertEqual(
            [layer.layer for layer in result.layers_over_threshold], ["B_Cu"]
        )

    def test_to_dict_is_json_shaped(self):
        import json

        data = self.result([make_layer()]).to_dict()
        self.assertEqual(
            json.loads(json.dumps(data))["um_per_pixel"], 254.0
        )


if __name__ == "__main__":
    unittest.main()
