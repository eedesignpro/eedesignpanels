"""The SOURCE board's copper, re-filled against the keepout a decision wrote.

A v-score accepted on the source board writes its rule area, and the copper is
re-filled, so the source board's zones are filled and can be reviewed before
they are frozen into polygons and moved to the panel.

Two kinds of test live here. The arithmetic and the refusals need nothing but
Python. The pull-back itself needs KiCad - only KiCad fills copper - so those
cases are skipped where there is no `kicad-cli`, exactly as the rail pour's are.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    PcbLine,
    Stroke,
    Zone,
    ZoneFill,
    ZoneOutlinePolygon,
    dump_board,
)

# The tag says a case starts a real KiCad so the fast runner can leave it
# out. It skips nothing and asserts nothing; the full run is unchanged. See
# tests/runner_tags.py.
from runner_tags import runs_real_kicad
from shared_setup import (
    board_fingerprint,
    shared_setup_must_survive_every_test,
)

from eedesignpanels.stages import board_copper, board_scores, refill
from eedesignpanels.stages.panel_scores import SCORE_LAYER

HAVE_KICAD = shutil.which("kicad-cli") is not None or (
    refill.DEFAULT_KICAD_CLI.is_file()
)

# The same blank the rail pour's fixture uses: 44 x 47.5 mm at (100, 100), three
# copper layers and nothing else. A board KiCad will load.
FIXTURE = Path(__file__).parent / "data" / "rail_fill_fixture.kicad_pcb"
POUR = (102.0, 102.0, 142.0, 145.5)
# Down the middle of the pour, from edge to edge of the board.
SCORE_X = 122.0
HALF_WIDTH = 0.3
# A real uuid, because KiCad mints a new one for anything that is not
# and the transplant matches zones back by uuid.
POUR_UUID = "00000000-0000-0000-0000-0000000000b1"
SCORE_UUID = "00000000-0000-0000-0000-0000000000b2"


def pour_zone(uid=POUR_UUID) -> Zone:
    """One copper zone over most of the fixture, keeping its islands.

    A zone with no net is all islands by KiCad's reckoning, so the fixture says
    "never remove" for the reason the rail pour does. A designer's own netted
    zone needs no such thing, and `take` does not ask for it - see
    :class:`IslandRuleTests`.
    """
    x0, y0, x1, y1 = POUR
    return Zone(
        net=0, net_name="", layers=["F.Cu"], id=uid, name="board_pour",
        hatch_style="edge", hatch_pitch=0.508, min_thickness=0.25,
        fill=ZoneFill(filled=True, thermal_gap=0.5,
                      thermal_bridge_width=0.5,
                      island_removal_mode=refill.ISLAND_REMOVAL_NEVER),
        polygons=[ZoneOutlinePolygon(
            pts=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        )],
    )


def score_line() -> PcbLine:
    return PcbLine(start=(SCORE_X, 100.0), end=(SCORE_X, 147.5),
                   layer=SCORE_LAYER, stroke=Stroke(width=0.1), id=SCORE_UUID)


def fixture_board(scored: bool) -> Board:
    board = Board.from_file(FIXTURE)
    board.zones.append(pour_zone())
    if scored:
        board.drawings.append(score_line())
    board_scores.sync(board, HALF_WIDTH)
    return board


def filled_points(board: Board) -> list:
    return [
        point
        for zone in board.zones if zone.name == "board_pour"
        for record in zone.filled_polygons
        for point in record.pts
        if isinstance(point, tuple)
    ]


class IslandRuleTests(unittest.TestCase):
    """A source board's island setting is the designer's, and is left alone."""

    def zone_that_removes_islands(self) -> Zone:
        zone = pour_zone()
        zone.fill.island_removal_mode = 0
        return zone

    def test_the_panel_still_refuses_a_zone_that_would_come_back_empty(self):
        with self.assertRaises(ValueError):
            refill.checked([self.zone_that_removes_islands()])

    def test_the_board_path_asks_no_such_thing(self):
        refill.checked([self.zone_that_removes_islands()],
                       require_island_keep=False)

    def test_a_zone_with_no_uuid_is_refused_either_way(self):
        zone = pour_zone(uid="")
        for require in (True, False):
            with self.subTest(require=require):
                with self.assertRaises(ValueError):
                    refill.checked([zone], require_island_keep=require)


class WhichZonesTests(unittest.TestCase):
    """What is handed to KiCad, and what is not."""

    def test_a_keepout_is_never_handed_to_the_filler(self):
        board = fixture_board(scored=True)
        self.assertEqual(len(board.zones), 2)
        self.assertEqual(
            [zone.name for zone in board_copper.copper_zones(board)],
            ["board_pour"],
        )

    def test_a_board_with_no_copper_zone_costs_no_process(self):
        board = Board.from_file(FIXTURE)
        self.assertIsNone(board_copper.take(board, cli_path=Path("C:/nowhere")))

    def test_applying_nothing_changes_nothing(self):
        board = fixture_board(scored=True)
        before = dump_board(board)
        fill = board_copper.apply(board, None)
        self.assertEqual(fill.zones, 0)
        self.assertEqual(dump_board(board), before)

    def test_a_copper_zone_inside_a_footprint_is_counted_and_named(self):
        board = fixture_board(scored=False)
        self.assertEqual(board_copper.footprint_copper_zones(board), [])
        said = board_copper.note_for(
            board_copper.BoardFill(zones=2, records=3, seconds=1.5,
                                   footprint_zones=1)
        )
        self.assertIn("not re-filled", said)


class ProjectSidecarTests(unittest.TestCase):
    """The net classes and the custom rules live beside the board, not in it."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.addCleanup(self._folder.cleanup)

    def test_the_project_and_the_rules_are_carried_in(self):
        real = self.folder / "design.kicad_pcb"
        real.write_text("(kicad_pcb)", encoding="utf-8")
        real.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
        real.with_suffix(".kicad_dru").write_text("(version 1)", encoding="utf-8")
        sandbox = self.folder / "box"
        sandbox.mkdir()
        carried = refill.carry_project(real, sandbox / "refill.kicad_pcb")
        self.assertEqual(carried, ("design.kicad_pro", "design.kicad_dru"))
        self.assertTrue((sandbox / "refill.kicad_pro").is_file())
        self.assertTrue((sandbox / "refill.kicad_dru").is_file())

    def test_a_project_that_is_not_there_is_not_an_error(self):
        real = self.folder / "design.kicad_pcb"
        real.write_text("(kicad_pcb)", encoding="utf-8")
        self.assertEqual(
            refill.carry_project(real, self.folder / "refill.kicad_pcb"), ()
        )

    def test_a_caller_with_no_board_path_carries_nothing(self):
        self.assertEqual(
            refill.carry_project(None, self.folder / "refill.kicad_pcb"), ()
        )


@unittest.skipUnless(HAVE_KICAD, "needs kicad-cli, which CI does not have")
@runs_real_kicad("fills copper with KiCad's own zone filler")
@shared_setup_must_survive_every_test(
    lambda cls: (board_fingerprint(cls.one_unscored_fill[0]),
                 board_fingerprint(cls.one_scored_fill[0]))
)
class RealBoardFillTests(unittest.TestCase):
    """KiCad's own filler, over a board small enough to fill in a moment."""

    @classmethod
    def setUpClass(cls):
        # To keep the suite fast, the three
        # tests that only MEASURE a finished fill share one fill per answer
        # instead of taking their own, at about 7 s of real zone filling each.
        # The three below this that need independent fills - the byte-equality
        # of two separate runs, the accept-decline round trip and the
        # transplant, which writes to its board - still call `filled` and get
        # their own. The fence on this class fingerprints both shared boards
        # after this runs and again after the last test, so a test that wrote
        # to one fails this class by name rather than the next one.
        cls.one_unscored_fill = cls.filled(scored=False)
        cls.one_scored_fill = cls.filled(scored=True)

    @classmethod
    def filled(cls, scored: bool):
        board = fixture_board(scored=scored)
        fill = board_copper.refill_board(board)
        return board, fill

    def test_an_unscored_board_fills_across_the_middle(self):
        board, fill = self.one_unscored_fill
        self.assertEqual(fill.zones, 1)
        self.assertGreaterEqual(fill.records, 1)
        xs = [point[0] for point in filled_points(board)]
        self.assertTrue(xs)
        self.assertLess(min(xs), SCORE_X)
        self.assertGreater(max(xs), SCORE_X)

    def test_a_scored_board_pulls_its_copper_off_the_line(self):
        board, _fill = self.one_scored_fill
        points = filled_points(board)
        self.assertTrue(points)
        closest = min(abs(point[0] - SCORE_X) for point in points)
        # Measured: the copper stops exactly on the edge of the
        # keepout, 0.3 mm from the line - KiCad adds no clearance of its own
        # round a rule area. The tolerance is float noise, not slack.
        self.assertGreaterEqual(closest, HALF_WIDTH - 1e-6)

    def test_the_score_splits_the_pour_in_two(self):
        unscored, _ = self.one_unscored_fill
        scored, _ = self.one_scored_fill
        self.assertLess(
            len(unscored.zones[0].filled_polygons),
            len(scored.zones[0].filled_polygons),
        )

    def test_two_fills_of_one_board_agree_to_the_byte(self):
        self.assertEqual(dump_board(self.filled(scored=True)[0]),
                         dump_board(self.filled(scored=True)[0]))

    def test_accepting_and_declining_returns_the_board_it_started_from(self):
        # The accept-decline round trip, through the real filler: a board
        # filled, scored and filled again, then unscored and filled again, is
        # the board it was - to the byte.
        board = fixture_board(scored=False)
        board_copper.refill_board(board)
        original = dump_board(board)

        board.drawings.append(score_line())
        board_scores.sync(board, HALF_WIDTH)
        board_copper.refill_board(board)
        self.assertNotEqual(dump_board(board), original)

        board.drawings = [
            item for item in board.drawings
            if SCORE_LAYER not in getattr(item, "layer", "")
        ]
        board_scores.sync(board, HALF_WIDTH)
        board_copper.refill_board(board)
        self.assertEqual(dump_board(board), original)

    def test_the_transplant_touches_nothing_but_the_zones(self):
        board = fixture_board(scored=True)
        taken = board_copper.take(board)
        before = dump_board(_without_pour(board))
        board_copper.apply(board, taken)
        self.assertEqual(dump_board(_without_pour(board)), before)


def _without_pour(board: Board) -> Board:
    """A shallow view of *board* with its copper zone left out, for the gate."""
    view = board.model_copy()
    targets = {id(zone) for zone in board_copper.copper_zones(board)}
    view.zones = [zone for zone in board.zones if id(zone) not in targets]
    return view


if __name__ == "__main__":
    unittest.main()
