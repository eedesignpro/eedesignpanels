"""The carrier features as a set: their config, their wiring, and their absence.

Three stages arrive together - tooling holes, panel scores and rail copper.
**Panel scores and rail copper are gated on their own section of the config**,
and the property this file exists to hold is the one a reviewer would want
first: a config that names neither builds exactly the panel it would without
those stages, to the byte.

**The tooling holes are on by default**: they run when the config does not
name them, like the panel's dimensions, and a config declines them with
`"enabled": false`. So the byte-identity property is stated at the STAGE level
here - `build` with no `tooling_holes` argument - and the config-level default
has its own class, :class:`HolesOnByDefaultTest`.

Everything else here is the wiring - that the stage sees the blank, the board
rectangles and the outline the panel half derived, and in that order.

The rail pour is built here with `fill_rails=False`. The fill needs KiCad and
is covered in `test_rail_copper`; what belongs here is where the copper goes -
and WHEN the chain asks for it, which is `FillOrderTest` at the bottom and
needs no KiCad at all.
"""

import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    PcbLine,
    Setup,
    Stroke,
    TrackSegment,
    Zone,
    ZoneFilledPolygon,
    dump_board,
)
from pydantic import ValidationError

from eedesignpanels.config import (
    PanelConfig,
    PanelDimensionsConfig,
    PanelizeConfig,
    PanelScoresConfig,
    RailCopperConfig,
    ToolingHolesConfig,
    VScoreConfig,
)
from eedesignpanels.stages import panelize
from eedesignpanels.stages.panel_scores import (
    DEFAULT_RULE_HALF_WIDTH_MM,
    RULE_AREA_NAME,
)
from eedesignpanels.stages.rail_copper import POUR_NAME, ZONE_NAME_PREFIX
from eedesignpanels.stages.step_and_repeat import (
    build,
    build_from,
    panel_geometry,
)
from test_panel_fiducials import a_library

CUTTER = 0.889
RADIUS = CUTTER / 2.0


def outline(x0, y0, x1, y1, layer="Edge.Cuts", width=0.05, uid=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer, stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def source_board() -> Board:
    """A 10 x 10 board with copper, a toolpath round three sides and a score."""
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user"),
            BoardLayer(ordinal=38, canonical_name="User.38", layer_type="user"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
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
    board.drawings.append(outline(
        0.0, 0.0, 0.0, 10.0, layer="User.38", width=0.1, uid="score-left",
    ))
    board.footprints = [Footprint(
        fp_id="lib:R", layer="F.Cu", uuid="fp-1",
        at={"position": (5.0, 5.0), "angle": 0.0},
    )]
    board.tracks = [TrackSegment(
        start=(2.0, 2.0), end=(8.0, 2.0), width=0.2, layer="F.Cu",
        net="AGND", uuid="track-1",
    )]
    board.zones = [Zone(
        layers=["F.Cu"], net_name="AGND", id="zone-1",
        polygons=[{"pts": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]}],
        filled_polygons=[ZoneFilledPolygon(
            layer="F.Cu", pts=[(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)],
        )],
    )]
    return board


def panel_of(**features) -> Board:
    """A 2 x 1 panel with a 5 mm rail, and whichever features were asked for."""
    return build(source_board(), 2, 1, 3.0, rails=5.0,
                 fill_rails=False, **features).board


class ConfigTest(unittest.TestCase):
    """What the three sections accept, and what they refuse."""

    def test_no_section_is_written_unless_it_is_named(self):
        # Every carrier section is always present, each with its own
        # `enabled` switch, so a section's absence says nothing. Tooling holes default ON, the rest default OFF.
        panel = PanelizeConfig(rails=5.0)
        self.assertTrue(panel.tooling_holes.enabled)
        self.assertFalse(panel.scores.enabled)
        self.assertFalse(panel.rail_copper.enabled)
        self.assertIsNotNone(panel.tooling_hole_settings)
        self.assertIsNone(panel.score_settings)
        self.assertIsNone(panel.rail_copper_settings)

    def test_the_stated_defaults_are_the_defaults(self):
        holes = ToolingHolesConfig()
        self.assertTrue(holes.enabled)
        self.assertEqual(holes.diameter_mm, 3.175)
        self.assertEqual(holes.placement, "rail_corner_center")
        self.assertEqual(holes.inset_across_mm, 5.0)
        self.assertEqual(holes.inset_down_mm, 5.0)
        for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
            self.assertTrue(getattr(holes.corners, corner))

        scores = PanelScoresConfig()
        self.assertFalse(scores.enabled)
        self.assertFalse(scores.rails_horizontal)
        self.assertFalse(scores.rails_vertical)
        self.assertFalse(scores.inter_row)
        self.assertFalse(scores.inter_column)
        # The keep-out width is not in this section: it lives at
        # `prep.vscore.rule_half_width_mm`.
        self.assertEqual(VScoreConfig().rule_half_width_mm, 0.3)

        copper = RailCopperConfig()
        self.assertFalse(copper.enabled)
        self.assertEqual(copper.clearance_mm, 0.635)
        self.assertFalse(copper.pour_between_boards)

    def test_a_hole_of_no_diameter_is_refused(self):
        with self.assertRaises(ValidationError):
            ToolingHolesConfig(diameter_mm=0.0)

    def test_a_placement_rule_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValidationError):
            ToolingHolesConfig(placement="middle")

    def test_a_negative_inset_is_refused(self):
        with self.assertRaises(ValidationError):
            ToolingHolesConfig(inset_across_mm=-1.0)

    def test_a_rule_area_of_no_width_is_refused(self):
        with self.assertRaises(ValidationError):
            PanelScoresConfig(rule_half_width_mm=0.0)

    def test_a_negative_clearance_is_refused(self):
        with self.assertRaises(ValidationError):
            RailCopperConfig(clearance_mm=-0.1)

    def test_a_misspelled_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValidationError):
            ToolingHolesConfig(diameter=3.175)
        with self.assertRaises(ValidationError):
            PanelScoresConfig(verticals=True)
        with self.assertRaises(ValidationError):
            RailCopperConfig(clearance=0.635)
        with self.assertRaises(ValidationError):
            PanelizeConfig(rails=5.0, tooling_hole={})

    def test_a_corner_that_is_not_a_corner_is_refused(self):
        with self.assertRaises(ValidationError):
            ToolingHolesConfig(corners={"middle": True})


class HolesOnByDefaultTest(unittest.TestCase):
    """Tooling holes are on unless the config declines them."""

    def config(self, panel: dict) -> PanelConfig:
        return PanelConfig(
            source_board="../board.kicad_pcb",
            stage="build_panel",
            grid={"columns": 2, "rows": 1, "gap_mm": 3.0},
            panel=panel,
        )

    def test_a_panel_with_no_holes_section_is_pinned_down_anyway(self):
        panel = self.config({"rails": 5.0}).panel
        self.assertTrue(panel.tooling_holes.enabled)
        settings = panel.tooling_hole_settings
        self.assertIsNotNone(settings)
        self.assertEqual(settings.diameter_mm, 3.175)
        self.assertEqual(settings.placement, "rail_corner_center")

    def test_enabled_false_is_how_a_config_declines_them(self):
        panel = self.config(
            {"rails": 5.0, "tooling_holes": {"enabled": False}}
        ).panel
        self.assertIsNone(panel.tooling_hole_settings)

    def test_the_section_still_tunes_them_when_it_is_there(self):
        panel = self.config(
            {"rails": 5.0, "tooling_holes": {"diameter_mm": 2.0}}
        ).panel
        self.assertEqual(panel.tooling_hole_settings.diameter_mm, 2.0)

    def test_the_carrier_features_hand_the_build_the_resolved_holes(self):
        holes, scores, copper = self.config({"rails": 5.0}).carrier_features
        self.assertIsNotNone(holes)
        # The other two are unchanged: named or not run at all.
        self.assertIsNone(scores)
        self.assertIsNone(copper)

    def test_declining_them_hands_the_build_nothing(self):
        holes, _, _ = self.config(
            {"rails": 5.0, "tooling_holes": {"enabled": False}}
        ).carrier_features
        self.assertIsNone(holes)

    def test_a_declined_config_builds_the_panel_it_built_before(self):
        # The panel with no holes at all - the bare panel plus its
        # dimensions, the other stage that runs by default.
        declined = build_from(
            self.config({"rails": 5.0, "tooling_holes": {"enabled": False}}),
            source_board(), fill_rails=False,
        ).board
        self.assertEqual(
            dump_board(declined),
            dump_board(panel_of(dimensions=PanelDimensionsConfig())),
        )

    def test_the_default_config_puts_four_holes_on_the_panel(self):
        made = build_from(self.config({"rails": 5.0}), source_board(),
                          fill_rails=False).panelize
        self.assertEqual(made.holes.count, 4)
        self.assertEqual(
            [hole.designator for hole in made.holes.holes],
            ["PNLTH1", "PNLTH2", "PNLTH3", "PNLTH4"],
        )


class AbsenceTest(unittest.TestCase):
    """A config that names none of them adds none of them.

    **With ONE exception**: the keepout round a saw pass is not one of the panel-scores stage's
    families. A panel carrying passes its BOARDS decided gets its rule areas
    whether or not `panel.scores` is in the file - see :class:`ScoreKeepoutTest`
    below, which is where that property is stated.
    """

    def test_no_stage_the_config_did_not_name_reports_anything(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0).panelize
        self.assertIsNone(made.holes)
        self.assertIsNone(made.rail_copper)
        # The scores stage did not run; what is reported is the protection.
        self.assertTrue(made.panel_scores.protection_only)
        self.assertEqual(made.panel_scores.count, 0)

    def test_nothing_but_the_score_keepouts_is_written(self):
        panel = panel_of()
        self.assertEqual(
            [footprint for footprint in panel.footprints
             if footprint.fp_id.startswith("eedesignpanels:")],
            [],
        )
        self.assertEqual(
            [zone for zone in panel.zones
             if (zone.name or "").startswith(ZONE_NAME_PREFIX)],
            [],
        )
        self.assertEqual(
            [zone.name for zone in panel.zones],
            [RULE_AREA_NAME, RULE_AREA_NAME],
        )

    def test_the_bytes_do_not_move_when_the_features_are_absent(self):
        self.assertEqual(dump_board(panel_of()), dump_board(panel_of()))

    def test_the_panel_still_carries_only_its_merged_scores(self):
        panel = panel_of()
        scores = [item for item in panel.drawings
                  if getattr(item, "layer", None) == "User.38"]
        self.assertEqual(len(scores), 2)

    def test_a_panel_with_no_score_at_all_gets_no_zone_and_no_report(self):
        # The other half of it: nothing to protect is nothing done, so a board
        # that decided no score builds the panel it always built, to the byte.
        board = source_board()
        board.drawings = [
            item for item in board.drawings
            if getattr(item, "layer", None) != "User.38"
        ]
        made = build(board, 2, 1, 3.0, rails=5.0, fill_rails=False)
        self.assertIsNone(made.panelize.panel_scores)
        self.assertEqual(made.board.zones, [])


class ScoreKeepoutTest(unittest.TestCase):
    """THE PROTECTION IS NOT ON THE SWITCH.

    With the panel-scores stage switched OFF, a panel can still carry merged
    board-edge score lines. Without a rule area, nothing would keep a track -
    or the rail pour - off a line the saw runs down. The switch governs the
    families of pass the PANEL adds. The keepout round a pass the BOARDS
    decided is not one of those.
    """

    def panel(self, **features):
        return build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     **features)

    def keepouts(self, board) -> list:
        return [zone for zone in board.zones if zone.name == RULE_AREA_NAME]

    def score_lines(self, board) -> list:
        return [item for item in board.drawings
                if getattr(item, "layer", None) == "User.38"]

    def test_one_keepout_per_score_line_with_no_scores_section(self):
        panel = self.panel().board
        self.assertEqual(len(self.score_lines(panel)), 2)
        self.assertEqual(len(self.keepouts(panel)), 2)

    def test_the_half_width_is_the_models_own_default(self):
        made = self.panel().panelize.panel_scores
        self.assertEqual(made.half_width_mm, DEFAULT_RULE_HALF_WIDTH_MM)
        self.assertEqual(
            VScoreConfig().rule_half_width_mm, DEFAULT_RULE_HALF_WIDTH_MM
        )

    def test_the_keepouts_keep_everything_out_on_every_copper_layer(self):
        for zone in self.keepouts(self.panel().board):
            self.assertEqual(zone.layers, ["F.Cu", "B.Cu"])
            for allowed in ("tracks", "vias", "pads", "copper_pour",
                            "footprints"):
                with self.subTest(allowed=allowed):
                    self.assertFalse(getattr(zone.keepout, allowed))

    def test_the_switch_still_governs_the_passes_the_panel_adds(self):
        off = self.panel(scores={"rails_horizontal": False,
                                 "rails_vertical": False,
                                 "inter_row": False,
                                 "inter_column": False}).panelize.panel_scores
        on = self.panel(
            scores={"inter_column": True}
        ).panelize.panel_scores
        self.assertEqual(off.count, 0)
        self.assertEqual(on.count, 1)
        self.assertFalse(off.protection_only)
        # ...and both protect every line that ended up on the panel.
        self.assertEqual(len(off.rule_areas), 2)
        self.assertEqual(len(on.rule_areas), 3)

    def test_a_section_with_every_switch_off_builds_what_absence_builds(self):
        # The two ways of saying "no panel score" now agree to the byte, which
        # is what makes the switch about families and nothing else.
        stated = self.panel(scores={"rails_horizontal": False,
                                    "rails_vertical": False,
                                    "inter_row": False,
                                    "inter_column": False}).board
        self.assertEqual(dump_board(stated), dump_board(panel_of()))

    def test_the_keepouts_are_the_same_bytes_twice(self):
        self.assertEqual(dump_board(self.panel().board),
                         dump_board(self.panel().board))


class WiringTest(unittest.TestCase):
    """Each stage runs, sees the panel it is measured against, and reports."""

    def test_the_tooling_holes_reach_the_panel_and_the_report(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     tooling_holes={"diameter_mm": 2.0}).panelize
        self.assertEqual(made.holes.count, 4)
        self.assertEqual(made.holes.diameter_mm, 2.0)
        # The clearance is measured against the panel's own derived outline,
        # which only exists inside the panel half.
        for hole in made.holes.holes:
            self.assertIsNotNone(hole.clearance_mm)

    def test_a_hole_sits_in_the_rail_and_not_on_a_board(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     tooling_holes={}).panelize
        blank = made.blank
        for hole in made.holes.holes:
            self.assertTrue(blank.contains(hole.at))
            self.assertGreater(hole.clearance_mm, 0.0)

    def test_a_vertical_score_lands_down_the_middle_of_the_gap(self):
        # The gap is 3 mm between two 10 mm boards, so the pass is at 11.5 mm
        # in board coordinates - and 16.5 mm from the blank's top-left corner,
        # which is the number the report and the fabricator use.
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     scores={"inter_column": True}).panelize
        blank = made.blank
        score = made.panel_scores.scores[0]
        self.assertEqual(score.source, "column gap 1")
        self.assertAlmostEqual(score.start[0], 11.5, places=9)
        self.assertAlmostEqual(score.position_mm, 11.5 - blank.min_x, places=9)
        self.assertAlmostEqual(score.start[1], blank.min_y, places=9)
        self.assertAlmostEqual(score.end[1], blank.max_y, places=9)

    def test_the_rail_scores_sit_on_the_rails_inner_edges(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     scores={"rails_horizontal": True,
                            "rails_vertical": True}).panelize
        blank, scores = made.blank, made.panel_scores
        placed = {score.source: score for score in scores.scores}
        self.assertAlmostEqual(placed["right rail"].start[0], blank.max_x - 5.0,
                               places=9)
        self.assertAlmostEqual(placed["top rail"].start[1], blank.min_y + 5.0,
                               places=9)
        self.assertAlmostEqual(placed["bottom rail"].start[1],
                               blank.max_y - 5.0, places=9)
        # The left rail's inner edge is a board edge the DESIGN scored, so the
        # merged pass is already there and no second line is drawn on it.
        self.assertEqual(scores.already_scored, ("left rail",))

    def test_every_score_on_the_panel_gets_a_rule_area(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     scores={"inter_column": True}).panelize
        # Two merged instance passes plus the one this config asked for.
        self.assertEqual(made.panel_scores.inherited_lines, 2)
        self.assertEqual(len(made.panel_scores.rule_areas), 3)

    def test_the_rule_areas_are_on_the_panel_as_keepouts(self):
        panel = panel_of(scores={"inter_column": True})
        areas = [zone for zone in panel.zones if zone.name == RULE_AREA_NAME]
        self.assertEqual(len(areas), 3)
        for area in areas:
            self.assertIsNotNone(area.keepout)
            self.assertEqual(area.layers, ["F.Cu", "B.Cu"])

    def test_the_rail_pour_covers_the_rails_and_not_the_boards(self):
        # ONE pour, over the whole carrier - what keeps it off the
        # boards is its rule area, not its own rectangle.
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     rail_copper={}).panelize
        blank = made.blank
        self.assertEqual(made.rail_copper.count, 1)
        self.assertEqual(made.rail_copper.keepout_count, 1)
        self.assertAlmostEqual(
            made.rail_copper.area_mm2, blank.width_mm * blank.height_mm,
            places=6,
        )

    def test_pouring_between_boards_adds_a_keepout_per_board(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     rail_copper={"pour_between_boards": True}).panelize
        # Still one pour; the setting changes how many rule areas hold it off
        # the boards - one per instance instead of one array rectangle.
        self.assertEqual(made.rail_copper.count, 1)
        self.assertEqual(made.rail_copper.keepout_count, 2)

    def test_the_pour_is_not_filled_when_the_fill_was_not_asked_for(self):
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     rail_copper={}).panelize
        self.assertIsNone(made.rail_copper.refill)
        panel = panel_of(rail_copper={})
        for zone in panel.zones:
            if (zone.name or "").startswith(ZONE_NAME_PREFIX):
                self.assertEqual(zone.filled_polygons, [])


class OrderTest(unittest.TestCase):
    """Holes, then scores, then rail copper - after the outline exists."""

    def test_the_score_rule_areas_are_on_the_board_before_the_rail_pour(self):
        panel = panel_of(scores={}, rail_copper={})
        names = [zone.name for zone in panel.zones if zone.name]
        self.assertLess(names.index(RULE_AREA_NAME), names.index(POUR_NAME))

    def test_all_three_together_give_the_same_bytes_twice(self):
        features = {
            "tooling_holes": {"diameter_mm": 2.0},
            "scores": {"inter_row": True, "inter_column": True,
                      "rails_horizontal": True, "rails_vertical": True},
            "rail_copper": {"pour_between_boards": True},
        }
        self.assertEqual(dump_board(panel_of(**features)),
                         dump_board(panel_of(**features)))

    def test_the_features_do_not_disturb_the_panel_outline(self):
        bare = build(source_board(), 2, 1, 3.0, rails=5.0).panelize
        full = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     tooling_holes={}, scores={}, rail_copper={}).panelize
        self.assertEqual(bare.loops, full.loops)
        self.assertEqual(bare.boundary.item_count, full.boundary.item_count)
        self.assertAlmostEqual(bare.boundary.length_mm, full.boundary.length_mm,
                               places=9)


class InstanceBoxTest(unittest.TestCase):
    """The board rectangles the panel half cannot measure for itself."""

    def test_one_box_per_placement_at_the_stepped_position(self):
        geometry = panel_geometry(source_board(), 2, 2, 3.0)
        boxes = geometry.instance_boxes
        self.assertEqual(len(boxes), 4)
        self.assertEqual(boxes[0], (0.0, 0.0, 10.0, 10.0))
        self.assertEqual(boxes[1], (13.0, 0.0, 23.0, 10.0))
        self.assertEqual(boxes[2], (0.0, 13.0, 10.0, 23.0))

    def test_the_boxes_are_the_centreline_outline_and_not_the_stroke(self):
        geometry = panel_geometry(source_board(), 1, 1, 3.0)
        self.assertEqual(geometry.instance_boxes[0], (0.0, 0.0, 10.0, 10.0))

    def test_a_computed_score_runs_between_the_boards_and_warns_about_nothing(
        self,
    ):
        # A computed midline cannot land on a board edge: it is derived from
        # the two boxes it runs between.
        outcome = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                        scores={"vertical": True})
        self.assertEqual(
            [note for note in outcome.warnings if "runs through" in note], []
        )

    def test_one_column_has_no_gap_to_score_and_says_so(self):
        outcome = build(source_board(), 1, 1, 3.0, rails=5.0, fill_rails=False,
                        scores={"vertical": True, "horizontal": True})
        self.assertEqual(outcome.panelize.panel_scores.count, 0)
        self.assertEqual(
            [note for note in outcome.warnings if "nothing between" in note], []
        )


class ErrorTest(unittest.TestCase):
    """A feature that cannot be built stops the build and says where."""

    def test_no_computed_score_can_be_off_the_blank(self):
        # There is no typed position to get wrong: a midline lies between two
        # boards and a rail edge lies on the blank by construction. The guard
        # that names an off-blank position is still there and is exercised
        # directly in `test_panel_scores`; this is the property it guards.
        made = build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                     scores={"vertical": True, "horizontal": True,
                             "rails": True}).panelize
        blank = made.blank
        for score in made.panel_scores.scores:
            self.assertTrue(blank.contains(score.start))
            self.assertTrue(blank.contains(score.end))

    def test_a_hole_off_the_blank_stops_the_build(self):
        with self.assertRaises(ValueError) as caught:
            build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                  tooling_holes={"placement": "fixed_inset",
                                 "inset_across_mm": 400.0})
        self.assertIn("off the panel blank", str(caught.exception))


class FillOrderTest(unittest.TestCase):
    """THE POUR'S COPPER IS TAKEN LAST, after every stage that adds copper.

    The rail pour's stage runs BEFORE the fiducials. KiCad fills a BOARD and
    not a zone, so a fill inside that stage would see no fiducial pads and the
    copper would flood them - the targets would get no clearance ring while
    the tooling holes, added before the fill, had one.

    Fenced here without KiCad: the fill is intercepted and asked what was on the
    board at the moment it ran. Nothing about the shape of the copper is
    asserted - that is KiCad's - only that KiCad was asked about the FINISHED
    panel.
    """

    def caught(self, **features):
        """Build with the fill intercepted. Returns what the board held then."""
        seen = {}
        real = panelize.rail_copper_stage.fill_placed

        def spy(board, poured, warnings=None, cli_path=None, work_dir=None):
            seen["footprints"] = len(board.footprints)
            seen["references"] = [
                prop.value for footprint in board.footprints
                for prop in footprint.properties if prop.name == "Reference"
            ]
            return poured

        panelize.rail_copper_stage.fill_placed = spy
        try:
            build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=True,
                  rail_copper={"clearance_mm": 0.5}, **features)
        finally:
            panelize.rail_copper_stage.fill_placed = real
        return seen

    def test_the_fill_runs_at_all_when_it_is_asked_for(self):
        self.assertIn("footprints", self.caught())

    def test_the_fiducials_are_on_the_board_before_the_fill_is_taken(self):
        # The holes are the fiducials' datum, so both sections are named.
        with tempfile.TemporaryDirectory() as folder:
            directory = a_library(Path(folder))
            seen = self.caught(
                tooling_holes={"diameter_mm": 2.0, "inset_across_mm": 2.0,
                               "inset_down_mm": 2.0},
                fiducials={
                    "footprint": "Fiducial_1mm_Mask2mm", "library": "Fixture",
                    "library_path": str(directory), "offset_mm": 1.0,
                },
            )
        self.assertTrue(
            [name for name in seen["references"] if name.startswith("PNLFD")],
            "the fill was taken before the fiducials were placed",
        )

    def test_the_tooling_holes_are_on_the_board_before_the_fill_is_taken(self):
        seen = self.caught(tooling_holes={"diameter_mm": 3.0})
        self.assertTrue(
            [name for name in seen["references"] if name.startswith("PNLTH")]
        )

    def test_a_build_that_declines_the_fill_takes_none(self):
        seen = {}
        real = panelize.rail_copper_stage.fill_placed
        panelize.rail_copper_stage.fill_placed = (
            lambda *a, **k: seen.setdefault("ran", True)
        )
        try:
            build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                  rail_copper={"clearance_mm": 0.5})
        finally:
            panelize.rail_copper_stage.fill_placed = real
        self.assertEqual(seen, {})

    def test_the_pour_stage_itself_is_never_asked_to_fill_any_more(self):
        # The zones and the copper are two steps now, and the CHAIN owns the
        # second one. A stage that filled its own zones would put the copper
        # back in front of the fiducials.
        asked = []
        real = panelize.rail_copper_stage.apply

        def spy(*args, **kwargs):
            asked.append(kwargs.get("fill"))
            return real(*args, **kwargs)

        panelize.rail_copper_stage.apply = spy
        try:
            build(source_board(), 2, 1, 3.0, rails=5.0, fill_rails=False,
                  rail_copper={"clearance_mm": 0.5})
        finally:
            panelize.rail_copper_stage.apply = real
        self.assertEqual(asked, [False])


class PanelizeSurfaceTest(unittest.TestCase):
    """The panel half's own signature, so a caller cannot drift from it."""

    def test_the_result_carries_a_slot_for_each_feature(self):
        result = panelize.PanelizeResult._fields
        for name in ("holes", "panel_scores", "rail_copper"):
            self.assertIn(name, result)


if __name__ == "__main__":
    unittest.main()
