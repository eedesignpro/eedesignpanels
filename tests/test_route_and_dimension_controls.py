"""Route-end distances and conservative fabrication annotations."""
import math
from types import SimpleNamespace
import unittest
from unittest import mock

from eedesigncore import Board, Dimension, DimensionType, PcbCircle, PcbLine

from eedesignpanels.channels import Rect
from eedesignpanels.stages import panel_dimensions as dims, route_toolpath as rt
from eedesignpanels.toolpath import Line, ScoreCandidate, ScoreSpan


class RouteControlsTests(unittest.TestCase):
    def test_disabled_custom_behavior_retains_but_does_not_apply_overrides(self):
        from eedesignpanels.config import VScoreConfig
        from test_route_toolpath import contoured_board
        config = VScoreConfig(route_past_score_mm=0, route_end_overrides={'orphan': 3})
        off = rt.build(contoured_board(), 0.8, config)
        self.assertFalse(any('Route end override' in warning for warning in off.warnings))
        config.custom_route_ends = True
        on = rt.build(contoured_board(), 0.8, config)
        self.assertTrue(any('Route end override orphan' in warning for warning in on.warnings))
        self.assertEqual(config.route_end_overrides, {'orphan': 3})

    def test_requested_tangent_contact_is_allowed_but_penetration_warns(self):
        span = ScoreSpan((), (0, 0), (10, 0), 10, ())
        candidate = ScoreCandidate((span,), (0, 0), (10, 0), (0, 1), 10, 10, (), ())
        score = rt.Score(0, 'score', candidate, frozenset())
        contact = Line((0, -0.5), (10, -0.5), role='score_extension')
        intrusion = Line((0, -0.4), (10, -0.4), role='score_extension')
        self.assertFalse(rt.routed_scores([[contact]], [score], 0.5))
        self.assertTrue(rt.routed_scores([[intrusion]], [score], 0.5))

    def route(self, angle, overrides=None):
        theta = math.radians(angle)
        piece = Line((0, 0), (math.cos(theta), math.sin(theta)), source='edge')
        candidate = ScoreCandidate((), (0, 0), (10, 0), (0, 1), 10, 10, (), ())
        score = rt.Score(0, 'score', candidate, frozenset())
        warnings = []
        with mock.patch.object(rt, 'split_run_at_scores', return_value=[([piece], None, score)]):
            runs, junctions = rt.apply_scores([[piece]], [score], 2, warnings, [],
                                             along_score=5, tangent_angle=10, overrides=overrides)
        return piece, runs, junctions, warnings

    def test_tangent_and_oblique_near_threshold_extend_five_along_route(self):
        for angle in (0, 5, 9.9):
            piece, runs, ends, _ = self.route(angle)
            self.assertAlmostEqual(runs[0][-1].length, 5)
            self.assertEqual(runs[0][-1].start, piece.end)
            self.assertAlmostEqual(ends[0].approach_angle_deg, angle)

    def test_at_threshold_uses_crossing_rule(self):
        with mock.patch.object(rt, '_end_run_at_score', return_value=([], rt.ScoreJunction(0, 'score', (0, 0), 2, 2, 'test', 0, True))) as finish:
            self.route(10)
            self.assertEqual(finish.call_args.args[2], 2)

    def test_end_override_and_unmatched_override(self):
        _, _, ends, _ = self.route(0)
        key = ends[0].end_id
        _, runs, again, _ = self.route(0, {key: 3})
        self.assertAlmostEqual(runs[0][-1].length, 3)
        self.assertEqual(again[0].end_id, key)
        self.assertTrue(self.route(0, {'unmatched': 1})[3])


class DimensionControlsTests(unittest.TestCase):
    def setUp(self):
        self.board = Board()
        self.blank = Rect(0, 0, 35, 20)
        self.boxes = [(5, 5, 15, 15), (20, 5, 30, 15)]
        points = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.outline = [Line(a, b) for a, b in zip(points, points[1:] + points[:1])]
        self.holes = SimpleNamespace(holes=[SimpleNamespace(at=(2, 2), diameter_mm=1, designator='PNLTH1')])

    def test_measured_gap_borders_and_diameter_survive_file_roundtrip(self):
        result = dims.apply(self.board, self.blank, self.boxes, source_outline=self.outline, holes=self.holes)
        self.assertEqual(result.value_of('board_gap_0_5000000'), 5)
        self.assertEqual(result.value_of('rail_right'), 5)
        self.assertEqual(sum(name.startswith('rail_') for name in result.by_name), 1)
        self.assertEqual(result.value_of('hole_diameter_PNLTH1'), 1)
        restored = Board.from_string(self.board.to_string())
        self.assertEqual(sum(isinstance(item, PcbCircle) for item in restored.drawings), 1)
        diameter = [item for item in restored.drawings if isinstance(item, Dimension) and item.dim_type == DimensionType.leader]
        self.assertEqual(len(diameter), 1)

    def test_irregular_layout_omits_gaps_and_keeps_other_annotations(self):
        warnings = []
        result = dims.apply(self.board, self.blank, [(5, 5, 15, 15), (20, 6, 30, 16)], holes=self.holes, warnings=warnings)
        self.assertFalse(any(name.startswith('board_gap') for name in result.by_name))
        self.assertIn('hole_diameter_PNLTH1', result.by_name)
        self.assertTrue(any('omitted' in text for text in warnings))

    def test_repeated_generation_preserves_user_art_without_duplicates(self):
        self.board.drawings.append(PcbLine(start=(0, 0), end=(1, 1), layer='User.16', id='user-line'))
        dims.apply(self.board, self.blank, self.boxes, holes=self.holes)
        first = self.board.to_string()
        dims.apply(self.board, self.blank, self.boxes, holes=self.holes)
        self.assertEqual(first, self.board.to_string())
        self.assertEqual(self.board.drawings[0].id, 'user-line')


class RevisedDimensionLayoutTests(unittest.TestCase):
    def make(self, settings=None):
        board = Board()
        holes = SimpleNamespace(holes=[SimpleNamespace(at=p, diameter_mm=3.175, designator=f'H{i}')
                                      for i, p in enumerate(((5, 5), (95, 5), (95, 75), (5, 75)))])
        warnings = []
        made = dims.apply(board, Rect(0, 0, 100, 80),
                          [(10, 10, 45, 35), (55, 10, 90, 35),
                           (10, 45, 45, 70), (55, 45, 90, 70)],
                          settings=settings, holes=holes, warnings=warnings)
        return board, made, warnings

    def test_single_leader_has_valid_native_style_and_one_unit_suffix(self):
        board, _, _ = self.make()
        restored = Board.from_string(board.to_string())
        leaders = [d for d in restored.drawings if isinstance(d, Dimension)
                   and d.dim_type == DimensionType.leader]
        self.assertEqual(len(leaders), 1)
        leader = leaders[0]
        self.assertIsNone(leader.style.extension_height)
        self.assertIsNone(leader.height)
        self.assertEqual(leader.dim_format.units_format, 0)
        self.assertEqual(leader.dim_format.override_value.count('mm'), 1)
        self.assertIn('4x', leader.text.content)
        self.assertGreater(leader.pts[1][0], leader.pts[0][0])
        self.assertGreater(leader.pts[1][1], leader.pts[0][1])
        self.assertGreater(leader.text.position[0], leader.pts[1][0])

    def test_gap_dimensions_do_not_require_a_rectangular_source_outline(self):
        _, made, _ = self.make()
        across = made.by_name['board_gap_0_10000000']
        down = made.by_name['board_gap_1_10000000']
        self.assertEqual(across.side, 'top')
        self.assertEqual(down.side, 'left')
        self.assertEqual(across.start, (45, 10))
        self.assertEqual(down.start, (10, 35))

    def test_offsets_are_staggered_and_preserved_at_automatic_and_override_sizes(self):
        for settings in (None, {'text_height_mm': 2.0}):
            board, made, warnings = self.make(settings)
            self.assertEqual(made.value_of('hole_offset_x'), 5)
            self.assertEqual(made.value_of('hole_offset_y'), 5)
            linear = [d for d in board.drawings if isinstance(d, Dimension)
                      and d.dim_type == DimensionType.orthogonal]
            offsets = [d for d in linear if d.text.content == '5 mm']
            self.assertEqual({d.orientation for d in offsets}, {0, 1})
            for item in offsets:
                self.assertNotEqual(item.pts[0][0], item.pts[1][0])
                self.assertNotEqual(item.pts[0][1], item.pts[1][1])
            self.assertFalse(any('could not be separated' in text for text in warnings))

    def test_outset_labels_share_distance_and_stay_centered(self):
        board, made, _ = self.make()
        distances = []
        for item in board.drawings:
            if not isinstance(item, Dimension) or item.dim_type != DimensionType.orthogonal:
                continue
            along = item.orientation
            cross = 1 - along
            self.assertAlmostEqual(item.text.position[along],
                                   (item.pts[0][along] + item.pts[1][along]) / 2, places=6)
            if item.style.text_position_mode == dims.TEXT_POSITION_MANUAL:
                distances.append(round(abs(item.text.position[cross]
                                           - item.pts[0][cross] - item.height), 6))
        self.assertEqual(len(set(distances)), 1)
        self.assertAlmostEqual(made.text_height_mm, 1.2)
        detail = {round(m.offset_mm, 6) for m in made.measures if m.name not in dims.OVERALL_NAMES}
        overall = {round(m.offset_mm, 6) for m in made.measures if m.name in dims.OVERALL_NAMES}
        self.assertEqual(len(detail), 1)
        self.assertEqual(len(overall), 1)
        self.assertGreater(min(overall), max(detail))

    def test_text_boxes_clear_other_labels_and_dimension_lines(self):
        from eedesigncore.render.dimensions import dimension_segments_mm
        board, _, _ = self.make()
        dimensions = [d for d in board.drawings if isinstance(d, Dimension)]
        for i, item in enumerate(dimensions):
            box = dims.annotation_box(item)
            for j, other in enumerate(dimensions):
                if i == j:
                    continue
                self.assertFalse(dims._boxes_overlap(box, dims.annotation_box(other)))
                self.assertFalse(any(dims._segment_hits_box(a, b, box)
                                     for a, b in dimension_segments_mm(other)))
