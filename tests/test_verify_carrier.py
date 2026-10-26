"""Region-scoped judgment: the boards are judged, the carrier is named.

A panel with rails, tooling holes and a pour carries artwork the reference -
the source board stamped at every placement - can never contain. Judged
literally, such a panel can only fail, and the failure says nothing. So
judgment is scoped to the boards, and everything outside them is measured,
named and set aside.

Four things are pinned here, because each of them failing would be invisible:

* the windows come from the build's own placements and land where the boards
  do, through the gerber Y flip;
* a layer with no carrier artwork is measured **exactly** as before - the
  scoping has to be arithmetically a no-op, or every panel without a carrier
  reports differently for no reason;
* a fault inside an instance still flags on a panel that HAS a carrier, which
  is the whole point of scoping rather than blinding;
* the case the rule says is impossible - the board's own artwork missing from
  outside every board - is measured, judged anyway, and reported loudly.
"""

import unittest

from PIL import Image

from eedesignpanels.placement import Placement
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.verify import carrier, raster
from eedesignpanels.verify.compare import format_summary, measure_layer
from eedesignpanels.verify.panel_diff import LayerJob, scope_difference
from eedesignpanels.verify.regions import Bounds, Canvas
from eedesignpanels.verify.report import DiffRun, build_diff_page, diff_body
from eedesignpanels.verify.results import CompareResult, LayerDiff

# A 10 x 10 mm canvas at 100 um/px: 100 x 100 pixels, small enough to reason
# about by eye and still real millimetres.
CANVAS = Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 100.0)
OUTLINE = Bounds(1.0, 1.0, 3.0, 3.0)


def placements(*cells) -> list:
    return [
        Placement(column=column, row=row, dx_mm=dx, dy_mm=dy,
                  rotation_deg=0.0, mirror=False)
        for column, row, dx, dy in cells
    ]


def painted(boxes) -> Image.Image:
    """A 1-bit image with those millimetre rectangles inked."""
    return raster.mask_over(CANVAS, boxes)


class InstanceWindowTest(unittest.TestCase):
    """Where each board is, taken from the build's own placement data."""

    def test_one_window_per_placement(self):
        windows = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0), (1, 0, 4.0, 0.0)), 1.0
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual([(w.column, w.row) for w in windows], [(0, 0), (1, 0)])

    def test_the_board_box_is_the_outline_moved_to_the_placement(self):
        window = carrier.instance_windows(
            OUTLINE, placements((1, 0, 4.0, 0.0)), 0.0
        )[0]
        self.assertEqual(window.board, Bounds(5.0, 1.0, 7.0, 3.0))

    def test_a_placement_moving_down_the_board_moves_up_in_gerber_space(self):
        # Gerber Y is the board's Y negated. Getting this backwards would put
        # every window on the mirror image of where the boards are, and the
        # scoping would then excuse the boards and judge the carrier.
        window = carrier.instance_windows(
            OUTLINE, placements((0, 1, 0.0, 2.5)), 0.0
        )[0]
        self.assertEqual(window.board, Bounds(1.0, -1.5, 3.0, 0.5))

    def test_the_window_is_the_board_plus_the_margin_on_every_side(self):
        window = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0)), 0.5
        )[0]
        self.assertEqual(window.window, Bounds(0.5, 0.5, 3.5, 3.5))

    def test_a_negative_margin_is_refused(self):
        # It would shrink the window and drop instance artwork out of judgment
        # without saying so.
        with self.assertRaises(ValueError):
            carrier.instance_windows(
                OUTLINE, placements((0, 0, 0.0, 0.0)), -0.1
            )

    def test_the_block_spans_every_window(self):
        windows = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0), (1, 1, 4.0, 2.5)), 0.5
        )
        self.assertEqual(carrier.block_of(windows), Bounds(0.5, -2.0, 7.5, 3.5))

    def test_a_panel_with_no_instances_has_no_block(self):
        with self.assertRaises(ValueError):
            carrier.block_of(())

    def test_boards_and_windows_come_back_in_placement_order(self):
        windows = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0), (1, 0, 4.0, 0.0)), 0.5
        )
        self.assertEqual(
            carrier.boards_of(windows),
            (Bounds(1.0, 1.0, 3.0, 3.0), Bounds(5.0, 1.0, 7.0, 3.0)),
        )
        self.assertEqual(
            [box.min_x_mm for box in carrier.windows_of(windows)], [0.5, 4.5]
        )


class CanvasBoxTest(unittest.TestCase):
    """Millimetre rectangles onto the measurement's own pixel lattice."""

    def test_a_box_becomes_the_pixels_it_covers(self):
        self.assertEqual(
            CANVAS.mm_box_to_pixels(Bounds(0.0, 9.0, 1.0, 10.0)),
            (0, 0, 9, 9),
        )

    def test_it_is_the_inverse_of_the_pixel_mapping(self):
        # The scoping draws its rectangles on the lattice the measurement is
        # taken on; a half-pixel drift here would scope the wrong pixels.
        from eedesignpanels.verify.regions import PixelBox

        box = PixelBox(12, 34, 56, 78, 1)
        region = CANVAS.pixel_box_to_mm(box)
        back = CANVAS.mm_box_to_pixels(
            Bounds(region.min_x_mm, region.min_y_mm,
                   region.max_x_mm, region.max_y_mm)
        )
        self.assertEqual(back, (12, 34, 56, 78))

    def test_a_box_off_the_canvas_is_nothing_rather_than_an_error(self):
        self.assertIsNone(CANVAS.mm_box_to_pixels(Bounds(20.0, 20.0, 30.0, 30.0)))

    def test_a_box_hanging_over_the_edge_is_clipped(self):
        self.assertEqual(
            CANVAS.mm_box_to_pixels(Bounds(-5.0, 9.0, 1.0, 15.0)), (0, 0, 9, 9)
        )


class MaskTest(unittest.TestCase):
    def test_a_mask_inks_exactly_the_boxes_it_is_given(self):
        mask = raster.mask_over(CANVAS, [Bounds(0.0, 9.0, 1.0, 10.0)])
        self.assertEqual(raster.count_ink(mask), 100)

    def test_the_mask_is_drawn_at_full_ink(self):
        # Pillow's one-bit images hold 0 or 255, and a rectangle drawn with
        # fill=1 lands as a literal 1: the logical operators read it as set
        # and count_ink does not, so every measurement taken from such a mask
        # comes back zero. Measured on Pillow 12.3. This test pins it.
        mask = raster.mask_over(CANVAS, [Bounds(0.0, 9.0, 1.0, 10.0)])
        self.assertEqual(mask.getpixel((0, 0)), 255)

    def test_boxes_off_the_canvas_ink_nothing_and_do_not_raise(self):
        mask = raster.mask_over(CANVAS, [Bounds(50.0, 50.0, 60.0, 60.0)])
        self.assertEqual(raster.count_ink(mask), 0)


class LayerKindTest(unittest.TestCase):
    """What a layer is, read off the layer itself - never off a stage name."""

    def test_the_kinds_that_carry_a_panel_carrier(self):
        self.assertEqual(carrier.layer_kind("F_Cu"), "copper")
        self.assertEqual(carrier.layer_kind("In3_Cu"), "copper")
        self.assertEqual(carrier.layer_kind("B_Mask"), "solder mask")
        self.assertEqual(carrier.layer_kind("F_Paste"), "solder paste")
        self.assertEqual(carrier.layer_kind("Edge_Cuts"), "outline")
        self.assertEqual(carrier.layer_kind("User_38-VScore"), "score lines")
        self.assertEqual(carrier.layer_kind("F_Silkscreen"), "silkscreen")

    def test_a_layer_it_cannot_name_is_called_artwork_rather_than_guessed(self):
        self.assertEqual(carrier.layer_kind("User_36-RouteToolPath"), "artwork")

    def test_the_key_comes_from_the_layer_table(self):
        # The gerber key is kicad-cli's: the display name, dots to underscores.
        spec = LayerSpec(0, "F.Cu", "signal", "F.Cu")
        self.assertEqual(carrier.layer_kind(spec.svg_suffix), "copper")


class DescribeTest(unittest.TestCase):
    """The name a reader gets, derived from the layer and from where it sits."""

    BLOCK = Bounds(0.0, 0.0, 20.0, 20.0)
    RAIL = (Bounds(-5.0, -5.0, 25.0, 25.0),)
    BETWEEN = (Bounds(9.0, 2.0, 11.0, 18.0),)
    HOLES = tuple(
        Bounds(x, y, x + 3.2, y + 3.2)
        for x, y in ((-4.0, -4.0), (21.0, -4.0), (-4.0, 21.0), (21.0, 21.0))
    )

    def test_a_pour_on_the_rails_is_rail_copper(self):
        self.assertEqual(
            carrier.describe("F_Cu", self.RAIL, self.BLOCK),
            "carrier artwork: rail copper",
        )

    def test_copper_that_never_leaves_the_block_is_named_for_where_it_is(self):
        self.assertIn(
            "between the boards",
            carrier.describe("In1_Cu", self.BETWEEN, self.BLOCK),
        )

    def test_small_square_openings_on_a_mask_read_as_tooling_holes(self):
        said = carrier.describe("B_Mask", self.HOLES, self.BLOCK)
        self.assertIn("solder mask openings", said)
        self.assertIn("4 tooling holes", said)
        self.assertIn("on the rails", said)

    def test_the_count_comes_from_the_features_and_is_not_assumed(self):
        self.assertIn(
            "2 tooling holes",
            carrier.describe("F_Mask", self.HOLES[:2], self.BLOCK),
        )

    def test_a_long_feature_is_not_called_a_hole(self):
        long_one = (Bounds(-5.0, -5.0, 25.0, -1.0),)
        self.assertNotIn(
            "tooling", carrier.describe("F_Mask", long_one, self.BLOCK)
        )

    def test_the_panel_outline_and_its_scores_say_what_they_are(self):
        self.assertEqual(
            carrier.describe("Edge_Cuts", self.RAIL, self.BLOCK),
            "carrier artwork: the panel's own outline",
        )
        self.assertIn(
            "score lines",
            carrier.describe("User_38-VScore", self.RAIL, self.BLOCK),
        )

    def test_every_reason_says_carrier_artwork_first(self):
        # The page and the log both lead with this phrase, so a reader who
        # reads three words already knows nothing was hidden from them.
        for key in ("F_Cu", "B_Mask", "Edge_Cuts", "User_38-VScore", "F_Fab"):
            self.assertTrue(
                carrier.describe(key, self.RAIL, self.BLOCK).startswith(
                    "carrier artwork: "
                ),
                key,
            )


class CarrierInkTest(unittest.TestCase):
    def measurement(self, only_expected=0) -> carrier.CarrierInk:
        return carrier.CarrierInk(
            ink_pixels=400, differing_pixels=500,
            only_expected_pixels=only_expected,
            reason="carrier artwork: rail copper",
            pixel_area_mm2=0.01,
        )

    def test_areas_are_the_counts_in_real_millimetres(self):
        measured = self.measurement()
        self.assertAlmostEqual(measured.ink_area_mm2, 4.0)
        self.assertAlmostEqual(measured.area_differing_mm2, 5.0)

    def test_nothing_missing_outside_a_board_means_nothing_leaked(self):
        self.assertFalse(self.measurement().leaked)

    def test_missing_artwork_outside_a_board_is_a_leak(self):
        self.assertTrue(self.measurement(only_expected=7).leaked)

    def test_the_measurements_survive_into_the_mapping(self):
        payload = self.measurement(only_expected=7).to_dict()
        self.assertEqual(payload["ink_pixels"], 400)
        self.assertAlmostEqual(payload["ink_area_mm2"], 4.0)
        self.assertIs(payload["leaked"], True)
        self.assertIn("rail copper", payload["reason"])

    def test_the_loud_line_says_it_is_impossible_and_still_judged(self):
        said = carrier.leak_warning("F_Cu", self.measurement(only_expected=7))
        self.assertIn("IMPOSSIBLE", said)
        self.assertIn("F_Cu", said)
        self.assertIn("judged", said)


class ScopeDifferenceTest(unittest.TestCase):
    """The split itself, on pictures small enough to read.

    One board window at 1..3 mm with a 0.5 mm margin, on a 10 mm canvas: the
    board is the middle, the ring round it is the fabrication margin, and the
    rest is carrier.
    """

    def setUp(self):
        self.windows = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0)), 0.5
        )

    def split(self, expected_boxes, built_boxes):
        image_a = painted(expected_boxes)
        image_b = painted(built_boxes)
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        return image_a, image_b, difference, judged, measured

    def test_with_no_windows_nothing_is_scoped_at_all(self):
        image = painted([Bounds(1.0, 1.0, 2.0, 2.0)])
        blank = raster.blank(CANVAS)
        difference = raster.xor(image, blank)
        judged, measured = scope_difference(
            "F_Cu", image, blank, difference, CANVAS, ()
        )
        self.assertIs(judged, difference)
        self.assertIsNone(measured)

    def test_a_layer_with_no_carrier_artwork_is_untouched(self):
        # The no-op that matters: a panel without a carrier has to report
        # exactly what it reported before there was any such thing.
        _, _, difference, judged, measured = self.split(
            [Bounds(1.0, 1.0, 2.0, 2.0)], []
        )
        self.assertIs(judged, difference)
        self.assertIsNone(measured)

    def test_extra_ink_outside_every_window_is_carrier_and_not_judged(self):
        _, _, difference, judged, measured = self.split(
            [], [Bounds(6.0, 6.0, 8.0, 8.0)]
        )
        self.assertEqual(raster.count_ink(judged), 0)
        self.assertIsNotNone(measured)
        self.assertEqual(measured.ink_pixels, raster.count_ink(difference))
        self.assertEqual(measured.differing_pixels, raster.count_ink(difference))
        self.assertIn("carrier artwork", measured.reason)

    def test_a_fault_inside_a_board_still_flags_with_a_carrier_present(self):
        # THE FENCE. Rail copper on the layer, and a square of the board's own
        # copper missing: the carrier is set aside and the fault is not.
        image_a = painted([Bounds(1.5, 1.5, 2.0, 2.0)])
        image_b = painted([Bounds(6.0, 6.0, 8.0, 8.0)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertEqual(raster.count_ink(judged), 25)   # the 0.5 mm square
        self.assertEqual(measured.ink_pixels, 400)       # the 2 mm carrier
        result = measure_layer(
            layer="F_Cu", image_a=image_a, image_b=image_b,
            difference=judged, canvas=CANVAS, present_in_a=True,
            present_in_b=True, threshold=0, cell_px=CANVAS.cell_size_px(1.0),
            displacement_threshold_mm=0.01, carrier=measured,
        )
        self.assertEqual(result.pixels_differing, 25)
        self.assertEqual(result.pixels_only_in_a, 25)
        self.assertTrue(result.over_threshold)

    def test_extra_ink_inside_a_board_is_judged_even_with_a_carrier(self):
        image_a = raster.blank(CANVAS)
        image_b = painted([Bounds(1.5, 1.5, 2.0, 2.0), Bounds(6.0, 6.0, 8.0, 8.0)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertEqual(raster.count_ink(judged), 25)
        self.assertEqual(measured.ink_pixels, 400)

    def test_the_carrier_reaching_into_the_margin_is_set_aside(self):
        # A rail pour stands off a scored board edge by its copper clearance,
        # which is inside the millimetre of margin. Measured on the corpus
        # panel: 0.635 mm. Judging it would fail every panel with rails.
        image_a = raster.blank(CANVAS)
        image_b = painted([Bounds(3.2, 1.0, 8.0, 3.0)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertEqual(raster.count_ink(judged), 0)
        self.assertGreater(measured.differing_pixels, measured.ink_pixels)

    def test_the_same_ink_in_the_margin_is_judged_when_there_is_no_carrier(self):
        # Same pixels, no carrier artwork on the layer: the margin is judged
        # exactly as it always was.
        image_a = raster.blank(CANVAS)
        image_b = painted([Bounds(3.2, 1.0, 3.4, 3.0)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertIs(judged, difference)
        self.assertIsNone(measured)

    def test_a_speck_outside_the_windows_is_rounding_rather_than_artwork(self):
        # A tenth of a millimetre square is where artwork starts. Measured at
        # 20 um/px so a two-pixel speck - 0.0008 mm2 - is unambiguously under
        # it rather than sitting on the boundary.
        fine = Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 20.0)
        windows = carrier.instance_windows(
            OUTLINE, placements((0, 0, 0.0, 0.0)), 0.5
        )
        image_a = raster.blank(fine)
        image_b = raster.mask_over(fine, [Bounds(6.0, 6.0, 6.04, 6.02)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, fine, windows
        )
        self.assertEqual(raster.count_ink(difference), 2)
        self.assertIs(judged, difference)
        self.assertIsNone(measured)

    def test_where_artwork_starts_is_one_rule_asked_once(self):
        self.assertFalse(carrier.is_artwork(399, 0.000025))
        self.assertTrue(carrier.is_artwork(401, 0.000025))

    def test_missing_artwork_outside_every_window_is_impossible_and_loud(self):
        # The reference holds boards and nothing else, so it can have no ink
        # of its own out in the carrier. If it ever does, it is judged all the
        # same and the run says so.
        image_a = painted([Bounds(6.0, 6.0, 6.5, 6.5)])
        image_b = painted([Bounds(7.0, 7.0, 9.0, 9.0)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertTrue(measured.leaked)
        self.assertEqual(measured.only_expected_pixels, 25)
        # Judged, not excused: the missing 25 pixels are in the judged image.
        self.assertEqual(raster.count_ink(judged), 25)

    def test_the_split_is_exhaustive(self):
        # Every differing pixel is either judged or carrier, never both and
        # never neither: a rule that loses pixels would hide a difference.
        image_a = painted([Bounds(1.5, 1.5, 2.0, 2.0)])
        image_b = painted([Bounds(6.0, 6.0, 8.0, 8.0), Bounds(2.5, 2.5, 2.8, 2.8)])
        difference = raster.xor(image_a, image_b)
        judged, measured = scope_difference(
            "F_Cu", image_a, image_b, difference, CANVAS, self.windows
        )
        self.assertEqual(
            raster.count_ink(judged) + measured.differing_pixels,
            raster.count_ink(difference),
        )
        self.assertEqual(
            raster.count_ink(raster.intersect(judged, raster.outside(
                difference, judged))), 0
        )


class MeasureScopedTest(unittest.TestCase):
    """A scoped difference is measured against itself, not against its box."""

    def test_counts_are_taken_against_the_scoped_difference(self):
        image_a = painted([Bounds(1.0, 1.0, 2.0, 2.0)])
        image_b = painted([Bounds(6.0, 6.0, 7.0, 7.0)])
        difference = raster.xor(image_a, image_b)
        # Judge only the board's half of it. The bounding box of that half is
        # small, but the bounding box of a scoped difference on a real panel
        # spans the carrier too - which is why the counts intersect rather
        # than crop.
        judged = raster.intersect(difference, raster.mask_over(
            CANVAS, [Bounds(0.0, 0.0, 5.0, 5.0)]
        ))
        result = measure_layer(
            layer="F_Cu", image_a=image_a, image_b=image_b,
            difference=judged, canvas=CANVAS, present_in_a=True,
            present_in_b=True, threshold=0, cell_px=CANVAS.cell_size_px(1.0),
        )
        self.assertEqual(result.pixels_differing, 100)
        self.assertEqual(result.pixels_only_in_a, 100)
        self.assertEqual(result.pixels_only_in_b, 0)
        # The ink counts are still the whole layer: what is scoped is the
        # judgment, never the measurement of how much a layer covers.
        self.assertEqual(result.ink_pixels_b, 100)

    def test_an_unscoped_difference_measures_exactly_as_before(self):
        image_a = painted([Bounds(1.0, 1.0, 2.0, 2.0)])
        image_b = painted([Bounds(6.0, 6.0, 7.0, 7.0)])
        difference = raster.xor(image_a, image_b)
        result = measure_layer(
            layer="F_Cu", image_a=image_a, image_b=image_b,
            difference=difference, canvas=CANVAS, present_in_a=True,
            present_in_b=True, threshold=0, cell_px=CANVAS.cell_size_px(1.0),
        )
        self.assertEqual(result.pixels_differing, 200)
        self.assertEqual(result.pixels_only_in_a, 100)
        self.assertEqual(result.pixels_only_in_b, 100)


def carrier_layer(name="F_Cu", reason="carrier artwork: rail copper",
                  only_expected=0) -> LayerDiff:
    return LayerDiff(
        layer=name, present_in_a=True, present_in_b=True,
        total_pixels=10000, pixel_area_mm2=0.01,
        ink_pixels_a=1000, ink_pixels_b=1400,
        pixels_differing=0, pixels_only_in_a=0, pixels_only_in_b=0,
        displacement_threshold_mm=0.01,
        carrier=carrier.CarrierInk(
            ink_pixels=400, differing_pixels=450,
            only_expected_pixels=only_expected, reason=reason,
            pixel_area_mm2=0.01,
        ),
    )


def result_of(*layers) -> CompareResult:
    return CompareResult(
        canvas=CANVAS, um_per_pixel=100.0, threshold=0, region_cell_mm=1.0,
        layers=tuple(layers), dir_a="a", dir_b="b",
    )


class LayerDiffCarrierTest(unittest.TestCase):
    def test_a_layer_with_no_carrier_says_so_and_carries_nothing(self):
        plain = LayerDiff(
            layer="F_Cu", present_in_a=True, present_in_b=True,
            total_pixels=100, pixel_area_mm2=0.01,
            ink_pixels_a=10, ink_pixels_b=10,
            pixels_differing=0, pixels_only_in_a=0, pixels_only_in_b=0,
        )
        self.assertFalse(plain.has_carrier)
        self.assertEqual(plain.carrier_reason, "")
        self.assertEqual(plain.carrier_ink_area_mm2, 0.0)
        self.assertFalse(plain.carrier_leaked)
        self.assertIsNone(plain.to_dict()["carrier"])

    def test_the_carrier_rides_on_the_measurement_with_its_reason(self):
        layer = carrier_layer()
        self.assertTrue(layer.has_carrier)
        self.assertAlmostEqual(layer.carrier_ink_area_mm2, 4.0)
        self.assertAlmostEqual(layer.carrier_area_differing_mm2, 4.5)
        self.assertEqual(layer.carrier_reason, "carrier artwork: rail copper")
        self.assertIn("rail copper", layer.to_dict()["carrier"]["reason"])

    def test_carrier_ink_can_never_make_a_layer_go_over(self):
        # It is not in the judged numbers at all, which is the point: the
        # verdict is computed from what was judged and nothing else.
        self.assertFalse(carrier_layer().over_threshold)

    def test_a_run_lists_its_carrier_layers_and_their_total(self):
        result = result_of(carrier_layer("F_Cu"), carrier_layer("B_Cu"))
        self.assertEqual([layer.layer for layer in result.carrier_layers],
                         ["F_Cu", "B_Cu"])
        self.assertAlmostEqual(result.carrier_ink_area_mm2, 8.0)
        self.assertEqual(result.leaked_layers, [])
        self.assertTrue(result.clean)

    def test_a_leak_is_listed_on_the_run_and_in_the_mapping(self):
        result = result_of(carrier_layer("F_Cu", only_expected=9))
        self.assertEqual([layer.layer for layer in result.leaked_layers],
                         ["F_Cu"])
        self.assertEqual(result.to_dict()["leaked_layers"], ["F_Cu"])


class SummaryTest(unittest.TestCase):
    """The stdout block: what was set aside, how much, and what it is."""

    def test_the_layer_line_names_the_carrier_and_says_it_is_not_judged(self):
        said = format_summary(result_of(carrier_layer()))
        self.assertIn("carrier artwork: rail copper", said)
        self.assertIn("NOT judged", said)

    def test_the_footer_totals_the_carrier_across_layers(self):
        said = format_summary(result_of(carrier_layer("F_Cu"),
                                        carrier_layer("B_Cu")))
        self.assertIn("Carrier artwork on 2 layer(s)", said)
        self.assertIn("never judged", said)

    def test_a_run_with_no_carrier_says_nothing_about_one(self):
        plain = LayerDiff(
            layer="F_Cu", present_in_a=True, present_in_b=True,
            total_pixels=100, pixel_area_mm2=0.01,
            ink_pixels_a=10, ink_pixels_b=10,
            pixels_differing=0, pixels_only_in_a=0, pixels_only_in_b=0,
        )
        self.assertNotIn("arrier", format_summary(result_of(plain)))

    def test_a_leak_is_impossible_to_scroll_past(self):
        said = format_summary(result_of(carrier_layer(only_expected=9)))
        self.assertIn("THE WINDOW RULE IS WRONG SOMEWHERE", said)
        self.assertIn("IMPOSSIBLE", said)


class PageTest(unittest.TestCase):
    """The emitted page: the carrier is named where the reader is looking."""

    def run_with(self, *layers) -> DiffRun:
        return DiffRun(
            name="run", description="a comparison", result=result_of(*layers),
            images={},
        )

    def test_the_layer_section_names_the_carrier_in_designer_language(self):
        body = diff_body([self.run_with(carrier_layer())])
        self.assertIn("carrier artwork: rail copper", body)
        self.assertIn("belongs to the panel itself", body)
        self.assertIn("decides nothing", body)

    def test_the_table_carries_the_carrier_area_in_its_own_column(self):
        body = diff_body([self.run_with(carrier_layer())])
        self.assertIn("carrier mm&sup2;", body)
        self.assertIn("<td class=\"num\">4.000</td>", body)

    def test_the_banner_says_the_panel_carries_its_own_artwork(self):
        body = diff_body([self.run_with(carrier_layer("F_Cu"),
                                        carrier_layer("B_Cu"))])
        self.assertIn("The panel also carries", body)
        self.assertIn("judges", body)

    def test_one_sentence_per_distinct_carrier_not_one_per_layer(self):
        body = diff_body([self.run_with(carrier_layer("F_Cu"),
                                        carrier_layer("B_Cu"),
                                        carrier_layer("In1_Cu"))])
        # The banner names the pour once; the layer sections still carry it
        # three times, one each.
        self.assertEqual(body.count("The panel also carries"), 1)

    def test_the_legend_explains_the_cyan_round_the_boards(self):
        body = diff_body([self.run_with(carrier_layer())])
        self.assertIn("the cyan round the boards is the carrier", body)

    def test_a_page_with_no_carrier_says_nothing_about_one(self):
        plain = LayerDiff(
            layer="F_Cu", present_in_a=True, present_in_b=True,
            total_pixels=100, pixel_area_mm2=0.01,
            ink_pixels_a=10, ink_pixels_b=10,
            pixels_differing=0, pixels_only_in_a=0, pixels_only_in_b=0,
        )
        body = diff_body([self.run_with(plain)])
        self.assertNotIn("carrier artwork", body)
        self.assertNotIn("carriernote", body)

    def test_a_leak_is_shown_on_the_layer_it_happened_on(self):
        body = diff_body([self.run_with(carrier_layer(only_expected=9))])
        self.assertIn("This should not be possible", body)
        self.assertIn("It is judged, not excused", body)

    def test_the_page_stays_self_contained(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            page = build_diff_page(
                [self.run_with(carrier_layer())], Path(folder) / "page.html"
            )
            document = page.read_text(encoding="utf-8")
        self.assertIn("carrier artwork: rail copper", document)
        for forbidden in ("<link", "<script src", "@import", "url(", "fetch("):
            self.assertNotIn(forbidden, document)


class LayerJobTest(unittest.TestCase):
    def test_a_job_carries_the_windows_and_defaults_to_none(self):
        # The pool is spawned on Windows, so a job has to be plain values -
        # and a job written before the scoping existed still has to build.
        job = LayerJob(
            key="F_Cu", source_gerber=None, panel_gerber=None, placed=False,
            present_in_panel=False, canvas=CANVAS, offsets=(), threshold=0,
            displacement_threshold_mm=0.01, judged=True, cell_px=10,
            display_factor=1, vector_max_kb=1.0, vector_max_elements=1,
            blend_path=None,
        )
        self.assertEqual(job.windows, ())


if __name__ == "__main__":
    unittest.main()
