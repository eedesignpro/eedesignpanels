"""Placement is pure geometry, so it is checked by plain values."""

import unittest

from eedesignpanels.placement import Placement, grid_extent, grid_placements


class GridPlacementsTest(unittest.TestCase):
    def test_row_major_order_and_offsets(self):
        placements = grid_placements(2, 3, 10.0, 20.0)
        self.assertEqual(len(placements), 6)
        self.assertEqual(
            [(p.column, p.row) for p in placements],
            [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2)],
        )
        self.assertEqual(
            [(p.dx_mm, p.dy_mm) for p in placements],
            [(0.0, 0.0), (10.0, 0.0),
             (0.0, 20.0), (10.0, 20.0),
             (0.0, 40.0), (10.0, 40.0)],
        )

    def test_single_instance_sits_at_the_origin(self):
        self.assertEqual(
            grid_placements(1, 1, 5.0, 5.0),
            [Placement(0, 0, 0.0, 0.0, 0.0, False)],
        )

    def test_iteration_1_places_everything_upright(self):
        for placement in grid_placements(3, 3, 1.0, 1.0):
            self.assertEqual(placement.rotation_deg, 0.0)
            self.assertFalse(placement.mirror)

    def test_empty_grid_is_rejected(self):
        with self.assertRaises(ValueError):
            grid_placements(0, 3, 1.0, 1.0)
        with self.assertRaises(ValueError):
            grid_placements(3, 0, 1.0, 1.0)

    def test_negative_step_is_rejected(self):
        with self.assertRaises(ValueError):
            grid_placements(2, 2, -1.0, 1.0)


class GridExtentTest(unittest.TestCase):
    def test_step_is_the_instance_plus_the_gap(self):
        extent = grid_extent(15.5, 10.5, columns=2, rows=3, gap_mm=3.0)
        self.assertAlmostEqual(extent.step_x_mm, 18.5)
        self.assertAlmostEqual(extent.step_y_mm, 13.5)

    def test_panel_is_one_instance_plus_a_step_per_extra_cell(self):
        extent = grid_extent(15.5, 10.5, columns=2, rows=3, gap_mm=3.0)
        self.assertAlmostEqual(extent.width_mm, 34.0)
        self.assertAlmostEqual(extent.height_mm, 37.5)

    def test_one_by_one_is_the_instance_itself(self):
        extent = grid_extent(15.5, 10.5, columns=1, rows=1, gap_mm=3.0)
        self.assertAlmostEqual(extent.width_mm, 15.5)
        self.assertAlmostEqual(extent.height_mm, 10.5)

    def test_zero_gap_abuts_the_instances(self):
        extent = grid_extent(10.0, 10.0, columns=3, rows=1, gap_mm=0.0)
        self.assertAlmostEqual(extent.step_x_mm, 10.0)
        self.assertAlmostEqual(extent.width_mm, 30.0)

    def test_the_extent_matches_the_placements_it_describes(self):
        extent = grid_extent(15.5, 10.5, columns=2, rows=3, gap_mm=3.0)
        placements = grid_placements(2, 3, extent.step_x_mm, extent.step_y_mm)
        last = placements[-1]
        self.assertAlmostEqual(last.dx_mm + 15.5, extent.width_mm)
        self.assertAlmostEqual(last.dy_mm + 10.5, extent.height_mm)

    def test_bad_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            grid_extent(10.0, 10.0, columns=0, rows=1, gap_mm=0.0)
        with self.assertRaises(ValueError):
            grid_extent(-1.0, 10.0, columns=1, rows=1, gap_mm=0.0)
        with self.assertRaises(ValueError):
            grid_extent(10.0, 10.0, columns=1, rows=1, gap_mm=-0.1)


if __name__ == "__main__":
    unittest.main()
