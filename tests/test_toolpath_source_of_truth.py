"""THE INVARIANT: `Edge.Cuts` is the outline of the toolpath layer's own ink.

The panel's `Edge.Cuts` is derived as the outline of the route toolpath
layer. Whether slivers are routed off is chosen on the PREP page, so the
covering passes are on the single board's toolpath layer before the panel is
built, and the panel traces that layer. The outline and the toolpath come
from one source, so a pass cannot shape the outline without being cut.

So there are two properties here and they are different:

* **THE FLOW.** Every pass that shapes the outline exists on the toolpath layer
  FIRST - the contour passes, the minimum-tab crossings, the pinch cuts - and
  the outline is derived from that layer. The fence is arithmetic: re-read
  `User.36` off the finished board, take the boundary of the blank minus what it
  says was cut, and it has to be the outline the build wrote down. A pass that
  changed the boundary without landing on the layer fails that by exactly its
  own width.
* **THE SWITCH.** Whether the thin material is routed off at all is the
  user's choice, on step 1. ON, the covering chains are computed at PREP time
  and land in the single board's `User.36`, so the board file is the source
  of truth and the panel steps them like any other ink. OFF, nothing is
  covered anywhere, every ribbon is named with its size and position, and the
  outline honestly follows the material that is left.

The raster half of the same fence - both answers drawn and subtracted, with
KiCad's own plot as the witness - lives in `test_outline_oracle.py`.
"""

import json
import math
import os
import unittest
from pathlib import Path

from eedesigncore import Board, BoardLayer, PcbLine, Stroke

from eedesignpanels import channels, mintab, outline_check
from eedesignpanels.stages import panelize
from eedesignpanels.toolpath import Line

# THE CORPUS IS PRIVATE AND OPTIONAL. It is a real project folder that holds
# `panel/panel.json`, named only by the EEDESIGNPANELS_CORPUS variable. With the
# variable unset, or naming a folder that is not there, these cases skip.
_CORPUS_SETTING = os.environ.get("EEDESIGNPANELS_CORPUS", "").strip()
CORPUS = Path(_CORPUS_SETTING) if _CORPUS_SETTING else None
CORPUS_CONFIG = CORPUS / "panel" / "panel.json" if CORPUS else None
HAVE_CORPUS = CORPUS_CONFIG is not None and CORPUS_CONFIG.is_file()
NO_CORPUS = (f"no corpus at {CORPUS}" if CORPUS
             else "EEDESIGNPANELS_CORPUS is not set")

# A 0.6 mm-class cutter: the size at which a consumed sliver's well shows in
# `Edge.Cuts` when the toolpath band stops short of it.
SNAPSHOT_CUTTER_MM = 0.6
# ...and the gap that puts a web BETWEEN two neighbouring instances, which is a
# sliver no board can have and which the panel therefore has to cut itself.
WEBBED_GAP_MM = 1.25

WIDTH = 0.889
RADIUS = WIDTH / 2.0
MIN_TAB = 0.5


# ---------------------------------------------------------------------------
# the rule: one width, and whether the router takes what is under it
# ---------------------------------------------------------------------------


class MinTabRuleTest(unittest.TestCase):
    """`mintab.Rule` is the number, and it carries the switch beside it."""

    def test_it_is_the_number_and_nothing_about_it_reads_differently(self):
        rule = mintab.Rule(0.5, False)
        self.assertIsInstance(rule, float)
        self.assertEqual(rule, 0.5)
        self.assertEqual(f"{rule:.4f}", "0.5000")
        self.assertLess(rule, 0.6)
        self.assertEqual(rule * 2.0, 1.0)

    def test_a_plain_number_eliminates(self):
        # THE SAFE DIRECTION. A caller that drops the rule back to a float keeps
        # cutting the swarf away; it never quietly leaves a whisker behind.
        self.assertTrue(mintab.eliminating(0.5))
        self.assertTrue(mintab.Rule.of(0.5).eliminate)

    def test_the_switch_travels_with_the_number(self):
        self.assertFalse(mintab.Rule.of(0.5, False).eliminate)
        self.assertFalse(mintab.eliminating(mintab.Rule(0.5, False)))

    def test_an_explicit_override_beats_what_the_value_says(self):
        self.assertTrue(mintab.eliminating(mintab.Rule(0.5, False), True))
        self.assertFalse(mintab.eliminating(0.5, False))

    def test_the_checked_value_keeps_the_switch(self):
        rule = mintab.checked_min_tab(mintab.Rule(0.5, False))
        self.assertEqual(rule, 0.5)
        self.assertFalse(rule.eliminate)

    def test_the_floor_is_still_a_config_error(self):
        with self.assertRaises(ValueError):
            mintab.checked_min_tab(0.05)

    def test_it_says_what_it_is_when_it_is_printed(self):
        self.assertIn("not enforced", repr(mintab.Rule(0.5, False)))
        self.assertNotIn("not enforced", repr(mintab.Rule(0.5, True)))


# ---------------------------------------------------------------------------
# the ribbons, as things rather than as passes
# ---------------------------------------------------------------------------


def facing_runs(material_mm: float, length_mm: float = 6.0,
                radius: float = 0.3):
    """Two straight cuts running alongside each other, leaving a ribbon."""
    gap = material_mm + 2.0 * radius
    return [
        [Line(p0=(0.0, 0.0), p1=(length_mm, 0.0), source="a")],
        [Line(p0=(0.0, gap), p1=(length_mm, gap), source="b")],
    ]


class SliverRibbonTest(unittest.TestCase):
    """A sliver is a ribbon, and it is reported as one."""

    def test_a_thin_ribbon_is_found_with_its_width_and_its_length(self):
        found = mintab.standing_slivers(facing_runs(0.35), 0.3, MIN_TAB)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].material_mm, 0.35, places=6)
        self.assertGreater(found[0].length_mm, 4.0)

    def test_material_over_the_minimum_tab_is_not_a_sliver(self):
        self.assertEqual(mintab.standing_slivers(facing_runs(0.8), 0.3, MIN_TAB),
                         ())

    def test_the_covering_chain_overlaps_along_the_whole_ribbon(self):
        made = mintab.covering_joins(facing_runs(0.35), 0.3, MIN_TAB)
        self.assertGreater(len(made), 1)
        middles = sorted(
            (join.at[0] + join.to[0]) / 2.0 for join in made
        )
        # Every neighbouring pair closer than one bit DIAMETER, so the swept
        # stadiums overlap and nothing is left standing between two crossings.
        for one, two in zip(middles, middles[1:]):
            self.assertLess(two - one, 2.0 * 0.3)

    def test_a_declared_gap_is_never_covered(self):
        # The two stop lines of a declared tab, spanning the whole run: every
        # chord across this gap crosses both, so the design owns all of it.
        guard = mintab.Guard(
            label="TAB1", kind="tab", at=(3.0, 0.5), material_mm=0.35,
            blades=(
                Line(p0=(-1.0, 0.3), p1=(7.0, 0.3), source="s0"),
                Line(p0=(-1.0, 0.65), p1=(7.0, 0.65), source="s1"),
            ),
        )
        self.assertEqual(
            mintab.covering_joins(facing_runs(0.35), 0.3, MIN_TAB,
                                  guards=(guard,)),
            (),
        )

    def test_the_warning_names_the_size_and_the_place(self):
        ribbon = mintab.standing_slivers(facing_runs(0.35), 0.3, MIN_TAB)[0]
        said = mintab.sliver_warning(ribbon, MIN_TAB)
        self.assertIn("0.3500 mm", said)
        self.assertIn(f"{ribbon.middle[0]:.4f}", said)
        self.assertIn("eliminate_slivers is off", said)

    def test_one_ribbon_found_twice_is_reported_once(self):
        ribbons = mintab.sliver_ribbons(
            mintab.cuts_of([(run, 0.3, False) for run in facing_runs(0.35)]),
            MIN_TAB,
        )
        self.assertGreaterEqual(len(ribbons), 1)
        self.assertEqual(len(mintab.distinct_slivers(ribbons + ribbons)),
                         len(mintab.distinct_slivers(ribbons)))


class RunEndsSwitchTest(unittest.TestCase):
    """The other half of the board rule: two run ends, joined or not."""

    def ends(self, consume):
        runs = [
            [Line(p0=(0.0, 0.0), p1=(5.0, 0.0), source="a")],
            [Line(p0=(5.3, 0.0), p1=(10.0, 0.0), source="b")],
        ]
        return mintab.join_run_ends(runs, 0.1, MIN_TAB, consume=consume)

    def test_consuming_makes_the_two_runs_one(self):
        made = self.ends(True)
        self.assertEqual(len(made.joins), 1)
        self.assertEqual(len(made.runs), 1)

    def test_not_consuming_finds_the_same_sliver_and_cuts_nothing(self):
        made = self.ends(False)
        self.assertEqual(len(made.joins), 1)
        self.assertEqual(len(made.runs), 2)
        self.assertAlmostEqual(made.joins[0].material_mm, 0.1, places=6)


# ---------------------------------------------------------------------------
# the panel: the passes land on the layer, and the outline comes out of it
# ---------------------------------------------------------------------------


def edge(x0, y0, x1, y1, layer="Edge.Cuts", width=0.05, uid=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer, stroke=Stroke(width=width),
        id=uid or f"{layer}-{x0}-{y0}-{x1}-{y1}",
    )


def webbed_panel(material_mm: float = 0.3) -> Board:
    """Two instances whose channels leave a web of *material_mm* between them.

    A sliver that exists in NO board: it is between two copies, so it is an
    accident of the arrangement and the panel is the only thing that can cut it.
    Each instance is a 10 mm square with a channel round three of its sides, and
    the second one is placed so that the two facing channel walls leave exactly
    the web asked for.
    """
    board = Board(layers=[
        BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
        BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user"),
    ])
    pitch = 10.0 + 4.0 * RADIUS + material_mm
    for row in range(2):
        base = pitch * row
        corners = [(0.0, base), (10.0, base), (10.0, base + 10.0),
                   (0.0, base + 10.0)]
        for index in range(4):
            board.drawings.append(edge(
                *corners[index], *corners[(index + 1) % 4],
                uid=f"i{row}-edge-{index}",
            ))
        cut = [(-RADIUS, base - RADIUS), (10.0 + RADIUS, base - RADIUS),
               (10.0 + RADIUS, base + 10.0 + RADIUS),
               (-RADIUS, base + 10.0 + RADIUS)]
        for index in range(3):
            board.drawings.append(edge(
                *cut[index], *cut[index + 1], layer="User.36", width=WIDTH,
                uid=f"c{row}-path-{index}",
            ))
    return board


def toolpath_items(board: Board) -> list:
    return panelize.board_items_on(board, panelize.TOOLPATH_LAYER)


class PanelInkTest(unittest.TestCase):
    """What the panel adds to its own toolpath layer, and what it says."""

    def test_the_web_between_two_instances_is_covered(self):
        board = webbed_panel()
        before = len(toolpath_items(board))
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        self.assertTrue(made.added_channels)
        self.assertEqual(len(made.added_items), len(made.added_channels))
        self.assertEqual(len(toolpath_items(board)),
                         before + len(made.added_items))

    def test_every_added_item_is_drawn_at_its_own_cutter_width(self):
        board = webbed_panel()
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        for channel, item in zip(made.added_channels, made.added_items):
            self.assertEqual(item.layer, panelize.TOOLPATH_LAYER)
            self.assertAlmostEqual(item.stroke.width, channel.radius * 2.0)

    def test_an_added_item_names_itself_from_its_geometry(self):
        one = panelize.apply(webbed_panel(), 5.0, min_tab_mm=MIN_TAB)
        two = panelize.apply(webbed_panel(), 5.0, min_tab_mm=MIN_TAB)
        self.assertEqual([item.id for item in one.added_items],
                         [item.id for item in two.added_items])
        self.assertTrue(all(item.id for item in one.added_items))

    def test_a_panel_with_nothing_to_cover_adds_nothing_at_all(self):
        # THE BYTE STATEMENT: a panel with no sliver in it writes exactly the
        # items it wrote before any of this existed.
        board = webbed_panel(material_mm=4.0)
        before = [item.id for item in toolpath_items(board)]
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        self.assertEqual(made.added_channels, ())
        self.assertEqual([item.id for item in toolpath_items(board)], before)

    def test_the_switch_off_leaves_the_web_and_names_it(self):
        board = webbed_panel()
        before = [item.id for item in toolpath_items(board)]
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB,
                              eliminate_slivers=False)
        self.assertEqual(made.added_channels, ())
        self.assertEqual([item.id for item in toolpath_items(board)], before)
        self.assertTrue(made.standing_slivers)
        self.assertTrue(any(
            "SLIVER LEFT STANDING" in note for note in made.warnings
        ))

    def test_the_switch_rides_on_the_minimum_tab_when_nobody_passes_it(self):
        # How the setting reaches this stage at all: on the rule, not through
        # every signature between the config and here.
        made = panelize.apply(webbed_panel(), 5.0,
                              min_tab_mm=mintab.Rule(MIN_TAB, False))
        self.assertFalse(made.eliminate_slivers)
        self.assertEqual(made.added_channels, ())


class OutlineComesOutOfTheInkTest(unittest.TestCase):
    """THE FLOW, as arithmetic: the outline is what the LAYER says it is.

    Nothing here asks the build what it did. The finished board is read back,
    the ink on `User.36` is dilated by half its own stroke, and the boundary of
    the blank minus that region is compared against the `Edge.Cuts` the build
    wrote. They are the same region or the flow is a fiction.
    """

    def rederived(self, board: Board, blank):
        warnings: list = []
        runs = panelize.channels_of(board, warnings)
        return channels.region_boundary(blank, list(runs.channels))

    def assert_same_outline(self, made, board):
        again = self.rederived(board, made.blank)
        self.assertEqual(again.loop_count, made.boundary.loop_count)
        self.assertAlmostEqual(again.length_mm, made.boundary.length_mm,
                               places=6)
        for one, two in zip(sorted(again.bbox), sorted(made.boundary.bbox)):
            self.assertAlmostEqual(one, two, places=6)

    def test_a_panel_that_covered_a_web_derives_the_same_outline_from_its_ink(self):
        board = webbed_panel()
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        self.assertTrue(made.added_channels)
        self.assert_same_outline(made, board)

    def test_a_panel_that_covered_nothing_derives_the_same_outline_too(self):
        board = webbed_panel(material_mm=4.0)
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        self.assert_same_outline(made, board)

    def test_the_switch_off_derives_the_same_outline_from_its_ink(self):
        board = webbed_panel()
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB,
                              eliminate_slivers=False)
        self.assert_same_outline(made, board)

    def test_the_defect_put_back_is_caught_by_the_same_arithmetic(self):
        """The defect put back: the passes shape the outline and never land.

        The result is the sliver's well cut in
        `Edge.Cuts` and the toolpath band stopping short of it - and the flow
        check has to be what sees it, because every number inside the build
        agrees with itself.
        """
        board = webbed_panel()
        original = panelize.to_toolpath_items
        panelize.to_toolpath_items = lambda added, layer=None: []
        try:
            made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        finally:
            panelize.to_toolpath_items = original
        self.assertTrue(made.added_channels)
        again = self.rederived(board, made.blank)
        self.assertNotAlmostEqual(again.length_mm, made.boundary.length_mm,
                                  places=3)

    def test_the_cut_channels_reported_are_what_the_layer_holds(self):
        board = webbed_panel()
        made = panelize.apply(board, 5.0, min_tab_mm=MIN_TAB)
        warnings: list = []
        on_layer = panelize.channels_of(board, warnings)
        self.assertAlmostEqual(
            sum(channel.length_mm for channel in on_layer.channels),
            sum(channel.length_mm for channel in made.cut_channels),
            places=6,
        )


class ChipRuleIsConditionalTest(unittest.TestCase):
    """A loose chip is a defect when the build promised to cut it away."""

    def chip_case(self):
        """A rectangle with a cut either side of a 0.3 mm strip of it.

        The strip is severed from the rest of the material at both ends, so it
        is a loose piece of swarf - which is a defect when the minimum tab said
        it would be routed off, and the warned-about consequence when it did not.
        """
        blank = channels.Rect(0.0, 0.0, 20.0, 20.0)
        radius = 0.3
        cuts = [
            channels.Channel(
                path=(Line(p0=(-1.0, y), p1=(21.0, y), source=f"c{y}"),),
                radius=radius, closed=False,
            )
            for y in (9.5, 10.2)
        ]
        return blank, cuts

    def test_the_chip_is_a_defect_when_slivers_are_eliminated(self):
        blank, cuts = self.chip_case()
        boundary = channels.region_boundary(blank, cuts)
        report = outline_check.check(
            blank, boundary.loops, cuts, region_step_mm=0.0,
            min_tab_mm=MIN_TAB, eliminate_slivers=True,
        )
        self.assertTrue(report.of_kind("chip"))
        self.assertFalse(report.ok)

    def test_the_same_chip_is_an_advisory_when_they_are_not(self):
        blank, cuts = self.chip_case()
        boundary = channels.region_boundary(blank, cuts)
        report = outline_check.check(
            blank, boundary.loops, cuts, region_step_mm=0.0,
            min_tab_mm=MIN_TAB, eliminate_slivers=False,
        )
        self.assertFalse(report.of_kind("chip"))
        self.assertTrue(any(
            entry.kind == "chip" for entry in report.advisories
        ))
        self.assertTrue(report.ok)


# ---------------------------------------------------------------------------
# the config: one rule, and a change to it is a BOARD change
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class ConfigSwitchTest(unittest.TestCase):
    """The setting, as `panel.json` states it and as the stages receive it."""

    def loaded(self, **update):
        from eedesignpanels.config import load_config

        config = load_config(CORPUS_CONFIG)
        return config.model_copy(update=update) if update else config

    def test_the_default_is_what_this_project_has_always_done(self):
        config = self.loaded()
        self.assertTrue(config.eliminate_slivers)
        self.assertTrue(config.min_tab_mm.eliminate)

    def test_the_switch_binds_to_the_width_so_the_stages_receive_one_rule(self):
        config = self.loaded(eliminate_slivers=False)
        self.assertFalse(config.min_tab_mm.eliminate)
        self.assertEqual(float(config.min_tab_mm), float(self.loaded().min_tab_mm))

    def test_a_copy_never_carries_a_stale_switch(self):
        # The one hazard a flag riding on a number invites, closed by name.
        config = self.loaded().model_copy(update={"eliminate_slivers": False})
        self.assertFalse(config.min_tab_mm.eliminate)
        self.assertTrue(
            config.model_copy(update={"eliminate_slivers": True})
            .min_tab_mm.eliminate
        )

    def test_the_file_still_serializes_as_a_plain_number(self):
        data = self.loaded(eliminate_slivers=False).model_dump(mode="json")
        self.assertIsInstance(json.loads(json.dumps(data))["min_tab_mm"], float)
        self.assertIs(data["eliminate_slivers"], False)

    def test_the_stepped_panel_cache_sees_it(self):
        # A change to it is a BOARD change, so a kept stepped panel may not
        # survive it. The fingerprint excludes only the carrier subtrees.
        from eedesignpanels.preview import panelbuild

        self.assertNotEqual(
            panelbuild.layout_fingerprint(self.loaded()),
            panelbuild.layout_fingerprint(self.loaded(eliminate_slivers=False)),
        )

    def test_the_page_sends_it_as_a_prep_change(self):
        from eedesignpanels.preview import actions

        self.assertIn("eliminate_slivers", actions.PREP_PARAMS)
        self.assertIn("eliminate_slivers", actions.BOOLEAN_PARAMS)
        self.assertIn("eliminate_slivers", actions.PREP_SECTIONS)
        self.assertEqual(actions.SETTABLE["eliminate_slivers"],
                         ("eliminate_slivers",))

    def test_the_page_reports_it_beside_the_declared_numbers(self):
        from eedesignpanels.preview import page_served

        params = page_served.params_json(self.loaded(eliminate_slivers=False))
        self.assertIs(params["eliminate_slivers"], False)
        self.assertIsInstance(params["min_tab_mm"], float)
        self.assertNotIsInstance(params["min_tab_mm"], mintab.Rule)


class PrepPageControlTest(unittest.TestCase):
    """The control on step 1, read off the bytes the page is built from."""

    @classmethod
    def setUpClass(cls):
        from eedesignpanels.preview import page_served

        cls.page_served = page_served
        cls.markup = page_served._controls_html(1, "stub", "User.38")

    def test_it_wears_the_field_idioms_every_control_wears(self):
        # Like every control: bold title, the control, one helper line - and
        # the helper is the tooltip too.
        self.assertIn("<span class=t>Eliminate slivers</span>", self.markup)
        self.assertIn("<input type=checkbox id=pb-elim data-act=param",
                      self.markup)
        self.assertIn(
            "<span class=h>Route the thin material away. Off leaves it and "
            "warns.</span>",
            self.markup,
        )

    def test_it_sits_with_the_declared_numbers_and_not_in_its_own_group(self):
        numbers = self.markup.index("pb-sec-router")
        candidates = self.markup.index("pb-sec-candidates")
        self.assertLess(numbers, self.markup.index("pb-elim"))
        self.assertLess(self.markup.index("pb-elim"), candidates)

    def test_no_bare_layer_number_reaches_the_page(self):
        # The standing naming fence: a layer is named by what the board calls
        # it, never by its ordinal.
        self.assertNotIn("User.36", self.markup)

    def test_the_script_applies_it_on_the_click(self):
        # Every setting on this page is wired through the shared `bindField`.
        # A switch's own `change` listener lives inside that one function,
        # not spelled out per field.
        script = self.page_served.PREP_JS
        self.assertIn("const elimEl = document.getElementById('pb-elim');",
                      script)
        self.assertIn(
            "[cutterEl, pastEl, alongEl, tangentEl, scoreWidthEl, minTabEl, healEl, elimEl, "
            "pocketEl].forEach(",
            script,
        )
        self.assertIn("el => bindField(el, applyParams)", script)
        self.assertIn("body.eliminate_slivers = elimEl.checked;", script)


# ---------------------------------------------------------------------------
# the corpus: the reference board, at the 0.6 mm-class cutter
# ---------------------------------------------------------------------------


def corpus_run(cutter_mm: float, eliminate: bool = True, gap_mm=None):
    """Prep and step the corpus board, in memory. `(config, prep, stepped)`."""
    from eedesignpanels import prep
    from eedesignpanels.config import load_config
    from eedesignpanels.stages import step_and_repeat

    config = load_config(CORPUS_CONFIG).model_copy(
        update={"eliminate_slivers": eliminate}
    )
    config = config.model_copy(update={
        "prep": config.prep.model_copy(update={"cutter_diameter": cutter_mm}),
    })
    if gap_mm is not None:
        # ALL THREE, because the corpus config now names per-axis gaps and they
        # WIN over `gap_mm` (the placement suite). Setting the one
        # number left the panel at its config gap, so the test that asks for a
        # narrow gap to make a web silently got no web at all.
        config = config.model_copy(update={
            "grid": config.grid.model_copy(update={
                "gap_mm": gap_mm, "gap_x_mm": gap_mm, "gap_y_mm": gap_mm,
            }),
        })
    computed = prep.compute(config)
    return config, computed, step_and_repeat.stepped_from(config, computed.board)


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusFlowTest(unittest.TestCase):
    """The corpus board: where each kind of covering pass ends up."""

    @classmethod
    def setUpClass(cls):
        cls.on = corpus_run(SNAPSHOT_CUTTER_MM, eliminate=True)
        cls.off = corpus_run(SNAPSHOT_CUTTER_MM, eliminate=False)

    def test_a_boards_own_sliver_is_cut_at_prep_time_and_lands_in_the_board(self):
        _config, computed, _built = self.on
        self.assertTrue(computed.result.covering)
        crossings = [
            item for item in toolpath_items(computed.board)
            if abs(item.stroke.width - SNAPSHOT_CUTTER_MM) < 1e-9
        ]
        # Every covering pass is on the layer, at the cutter's own width.
        self.assertEqual(
            len(toolpath_items(computed.board)),
            len(crossings),
        )
        self.assertEqual(len(computed.result.warnings),
                         len([w for w in computed.result.warnings]))

    def test_the_panel_adds_nothing_because_the_board_already_said_it_all(self):
        # The slivers on this board are the BOARD's, so
        # by the time the panel looks there is nothing left for it to find.
        _config, _computed, built = self.on
        self.assertEqual(built.stepped.added_channels, ())

    def test_the_switch_off_cuts_nothing_anywhere_and_warns_per_sliver(self):
        _config, computed, built = self.off
        self.assertEqual(computed.result.covering, ())
        self.assertTrue(computed.result.standing)
        self.assertEqual(
            len([note for note in computed.result.warnings
                 if note.startswith("SLIVER LEFT STANDING")]),
            len(computed.result.standing),
        )
        self.assertEqual(built.stepped.added_channels, ())
        self.assertTrue(built.stepped.standing_slivers)

    def test_the_outline_closes_the_same_way_either_way(self):
        # 25 loops on this corpus board, with all six Edge.Cuts shapes read
        # and gaps healed, MEASURED here rather than assumed -
        # `test_the_panels_outline_is_the_outline_of_its_own_ink` below
        # cross-checks it against an independently recomputed boundary. What
        # this test protects: eliminating slivers does not change how the
        # outline closes.
        self.assertEqual(self.on[2].stepped.boundary.loop_count, 25)
        self.assertEqual(self.off[2].stepped.boundary.loop_count, 25)

    def test_the_panels_outline_is_the_outline_of_its_own_ink(self):
        for name, (_config, _computed, built) in (("on", self.on),
                                                  ("off", self.off)):
            with self.subTest(eliminate=name):
                made = built.stepped
                warnings: list = []
                runs = panelize.channels_of(built.board, warnings)
                again = channels.region_boundary(made.blank, list(runs.channels))
                self.assertEqual(again.loop_count, made.boundary.loop_count)
                self.assertAlmostEqual(again.length_mm, made.boundary.length_mm,
                                       places=6)

    def test_the_thin_material_left_standing_is_an_advisory_not_a_defect(self):
        made = self.off[2].stepped
        report = outline_check.check(
            made.blank, made.boundary.loops, made.cut_channels,
            min_tab_mm=made.min_tab_mm,
            guards=panelize.channel_guards(self.off[2].board),
            region_step_mm=0.0, eliminate_slivers=False,
        )
        self.assertTrue(report.ok, report.summary)
        self.assertTrue(report.advisories)


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusPanelLevelWebTest(unittest.TestCase):
    """A web BETWEEN two instances - the one sliver no board can carry."""

    @classmethod
    def setUpClass(cls):
        cls.built = corpus_run(0.889, gap_mm=WEBBED_GAP_MM)[2]

    def test_the_panel_cuts_it_itself_and_puts_the_pass_on_its_own_layer(self):
        made = self.built.stepped
        self.assertTrue(made.added_channels)
        self.assertEqual(len(made.added_items), len(made.added_channels))
        ink = panelize.board_items_on(self.built.board, panelize.TOOLPATH_LAYER)
        identities = {item.id for item in ink}
        for item in made.added_items:
            self.assertIn(item.id, identities)

    def test_the_outline_is_still_the_outline_of_the_layer(self):
        made = self.built.stepped
        warnings: list = []
        runs = panelize.channels_of(self.built.board, warnings)
        again = channels.region_boundary(made.blank, list(runs.channels))
        self.assertEqual(again.loop_count, made.boundary.loop_count)
        self.assertAlmostEqual(again.length_mm, made.boundary.length_mm,
                               places=6)


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusSavedBoardTest(unittest.TestCase):
    """The file on disk satisfies the invariant standalone, after a save."""

    def test_a_saved_board_carries_every_pass_the_router_makes(self):
        import tempfile

        _config, computed, _built = corpus_run(SNAPSHOT_CUTTER_MM)
        with tempfile.TemporaryDirectory(prefix="pb-saved-") as temporary:
            path = Path(temporary) / "board.kicad_pcb"
            computed.board.to_file(path)
            reread = Board.from_file(path)
        written = panelize.board_items_on(reread, panelize.TOOLPATH_LAYER)
        self.assertEqual(len(written), computed.result.written)
        # ...and the covering passes are among them, by geometry: each crossing
        # of the rule is a line the file now holds.
        lines = [
            (item.start, item.end) for item in written
            if type(item).__name__ == "PcbLine"
        ]
        for join in computed.result.covering:
            crossing = join.crossing
            self.assertTrue(
                any(
                    math.dist(start, crossing.p0) < 1e-6
                    and math.dist(end, crossing.p1) < 1e-6
                    for start, end in lines
                ),
                f"the covering pass at {join.at} is not in the saved file",
            )


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusDeterminismTest(unittest.TestCase):
    """Same board, same config, same bytes - with the covering passes in it."""

    def test_two_preps_of_a_slivered_board_write_the_same_file(self):
        import tempfile

        digests = []
        for _attempt in range(2):
            _config, computed, _built = corpus_run(SNAPSHOT_CUTTER_MM)
            with tempfile.TemporaryDirectory(prefix="pb-twice-") as temporary:
                path = Path(temporary) / "board.kicad_pcb"
                computed.board.to_file(path)
                digests.append(path.read_bytes())
        self.assertEqual(digests[0], digests[1])
        self.assertTrue(corpus_run(SNAPSHOT_CUTTER_MM)[1].result.covering)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
