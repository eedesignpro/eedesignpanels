"""The comparison report, in both of the shapes it is asked for.

The panel review's diff artifact drives the report from the shared layer panel
and shows one layer at a time; a comparison-only run - the iteration-2 harness
driver - lists every layer at once. Both are written here so neither form can
be broken while only the other is exercised.
"""

import dataclasses
import re
import tempfile
import unittest
from pathlib import Path

from eedesignpanels.review import layerpanel
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.verify.regions import Bounds, Canvas, Region
from eedesignpanels.verify.report import (
    DiffRun,
    build_diff_page,
    compared_specs,
    diff_body,
    differing_counts,
)
from eedesignpanels.verify.results import CompareResult, LayerDiff

LAYERS = [
    LayerSpec(0, "F.Cu", "signal", "F.Cu"),
    LayerSpec(25, "Edge.Cuts", "user", "Edge.Cuts"),
]


def layer_diff(name, count, ink_a=100, ink_b=100) -> LayerDiff:
    return LayerDiff(
        layer=name, present_in_a=True, present_in_b=True,
        total_pixels=1000, pixel_area_mm2=0.000025,
        ink_pixels_a=ink_a, ink_pixels_b=ink_b,
        pixels_differing=count, pixels_only_in_a=count,
        pixels_only_in_b=0,
    )


def run_from(layers) -> DiffRun:
    return DiffRun(
        name="run",
        description="a comparison",
        result=CompareResult(
            canvas=Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 5.0),
            um_per_pixel=5.0, threshold=0, region_cell_mm=1.0,
            layers=tuple(layers), dir_a="a", dir_b="b",
        ),
        images={},
    )


def run_for(*differing) -> DiffRun:
    layers = tuple(layer_diff(name, count) for name, count in differing)
    return DiffRun(
        name="run",
        description="a comparison",
        result=CompareResult(
            canvas=Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 5.0),
            um_per_pixel=5.0, threshold=0, region_cell_mm=1.0,
            layers=layers, dir_a="a", dir_b="b",
        ),
        images={},
    )


class DiffBodyTest(unittest.TestCase):
    def test_every_layer_section_names_the_layer_it_shows(self):
        body = diff_body([run_for(("F_Cu", 0), ("Edge_Cuts", 5))])
        self.assertIn('data-layer="F_Cu"', body)
        self.assertIn('data-layer="Edge_Cuts"', body)

    def test_single_layer_marks_the_root_so_the_page_can_show_one(self):
        self.assertIn("diffroot single",
                      diff_body([run_for(("F_Cu", 0))], single_layer=True))
        self.assertNotIn("diffroot single", diff_body([run_for(("F_Cu", 0))]))

    def test_an_empty_report_is_refused(self):
        # A blank report reads as "nothing was wrong", which is not the same
        # as "nothing was compared".
        with self.assertRaises(ValueError):
            diff_body([])


class OneSidedLabelTest(unittest.TestCase):
    """Which side a one-sided layer is attributed to.

    The bug this guards - a blank layer announced as "only in the built panel"
    because the panel's export wrote an empty file for it - no longer occurs,
    because the stage injects every layer and nothing is one-sided any more.
    The test stays: it asserts the DIRECTION, which is the part
    that would be wrong if the label is ever rewritten.
    """

    def label_of(self, ink_a, ink_b) -> str:
        run = run_from([layer_diff("F_Cu", 5, ink_a=ink_a, ink_b=ink_b)])
        run = run._replace(label_a="reference", label_b="built panel")
        return diff_body([run])

    def test_content_only_on_the_reference_side_names_the_reference(self):
        body = self.label_of(ink_a=100, ink_b=0)
        self.assertIn("content only in reference", body)
        self.assertNotIn("content only in built panel", body)

    def test_content_only_on_the_panel_side_names_the_panel(self):
        body = self.label_of(ink_a=0, ink_b=100)
        self.assertIn("content only in built panel", body)
        self.assertNotIn("content only in reference", body)

    def test_a_layer_blank_on_both_sides_is_not_called_one_sided(self):
        # The trap: an empty gerber exists on both sides, so file presence
        # would have said "only in B" about a layer with nothing on it.
        self.assertNotIn("content only in", self.label_of(ink_a=0, ink_b=0))

    def test_a_layer_inked_on_both_sides_is_not_called_one_sided(self):
        self.assertNotIn("content only in", self.label_of(ink_a=100, ink_b=100))


class DesignerLanguageTest(unittest.TestCase):
    """No software-development vocabulary on a page a PCB designer reads.

    The page is for someone judging a panel, not for someone maintaining this
    program. Internal record numbers, iteration counts and in-house jargon are
    noise to them, and this is what keeps them out as the wording gets edited.
    """

    FORBIDDEN = (
        "ADR-", "BS-", "iteration", "commit", "harness", "XOR", "gerbonara",
        "verdict", "docstring", "ground rule", "backlog", "threshold",
    )

    def page(self) -> str:
        run = run_from([layer_diff("F_Cu", 5), layer_diff("F_Fab", 0)])
        return diff_body([run], title="Panel check", lede="Everything checked.")

    def test_the_page_carries_no_developer_vocabulary(self):
        page = self.page().lower()
        for term in self.FORBIDDEN:
            self.assertNotIn(term.lower(), page, term)

    def test_the_workaround_is_still_disclosed_in_plain_words(self):
        from eedesignpanels.verify.raster import WORKAROUND
        text = WORKAROUND.lower()
        self.assertIn("workaround", text)
        self.assertIn("precision", text)
        # It says what it means for the reader, not what it patches.
        self.assertNotIn("adr-", text)
        self.assertNotIn("serialization", text)


class LayerPanelContentTest(unittest.TestCase):
    """The panel lists what was compared, with each layer's pixel count."""

    SPECS = [
        LayerSpec(0, "F.Cu", "signal", "F.Cu"),
        LayerSpec(1, "F.Fab", "user", "F.Fab"),
        LayerSpec(2, "Edge.Cuts", "user", "Edge.Cuts"),
    ]

    def result(self):
        return run_from(
            [layer_diff("F_Cu", 12), layer_diff("Edge_Cuts", 0)]
        ).result

    def test_only_compared_layers_reach_the_panel(self):
        # F.Fab was never compared, so it is absent - not greyed, not an
        # informational row. A row that cannot be selected is furniture.
        specs = compared_specs(self.result(), self.SPECS)
        self.assertEqual(
            [spec.canonical_name for spec in specs], ["F.Cu", "Edge.Cuts"]
        )

    def test_board_order_survives_the_filter(self):
        specs = compared_specs(self.result(), self.SPECS)
        self.assertEqual([spec.ordinal for spec in specs], [0, 2])

    def test_each_compared_layer_carries_its_differing_pixel_count(self):
        counts = differing_counts(self.result(), self.SPECS)
        self.assertEqual(counts, {"F.Cu": 12, "Edge.Cuts": 0})

    def test_the_count_reaches_the_row(self):
        rows = layerpanel.rows_html(
            self.SPECS[:1],
            status_by_layer={"F.Cu": layerpanel.STATUS_OK},
            counts_by_layer={"F.Cu": 1234},
        )
        self.assertIn("lp-count", rows)
        self.assertIn("1,234", rows)

    def test_a_layer_with_no_differences_shows_a_muted_zero(self):
        rows = layerpanel.rows_html(
            self.SPECS[:1],
            status_by_layer={"F.Cu": layerpanel.STATUS_OK},
            counts_by_layer={"F.Cu": 0},
        )
        self.assertIn("lp-count zero", rows)


class TableAtTheBottomTest(unittest.TestCase):
    def test_the_full_table_comes_after_the_layer_sections(self):
        # The picture is what the page is for; the table is reference,
        # consulted after it rather than read before it.
        body = diff_body([run_for(("F_Cu", 5))])
        self.assertLess(body.index('section class="layer'), body.index("tablebox"))

    def test_the_table_still_lists_every_layer(self):
        body = diff_body([run_for(("F_Cu", 5), ("F_Fab", 0))])
        table = body[body.index("tablebox"):]
        self.assertIn("F_Cu", table)
        self.assertIn("F_Fab", table)


class ViewerLayoutTest(unittest.TestCase):
    """The viewer is sizeable, and shares the row with the region list."""

    def page(self) -> str:
        run = run_for(("F_Cu", 5))
        run = run._replace(images={"F_Cu": {"blend": "data:image/png;base64,x"}})
        return diff_body([run])

    def test_the_viewer_and_the_list_sit_side_by_side(self):
        body = self.page()
        self.assertIn('class="split"', body)
        self.assertIn('class="viewcol"', body)
        self.assertIn('class="listcol"', body)

    def test_the_viewer_box_is_resizable(self):
        from eedesignpanels.verify.report import DIFF_CSS
        self.assertIn("resize: vertical", DIFF_CSS)
        self.assertIn(".diffroot .viewbox", DIFF_CSS)

    def test_a_plain_wheel_over_the_picture_zooms_with_no_modifier(self):
        # KiCad's convention, and the one this audience already has in its
        # hands. Requiring ctrl was the wrong convention for the reader and
        # read to them as "zoom does not work".
        from eedesignpanels.verify.report import DIFF_JS
        self.assertNotIn("ctrlKey", DIFF_JS)
        self.assertNotIn("metaKey", DIFF_JS)

    def test_zoom_is_bound_to_the_picture_itself(self):
        # Bound to the whole row, a wheel over the difference list zoomed as
        # well, which is not what a wheel outside a picture should do.
        from eedesignpanels.verify.report import DIFF_JS
        wheel = DIFF_JS.index(".addEventListener('wheel'")
        self.assertTrue(DIFF_JS[:wheel].rstrip().endswith("vp"))

    def test_the_default_is_suppressed_only_while_zooming(self):
        # So that a wheel anywhere else on the page scrolls it as usual.
        from eedesignpanels.verify.report import DIFF_JS
        start = DIFF_JS.index(".addEventListener('wheel'")
        handler = DIFF_JS[start:DIFF_JS.index("passive: false", start)]
        self.assertIn("if (!vp.clientWidth) { return; }", handler)
        self.assertLess(handler.index("clientWidth"),
                        handler.index("preventDefault"))

    def test_a_jump_scrolls_only_when_it_must_and_never_smoothly(self):
        # Measured live: a smooth scroll glided the picture about 550 px out
        # from under the cursor after the click, so the thing you were about
        # to point at moved while you were pointing at it.
        from eedesignpanels.verify.report import DIFF_JS
        self.assertIn("behavior: 'instant'", DIFF_JS)
        self.assertNotIn("scrollIntoView({ block: 'center' })", DIFF_JS)
        jump = DIFF_JS[DIFF_JS.index("button.jump"):]
        self.assertIn("showing < seen.height * 0.9", jump)

    def test_the_viewer_suppresses_native_drag_and_selection(self):
        # The guards the board viewer already carried, now on this page too.
        from eedesignpanels.verify.report import DIFF_CSS, DIFF_JS
        self.assertIn("dragstart", DIFF_JS)
        self.assertIn("pointer-events: none", DIFF_CSS)
        self.assertIn("user-select: none", DIFF_CSS)

    def test_the_page_says_how_to_drive_the_viewer(self):
        page = self.page()
        self.assertIn("drag the bottom edge to resize", page)
        self.assertIn("double-click resets the view", page)


class JumpTest(unittest.TestCase):
    """Jumping to a difference has to frame THAT difference.

    A fixed magnification put a sliver-sized difference in the middle of a
    flat field: zooming further changed nothing visible, which reads exactly
    like a broken control. The zoom must come from the region's own size.
    """

    def region_page(self) -> str:
        layer = layer_diff("F_Cu", 5)
        layer = dataclasses.replace(layer, regions=(
            Region(1.0, 1.0, 1.02, 1.015, pixels=5, area_mm2=0.0001,
                   perimeter_mm=0.05, widest_mm=0.01),
        ))
        return diff_body([run_from([layer])])

    def test_the_button_carries_the_regions_size(self):
        body = self.region_page()
        self.assertIn("data-w=", body)
        self.assertIn("data-h=", body)

    def test_the_zoom_is_derived_from_that_size_not_a_constant(self):
        from eedesignpanels.verify.report import DIFF_JS
        jump = DIFF_JS[DIFF_JS.index("button.jump"):]
        self.assertIn("btn.dataset.w", jump)
        self.assertIn("btn.dataset.h", jump)
        self.assertNotIn("view.z = 24", jump)

    def test_the_zoom_is_bounded_at_both_ends(self):
        # Never wider than the whole layer - a jump that zoomed out would be
        # absurd - and never so far in that the picture is one flat field.
        from eedesignpanels.verify.report import DIFF_JS
        frame = DIFF_JS[DIFF_JS.index("frame: function (x, y, w, h) {"):]
        self.assertIn("Math.min(Math.max(want, base[2] * 0.0008), base[2])",
                      frame[:400])
        # ...and the same two ends for a layer that fell back to pixels.
        self.assertIn("Math.min(120, Math.max(fitZoom()", DIFF_JS)

    def test_the_wheel_anchor_is_clamped_into_the_viewport(self):
        # Anchoring a zoom outside the picture slings the content away, and at
        # high magnification the view goes blank in a single notch.
        from eedesignpanels.verify.report import DIFF_JS
        self.assertIn("Math.min(Math.max(e.clientX, r.left), r.right)", DIFF_JS)
        self.assertIn("Math.min(Math.max(e.clientY, r.top), r.bottom)", DIFF_JS)


class InitialRevealTest(unittest.TestCase):
    """A freshly opened page shows a layer without any script running.

    This page gets mailed to people. It has to greet them with a picture
    rather than an empty frame, whatever their browser remembers and whenever
    the script happens to start.
    """

    def test_one_section_is_revealed_in_the_markup(self):
        body = diff_body([run_for(("F_Cu", 5), ("Edge_Cuts", 2))],
                         single_layer=True)
        self.assertEqual(body.count(' shown"'), 1)

    def test_it_is_the_layer_the_panel_also_opens_on(self):
        # Otherwise a fresh open shows one layer and swaps the instant the
        # script runs.
        body = diff_body([run_for(("F_Cu", 5), ("Edge_Cuts", 2))],
                         single_layer=True)
        shown = body[body.index(' shown"'):]
        self.assertIn('data-layer="Edge_Cuts"', shown[:400])

    def test_it_falls_back_to_the_first_layer_without_an_outline(self):
        body = diff_body([run_for(("F_Cu", 5), ("F_Mask", 2))],
                         single_layer=True)
        self.assertEqual(body.count(' shown"'), 1)
        shown = body[body.index(' shown"'):]
        self.assertIn('data-layer="F_Cu"', shown[:400])

    def test_a_full_listing_reveals_nothing_because_all_are_visible(self):
        body = diff_body([run_for(("F_Cu", 5))], single_layer=False)
        self.assertNotIn(' shown"', body)


class BuildDiffPageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "page.html"
        self.addCleanup(self._tmp.cleanup)

    def test_without_a_layer_table_every_section_is_listed(self):
        build_diff_page([run_for(("F_Cu", 0), ("Edge_Cuts", 5))], self.out)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("layerpanel", text)
        self.assertNotIn("diffroot single", text)

    def test_with_a_layer_table_the_panel_drives_one_layer_at_a_time(self):
        build_diff_page(
            [run_for(("F_Cu", 0), ("Edge_Cuts", 5))],
            self.out,
            layers=LAYERS,
            status_by_layer={"F.Cu": "ok", "Edge.Cuts": "bad"},
        )
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("diffroot single", text)
        self.assertIn("layerpanel", text)
        self.assertIn('class="lp-status bad"', text)
        self.assertIn("lp-diff", text)

    def test_the_page_is_self_contained_either_way(self):
        for layers in (None, LAYERS):
            build_diff_page([run_for(("F_Cu", 0))], self.out, layers=layers)
            text = self.out.read_text(encoding="utf-8")
            for forbidden in ("<link", "<script src", "@import", "fetch("):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()


class RegionEntryFormatTest(unittest.TestCase):
    """Short lines: moved, widest, pixels.

    Coordinates are on the numbered button, not in the line; the area is left
    out because displacement already says it; and the soft class's
    explanation sits once on its group heading, not on every line.
    """

    def layer(self, *widths_px):
        canvas = Canvas.over(Bounds(0.0, 0.0, 10.0, 10.0), 5.0)
        regions = tuple(
            Region(1.0, 2.0, 1.5, 2.5, pixels=6, area_mm2=0.00015,
                   perimeter_mm=0.12,
                   widest_mm=width * canvas.mm_per_pixel)
            for width in widths_px
        )
        return dataclasses.replace(
            layer_diff("Edge_Cuts", 6),
            regions=regions,
            pixel_area_mm2=canvas.pixel_area_mm2,
            displacement_threshold_mm=0.01,
        )

    def body(self, *widths_px):
        return diff_body([run_from([self.layer(*widths_px)])])

    def test_an_entry_carries_only_the_three_numbers(self):
        entry = re.search(r"<li class=[^>]*>.*?</li>", self.body(1), re.S)
        self.assertIsNotNone(entry)
        text = entry.group(0)
        self.assertIn("moved", text)
        self.assertIn("widest", text)
        self.assertIn("px</span>", text)

    def test_an_entry_carries_no_coordinates_or_area(self):
        text = re.search(r"<li class=[^>]*>.*?</li>", self.body(1), re.S).group(0)
        # The jump button holds the position; the numbers do not repeat it.
        self.assertNotIn("mm&sup2;", text)
        self.assertNotIn("..", text)

    def test_the_soft_explanation_appears_once_not_on_every_line(self):
        body = self.body(1, 1, 1, 1)
        self.assertEqual(body.count("most likely the drawing's own rounding"), 1)

    def test_the_explanation_sits_on_the_group_heading(self):
        body = self.body(1, 1)
        heading = re.search(
            r'<div class="regiongroup soft">([^<]+)</div>', body)
        self.assertIsNotNone(heading)
        self.assertIn("rounding", heading.group(1))

    def test_hard_entries_keep_their_badge_and_come_first(self):
        body = self.body(1, 4)
        self.assertIn("hardmark", body)
        self.assertLess(body.index("wider than one pixel"),
                        body.index("no wider than one pixel"))

    def test_a_layer_with_only_hard_regions_has_no_soft_heading(self):
        body = self.body(4)
        self.assertNotIn("regiongroup soft", body)
