"""The panel's companion `.kicad_pro`, checked on a project tree in memory.

`eedesigncore` models a project file as a preserved JSON tree, so the thing
worth pinning is exactly what the four edits touch - and, just as much, that
everything else survives them untouched.
"""

import unittest

from eedesigncore import ProjectFile

from eedesignpanels.project import PANEL_MARKER, PANEL_MARKER_VALUE, build_project

SOURCE_JSON = """{
  "board": {"design_settings": {"defaults": {"board_outline_line_width": 0.05}}},
  "cvpcb": {"equivalence_files": []},
  "erc": {"erc_exclusions": []},
  "meta": {"filename": "design.kicad_pro", "version": 3},
  "net_settings": {"classes": [{"name": "Default"}, {"name": "Analog"}]},
  "schematic": {
    "bom_export_filename": "",
    "top_level_sheets": [{"uuid": "651ed2da-3474-44bd-a907-0c667fa7080e"}]
  },
  "sheets": [["651ed2da-3474-44bd-a907-0c667fa7080e", "design"]],
  "text_variables": {"PART_ID": "100-200-3000", "PCB_DWG_SCALE": "10"},
  "an_unknown_key": {"kicad": "never heard of it"}
}
"""


def source_project():
    return ProjectFile.from_string(SOURCE_JSON)


class BuildProjectTest(unittest.TestCase):
    def setUp(self):
        self.source = source_project()
        self.panel = build_project(self.source, filename="panel.kicad_pro")

    def test_the_panel_names_itself_not_the_design(self):
        self.assertEqual(self.panel.meta.filename, "panel.kicad_pro")

    def test_the_text_variables_come_across(self):
        self.assertEqual(self.panel.text_variables["PART_ID"], "100-200-3000")
        self.assertEqual(self.panel.text_variables["PCB_DWG_SCALE"], "10")

    def test_the_panel_marker_is_added(self):
        self.assertEqual(self.panel.text_variables[PANEL_MARKER], PANEL_MARKER_VALUE)

    def test_the_net_classes_come_across(self):
        classes = self.panel.value_at("net_settings.classes")
        self.assertEqual([entry["name"] for entry in classes], ["Default", "Analog"])

    def test_the_board_settings_come_across(self):
        self.assertEqual(
            self.panel.value_at("board.design_settings.defaults.board_outline_line_width"),
            0.05,
        )

    def test_nothing_names_the_designs_schematic_any_more(self):
        self.assertEqual(self.panel.value_at("sheets"), [])
        self.assertEqual(self.panel.value_at("schematic.top_level_sheets"), [])

    def test_the_schematic_only_sections_are_dropped(self):
        self.assertNotIn("erc", self.panel.section_names())
        self.assertNotIn("cvpcb", self.panel.section_names())

    def test_a_key_the_library_does_not_model_survives(self):
        # The preserve-only policy is the whole reason to copy and edit rather
        # than compose: a key nobody here understands must still reach KiCad.
        self.assertEqual(
            self.panel.value_at("an_unknown_key.kicad"), "never heard of it"
        )

    def test_the_source_project_is_never_modified(self):
        self.assertEqual(self.source.meta.filename, "design.kicad_pro")
        self.assertNotIn(PANEL_MARKER, self.source.text_variables)
        self.assertIn("erc", self.source.section_names())
        self.assertEqual(len(self.source.value_at("sheets")), 1)

    def test_building_twice_gives_the_same_bytes(self):
        again = build_project(source_project(), filename="panel.kicad_pro")
        self.assertEqual(self.panel.to_string(), again.to_string())

    def test_the_result_round_trips_through_the_reader(self):
        reread = ProjectFile.from_string(self.panel.to_string())
        self.assertEqual(reread.to_string(), self.panel.to_string())
        self.assertEqual(reread.text_variables[PANEL_MARKER], PANEL_MARKER_VALUE)


class MissingBlocksTest(unittest.TestCase):
    def test_a_project_with_no_text_variables_gets_the_marker_anyway(self):
        source = ProjectFile.from_string('{"meta": {"filename": "d.kicad_pro"}}\n')
        panel = build_project(source, filename="panel.kicad_pro")
        self.assertEqual(panel.text_variables, {PANEL_MARKER: PANEL_MARKER_VALUE})

    def test_a_project_with_no_schematic_block_is_not_an_error(self):
        source = ProjectFile.from_string('{"board": {}}\n')
        panel = build_project(source, filename="panel.kicad_pro")
        self.assertEqual(panel.value_at("sheets"), [])


if __name__ == "__main__":
    unittest.main()
