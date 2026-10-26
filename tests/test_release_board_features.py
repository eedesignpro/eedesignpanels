"""Release regressions for board saves, routing and preview inputs."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eedesigncore import Board, BoardStackup, Footprint, PcbLine, Setup, Stroke
from test_panel_page import a_session, write_panel_project

from eedesignpanels import fplib, rigid_flex
from eedesignpanels.preview import actions
from eedesignpanels.review import overlay
from eedesignpanels.stages import edge_plating, panelize, step_and_repeat
from eedesignpanels.toolpath import Line


def line(layer='User.39', start=(0, 0), end=(10, 0), width=0.4):
    return PcbLine(start=start, end=end, layer=layer, stroke=Stroke(width=width))


class BoardSaveRegressionTest(unittest.TestCase):
    def test_plating_setting_tracks_real_geometry_and_save_does_not_mutate(self):
        board = Board(setup=Setup(stackup=BoardStackup(edge_plating=False)),
                      drawings=[line()])
        saved = actions.board_for_save(board, {})
        self.assertTrue(saved.setup.stackup.edge_plating)
        self.assertTrue(saved.drawings[0].locked)
        self.assertFalse(board.setup.stackup.edge_plating)
        self.assertFalse(board.drawings[0].locked)
        saved.drawings.clear()
        self.assertFalse(actions.board_for_save(saved, {}).setup.stackup.edge_plating)

    def test_mask_zero_length_and_zero_width_do_not_claim_plating(self):
        for item in [line('F.Mask'), line(end=(0, 0)), line(width=0)]:
            with self.subTest(item=item):
                board = Board(setup=Setup(stackup=BoardStackup(edge_plating=True)),
                              drawings=[item])
                self.assertFalse(actions.board_for_save(board, {}).setup.stackup.edge_plating)

    def test_trailing_plating_counts_and_lock_can_be_disabled(self):
        board = Board(setup=Setup(stackup=BoardStackup()), trailing_drawings=[line()])
        saved = actions.board_for_save(board, {'lock_board_on_save': False})
        self.assertTrue(saved.setup.stackup.edge_plating)
        self.assertFalse(saved.trailing_drawings[0].locked)

    def test_plating_without_stackup_reports_failure(self):
        with self.assertRaisesRegex(ValueError, 'stackup'):
            actions.board_for_save(Board(drawings=[line()]), {})

    def test_saved_prepared_board_does_not_warn_after_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = write_panel_project(folder, prepped=False)
            session = a_session(folder, path)
            session.save()
            reopened = a_session(folder, path)
            reopened.build_panel({})
            self.assertFalse(reopened.panel.board_differs)
            self.assertEqual(reopened.panel_fragment()['unsaved'], '')


class MaskRegressionTest(unittest.TestCase):
    def test_masks_use_own_radius_and_remove_only_owned_artwork(self):
        unrelated = line('F.Mask', start=(20, 0), end=(25, 0))
        board = Board(drawings=[unrelated])
        config = SimpleNamespace(edge_plating_front_mask=True,
                                 edge_plating_back_mask=True,
                                 edge_plating_mask_width_mm=0.2)
        result = SimpleNamespace(boundary_runs=(), boundary_chain=(), jobs=[])
        with mock.patch.object(edge_plating, 'contact_edges', return_value=[Line((0, 0), (10, 0))]):
            edge_plating.generate(board, config, result)
        generated = [item for item in board.drawings if item.id != unrelated.id]
        self.assertEqual({item.layer for item in generated}, {'User.39', 'F.Mask', 'B.Mask'})
        for item in generated:
            radius = 0.2 if item.layer == 'User.39' else 0.1
            self.assertAlmostEqual(item.start[0], radius)
            self.assertAlmostEqual(item.end[0], 10 - radius)
            self.assertAlmostEqual(item.stroke.width, radius * 2)
        # Ownership must survive a KiCad file round trip.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'board.kicad_pcb'
            board.to_file(path)
            restored = Board.from_file(path)
        edge_plating.clear(restored)
        self.assertEqual([item.id for item in restored.drawings], [unrelated.id])
        self.assertFalse(restored.groups)


class FiducialLibrarySelectionRegressionTest(unittest.TestCase):
    def test_the_default_and_an_explicit_library_resolve_by_their_own_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'Fiducial_Test.kicad_mod').touch()
            def row(nickname):
                return SimpleNamespace(nickname=nickname, uri=directory,
                                       raw_uri=str(directory), source_table=directory / 'fp-lib-table',
                                       scope='global')
            default = fplib.DEFAULT_FIDUCIAL_LIBRARY
            self.assertEqual(default, 'Fiducial')
            for requested in (default, 'My_Fiducials'):
                table = SimpleNamespace(libraries=[row(requested)], nicknames=[requested])
                with mock.patch.object(fplib, 'resolve_lib_table', return_value=table):
                    resolved = fplib.resolve(requested, settings=directory)
                    self.assertEqual(resolved.library, requested)
                    self.assertIn('Fiducial_Test', resolved.footprints)
                    self.assertTrue(fplib.resolve('OtherLibrary', settings=directory).reason)
            # No silent substitute: a library that is not in the table is a
            # reason, even when another library holds fiducials.
            table = SimpleNamespace(libraries=[row('My_Fiducials')], nicknames=['My_Fiducials'])
            with mock.patch.object(fplib, 'resolve_lib_table', return_value=table):
                missing = fplib.resolve(default, settings=directory)
                self.assertFalse(missing.ok)
                self.assertIn(default, missing.reason)


class FootprintOutlineRegressionTest(unittest.TestCase):
    def test_footprint_only_outline_measures_rotated_board_coordinates(self):
        points = [(0, 0), (10, 0), (10, 5), (0, 5)]
        drawings = [line(layer, start, end) for layer in ['Edge.Cuts', 'User.24']
                    for start, end in zip(points, points[1:] + points[:1])]
        board = Board(footprints=[Footprint(fp_id='Outline', layer='F.Cu',
                      at={'position': (100, 200), 'angle': 90}, drawings=drawings)])
        geometry = step_and_repeat.panel_geometry(board, 2, 2, 2)
        self.assertAlmostEqual(geometry.outline_box.max_x - geometry.outline_box.min_x, 5)
        self.assertAlmostEqual(geometry.outline_box.max_y - geometry.outline_box.min_y, 10)
        self.assertGreater(geometry.outline_box.min_x, 90)
        loops = rigid_flex.region_loops(board, 0.01)
        self.assertEqual(len(loops), 1)
        self.assertAlmostEqual(sum(piece.length for piece in loops[0]), 30)
        self.assertFalse(board.drawings)


class ScoreAlignmentRegressionTest(unittest.TestCase):
    def test_slightly_sloped_repeated_scores_warn_but_aligned_scores_do_not(self):
        placements = [SimpleNamespace(row=0, column=0), SimpleNamespace(row=0, column=1)]
        aligned = [[line('User.38')], [line('User.38', (12, 0), (22, 0))]]
        self.assertFalse(panelize.score_alignment_warnings(aligned, placements))
        sloped = [[line('User.38', (0, 0), (10, 0.05))],
                  [line('User.38', (12, 0), (22, 0.05))]]
        original = [[item.model_dump() for item in copy] for copy in sloped]
        self.assertTrue(panelize.score_alignment_warnings(sloped, placements))
        self.assertEqual(original, [[item.model_dump() for item in copy] for copy in sloped])

    def test_score_extensions_are_noninteractive_and_preserve_angle(self):
        svg = overlay.score_extensions([line('User.38', (0, 0), (10, 1))],
                                       (0, 0), overlay.ViewBox(0, 0, 30, 20))
        paths = ET.fromstring(svg).findall('path')
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertEqual(path.attrib['pointer-events'], 'none')
            self.assertIn('stroke-dasharray', path.attrib)
            self.assertNotIn('data-act', path.attrib)
            _, x0, y0, _, x1, y1 = path.attrib['d'].split()
            self.assertAlmostEqual((float(y1) - float(y0)) / (float(x1) - float(x0)), 0.1, places=4)

class TextPreviewRegressionTest(unittest.TestCase):
    def test_project_variables_render_like_literal_text_without_editing_source(self):
        import json

        from eedesigncore import PcbText, ProjectFile
        from eedesigncore.render import render_board_svg

        from eedesignpanels.preview import render_seam
        board = Board(drawings=[PcbText(content='${PART_ID}', layer='F.SilkS', position=(2, 2))])
        literal = board.model_copy(deep=True)
        literal.drawings[0].content = '123-456'
        with tempfile.TemporaryDirectory() as temporary:
            project_path = Path(temporary) / 'source.kicad_pro'
            project_path.write_text(json.dumps({'text_variables': {'PART_ID': '123-456'}}))
            project = ProjectFile.from_file(project_path)
            def render(subject):
                return render_seam.render_with_library(
                    subject, [SimpleNamespace(canonical_name='F.SilkS')], (0, 0, 30, 10),
                    {}, render_board_svg, renderer_name='render_board_svg',
                    project=project, filename='source.kicad_pcb')
            self.assertEqual(render(board), render(literal))
            self.assertEqual(board.drawings[0].content, '${PART_ID}')

    def test_mask_width_above_limit_is_rejected(self):
        with self.assertRaises(actions.ActionError):
            actions._coerced('edge_plating_mask_width_mm', 10.01)


class PanelSaveEquivalenceRegressionTest(unittest.TestCase):
    def test_lock_setting_changes_refresh_existing_panel_and_match_headless_build(self):
        from eedesignpanels.config import load_config
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = write_panel_project(folder, prepped=False)
            session = a_session(folder, path)
            session.build_panel({})
            for enabled in (False, True):
                with self.subTest(lock_board_on_save=enabled):
                    before = session.panel
                    session.set_param({'lock_board_on_save': enabled})
                    self.assertIsNot(session.panel, before)
                    session.save()
                    session.build_panel({})
                    self.assertFalse(session.panel.board_differs)
                    session.save_panel()
                    preview_bytes = session.panel_saved.output_path.read_bytes()
                    expected = step_and_repeat.run(load_config(path))
                    self.assertEqual(expected.output_path.read_bytes(), preview_bytes)
