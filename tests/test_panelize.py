"""The panel half of the build, checked on a board assembled in memory.

No file and no KiCad. What is pinned here is what the stage promises: EVERY
`Edge.Cuts` item the instances brought goes - board-level and footprint-hosted
alike - the blank is the grid grown by its rails, the panel outline is
the boundary of the blank minus what the cutter removed, six instance score
lines on two blade lines become two panel passes, no COPPER zone survives, and
every rule area does.

The RAILS are four numbers. Four equal ones build the same panel as one shared
width, to the byte; four different ones move only where the blank's own edges
sit.
"""

import math
import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    PcbArc,
    PcbGroup,
    PcbLine,
    Stroke,
    Zone,
    ZoneFilledPolygon,
    ZoneKeepout,
)

from eedesignpanels import outline_check
from eedesignpanels.channels import Rails, Rect
from eedesignpanels.stages import panelize
from eedesignpanels.stages.panel_scores import RULE_AREA_NAME
from eedesignpanels.stages.panelize import (
    PANEL_OWNED_LAYERS,
    apply,
    blade_lines,
    board_items_on,
    edge_key,
    footprint_items_on,
    merge_scores,
    outline_width_of,
    blank_extent_mm,
    primitive_of,
    strip_outline,
)
from eedesignpanels.toolpath import distance_to_primitive, midpoint_of

WIDTH = 0.889          # the corpus cutter, so the channel is 0.889 mm wide
RADIUS = WIDTH / 2.0
OUTLINE_WIDTH = 0.05


def edge(x0, y0, x1, y1, layer="Edge.Cuts", width=OUTLINE_WIDTH, uid=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer,
        stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def outline_of(x0, y0, x1, y1, tag="i"):
    """Four `Edge.Cuts` lines round a rectangle."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        edge(*corners[index], *corners[(index + 1) % 4],
             uid=f"{tag}-edge-{index}")
        for index in range(4)
    ]


def channel_of(x0, y0, x1, y1, tag="c"):
    """One toolpath run: three sides of a rectangle, drawn at cutter width."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        edge(*corners[index], *corners[(index + 1) % 4], layer="User.36",
             width=WIDTH, uid=f"{tag}-path-{index}")
        for index in range(3)
    ]


def panel_board(rows=2) -> Board:
    """Two instances stacked 20 mm apart, each routed and each scored.

    Instance n sits at y = 20n: a 10 x 10 outline, a channel round three of its
    sides half a cutter clear of it, and a score line down its left edge.
    """
    board = Board(
        layers=[
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user"),
            BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
        ]
    )
    for row in range(rows):
        base = 20.0 * row
        board.drawings += outline_of(0.0, base, 10.0, base + 10.0, f"i{row}")
        board.drawings += channel_of(
            -RADIUS, base - RADIUS, 10.0 + RADIUS, base + 10.0 + RADIUS,
            f"c{row}",
        )
        board.drawings.append(PcbLine(
            start=(0.0, base), end=(0.0, base + 10.0), layer="User.38",
            stroke=Stroke(width=0.1), id=f"score-{row}",
        ))
    return board


def tab_footprint(tag="1") -> Footprint:
    """A tab whose break contour lives on `Edge.Cuts` inside the footprint.

    It carries one item on another layer too, because the strip must take the
    contour and leave everything else in the footprint alone.
    """
    return Footprint(
        fp_id="PanelTab:MouseBite",
        layer="F.Cu",
        uuid=f"tab-{tag}",
        at={"position": (5.0, 0.0), "angle": 0.0},
        drawings=[
            PcbLine(start=(-0.5, 0.0), end=(0.5, 0.0), layer="Edge.Cuts",
                    stroke=Stroke(width=OUTLINE_WIDTH),
                    id=f"tab-contour-{tag}"),
            PcbArc(start=(-0.5, 0.0), mid=(0.0, 0.2), end=(0.5, 0.0),
                   layer="Edge.Cuts", stroke=Stroke(width=OUTLINE_WIDTH),
                   id=f"tab-scallop-{tag}"),
            PcbLine(start=(-0.3, 0.0), end=(-0.3, 0.4), layer="User.8",
                    stroke=Stroke(width=0.05), id=f"tab-stop-{tag}"),
        ],
    )


class BlankTest(unittest.TestCase):
    """The blank: the instance grid grown by its four rails, and nothing else."""

    def test_the_blank_extent_is_the_grid_plus_two_rails(self):
        self.assertEqual(blank_extent_mm((34.0, 37.5), 5.0), (44.0, 47.5))

    def test_a_zero_rail_leaves_the_grid_extent_alone(self):
        self.assertEqual(blank_extent_mm((34.0, 37.5), 0.0), (34.0, 37.5))

    def test_the_stage_grows_the_blank_from_the_instances(self):
        made = apply(panel_board(), 5.0)
        self.assertEqual(made.instance_bbox, (0.0, 0.0, 10.0, 30.0))
        self.assertEqual(made.blank, Rect(-5.0, -5.0, 15.0, 35.0))
        self.assertEqual(made.blank_extent_mm, (20.0, 40.0))
        self.assertEqual(made.rails, Rails(5.0, 5.0, 5.0, 5.0))

    # -- per-side rails ----------------------------------------------------

    def test_one_number_and_four_equal_ones_are_the_same_blank(self):
        # THE COMPATIBILITY STATEMENT. Uniform rails are today's geometry, so a
        # linked rail width and the four it stands for cannot differ.
        one = apply(panel_board(), 5.0)
        four = apply(panel_board(), Rails(5.0, 5.0, 5.0, 5.0))
        self.assertEqual(one.blank, four.blank)
        self.assertEqual(one.rails, four.rails)

    def test_each_side_grows_its_own_edge(self):
        # File coordinates run y DOWNWARD, so `top` is at min_y.
        made = apply(panel_board(), Rails(top=1.0, bottom=2.0, left=3.0,
                                          right=4.0))
        self.assertEqual(made.blank, Rect(-3.0, -1.0, 14.0, 32.0))
        self.assertEqual(made.blank_extent_mm, (17.0, 33.0))
        self.assertFalse(made.rails.uniform)

    def test_a_zero_side_removes_that_rail_and_leaves_the_others(self):
        made = apply(panel_board(), Rails(top=0.0, bottom=5.0, left=5.0,
                                          right=5.0))
        self.assertEqual(made.blank, Rect(-5.0, 0.0, 15.0, 35.0))
        # The instances' own top edge IS the panel's there.
        self.assertEqual(made.blank.min_y, made.instance_bbox[1])

    def test_the_blank_extent_follows_the_four_rails(self):
        self.assertEqual(
            blank_extent_mm((34.0, 37.5),
                            Rails(top=1.0, bottom=2.0, left=3.0, right=4.0)),
            (41.0, 40.5),
        )

    def test_the_rails_read_back_in_words_a_report_can_use(self):
        self.assertEqual(Rails(5.0, 5.0, 5.0, 5.0).summary, "5 mm rails")
        self.assertEqual(
            Rails(top=8.0, bottom=6.0, left=3.0, right=0.0).summary,
            "rails 8 mm top, 6 mm bottom, 3 mm left, 0 mm right",
        )

    def test_the_linked_view_of_four_rails_is_the_widest(self):
        # What re-linking applies to every side: collapsing four rails into one
        # must not quietly delete material somebody asked for.
        self.assertEqual(Rails(top=8.0, bottom=6.0, left=3.0, right=0.0)
                         .linked_mm, 8.0)

    def test_a_negative_rail_is_refused_by_side(self):
        with self.assertRaises(ValueError) as caught:
            apply(panel_board(), Rails(top=1.0, bottom=-1.0, left=1.0,
                                       right=1.0))
        self.assertIn("bottom", str(caught.exception))


class ReadingTest(unittest.TestCase):
    def test_a_collinear_arc_is_read_as_the_line_between_its_ends(self):
        sliver = PcbArc(start=(0.0, 0.0), mid=(0.5, 0.0), end=(1.0, 0.0),
                        layer="User.36", stroke=Stroke(width=WIDTH), id="s")
        primitive = primitive_of(sliver, "s")
        self.assertEqual(primitive.start, (0.0, 0.0))
        self.assertEqual(primitive.end, (1.0, 0.0))

    def test_a_real_arc_stays_an_arc(self):
        bend = PcbArc(start=(1.0, 0.0), mid=(0.0, 1.0), end=(-1.0, 0.0),
                      layer="User.36", stroke=Stroke(width=WIDTH), id="b")
        self.assertAlmostEqual(primitive_of(bend, "b").radius, 1.0)

    def test_a_shape_the_stage_cannot_offset_is_named(self):
        class Rectangle:
            pass

        with self.assertRaises(ValueError) as caught:
            primitive_of(Rectangle(), "r")
        self.assertIn("Rectangle", str(caught.exception))

    def test_the_outline_width_is_the_commonest_the_instances_used(self):
        items = [edge(0, 0, 1, 0, width=0.1), edge(0, 0, 1, 0, width=0.05),
                 edge(0, 0, 1, 0, width=0.05)]
        self.assertEqual(outline_width_of(items), 0.05)

    def test_no_outline_to_learn_from_takes_the_kicad_default(self):
        self.assertEqual(outline_width_of([]), panelize.DEFAULT_OUTLINE_WIDTH_MM)


class OutlineTest(unittest.TestCase):
    def test_the_instances_board_outlines_are_removed(self):
        board = panel_board()
        made = apply(board, 5.0)
        self.assertEqual(made.strip.board_items, 8)
        remaining = board_items_on(board, "Edge.Cuts")
        self.assertTrue(remaining)
        self.assertNotIn(
            "i0-edge-0", {item.id for item in remaining}
        )

    def test_the_tabs_contours_go_too_and_nothing_else_in_the_footprint_does(self):
        board = panel_board()
        board.footprints.append(tab_footprint())
        made = apply(board, 5.0)
        self.assertEqual(made.strip.footprint_items, 2)
        self.assertEqual(made.strip.footprints_touched, 1)
        self.assertEqual(footprint_items_on(board, "Edge.Cuts"), [])
        self.assertEqual(
            [item.id for item in board.footprints[0].drawings], ["tab-stop-1"]
        )

    def test_every_edge_cuts_item_removed_is_counted_by_kind(self):
        board = panel_board()
        board.footprints.append(tab_footprint())
        made = apply(board, 5.0)
        self.assertEqual(made.strip.by_kind, (("PcbArc", 1), ("PcbLine", 9)))
        self.assertEqual(made.strip.total, 10)
        self.assertEqual(made.outlines_removed, 10)

    def test_the_panel_carries_only_derived_edge_cuts_afterwards(self):
        board = panel_board()
        board.footprints.append(tab_footprint("a"))
        board.footprints.append(tab_footprint("b"))
        apply(board, 5.0)
        self.assertEqual(footprint_items_on(board, "Edge.Cuts"), [])
        brought = {"i0-edge-0", "i1-edge-0", "tab-contour-a", "tab-contour-b"}
        left = {item.id for item in board_items_on(board, "Edge.Cuts")}
        self.assertEqual(brought & left, set())

    def test_a_footprint_with_no_outline_is_not_touched_at_all(self):
        board = panel_board()
        plain = Footprint(
            fp_id="R:0402", layer="F.Cu", uuid="r1",
            at={"position": (5.0, 5.0), "angle": 0.0},
            drawings=[PcbLine(start=(0.0, 0.0), end=(1.0, 0.0), layer="F.SilkS",
                              stroke=Stroke(width=0.1), id="silk")],
        )
        board.footprints.append(plain)
        before = plain.model_dump()
        made = apply(board, 5.0)
        self.assertEqual(made.strip.footprints_touched, 0)
        self.assertEqual(board.footprints[0].model_dump(), before)

    def test_an_item_on_edge_cuts_and_another_layer_is_named(self):
        board = panel_board()
        board.drawings.append(PcbLine(
            start=(0.0, 0.0), end=(1.0, 0.0), layer="Edge.Cuts",
            layers=["Edge.Cuts", "F.SilkS"],
            stroke=Stroke(width=0.05), id="both",
        ))
        made = apply(board, 5.0)
        self.assertEqual(made.strip.multi_layer, 1)
        self.assertTrue(any("alongside another layer" in w
                            for w in made.warnings))

    def test_the_panel_outline_is_the_sheet_minus_the_channels(self):
        made = apply(panel_board(), 5.0)
        # The sheet, and one hole per routed run.
        self.assertEqual(made.loops, 3)
        self.assertEqual(made.boundary.extent_mm, (20.0, 40.0))
        self.assertAlmostEqual(
            sum(piece.length for piece in made.boundary.loops[0]), 120.0
        )

    def test_the_outline_is_drawn_at_the_width_the_instances_used(self):
        board = panel_board()
        made = apply(board, 5.0)
        widths = {
            item.stroke.width for item in board_items_on(board, "Edge.Cuts")
        }
        self.assertEqual(widths, {OUTLINE_WIDTH})
        self.assertEqual(made.outline_width_mm, OUTLINE_WIDTH)

    def test_the_outline_items_are_lines_and_true_arcs(self):
        board = panel_board()
        apply(board, 5.0)
        kinds = {
            type(item).__name__ for item in board_items_on(board, "Edge.Cuts")
        }
        self.assertEqual(kinds, {"PcbLine", "PcbArc"})

    def test_every_outline_item_has_a_uuid_derived_from_its_geometry(self):
        first = panel_board()
        second = panel_board()
        apply(first, 5.0)
        apply(second, 5.0)
        self.assertEqual(
            [item.id for item in board_items_on(first, "Edge.Cuts")],
            [item.id for item in board_items_on(second, "Edge.Cuts")],
        )
        ids = [item.id for item in board_items_on(first, "Edge.Cuts")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_identity_key_is_the_geometry_and_nothing_else(self):
        from eedesignpanels.toolpath import Line

        one = Line(p0=(1.0, 2.0), p1=(3.0, 4.0), source="a")
        other = Line(p0=(1.0, 2.0), p1=(3.0, 4.0), source="b")
        self.assertEqual(edge_key(one), edge_key(other))

    def test_no_channels_leaves_the_bare_sheet_and_says_so(self):
        board = panel_board()
        board.drawings = [
            item for item in board.drawings if item.layer != "User.36"
        ]
        made = apply(board, 5.0)
        self.assertEqual(made.loops, 1)
        self.assertTrue(any("nothing is routed" in w for w in made.warnings))

    def test_a_channel_reaching_the_outer_edge_is_reported(self):
        made = apply(panel_board(), 0.2)
        self.assertTrue(made.boundary.reaches_outer_edge)
        self.assertTrue(any("reaches the panel's outer edge" in w
                            for w in made.warnings))


class ScoreMergeTest(unittest.TestCase):
    def test_collinear_instance_scores_become_one_panel_pass(self):
        board = panel_board()
        made = apply(board, 5.0)
        self.assertEqual(made.scores.replaced, 2)
        self.assertEqual(made.scores.merged_count, 1)
        blade = made.scores.blades[0]
        self.assertEqual(len(blade.sources), 2)
        # ...produced to the sheet's own edge, top to bottom.
        self.assertEqual(blade.start, (0.0, -5.0))
        self.assertEqual(blade.end, (0.0, 35.0))
        self.assertAlmostEqual(blade.length_mm, 40.0)

    def test_distinct_blade_lines_stay_distinct(self):
        board = panel_board()
        board.drawings.append(PcbLine(
            start=(10.0, 0.0), end=(10.0, 10.0), layer="User.38",
            stroke=Stroke(width=0.1), id="score-right",
        ))
        made = apply(board, 5.0)
        self.assertEqual(made.scores.merged_count, 2)
        self.assertEqual(
            [blade.start[0] for blade in made.scores.blades], [0.0, 10.0]
        )

    def test_a_line_drawn_backwards_is_the_same_pass(self):
        board = panel_board()
        for item in board_items_on(board, "User.38"):
            item.start, item.end = item.end, item.start
        made = apply(board, 5.0)
        self.assertEqual(made.scores.merged_count, 1)
        self.assertEqual(made.scores.blades[0].start, (0.0, -5.0))

    def test_the_panel_carries_one_line_per_pass_afterwards(self):
        board = panel_board()
        apply(board, 5.0)
        lines = board_items_on(board, "User.38")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].stroke.width, 0.1)
        self.assertEqual(lines[0].start, (0.0, -5.0))

    def test_a_merged_line_keeps_one_identity_across_builds(self):
        first, second = panel_board(), panel_board()
        apply(first, 5.0)
        apply(second, 5.0)
        self.assertEqual(
            board_items_on(first, "User.38")[0].id,
            board_items_on(second, "User.38")[0].id,
        )

    def test_a_diagonal_pass_is_produced_to_the_sheet_corner(self):
        sheet = Rect(0.0, 0.0, 10.0, 10.0)
        line = PcbLine(start=(2.0, 2.0), end=(4.0, 4.0), layer="User.38",
                       stroke=Stroke(width=0.1), id="d")
        blade = blade_lines([line], sheet)[0]
        self.assertAlmostEqual(blade.start[0], 0.0)
        self.assertAlmostEqual(blade.start[1], 0.0)
        self.assertAlmostEqual(blade.length_mm, math.hypot(10.0, 10.0))

    def test_a_score_that_is_not_a_line_is_left_alone_and_named(self):
        board = panel_board()
        board.drawings.append(PcbArc(
            start=(1.0, 1.0), mid=(2.0, 2.0), end=(3.0, 1.0), layer="User.38",
            stroke=Stroke(width=0.1), id="bent-score",
        ))
        warnings = []
        merged = merge_scores(board, Rect(-5.0, -5.0, 15.0, 35.0), warnings)
        self.assertEqual(merged.kept, 1)
        self.assertTrue(any("not lines" in w for w in warnings))
        self.assertIn(
            "bent-score", {item.id for item in board_items_on(board, "User.38")}
        )

    def test_a_panel_with_no_scores_merges_nothing(self):
        board = panel_board()
        board.drawings = [
            item for item in board.drawings if item.layer != "User.38"
        ]
        made = apply(board, 5.0)
        self.assertEqual(made.scores.merged_count, 0)
        self.assertEqual(made.scores.replaced, 0)


class ZoneTest(unittest.TestCase):
    """Panelizing owns the zones too: copper goes, the rule areas stay."""

    def zoned_board(self) -> Board:
        board = panel_board()
        board.zones.append(Zone(
            layer="F.Cu", net_name="AGND_1", id="zone-1",
            fill={"filled": True},
            filled_polygons=[ZoneFilledPolygon(
                layer="F.Cu",
                pts=[(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)],
            )],
        ))
        return board

    def keepout_board(self) -> Board:
        board = self.zoned_board()
        board.zones.append(Zone(
            layer="F.Cu", id="keepout-1",
            keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                                copper_pour=False, footprints=False),
            polygons=[{"pts": [(2.0, 2.0), (3.0, 2.0), (3.0, 3.0)]}],
        ))
        board.footprints.append(Footprint(
            fp_id="PanelTab:MouseBite", layer="F.Cu", uuid="fp-keepout",
            at={"position": (5.0, 0.0), "angle": 0.0},
            zones=[Zone(
                layer="F.Cu", id="keepout-2",
                keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                                    copper_pour=False, footprints=False),
                polygons=[{"pts": [(4.0, 4.0), (5.0, 4.0), (5.0, 5.0)]}],
            )],
        ))
        return board

    def test_the_panel_holds_no_copper_zone_afterwards(self):
        board = self.zoned_board()
        made = apply(board, 5.0)
        # The only zone left is the keepout round the merged saw pass, which
        # every panel with a score line gets whether or not a config named the
        # scores section.
        self.assertEqual([zone.name for zone in board.zones], [RULE_AREA_NAME])
        self.assertEqual(made.zones.filled_zones, 1)
        self.assertEqual(made.zones.polygons, 1)

    def test_every_rule_area_survives_the_panel(self):
        board = self.keepout_board()
        made = apply(board, 5.0)
        self.assertEqual(
            [zone.id for zone in board.zones
             if zone.name != RULE_AREA_NAME],
            ["keepout-1"],
        )
        self.assertEqual(
            [zone.id for zone in board.footprints[0].zones], ["keepout-2"]
        )
        self.assertEqual(made.zones.rule_areas, 1)
        self.assertEqual(made.zones.footprint_rule_areas, 1)
        self.assertEqual(made.zones.rule_areas_kept, 2)
        self.assertEqual(made.zones.zones_removed, 1)

    def test_the_fill_is_on_the_board_as_copper(self):
        board = self.zoned_board()
        apply(board, 5.0)
        polys = [item for item in board.drawings
                 if type(item).__name__ == "PcbPoly"]
        self.assertEqual(len(polys), 1)
        self.assertEqual(polys[0].layer, "F.Cu")
        self.assertEqual(polys[0].net_name, "AGND_1")

    def test_flattening_does_not_disturb_the_derived_outline(self):
        plain = apply(panel_board(), 5.0)
        zoned = apply(self.zoned_board(), 5.0)
        self.assertEqual(zoned.loops, plain.loops)
        self.assertEqual(zoned.boundary.extent_mm, plain.boundary.extent_mm)


class StripTest(unittest.TestCase):
    def test_a_group_of_nothing_but_outline_items_is_dropped_and_named(self):
        board = panel_board()
        board.groups.append(PcbGroup(name="outline", id="g1",
                                     members=["i0-edge-0", "i0-edge-1"]))
        warnings = []
        strip = strip_outline(board, warnings)
        self.assertEqual(strip.groups_dropped, 1)
        self.assertEqual(board.groups, [])
        self.assertTrue(any("were dropped with them" in w for w in warnings))

    def test_a_board_with_no_outline_strips_nothing_and_says_nothing(self):
        board = Board()
        warnings = []
        strip = strip_outline(board, warnings)
        self.assertEqual(strip.total, 0)
        self.assertEqual(strip.by_kind, ())
        self.assertEqual(warnings, [])


class SteppedEdgeTest(unittest.TestCase):
    """A step in a board's edge, routed at a 0.2 mm cutter.

    The board level of the minimum-tab corner case (`test_mintab`'s
    `TurnedCornerTest` holds the arithmetic). The run travels the upper edge,
    turns a right angle round the material's corner and fillets away along the
    lower edge. Asked pairwise, the leg before the corner and the fillet after
    it claim 0.3795 mm of material, and a join built on that chamfers the
    board's corner off - a straight panel edge across the toolpath band.

    What is fenced here is the property a reviewer reads off the
    picture: EVERY piece of the derived panel edge sits one cutter radius from
    some toolpath item, or on the blank's own rectangle. Nothing else can be a
    panel edge, because nothing else is a face the cutter left.
    """

    SMALL = 0.2
    APEX = (10.0, 0.0)

    def board(self) -> Board:
        radius = self.SMALL / 2.0
        board = Board(layers=[
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts",
                       layer_type="user"),
            BoardLayer(ordinal=36, canonical_name="User.36",
                       layer_type="user"),
        ])
        board.drawings += outline_of(0.0, 0.0, 20.0, 10.0, "step")
        board.drawings += [
            edge(8.0, -radius, 9.5561, -radius, layer="User.36",
                 width=self.SMALL, uid="p-upper"),
            edge(9.5561, -radius, 10.0, -radius, layer="User.36",
                 width=self.SMALL, uid="p-upper-end"),
            PcbArc(start=(10.0, -radius), mid=(10.070711, -0.070711),
                   end=(10.0 + radius, 0.0), layer="User.36",
                   stroke=Stroke(width=self.SMALL), id="p-corner"),
            edge(10.0 + radius, 0.0, 10.0 + radius, 0.1, layer="User.36",
                 width=self.SMALL, uid="p-step"),
            PcbArc(start=(10.0 + radius, 0.1), mid=(10.217157, 0.382843),
                   end=(10.5, 0.5), layer="User.36",
                   stroke=Stroke(width=self.SMALL), id="p-fillet"),
            edge(10.5, 0.5, 12.0, 0.5, layer="User.36", width=self.SMALL,
                 uid="p-lower"),
        ]
        return board

    def stepped(self):
        return panelize.stepped(self.board(), 2.0, min_tab_mm=0.5)

    def test_the_corner_leaves_no_sliver_to_consume(self):
        made = self.stepped()
        self.assertEqual(made.joins, ())
        self.assertEqual(
            [note for note in made.warnings if "sliver" in note], []
        )

    def test_every_derived_panel_edge_piece_is_a_face_the_cutter_left(self):
        made = self.stepped()
        radius = self.SMALL / 2.0
        path = [
            primitive_of(item, item.id or "")
            for item in board_items_on(self.board(), "User.36")
        ]
        blank = made.blank
        for piece in made.outline:
            middle = midpoint_of(piece)
            if min(abs(middle[0] - blank.min_x), abs(middle[0] - blank.max_x),
                   abs(middle[1] - blank.min_y),
                   abs(middle[1] - blank.max_y)) < 1e-9:
                continue
            off = min(
                abs(distance_to_primitive(middle, item) - radius)
                for item in path
            )
            self.assertLess(off, 1e-6, f"{piece} stands {off:.6f} mm off")

    def test_the_panel_edge_keeps_the_board_corner(self):
        corners = [
            piece for piece in self.stepped().outline
            if math.dist(piece.start, self.APEX) < 1e-6
        ]
        self.assertEqual(len(corners), 1)

    def test_the_panel_is_the_blank_and_one_hole(self):
        self.assertEqual(self.stepped().boundary.loop_count, 2)

    def test_the_same_board_derives_the_same_outline_twice(self):
        self.assertEqual(self.stepped().outline, self.stepped().outline)


class OracleTest(unittest.TestCase):
    """The independent verdict, on every panel this module builds.

    Not a loop count and not a piece-by-piece comparison against an expected
    answer: the outline is checked against the DEFINITION of the region it is
    supposed to be the boundary of - :mod:`eedesignpanels.outline_check`.
    That is what makes a wrong outline which happens to close still
    fail, which is how three batches of defects got past a loop count.
    """

    def judge(self, made, min_tab_mm=0.5):
        report = outline_check.check(
            made.blank, made.boundary.loops, made.cut_channels,
            min_tab_mm=min_tab_mm,
        )
        self.assertTrue(
            report.ok,
            report.summary + "\n" + "\n".join(
                str(defect) for defect in report.defects[:6]
            ),
        )
        return report

    def test_the_two_instance_panel_is_a_valid_outline(self):
        self.judge(apply(panel_board(), 5.0))

    def test_it_is_valid_with_four_different_rails_too(self):
        self.judge(apply(panel_board(), Rails(2.0, 7.0, 3.0, 11.0)))

    def test_the_panel_with_a_tab_footprint_on_it_is_valid(self):
        board = panel_board()
        board.footprints.append(tab_footprint())
        self.judge(apply(board, 5.0))

    def test_the_checker_is_not_passing_everything_it_is_shown(self):
        """The control: one wall moved a third of a millimetre, and it fails."""
        made = apply(panel_board(), 5.0)
        loops = list(made.boundary.loops)
        pieces = list(loops[0])
        moved = pieces[0]
        pieces[0] = moved._replace(
            p0=(moved.p0[0], moved.p0[1] + 0.3),
            p1=(moved.p1[0], moved.p1[1] + 0.3),
        )
        loops[0] = tuple(pieces)
        report = outline_check.check(
            made.blank, tuple(loops), made.cut_channels
        )
        self.assertFalse(report.ok, report.summary)


class PanelOwnedTest(unittest.TestCase):
    def test_the_outline_and_the_scores_are_the_panels_own(self):
        self.assertEqual(PANEL_OWNED_LAYERS, frozenset({"Edge.Cuts", "User.38"}))


if __name__ == "__main__":
    unittest.main()
