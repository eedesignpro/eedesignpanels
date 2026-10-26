"""Pocketing is a checkbox, and it is OFF by default.

Three things are pinned here, and the first is the one that matters most. OFF,
the tool routes only - the wall of a wide cutout is routed, the middle is left
attached to nothing, and the report says so. ON, concentric passes clear the
middle and no loose piece remains. Either way the report names which cutouts
were pocketed and which were left.

The measured case: a 6 mm cutout at a 0.88 mm cutter leaves a 4.24 mm
loose piece.
"""

import unittest

from eedesignpanels import cuttability
from eedesignpanels.config import PanelConfig
from eedesignpanels.preview import actions, page_served
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import Line, chain_bbox

CUTTER = 0.88


def rectangle(x0: float, y0: float, x1: float, y1: float, tag: str) -> list:
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        Line(p0=points[index], p1=points[(index + 1) % 4],
             source=f"{tag}{index}", origin=index)
        for index in range(4)
    ]


def a_board_with_a_wide_cutout() -> list:
    """A 40 x 20 board with the measured 6 mm cutout in the middle of it."""
    return [*rectangle(0.0, 0.0, 40.0, 20.0, "b"),
            *rectangle(10.0, 7.0, 16.0, 13.0, "h")]


class ConfigTest(unittest.TestCase):
    """The setting, where it lives and what it says when nobody sets it."""

    def a_config(self, **fields) -> PanelConfig:
        return PanelConfig(
            source_board="../board.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 1.0},
            prep={"cutter_diameter": CUTTER},
            **fields,
        )

    def test_the_default_is_off(self):
        self.assertFalse(self.a_config().pocket_cutouts)

    def test_it_can_be_declared_on(self):
        self.assertTrue(self.a_config(pocket_cutouts=True).pocket_cutouts)

    def test_a_copy_carries_it(self):
        config = self.a_config(pocket_cutouts=True)
        self.assertTrue(config.model_copy().pocket_cutouts)

    def test_the_page_is_told_what_the_setting_is(self):
        self.assertFalse(
            page_served.params_json(self.a_config())["pocket_cutouts"])
        self.assertTrue(
            page_served.params_json(
                self.a_config(pocket_cutouts=True))["pocket_cutouts"])


class ControlTest(unittest.TestCase):
    """One checkbox, with the routing settings, a name and one short line."""

    @classmethod
    def setUpClass(cls):
        cls.markup = page_served._controls_html(1, "stub", "User.38")

    def test_the_page_carries_the_box(self):
        self.assertIn("id=pb-pocket", self.markup)
        self.assertIn("<span class=t>Pocket cutouts</span>", self.markup)

    def test_it_sits_under_the_sliver_switch(self):
        self.assertLess(self.markup.index("pb-elim"),
                        self.markup.index("pb-pocket"))

    def test_the_box_carries_a_name_and_one_short_line(self):
        # The GUI carries no prose: a control gets a name and at most one line.
        self.assertIn(
            "<span class=h>Mill out a cutout wider than the cutter. Off "
            "leaves the middle.</span>",
            self.markup,
        )

    def test_the_script_reads_and_writes_it(self):
        script = page_served.CONTROLS_JS
        self.assertIn("const pocketEl = document.getElementById('pb-pocket');",
                      script)
        self.assertEqual(script.count("PB.params.pocket_cutouts"), 3)
        self.assertIn("elimEl, pocketEl", script)

    def test_a_click_may_set_it_and_it_costs_a_prep(self):
        self.assertEqual(actions.SETTABLE["pocket_cutouts"],
                         ("pocket_cutouts",))
        self.assertIn("pocket_cutouts", actions.BOOLEAN_PARAMS)
        self.assertIn("pocket_cutouts", actions.PREP_PARAMS)
        self.assertIn("pocket_cutouts", actions.PREP_SECTIONS)


class StageTest(unittest.TestCase):
    """What the prep stage writes, both ways."""

    def jobs(self, pocket: bool) -> tuple:
        return route_toolpath.routing_jobs(
            a_board_with_a_wide_cutout(), CUTTER, [], pocket=pocket)[1]

    def test_off_leaves_the_users_own_slug(self):
        hole = self.jobs(pocket=False)[1]
        self.assertTrue(hole.leaves_slug)
        self.assertAlmostEqual(hole.slug_mm[0], 4.24, places=6)
        self.assertAlmostEqual(hole.slug_mm[1], 4.24, places=6)

    def test_off_is_the_default_of_the_stage_too(self):
        default = route_toolpath.routing_jobs(
            a_board_with_a_wide_cutout(), CUTTER, [])[1]
        self.assertEqual(default, self.jobs(pocket=False))

    def test_on_leaves_no_loose_piece(self):
        hole = self.jobs(pocket=True)[1]
        self.assertTrue(hole.pocketed)
        self.assertFalse(hole.leaves_slug)

    def test_on_says_so_in_the_warnings_and_off_says_the_other_thing(self):
        on, off = [], []
        route_toolpath.routing_jobs(
            a_board_with_a_wide_cutout(), CUTTER, on, pocket=True)
        route_toolpath.routing_jobs(
            a_board_with_a_wide_cutout(), CUTTER, off, pocket=False)
        self.assertTrue(any(note.startswith("POCKETED:") for note in on), on)
        self.assertFalse(any(note.startswith("LOOSE PIECE:") for note in on), on)
        self.assertTrue(
            any(note.startswith("LOOSE PIECE:") for note in off), off)
        self.assertFalse(
            any(note.startswith("POCKETED:") for note in off), off)

    def test_every_pass_stays_inside_the_cutout(self):
        hole = self.jobs(pocket=True)[1]
        for ring in hole.pocket:
            box = chain_bbox(ring)
            self.assertGreaterEqual(box[0], 10.0)
            self.assertLessEqual(box[2], 16.0)

    def test_the_boundary_is_never_pocketed(self):
        self.assertEqual(self.jobs(pocket=True)[0].pocket, ())

    def test_the_report_names_both_outcomes(self):
        milled = cuttability.report_lines(self.jobs(pocket=True), CUTTER)
        left = cuttability.report_lines(self.jobs(pocket=False), CUTTER)
        self.assertTrue(any("pocketed out in" in line for line in milled))
        self.assertTrue(any("leaves a loose piece" in line for line in left))


if __name__ == "__main__":
    unittest.main()
