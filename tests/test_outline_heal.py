"""The heal is a declared setting, and every heal it allows is announced.

The heal threshold is a setting on step 1, and each heal it allows is
reported with its place and size, so a bad outline is found and fixed at the
source rather than hidden.

Three things are pinned here. The default is KiCad's own figure
(`pcbnew/board.h:86-88`, 10.0.3), because departing from it would refuse boards
KiCad accepts. Zero refuses any gap at all. And a gap that IS closed is stated
with its coordinate and its distance in every place a person reads - which is
the warnings list, because the page, the panel report and the session record
all read it.
"""

import json
import tempfile
import unittest
from pathlib import Path

from eedesignpanels import prep
from eedesignpanels.config import (
    CONFIG_REVISION,
    MAX_OUTLINE_HEAL_MM,
    OUTLINE_HEAL_DEFAULT_MM,
    ConfigError,
    PanelConfig,
    load_config,
)
from eedesignpanels.preview import actions, page_served
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import (
    CHAINING_EPSILON_MM,
    JOIN_TOL_MM,
    Heal,
    Line,
    OffsetError,
    chain_loops,
)

# An outline whose last end stops
# 8.9 um short of its first. Small enough that KiCad builds the board, big
# enough that it is nothing a coordinate rounding step could explain.
GAP_MM = 0.0089


def gapped_square(gap_mm: float = GAP_MM) -> list:
    """A 40 x 30 mm outline whose closing end falls *gap_mm* short."""
    corners = [(100.0, 100.0), (140.0, 100.0), (140.0, 130.0), (100.0, 130.0)]
    chain = []
    for index in range(4):
        start = corners[index]
        end = corners[(index + 1) % 4]
        if index == 3:
            end = (end[0], end[1] + gap_mm)
        chain.append(Line(p0=start, p1=end, source=f"e{index}", origin=index))
    return chain


class ChainHealTest(unittest.TestCase):
    """What the chain does with a gap, and what it says about it."""

    def test_the_default_closes_kicads_own_gap_and_records_it(self):
        heals = []
        loops = chain_loops(gapped_square(), CHAINING_EPSILON_MM, heals)
        self.assertEqual(len(loops), 1)
        self.assertEqual(len(heals), 1)
        self.assertAlmostEqual(heals[0].distance_mm, GAP_MM, places=9)
        self.assertAlmostEqual(heals[0].at[0], 100.0, places=6)
        self.assertAlmostEqual(heals[0].at[1], 100.0 + GAP_MM, places=6)

    def test_a_rounding_step_is_not_a_heal(self):
        # Every real file's joints sit tens of nanometres apart. Reporting those
        # would bury the one gap a designer can act on.
        heals = []
        chain_loops(gapped_square(JOIN_TOL_MM / 10.0), CHAINING_EPSILON_MM, heals)
        self.assertEqual(heals, [])

    def test_a_loop_that_meets_exactly_reports_nothing(self):
        heals = []
        chain_loops(gapped_square(0.0), CHAINING_EPSILON_MM, heals)
        self.assertEqual(heals, [])

    def test_zero_refuses_any_gap_at_all(self):
        with self.assertRaises(OffsetError) as caught:
            chain_loops(gapped_square(), 0.0, [])
        self.assertIn("8.900 um", str(caught.exception))

    def test_a_setting_under_the_gap_refuses_too(self):
        with self.assertRaises(OffsetError) as caught:
            chain_loops(gapped_square(), 0.005, [])
        said = str(caught.exception)
        # BOTH DISTANCES: how far apart the ends are, and
        # what the setting is, so the reader knows which of the two to change.
        self.assertIn("8.900 um", said)
        self.assertIn("5.000 um", said)
        self.assertIn("(100.0000, 100.0089) mm", said)

    def test_an_exactly_closed_loop_still_chains_at_zero(self):
        # Stricter than KiCad is not the same as broken: a board drawn shut
        # builds at any setting.
        self.assertEqual(len(chain_loops(gapped_square(0.0), 0.0, [])), 1)


class HealSentenceTest(unittest.TestCase):
    """The one line the page, the report and the record all carry."""

    def test_it_names_where_and_how_far(self):
        said = Heal((12.5, -3.25), 0.0089).sentence
        self.assertIn("(12.5000, -3.2500) mm", said)
        self.assertIn("8.900 um", said)
        self.assertIn("0.008900 mm", said)
        self.assertIn("does not meet", said)


class HealConfigTest(unittest.TestCase):
    """The setting obeys every rule the other settings obey."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.panel_dir = self.project / "panel"
        self.panel_dir.mkdir()
        (self.project / "board.kicad_pcb").write_text("(kicad_pcb)", "utf-8")

    def written(self, **extra):
        data = {
            "revision": CONFIG_REVISION,
            "source_board": "../board.kicad_pcb",
            "grid": {"columns": 2, "rows": 3, "gap_mm": 3.0},
            **extra,
        }
        path = self.panel_dir / "panel.json"
        path.write_text(json.dumps(data), "utf-8")
        return path

    def test_the_default_is_kicads_own_figure(self):
        self.assertEqual(OUTLINE_HEAL_DEFAULT_MM, CHAINING_EPSILON_MM)
        self.assertEqual(load_config(self.written()).outline_heal_mm,
                         CHAINING_EPSILON_MM)

    def test_it_is_always_written_to_the_file(self):
        config = load_config(self.written())
        self.assertIn("outline_heal_mm", config.model_dump())

    def test_zero_is_a_legal_setting(self):
        self.assertEqual(
            load_config(self.written(outline_heal_mm=0.0)).outline_heal_mm, 0.0
        )

    def test_a_negative_gap_is_refused(self):
        with self.assertRaises(ConfigError):
            load_config(self.written(outline_heal_mm=-0.001))

    def test_a_gap_no_board_has_is_refused(self):
        with self.assertRaises(ConfigError):
            load_config(self.written(
                outline_heal_mm=MAX_OUTLINE_HEAL_MM + 1.0
            ))

    def test_a_copy_carries_it(self):
        config = PanelConfig(
            source_board="../board.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 1.0},
            outline_heal_mm=0.0,
        )
        self.assertEqual(config.model_copy().outline_heal_mm, 0.0)


class HealOnStepOneTest(unittest.TestCase):
    """The heal field, beside the cutter and the keep-out."""

    @classmethod
    def setUpClass(cls):
        cls.markup = page_served._controls_html(1, "stub", "User.38")

    def test_the_page_carries_the_field(self):
        self.assertIn("<input type=number id=pb-heal data-act=param",
                      self.markup)
        self.assertIn("<span class=t>Largest outline gap closed (mm)</span>",
                      self.markup)

    def test_it_sits_between_the_keep_out_and_the_minimum_tab(self):
        self.assertLess(self.markup.index("pb-sec-router"),
                        self.markup.index("pb-heal"))
        self.assertLess(self.markup.index("pb-heal"),
                        self.markup.index("pb-mintab"))

    def test_the_field_carries_a_name_and_one_short_line(self):
        # The GUI carries no prose: a control gets a name and at most one line.
        self.assertIn(
            "<span class=h>Zero refuses any gap. Every gap closed is "
            "reported.</span>",
            self.markup,
        )

    def test_the_script_reads_and_writes_it(self):
        script = page_served.CONTROLS_JS
        self.assertIn("const healEl = document.getElementById('pb-heal');",
                      script)
        self.assertEqual(script.count("[healEl, 'outline_heal_mm']"), 2)
        self.assertIn("minTabEl, healEl, elimEl", script)

    def test_a_click_may_set_it_and_it_costs_a_prep(self):
        self.assertEqual(actions.SETTABLE["outline_heal_mm"],
                         ("outline_heal_mm",))
        self.assertIn("outline_heal_mm", actions.PREP_PARAMS)
        self.assertIn("outline_heal_mm", actions.PREP_SECTIONS)

    def test_the_page_is_told_what_the_setting_is(self):
        config = PanelConfig(
            source_board="../board.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 1.0},
            prep={"cutter_diameter": 2.0},
            outline_heal_mm=0.004,
        )
        self.assertEqual(page_served.params_json(config)["outline_heal_mm"],
                         0.004)


class HealReportedTest(unittest.TestCase):
    """Announced in the places a person already reads."""

    def test_the_stage_puts_every_heal_in_the_warnings(self):
        warnings = []
        boundary, jobs, heals = route_toolpath.routing_jobs(
            gapped_square(), 2.0, warnings
        )
        self.assertEqual(len(heals), 1)
        self.assertEqual(len(boundary), 4)
        self.assertTrue(
            any(note.startswith("OUTLINE HEALED") for note in warnings),
            warnings,
        )

    def test_a_tighter_setting_refuses_and_names_both_distances(self):
        with self.assertRaises(OffsetError) as caught:
            route_toolpath.routing_jobs(gapped_square(), 2.0, [], 0.0)
        self.assertIn("8.900 um", str(caught.exception))

    def test_the_subtitle_states_it(self):
        result = _stub_result(heals=(Heal((100.0, 100.0089), GAP_MM),))
        self.assertIn("OUTLINE HEALED", prep.subtitle_for(result))

    def test_a_clean_outline_says_nothing_about_healing(self):
        self.assertNotIn("OUTLINE HEALED", prep.subtitle_for(_stub_result()))


def _stub_result(heals=()):
    """A `PrepResult` with nothing in it but what the subtitle reads."""
    return route_toolpath.PrepResult(
        board_path=Path("board.kicad_pcb"),
        board_sha256="",
        cutter_diameter_mm=2.0,
        tabs=[],
        stops=[],
        removed=0,
        outline_items=4,
        loop_items=4,
        written=4,
        run_lengths=[10.0],
        bridges=[],
        tool_max_mm=None,
        warnings=[],
        load_seconds=0.0,
        build_seconds=0.0,
        write_seconds=0.0,
        heals=heals,
    )


if __name__ == "__main__":
    unittest.main()
