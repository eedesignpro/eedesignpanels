"""Independent rigid and flex routing with declared tab mouths."""

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from eedesignpanels import rigid_flex
from eedesignpanels.config import PrepConfig
from test_route_toolpath import contoured_board


class RigidFlexRoutingTest(unittest.TestCase):
    def test_rigid_region_uses_tab_mouth_and_matches_flex_routing(self):
        board = contoured_board()
        for drawings in (board.drawings,
                         *(footprint.drawings for footprint in board.footprints)):
            drawings.extend(item.model_copy(update={"layer": "User.24"}, deep=True)
                            for item in list(drawings)
                            if getattr(item, "layer", None) == "Edge.Cuts")
        config = SimpleNamespace(
            prep=PrepConfig(cutter_diameter=0.8), min_tab_mm=0.5,
            outline_heal_mm=0.01, pocket_cutouts=False,
        )
        result = rigid_flex.build(board, config)
        self.assertEqual(len(result.rigid_results), 1)
        rigid = result.rigid_results[0]
        self.assertTrue(rigid.tabs)
        self.assertTrue(rigid.tabs[0].mouth)
        self.assertEqual(
            [item.model_dump(exclude={"id", "layer"}) for item in rigid.items],
            [item.model_dump(exclude={"id", "layer"}) for item in result.items],
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "repeat.kicad_pcb"
            board.to_file(path)
            first = path.read_bytes()
            rigid_flex.build(board, config)
            board.to_file(path)
            self.assertEqual(path.read_bytes(), first)
