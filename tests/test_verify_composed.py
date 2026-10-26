"""The composed reference document - structure, exactness, and the blanks.

The pixel-level equivalence of the composed render against the old ORed
per-placement renders is a measured property (264 differing pixels in 57
million on the densest corpus layer) and needs a
rasterizer to check; what a unit test can pin is everything the document
promises by construction: one artwork declaration, one <use> per placement,
offsets written exactly, and the y flip matching the page's own composition.
"""

import re
import unittest

from eedesignpanels.verify.panel_diff import blank_layer
from eedesignpanels.verify.regions import Bounds, Canvas
from eedesignpanels.verify.vector import placed_reference_document


class _FakeGerber:
    """The smallest thing vector._svg_of can consume: to_svg only."""

    def __init__(self, svg: str):
        self._svg = svg

    def to_svg(self, **_kwargs):
        return self._svg


BACKEND_SVG = (
    '<svg width="36mm" height="39mm" viewBox="194 -162 36 39" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<rect fill="white" x="194" y="-162" width="36" height="39"/>'
    '<g transform="translate(0 0) scale(1 -1)"><path d="M0 0h1"/></g></svg>'
)

CANVAS = Canvas.over(Bounds(194.0, -162.0, 230.0, -123.0), 5.0)
OFFSETS = ((0.0, 0.0), (18.499999, 0.0), (0.0, -13.500001))


class ComposedDocumentTest(unittest.TestCase):
    def document(self):
        return placed_reference_document(_FakeGerber(BACKEND_SVG), CANVAS, OFFSETS)

    def test_one_declaration_and_one_use_per_placement(self):
        document = self.document()
        self.assertEqual(document.count("<defs>"), 1)
        self.assertEqual(document.count('<g id="placed">'), 1)
        self.assertEqual(document.count("<use "), len(OFFSETS))

    def test_the_backends_own_document_tag_is_kept(self):
        # The opening tag carries the millimetre size and viewBox the
        # rasterizer scales by; restyling it is the page's job, not this one's.
        self.assertTrue(self.document().startswith(
            '<svg width="36mm" height="39mm" viewBox="194 -162 36 39"'
        ))

    def test_offsets_are_written_exactly_and_y_is_flipped(self):
        # <use> translates apply outside the artwork's own transform, in
        # document space, where y already runs down - so dy is negated,
        # exactly as the review page's own placed_reference writes it.
        # f"{-0.0:.6f}" is "-0.000000": harmless to the renderer, pinned here
        # so a future tidy-up that changes the string knows it changed bytes.
        document = self.document()
        self.assertIn('<use href="#placed" x="0.000000" y="-0.000000"/>', document)
        self.assertIn('<use href="#placed" x="18.499999" y="-0.000000"/>', document)
        self.assertIn('<use href="#placed" x="0.000000" y="13.500001"/>', document)

    def test_the_backends_background_sheet_is_not_instanced(self):
        # The white background rect sits outside the drawing group; instanced
        # per placement it would paint over a neighbouring instance's ink.
        artwork = re.search(r"<defs>(.*)</defs>", self.document()).group(1)
        self.assertNotIn("rect fill", artwork)

    def test_the_artwork_keeps_its_own_transform(self):
        # The group's transform is the gerber-to-document y flip; dropping it
        # sent five of six placements off the canvas once before (vector.py's
        # _BODY note). The declaration must carry it.
        self.assertIn(
            '<g id="placed"><g transform="translate(0 0) scale(1 -1)">',
            self.document(),
        )


class BlankLayerTest(unittest.TestCase):
    """A layer with no ink on either side is answered without rendering."""

    def test_every_count_is_zero_and_nothing_is_judged_wrongly(self):
        layer = blank_layer(
            key="F_Adhesive", canvas=CANVAS, placed=True,
            present_in_panel=True, threshold=0,
            displacement_threshold_mm=0.01, judged=False, placements=6,
        )
        self.assertEqual(layer.ink_pixels_a, 0)
        self.assertEqual(layer.ink_pixels_b, 0)
        self.assertEqual(layer.pixels_differing, 0)
        self.assertEqual(layer.regions, ())
        self.assertFalse(layer.over_threshold)
        self.assertEqual(layer.total_pixels, CANVAS.total_pixels)

    def test_the_metrics_say_what_the_rendered_path_would_say(self):
        placed = blank_layer(
            key="F_Cu", canvas=CANVAS, placed=True, present_in_panel=True,
            threshold=0, displacement_threshold_mm=0.01, judged=True,
            placements=6,
        )
        self.assertEqual(
            placed.metrics["reference"], "source F_Cu rendered at 6 placement(s)"
        )
        empty = blank_layer(
            key="F_Cu", canvas=CANVAS, placed=False, present_in_panel=True,
            threshold=0, displacement_threshold_mm=0.01, judged=True,
            placements=6,
        )
        self.assertEqual(
            empty.metrics["reference"],
            "empty - this stage does not replicate the layer",
        )


if __name__ == "__main__":
    unittest.main()
