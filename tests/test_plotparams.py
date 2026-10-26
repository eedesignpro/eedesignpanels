"""The panel's plot layer selection: the token, the rule, and the wiring.

The panel's dimensions layer is selected for plotting. The panel builder does
not run the fab outputs; a separate CI pipeline does, from the files the
builder writes, so what those files SELECT is the whole of the
interface. These tests fence four things a wrong selection would hide.

**The token.** `(layerselection 0x…)` is a 128-bit hex bitmask over
`PCB_LAYER_ID`, most significant nibble first, underscore every eight nibbles
(`include/base_set.h:302-345`, `include/layer_ids.h:171`). One transposed digit
selects a different layer and still parses, so the spelling is pinned against
the corpus board's real token and against the bit KiCad reserves for `User.16`
(id 69, `include/layer_ids.h:139`).

**The rule.** Which layers a fab run actually inks is not the bitmask alone:
copper comes from the board's ENABLED layers and only non-copper comes from the
mask (`pcbnew/pcbnew_jobs_handler.cpp:1356`), and the whole thing is
intersected with the enabled set (`:1370`), which is exactly the board's
`(layers …)` block (`pcb_io_kicad_sexpr_parser.cpp:2331,2358`).

**The sharing.** A panel made by `cheap_copy` shares its `setup` object with
the stepped panel the live preview keeps between edits, so writing the
selection through it would change a panel somebody else is holding.

**The wiring.** Dimensions on means the layer is selected; dimensions declined
means the selection is the design's, untouched.
"""

import unittest

from eedesigncore import Board, BoardLayer, Footprint, PcbLine, Setup, Stroke

from eedesignpanels import plotparams
from eedesignpanels.config import PanelDimensionsConfig
from eedesignpanels.stages.step_and_repeat import build, cheap_copy

# A six-layer design's own stored selection, as KiCad saves it: all six
# copper layers, both masks, both pastes and `Edge.Cuts`. No user layer,
# because a design does not plot a layer only a panel draws on.
CORPUS_SELECTION = "0x00000000_00000000_55555555_5755f55f"
# The same selection with `User.16` added. Nibble 17 of 32 gains bit 1.
CORPUS_WITH_DIMENSIONS = "0x00000000_00000020_55555555_5755f55f"

# `include/layer_ids.h`: the ordinals this module has to get right.
USER_16 = 69
EDGE_CUTS = 25
F_CU = 0

CUTTER = 0.889
RADIUS = CUTTER / 2


def outline(x0, y0, x1, y1, layer="Edge.Cuts", width=0.05, uid=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer, stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def source_board(selection: str = CORPUS_SELECTION) -> Board:
    """A 10 x 10 mm board ready to panelize, carrying a real plot selection."""
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
            BoardLayer(ordinal=1, canonical_name="F.Mask", layer_type="user"),
            BoardLayer(ordinal=3, canonical_name="B.Mask", layer_type="user"),
            BoardLayer(ordinal=5, canonical_name="F.SilkS", layer_type="user"),
            BoardLayer(ordinal=25, canonical_name="Edge.Cuts",
                       layer_type="user"),
            BoardLayer(ordinal=69, canonical_name="User.16",
                       layer_type="user", user_name="User.16-Dimensions"),
            BoardLayer(ordinal=109, canonical_name="User.36",
                       layer_type="user"),
            BoardLayer(ordinal=113, canonical_name="User.38",
                       layer_type="user"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    board.setup.pcbplotparams.layerselection = selection
    corners = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    board.drawings = [
        outline(*corners[index], *corners[(index + 1) % 4], uid=f"edge-{index}")
        for index in range(4)
    ]
    path = [(-RADIUS, -RADIUS), (10 + RADIUS, -RADIUS),
            (10 + RADIUS, 10 + RADIUS), (-RADIUS, 10 + RADIUS)]
    for index in range(3):
        board.drawings.append(outline(
            *path[index], *path[index + 1], layer="User.36", width=CUTTER,
            uid=f"path-{index}",
        ))
    board.footprints = [Footprint(
        fp_id="lib:R", layer="F.Cu", uuid="fp-1",
        at={"position": (5.0, 5.0), "angle": 0.0},
    )]
    return board


class TokenTest(unittest.TestCase):
    """`0x…` in, layer ordinals out, and back to the identical string."""

    def test_the_corpus_selection_reads_as_the_layers_it_names(self):
        ordinals = plotparams.parse_selection(CORPUS_SELECTION)
        # All six copper layers of the corpus stackup, plus the four
        # mask/paste layers and the outline. Copper ids are even, 0..62.
        self.assertEqual(
            sorted(ordinals),
            [0, 1, 2, 3, 4, 6, 8, 10, 12, 13, 14, 15, 16, 18, 20, 22, 24, 25,
             26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56,
             58, 60, 62],
        )
        self.assertNotIn(USER_16, ordinals)

    def test_the_corpus_selection_round_trips_to_the_identical_string(self):
        token = plotparams.format_selection(
            plotparams.parse_selection(CORPUS_SELECTION)
        )
        self.assertEqual(token, CORPUS_SELECTION)

    def test_the_spelling_is_kicads_own(self):
        """32 nibbles, four groups of eight, most significant first."""
        token = plotparams.format_selection([F_CU])
        self.assertEqual(token, "0x00000000_00000000_00000000_00000001")
        body = token[2:]
        self.assertEqual(len(body.replace("_", "")), 32)
        self.assertEqual([len(group) for group in body.split("_")],
                         [8, 8, 8, 8])

    def test_user_16_is_bit_69(self):
        self.assertEqual(plotparams.ordinal_of("User.16"), USER_16)
        self.assertEqual(plotparams.ordinal_of("Edge.Cuts"), EDGE_CUTS)
        token = plotparams.format_selection([USER_16])
        self.assertEqual(token, "0x00000000_00000020_00000000_00000000")
        self.assertTrue(plotparams.selects(token, "User.16"))
        self.assertFalse(plotparams.selects(token, "User.15"))

    def test_an_empty_selection_is_a_real_answer(self):
        empty = "0x00000000_00000000_00000000_00000000"
        self.assertEqual(plotparams.parse_selection(empty), frozenset())
        self.assertEqual(plotparams.format_selection([]), empty)

    def test_underscores_and_the_prefix_are_optional_on_the_way_in(self):
        """A legacy file carries a short token; `ParseHex` reads from the right."""
        self.assertEqual(plotparams.parse_selection("0x02000000"),
                         frozenset({EDGE_CUTS}))
        self.assertEqual(plotparams.parse_selection("2000000"),
                         frozenset({EDGE_CUTS}))
        # ...and it is normalised on the way out, which is what KiCad does the
        # first time it saves that board.
        self.assertEqual(
            plotparams.with_layers("0x02000000", []),
            "0x00000000_00000000_00000000_02000000",
        )

    def test_a_display_name_is_refused(self):
        """`User.16-Dimensions` is what the board CALLS it, not what it is."""
        with self.assertRaises(ValueError) as caught:
            plotparams.ordinal_of("User.16-Dimensions")
        self.assertIn("canonical", str(caught.exception))

    def test_a_bit_outside_the_set_is_refused(self):
        with self.assertRaises(ValueError):
            plotparams.format_selection([plotparams.SELECTION_BITS])


class WithLayersTest(unittest.TestCase):
    """Widening a selection, which is the only edit this module makes."""

    def test_the_dimensions_layer_is_added_and_nothing_is_removed(self):
        widened = plotparams.with_layers(CORPUS_SELECTION, ["User.16"])
        self.assertEqual(widened, CORPUS_WITH_DIMENSIONS)
        before = plotparams.parse_selection(CORPUS_SELECTION)
        after = plotparams.parse_selection(widened)
        self.assertTrue(before < after)
        self.assertEqual(after - before, {USER_16})

    def test_adding_it_twice_changes_nothing(self):
        once = plotparams.with_layers(CORPUS_SELECTION, ["User.16"])
        self.assertEqual(plotparams.with_layers(once, ["User.16"]), once)

    def test_a_layer_already_selected_is_not_a_change(self):
        self.assertEqual(
            plotparams.with_layers(CORPUS_SELECTION, ["Edge.Cuts"]),
            CORPUS_SELECTION,
        )


class PlottedLayersTest(unittest.TestCase):
    """KiCad's own rule: copper from the board, everything else from the mask."""

    def test_the_selected_non_copper_layers_and_all_the_enabled_copper(self):
        board = source_board()
        self.assertEqual(
            plotparams.plotted_layers(board),
            ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "Edge.Cuts"),
        )

    def test_copper_is_plotted_even_when_the_mask_does_not_name_it(self):
        """`pcbnew_jobs_handler.cpp:1356-1357` takes copper from the board."""
        board = source_board(selection="0x00000000_00000000_00000000_00000000")
        self.assertEqual(plotparams.plotted_layers(board), ("F.Cu", "B.Cu"))

    def test_a_layer_the_board_does_not_declare_is_not_plotted(self):
        """The enabled set is the `(layers …)` block, and only that."""
        board = source_board()
        board.layers = [
            layer for layer in board.layers
            if layer.canonical_name != "Edge.Cuts"
        ]
        self.assertNotIn("Edge.Cuts", plotparams.plotted_layers(board))

    def test_the_dimensions_layer_appears_once_it_is_selected(self):
        board = source_board(selection=CORPUS_WITH_DIMENSIONS)
        self.assertIn("User.16", plotparams.plotted_layers(board))

    def test_a_board_with_no_setup_plots_nothing_it_can_state(self):
        board = source_board()
        board.setup = None
        self.assertEqual(plotparams.plotted_layers(board), ())


class SelectLayersTest(unittest.TestCase):
    """The edit on a board, and what it is careful not to touch."""

    def test_it_reports_the_selection_before_and_after(self):
        board = source_board()
        self.assertEqual(
            plotparams.select_layers(board, ["User.16"]),
            (CORPUS_SELECTION, CORPUS_WITH_DIMENSIONS),
        )
        self.assertEqual(board.setup.pcbplotparams.layerselection,
                         CORPUS_WITH_DIMENSIONS)

    def test_a_board_with_no_setup_says_so_rather_than_inventing_one(self):
        board = source_board()
        board.setup = None
        self.assertIsNone(plotparams.select_layers(board, ["User.16"]))
        self.assertIsNone(board.setup)

    def test_it_does_not_write_through_a_shared_setup(self):
        """The fence for `cheap_copy`: two panels share one setup object."""
        original = source_board()
        copy = cheap_copy(original)
        self.assertIs(copy.setup, original.setup)

        plotparams.select_layers(copy, ["User.16"])

        self.assertEqual(copy.setup.pcbplotparams.layerselection,
                         CORPUS_WITH_DIMENSIONS)
        self.assertEqual(original.setup.pcbplotparams.layerselection,
                         CORPUS_SELECTION)
        self.assertIsNot(copy.setup, original.setup)


class WiringTest(unittest.TestCase):
    """The selection inside a real panel build."""

    def panel(self, **kwargs):
        return build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     **kwargs)

    def selection_of(self, outcome) -> str:
        return outcome.board.setup.pcbplotparams.layerselection

    def test_a_panel_with_dimensions_selects_the_dimensions_layer(self):
        outcome = self.panel(dimensions=PanelDimensionsConfig())
        self.assertEqual(outcome.panelize.dimensions.layer, "User.16")
        self.assertEqual(self.selection_of(outcome), CORPUS_WITH_DIMENSIONS)
        self.assertIn("User.16", plotparams.plotted_layers(outcome.board))

    def test_the_design_selection_survives_underneath_it(self):
        outcome = self.panel(dimensions=PanelDimensionsConfig())
        self.assertTrue(
            plotparams.parse_selection(CORPUS_SELECTION)
            < plotparams.parse_selection(self.selection_of(outcome))
        )

    def test_dimensions_declined_leaves_the_selection_exactly_as_it_was(self):
        outcome = self.panel(dimensions=None)
        self.assertIsNone(outcome.panelize.dimensions)
        self.assertEqual(self.selection_of(outcome), CORPUS_SELECTION)

    def test_the_dimensions_layer_follows_the_config(self):
        """`panel.dimensions.layer` moves them, and the selection follows."""
        outcome = self.panel(
            dimensions=PanelDimensionsConfig(layer="Dwgs.User")
        )
        self.assertEqual(outcome.panelize.dimensions.layer, "Dwgs.User")
        self.assertTrue(
            plotparams.selects(self.selection_of(outcome), "Dwgs.User")
        )
        self.assertFalse(
            plotparams.selects(self.selection_of(outcome), "User.16")
        )

    def test_the_stepped_panel_kept_for_the_next_edit_is_untouched(self):
        """A carrier-only edit must not leak the selection into the cache."""
        from eedesignpanels.stages.step_and_repeat import finish, stepped_build

        stepped = stepped_build(source_board(), 2, 1, 3.0, rails=5.0)
        finished = finish(stepped, dimensions=PanelDimensionsConfig(),
                          fill_rails=False)
        self.assertEqual(
            finished.board.setup.pcbplotparams.layerselection,
            CORPUS_WITH_DIMENSIONS,
        )
        self.assertEqual(
            stepped.board.setup.pcbplotparams.layerselection,
            CORPUS_SELECTION,
        )

    def test_two_builds_select_the_same_layers(self):
        """Same input, same output, at the granularity this change touches."""
        first = self.panel(dimensions=PanelDimensionsConfig())
        second = self.panel(dimensions=PanelDimensionsConfig())
        self.assertEqual(self.selection_of(first), self.selection_of(second))
        self.assertEqual(plotparams.plotted_layers(first.board),
                         plotparams.plotted_layers(second.board))


class SweepTest(unittest.TestCase):
    """The builder's OWN plotted outputs, swept for the same gap.

    Three of them render from one seam - `review.render.layers_of` over the
    board's `(layers …)` block, filtered by `review.layers`: the frozen review
    artifact, the gerber comparison's own export, and the live preview's
    content phase. So the sweep is one assertion: a declared dimensions layer
    is in that list, and every consumer of the list therefore inks it.

    The fourth output is the one this module exists for - the panel's stored
    plot layer selection, which nothing derives from the layer table and which
    is covered above.
    """

    def test_the_shared_layer_list_names_the_dimensions_layer(self):
        from eedesignpanels.review import render

        listed = render.select_layers(
            render.layers_of(source_board()), "all"
        )
        names = [layer.canonical_name for layer in listed]
        self.assertIn("User.16", names)
        # ...and the gerber/svg filename key is the DISPLAY name, which is how
        # the exported file is found again.
        spec = next(
            layer for layer in listed if layer.canonical_name == "User.16"
        )
        self.assertEqual(spec.svg_suffix, "User_16-Dimensions")

    def test_naming_it_explicitly_is_also_honoured(self):
        """`review.layers` as a list, which is the other config shape."""
        from eedesignpanels.review import render

        listed = render.select_layers(
            render.layers_of(source_board()), ["User.16"]
        )
        self.assertEqual([layer.canonical_name for layer in listed],
                         ["User.16"])


if __name__ == "__main__":
    unittest.main()
