"""The panel drill/place origin follows the finished panel bounds."""
import tempfile
import unittest
from pathlib import Path

from eedesigncore import Board, Setup

from eedesignpanels.stages.step_and_repeat import build, finish
from test_step_and_repeat import square_board


class PanelDrillOriginTest(unittest.TestCase):
    def test_rails_and_array_size_set_the_bottom_left_without_changing_source(self):
        source = square_board()
        source.setup = Setup(aux_axis_origin=(123.0, 456.0), grid_origin=(7.0, 8.0))
        for rails in (0.0, 5.0, 12.0):
            for columns, rows in ((1, 1), (2, 3)):
                with self.subTest(rails=rails, columns=columns, rows=rows):
                    result = build(source, columns, rows, 3.0, rails=rails)
                    blank = result.panelize.blank
                    expected = (blank.min_x, blank.max_y)
                    self.assertEqual(result.board.setup.aux_axis_origin, expected)
                    self.assertEqual(result.board.setup.grid_origin, expected)
                    self.assertEqual(source.setup.aux_axis_origin, (123.0, 456.0))
                    self.assertEqual(source.setup.grid_origin, (7.0, 8.0))
                    repeated = finish(result.stepped)
                    self.assertEqual(repeated.board.setup.aux_axis_origin, expected)
                    self.assertEqual(repeated.board.setup.grid_origin, expected)
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / 'panel.kicad_pcb'
                        result.board.to_file(path)
                        self.assertEqual(Board.from_file(path).setup.aux_axis_origin, expected)
                        self.assertEqual(Board.from_file(path).setup.grid_origin, expected)

    def test_board_without_setup_or_carrier_gets_the_array_bottom_left(self):
        source = square_board()
        source.setup = None
        result = build(source, 2, 3, 3.0)
        self.assertEqual(result.board.setup.aux_axis_origin, (0.0, 36.0))
        self.assertEqual(result.board.setup.grid_origin, (0.0, 36.0))
        self.assertIsNone(source.setup)
