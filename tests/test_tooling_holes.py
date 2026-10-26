"""Tooling holes: where they go, what they are called, and what they warn about.

Pure geometry plus one footprint, on a board assembled in memory. No file, no
KiCad. What is pinned here is what a fabricator would notice if it were wrong:
the position rule, the clockwise designator order, the pad that reaches the
drill file, and the advisory that says a hole is about to break into an edge.
"""

import unittest

from eedesigncore import Board, PadShape, PadType

from eedesignpanels.channels import Rect
from eedesignpanels.stages import tooling_holes
from eedesignpanels.stages.tooling_holes import (
    CLEARANCE_WARN_MM,
    CORNERS,
    PAD_LAYERS,
    Settings,
    apply,
    clearance_of,
    footprint_for,
    hole_centre,
    plan,
)
from eedesignpanels.toolpath import Line

# A 44 x 47.5 mm blank round a 34 x 37.5 mm grid: the corpus panel's numbers,
# with the origin moved to zero so the arithmetic is readable.
BLANK = Rect(min_x=0.0, min_y=0.0, max_x=44.0, max_y=47.5)
GRID = (5.0, 5.0, 39.0, 42.5)


def blank_edges(blank=BLANK):
    """The blank's four sides as offsettable primitives, for the clearance."""
    corners = [
        (blank.min_x, blank.min_y), (blank.max_x, blank.min_y),
        (blank.max_x, blank.max_y), (blank.min_x, blank.max_y),
    ]
    return [
        Line(p0=corners[index], p1=corners[(index + 1) % 4])
        for index in range(4)
    ]


class SettingsTest(unittest.TestCase):
    """The settings take a mapping, a config object or themselves."""

    def test_defaults_are_the_stated_ones(self):
        settings = Settings()
        self.assertEqual(settings.diameter_mm, 3.175)
        self.assertEqual(settings.placement, "rail_corner_center")
        self.assertEqual(settings.inset_across_mm, 5.0)
        self.assertEqual(settings.inset_down_mm, 5.0)
        self.assertEqual(settings.enabled_corners, CORNERS)

    def test_a_mapping_reads_as_settings(self):
        settings = Settings.of({
            "diameter_mm": 2.0,
            "corners": {"top_right": False},
            "placement": "fixed_inset",
            "inset_across_mm": 6.0,
        })
        self.assertEqual(settings.diameter_mm, 2.0)
        self.assertEqual(settings.placement, "fixed_inset")
        self.assertEqual(settings.inset_across_mm, 6.0)
        self.assertEqual(
            settings.enabled_corners, ("top_left", "bottom_right", "bottom_left")
        )

    def test_settings_pass_through_themselves(self):
        settings = Settings(diameter_mm=1.0)
        self.assertIs(Settings.of(settings), settings)

    def test_the_wrong_number_of_corner_flags_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            Settings.of({"corners": [True, True]})
        self.assertIn("4 booleans", str(caught.exception).replace("  ", " "))


class PlacementTest(unittest.TestCase):
    """Where a hole sits under each of the two rules."""

    def test_rail_corner_centre_is_the_middle_of_the_corner_block(self):
        self.assertEqual(
            hole_centre("top_left", BLANK, GRID, Settings()), (2.5, 2.5)
        )
        self.assertEqual(
            hole_centre("bottom_right", BLANK, GRID, Settings()), (41.5, 45.0)
        )

    def test_rail_corner_centre_follows_an_uneven_rail(self):
        blank = Rect(min_x=-8.0, min_y=-2.0, max_x=42.0, max_y=45.0)
        grid = (0.0, 0.0, 34.0, 37.5)
        self.assertEqual(
            hole_centre("top_left", blank, grid, Settings()), (-4.0, -1.0)
        )

    def test_fixed_inset_measures_from_the_blank_corner(self):
        settings = Settings(placement="fixed_inset",
                            inset_across_mm=4.0, inset_down_mm=3.0)
        self.assertEqual(hole_centre("top_left", BLANK, GRID, settings), (4.0, 3.0))
        self.assertEqual(
            hole_centre("bottom_right", BLANK, GRID, settings), (40.0, 44.5)
        )

    def test_an_unknown_placement_rule_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            hole_centre("top_left", BLANK, GRID, Settings(placement="middle"))
        self.assertIn("'middle'", str(caught.exception))


class DesignatorTest(unittest.TestCase):
    """Top-left first, then clockwise, over the corners that are switched on."""

    def test_four_holes_are_numbered_clockwise_from_the_top_left(self):
        made = plan(BLANK, GRID, Settings())
        self.assertEqual(
            [hole.designator for hole in made.holes],
            ["PNLTH1", "PNLTH2", "PNLTH3", "PNLTH4"],
        )
        self.assertEqual([hole.corner for hole in made.holes], list(CORNERS))

    def test_switching_a_corner_off_closes_the_numbering_up(self):
        made = plan(BLANK, GRID,
                    Settings(corners=(True, False, True, True)))
        self.assertEqual(
            [(hole.designator, hole.corner) for hole in made.holes],
            [("PNLTH1", "top_left"), ("PNLTH2", "bottom_right"),
             ("PNLTH3", "bottom_left")],
        )

    def test_no_corner_at_all_makes_no_hole(self):
        made = plan(BLANK, GRID, Settings(corners=(False,) * 4))
        self.assertEqual(made.count, 0)


class ClearanceTest(unittest.TestCase):
    """The advisory: how close a hole came to an edge, and what it says."""

    def test_clearance_is_measured_from_the_hole_edge(self):
        # Centre 2.5 mm from the blank's left edge, hole radius 1.5875.
        self.assertAlmostEqual(
            clearance_of((2.5, 20.0), 1.5875, blank_edges()), 0.9125, places=6
        )

    def test_a_hole_over_an_edge_reports_a_negative_clearance(self):
        self.assertAlmostEqual(
            clearance_of((1.0, 20.0), 1.5875, blank_edges()), -0.5875, places=6
        )

    def test_no_outline_means_no_measurement_rather_than_a_wrong_one(self):
        self.assertIsNone(clearance_of((2.5, 20.0), 1.5875, ()))
        made = plan(BLANK, GRID, Settings())
        self.assertIsNone(made.holes[0].clearance_mm)
        self.assertEqual(made.tight, ())

    def test_a_tight_hole_warns_by_designator_and_is_still_placed(self):
        board = Board()
        warnings = []
        settings = Settings(placement="fixed_inset",
                            inset_across_mm=1.8, inset_down_mm=1.8)
        made = apply(board, BLANK, GRID, settings,
                     outline=blank_edges(), warnings=warnings)
        self.assertEqual(made.count, 4)
        self.assertEqual(len(board.footprints), 4)
        self.assertEqual(len(made.tight), 4)
        self.assertIn("PNLTH1", warnings[0])
        self.assertIn("0.2125 mm", warnings[0])

    def test_a_comfortable_hole_says_nothing(self):
        board = Board()
        warnings = []
        apply(board, BLANK, GRID, Settings(),
              outline=blank_edges(), warnings=warnings)
        self.assertEqual(warnings, [])

    def test_the_advisory_threshold_is_the_adopted_half_millimetre(self):
        self.assertEqual(CLEARANCE_WARN_MM, 0.5)


class OffThePanelTest(unittest.TestCase):
    """A hole centred off the blank is an error, and it says where."""

    def test_a_centre_off_the_blank_raises_with_the_coordinates(self):
        settings = Settings(placement="fixed_inset", inset_across_mm=60.0)
        with self.assertRaises(ValueError) as caught:
            plan(BLANK, GRID, settings)
        message = str(caught.exception)
        self.assertIn("top left", message)
        self.assertIn("60.0000", message)

    def test_nothing_is_written_when_a_hole_is_off_the_panel(self):
        board = Board()
        settings = Settings(placement="fixed_inset", inset_down_mm=90.0)
        with self.assertRaises(ValueError):
            apply(board, BLANK, GRID, settings)
        self.assertEqual(board.footprints, [])


class FootprintTest(unittest.TestCase):
    """The footprint is a hole that reaches the drill file and nothing else."""

    def one(self):
        return footprint_for(plan(BLANK, GRID, Settings()).holes[0])

    def test_the_pad_is_a_circular_npth_with_no_annular_ring(self):
        pad = self.one().pads[0]
        self.assertEqual(pad.pad_type, PadType.np_thru_hole)
        self.assertEqual(pad.shape, PadShape.circle)
        self.assertEqual(pad.number, "")
        self.assertEqual(pad.size, (3.175, 3.175))
        self.assertEqual(pad.drill.width, 3.175)
        self.assertEqual(pad.layers, list(PAD_LAYERS))
        self.assertEqual(pad.position, (0.0, 0.0))

    def test_the_footprint_sits_at_the_hole_and_carries_its_designator(self):
        footprint = self.one()
        self.assertEqual(footprint.at.position, (2.5, 2.5))
        reference = next(
            prop for prop in footprint.properties if prop.name == "Reference"
        )
        self.assertEqual(reference.value, "PNLTH1")
        self.assertFalse(reference.visible)
        self.assertEqual(reference.layer, "F.Fab")

    def test_the_footprint_is_kept_out_of_the_assembly_outputs(self):
        attributes = self.one().attributes
        self.assertTrue(attributes.exclude_from_bom)
        self.assertTrue(attributes.exclude_from_pos_files)
        self.assertTrue(attributes.board_only)

    def test_the_diameter_reaches_the_footprint_name(self):
        hole = plan(BLANK, GRID, Settings(diameter_mm=2.0)).holes[0]
        self.assertEqual(
            footprint_for(hole).fp_id, "eedesignpanels:PanelToolingHole_2mm"
        )


class IdentityTest(unittest.TestCase):
    """Deterministic uuids, derived from the hole and from nothing else."""

    def test_the_same_hole_gets_the_same_uuid_every_time(self):
        first = footprint_for(plan(BLANK, GRID, Settings()).holes[0])
        second = footprint_for(plan(BLANK, GRID, Settings()).holes[0])
        self.assertEqual(first.uuid, second.uuid)
        self.assertEqual(first.pads[0].id, second.pads[0].id)

    def test_moving_a_hole_gives_it_a_new_identity(self):
        here = footprint_for(plan(BLANK, GRID, Settings()).holes[0])
        there = footprint_for(plan(
            BLANK, GRID,
            Settings(placement="fixed_inset", inset_across_mm=4.0),
        ).holes[0])
        self.assertNotEqual(here.uuid, there.uuid)

    def test_no_two_holes_on_one_panel_share_an_identity(self):
        made = plan(BLANK, GRID, Settings())
        uuids = {footprint_for(hole).uuid for hole in made.holes}
        self.assertEqual(len(uuids), 4)

    def test_every_uuid_is_a_version_five_one(self):
        footprint = footprint_for(plan(BLANK, GRID, Settings()).holes[0])
        for value in (footprint.uuid, footprint.pads[0].id):
            self.assertEqual(value[14], "5")


class ModuleTest(unittest.TestCase):
    """The exported surface, so a rename cannot happen quietly."""

    def test_the_corner_order_is_clockwise_from_the_top_left(self):
        self.assertEqual(
            tooling_holes.CORNERS,
            ("top_left", "top_right", "bottom_right", "bottom_left"),
        )


if __name__ == "__main__":
    unittest.main()
