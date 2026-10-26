"""Rail copper: the rectangles the pour covers, and the fill transplant.

The geometry runs anywhere. The FILL needs KiCad, because nothing in this
project computes a copper fill - so the transplant is checked
here against a stub filler that behaves the way `kicad-cli` does, and against
the real one only when this machine has it. What the stub cannot fake is the
gate, and that is the part worth pinning: the transplant must leave the panel
byte-identical everywhere except in the pours it was given.

The live preview fills pours behind the picture, which adds two more things.
The round trip and the transplant are SEPARATE CALLS, and each
half is pinned to what it may do: the round trip must leave the caller's board
untouched, and the transplant must be the only thing that changes it. And a
pour filled LATE - after the panel is finished - must give the same board as one
filled inside the build, because otherwise a saved panel and a CI runner's panel
would be two different files.
"""

import math
import shutil
import tempfile
import unittest
from pathlib import Path

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    Pad,
    PcbLine,
    Property,
    Stroke,
    Zone,
    ZoneFill,
    ZoneFilledPolygon,
    ZoneOutlinePolygon,
    dump_board,
)

# The tag marks a case that starts a real KiCad, so the fast runner can leave
# it out. It skips nothing and asserts nothing - the full run is unchanged.
# See tests/runner_tags.py.
from runner_tags import runs_real_kicad
from shared_setup import (
    board_fingerprint,
    shared_setup_must_survive_every_test,
)

from eedesignpanels.channels import Rect
from eedesignpanels.stages import panel_scores, rail_copper, refill
from eedesignpanels.stages.panel_scores import DEFAULT_RULE_HALF_WIDTH_MM
from eedesignpanels.stages.rail_copper import (
    KEEPOUT_NAME,
    POUR_NAME,
    RAIL_NAMESPACE,
    ZONE_NAME_PREFIX,
    RailZone,
    Settings,
    apply,
    carrier_rect,
    copper_layers,
    keepout_plan,
    outset_bbox,
    plan,
    rect_pts,
    zone_for,
)
from eedesignpanels.stages.refill import (
    ISLAND_REMOVAL_NEVER,
    RefillError,
    refill_zones,
)

BLANK = Rect(min_x=0.0, min_y=0.0, max_x=44.0, max_y=47.5)
GRID = (5.0, 5.0, 39.0, 42.5)
BOXES = (
    (5.0, 5.0, 20.5, 15.5),
    (23.5, 5.0, 39.0, 15.5),
    (5.0, 18.5, 20.5, 29.0),
    (23.5, 18.5, 39.0, 29.0),
    (5.0, 32.0, 20.5, 42.5),
    (23.5, 32.0, 39.0, 42.5),
)


def copper_board() -> Board:
    return Board(layers=[
        BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
        BoardLayer(ordinal=1, canonical_name="In1.Cu", layer_type="signal"),
        BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
        BoardLayer(ordinal=44, canonical_name="Edge.Cuts", layer_type="user"),
    ])


class SettingsTest(unittest.TestCase):
    def test_defaults_are_the_stated_ones(self):
        settings = Settings()
        self.assertEqual(settings.clearance_mm, 0.635)
        self.assertFalse(settings.pour_between_boards)

    def test_a_mapping_reads_as_settings(self):
        settings = Settings.of({"clearance_mm": 0.3,
                                "pour_between_boards": True})
        self.assertEqual(settings.clearance_mm, 0.3)
        self.assertTrue(settings.pour_between_boards)


class CarrierGeometryTest(unittest.TestCase):
    """ONE pour, the whole carrier blank.

    One zone and not one per rail, because KiCad does not handle overlapping zones of the same net
    well.
    """

    def test_the_pour_is_the_whole_blank(self):
        rect = carrier_rect(BLANK)
        self.assertEqual(rect.name, "carrier")
        self.assertEqual(rect.rect, BLANK.bbox)

    def test_the_pour_s_area_is_the_blank_s_area(self):
        self.assertAlmostEqual(
            carrier_rect(BLANK).area_mm2,
            BLANK.width_mm * BLANK.height_mm,
            places=9,
        )

    def test_plan_makes_one_strip_regardless_of_the_grid(self):
        self.assertEqual(len(plan(BLANK, GRID, BOXES, Settings())), 1)
        self.assertEqual(
            len(plan(BLANK, GRID, BOXES,
                     Settings(pour_between_boards=True))), 1
        )

    def test_a_degenerate_blank_makes_nothing(self):
        empty = Rect(min_x=0.0, min_y=0.0, max_x=0.0, max_y=0.0)
        self.assertEqual(plan(empty, GRID, BOXES, Settings()), [])


class KeepoutPlanTest(unittest.TestCase):
    """The rule areas that hold the one pour off the boards.

    No source outline and no placements are given here, so every plan falls
    back to rectangles (the pessimistic case) - the shaped case is
    exercised in `test_step_and_repeat`/`test_carrier_features` where a real
    source outline exists.
    """

    def test_pour_between_boards_off_makes_one_array_rectangle(self):
        plans = keepout_plan(GRID, BOXES, Settings(pour_between_boards=False))
        self.assertEqual(len(plans), 1)
        area, pts = plans[0]
        self.assertEqual(area.name, KEEPOUT_NAME)
        self.assertEqual(area.key, "array")
        self.assertFalse(area.shaped)
        self.assertEqual(area.bbox, outset_bbox(GRID, Settings().clearance_mm))
        self.assertEqual(pts, rect_pts(area.bbox))

    def test_pour_between_boards_on_makes_one_rectangle_per_board(self):
        warnings = []
        plans = keepout_plan(GRID, BOXES, Settings(pour_between_boards=True),
                             warnings=warnings)
        self.assertEqual(len(plans), len(BOXES))
        for area, _pts in plans:
            self.assertFalse(area.shaped)
        # No source outline was given, so the fallback warns about it.
        self.assertTrue(warnings)

    def test_a_degenerate_array_with_no_clearance_makes_nothing(self):
        # Zero width and no clearance to grow it: the outset rectangle stays
        # degenerate, below MIN_STRIP_MM.
        flat = (0.0, 0.0, 0.0, 5.0)
        self.assertEqual(
            keepout_plan(flat, (), Settings(clearance_mm=0.0,
                                            pour_between_boards=False)),
            [],
        )

    def test_one_board_still_gets_its_own_rectangle(self):
        plans = keepout_plan(GRID, [BOXES[0]],
                             Settings(pour_between_boards=True))
        self.assertEqual(len(plans), 1)


class ZoneTest(unittest.TestCase):
    """What a rail zone is: no net, every copper layer, and its islands kept."""

    def one(self, clearance=0.635):
        return zone_for(
            RailZone("top", (0.0, 0.0, 44.0, 5.0)),
            ("F.Cu", "In1.Cu", "B.Cu"), clearance,
        )

    def test_the_pour_belongs_to_nobody(self):
        zone = self.one()
        self.assertEqual(zone.net, 0)
        self.assertEqual(zone.net_name, "")

    def test_the_pour_covers_every_copper_layer(self):
        self.assertEqual(self.one().layers, ["F.Cu", "In1.Cu", "B.Cu"])

    def test_the_clearance_is_the_configured_one(self):
        self.assertEqual(self.one(0.3).connect_pads_clearance, 0.3)

    def test_the_outline_is_the_rectangle(self):
        self.assertEqual(
            self.one().polygons[0].pts,
            [(0.0, 0.0), (44.0, 0.0), (44.0, 5.0), (0.0, 5.0)],
        )

    def test_islands_are_never_removed(self):
        # A no-net pour is all islands. Under KiCad's default a single tooling
        # hole took the corpus top rail from a 43 mm pour to a 0.4 mm sliver.
        self.assertEqual(
            self.one().fill.island_removal_mode, ISLAND_REMOVAL_NEVER
        )

    def test_the_zone_is_named_for_the_part_of_the_panel_it_covers(self):
        self.assertEqual(self.one().name, f"{ZONE_NAME_PREFIX}_top")

    def test_the_carrier_pour_carries_the_pour_name(self):
        carrier = zone_for(RailZone("carrier", (0.0, 0.0, 44.0, 47.5)),
                           ("F.Cu",), 0.635)
        self.assertEqual(carrier.name, POUR_NAME)

    def test_the_uuid_is_derived_and_stable(self):
        self.assertEqual(self.one().id, self.one().id)
        self.assertEqual(self.one().id[14], "5")

    def test_moving_a_rail_gives_it_a_new_identity(self):
        moved = zone_for(RailZone("top", (0.0, 0.0, 44.0, 6.0)),
                         ("F.Cu",), 0.635)
        self.assertNotEqual(self.one().id, moved.id)

    def test_no_two_zones_on_one_panel_share_an_identity(self):
        # One pour plus one keep-out (pour_between_boards is off by default).
        board = copper_board()
        apply(board, BLANK, GRID, Settings(), instance_boxes=BOXES, fill=False)
        self.assertEqual(len({zone.id for zone in board.zones}), 2)


class ApplyTest(unittest.TestCase):
    """What the stage puts on a board when it is not asked to fill."""

    def test_the_zones_land_on_the_board(self):
        board = copper_board()
        made = apply(board, BLANK, GRID, Settings(),
                     instance_boxes=BOXES, fill=False)
        self.assertEqual(made.count, 1)
        self.assertEqual(made.keepout_count, 1)
        # The pour plus its one array keep-out.
        self.assertEqual(len(board.zones), 2)
        self.assertIsNone(made.refill)

    def test_the_reported_area_is_the_whole_carrier(self):
        # The pour's OWN rectangle is the blank, always - what the rule areas
        # hold it off is a separate thing.
        made = apply(copper_board(), BLANK, GRID, Settings(),
                     instance_boxes=BOXES, fill=False)
        self.assertAlmostEqual(made.area_mm2, 44.0 * 47.5, places=9)

    def test_a_board_with_no_copper_layer_says_so_and_makes_nothing(self):
        board = Board(layers=[
            BoardLayer(ordinal=44, canonical_name="Edge.Cuts",
                       layer_type="user"),
        ])
        warnings = []
        made = apply(board, BLANK, GRID, Settings(),
                     instance_boxes=BOXES, warnings=warnings, fill=False)
        self.assertEqual(made.count, 0)
        self.assertEqual(board.zones, [])
        self.assertIn("no copper layer", warnings[0])

    def test_a_degenerate_carrier_says_so_and_makes_nothing(self):
        empty = Rect(min_x=0.0, min_y=0.0, max_x=0.0, max_y=0.0)
        warnings = []
        made = apply(copper_board(), empty, GRID, Settings(),
                     instance_boxes=BOXES, warnings=warnings, fill=False)
        self.assertEqual(made.count, 0)
        self.assertIn("no area to pour into", warnings[0])

    def test_the_copper_layers_come_out_in_file_order(self):
        self.assertEqual(
            copper_layers(copper_board()), ("F.Cu", "In1.Cu", "B.Cu")
        )


def filled_board(source: Board, zone_ids, records=1) -> Board:
    """What a filler would hand back: the same board, with those zones filled."""
    board = source.model_copy(deep=True)
    for zone in board.zones:
        if zone.id in zone_ids:
            zone.filled_polygons = [
                ZoneFilledPolygon(layer=layer, pts=[(0.0, 0.0), (1.0, 0.0),
                                                   (1.0, 1.0)])
                for layer in (zone.layers or [])[:records]
            ]
    return board


class StubFiller:
    """Stands in for `kicad-cli pcb drc --refill-zones --save-board`.

    It writes the fill records the real one would, so the transplant and its
    gate can be exercised on a machine with no KiCad. What it deliberately does
    NOT do is compute copper: that is KiCad's job and this suite never checks
    a fill's shape.

    It stands in for `Popen` and not for `run`: the refill has to
    be stoppable mid flight, so it launches a process and watches it rather than
    blocking on one call. The stand-in therefore answers `wait`, `poll`,
    `terminate` and `kill`, and writes what it said into the caller's own sink -
    which is where the refill reads a failure's words from.
    """

    def __init__(self, zone_ids, records=1, drop=(), fail=False):
        self.zone_ids = set(zone_ids)
        self.records = records
        self.drop = set(drop)
        self.fail = fail
        self.calls = 0

    def __call__(self, args, **kwargs):
        self.calls += 1
        board_path = Path(args[-1])
        if self.fail:
            return _Process(4, "the sandbox board would not open",
                            kwargs.get("stdout"))
        board = Board.from_file(board_path)
        board.zones = [
            zone for zone in board.zones if zone.id not in self.drop
        ]
        filled_board(board, self.zone_ids, self.records).to_file(board_path)
        return _Process(0, "Found 0 violations\nSaved board\n",
                        kwargs.get("stdout"))


class _Process:
    """What `Popen` hands back, as much of it as the refill asks for."""

    def __init__(self, returncode, said, sink=None):
        self.returncode = returncode
        self.said = said
        if sink is not None:
            sink.write(said.encode("utf-8"))
            sink.flush()

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


class TransplantTest(unittest.TestCase):
    """The fill transplant, and the gate that says it touched nothing else."""

    def setUp(self):
        self.board = copper_board()
        self.board.drawings.append(PcbLine(
            start=(0.0, 0.0), end=(44.0, 0.0), layer="Edge.Cuts",
            stroke=Stroke(width=0.05), id="edge-1",
        ))
        self.made = apply(self.board, BLANK, GRID, Settings(),
                          instance_boxes=BOXES, fill=False)
        self.zones = [
            zone for zone in self.board.zones
            if (zone.name or "").startswith(ZONE_NAME_PREFIX)
        ]

    def run_with(self, stub, **kwargs):
        original = refill.subprocess.Popen
        original_is_file = refill.Path.is_file
        refill.subprocess.Popen = stub
        refill.Path.is_file = lambda self: True
        try:
            return refill_zones(self.board, self.zones, **kwargs)
        finally:
            refill.subprocess.Popen = original
            refill.Path.is_file = original_is_file

    def test_the_records_land_on_the_zones_they_came_from(self):
        # One carrier pour, three copper layers.
        stub = StubFiller([zone.id for zone in self.zones], records=3)
        result = self.run_with(stub)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(result.records, 3)
        self.assertEqual(result.zones_filled, 1)
        self.assertEqual(result.zones_empty, 0)
        for zone in self.zones:
            self.assertEqual(len(zone.filled_polygons), 3)

    def test_the_gate_holds_the_rest_of_the_panel_byte_identical(self):
        before = dump_board(_without(self.board, self.zones))
        result = self.run_with(StubFiller([z.id for z in self.zones]))
        self.assertTrue(result.gate_passed)
        self.assertEqual(dump_board(_without(self.board, self.zones)), before)

    def test_a_transplant_that_touched_anything_else_is_caught(self):
        # The gate exists for a change nothing here can currently make, so it
        # is exercised by making one: a transplant that also puts a line on the
        # board must be refused, not reported as a clean fill.
        board = self.board
        original = refill.replace_fills

        def meddling_replace(zone, fills):
            original(zone, fills)
            board.drawings.append(PcbLine(
                start=(0.0, 0.0), end=(1.0, 1.0), layer="Edge.Cuts",
                stroke=Stroke(width=0.05), id="meddled",
            ))

        refill.replace_fills = meddling_replace
        try:
            with self.assertRaises(RefillError) as caught:
                self.run_with(StubFiller([zone.id for zone in self.zones]))
        finally:
            refill.replace_fills = original
        self.assertIn("changed something other than", str(caught.exception))

    def test_a_lost_zone_stops_the_run(self):
        stub = StubFiller([zone.id for zone in self.zones],
                          drop=[self.zones[0].id])
        with self.assertRaises(RefillError) as caught:
            self.run_with(stub)
        self.assertIn("did not come back", str(caught.exception))

    def test_a_failing_cli_stops_the_run_and_says_what_it_said(self):
        with self.assertRaises(RefillError) as caught:
            self.run_with(StubFiller([], fail=True))
        self.assertIn("would not open", str(caught.exception))

    def test_an_empty_fill_is_reported_rather_than_hidden(self):
        stub = StubFiller([zone.id for zone in self.zones], records=0)
        result = self.run_with(stub)
        self.assertEqual(result.zones_empty, 1)
        self.assertEqual(result.records, 0)

    def test_nothing_to_fill_costs_no_process(self):
        result = refill_zones(self.board, [])
        self.assertEqual(result.records, 0)
        self.assertTrue(result.gate_passed)

    def test_a_zone_with_no_uuid_is_refused_before_anything_runs(self):
        self.zones[0].id = None
        with self.assertRaises(ValueError) as caught:
            self.run_with(StubFiller([]))
        self.assertIn("no uuid", str(caught.exception))

    def test_a_zone_that_would_lose_its_islands_is_refused(self):
        self.zones[0].fill.island_removal_mode = 0
        with self.assertRaises(ValueError) as caught:
            self.run_with(StubFiller([]))
        self.assertIn("island", str(caught.exception))

    def test_a_missing_kicad_cli_names_the_binary(self):
        with self.assertRaises(RefillError) as caught:
            refill_zones(self.board, self.zones,
                         cli_path=Path("C:/nowhere/kicad-cli.exe"))
        self.assertIn("kicad-cli", str(caught.exception))
        self.assertIn("nowhere", str(caught.exception))


class PourZonesTest(unittest.TestCase):
    """Finding the pours again on a finished panel, by the name this stage owns."""

    def setUp(self):
        self.board = copper_board()
        apply(self.board, BLANK, GRID, Settings(),
              instance_boxes=BOXES, fill=False)

    def test_every_pour_this_stage_made_is_found_again(self):
        found = rail_copper.pour_zones(self.board)
        self.assertEqual([zone.name for zone in found], [POUR_NAME])

    def test_a_zone_that_is_not_a_pour_is_not_one(self):
        # A keepout rule area is a zone too - both the array keep-out this
        # stage made and one from an unrelated stage - and filling either
        # would be filling something the panel put there to keep copper OUT.
        self.board.zones.append(Zone(
            net=0, net_name="", layers=["F.Cu"], id="keepout-1",
            name="PanelScoreKeepout_1", fill=ZoneFill(filled=False),
            polygons=[ZoneOutlinePolygon(pts=[(0.0, 0.0), (1.0, 0.0),
                                              (1.0, 1.0)])],
        ))
        self.assertEqual(len(rail_copper.pour_zones(self.board)), 1)

    def test_a_board_with_no_pour_has_none(self):
        self.assertEqual(rail_copper.pour_zones(copper_board()), [])


class SplitRefillTest(unittest.TestCase):
    """The round trip and the transplant, apart: what each half may do.

    The split is what lets a live preview pay ten seconds without holding a
    lock, so the properties worth pinning are which half changes the board and
    that an answer can be carried around before it is applied.
    """

    def setUp(self):
        self.board = copper_board()
        apply(self.board, BLANK, GRID, Settings(),
              instance_boxes=BOXES, fill=False)
        self.zones = rail_copper.pour_zones(self.board)

    def take(self, stub, **kwargs):
        original = refill.subprocess.Popen
        original_is_file = refill.Path.is_file
        refill.subprocess.Popen = stub
        refill.Path.is_file = lambda self: True
        try:
            return refill.take_fills(self.board, self.zones, **kwargs)
        finally:
            refill.subprocess.Popen = original
            refill.Path.is_file = original_is_file

    def test_the_round_trip_changes_nothing_on_the_caller_s_board(self):
        # One carrier pour.
        before = dump_board(self.board)
        taken = self.take(StubFiller([zone.id for zone in self.zones],
                                     records=2))
        self.assertEqual(len(taken.records), 1)
        self.assertEqual(dump_board(self.board), before)
        for zone in self.zones:
            self.assertFalse(zone.filled_polygons)

    def test_the_transplant_is_what_puts_the_copper_on(self):
        taken = self.take(StubFiller([zone.id for zone in self.zones],
                                     records=2))
        result = refill.transplant(self.board, self.zones, taken)
        self.assertEqual(result.records, 2)
        self.assertEqual(result.zones_filled, 1)
        self.assertTrue(result.gate_passed)
        for zone in self.zones:
            self.assertEqual(len(zone.filled_polygons), 2)

    def test_the_two_halves_in_order_are_the_one_call(self):
        stub = StubFiller([zone.id for zone in self.zones], records=2)
        taken = self.take(stub)
        result = refill.transplant(self.board, self.zones, taken)
        self.assertEqual(result.seconds, taken.seconds)
        self.assertEqual(result.cli_path, taken.cli_path)

    def test_an_answer_that_is_not_this_panel_s_is_refused(self):
        taken = refill.FillTaken(records={}, seconds=0.0,
                                 cli_path=Path("stub"))
        with self.assertRaises(RefillError) as caught:
            refill.transplant(self.board, self.zones, taken)
        self.assertIn("no fill in the answer", str(caught.exception))

    def test_nothing_to_fill_costs_no_process_and_no_gate(self):
        taken = refill.take_fills(self.board, [])
        self.assertEqual(taken.records, {})
        result = refill.transplant(self.board, [], taken)
        self.assertEqual(result.records, 0)
        self.assertTrue(result.gate_passed)

    def test_a_zone_that_would_lose_its_islands_is_refused_before_the_process(self):
        self.zones[0].fill.island_removal_mode = 0
        with self.assertRaises(ValueError):
            self.take(StubFiller([]))

    def test_the_binary_is_asked_for_rather_than_assumed(self):
        self.assertFalse(refill.cli_present(Path("C:/nowhere/kicad-cli.exe")))


def _without(board: Board, zones) -> Board:
    """A shallow view of *board* with *zones* left out, for the byte gate."""
    view = board.model_copy()
    targets = {id(zone) for zone in zones}
    view.zones = [zone for zone in board.zones if id(zone) not in targets]
    return view


HAVE_KICAD = shutil.which("kicad-cli") is not None or (
    refill.DEFAULT_KICAD_CLI.is_file()
)


# A blank KiCad itself will load: 44 x 47.5 mm of Edge.Cuts at (100, 100),
# three copper layers, nothing else. Written as a file rather than assembled in
# memory because KiCad refuses a board without the header blocks it expects.
FIXTURE = Path(__file__).parent / "data" / "rail_fill_fixture.kicad_pcb"
FIXTURE_BLANK = Rect(min_x=100.0, min_y=100.0, max_x=144.0, max_y=147.5)
FIXTURE_GRID = (105.0, 105.0, 139.0, 142.5)


@unittest.skipUnless(HAVE_KICAD, "needs kicad-cli, which CI does not have")
@runs_real_kicad("fills copper with KiCad's own zone filler")
@shared_setup_must_survive_every_test(
    lambda cls: board_fingerprint(cls.one_pour[0])
)
class RealFillTest(unittest.TestCase):
    """The real thing, on a board small enough to fill in a moment.

    The point is not the shape of the copper - KiCad owns that - but that a
    zone built by this stage comes back from KiCad's own filler with copper in
    it at all, which is the property a wrong zone would fail.
    """

    @classmethod
    def setUpClass(cls):
        # A real KiCad fill is slow, so one fill per answer is shared by the
        # tests that only measure it. The tests that need INDEPENDENT fills - the byte-equality ones - still
        # take their own below. The fence on this class proves the sharing:
        # it fingerprints the shared board after this runs and again after the
        # last test, and fails the class by name if a test changed it.
        cls.one_pour = cls.poured()

    @classmethod
    def poured(cls):
        board = Board.from_file(FIXTURE)
        made = apply(board, FIXTURE_BLANK, FIXTURE_GRID, Settings(),
                     instance_boxes=(), fill=True)
        return board, made

    def test_a_rail_pour_comes_back_filled(self):
        _, made = self.one_pour
        self.assertIsNotNone(made.refill)
        self.assertTrue(made.refill.gate_passed)
        self.assertEqual(made.refill.zones_filled, 1)
        self.assertEqual(made.refill.zones_empty, 0)
        # Three copper layers, one carrier pour, at least one record each.
        self.assertGreaterEqual(made.refill.records, 3)

    def test_the_fill_is_held_off_the_board_edge(self):
        board, _ = self.one_pour
        carrier = next(zone for zone in board.zones
                       if zone.name == POUR_NAME)
        xs = [point[0] for record in carrier.filled_polygons
              for point in record.pts if isinstance(point, tuple)]
        self.assertGreater(min(xs), FIXTURE_BLANK.min_x)
        self.assertLess(max(xs), FIXTURE_BLANK.max_x)

    def test_two_fills_of_one_panel_agree(self):
        self.assertEqual(dump_board(self.poured()[0]),
                         dump_board(self.poured()[0]))


@unittest.skipUnless(HAVE_KICAD, "needs kicad-cli, which CI does not have")
@runs_real_kicad("fills copper with KiCad's own zone filler")
@shared_setup_must_survive_every_test(
    lambda cls: board_fingerprint(cls.filled_late[0])
)
class LateFillTest(unittest.TestCase):
    """Filling a pour AFTERWARDS gives the same board as filling it at once.

    This is what lets the live preview draw an unfilled panel and fill it
    behind the picture without inventing a second kind of panel: the bytes a
    save writes after a late fill are the bytes a build writes. Proved here on
    the fixture.
    """

    @classmethod
    def setUpClass(cls):
        # A real KiCad fill is slow, so the late fill is one fill and two
        # tests read it. The fill AT ONCE is
        # taken fresh by the test that compares the two, because that
        # comparison is the whole assertion. The fence on this class proves the
        # shared one is never written to.
        cls.filled_late = cls.board_filled_late()

    @classmethod
    def board_filled_at_once(cls) -> Board:
        board = Board.from_file(FIXTURE)
        apply(board, FIXTURE_BLANK, FIXTURE_GRID, Settings(),
              instance_boxes=(), fill=True)
        return board

    @classmethod
    def board_filled_late(cls):
        board = Board.from_file(FIXTURE)
        apply(board, FIXTURE_BLANK, FIXTURE_GRID, Settings(),
              instance_boxes=(), fill=False)
        result = rail_copper.fill_pours(board)
        return board, result

    def test_a_late_fill_lands_on_the_pours(self):
        _, result = self.filled_late
        self.assertEqual(result.zones_filled, 1)
        self.assertEqual(result.zones_empty, 0)
        self.assertGreaterEqual(result.records, 3)
        self.assertTrue(result.gate_passed)

    def test_a_late_fill_is_the_same_board_as_a_fill_at_build_time(self):
        late, _ = self.filled_late
        self.assertEqual(dump_board(late),
                         dump_board(self.board_filled_at_once()))

    def test_a_board_with_no_pour_costs_no_process(self):
        result = rail_copper.fill_pours(Board.from_file(FIXTURE))
        self.assertEqual(result.records, 0)
        self.assertEqual(result.seconds, 0.0)


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _segment_distance(a, b, point) -> float:
    """Distance from *point* to the segment a-b, in millimetres."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return _distance(a, point)
    along = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)
    along = max(0.0, min(1.0, along))
    return _distance((a[0] + along * dx, a[1] + along * dy), point)


def _inside(points, point) -> bool:
    """Is *point* inside the polygon? Crossing count, the usual one."""
    hit = False
    count = len(points)
    previous = count - 1
    for index in range(count):
        xi, yi = points[index]
        xj, yj = points[previous]
        if (yi > point[1]) != (yj > point[1]) and point[0] < (
            (xj - xi) * (point[1] - yi) / (yj - yi) + xi
        ):
            hit = not hit
        previous = index
    return hit


@unittest.skipUnless(HAVE_KICAD, "needs kicad-cli, which CI does not have")
@runs_real_kicad("fills copper with KiCad's own zone filler")
@shared_setup_must_survive_every_test(
    lambda cls: (board_fingerprint(cls.with_pad[0]),
                 board_fingerprint(cls.without_pad[0]))
)
class PourRoundAPadTest(unittest.TestCase):
    """A pad placed AFTER the pour still gets its clearance ring.

    THE DEFECT, in its smallest form: the panel's
    fiducials are placed after the rail pour, and the pour was filled inside its
    own stage - so the board KiCad was handed had no fiducial pads on it and the
    copper flooded them. The tooling holes, added before the fill, came out with
    a ring; the fiducials did not.

    KiCad fills a BOARD, not a zone. The fix is an ordering one and it is fenced
    twice: here, on real copper from the real filler, and in
    `test_carrier_features.FillOrderTest`, which asks what was on the board when
    the fill ran and needs no KiCad.

    **THE ORACLE IS A KICAD RE-POUR OF THE SAVED PANEL.** The panel this build
    holds is written out and handed back to KiCad, and the fill records that
    come back must be the ones already on it. Anything else means the file a
    fabricator opens is not the file this build measured.
    """

    # A fiducial-shaped target: one bare SMD dot with a mask opening, on the top
    # rail well clear of the boards. Built here rather than read from a library,
    # so the fence owns its own input.
    PAD_AT = (110.0, 102.0)
    PAD_DIAMETER_MM = 1.0

    @classmethod
    def a_fiducial(cls) -> Footprint:
        return Footprint(
            fp_id="Fixture:Fiducial", layer="F.Cu",
            uuid="00000000-0000-0000-0000-0000000000fd",
            at={"position": cls.PAD_AT, "angle": 0.0},
            attributes={"smd": True, "exclude_from_bom": True},
            properties=[Property(
                name="Reference", value="PNLFD1", layer="F.SilkS",
                position=(0.0, -1.0), visible=False,
                id="00000000-0000-0000-0000-0000000000f1",
            )],
            pads=[Pad(
                number="", pad_type="smd", shape="circle",
                position=(0.0, 0.0),
                size=(cls.PAD_DIAMETER_MM, cls.PAD_DIAMETER_MM),
                layers=["F.Cu", "F.Mask"],
                id="00000000-0000-0000-0000-0000000000f2",
            )],
        )

    @classmethod
    def setUpClass(cls):
        # ONE KiCad fill per answer, not one per test. There are two answers here - with the
        # pad and without - and six tests were asking for them six times, at
        # about 6.5 s of real zone filling each. Every test below only MEASURES
        # the fill records; none writes to the board. That is not taken on
        # trust: the fence on this class fingerprints both boards after this
        # runs and again after the last test, so a test that changed one fails
        # this class by name instead of corrupting the next one.
        cls.with_pad = cls.pour(with_pad=True)
        cls.without_pad = cls.pour(with_pad=False)

    @classmethod
    def pour(cls, with_pad: bool):
        """The fixture, poured, with the pad placed BEFORE the fill is taken."""
        board = Board.from_file(FIXTURE)
        made = apply(board, FIXTURE_BLANK, FIXTURE_GRID,
                     Settings(clearance_mm=0.5), instance_boxes=(), fill=False)
        if with_pad:
            board.footprints.append(cls.a_fiducial())
        return board, rail_copper.fill_placed(board, made)

    def poured(self, with_pad: bool):
        """The shared answer. Read it. Never write to it - see setUpClass."""
        return self.with_pad if with_pad else self.without_pad

    def front_records(self, board):
        for zone in rail_copper.pour_zones(board):
            for record in zone.filled_polygons:
                if record.layer == "F.Cu":
                    yield zone, [p for p in record.pts if isinstance(p, tuple)]

    def test_the_pour_is_held_off_the_pad(self):
        board, _ = self.poured(with_pad=True)
        radius = self.PAD_DIAMETER_MM / 2.0
        gaps = [
            _segment_distance(points[index], points[(index + 1) % len(points)],
                              self.PAD_AT) - radius
            for _, points in self.front_records(board)
            for index in range(len(points))
        ]
        self.assertTrue(gaps)
        self.assertGreater(min(gaps), 0.0)

    def test_no_poured_copper_lands_on_the_pad_at_all(self):
        # The flooded panel's signature: a filled polygon whose outline runs
        # straight past the pad, with the copper dot underneath it.
        board, _ = self.poured(with_pad=True)
        radius = self.PAD_DIAMETER_MM / 2.0
        for zone, points in self.front_records(board):
            self.assertFalse(
                _inside(points, self.PAD_AT),
                f"{zone.name} poured copper over the pad at {self.PAD_AT}",
            )
            for point in points:
                self.assertGreaterEqual(
                    round(_distance(point, self.PAD_AT), 6), radius,
                    f"{zone.name} has a fill vertex inside the pad",
                )

    def test_the_pad_changes_the_fill_it_was_added_before(self):
        # The control. Without the pad the top rail fills as one strip per
        # layer; with it there is a hole in that strip, so the two answers
        # cannot be the same - which is what a fill taken too early would make
        # them.
        with_pad, _ = self.poured(with_pad=True)
        without, _ = self.poured(with_pad=False)
        self.assertNotEqual(
            [points for _, points in self.front_records(with_pad)],
            [points for _, points in self.front_records(without)],
        )

    def repoured(self, board):
        """*board*, written out and handed back to KiCad to fill again."""
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / "panel.kicad_pcb"
            board.to_file(saved)
            again = Board.from_file(saved)
            refill_zones(again, rail_copper.pour_zones(again),
                         work_dir=Path(folder) / "sandbox")
        return again

    def test_our_fill_is_what_a_kicad_repour_of_the_saved_panel_gives(self):
        # THE ORACLE. Write the panel, hand it back to KiCad, and compare the
        # fill records. Equal means the copper in the file is the copper this
        # build measured and reported.
        board, _ = self.poured(with_pad=True)
        again = self.repoured(board)
        ours = {zone.id: [record.model_dump()
                          for record in zone.filled_polygons]
                for zone in rail_copper.pour_zones(board)}
        kicad = {zone.id: [record.model_dump()
                           for record in zone.filled_polygons]
                 for zone in rail_copper.pour_zones(again)}
        self.assertEqual(sorted(ours), sorted(kicad))
        for identity in ours:
            self.assertEqual(ours[identity], kicad[identity])

    def test_the_whole_saved_panel_survives_a_repour_unchanged(self):
        # The same oracle said over the whole file: a panel KiCad re-pours is
        # the panel it was handed, to the byte.
        board, _ = self.poured(with_pad=True)
        self.assertEqual(dump_board(self.repoured(board)), dump_board(board))


@unittest.skipUnless(HAVE_KICAD, "needs kicad-cli, which CI does not have")
@runs_real_kicad("fills copper with KiCad's own zone filler")
@shared_setup_must_survive_every_test(
    lambda cls: (board_fingerprint(cls.guarded[0]),
                 board_fingerprint(cls.bare[0]))
)
class PourAcrossAScoreTest(unittest.TestCase):
    """The pour holds off a saw pass THE BOARDS decided, with no scores section.

    The defect this exists for: the keepout round
    a score line was made inside the panel-scores stage, so a config that named
    no `panel.scores` ran no stage and got no rule area - and the rail pour then
    ran straight across the merged board-edge passes. Nothing here asks for a
    panel score: `panel_scores.protect` is what a build does for the lines that
    are already on the panel, and the copper KiCad pours has to respect it.
    """

    SCORE_Y = 102.5

    @classmethod
    def setUpClass(cls):
        # A real KiCad fill is slow, so one fill per answer is shared by the
        # tests that only measure it. The fence on this class fingerprints the shared boards after this runs
        # and again after the last test, so a test that changed one fails this
        # class by name rather than corrupting the next.
        cls.guarded = cls.poured(protect=True)
        cls.bare = cls.poured(protect=False)

    @classmethod
    def poured(cls, protect: bool):
        board = Board.from_file(FIXTURE)
        board.drawings.append(PcbLine(
            start=(FIXTURE_BLANK.min_x, cls.SCORE_Y),
            end=(FIXTURE_BLANK.max_x, cls.SCORE_Y),
            layer="User.38", stroke=Stroke(width=0.1),
            id="00000000-0000-0000-0000-0000000000b1",
        ))
        made = panel_scores.protect(board) if protect else None
        apply(board, FIXTURE_BLANK, FIXTURE_GRID, Settings(),
              instance_boxes=(), fill=True)
        return board, made

    def top_pour(self, board):
        # There is one carrier pour now; the score still crosses it.
        return next(zone for zone in board.zones
                    if zone.name == POUR_NAME)

    def test_the_protection_runs_without_any_scores_section(self):
        _, made = self.guarded
        self.assertTrue(made.protection_only)
        self.assertEqual(len(made.rule_areas), 1)
        self.assertEqual(made.half_width_mm, DEFAULT_RULE_HALF_WIDTH_MM)

    def test_no_poured_copper_crosses_the_score(self):
        board, made = self.guarded
        half = made.half_width_mm
        for record in self.top_pour(board).filled_polygons:
            for point in record.pts:
                if not isinstance(point, tuple):
                    continue
                self.assertFalse(
                    self.SCORE_Y - half < point[1] < self.SCORE_Y + half,
                    f"poured copper at {point} is inside the saw's band",
                )

    def test_the_pour_is_cut_in_two_where_it_used_to_run_through(self):
        # The visible difference: without the rule area the top rail fills as
        # one strip per layer, and with it the saw line divides it.
        guarded, _ = self.guarded
        bare, _ = self.bare
        self.assertGreater(len(self.top_pour(guarded).filled_polygons),
                           len(self.top_pour(bare).filled_polygons))


class UnusedImportGuard(unittest.TestCase):
    """The namespace is exported so a rename cannot happen quietly."""

    def test_the_namespace_is_fixed(self):
        self.assertEqual(str(RAIL_NAMESPACE)[14], "5")
        self.assertEqual(rail_copper.ZONE_NAME_PREFIX, "PanelRail")

    def test_a_zone_and_its_fill_model_are_what_the_library_exposes(self):
        zone = Zone(fill=ZoneFill(filled=True),
                    polygons=[ZoneOutlinePolygon(pts=[(0.0, 0.0)])])
        self.assertTrue(zone.fill.filled)


if __name__ == "__main__":
    unittest.main()
