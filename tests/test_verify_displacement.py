"""The displacement metric: the number that now decides a verdict.

Total differing area answers "how much differs", which
grows with how long an edge is; a sub-micrometre rendering sliver traced right
around an outline can carry more of it than a genuinely moved track. This
metric answers "how far did it move", which is the question, and these tests
pin it on shapes whose answer can be worked out by hand.
"""

import unittest

from eedesignpanels.verify.regions import (
    Bounds,
    Canvas,
    PixelBox,
    Region,
    band_metrics,
    cluster_regions,
)
from eedesignpanels.verify.results import LayerDiff

CANVAS = Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 5.0)  # 5 um per pixel


def band(length, width, x0=0, y0=0):
    """A solid rectangle of pixels - a band of known length and width."""
    return [(x0 + x, y0 + y) for x in range(length) for y in range(width)]


class BandMetricsTest(unittest.TestCase):
    def test_a_single_pixel_has_four_edges_and_is_one_thick(self):
        self.assertEqual(band_metrics([(0, 0)]), (4, 1))

    def test_a_thin_band_reports_its_own_width(self):
        # 20 x 1: perimeter 2*(20+1) = 42, widest 1.
        self.assertEqual(band_metrics(band(20, 1)), (42, 1))

    def test_a_thicker_band_reports_the_thicker_width(self):
        # 20 x 3: perimeter 2*(20+3) = 46, widest 3.
        self.assertEqual(band_metrics(band(20, 3)), (46, 3))

    def test_a_square_is_as_thick_as_it_is_wide(self):
        self.assertEqual(band_metrics(band(5, 5)), (20, 5))

    def test_an_empty_set_measures_nothing(self):
        self.assertEqual(band_metrics([]), (0, 0))

    def test_a_hole_counts_as_boundary(self):
        # A 3x3 ring: 8 pixels, outer boundary 12 edges plus 4 around the hole.
        ring = [p for p in band(3, 3) if p != (1, 1)]
        perimeter, widest = band_metrics(ring)
        self.assertEqual(perimeter, 16)
        # The known bias of a run-based estimate, pinned rather than hidden: a
        # corner pixel sits on a 3-long run both ways, so a one-pixel-thick
        # ring reads as 3 thick. It errs toward reporting a fault, which is the
        # right direction for a widest-point estimate.
        self.assertEqual(widest, 3)


class DisplacementTest(unittest.TestCase):
    """2A/P recovers the width of a band, which is what moved."""

    def region(self, pixels):
        box = PixelBox(
            min_px=min(p[0] for p in pixels), min_py=min(p[1] for p in pixels),
            max_px=max(p[0] for p in pixels), max_py=max(p[1] for p in pixels),
            pixels=len(pixels),
        )
        return CANVAS.region_of(box, pixels)

    def test_a_one_pixel_wide_band_displaced_one_pixel(self):
        # 40 px long, 1 px wide, at 5 um per pixel: the geometry moved 5 um.
        region = self.region(band(40, 1))
        self.assertAlmostEqual(region.displacement_mm, 0.00488, places=5)
        self.assertAlmostEqual(region.widest_mm, 0.005, places=9)

    def test_a_longer_band_of_the_same_width_reports_the_same_displacement(self):
        # THE WHOLE POINT: area quadruples, displacement does not move.
        short = self.region(band(40, 1))
        long = self.region(band(160, 1))
        self.assertGreater(long.area_mm2, 3.9 * short.area_mm2)
        # Both are one pixel wide and both read as one pixel wide, to within a
        # tenth of a micrometre - the end caps are all that separate them.
        self.assertAlmostEqual(
            long.displacement_mm, short.displacement_mm, delta=0.0001
        )
        for region in (short, long):
            self.assertLess(region.displacement_mm, CANVAS.mm_per_pixel)

    def test_a_wider_band_reports_a_larger_displacement(self):
        thin = self.region(band(40, 1))
        thick = self.region(band(40, 8))
        self.assertGreater(thick.displacement_mm, 6 * thin.displacement_mm)

    def test_displacement_is_zero_without_a_perimeter(self):
        # The default Region carries no band measurement; it must not divide.
        from eedesignpanels.verify.regions import Region
        bare = Region(0.0, 0.0, 1.0, 1.0, pixels=10, area_mm2=0.1)
        self.assertEqual(bare.displacement_mm, 0.0)

    def test_the_measurements_reach_the_dict(self):
        payload = self.region(band(40, 1)).to_dict()
        for key in ("perimeter_mm", "displacement_mm", "widest_mm"):
            self.assertIn(key, payload)


class RegionOrderTest(unittest.TestCase):
    def test_clustering_returns_each_region_with_its_own_pixels(self):
        pixels = band(3, 1) + band(3, 1, x0=100, y0=100)
        grouped = cluster_regions(pixels, cell_px=10)
        self.assertEqual(len(grouped), 2)
        self.assertEqual(sum(len(members) for _, members in grouped), 6)
        for box, members in grouped:
            self.assertEqual(box.pixels, len(members))

    def test_every_pixel_lands_in_exactly_one_region(self):
        pixels = band(4, 2) + band(4, 2, x0=60, y0=60) + [(200, 200)]
        grouped = cluster_regions(pixels, cell_px=10)
        seen = [p for _, members in grouped for p in members]
        self.assertEqual(sorted(seen), sorted(pixels))


class VerdictTest(unittest.TestCase):
    """The verdict judges the WORST SINGLE REGION, not the totals."""

    def layer(self, regions, limit=0.01, differing=1):
        return LayerDiff(
            layer="Edge_Cuts", present_in_a=True, present_in_b=True,
            total_pixels=1000, pixel_area_mm2=CANVAS.pixel_area_mm2,
            ink_pixels_a=100, ink_pixels_b=100,
            pixels_differing=differing, pixels_only_in_a=differing,
            pixels_only_in_b=0,
            displacement_threshold_mm=limit,
            regions=tuple(regions),
        )

    def make(self, pixels):
        box = PixelBox(
            min_px=min(p[0] for p in pixels), min_py=min(p[1] for p in pixels),
            max_px=max(p[0] for p in pixels), max_py=max(p[1] for p in pixels),
            pixels=len(pixels),
        )
        return CANVAS.region_of(box, pixels)

    def test_a_hair_thin_sliver_of_enormous_area_stays_within_threshold(self):
        # The rendering floor: one pixel wide, going on forever. Its area
        # dwarfs a real fault's and it is still not a fault.
        sliver = self.make(band(4000, 1))
        layer = self.layer([sliver], differing=4000)
        self.assertLess(layer.worst_displacement_mm, 0.01)
        self.assertFalse(layer.over_threshold)

    def test_a_short_thick_region_of_tiny_area_goes_over(self):
        # A real fault: 0.2 mm of movement is 40 px at 5 um, over a short run.
        fault = self.make(band(20, 40))
        layer = self.layer([fault], differing=800)
        self.assertGreater(layer.worst_displacement_mm, 0.01)
        self.assertTrue(layer.over_threshold)

    def test_the_worst_region_decides_even_when_most_are_quiet(self):
        quiet = [self.make(band(50, 1, y0=10 * i)) for i in range(20)]
        fault = self.make(band(20, 40, x0=500, y0=500))
        self.assertFalse(self.layer(quiet).over_threshold)
        self.assertTrue(self.layer(quiet + [fault]).over_threshold)

    def test_the_worst_region_is_found_whatever_order_it_arrives_in(self):
        quiet = self.make(band(50, 1))
        fault = self.make(band(20, 40, x0=500, y0=500))
        self.assertIs(self.layer([quiet, fault]).worst_region, fault)
        self.assertIs(self.layer([fault, quiet]).worst_region, fault)

    def test_a_clean_layer_has_no_displacement_and_no_verdict(self):
        layer = self.layer([], differing=0)
        self.assertEqual(layer.worst_displacement_mm, 0.0)
        self.assertEqual(layer.widest_mm, 0.0)
        self.assertIsNone(layer.worst_region)
        self.assertFalse(layer.over_threshold)

    def test_without_a_displacement_threshold_it_falls_back_to_pixels(self):
        # The bare export-against-export path: nothing was translated there,
        # so any differing pixel at all is news.
        layer = self.layer([self.make(band(4000, 1))], limit=None,
                           differing=4000)
        self.assertTrue(layer.over_threshold)

    def test_the_thresholds_both_reach_the_dict(self):
        payload = self.layer([self.make(band(50, 1))]).to_dict()
        self.assertEqual(payload["displacement_threshold_mm"], 0.01)
        self.assertIn("worst_displacement_mm", payload)
        self.assertIn("widest_mm", payload)


if __name__ == "__main__":
    unittest.main()


class HardAndSoftTest(unittest.TestCase):
    """Two classes of difference.

    No wider than one measuring pixel and it cannot be told from where the
    rasterizer happened to put an edge - the drawing's own rounding. Wider
    than that is a real shape, and its presence has to be visible without
    opening the layer.
    """

    def region(self, widest_px):
        return Region(
            0.0, 0.0, 1.0, 1.0, pixels=10, area_mm2=0.001,
            perimeter_mm=0.4, widest_mm=widest_px * CANVAS.mm_per_pixel,
        )

    def layer(self, *regions):
        return LayerDiff(
            layer="Edge_Cuts", present_in_a=True, present_in_b=True,
            total_pixels=1000, pixel_area_mm2=CANVAS.pixel_area_mm2,
            ink_pixels_a=100, ink_pixels_b=100,
            pixels_differing=10, pixels_only_in_a=10, pixels_only_in_b=0,
            displacement_threshold_mm=0.01, regions=tuple(regions),
        )

    def test_one_pixel_wide_is_soft(self):
        self.assertEqual(self.layer(self.region(1)).hard_regions, [])
        self.assertFalse(self.layer(self.region(1)).has_hard_regions)

    def test_wider_than_one_pixel_is_hard(self):
        self.assertTrue(self.layer(self.region(2)).has_hard_regions)

    def test_exactly_one_pixel_is_not_flagged_by_float_noise(self):
        # widest_mm is a product of floats; the class boundary must not turn
        # on the last bit of it.
        self.assertFalse(self.layer(self.region(1.0)).has_hard_regions)

    def test_the_pixel_width_comes_from_the_measurement_itself(self):
        self.assertAlmostEqual(
            self.layer().pixel_width_mm, CANVAS.mm_per_pixel, places=12)

    def test_a_layer_of_only_soft_regions_holds_none(self):
        soft = self.layer(*(self.region(1) for _ in range(20)))
        self.assertEqual(soft.hard_regions, [])

    def test_one_hard_among_many_soft_still_shows(self):
        mixed = self.layer(*([self.region(1)] * 20 + [self.region(4)]))
        self.assertEqual(len(mixed.hard_regions), 1)
        self.assertTrue(mixed.has_hard_regions)
