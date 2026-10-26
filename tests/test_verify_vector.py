"""The comparison's picture as vectors, and the guardrail behind it.

Drawn rather than rasterised so a difference stays sharp when the user
zooms into it to judge it. What is pinned here is the composition - which
sheet carries what, so the blend still averages to neutral grey - the fact
that the expected side is placed by reference rather than repeated, and that
markers sit at true millimetre positions.
"""

import re
import unittest

from PIL import Image

from eedesignpanels.verify import vector
from eedesignpanels.verify.regions import Bounds, Canvas

CANVAS = Canvas.over(Bounds(0.0, -10.0, 20.0, 0.0), 5.0)
OFFSETS = [(0.0, 0.0), (18.499999, 0.0), (0.0, -13.500001)]


def mask(width, height, pixels) -> Image.Image:
    image = Image.new("1", (width, height), 0)
    for x, y in pixels:
        image.putpixel((x, y), 1)
    return image


class _StubGerber:
    """Stands in for a parsed gerber: it only has to answer `to_svg`.

    Shaped like the real thing, transform and all, because that transform is
    the space the placement offsets are stated in.
    """

    def to_svg(self, **_):
        return ('<svg viewBox="0.0 -10.0 20.0 10.0">'
                '<g transform="translate(0 -10) scale(1 -1) translate(0 10)">'
                '<path d="M 0 0 L 1 1" fill="#ff0000"/></g></svg>')


class CoordinateTransformTest(unittest.TestCase):
    """The drawing keeps the transform its own coordinates are stated in.

    Gerber y runs up, document y runs down, and the backend bridges the two
    with a transform on the group it wraps the artwork in. Taking only what
    was *inside* that group dropped the flip: the artwork landed at the wrong
    end of the window and five of six placements translated clean off the
    canvas. On screen that read as "the expected side does not paint" - the
    one placement still on canvas did paint, and matched the built panel
    exactly, because both sides had lost the same flip. Measured on a
    delivered page.
    """

    def sheet(self):
        return vector.placed_reference(_StubGerber(), CANVAS, OFFSETS, "F_Cu")

    def test_the_artwork_keeps_its_own_transform(self):
        self.assertIn("scale(1 -1)", self.sheet())

    def test_the_built_side_keeps_it_too(self):
        self.assertIn("scale(1 -1)", vector.built_panel(_StubGerber(), CANVAS))

    def test_the_flip_wraps_the_artwork_rather_than_being_stripped(self):
        # The flip must sit between the declaration and the artwork, so a
        # <use> translate applies outside it and moves an instance down the
        # page when the placement moves it down the panel.
        sheet = self.sheet()
        self.assertLess(sheet.index('<g id="'), sheet.index("scale(1 -1)"))
        self.assertLess(sheet.index("scale(1 -1)"), sheet.index("<path"))

    def test_both_sides_are_built_by_the_same_mechanism(self):
        # If the two sides are ever produced differently they can diverge in
        # paint or in position, and one silently stops meaning what the other
        # means. Identical but for the colour is the invariant.
        expected = vector.placed_reference(
            _StubGerber(), CANVAS, [(0.0, 0.0)], "F_Cu",
            colour=vector.EXPECTED_COLOUR)
        built = vector.built_panel(
            _StubGerber(), CANVAS, colour=vector.EXPECTED_COLOUR)
        drawing = re.search(r"(<g transform=.*</g>)", built, re.S).group(1)
        self.assertIn(drawing, expected)

    def test_neither_side_has_its_paint_overridden(self):
        # The artwork states its own fill and stroke per primitive. A blanket
        # override - fill everywhere, stroke:none - would erase every stroked
        # aperture draw on a copper layer.
        for css in (vector.CSS,):
            for rule in re.findall(r"[^{}]+\{[^}]*\}", css):
                if "delta" in rule:
                    continue
                self.assertNotIn("stroke:", rule.replace(" ", ""))
                self.assertNotIn("fill:none", rule.replace(" ", ""))


class ReferenceIdTest(unittest.TestCase):
    """Ids are shared by every layer inlined into one document.

    The defect that shipped: a fixed `id="expected"`, declared by all nineteen
    layers. Every `<use>` on the page resolved to the FIRST of them, so each
    layer showed one correct instance of its own artwork plus six ghosts of
    another layer's - offset and mirrored, because that layer's own flip
    travelled with it. Seen live. These fail on that exact shape.
    """

    KEYS = ["F_Cu", "B_Fab", "Edge_Cuts", "User.4-F.FpRef", "In1_Cu"]

    def sheet(self, key: str) -> str:
        return vector.placed_reference(_StubGerber(), CANVAS, OFFSETS, key)

    def test_two_layers_never_share_a_reference_id(self):
        self.assertNotEqual(
            vector.reference_id("F_Cu"), vector.reference_id("B_Fab"))

    def test_the_id_survives_a_layer_name_that_is_not_an_identifier(self):
        made = vector.reference_id("User.4-F.FpRef")
        self.assertNotIn(".", made)
        self.assertRegex(made, r"^[A-Za-z][-\w]*$")

    def test_every_id_in_a_whole_document_is_unique(self):
        # The real condition: one page, every layer's SVG inside it.
        page = "".join(self.sheet(key) for key in self.KEYS)
        ids = re.findall(r'\sid="([^"]+)"', page)
        self.assertEqual(len(ids), len(self.KEYS))
        self.assertEqual(len(set(ids)), len(ids), f"duplicate ids: {ids}")

    def test_every_placement_reference_resolves_inside_its_own_layer(self):
        page = "".join(self.sheet(key) for key in self.KEYS)
        for key in self.KEYS:
            target = vector.reference_id(key)
            # Declared exactly once in the whole document, so this layer's
            # placements cannot resolve to another layer's artwork.
            self.assertEqual(page.count(f'id="{target}"'), 1, key)
            self.assertEqual(
                self.sheet(key).count(f'href="#{target}"'), len(OFFSETS), key)


class PlacementCountTest(unittest.TestCase):
    """One instance per placement - no more, no fewer.

    Declared outside `<defs>` the artwork also drew itself in place, so the
    expected side carried one instance too many; and with a colliding id the
    six placements drew another layer's artwork instead of this one's. The
    picture showed a single instance where six belonged.
    """

    def sheet(self) -> str:
        return vector.placed_reference(_StubGerber(), CANVAS, OFFSETS, "F_Cu")

    def test_there_is_one_placement_reference_per_placement(self):
        self.assertEqual(self.sheet().count("<use "), len(OFFSETS))

    def test_the_artwork_is_declared_exactly_once(self):
        self.assertEqual(self.sheet().count("<defs>"), 1)
        self.assertEqual(self.sheet().count("<g id="), 1)

    def test_the_declaration_sits_in_defs_so_it_does_not_draw_itself(self):
        sheet = self.sheet()
        self.assertLess(sheet.index("<defs>"), sheet.index("<g id="))
        self.assertLess(sheet.index("</defs>"), sheet.index("<use "))

    def test_the_uses_carry_the_placements_they_were_given(self):
        sheet = self.sheet()
        for dx, dy in OFFSETS:
            self.assertIn(f'x="{dx:.6f}" y="{-dy:.6f}"', sheet)


class EmptySheetTest(unittest.TestCase):
    def test_a_missing_side_still_gets_a_sheet(self):
        # Without one the stack would show the other side alone and the blend
        # would read as agreement.
        sheet = vector.empty_sheet(CANVAS, "built")
        self.assertIn("<svg", sheet)
        self.assertIn("data-base=", sheet)

    def test_its_window_matches_the_canvas(self):
        sheet = vector.empty_sheet(CANVAS, "built")
        self.assertIn("0.000000 -10.000000 20.000000 10.000000", sheet)


class BlendTest(unittest.TestCase):
    """The stack that makes agreement average to neutral grey."""

    def blend(self, markers=""):
        return vector.blend(
            vector.empty_sheet(CANVAS, "expected"),
            vector.empty_sheet(CANVAS, "built"),
            markers,
        )

    def test_the_expected_sheet_is_underneath_and_the_built_over_it(self):
        markup = self.blend().markup
        self.assertLess(markup.index('vsheet base'), markup.index('vsheet over'))

    def test_the_averaging_is_in_the_stylesheet_not_the_markup(self):
        # Both sheets carry white; the top one is half transparent. That is
        # what turns "in both" into grey rather than into whichever went last.
        self.assertIn(".diffroot .vsheet.base { background: #fff; }", vector.CSS)
        self.assertIn("opacity: .5", vector.CSS)

    def test_a_clean_layer_carries_no_marker_sheet(self):
        self.assertNotIn("delta-sheet", self.blend().markup)

    def test_the_cost_is_reported_for_the_caller_to_judge(self):
        drawn = self.blend()
        self.assertGreater(drawn.bytes, 0)
        self.assertAlmostEqual(drawn.kilobytes, drawn.bytes / 1024)
        self.assertEqual(drawn.elements, 0)


class DeltaMarkerTest(unittest.TestCase):
    """Measured differences, at their true millimetre positions."""

    def blank(self):
        return mask(CANVAS.width_px, CANVAS.height_px, [])

    def one(self, pixels):
        return mask(CANVAS.width_px, CANVAS.height_px, pixels)

    def test_a_clean_layer_produces_nothing(self):
        self.assertEqual(
            vector.delta_markers(self.blank(), self.blank(), CANVAS), "")

    def test_one_differing_pixel_still_produces_a_marker(self):
        markers = vector.delta_markers(
            self.one([(100, 200)]), self.blank(), CANVAS)
        self.assertEqual(markers.count("<rect"), 1)

    def test_a_marker_sits_where_the_pixel_was(self):
        # Pixel (100, 200) pooled in fours: cell (25, 50), which is 25 and 50
        # cells of 20 um from the window's own corner.
        markers = vector.delta_markers(
            self.one([(100, 200)]), self.blank(), CANVAS, pool_px=4)
        step = CANVAS.mm_per_pixel * 4
        self.assertIn(f'x="{0.0 + 25 * step:.4f}"', markers)
        self.assertIn(f'y="{-10.0 + 50 * step:.4f}"', markers)

    def test_neighbouring_pixels_pool_into_one_marker(self):
        markers = vector.delta_markers(
            self.one([(100, 200), (101, 200), (100, 201)]),
            self.blank(), CANVAS, pool_px=4,
        )
        self.assertEqual(markers.count("<rect"), 1)

    def test_each_side_is_marked_in_its_own_group(self):
        markers = vector.delta_markers(
            self.one([(100, 200)]), self.one([(300, 400)]), CANVAS)
        self.assertIn("delta-expected", markers)
        self.assertIn("delta-built", markers)

    def test_a_side_with_nothing_on_it_gets_no_group(self):
        markers = vector.delta_markers(
            self.one([(100, 200)]), self.blank(), CANVAS)
        self.assertIn("delta-expected", markers)
        self.assertNotIn("delta-built", markers)


class MarkerStrokeTest(unittest.TestCase):
    """The defect that shipped: markers that buried the drawing.

    `vector-effect` does not inherit. Set on the group it applies to nothing,
    every rectangle computes `vector-effect: none`, and `stroke-width: 1.5`
    falls back to user units - millimetres - so at a fit view each marker
    painted as a roughly 30 px near-black blob over the drawing it existed to
    point at. Measured live on a delivered page. These fail on that exact
    shape, and "the effect appears somewhere in the file" does not satisfy
    them.
    """

    def markers(self) -> str:
        return vector.delta_markers(
            mask(CANVAS.width_px, CANVAS.height_px, [(100, 200)]),
            mask(CANVAS.width_px, CANVAS.height_px, []),
            CANVAS,
        )

    def test_the_effect_reaches_every_marker_individually(self):
        rects = re.findall(r"<rect[^>]*>", self.markers())
        self.assertTrue(rects)
        per_element = all(
            'vector-effect="non-scaling-stroke"' in rect for rect in rects
        )
        # ...or a stylesheet rule that selects the primitives themselves.
        rule = re.search(
            r"\.delta\s+rect\s*\{[^}]*vector-effect:\s*non-scaling-stroke",
            vector.CSS,
        )
        self.assertTrue(
            per_element or rule,
            "every delta rect must carry the effect itself, or a CSS rule "
            "must select the rects - it does not inherit from the group",
        )

    def test_the_effect_is_not_parked_on_the_group_where_it_does_nothing(self):
        markers = self.markers()
        group = markers[:markers.index("<rect")]
        self.assertNotIn("vector-effect", group)

    def test_the_width_is_declared_with_the_effect_that_makes_it_pixels(self):
        # Apart, the width silently means millimetres. Together is the only
        # arrangement in which 1.5 is 1.5 screen pixels.
        rule = re.search(r"\.delta\s+rect\s*\{([^}]*)\}", vector.CSS)
        self.assertIsNotNone(rule)
        self.assertIn("vector-effect: non-scaling-stroke", rule.group(1))
        self.assertIn("stroke-width", rule.group(1))

    def test_the_stroke_is_a_signal_colour_and_never_ink(self):
        for colour in (vector.MARKER_EXPECTED, vector.MARKER_BUILT):
            channels = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
            self.assertGreater(
                max(channels), 0x60,
                f"{colour} is too dark to be a signal colour; near-black "
                f"markers bury the drawing they point at",
            )
            # A colour, not a grey: grey means agreement on this page.
            self.assertGreater(max(channels) - min(channels), 0x40, colour)

    def test_both_marker_colours_are_styled(self):
        self.assertIn(vector.MARKER_EXPECTED, vector.CSS)
        self.assertIn(vector.MARKER_BUILT, vector.CSS)


class ViewControlsTest(unittest.TestCase):
    """Either side alone, and the differences alone - the evidence.

    It used to be three more pictures embedded behind a disclosure. It is now
    three ways of looking at the drawing already on screen, which costs
    nothing and shows the same thing.
    """

    def test_every_view_has_a_button(self):
        controls = vector.view_controls()
        for _, label in vector.VIEWS:
            self.assertIn(f">{label}</button>", controls)

    def test_the_blend_is_the_one_selected_to_start(self):
        self.assertIn('data-view="" class=on>blend', vector.view_controls())

    def test_each_view_has_a_rule_that_implements_it(self):
        for name, _ in vector.VIEWS:
            if name:
                self.assertIn(f".vblend.{name}", vector.CSS)


if __name__ == "__main__":
    unittest.main()


class DifferencesViewTest(unittest.TestCase):
    """Differences mode keeps the artwork, faintly.

    Markers alone on white are the instrument's own evidence and nothing else
    - true, and useless, because a difference with no artwork around it has no
    position anybody can judge.
    """

    def markup(self) -> str:
        return vector.blend(
            vector.empty_sheet(CANVAS, "expected"),
            vector.empty_sheet(CANVAS, "built"),
            '<g class="delta delta-expected"><rect/></g>',
        ).markup

    def test_the_two_artwork_sheets_share_a_wrapper(self):
        # Fading them separately would break the average that makes agreement
        # read as neutral grey.
        markup = self.markup()
        wrapper = markup.index('class="vartwork"')
        self.assertLess(wrapper, markup.index('class="vsheet base"'))
        self.assertLess(wrapper, markup.index('class="vsheet over"'))

    def test_differences_mode_fades_the_artwork_rather_than_hiding_it(self):
        rule = re.search(r"\.vblend\.only-delta \.vartwork \{([^}]*)\}",
                         vector.CSS)
        self.assertIsNotNone(rule)
        self.assertIn("opacity", rule.group(1))
        self.assertNotIn("display: none", rule.group(1))

    def test_the_underlay_is_faint_enough_for_markers_to_dominate(self):
        rule = re.search(r"\.vblend\.only-delta \.vartwork \{[^}]*opacity: *([\d.]+)",
                         vector.CSS)
        self.assertIsNotNone(rule)
        self.assertLessEqual(float(rule.group(1)), 0.25)
        self.assertGreater(float(rule.group(1)), 0.0)

    def test_the_markers_are_never_faded_with_it(self):
        self.assertNotIn(".only-delta .vsheet.marks", vector.CSS)

    def test_the_composition_is_said_on_the_page_in_that_mode(self):
        self.assertIn("faint copy of", self.markup())
        self.assertIn(".vblend.only-delta .deltanote", vector.CSS)

    def test_the_note_is_hidden_in_every_other_mode(self):
        self.assertIn(".diffroot .vblend .deltanote { display: none; }",
                      vector.CSS)
