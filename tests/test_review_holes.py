"""The holes entry: what is lifted out of an export, and what the page shows.

The fixtures under `tests/data` are REAL kicad-cli 10.0.4 exports - `--mode-multi
--page-size-mode 2 --exclude-drawing-sheet` over `holes_fixture.kicad_pcb`,
which is committed beside them so the pair can be made again. The board is the
smallest thing that produces every case at once: a track and two pads give the
layer its own ink, a via and a plated pad give white knockout marks, and an NPTH
pad exactly its own drill size gives a black one.

Structure is what the extraction keys on, so structure is what is tested: the
trailing run, the root group left behind, and each way an export can fail to be
the shape the extraction was measured against - which produces a sentence for
the page and not an overlay in the wrong place.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from eedesignpanels.review import holes, layerpanel, page
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.review.theme import Theme

DATA = Path(__file__).parent / "data"
FRONT = DATA / "holes_export_F_Cu.svg"
BACK = DATA / "holes_export_B_Cu.svg"

# The four marks the fixture board draws on F.Cu, as kicad-cli wrote them.
VIA_MARK = '<circle cx="4.0000" cy="4.0000" r="0.1500" />'
PLATED_MARK = '<circle cx="3.0000" cy="3.0000" r="0.3000" />'
NPTH_MARK = '<circle cx="6.0000" cy="3.0000" r="0.2750" />'
FRONT_PAD_MARK = '<circle cx="3.0000" cy="5.0000" r="0.3000" />'

# The hole keys carry KiCad's own built-in defaults, which is also what an
# unchanged user theme has: `plated_hole` is the NON-plated one (light blue,
# color_settings.cpp:136) and `via_hole` is the plated one (golden).
THEME = Theme("test", {"copper": {"f": "rgb(200, 52, 52)",
                                  "b": "rgb(77, 127, 196)"},
                       "plated_hole": "rgb(26, 196, 210)",
                       "pad_plated_hole": "rgb(194, 194, 0)",
                       "via_hole": "rgb(227, 183, 46)",
                       "via_hole_walls": "rgb(236, 236, 236)"},
              "rgb(0, 16, 35)")
NON_PLATED = "rgb(26, 196, 210)"
PLATED = "rgb(227, 183, 46)"

LAYERS = [
    LayerSpec(0, "F.Cu", "signal", "F.Cu"),
    LayerSpec(2, "B.Cu", "signal", "B.Cu"),
    LayerSpec(44, "Edge.Cuts", "user", "Edge.Cuts"),
]


def wrap(body: str, opening: str = "") -> str:
    """A document shaped like an export, for the cases no board can produce."""
    head = opening or (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10.0000mm" '
        'height="10.0000mm" viewBox="0.0000 0.0000 10.0000 10.0000">'
    )
    return f"{head}\n{body}\n</svg>\n"


ROOT_GROUP = (
    '<g style="fill:#000000; fill-opacity:1.0000;stroke:#000000;\n'
    'stroke-opacity:1.0000;"\n transform="translate(0 0) scale(1 1)">\n</g>'
)


class ExportStructureTest(unittest.TestCase):
    """What a real export is made of, read back off one."""

    def setUp(self):
        self.text = FRONT.read_text(encoding="utf-8")

    def test_groups_are_flat_siblings(self):
        # kicad-cli closes each style group before opening the next, root
        # included - so "the trailing run" is a run of siblings, not a subtree.
        # Root, the layer's filled pads, its stroked track, then the four
        # drill-mark groups the plotter emits one colour change at a time.
        groups = holes.top_level_groups(self.text)
        self.assertEqual(len(groups), 7)
        self.assertIn("transform", groups[0].open_tag)

    def test_the_root_group_is_not_a_drill_group(self):
        # It is painted black, which is a drill-mark colour, and it is only the
        # plotter's default state. The transform is what tells them apart.
        groups = holes.top_level_groups(self.text)
        self.assertFalse(holes.is_drill_group(groups[0]))

    def test_the_layers_own_ink_is_not_a_drill_group(self):
        groups = holes.top_level_groups(self.text)
        self.assertFalse(holes.is_drill_group(groups[1]))  # filled pads
        self.assertFalse(holes.is_drill_group(groups[2]))  # stroked track

    def test_the_trailing_run_is_the_drill_marks(self):
        groups, reason = holes.drill_groups(self.text)
        self.assertEqual(reason, "")
        self.assertEqual(len(groups), 4)
        found = [element for group in groups for element in group.elements]
        self.assertEqual(
            found, [VIA_MARK, PLATED_MARK, NPTH_MARK, FRONT_PAD_MARK]
        )

    def test_white_is_a_knockout_and_black_is_a_bare_mark(self):
        # KiCad plots a drill mark white when a plotted layer has copper to
        # knock it out of, and black when nothing does
        # (pcbnew/plot_brditems_plotter.cpp PlotDrillMarks). The NPTH pad is
        # exactly its own drill, so it is the black one.
        groups, _ = holes.drill_groups(self.text)
        black = [group for group in groups
                 if holes.MARK_COLOR in group.paints]
        self.assertEqual(len(black), 1)
        self.assertEqual(black[0].elements, [NPTH_MARK])

    def test_the_frame_comes_off_the_export(self):
        frame = holes.frame_of(self.text)
        self.assertEqual(frame.view_box, "0.0000 0.0000 9.9822 5.9944")
        self.assertEqual(frame.width, "9.9822mm")


class MergeTest(unittest.TestCase):
    def merged(self, sources=None, colors=None):
        return holes.merge(
            sources or [("F.Cu", FRONT), ("B.Cu", BACK)],
            colors if colors is not None else {
                "F.Cu": THEME.layer_color("F.Cu"),
                "B.Cu": THEME.layer_color("B.Cu"),
            },
        )

    def test_marks_are_re_emitted_verbatim(self):
        marks = self.merged()
        for element in (VIA_MARK, PLATED_MARK, NPTH_MARK, FRONT_PAD_MARK):
            self.assertIn(element, marks.markup)

    def test_the_same_hole_on_two_layers_is_drawn_once(self):
        # Both copper exports carry the same four marks; the merged document
        # has four, not eight.
        marks = self.merged()
        self.assertEqual(marks.elements, 4)
        self.assertEqual(marks.markup.count("<circle"), 4)
        self.assertEqual(marks.sources, ("F.Cu",))
        self.assertEqual(marks.notice, "")

    def test_the_document_keeps_the_exports_own_frame(self):
        marks = self.merged()
        self.assertIn('viewBox="0.0000 0.0000 9.9822 5.9944"', marks.markup)
        self.assertIn('width="9.9822mm"', marks.markup)
        self.assertIn('height="5.9944mm"', marks.markup)

    def test_without_a_palette_the_exports_own_group_styles_are_kept(self):
        # No palette is the honest fallback: the marks look exactly as
        # kicad-cli plotted them, white knockout and black mark.
        marks = self.merged()
        self.assertIn('<g style="fill:#FFFFFF; fill-opacity:1.0000; '
                      'stroke:none;">', marks.markup)
        self.assertIn('<g style="fill:#000000; fill-opacity:1.0000; '
                      'stroke:none;">', marks.markup)

    def test_a_hole_knocked_out_anywhere_is_drawn_as_a_knockout(self):
        # KiCad's own pad rule takes white as soon as ANY plotted layer has an
        # annulus, so merging the copper stack has to answer the same way: the
        # black copy of a hole that is white elsewhere is dropped.
        white = wrap(ROOT_GROUP + '\n<g style="fill:#C83434;">'
                     '<circle cx="1" cy="1" r="1" /></g>\n'
                     '<g style="fill:#FFFFFF; stroke:none;">'
                     f"{VIA_MARK}</g>")
        black = wrap(ROOT_GROUP + '\n<g style="fill:#4D7FC4;">'
                     '<circle cx="1" cy="1" r="1" /></g>\n'
                     '<g style="fill:#000000; stroke:none;">'
                     f"{VIA_MARK}</g>")
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "f.svg", Path(tmp) / "b.svg"
            first.write_text(white, encoding="utf-8")
            second.write_text(black, encoding="utf-8")
            marks = holes.merge([("F.Cu", first), ("B.Cu", second)])
        self.assertEqual(marks.elements, 1)
        self.assertIn("#FFFFFF", marks.markup)
        self.assertNotIn("fill:#000000", marks.markup)

    def test_an_export_with_no_holes_says_nothing(self):
        # A board with nothing drilled is not a failure and gets no sentence.
        plain = wrap(ROOT_GROUP + '\n<g style="fill:none; stroke:#8D8E8B;">'
                     '<path d="M0 0 L1 1" /></g>')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edge.svg"
            path.write_text(plain, encoding="utf-8")
            marks = holes.merge([("Edge.Cuts", path)])
        self.assertEqual(marks.markup, "")
        self.assertEqual(marks.notice, "")

    def test_copper_sources_are_the_copper_exports_in_board_order(self):
        svgs = {"F.Cu": FRONT, "Edge.Cuts": FRONT, "B.Cu": BACK}
        self.assertEqual(
            holes.copper_sources(LAYERS, svgs),
            [("F.Cu", FRONT), ("B.Cu", BACK)],
        )

    def test_a_layer_that_was_not_exported_is_not_a_source(self):
        self.assertEqual(holes.copper_sources(LAYERS, {"B.Cu": BACK}),
                         [("B.Cu", BACK)])


class EditorColorsTest(unittest.TestCase):
    """The overlay is painted the way the PCB EDITOR paints holes.

    An NPTH must not show as a black void, because
    KiCad shows it light blue. The mapping is read off the 10.0.3 clone - the
    theme key `board.plated_hole` is `LAYER_NON_PLATEDHOLES`
    (color_settings.cpp:136), and `board.via_hole` is what the editor paints a
    plated hole with, via or pad (pcb_painter.cpp:1238, :295).
    """

    def test_black_takes_the_non_plated_key_and_white_the_plated_one(self):
        palette = holes.editor_palette(THEME)
        self.assertEqual(palette[holes.MARK_COLOR], NON_PLATED)
        self.assertEqual(palette[holes.KNOCKOUT_COLOR], PLATED)

    def test_a_missing_key_falls_back_to_the_colour_it_would_replace(self):
        palette = holes.editor_palette(Theme("bare", {}, "rgb(0, 0, 0)"))
        self.assertEqual(palette[holes.MARK_COLOR], holes.MARK_COLOR)
        self.assertEqual(palette[holes.KNOCKOUT_COLOR], holes.KNOCKOUT_COLOR)

    def test_a_key_that_is_not_a_colour_string_falls_back_too(self):
        theme = Theme("odd", {"plated_hole": {"f": "#fff"}, "via_hole": "  "},
                      "rgb(0, 0, 0)")
        palette = holes.editor_palette(theme)
        self.assertEqual(palette[holes.MARK_COLOR], holes.MARK_COLOR)
        self.assertEqual(palette[holes.KNOCKOUT_COLOR], holes.KNOCKOUT_COLOR)

    def test_restyle_repaints_only_the_drill_colours(self):
        palette = holes.editor_palette(THEME)
        tag = ('<g style="fill:#FFFFFF; fill-opacity:1.0000; stroke:none;">')
        self.assertEqual(
            holes.restyle(tag, palette),
            f'<g style="fill:{PLATED}; fill-opacity:1.0000; stroke:none;">',
        )

    def test_restyle_leaves_a_colour_that_is_not_a_drill_colour_alone(self):
        tag = '<g style="fill:#C83434; stroke:#C83434;">'
        self.assertEqual(holes.restyle(tag, holes.editor_palette(THEME)), tag)

    def test_no_palette_is_no_change(self):
        tag = '<g style="fill:#FFFFFF; stroke:none;">'
        self.assertEqual(holes.restyle(tag, None), tag)
        self.assertEqual(holes.restyle(tag, {}), tag)

    def test_the_merged_document_carries_the_editor_colours_not_the_plots(self):
        marks = holes.merge(
            [("F.Cu", FRONT), ("B.Cu", BACK)],
            {"F.Cu": THEME.layer_color("F.Cu"),
             "B.Cu": THEME.layer_color("B.Cu")},
            palette=holes.editor_palette(THEME),
        )
        self.assertIn(f"fill:{PLATED};", marks.markup)
        self.assertIn(f"fill:{NON_PLATED};", marks.markup)
        self.assertNotIn("fill:#FFFFFF", marks.markup)
        self.assertNotIn("fill:#000000", marks.markup)

    def test_not_one_coordinate_is_touched(self):
        # The geometry stays kicad-cli's byte for byte; only the paint moves.
        marks = holes.merge([("F.Cu", FRONT)], palette=holes.editor_palette(THEME))
        for element in (VIA_MARK, PLATED_MARK, NPTH_MARK, FRONT_PAD_MARK):
            self.assertIn(element, marks.markup)
        self.assertEqual(marks.elements, 4)

    def test_marks_of_one_editor_colour_become_one_group(self):
        # kicad-cli emits a fresh group per pad, so the plot has three white
        # groups here and one black; repainted, that is one group each.
        marks = holes.merge([("F.Cu", FRONT)], palette=holes.editor_palette(THEME))
        self.assertEqual(marks.markup.count(f"fill:{PLATED};"), 1)
        self.assertEqual(marks.markup.count(f"fill:{NON_PLATED};"), 1)
        self.assertEqual(marks.markup.count("<circle"), 4)

    def test_the_swatch_is_derived_from_the_same_two_keys(self):
        theme = holes.with_swatch(THEME)
        self.assertEqual(
            theme.layer_color(holes.HOLES_LAYER),
            f"linear-gradient(135deg, {PLATED} 50%, {NON_PLATED} 50%)",
        )
        self.assertEqual(theme.layer_alpha(holes.HOLES_LAYER), 1.0)

    def test_the_swatch_degrades_with_the_palette(self):
        theme = holes.with_swatch(Theme("bare", {}, "rgb(0, 0, 0)"))
        self.assertEqual(
            theme.layer_color(holes.HOLES_LAYER),
            "linear-gradient(135deg, #ffffff 50%, #000000 50%)",
        )

    def test_write_beside_passes_the_palette_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, marks = holes.write_beside(
                [("F.Cu", FRONT)], {}, Path(tmp) / "holes.svg",
                palette=holes.editor_palette(THEME),
            )
            self.assertIn(f"fill:{NON_PLATED};",
                          path.read_text(encoding="utf-8"))
        self.assertEqual(marks.elements, 4)


class NotMyStructureTest(unittest.TestCase):
    """Every way an export can miss the expected shape ends in a sentence."""

    def merge_text(self, text, color=""):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer.svg"
            path.write_text(text, encoding="utf-8")
            return holes.merge([("F.Cu", path)], {"F.Cu": color})

    def test_groups_that_do_not_nest_are_refused(self):
        self.assertIsNone(holes.top_level_groups("<g><g></g>"))
        marks = self.merge_text(wrap("<g><g></g>"))
        self.assertEqual(marks.markup, "")
        self.assertIn("do not nest", marks.notice)
        self.assertIn("could not be shown", marks.notice)

    def test_a_document_that_is_all_drill_marks_is_refused(self):
        # Even an empty layer keeps the plotter's transformed root group, so
        # this is not an export of ours and its "marks" are not marks.
        marks = self.merge_text(
            wrap(f'<g style="fill:#000000; stroke:none;">{NPTH_MARK}</g>')
        )
        self.assertEqual(marks.markup, "")
        self.assertIn("every group in it looks like a drill mark", marks.notice)

    def test_a_layer_plotted_in_a_drill_colour_is_refused(self):
        # A theme may paint copper black. Its own ink would then be
        # indistinguishable from a hole, so nothing is lifted.
        marks = self.merge_text(FRONT.read_text(encoding="utf-8"),
                                color="rgb(0, 0, 0)")
        self.assertEqual(marks.markup, "")
        self.assertIn("also a drill-mark colour", marks.notice)

    def test_a_theme_colour_is_compared_in_the_exports_own_notation(self):
        self.assertEqual(holes.normalize_color("rgb(200, 52, 52)"), "#c83434")
        self.assertEqual(holes.normalize_color("rgba(255, 255, 255, 0.5)"),
                         "#ffffff")
        self.assertEqual(holes.normalize_color("#FFF"), "#ffffff")

    def test_a_document_with_no_viewbox_is_refused(self):
        marks = self.merge_text("<svg><g></g></svg>")
        self.assertEqual(marks.markup, "")
        self.assertIn("no viewBox", marks.notice)

    def test_exports_cropped_differently_are_not_merged(self):
        other = wrap(
            ROOT_GROUP + f'\n<g style="fill:#000000; stroke:none;">'
            f"{NPTH_MARK}</g>",
            opening='<svg xmlns="http://www.w3.org/2000/svg" width="5mm" '
                    'height="5mm" viewBox="0 0 5 5">',
        )
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "f.svg", Path(tmp) / "b.svg"
            first.write_text(FRONT.read_text(encoding="utf-8"), encoding="utf-8")
            second.write_text(other, encoding="utf-8")
            marks = holes.merge([("F.Cu", first), ("B.Cu", second)])
        # The one that agrees with the first still shows; the odd one is named.
        self.assertEqual(marks.elements, 4)
        self.assertIn("cropped to a different area", marks.notice)
        self.assertIn("Some holes could not be shown", marks.notice)


class LayerPanelEntryTest(unittest.TestCase):
    def test_the_row_is_last_and_is_not_a_board_layer(self):
        entry = holes.holes_layer(LAYERS)
        self.assertEqual(entry.canonical_name, holes.HOLES_LAYER)
        self.assertEqual(entry.display_name, "Holes")
        self.assertGreater(entry.ordinal,
                           max(layer.ordinal for layer in LAYERS))

    def test_the_entry_is_never_physical_and_starts_visible_on_top(self):
        # `top` is stacking order and nothing more - what H does to the entry
        # is fenced on the emitted page, where the mode branches live.
        data = json.loads(layerpanel.layers_json(
            [*LAYERS, holes.holes_layer(LAYERS)],
            start_visible=(holes.HOLES_LAYER,),
            theme=holes.with_swatch(THEME),
            on_top=(holes.HOLES_LAYER,),
        ))
        entry = data[-1]
        self.assertFalse(entry["physical"])
        self.assertTrue(entry["start"])
        self.assertTrue(entry["top"])
        self.assertFalse(any(row["top"] for row in data[:-1]))


class HolesOnThePageTest(unittest.TestCase):
    """Read off the written HTML: a string in a generator is not a page."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        marks = holes.merge([("F.Cu", FRONT)], {"F.Cu": "rgb(200, 52, 52)"},
                            palette=holes.editor_palette(THEME))
        self.holes_svg = self.dir / "holes.svg"
        self.holes_svg.write_text(marks.markup, encoding="utf-8")

    def build(self, with_holes: bool, subtitle: str = "a panel"):
        layers = [LAYERS[0]]
        stack = {"F.Cu": FRONT}
        on_top = ()
        theme = THEME
        if with_holes:
            layers = [*layers, holes.holes_layer(layers)]
            stack[holes.HOLES_LAYER] = self.holes_svg
            on_top = (holes.HOLES_LAYER,)
            theme = holes.with_swatch(THEME)
        out = page.build_page(
            out_path=self.dir / "page.html",
            heading="Holes",
            subtitle=subtitle,
            layers=layers,
            views=[page.BoardView(title="board", svg_by_layer=stack),
                   page.BoardView(title="panel", svg_by_layer=stack)],
            theme=theme,
            start_visible=on_top,
            on_top=on_top,
        )
        return out.read_text(encoding="utf-8")

    def layers_of(self, html_text):
        return json.loads(
            re.search(r"const LAYERS = (\[.*?\]);", html_text, re.S).group(1)
        )

    def test_the_row_is_on_the_page_and_starts_visible(self):
        text = self.build(with_holes=True)
        self.assertIn(">Holes<", text)
        entry = self.layers_of(text)[-1]
        self.assertEqual(entry["canonical"], holes.HOLES_LAYER)
        self.assertTrue(entry["start"])
        self.assertTrue(entry["top"])

    def test_every_viewport_carries_the_marks(self):
        text = self.build(with_holes=True)
        stacks = text.split(f'data-layer="{holes.HOLES_LAYER}"')[1:]
        self.assertEqual(len(stacks), 2)
        for stack in stacks:
            self.assertIn(NPTH_MARK, stack[:stack.index("</svg>")])

    def test_the_marks_stack_above_every_layer(self):
        text = self.build(with_holes=True)
        self.assertIn("zIndex = layer.top ? 999 : (isActive ? 99 : 1)", text)

    def test_H_dims_and_hides_the_marks_like_any_other_layer(self):
        # Dimmed dims the holes and hidden hides them, as for any layer.
        # `top` is stacking order only.
        text = self.build(with_holes=True)
        self.assertIn("if (on && !isActive && panel.mode === 1) opacity *= 0.22;",
                      text)
        self.assertIn("if (on && !isActive && panel.mode === 2) shown = false;",
                      text)
        self.assertNotIn("!layer.top && panel.mode", text)

    def test_the_marks_are_painted_in_the_editors_hole_colours(self):
        text = self.build(with_holes=True)
        stack = text.split(f'data-layer="{holes.HOLES_LAYER}"')[1]
        overlay = stack[:stack.index("</svg>")]
        self.assertIn(f"fill:{NON_PLATED};", overlay)
        self.assertIn(f"fill:{PLATED};", overlay)
        self.assertNotIn("fill:#FFFFFF", overlay)

    def test_the_swatch_on_the_row_is_the_derived_gradient(self):
        text = self.build(with_holes=True)
        self.assertIn(
            f"background:linear-gradient(135deg, {PLATED} 50%, "
            f"{NON_PLATED} 50%)",
            text,
        )

    def test_without_marks_there_is_no_row_and_the_notice_is_on_the_page(self):
        notice = ("The holes could not be shown: no export could be read for "
                  "them - F.Cu because it carries no viewBox.")
        text = self.build(with_holes=False, subtitle=f"{notice}  //  a panel")
        self.assertNotIn(f'data-layer="{holes.HOLES_LAYER}"', text)
        self.assertNotIn(holes.HOLES_LAYER, str(self.layers_of(text)))
        self.assertIn("The holes could not be shown", text)


if __name__ == "__main__":
    unittest.main()
