"""Zone flattening: the panel carries copper, never a live copper zone.

What is pinned here:
every COPPER zone is gone by the time the panel is written, leaving its cached
fill behind as `gr_poly` copper, and every RULE AREA survives untouched -
board-level and footprint-hosted alike, with no predicate between them, because
`eedesigncore` moves a footprint-hosted zone with its footprint.
"""

import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    PcbGroup,
    PcbLine,
    Stroke,
    Zone,
    ZoneFilledPolygon,
    ZoneKeepout,
)

from eedesignpanels.stages import zones

SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def filled_zone(net="AGND_1", uid="z1", layers=("F.Cu",), island=False):
    """A copper zone with one cached fill record per layer."""
    return Zone(
        net=1,
        net_name=net,
        layer=layers[0],
        id=uid,
        fill={"filled": True},
        filled_polygons=[
            ZoneFilledPolygon(layer=layer, island=island, pts=list(SQUARE))
            for layer in layers
        ],
    )


def rule_area(uid="k1"):
    """A keepout: a rule, not copper, so it has nothing to flatten - and it
    stays, because nothing downstream can refill what pours nothing."""
    return Zone(
        layer="F.Cu",
        id=uid,
        keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                            copper_pour=False, footprints=False),
        polygons=[{"pts": list(SQUARE)}],
    )


def board_with(*board_zones, footprint_zones=()) -> Board:
    board = Board(
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=2, canonical_name="B.Cu", layer_type="signal"),
        ],
        zones=list(board_zones),
    )
    if footprint_zones:
        board.footprints.append(Footprint(
            fp_id="PanelTab:MouseBite", layer="F.Cu", uuid="fp-1",
            at={"position": (0.0, 0.0), "angle": 0.0},
            zones=list(footprint_zones),
        ))
    return board


class ClassifyTest(unittest.TestCase):
    def test_a_keepout_is_a_rule_area(self):
        self.assertTrue(zones.is_rule_area(rule_area()))

    def test_a_copper_zone_is_not(self):
        self.assertFalse(zones.is_rule_area(filled_zone()))

    def test_the_fill_records_are_counted(self):
        self.assertEqual(zones.fill_records(filled_zone(layers=("F.Cu", "B.Cu"))), 2)
        self.assertEqual(zones.fill_records(rule_area()), 0)


class IdentityTest(unittest.TestCase):
    def test_the_key_is_the_zones_own_uuid(self):
        self.assertEqual(zone_key := zones.zone_key(filled_zone(uid="abc"), 7), "abc")
        self.assertNotIn("7", zone_key)

    def test_a_zone_with_no_uuid_falls_back_to_its_position(self):
        zone = filled_zone(uid=None)
        zone.id = None
        self.assertEqual(zones.zone_key(zone, 3), "index:3")

    def test_the_graphics_of_one_zone_are_identified_and_distinct(self):
        graphics = zones.graphics_of(filled_zone(layers=("F.Cu", "B.Cu")), 0)
        ids = [graphic.id for graphic in graphics]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(ids))

    def test_the_same_zone_gives_the_same_identities_every_time(self):
        first = [g.id for g in zones.graphics_of(filled_zone(), 0)]
        second = [g.id for g in zones.graphics_of(filled_zone(), 0)]
        self.assertEqual(first, second)

    def test_two_instances_of_one_zone_do_not_collide(self):
        left = [g.id for g in zones.graphics_of(filled_zone(uid="z|0|0"), 0)]
        right = [g.id for g in zones.graphics_of(filled_zone(uid="z|1|0"), 1)]
        self.assertNotEqual(left, right)


class FlattenTest(unittest.TestCase):
    def test_the_cached_fill_becomes_copper_and_the_zone_goes(self):
        board = board_with(filled_zone())
        warnings = []
        made = zones.apply(board, warnings)
        self.assertEqual(board.zones, [])
        self.assertEqual(made.filled_zones, 1)
        self.assertEqual(made.polygons, 1)
        self.assertEqual(len(board.drawings), 1)
        graphic = board.drawings[0]
        self.assertEqual(type(graphic).__name__, "PcbPoly")
        self.assertEqual(graphic.layer, "F.Cu")
        self.assertEqual(graphic.net_name, "AGND_1")
        self.assertEqual(graphic.stroke.width, 0.0)
        self.assertEqual([tuple(p) for p in graphic.pts], SQUARE)

    def test_the_copper_is_counted_per_layer(self):
        board = board_with(filled_zone(layers=("F.Cu", "B.Cu")))
        made = zones.apply(board, [])
        self.assertEqual(made.per_layer, (("B.Cu", 1), ("F.Cu", 1)))

    def test_an_island_record_carries_no_net(self):
        board = board_with(filled_zone(island=True))
        made = zones.apply(board, [])
        self.assertEqual(made.islands, 1)
        self.assertIsNone(board.drawings[0].net_name)

    def test_a_board_level_rule_area_survives_untouched(self):
        keepout = rule_area()
        board = board_with(keepout)
        warnings = []
        made = zones.apply(board, warnings)
        self.assertEqual(made.rule_areas, 1)
        self.assertEqual(made.polygons, 0)
        self.assertEqual(board.zones, [keepout])
        self.assertEqual(warnings, [])

    def test_keeping_a_rule_area_says_nothing(self):
        # No warning, no conditional, no ceremony: keeping it is the rule.
        warnings = []
        zones.apply(board_with(rule_area(), footprint_zones=[rule_area("fk1")]),
                    warnings)
        self.assertEqual(warnings, [])

    def test_a_copper_zone_with_no_fill_record_is_named(self):
        zone = filled_zone()
        zone.filled_polygons = []
        warnings = []
        made = zones.apply(board_with(zone), warnings)
        self.assertEqual(made.empty_zones, 1)
        self.assertEqual(made.polygons, 0)
        self.assertTrue(any("no fill record" in w for w in warnings))

    def test_a_footprint_hosted_rule_area_survives_too(self):
        keepout = rule_area("fk1")
        board = board_with(filled_zone(), footprint_zones=[keepout])
        made = zones.apply(board, [])
        self.assertEqual(made.footprint_rule_areas, 1)
        self.assertEqual(board.footprints[0].zones, [keepout])

    def test_only_the_copper_zones_are_counted_as_removed(self):
        board = board_with(filled_zone(), rule_area(),
                           footprint_zones=[rule_area("fk1")])
        made = zones.apply(board, [])
        self.assertEqual(made.zones_removed, 1)
        self.assertEqual(made.rule_areas_kept, 2)

    def test_a_group_that_held_a_zone_loses_the_reference(self):
        board = board_with(filled_zone(uid="z1"))
        board.drawings.append(PcbLine(start=(0.0, 0.0), end=(1.0, 0.0),
                                      layer="F.SilkS", stroke=Stroke(width=0.1),
                                      id="keeper"))
        board.groups.append(PcbGroup(name="g", id="g1",
                                     members=["z1", "keeper"]))
        made = zones.apply(board, [])
        self.assertEqual(made.groups_pruned, 1)
        self.assertEqual(board.groups[0].members, ["keeper"])

    def test_a_group_that_holds_a_kept_rule_area_keeps_the_reference(self):
        board = board_with(rule_area("k1"))
        board.groups.append(PcbGroup(name="g", id="g1", members=["k1"]))
        made = zones.apply(board, [])
        self.assertEqual(made.groups_pruned, 0)
        self.assertEqual(board.groups[0].members, ["k1"])

    def test_two_unidentified_zones_in_different_hosts_do_not_collide(self):
        # The position fallback runs over every zone on the panel, not over
        # each list, so a board zone and a footprint zone never share a key.
        board_zone = filled_zone(uid=None)
        board_zone.id = None
        hosted = filled_zone(uid=None)
        hosted.id = None
        board = board_with(board_zone, footprint_zones=[hosted])
        zones.apply(board, [])
        self.assertEqual(len({item.id for item in board.drawings}), 2)

    def test_a_group_of_nothing_but_zones_is_dropped(self):
        board = board_with(filled_zone(uid="z1"))
        board.groups.append(PcbGroup(name="g", id="g1", members=["z1"]))
        made = zones.apply(board, [])
        self.assertEqual(made.groups_dropped, 1)
        self.assertEqual(board.groups, [])


class FootprintFilledZoneTest(unittest.TestCase):
    """A footprint-hosted copper zone flattens like any other.

    `translate_board` moves its board-coordinate fill records with the
    footprint, so the records arrive at the replica's own position and the
    build has nothing to refuse.
    """

    def test_a_footprint_hosted_fill_becomes_board_level_copper(self):
        board = board_with(footprint_zones=[filled_zone(uid="fz1")])
        made = zones.apply(board, [])
        self.assertEqual(made.filled_zones, 1)
        self.assertEqual(made.polygons, 1)
        self.assertEqual(board.footprints[0].zones, [])
        self.assertEqual(len(board.drawings), 1)
        self.assertEqual(board.drawings[0].layer, "F.Cu")

    def test_a_footprint_fill_and_a_board_fill_get_distinct_identities(self):
        board = board_with(filled_zone(uid="z1"),
                           footprint_zones=[filled_zone(uid="fz1")])
        made = zones.apply(board, [])
        self.assertEqual(made.filled_zones, 2)
        self.assertEqual(len({item.id for item in board.drawings}), 2)

    def test_a_hosted_rule_area_beside_a_hosted_fill_still_stays(self):
        keepout = rule_area("fk1")
        board = board_with(footprint_zones=[filled_zone(uid="fz1"), keepout])
        made = zones.apply(board, [])
        self.assertEqual(made.filled_zones, 1)
        self.assertEqual(made.footprint_rule_areas, 1)
        self.assertEqual(board.footprints[0].zones, [keepout])


if __name__ == "__main__":
    unittest.main()
