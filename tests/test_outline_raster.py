"""The raster oracle's own parts: what it writes, and what it erodes.

The oracle as a whole is exercised in `test_outline_oracle.py`, where it judges
real panels. What is pinned here is the arithmetic underneath it - the SVG a
curve becomes, the canvas a blank becomes, and the erosion that tells a
rendering seam from a defect - because those are the pieces that could be
quietly wrong while every picture still looked plausible.
"""

import math
import unittest

from eedesignpanels.channels import Rect
from eedesignpanels.toolpath import Arc, Line

try:
    from PIL import Image

    from eedesignpanels.verify import outline_raster
    AVAILABLE = True
except ImportError:  # pragma: no cover - the harness is dev-side only
    AVAILABLE = False


def line(x0, y0, x1, y1) -> Line:
    return Line(p0=(x0, y0), p1=(x1, y1), source="t")


@unittest.skipUnless(AVAILABLE, "no rasterizer in this interpreter")
class PathTest(unittest.TestCase):
    def test_a_chain_of_lines_becomes_a_closed_path(self):
        data = outline_raster.path_of([
            line(0.0, 0.0, 1.0, 0.0), line(1.0, 0.0, 1.0, 1.0),
        ])
        self.assertTrue(data.startswith("M 0.000000 0.000000"))
        self.assertIn("L 1.000000 0.000000", data)
        self.assertTrue(data.endswith("Z"))

    def test_a_path_can_be_left_open_for_a_stroke(self):
        data = outline_raster.path_of([line(0.0, 0.0, 1.0, 0.0)], close=False)
        self.assertFalse(data.endswith("Z"))

    def test_an_empty_chain_writes_nothing(self):
        self.assertEqual(outline_raster.path_of([]), "")

    def test_a_quarter_turn_is_one_arc_command_ending_where_it_should(self):
        arc = Arc(center=(0.0, 0.0), radius=2.0, a0=0.0, a1=math.pi / 2.0,
                  ccw=True, source="a")
        data = outline_raster.arc_commands(arc)
        self.assertEqual(data.count("A "), 1)
        self.assertIn("A 2.000000 2.000000 0 0 1", data)
        self.assertTrue(data.endswith("0.000000 2.000000"))

    def test_a_clockwise_arc_takes_the_other_sweep_flag(self):
        arc = Arc(center=(0.0, 0.0), radius=2.0, a0=math.pi / 2.0, a1=0.0,
                  ccw=False, source="a")
        self.assertIn("0 0 0", outline_raster.arc_commands(arc))

    def test_a_turn_over_a_half_circle_is_drawn_in_two(self):
        arc = Arc(center=(0.0, 0.0), radius=1.0, a0=0.0, a1=1.9 * math.pi,
                  ccw=True, source="a")
        data = outline_raster.arc_commands(arc)
        self.assertEqual(data.count("A "), 2)

    def test_every_arc_command_lands_on_the_arc(self):
        for sweep in (0.3, 1.0, math.pi, 1.9 * math.pi):
            arc = Arc(center=(3.0, -2.0), radius=1.5, a0=0.4, a1=0.4 + sweep,
                      ccw=True, source="a")
            data = outline_raster.arc_commands(arc)
            last = data.split()[-2:]
            point = (float(last[0]), float(last[1]))
            self.assertAlmostEqual(math.dist(point, arc.end), 0.0, places=5)


@unittest.skipUnless(AVAILABLE, "no rasterizer in this interpreter")
class DocumentTest(unittest.TestCase):
    BLANK = Rect(5.0, 5.0, 25.0, 15.0)

    def test_the_canvas_covers_the_blank_and_its_margin(self):
        canvas = outline_raster.canvas_over(self.BLANK, 10.0)
        self.assertAlmostEqual(canvas.bounds.min_x_mm, 4.0)
        self.assertAlmostEqual(canvas.bounds.max_y_mm, 16.0)
        self.assertEqual(canvas.width_px, 2200)
        self.assertEqual(canvas.height_px, 1200)

    def test_the_page_is_sized_in_millimetres_and_viewboxed_the_same(self):
        canvas = outline_raster.canvas_over(self.BLANK, 10.0)
        page = outline_raster.document(canvas.bounds, "")
        self.assertIn('width="22.000000mm"', page)
        self.assertIn('viewBox="4.000000 4.000000 22.000000 12.000000"', page)

    def test_the_gerber_bounds_are_the_canvas_mirrored_in_y(self):
        canvas = outline_raster.canvas_over(self.BLANK, 10.0)
        low, high = outline_raster.gerber_bounds(canvas)
        self.assertAlmostEqual(low[1], -canvas.bounds.max_y_mm)
        self.assertAlmostEqual(high[1], -canvas.bounds.min_y_mm)

    def test_a_filled_outline_and_a_stroked_toolpath_differ_as_they_should(self):
        canvas = outline_raster.canvas_over(self.BLANK, 20.0)
        loop = [
            line(8.0, 8.0, 12.0, 8.0), line(12.0, 8.0, 12.0, 12.0),
            line(12.0, 12.0, 8.0, 12.0), line(8.0, 12.0, 8.0, 8.0),
        ]
        from eedesignpanels.verify import raster
        filled = raster.rasterize_svg(
            outline_raster.filled_svg(canvas.bounds, [loop]), canvas)
        stroked = raster.rasterize_svg(
            outline_raster.stroked_svg(canvas.bounds,
                                       [(line(8.0, 10.0, 12.0, 10.0), 1.0)]),
            canvas)
        # A 4 x 4 mm square, and a 4 mm pass of a 2 mm bit: 16 mm2 against
        # 4 x 2 plus a circle of radius 1.
        area = raster.count_ink(filled) * canvas.pixel_area_mm2
        self.assertAlmostEqual(area, 16.0, places=1)
        swept = raster.count_ink(stroked) * canvas.pixel_area_mm2
        self.assertAlmostEqual(swept, 8.0 + math.pi, places=1)

    def test_the_blank_fills_exactly_its_own_rectangle(self):
        from eedesignpanels.verify import raster
        canvas = outline_raster.canvas_over(self.BLANK, 20.0)
        image = raster.rasterize_svg(
            outline_raster.blank_svg(canvas.bounds, self.BLANK), canvas)
        area = raster.count_ink(image) * canvas.pixel_area_mm2
        self.assertAlmostEqual(area, 200.0, places=1)

    def test_the_instances_land_where_the_placements_say(self):
        from eedesignpanels.verify import raster
        canvas = outline_raster.canvas_over(self.BLANK, 20.0)
        loop = [
            line(6.0, 6.0, 8.0, 6.0), line(8.0, 6.0, 8.0, 8.0),
            line(8.0, 8.0, 6.0, 8.0), line(6.0, 8.0, 6.0, 6.0),
        ]
        image = raster.rasterize_svg(
            outline_raster.instances_svg(
                canvas.bounds, [loop], [(0.0, 0.0), (10.0, 5.0)]),
            canvas)
        area = raster.count_ink(image) * canvas.pixel_area_mm2
        self.assertAlmostEqual(area, 8.0, places=1)


@unittest.skipUnless(AVAILABLE, "no rasterizer in this interpreter")
class ErosionTest(unittest.TestCase):
    def image(self, width, height, box):
        image = Image.new("1", (width, height), 0)
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                image.putpixel((x, y), 255)
        return image

    def test_a_blob_smaller_than_the_radius_disappears(self):
        thin = self.image(40, 40, (10, 19, 30, 21))
        self.assertEqual(
            outline_raster.erode(thin, 3).histogram()[255], 0
        )

    def test_a_blob_bigger_than_it_survives_and_shrinks(self):
        fat = self.image(40, 40, (10, 10, 30, 30))
        left = outline_raster.erode(fat, 3)
        self.assertGreater(left.histogram()[255], 0)
        box = left.getbbox()
        self.assertEqual(box, (13, 13, 27, 27))

    def test_eroding_by_nothing_changes_nothing(self):
        fat = self.image(20, 20, (5, 5, 15, 15))
        self.assertEqual(outline_raster.erode(fat, 0).tobytes(), fat.tobytes())


if __name__ == "__main__":
    unittest.main()
