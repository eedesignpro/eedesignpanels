"""Canvas arithmetic and region clustering are pure, so plain values check them.

Alignment is the failure this suite exists to catch: if the pixel grid or the
millimetre mapping drifts, two identical gerber sets read as different
everywhere and the harness becomes noise.
"""

import unittest

from eedesignpanels.verify.regions import (
    Bounds,
    Canvas,
    PixelBox,
    cluster_pixels,
    set_bit_pixels,
    union_bounds,
)


class BoundsTest(unittest.TestCase):
    def test_extent(self):
        box = Bounds(1.0, 2.0, 4.5, 8.0)
        self.assertAlmostEqual(box.width_mm, 3.5)
        self.assertAlmostEqual(box.height_mm, 6.0)

    def test_union_takes_the_outer_edge_of_every_box(self):
        self.assertEqual(
            union_bounds(
                [
                    Bounds(0.0, 0.0, 10.0, 5.0),
                    Bounds(-2.0, 1.0, 4.0, 9.0),
                    Bounds(3.0, -1.0, 6.0, 2.0),
                ]
            ),
            Bounds(-2.0, -1.0, 10.0, 9.0),
        )

    def test_union_of_one_box_is_that_box(self):
        box = Bounds(1.0, 2.0, 3.0, 4.0)
        self.assertEqual(union_bounds([box]), box)

    def test_union_of_nothing_is_an_error(self):
        with self.assertRaises(ValueError):
            union_bounds([])


class CanvasTest(unittest.TestCase):
    def setUp(self):
        # 25.4 x 12.7 mm at 254 um per pixel is exactly 100 x 50 px, so every
        # expectation below is an integer and nothing hides in rounding.
        self.canvas = Canvas.over(Bounds(10.0, -20.0, 35.4, -7.3), 254.0)

    def test_pixel_dimensions_follow_the_resolution(self):
        self.assertEqual(self.canvas.width_px, 100)
        self.assertEqual(self.canvas.height_px, 50)
        self.assertEqual(self.canvas.total_pixels, 5000)
        self.assertAlmostEqual(self.canvas.mm_per_pixel, 0.254)
        self.assertAlmostEqual(self.canvas.pixel_area_mm2, 0.254 * 0.254)

    def test_micrometres_per_pixel_is_the_adjustable_knob(self):
        finer = Canvas.over(self.canvas.bounds, 127.0)
        self.assertEqual(finer.width_px, 200)
        self.assertEqual(finer.height_px, 100)
        self.assertAlmostEqual(finer.mm_per_pixel, 0.127)

    def test_the_default_five_micrometres_is_exactly_5080_dpi(self):
        # The one place dots per inch is allowed to appear: the rasterizer's
        # document is in millimetres and takes its scale in DPI.
        self.assertAlmostEqual(Canvas.over(self.canvas.bounds, 5.0).dpi, 5080.0)
        self.assertAlmostEqual(self.canvas.dpi, 100.0)

    def test_a_degenerate_canvas_is_rejected(self):
        with self.assertRaises(ValueError):
            Canvas.over(Bounds(1.0, 1.0, 1.0, 2.0), 254.0)
        with self.assertRaises(ValueError):
            Canvas.over(Bounds(1.0, 1.0, 2.0, 2.0), 0.0)

    def test_top_left_pixel_is_the_top_left_millimetre_corner(self):
        region = self.canvas.pixel_box_to_mm(PixelBox(0, 0, 0, 0, 1))
        self.assertAlmostEqual(region.min_x_mm, 10.0)
        self.assertAlmostEqual(region.max_x_mm, 10.254)
        # Image row 0 is the largest gerber Y: the render flips the axis.
        self.assertAlmostEqual(region.max_y_mm, -7.3)
        self.assertAlmostEqual(region.min_y_mm, -7.554)

    def test_bottom_right_pixel_reaches_the_far_corner(self):
        region = self.canvas.pixel_box_to_mm(PixelBox(99, 49, 99, 49, 1))
        self.assertAlmostEqual(region.max_x_mm, 35.4)
        self.assertAlmostEqual(region.min_y_mm, -20.0)

    def test_a_box_spans_whole_pixels(self):
        region = self.canvas.pixel_box_to_mm(PixelBox(10, 4, 12, 9, 7))
        self.assertAlmostEqual(region.width_mm, 3 * 0.254)
        self.assertAlmostEqual(region.height_mm, 6 * 0.254)
        self.assertEqual(region.pixels, 7)

    def test_cell_size_never_collapses_below_one_pixel(self):
        self.assertEqual(self.canvas.cell_size_px(2.54), 10)
        self.assertEqual(self.canvas.cell_size_px(0.0001), 1)
        with self.assertRaises(ValueError):
            self.canvas.cell_size_px(0.0)


class ClusterPixelsTest(unittest.TestCase):
    def test_no_pixels_no_regions(self):
        self.assertEqual(cluster_pixels([], 4), [])

    def test_one_pixel_is_one_region(self):
        self.assertEqual(cluster_pixels([(3, 7)], 4), [PixelBox(3, 7, 3, 7, 1)])

    def test_pixels_in_one_cell_share_a_region(self):
        pixels = [(0, 0), (3, 3), (1, 2)]
        self.assertEqual(cluster_pixels(pixels, 4), [PixelBox(0, 0, 3, 3, 3)])

    def test_touching_cells_merge_including_diagonally(self):
        # (0,0), (4,4) and (8,0) sit in cells (0,0), (1,1) and (2,0):
        # the middle one touches both corners, so all three are one region.
        pixels = [(0, 0), (4, 4), (8, 0)]
        self.assertEqual(cluster_pixels(pixels, 4), [PixelBox(0, 0, 8, 4, 3)])

    def test_distant_pixels_stay_separate(self):
        pixels = [(0, 0), (100, 100)]
        self.assertEqual(
            cluster_pixels(pixels, 4),
            [PixelBox(0, 0, 0, 0, 1), PixelBox(100, 100, 100, 100, 1)],
        )

    def test_a_translated_line_reads_as_a_single_region(self):
        # The acceptance-B shape: one thin line and its shifted twin. They are
        # one fault, and a cell wide enough to bridge them says so.
        original = [(10, y) for y in range(20, 60)]
        shifted = [(14, y) for y in range(20, 60)]
        regions = cluster_pixels(original + shifted, 8)
        self.assertEqual(regions, [PixelBox(10, 20, 14, 59, 80)])

    def test_region_bounds_are_of_pixels_not_of_cells(self):
        regions = cluster_pixels([(9, 9), (10, 10)], 8)
        self.assertEqual(regions, [PixelBox(9, 9, 10, 10, 2)])

    def test_order_is_stable_top_to_bottom_then_left_to_right(self):
        pixels = [(200, 200), (0, 100), (50, 100), (0, 0)]
        regions = cluster_pixels(pixels, 4)
        self.assertEqual(
            [(box.min_px, box.min_py) for box in regions],
            [(0, 0), (0, 100), (50, 100), (200, 200)],
        )
        self.assertEqual(cluster_pixels(reversed(pixels), 4), regions)

    def test_cell_size_must_be_at_least_one_pixel(self):
        with self.assertRaises(ValueError):
            cluster_pixels([(0, 0)], 0)


class SetBitPixelsTest(unittest.TestCase):
    """Pillow's mode "1" packing: rows padded to a byte, most significant bit
    leftmost. Getting this wrong mirrors or shears every reported region."""

    def test_single_row_reads_left_to_right(self):
        self.assertEqual(
            list(set_bit_pixels(bytes([0b10100001]), 8, 1)),
            [(0, 0), (2, 0), (7, 0)],
        )

    def test_rows_are_padded_to_a_byte_boundary(self):
        # 12 px wide is 2 bytes per row with 4 padding bits.
        packed = bytes([0b10000000, 0b00010000, 0b00000001, 0b00000000])
        self.assertEqual(list(set_bit_pixels(packed, 12, 2)), [(0, 0), (11, 0), (7, 1)])

    def test_padding_bits_are_never_yielded(self):
        packed = bytes([0b00000000, 0b00001111])
        self.assertEqual(list(set_bit_pixels(packed, 12, 1)), [])

    def test_empty_bitmap_yields_nothing(self):
        self.assertEqual(list(set_bit_pixels(bytes(4), 8, 4)), [])

    def test_wrong_length_is_an_error(self):
        with self.assertRaises(ValueError):
            list(set_bit_pixels(bytes(3), 8, 4))


if __name__ == "__main__":
    unittest.main()
