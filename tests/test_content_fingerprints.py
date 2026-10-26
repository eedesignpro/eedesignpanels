"""Generation evidence and independent read-only content checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from eedesigncore import Board, PcbLine, Property

from eedesignpanels import fingerprints as fp
from eedesignpanels.config import load_config
from eedesignpanels.stages import step_and_repeat
from test_panel_page import write_panel_project


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.settings = write_panel_project(self.folder)
        data = json.loads(self.settings.read_text())
        data['panel']['rail_copper'] = {'enabled': False}
        self.settings.write_text(json.dumps(data))
        self.config = load_config(self.settings)
        self.source = self.config.source_board_path
        self.panel = step_and_repeat.run(self.config).output_path

    def check(self):
        return fp.check_panel(source_board=self.source, settings=self.settings,
                              panel_board=self.panel, report_path=self.folder / 'report.json')

    def test_fresh_generation_matches_and_check_is_read_only(self):
        paths = (self.source, self.settings, self.panel)
        before = [p.read_bytes() for p in paths]
        self.assertEqual(self.check()['status'], 'matches')
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_direct_panel_edit_is_separate_from_source_edit(self):
        board = Board.from_file(self.panel)
        board.drawings.append(PcbLine(start=(1, 2), end=(3, 4), layer='F.Cu', stroke={'width': 0.2}))
        board.to_file(self.panel)
        result = self.check()
        self.assertEqual(result['checks']['inputs']['status'], 'matches')
        self.assertEqual(result['checks']['panel']['status'], 'differences_found')
        source = Board.from_file(self.source)
        source.drawings.append(PcbLine(start=(2, 3), end=(4, 5), layer='F.Cu', stroke={'width': 0.2}))
        source.to_file(self.source)
        self.assertEqual(self.check()['checks']['inputs']['changed'], ['source_board'])

    def test_settings_format_and_report_preferences_do_not_change_hash(self):
        data = json.loads(self.settings.read_text())
        data['verify'] = {'enabled': False}
        data['lock_board_on_save'] = False
        self.settings.write_text(json.dumps(data, sort_keys=True, indent=3))
        self.assertEqual(self.check()['status'], 'matches')
        data['grid']['gap_mm'] += 1
        self.settings.write_text(json.dumps(data))
        self.assertIn('panel_settings', self.check()['checks']['inputs']['changed'])

    def test_item_order_ids_and_locks_are_not_content(self):
        board = Board.from_file(self.panel)
        board.drawings.reverse()
        board.footprints.reverse()
        for item in board.drawings:
            if hasattr(item, 'id'):
                item.id = str(uuid.uuid4())
            if hasattr(item, 'locked'):
                item.locked = not item.locked
        board.to_file(self.panel)
        self.assertEqual(self.check()['status'], 'matches')

    def test_duplicate_item_is_content(self):
        board = Board.from_file(self.panel)
        board.drawings.append(board.drawings[0].model_copy(deep=True))
        board.to_file(self.panel)
        self.assertEqual(self.check()['checks']['panel']['status'], 'differences_found')

    def test_legacy_and_unsupported_records_are_unverified(self):
        board = Board.from_file(self.panel)
        fp.remove_stamp(board)
        board.to_file(self.panel)
        self.assertEqual(self.check()['status'], 'cannot_verify')
        board.properties.append(Property(name=fp.STAMP, value='{"fingerprint_version":99}'))
        board.to_file(self.panel)
        self.assertEqual(self.check()['status'], 'cannot_verify')

    def test_missing_panel(self):
        self.panel.unlink()
        self.assertEqual(self.check()['status'], 'no_panel')

    def test_missing_source_does_not_prevent_panel_check(self):
        self.source.unlink()
        result = self.check()
        self.assertEqual(result['checks']['inputs']['status'], 'cannot_verify')
        self.assertEqual(result['checks']['panel']['status'], 'matches')

    def test_difference_takes_precedence_over_unchecked_inputs(self):
        board = Board.from_file(self.panel)
        board.drawings.pop()
        board.to_file(self.panel)
        self.source.unlink()
        result = self.check()
        self.assertEqual(result['status'], 'differences_found')
        self.assertEqual(result['checks']['inputs']['status'], 'cannot_verify')

    def test_generation_cannot_stamp_changed_source(self):
        evidence = fp.capture(self.source, self.settings)
        before = self.panel.read_bytes()
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        with self.assertRaises(fp.Unverified):
            fp.stamp_saved(self.panel, evidence)
        self.assertEqual(before, self.panel.read_bytes())

    def test_concurrent_input_edit_invalidates_check(self):
        with mock.patch.object(fp, '_unchanged', return_value=False):
            self.assertEqual(self.check()['status'], 'cannot_verify')

    def test_report_cannot_overwrite_inputs(self):
        for target in (self.source, self.settings, self.panel):
            with self.assertRaises(ValueError):
                fp.check_panel(source_board=self.source, settings=self.settings,
                               panel_board=self.panel, report_path=target)

    def test_polygon_rotation_and_direction_are_noise(self):
        from eedesigncore.common.pcb_family.board_items import ZoneOutlinePolygon
        first = ZoneOutlinePolygon(pts=[(0, 0), (4, 0), (4, 4), (0, 4)])
        second = ZoneOutlinePolygon(pts=[(4, 4), (4, 0), (0, 0), (0, 4)])
        self.assertEqual(fp._normal(first), fp._normal(second))

    def test_unsaved_source_cannot_be_captured(self):
        held = Board.from_file(self.source)
        held.drawings.pop()
        with self.assertRaises(fp.Unverified):
            fp.capture(self.source, self.settings, held_board=held)

    def test_repeated_generation_has_identical_bytes(self):
        before = self.panel.read_bytes()
        step_and_repeat.run(self.config)
        self.assertEqual(before, self.panel.read_bytes())

    def test_preview_save_checks_fingerprints_and_plain_resave_preserves_record(self):
        from test_panel_page import a_session
        session = a_session(self.folder, self.settings)
        self.addCleanup(session.close)
        session.save()
        session.build_panel({})
        session.save_panel()
        self.assertEqual(self.check()['status'], 'matches')
        before = self.panel.read_bytes()
        session.save_panel()
        self.assertEqual(before, self.panel.read_bytes())
        self.assertEqual(session.check_fingerprints()['fingerprints']['status'], 'matches')

    def test_preview_unsaved_source_cannot_stamp_output(self):
        from test_panel_page import a_session
        session = a_session(self.folder, self.settings)
        self.addCleanup(session.close)
        session.save()
        session.set_param({'cutter_diameter': 1.0})
        session.build_panel({})
        session.save_panel()
        self.assertEqual(self.check()['status'], 'cannot_verify')

    def test_net_numbers_are_not_content(self):
        # Rename numeric codes consistently while retaining net names.
        import re
        text = self.panel.read_text()
        text = re.sub(r'\(net (\d+) ', lambda m: '(net ' + str(int(m[1]) + 100) + ' ', text)
        self.panel.write_text(text)
        self.assertEqual(self.check()['status'], 'matches')
