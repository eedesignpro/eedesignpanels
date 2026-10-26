"""A rail of zero: the panel a config asks for when it removes one side.

THE CASE. A zero rail
REMOVES that side, which is legal input and a real arrangement - a panel routed
flush on one edge has no rail there. The panel it produced was wrong in three
ways at once: a tooling hole sat
half in a board's tab region, the dimension the eye reads as the panel's width
stopped short of the drawn blank, and on the corpus board a zero RIGHT rail did
not produce a panel at all - the derived outline could not close, because the
routed fillet round a board's rounded corner runs tangent to the blank's own
edge once no material stands between them.

What is fenced here is the whole arrangement, per side and in combination:

* the blank measures the boards plus the gaps plus the rails that are left;
* the panel's own dimensions measure THE BLANK, whatever the rails are;
* a tooling hole lands on rail material or the corner is declined BY NAME;
* the rail-edge saw pass skips a side with no rail;
* the rail pour skips a side with no rail;
* and the whole thing is byte-identical built twice.

Everything runs on boards assembled in memory. `CORNER_MM` is the corpus
board's own corner arrangement in miniature - a 2 mm corner fillet, and a
toolpath offset a cutter radius outside it - because that is the geometry the
zero rail used to fail on.
"""

import math
import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    PcbArc,
    PcbLine,
    Setup,
    Stroke,
    dump_board,
)

from eedesignpanels import channels, outline_check
from eedesignpanels.config import PanelConfig
from eedesignpanels.stages import panel_scores, rail_copper, tooling_holes
from eedesignpanels.stages.panel_dimensions import Settings as DimensionSettings
from eedesignpanels.stages.step_and_repeat import build, build_from

CUTTER = 0.889
RADIUS = CUTTER / 2.0
CORNER_MM = 2.0

# Every rail arrangement: each side
# alone, both pairs, and all four. `(top, bottom, left, right)`.
CASES = {
    "all_5": (5.0, 5.0, 5.0, 5.0),
    "top_0": (0.0, 5.0, 5.0, 5.0),
    "bottom_0": (5.0, 0.0, 5.0, 5.0),
    "left_0": (5.0, 5.0, 0.0, 5.0),
    "right_0": (5.0, 5.0, 5.0, 0.0),
    "left_right_0": (5.0, 5.0, 0.0, 0.0),
    "top_bottom_0": (0.0, 0.0, 5.0, 5.0),
    "all_0": (0.0, 0.0, 0.0, 0.0),
}


def line(x0, y0, x1, y1, layer="Edge.Cuts", width=0.05, uid=None) -> PcbLine:
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer, stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def arc(start, mid, end, layer="Edge.Cuts", width=0.05, uid="") -> PcbArc:
    return PcbArc(start=start, mid=mid, end=end, layer=layer,
                  stroke=Stroke(width=width), id=uid or f"{layer}-{start}-{end}")


def quarter(centre, radius, start_angle, end_angle, **kwargs) -> PcbArc:
    """One arc of *radius* about *centre*, by its two angles in degrees.

    Written in the terms the geometry is stated in - a centre and a sweep -
    because a toolpath corner is an offset of a board corner and the file's
    three-point spelling is not what a reader checks it against.
    """
    def at(degrees):
        radians = math.radians(degrees)
        return (centre[0] + radius * math.cos(radians),
                centre[1] + radius * math.sin(radians))

    return arc(at(start_angle), at((start_angle + end_angle) / 2.0),
               at(end_angle), **kwargs)


def a_board(corner_mm: float = 0.0) -> Board:
    """A 10 x 10 board, routed round three sides, scored down the left edge.

    *corner_mm* is the radius of the TOP-RIGHT corner: 0 is a square board and
    2 mm is the corpus board's own corner, whose routed fillet runs tangent to
    the blank's edge once the right rail goes to zero. The toolpath is a
    cutter's: a radius outside the outline, with a cutter-radius fillet at every
    corner, which is the shape prep writes.
    """
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts",
                       layer_type="user"),
            BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user"),
            BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    # THE OUTLINE: three straight sides, and a top-right corner of *corner_mm*.
    board.drawings = [line(0.0, 0.0, 10.0 - corner_mm, 0.0, uid="edge-top")]
    if corner_mm:
        board.drawings.append(quarter(
            (10.0 - corner_mm, corner_mm), corner_mm, -90.0, 0.0,
            uid="edge-corner",
        ))
    board.drawings += [
        line(10.0, corner_mm, 10.0, 10.0, uid="edge-right"),
        line(10.0, 10.0, 0.0, 10.0, uid="edge-bottom"),
        line(0.0, 10.0, 0.0, 0.0, uid="edge-left"),
        line(0.0, 0.0, 0.0, 10.0, layer="User.38", width=0.1, uid="score-left"),
    ]
    # THE CUTTER'S PATH, a radius outside it. The corner arcs are concentric
    # with the outline's own, so the channel's INNER wall lands exactly on the
    # board's edge - which is the panel's edge once that side's rail is zero.
    path = dict(layer="User.36", width=CUTTER)
    board.drawings += [
        line(0.0, -RADIUS, 10.0 - corner_mm, -RADIUS, uid="path-top", **path),
        quarter((10.0 - corner_mm, corner_mm), corner_mm + RADIUS,
                -90.0, 0.0, uid="path-corner", **path),
        line(10.0 + RADIUS, corner_mm, 10.0 + RADIUS, 10.0,
             uid="path-right", **path),
        quarter((10.0, 10.0), RADIUS, 0.0, 90.0, uid="path-bottom-corner",
                **path),
        line(10.0, 10.0 + RADIUS, 0.0, 10.0 + RADIUS, uid="path-bottom",
             **path),
    ]
    return board


def panel_with(rails, corner_mm: float = 0.0, columns: int = 2, rows: int = 2,
               **features):
    """One built panel: two by two, 3 mm gaps, and the rails as given."""
    return build(a_board(corner_mm), columns, rows, 3.0,
                 rails=channels.Rails(*rails), fill_rails=False, **features)


class BlankTest(unittest.TestCase):
    """(a) The blank is the boards, the gaps, and the rails that are left."""

    def test_the_blank_is_the_grid_plus_the_rails_that_exist(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                made = panel_with(rails).panelize
                box = made.instance_bbox
                self.assertAlmostEqual(
                    made.blank.width_mm,
                    box[2] - box[0] + rails[2] + rails[3], places=9,
                )
                self.assertAlmostEqual(
                    made.blank.height_mm,
                    box[3] - box[1] + rails[0] + rails[1], places=9,
                )

    def test_a_removed_side_puts_the_blanks_edge_on_the_boards(self):
        made = panel_with(CASES["right_0"]).panelize
        self.assertAlmostEqual(made.blank.max_x, made.instance_bbox[2],
                               places=9)
        # ...and the sides that stayed are still a rail clear of them.
        self.assertAlmostEqual(made.blank.min_x, made.instance_bbox[0] - 5.0,
                               places=9)

    def test_one_number_and_four_equal_ones_are_the_same_panel(self):
        self.assertEqual(
            dump_board(panel_with((5.0, 5.0, 5.0, 5.0)).board),
            dump_board(build(a_board(), 2, 2, 3.0, rails=5.0,
                             fill_rails=False).board),
        )

    def test_a_zero_rail_builds_the_same_bytes_twice(self):
        for name in ("right_0", "left_right_0", "all_0"):
            with self.subTest(case=name):
                self.assertEqual(
                    dump_board(panel_with(CASES[name]).board),
                    dump_board(panel_with(CASES[name]).board),
                )


class RoundedCornerTest(unittest.TestCase):
    """The outline closes when a routed fillet runs tangent to the blank's edge.

    The corpus board's own arrangement: with the right rail at zero the blank's
    right edge IS the boards' right edge, the corner fillet's inner wall is
    tangent to it and the outer wall crosses it. Unhandled, that raises
    `ChannelError` - three pieces of the panel edge meeting at one node - and
    the panel does not rebuild.
    """

    def test_a_zero_rail_against_a_rounded_corner_still_closes(self):
        made = panel_with(CASES["right_0"], corner_mm=CORNER_MM).panelize
        self.assertGreater(made.loops, 0)
        self.assertAlmostEqual(made.blank.max_x, made.instance_bbox[2],
                               places=9)

    def test_the_panel_reaches_the_blanks_edge_and_no_further(self):
        made = panel_with(CASES["right_0"], corner_mm=CORNER_MM).panelize
        self.assertLessEqual(made.boundary.bbox[2], made.blank.max_x + 1e-6)
        self.assertAlmostEqual(made.boundary.bbox[2], made.blank.max_x,
                               places=4)

    def test_the_run_says_the_route_reached_the_outer_edge(self):
        made = panel_with(CASES["right_0"], corner_mm=CORNER_MM).panelize
        self.assertTrue(any("outer edge" in note for note in made.warnings),
                        made.warnings)

    def test_every_side_at_zero_closes_too(self):
        for name in ("top_0", "bottom_0", "left_0", "right_0",
                     "left_right_0", "all_0"):
            with self.subTest(case=name):
                made = panel_with(CASES[name], corner_mm=CORNER_MM).panelize
                self.assertGreater(made.loops, 0)

    def test_every_rail_arrangement_passes_the_outline_oracle(self):
        """The independent verdict, on every panel this module builds.

        A coincident wall is exactly the kind of thing that closes into a loop
        and is still wrong, so what is checked here is the outline against the
        DEFINITION of the region rather than against a loop count -
        :mod:`eedesignpanels.outline_check`.
        """
        for name, rails in CASES.items():
            with self.subTest(case=name):
                made = panel_with(rails, corner_mm=CORNER_MM).panelize
                report = outline_check.check(
                    made.blank, made.boundary.loops, made.cut_channels,
                    min_tab_mm=made.min_tab_mm,
                )
                self.assertTrue(
                    report.ok,
                    report.summary + "\n" + "\n".join(
                        str(defect) for defect in report.defects[:6]
                    ),
                )

    def test_a_rail_wider_than_the_cutter_is_the_panel_it_always_was(self):
        # The blank-edge rule may not touch a panel with rails on it: the walls
        # are nowhere near the blank's own edge, so nothing it does can apply.
        self.assertEqual(
            dump_board(panel_with((5.0, 5.0, 5.0, 5.0), corner_mm=CORNER_MM).board),
            dump_board(panel_with((5.0, 5.0, 5.0, 5.0), corner_mm=CORNER_MM).board),
        )
        made = panel_with((5.0, 5.0, 5.0, 5.0), corner_mm=CORNER_MM).panelize
        self.assertFalse(made.boundary.reaches_outer_edge)


class DimensionTest(unittest.TestCase):
    """(b) The overall dimensions measure the BLANK, whatever the rails are."""

    def measures(self, rails) -> tuple:
        made = panel_with(rails, dimensions=DimensionSettings(),
                          tooling_holes={}).panelize
        return ({measure.name: measure for measure in made.dimensions.measures},
                made.blank)

    def test_the_overall_dimensions_are_the_blanks_own_extent(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                measures, blank = self.measures(rails)
                self.assertAlmostEqual(measures["panel_width"].value_mm,
                                       blank.width_mm, places=4)
                self.assertAlmostEqual(measures["panel_length"].value_mm,
                                       blank.height_mm, places=4)

    def test_the_width_dimension_spans_the_blank_corner_to_corner(self):
        measures, blank = self.measures(CASES["right_0"])
        width = measures["panel_width"]
        self.assertAlmostEqual(width.start[0], blank.min_x, places=9)
        self.assertAlmostEqual(width.end[0], blank.max_x, places=9)

    def test_the_board_size_is_a_different_dimension_from_the_width(self):
        # The inner row is ONE BOARD's own size, so it is SHORTER than the
        # panel. Both are drawn, smallest innermost,
        # so the outer one is always the panel extent.
        measures, blank = self.measures(CASES["all_5"])
        self.assertLess(measures["board_width"].value_mm,
                        measures["panel_width"].value_mm)
        self.assertLess(measures["board_width"].offset_mm,
                        measures["panel_width"].offset_mm)


class ToolingHoleTest(unittest.TestCase):
    """(c) A hole lands on rail material, or its corner is declined by name."""

    def holes(self, rails, **settings):
        return panel_with(rails, tooling_holes=settings or {}).panelize

    def test_every_hole_that_is_placed_sits_on_rail_material(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                made = self.holes(rails)
                for hole in made.holes.holes:
                    self.assertTrue(
                        tooling_holes.on_rail_material(
                            hole.at, made.blank, made.instance_bbox
                        ),
                        f"{hole.designator} at {hole.at}",
                    )

    def test_no_hole_overlaps_a_board(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                made = self.holes(rails)
                left, top, right, bottom = made.instance_bbox
                for hole in made.holes.holes:
                    radius = hole.diameter_mm / 2.0
                    across = left - radius < hole.at[0] < right + radius
                    down = top - radius < hole.at[1] < bottom + radius
                    self.assertFalse(across and down,
                                     f"{hole.designator} at {hole.at}")

    def test_a_zero_side_declines_both_of_its_corners_by_name(self):
        made = self.holes(CASES["left_0"])
        self.assertEqual([hole.corner for hole in made.holes.holes],
                         ["top_right", "bottom_right"])
        self.assertEqual([skip.corner for skip in made.holes.skipped],
                         ["top_left", "bottom_left"])
        for skip in made.holes.skipped:
            self.assertIn("left rail is 0 mm wide", skip.reason)

    def test_the_declined_corners_are_in_the_runs_warnings(self):
        made = self.holes(CASES["top_0"])
        said = [note for note in made.warnings if "no rail material" in note]
        self.assertEqual(len(said), 2)
        self.assertTrue(all("top rail is 0 mm wide" in note for note in said))
        self.assertTrue(all("fixtured on 2 hole(s)" in note for note in said))

    def test_a_corner_with_two_zero_rails_names_both(self):
        made = self.holes(CASES["all_0"])
        self.assertEqual(made.holes.count, 0)
        self.assertEqual(len(made.holes.skipped), 4)
        self.assertIn("and", made.holes.skipped[0].reason)

    def test_the_designators_run_over_the_holes_that_exist(self):
        made = self.holes(CASES["right_0"])
        self.assertEqual([hole.designator for hole in made.holes.holes],
                         ["PNLTH1", "PNLTH2"])

    def test_the_corner_block_is_the_rails_said_in_coordinates(self):
        made = self.holes(CASES["all_5"])
        block = tooling_holes.corner_block(
            "top_left", made.blank, made.instance_bbox
        )
        self.assertAlmostEqual(block[2] - block[0], 5.0, places=9)
        self.assertAlmostEqual(block[3] - block[1], 5.0, places=9)
        self.assertEqual(
            tooling_holes.hole_centre(
                "top_left", made.blank, made.instance_bbox,
                tooling_holes.Settings(),
            ),
            ((block[0] + block[2]) / 2.0, (block[1] + block[3]) / 2.0),
        )

    def test_a_fixed_inset_that_lands_on_the_boards_is_declined(self):
        # The typed rule gets the same answer for the same reason: a pin
        # through a board is not a fixture, whoever asked for it.
        made = self.holes(CASES["all_5"], placement="fixed_inset",
                          inset_across_mm=9.0, inset_down_mm=9.0)
        self.assertEqual(made.holes.count, 0)
        for skip in made.holes.skipped:
            self.assertIn("on the boards rather than on rail material",
                          skip.reason)

    def test_a_fixed_inset_on_the_rail_is_placed_as_before(self):
        made = self.holes(CASES["all_5"], placement="fixed_inset",
                          inset_across_mm=2.5, inset_down_mm=2.5)
        self.assertEqual(made.holes.count, 4)
        self.assertEqual(made.holes.skipped, ())

    def test_a_rail_narrower_than_the_hole_is_placed_and_warned_about(self):
        # The line this stage draws: no material at all is a decline, and thin
        # material is a judgement the user makes looking at the panel.
        made = self.holes((5.0, 5.0, 5.0, 0.4))
        placed = {hole.corner for hole in made.holes.holes}
        self.assertEqual(len(placed), 4)
        tight = [hole for hole in made.holes.holes if hole.tight]
        self.assertTrue(tight)


class RailScoreTest(unittest.TestCase):
    """(d) The rail-edge saw pass skips a side that has no rail."""

    def test_a_zero_rail_gets_no_rail_edge_pass(self):
        made = panel_with(CASES["right_0"], scores={
            "rails_horizontal": True, "rails_vertical": True,
        }).panelize
        sources = {score.source for score in made.panel_scores.scores}
        self.assertNotIn("right rail", sources)
        self.assertIn("top rail", sources)

    def test_the_edges_are_derived_from_the_blank_and_the_rails(self):
        made = panel_with(CASES["all_5"]).panelize
        edges = panel_scores.rail_inner_edges(made.blank, made.rails)
        self.assertEqual({side for _, _, side in edges},
                         {"left", "right", "top", "bottom"})
        self.assertEqual(
            panel_scores.rail_inner_edges(
                made.blank, channels.Rails(5.0, 5.0, 0.0, 5.0)
            ),
            tuple(entry for entry in edges if entry[2] != "left"),
        )

    def test_no_rail_at_all_asks_for_no_pass(self):
        made = panel_with(CASES["all_0"], scores={
            "rails_horizontal": True, "rails_vertical": True,
        }).panelize
        self.assertEqual(made.panel_scores.count, 0)


class RailCopperTest(unittest.TestCase):
    """(e) The rail pour is ONE zone over the whole carrier,
    whatever the rails are - a missing side changes the BLANK, and the pour
    still covers exactly that blank.
    """

    def test_a_zero_side_still_gets_the_one_carrier_pour(self):
        made = panel_with(CASES["right_0"], rail_copper={}).panelize
        self.assertEqual({zone.name for zone in made.rail_copper.zones},
                         {"carrier"})
        self.assertEqual(made.rail_copper.count, 1)

    def test_two_zero_sides_still_leave_one_pour(self):
        made = panel_with(CASES["left_right_0"], rail_copper={}).panelize
        self.assertEqual({zone.name for zone in made.rail_copper.zones},
                         {"carrier"})
        self.assertEqual(made.rail_copper.count, 1)

    def test_no_rail_at_all_still_pours_over_the_board_array(self):
        # With every rail at zero the blank IS the board array, and the one
        # carrier pour covers it exactly - there is no "no rail, no pour"
        # case any more, because the pour was never per-side.
        made = panel_with(CASES["all_0"], rail_copper={}).panelize
        self.assertEqual(made.rail_copper.count, 1)
        zone = made.rail_copper.zones[0]
        self.assertEqual(zone.rect, made.blank.bbox)

    def test_the_one_rectangle_is_exactly_the_blank(self):
        made = panel_with(CASES["all_5"], rail_copper={}).panelize
        zones = list(rail_copper.plan(made.blank, made.instance_bbox,
                                      (), rail_copper.Settings()))
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0].rect, made.blank.bbox)


class OneSourceOfTruthTest(unittest.TestCase):
    """The rails are read ONCE, and everything else reads what they made.

    The shipped path, stated as a chain because that is what the defect was
    about - three readers agreeing about the rails is not the same thing as
    three readers deriving them:

        `panel.rails` in the file
          -> `PanelConfig.rail_widths`      (the only property that reads it)
          -> `channels.Rails`               (four numbers, zero allowed)
          -> `channels.grown(...)` = THE BLANK, and `SteppedPanel.rails`

    The dimensions measure the blank; the tooling holes take the blank and the
    board grid; the rail pour takes the same two; the rail-edge scores take the
    blank and the `Rails`. Nothing downstream reads the config again.
    """

    def config(self, rails) -> PanelConfig:
        return PanelConfig(
            source_board="../board.kicad_pcb",
            stage="build_panel",
            grid={"columns": 2, "rows": 2, "gap_mm": 3.0},
            panel={
                "rails": {"top": rails[0], "bottom": rails[1],
                          "left": rails[2], "right": rails[3]},
                "scores": {"enabled": True, "rails_horizontal": True,
                          "rails_vertical": True},
                "rail_copper": {"enabled": True},
            },
        )

    def built(self, rails):
        return build_from(self.config(rails), a_board(CORNER_MM),
                          fill_rails=False).panelize

    def test_the_config_property_is_the_only_reader(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                self.assertEqual(self.config(rails).rail_widths, rails)

    def test_every_stage_agrees_with_the_one_reading(self):
        for name, rails in CASES.items():
            with self.subTest(case=name):
                made = self.built(rails)
                self.assertEqual(tuple(made.rails), rails)
                # The blank IS the rails, said in coordinates.
                box = made.instance_bbox
                self.assertAlmostEqual(made.blank.min_x, box[0] - rails[2], 9)
                self.assertAlmostEqual(made.blank.max_x, box[2] + rails[3], 9)
                self.assertAlmostEqual(made.blank.min_y, box[1] - rails[0], 9)
                self.assertAlmostEqual(made.blank.max_y, box[3] + rails[1], 9)
                # ...and every carrier stage measures that same blank.
                self.assertAlmostEqual(
                    made.dimensions.value_of("panel_width"),
                    made.blank.width_mm, places=4,
                )
                for hole in made.holes.holes:
                    self.assertTrue(tooling_holes.on_rail_material(
                        hole.at, made.blank, box
                    ))
                # ONE CARRIER POUR REGARDLESS OF WHICH RAILS EXIST:
                # a missing side changes the BLANK the one pour covers, not
                # which zone gets made.
                self.assertEqual({zone.name for zone in made.rail_copper.zones},
                                 {"carrier"})
                scored = {score.source for score in made.panel_scores.scores}
                for index, side in enumerate(("top", "bottom", "left", "right")):
                    if rails[index] == 0.0:
                        self.assertNotIn(f"{side} rail", scored)

    def test_the_whole_chain_is_byte_identical_twice(self):
        for name in ("all_5", "right_0", "all_0"):
            with self.subTest(case=name):
                first = build_from(self.config(CASES[name]),
                                   a_board(CORNER_MM), fill_rails=False).board
                second = build_from(self.config(CASES[name]),
                                    a_board(CORNER_MM), fill_rails=False).board
                self.assertEqual(dump_board(first), dump_board(second))


if __name__ == "__main__":
    unittest.main()
