"""THE FENCE ROUND THE WHOLE OUTLINE DEFECT CLASS.

Several outline defects were symptoms of one mechanism: the minimum-tab
check decided where material was too thin by measuring ONE POINT between two
cutter paths, and cut there. A sliver is a ribbon, and material can pinch to
nothing in places no measurement between two centrelines will ever look at.

What is fenced here is not the three symptoms. It is the property they all
violated, checked by two oracles that do not share a line of reasoning with the
code that produces the answer:

* :mod:`eedesignpanels.outline_check` works from the DEFINITION of the region -
  distance to the toolpath, ray casting across the finished loops - and says
  whether the outline is a boundary at all.
* :mod:`eedesignpanels.verify.outline_raster` DRAWS both answers and subtracts
  them, with KiCad's own plot of the toolpath as the witness that the two
  renderers agree before anything is believed.

And every one of the four known defects is put BACK here, one at a time, so the
fence is shown catching each of them rather than asserted to.

The synthetic cases run everywhere. The corpus cases need the corpus board on
the machine, and the raster ones need kicad-cli and the rasterizer as well.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

# The tag says a case starts a real KiCad so the fast runner can leave it
# out. It skips nothing and asserts nothing; the full run is unchanged. See
# tests/runner_tags.py.
from runner_tags import runs_real_kicad

from eedesignpanels import channels, mintab, outline_check
from eedesignpanels.channels import Channel, Rect, chain_runs
from eedesignpanels.toolpath import Line

BLANK = Rect(0.0, 0.0, 40.0, 30.0)

# THE CORPUS IS PRIVATE AND OPTIONAL. It is a real project folder that holds
# `panel/panel.json`, named only by the EEDESIGNPANELS_CORPUS variable. With the
# variable unset, or naming a folder that is not there, these cases skip.
_CORPUS_SETTING = os.environ.get("EEDESIGNPANELS_CORPUS", "").strip()
CORPUS = Path(_CORPUS_SETTING) if _CORPUS_SETTING else None
CORPUS_CONFIG = CORPUS / "panel" / "panel.json" if CORPUS else None
HAVE_CORPUS = CORPUS_CONFIG is not None and CORPUS_CONFIG.is_file()
NO_CORPUS = (f"no corpus at {CORPUS}" if CORPUS
             else "EEDESIGNPANELS_CORPUS is not set")

# The cutters in everyday use, and the sizes the sweep found the mechanism at.
EVERYDAY_CUTTERS = (0.56, 0.6, 0.8, 0.8923)
MECHANISM_CUTTERS = (0.4, 0.44, 0.54, 0.6, 0.68, 0.72, 0.76, 0.889)


def line(x0, y0, x1, y1, source="cut") -> Line:
    return Line(p0=(x0, y0), p1=(x1, y1), source=source)


def run_of(points, radius) -> Channel:
    """One welded run through *points*, at *radius*."""
    return chain_runs([
        (line(points[index][0], points[index][1],
              points[index + 1][0], points[index + 1][1], f"p{index}"), radius)
        for index in range(len(points) - 1)
    ]).channels[0]


def assert_outline_valid(case, blank, boundary, cut_channels, min_tab_mm=0.0,
                         guards=(), region_step_mm=0.25):
    """The analytic oracle as an assertion. Every panel a test builds runs it."""
    report = outline_check.check(
        blank, boundary.loops, cut_channels, min_tab_mm=min_tab_mm,
        guards=guards, region_step_mm=region_step_mm,
    )
    case.assertTrue(
        report.ok,
        report.summary + "\n" + "\n".join(
            str(defect) for defect in report.defects[:8]
        ),
    )
    return report


# ---------------------------------------------------------------------------
# the mechanism, on geometry built to show it
# ---------------------------------------------------------------------------


class SliverIsARibbonTest(unittest.TestCase):
    """A hairpin whose two legs leave a sliver for their whole length.

    THE DEFECT, in miniature. The legs are 0.9 mm apart centre to centre and the
    bit is 0.6 mm, so 0.3 mm of material stands between them - under the
    minimum tab - for the whole 6 mm they run alongside each other. One crossing
    in the middle severs it there and leaves the rest: a loose chip at the
    closed end and a knife edge at the open one.
    """

    RADIUS = 0.3
    MIN_TAB = 0.5

    def hairpin(self):
        return run_of([
            (5.0, 2.0), (5.0, 14.0), (5.9, 14.0), (5.9, 2.0),
        ], self.RADIUS)

    def test_the_geometry_leaves_the_sliver_the_test_claims(self):
        cuts = mintab.cuts_of([(self.hairpin().path, self.RADIUS, False)])
        seeds = mintab.approaches(cuts, self.MIN_TAB)
        self.assertTrue(seeds)
        self.assertAlmostEqual(
            min(seed.material_mm for seed in seeds), 0.3, places=6
        )

    def test_the_ribbon_is_covered_end_to_end(self):
        found = channels.close_slivers([self.hairpin()], self.MIN_TAB)
        along = sorted(join.at[1] for join in found.joins)
        self.assertGreaterEqual(len(found.joins), 6)
        self.assertLessEqual(along[0], 2.0 + 2.0 * self.RADIUS)
        self.assertGreaterEqual(along[-1], 14.0 - 2.0 * self.RADIUS)
        for before, after in zip(along, along[1:]):
            self.assertLessEqual(after - before, 2.0 * self.RADIUS)

    def test_the_outline_that_comes_out_is_valid_and_has_no_chip(self):
        found = channels.close_slivers([self.hairpin()], self.MIN_TAB)
        boundary = channels.region_boundary(BLANK, list(found.channels))
        assert_outline_valid(self, BLANK, boundary, found.channels,
                             min_tab_mm=self.MIN_TAB)

    def test_the_point_shaped_rule_leaves_a_chip_and_the_oracle_says_so(self):
        """DEFECT ONE PUT BACK: one crossing at the narrowest point."""
        found = self._point_shaped()
        boundary = channels.region_boundary(BLANK, list(found))
        report = outline_check.check(
            BLANK, boundary.loops, found, min_tab_mm=self.MIN_TAB
        )
        self.assertFalse(report.ok)
        self.assertIn("chip", report.kinds)

    def _point_shaped(self):
        """`close_slivers` as it stood before the fix: one crossing per seed."""
        hairpin = self.hairpin()
        cuts = mintab.cuts_of([(hairpin.path, self.RADIUS, False)])
        extra = []
        for seed in mintab.approaches(cuts, self.MIN_TAB):
            join = mintab.Join(
                at=seed.at, to=seed.to, gap_mm=seed.gap_mm,
                material_mm=seed.material_mm,
                radius_mm=max(cuts[seed.first].radius, cuts[seed.second].radius),
                first_radius_mm=cuts[seed.first].radius,
                second_radius_mm=cuts[seed.second].radius,
            )
            extra.append(Channel(path=(join.crossing,),
                                 radius=join.radius_mm, closed=False))
        return (hairpin, *extra)


class PinchIsCutTest(unittest.TestCase):
    """Two channels whose end caps touch: material of zero width.

    DEFECT TWO. No measurement between two centrelines finds this - the two
    centrelines are a whole cutter diameter apart and the material between them
    is a single point - so the boolean used to be handed a node where four
    pieces of edge meet and refuse. Now it reports the pinch and the rule cuts
    it.
    """

    RADIUS = 0.5

    def pair(self, gap=1.0):
        return [
            Channel(path=(line(5.0, 15.0, 15.0, 15.0),), radius=self.RADIUS,
                    closed=False),
            Channel(path=(line(15.0 + gap, 15.0, 30.0, 15.0),),
                    radius=self.RADIUS, closed=False),
        ]

    def test_the_bare_boolean_reports_the_pinch_and_where_it_is(self):
        with self.assertRaises(channels.PinchError) as caught:
            channels.region_boundary(BLANK, self.pair())
        self.assertEqual(len(caught.exception.points), 1)
        self.assertAlmostEqual(caught.exception.points[0][0], 15.5, places=3)

    def test_the_whole_rule_cuts_it_and_the_panel_builds(self):
        derived = channels.derived_boundary(BLANK, self.pair(), 0.5)
        self.assertEqual(derived.boundary.loop_count, 2)
        assert_outline_valid(self, BLANK, derived.boundary, derived.channels,
                             min_tab_mm=0.5)

    def test_a_pinch_the_minimum_tab_cannot_see_is_cut_by_the_pinch_pass(self):
        """A tangency with the minimum tab switched off is still severed."""
        derived = channels.derived_boundary(BLANK, self.pair(), 0.1)
        self.assertEqual(derived.boundary.loop_count, 2)

    def test_the_control_stays_two_holes(self):
        derived = channels.derived_boundary(BLANK, self.pair(gap=2.0), 0.5)
        self.assertEqual(derived.boundary.loop_count, 3)
        self.assertEqual(derived.pinches, ())

    def test_the_same_arrangement_resolves_the_same_way_twice(self):
        first = channels.derived_boundary(BLANK, self.pair(), 0.5)
        second = channels.derived_boundary(BLANK, self.pair(), 0.5)
        self.assertEqual(first.boundary.loops, second.boundary.loops)
        self.assertEqual(first.pinches, second.pinches)

    def test_an_arrangement_that_will_not_converge_says_so(self):
        class NeverSettles:
            points = ((1.0, 1.0),)

        def always_pinches(*_args, **_kwargs):
            raise channels.PinchError("stuck", (1.0, 1.0), NeverSettles.points)

        original = channels.region_boundary
        channels.region_boundary = always_pinches
        try:
            with self.assertRaises(channels.ChannelError) as caught:
                channels.derived_boundary(BLANK, self.pair(), 0.5)
        finally:
            channels.region_boundary = original
        self.assertIn("not converging", str(caught.exception))


class PinchCutTest(unittest.TestCase):
    """The cut a pinch gets: across it, at the bit that made the wall."""

    def test_it_is_centred_on_the_pinch_and_made_by_the_nearest_bit(self):
        cuts = [Channel(path=(line(5.0, 15.0, 15.0, 15.0),), radius=0.5,
                        closed=False)]
        made = channels._pinch_cuts([(15.6, 15.0)], cuts)
        self.assertEqual(len(made), 1)
        point, cut = made[0]
        self.assertEqual(point, (15.6, 15.0))
        self.assertAlmostEqual(cut.radius, 0.5)
        piece = cut.path[0]
        self.assertAlmostEqual(
            (piece.p0[0] + piece.p1[0]) / 2.0, 15.6, places=9
        )
        self.assertAlmostEqual(piece.length, 0.5, places=9)

    def test_two_pinches_come_out_in_a_fixed_order(self):
        cuts = [Channel(path=(line(5.0, 15.0, 25.0, 15.0),), radius=0.5,
                        closed=False)]
        one = channels._pinch_cuts([(20.0, 16.0), (10.0, 16.0)], cuts)
        two = channels._pinch_cuts([(10.0, 16.0), (20.0, 16.0)], cuts)
        self.assertEqual([point for point, _ in one],
                         [point for point, _ in two])


# ---------------------------------------------------------------------------
# the corpus: every cutter size the mechanism was found at
# ---------------------------------------------------------------------------


def corpus_panel(diameter):
    """The corpus panel built at one cutter size, in memory."""
    from eedesignpanels import prep
    from eedesignpanels.config import load_config
    from eedesignpanels.stages import step_and_repeat

    config = load_config(CORPUS_CONFIG)
    config = config.model_copy(update={
        "prep": config.prep.model_copy(update={"cutter_diameter": diameter}),
    })
    computed = prep.compute(config)
    return config, step_and_repeat.stepped_from(config, computed.board)


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusSweepTest(unittest.TestCase):
    """The whole mechanism, on the corpus board.

    These are the cutter sizes where a one-point thinness measurement gives a
    wrong outline or none: 0.40 to 0.48 and 0.62 to 0.72 refused to close, 0.54
    to 0.60 and 0.66 to 0.76 left twelve loose chips apiece.
    """

    def check(self, diameter, region_step_mm=0.25):
        from eedesignpanels.stages import panelize

        _config, built = corpus_panel(diameter)
        made = built.stepped
        report = assert_outline_valid(
            self, made.blank, made.boundary, made.cut_channels,
            min_tab_mm=made.min_tab_mm,
            guards=panelize.channel_guards(built.board),
            region_step_mm=region_step_mm,
        )
        # The corpus panel is six boards in a frame: the frame, and four holes
        # per board plus one per gap. Whatever the bit, it is the same count -
        # a different count is a chip or a lost loop.
        #
        # The count is 25 with every Edge.Cuts shape KiCad accepts and 0.01 mm
        # gap healing, the same at every cutter checked here - measured at
        # each one, not assumed from the first.
        self.assertEqual(made.boundary.loop_count, 25, f"at {diameter} mm")
        return report

    def test_every_cutter_the_mechanism_was_found_at_builds_a_valid_outline(self):
        for diameter in MECHANISM_CUTTERS:
            with self.subTest(cutter_mm=diameter):
                # Half a millimetre of lattice here rather than a quarter: the
                # edge checks are the same either way and this is eight builds.
                self.check(diameter, region_step_mm=0.5)

    def test_the_everyday_cutters_build_a_valid_outline(self):
        for diameter in EVERYDAY_CUTTERS:
            with self.subTest(cutter_mm=diameter):
                self.check(diameter)

    def test_the_corpus_cutter_needs_no_crossing_and_no_pinch(self):
        """The panel the config actually builds is untouched by all of this."""
        _config, built = corpus_panel(0.889)
        self.assertEqual(built.stepped.joins, ())
        self.assertEqual(built.stepped.pinches, ())


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
class CorpusDefectsPutBackTest(unittest.TestCase):
    """Each defect returned to the corpus, and the oracle's verdict on it."""

    def test_the_point_shaped_crossing_leaves_twelve_chips(self):
        from eedesignpanels.stages import panelize

        def point_shaped(cuts, min_tab_mm, guards=()):
            return tuple(
                mintab.Join(
                    at=seed.at, to=seed.to, gap_mm=seed.gap_mm,
                    material_mm=seed.material_mm,
                    radius_mm=max(cuts[seed.first].radius,
                                  cuts[seed.second].radius),
                    first_radius_mm=cuts[seed.first].radius,
                    second_radius_mm=cuts[seed.second].radius,
                )
                for seed in mintab.approaches(cuts, min_tab_mm, guards=guards)
            )

        original = channels.mintab.sliver_joins
        channels.mintab.sliver_joins = point_shaped
        try:
            _config, built = corpus_panel(0.6)
        finally:
            channels.mintab.sliver_joins = original
        made = built.stepped
        report = outline_check.check(
            made.blank, made.boundary.loops, made.cut_channels,
            min_tab_mm=made.min_tab_mm,
            guards=panelize.channel_guards(built.board),
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.of_kind("chip")), 12)

    def test_the_pinch_pass_removed_refuses_to_build(self):
        def bare(blank, channel_list, min_tab_mm, guards=(), eliminate=None):
            enforced = channels.close_slivers(
                list(channel_list), min_tab_mm, guards=guards,
                eliminate=eliminate)
            return channels.Derived(
                boundary=channels.region_boundary(blank, enforced.channels),
                channels=enforced.channels, joins=enforced.joins,
                protected=enforced.protected, pinches=(), passes=1,
                added=enforced.added, standing=enforced.standing,
            )

        original = channels.derived_boundary
        channels.derived_boundary = bare
        try:
            with self.assertRaises(channels.PinchError):
                corpus_panel(0.4)
        finally:
            channels.derived_boundary = original


# ---------------------------------------------------------------------------
# the raster oracle - slower, and it needs KiCad on the machine
# ---------------------------------------------------------------------------


def _raster_available() -> bool:
    try:
        from eedesignpanels.verify import outline_raster
    except ImportError:
        return False
    return Path(outline_raster.DEFAULT_KICAD_CLI).is_file()


RASTER = _raster_available()


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
@unittest.skipUnless(RASTER, "no kicad-cli and rasterizer to draw with")
@runs_real_kicad("plots a layer with kicad-cli to register the oracle against")
class RasterOracleTest(unittest.TestCase):
    """Both answers drawn and subtracted, at the cutters in everyday use.

    SLOW - about seven seconds a size, most of it rendering a thirty-megapixel
    panel twice. It is here because it is the only check that owes nothing at
    all to the geometry code: KiCad plots the toolpath, resvg strokes and fills,
    and the subtraction is arithmetic on pixels.
    """

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="panel-outline-oracle-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def judge(self, diameter, built, config, name):
        from eedesigncore import Board

        from eedesignpanels.stages import panelize, route_toolpath
        from eedesignpanels.verify import outline_raster

        made = built.stepped
        work = self.work / name
        work.mkdir(parents=True, exist_ok=True)
        board_path = work / "panel.kicad_pcb"
        built.board.to_file(board_path)
        source = Board.from_file(config.source_board_path)
        loops = [
            list(run.path) for run in chain_runs([
                (item, 0.0)
                for item in route_toolpath.outline_primitives(source)
            ]).channels
        ]
        warnings: list = []
        runs = panelize.channels_of(built.board, warnings)
        places = [
            (place.dx_mm, place.dy_mm) for place in built.geometry.placements
        ] if not built.geometry.turns else []
        return outline_raster.check(
            board_path, made.blank, made.boundary.loops,
            outline_check.sweeps_of(made.cut_channels),
            board_sweeps=outline_check.sweeps_of(runs.channels),
            joins=made.joins, min_tab_mm=made.min_tab_mm,
            instance_loops=loops if places else (), placements=places,
            out_dir=work,
        )

    def test_the_everyday_cutters_draw_the_same_panel_two_ways(self):
        for diameter in EVERYDAY_CUTTERS:
            with self.subTest(cutter_mm=diameter):
                config, built = corpus_panel(diameter)
                report = self.judge(diameter, built, config, f"d{diameter}")
                self.assertTrue(report.registration_ok, report.summary)
                self.assertEqual(report.eaten, (), report.summary)
                self.assertEqual(report.differences, (), report.summary)

    def test_kicads_plot_and_the_oracles_stroke_are_the_same_picture(self):
        """The witness: if these disagree, nothing else measured here means
        anything."""
        config, built = corpus_panel(0.6)
        report = self.judge(0.6, built, config, "registration")
        self.assertLess(report.registration_px, report.registration_budget_px)
        self.assertGreater(report.cut_px, 100000)

    def test_a_chamfered_corner_is_caught_as_a_bite_out_of_a_board(self):
        """DEFECT THREE PUT BACK: the pairwise claim judged at one point only.

        Before commit 418b21b the minimum-tab rule measured a claim at its
        narrowest point, and where a run turned a corner it claimed the board's
        own corner as a sliver and chamfered it off. Nothing about the outline
        it produced is invalid - the chamfer is a cut a cutter could make - so
        only an oracle that knows where the BOARDS are can see it.
        """
        def narrowest_point_only(at, to, first_radius, second_radius, cuts,
                                 index, measured=()):
            return mintab.standing_at(
                ((at[0] + to[0]) / 2.0, (at[1] + to[1]) / 2.0), cuts, index
            )

        original = mintab.sliver_stands
        mintab.sliver_stands = narrowest_point_only
        try:
            config, built = corpus_panel(0.2)
        finally:
            mintab.sliver_stands = original
        report = self.judge(0.2, built, config, "chamfer")
        self.assertTrue(report.eaten, report.summary)
        self.assertFalse(report.ok)
        self.assertIn("bite out of", report.summary)

    def test_the_same_cutter_with_the_rule_as_it_is_eats_no_board(self):
        config, built = corpus_panel(0.2)
        report = self.judge(0.2, built, config, "no_chamfer")
        self.assertEqual(report.eaten, (), report.summary)
        self.assertTrue(report.ok, report.summary)


# ---------------------------------------------------------------------------
# THE INVARIANT, asked of a FILE
# ---------------------------------------------------------------------------


def corpus_panel_at(diameter, eliminate=True, gap_mm=None):
    """The corpus panel at one cutter and one switch setting, in memory."""
    from eedesignpanels import prep
    from eedesignpanels.config import load_config
    from eedesignpanels.stages import step_and_repeat

    config = load_config(CORPUS_CONFIG).model_copy(
        update={"eliminate_slivers": eliminate}
    )
    config = config.model_copy(update={
        "prep": config.prep.model_copy(update={"cutter_diameter": diameter}),
    })
    if gap_mm is not None:
        # ALL THREE, because the corpus config names per-axis gaps and they
        # take precedence over `gap_mm`. Setting only the one number leaves the
        # panel at its config gap, so a test that asks for a narrow gap to
        # make a web would silently get no web at all.
        config = config.model_copy(update={
            "grid": config.grid.model_copy(update={
                "gap_mm": gap_mm, "gap_x_mm": gap_mm, "gap_y_mm": gap_mm,
            }),
        })
    computed = prep.compute(config)
    return config, step_and_repeat.stepped_from(config, computed.board)


@unittest.skipUnless(HAVE_CORPUS, NO_CORPUS)
@unittest.skipUnless(RASTER, "no kicad-cli and rasterizer to draw with")
@runs_real_kicad("plots a layer with kicad-cli to register the oracle against")
class InvariantOracleTest(unittest.TestCase):
    """EVERY ROUTED SECTION OF `Edge.Cuts` IS THE OUTLINE OF `User.36`'s INK.

    The invariant, drawn and subtracted, with nothing from the build in the answer:
    the cut region comes from the FILE's own toolpath layer
    (`outline_raster.ink_of`), so a board that passes this passes it standing
    alone. It runs in BOTH switch states, because leaving a sliver is a shape
    the outline has to follow just as honestly as cutting it away.
    """

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="panel-invariant-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def judge(self, built, config, name, with_boards=True):
        from eedesigncore import Board

        from eedesignpanels.stages import route_toolpath
        from eedesignpanels.verify import outline_raster

        made = built.stepped
        work = self.work / name
        work.mkdir(parents=True, exist_ok=True)
        board_path = work / "panel.kicad_pcb"
        built.board.to_file(board_path)
        source = Board.from_file(config.source_board_path)
        loops = [
            list(run.path) for run in chain_runs([
                (item, 0.0)
                for item in route_toolpath.outline_primitives(source)
            ]).channels
        ] if with_boards else []
        places = [
            (place.dx_mm, place.dy_mm) for place in built.geometry.placements
        ] if (with_boards and not built.geometry.turns) else []
        return outline_raster.invariant(
            board_path, made.blank, made.boundary.loops,
            instance_loops=loops if places else (), placements=places,
            min_tab_mm=float(made.min_tab_mm), out_dir=work,
        )

    def test_the_snapshot_cutter_holds_the_invariant_with_it_on(self):
        """0.6 mm: the cutter whose consumed sliver used to diverge.

        The well was cut in `Edge.Cuts` and the toolpath band stopped short of
        it, because the covering passes changed the outline without landing on
        the layer. The covering passes for this board are now made at prep time
        and are in the board file, so the stepped panel's outline and its ink
        are one region.
        """
        config, built = corpus_panel_at(0.6, eliminate=True)
        report = self.judge(built, config, "snapshot_on")
        self.assertTrue(report.registration_ok, report.summary)
        self.assertEqual(report.differences, (), report.summary)
        self.assertEqual(report.eaten, (), report.summary)

    def test_the_same_cutter_holds_it_with_the_slivers_left_standing(self):
        config, built = corpus_panel_at(0.6, eliminate=False)
        self.assertEqual(built.stepped.added_channels, ())
        self.assertTrue(built.stepped.standing_slivers)
        report = self.judge(built, config, "snapshot_off")
        self.assertTrue(report.registration_ok, report.summary)
        self.assertEqual(report.differences, (), report.summary)
        self.assertEqual(report.eaten, (), report.summary)

    def test_a_web_between_two_instances_is_cut_by_the_panel_and_lands(self):
        """The one sliver no board can carry, and the panel's own layer.

        A 1.25 mm gap at the corpus cutter leaves material between neighbouring
        copies. There is no board file that could hold those passes - they exist
        because of where the copies are - so they are the panel's, and the
        invariant is what says they reached its toolpath layer.
        """
        config, built = corpus_panel_at(0.889, gap_mm=1.25)
        self.assertTrue(built.stepped.added_channels)
        report = self.judge(built, config, "web_on", with_boards=False)
        self.assertTrue(report.registration_ok, report.summary)
        self.assertEqual(report.differences, (), report.summary)

    def test_the_old_defect_put_back_fails_the_invariant(self):
        """THE DEFECT, returned: a pass that shapes the outline and never lands.

        Every number inside the build agrees with itself; only a check that
        reads the LAYER can see it.
        """
        from eedesignpanels.stages import panelize

        original = panelize.to_toolpath_items
        panelize.to_toolpath_items = lambda added, layer=None: []
        try:
            config, built = corpus_panel_at(0.889, gap_mm=1.25)
        finally:
            panelize.to_toolpath_items = original
        self.assertTrue(built.stepped.added_channels)
        report = self.judge(built, config, "web_defect", with_boards=False)
        self.assertFalse(report.ok, report.summary)
        self.assertEqual(len(report.differences),
                         len(built.stepped.added_channels))


if __name__ == "__main__":
    unittest.main()
