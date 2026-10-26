"""Native rail text and score-dependent edge plating release checks."""

import math
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eedesigncore import Board, PcbText, PtsArc, Zone, ZoneKeepout, ZoneOutlinePolygon

from eedesignpanels.channels import Channel, Rails, Rect
from eedesignpanels.config import PanelTextConfig
from eedesignpanels.stages import edge_plating, panel_text
from eedesignpanels.toolpath import Line, arc_from_three_points
from test_panel_page import a_session, write_panel_project


class PanelTextTest(unittest.TestCase):
    def test_malformed_project_values_do_not_reach_native_text(self):
        for invalid in ('x' * 257, 'PART\x00', 'PART\n', 'PART\t', 'PART\x7f'):
            with self.subTest(value=repr(invalid)), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'project.kicad_pro'
                path.touch()
                variables = {'PART_ID': invalid, 'ARRAY_TABULATION': '01', 'PCB_REVISION': '02'}
                project = SimpleNamespace(root=SimpleNamespace(get_object=lambda name: variables))
                with mock.patch.object(panel_text.ProjectFile, 'from_file', return_value=project):
                    content, warning = panel_text.read_sspn(path)
                self.assertEqual(content, '')
                self.assertTrue(warning)

    def base(self, rails=Rails(5, 5, 5, 5), channels=()):
        return SimpleNamespace(blank=Rect(0, 0, 60, 60), rails=rails,
                               cut_channels=channels)

    def test_native_text_round_trip_and_repeatable_identity(self):
        first, second = Board(), Board()
        for board in (first, second):
            items = panel_text.apply(board, self.base(), PanelTextConfig(), '100-200-3000-03-01')
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].angle, 90)
            self.assertEqual(items[0].layer, 'F.SilkS')
        self.assertEqual(first.drawings[0], second.drawings[0])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'panel.kicad_pcb'
            first.to_file(path)
            restored = Board.from_file(path)
            self.assertIsInstance(restored.drawings[0], PcbText)
            self.assertEqual(restored.drawings[0].content, '100-200-3000-03-01')

    def test_horizontal_fallback_and_no_rails_warning(self):
        board = Board()
        made = panel_text.apply(board, self.base(Rails(5, 5, 0, 0)),
                               PanelTextConfig(), 'ABC-01-02')
        self.assertEqual(made[0].angle, 0)
        warnings = []
        made = panel_text.apply(Board(), self.base(Rails(0, 0, 0, 0)),
                               PanelTextConfig(), 'ABC-01-02', warnings=warnings)
        self.assertFalse(made)
        self.assertTrue(warnings)

    def test_oversized_override_remains_visible(self):
        warnings = []
        made = panel_text.apply(Board(), self.base(),
                               PanelTextConfig(text_height_mm=12),
                               '100-200-3000-03-01', warnings=warnings)
        self.assertEqual(made[0].attributes.font_size, (12, 12))
        self.assertTrue(any('remains visible' in item for item in warnings))

    def test_cutter_width_reduces_automatic_text_height(self):
        settings = PanelTextConfig()
        original = panel_text.apply(Board(), self.base(), settings, 'ABC-01-02')[0]
        channel = Channel((Line((4, 0), (4, 60)),), 1, False)
        reduced = panel_text.apply(Board(), self.base(channels=(channel,)),
                                   settings, 'ABC-01-02')[0]
        self.assertLess(reduced.attributes.font_size[0], original.attributes.font_size[0])


class PlatingGeometryTest(unittest.TestCase):
    def test_outer_copper_keepouts_only(self):
        polygon = ZoneOutlinePolygon(pts=[(4, -1), (6, -1), (6, 1), (4, 1)])
        for layer, blocks in [('F.Cu', True), ('B.Cu', True), ('In1.Cu', False)]:
            with self.subTest(layer=layer):
                board = Board(zones=[Zone(layer=layer, polygons=[polygon],
                                          keepout=ZoneKeepout(copper_pour=False))])
                parts = list(edge_plating.outside_keepouts(
                    [Line((0, 0), (10, 0))], list(edge_plating.copper_keepouts(board))))
                self.assertAlmostEqual(sum(part.length for part in parts), 8 if blocks else 10)

    def test_arc_keepout_boundary_is_read_and_clipped(self):
        polygon = ZoneOutlinePolygon(pts=[
            PtsArc(start=(4, 0), mid=(5, -1), end=(6, 0)), (6, 2), (4, 2)])
        board = Board(zones=[Zone(layer='F.Cu', polygons=[polygon],
                                  keepout=ZoneKeepout(copper_pour=False))])
        parts = list(edge_plating.outside_keepouts(
            [Line((0, 0.5), (10, 0.5))], list(edge_plating.copper_keepouts(board))))
        self.assertAlmostEqual(sum(part.length for part in parts), 8)

    def test_retraction_consumes_short_end_without_gaps_at_internal_join(self):
        parts = list(edge_plating.retract_path_ends([
            Line((0, 0), (0.1, 0)), Line((0.1, 0), (1, 0)), Line((1, 0), (2, 0))], 0.2))
        self.assertAlmostEqual(parts[0].start[0], 0.2)
        self.assertAlmostEqual(parts[-1].end[0], 1.8)
        self.assertEqual(parts[0].end, parts[1].start)

    def test_closed_loop_is_not_retracted(self):
        loop = [Line((0, 0), (1, 0)), Line((1, 0), (1, 1)),
                Line((1, 1), (0, 1)), Line((0, 1), (0, 0))]
        self.assertEqual(list(edge_plating.retract_path_ends(loop, 0.2)), loop)

    def test_notch_rounding_does_not_create_open_path_ends(self):
        arc = arc_from_three_points((0, 0), (1, 1), (2, 0))
        path = [Line((0, -1), (0, -0.000001313)), arc,
                Line((2, -0.000001313), (2, -1))]
        parts = list(edge_plating.retract_path_ends(path, 0.2))
        self.assertAlmostEqual(parts[1].length, math.pi)


class PlatingSessionTest(unittest.TestCase):
    def test_manual_actions_cancel_refill_before_waiting_for_its_board_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            session = a_session(Path(folder) / 'session', write_panel_project(Path(folder)))
            try:
                session.refresh()
                for action in (session.generate_edge_plating, session.remove_edge_plating):
                    token = threading.Event()
                    held = threading.Event()
                    cancelled = []
                    session._upgrade_token = token

                    def refill_holding_board():
                        with session._board_lock:
                            held.set()
                            cancelled.append(token.wait(3))

                    worker = threading.Thread(target=refill_holding_board)
                    worker.start()
                    try:
                        self.assertTrue(held.wait(3))
                        action()
                    finally:
                        token.set()
                        worker.join(3)
                    self.assertEqual(cancelled, [True])
            finally:
                session.close()

    def test_manual_plating_survives_score_changes_until_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = write_panel_project(Path(folder))
            session = a_session(Path(folder) / 'session', config_path)
            try:
                session.refresh()
                session.generate_edge_plating()
                self.assertFalse(session.config.edge_plating)

                def plating():
                    return [item.model_dump() for item in session.board.drawings
                            if getattr(item, 'layer', None) == edge_plating.LAYER]

                generated = plating()
                self.assertTrue(generated)
                session.set_param({'score_rule_half_width_mm': 0.8})
                self.assertEqual(plating(), generated)
                session.remove_edge_plating()
                self.assertFalse(plating())
                session.refresh()
                self.assertFalse(plating())
                session.generate_edge_plating()
                self.assertTrue(plating())
                session.set_param({'edge_plating': True})
                session.remove_edge_plating()
                self.assertFalse(session.config.edge_plating)
                self.assertFalse(plating())
            finally:
                session.close()

    def test_checkbox_and_score_changes_update_plating_without_another_action(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = write_panel_project(Path(folder))
            session = a_session(Path(folder) / 'session', config_path)
            try:
                session.refresh()
                session.set_param({'edge_plating': True})

                def plating():
                    return [item.model_dump() for item in session._held_board().drawings
                            if getattr(item, 'layer', None) == edge_plating.LAYER]

                self.assertTrue(plating())
                self.assertIn(edge_plating.LAYER, session.layers['visible'])
                session.set_layer_visibility(visible=[], known=[edge_plating.LAYER])
                candidate = session.candidates[0]
                blade = {'start': candidate.start, 'end': candidate.end}
                session.decline(blade)
                without_score = plating()
                session.accept(blade)
                with_score = plating()
                self.assertNotEqual(with_score, without_score)
                session.set_param({'score_rule_half_width_mm': 0.8})
                self.assertNotEqual(plating(), with_score)
                self.assertNotIn(edge_plating.LAYER, session.layers['visible'])
                session.set_param({'edge_plating': False})
                self.assertFalse(plating())
            finally:
                session.close()
