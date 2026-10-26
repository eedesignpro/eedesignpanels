"""The stage builds board data from board data, so it is checked in memory.

No board file and no KiCad: a `Board` assembled here is enough to pin every
property the stage promises - that a whole copy lands per cell, that no two
copies share a uuid, a net name or a designator, that the zone fill records ride
along untouched, that the schematic linkage is cut, and that the source is never
modified.
"""

import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    Dimension,
    DimensionText,
    EmbeddedFile,
    Footprint,
    Pad,
    PcbGroup,
    PcbLine,
    Property,
    Setup,
    Stroke,
    TitleBlock,
    TrackSegment,
    Zone,
    ZoneFilledPolygon,
    ZoneKeepout,
    item_layers,
)

from eedesignpanels.project import PANEL_MARKER, PANEL_MARKER_VALUE
from eedesignpanels.stages import step_and_repeat
from eedesignpanels.stages.step_and_repeat import (
    ContentCounts,
    build,
    is_dimension,
    outline_items,
    panel_geometry,
    replicated_layers,
    replication_content,
    resolve_layer_names,
    set_board_property,
    unlink_from_schematic,
)


def outline(x0, y0, x1, y1, layer="Edge.Cuts", uuid_value=None):
    """One straight outline segment with a stable uuid."""
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer,
        id=uuid_value or f"{x0}-{y0}-{x1}-{y1}",
    )


def zone_anchor(zone) -> tuple:
    """A zone's first outline point, rounded, as its position on the board.

    A rule area is stored in BOARD coordinates whatever hosts it, so this is
    directly comparable across instances - which is what makes it the right
    probe for whether a replica's keepout actually moved.
    """
    polygon = zone.polygons[0]
    pts = polygon["pts"] if isinstance(polygon, dict) else polygon.pts
    return (round(float(pts[0][0]), 6), round(float(pts[0][1]), 6))


def part(reference="R1", uuid_value="fp-1"):
    """One placed footprint carrying everything the stage has to re-identify."""
    return Footprint(
        fp_id="Lib:R_0402",
        layer="F.Cu",
        uuid=uuid_value,
        at={"position": (5.0, 5.0), "angle": 0.0},
        path="/sheet-uuid/symbol-uuid",
        sheetname="/Amplifier/",
        sheetfile="amplifier.kicad_sch",
        properties=[
            Property(name="Reference", value=reference, id=f"{uuid_value}-ref"),
            Property(name="Value", value="10k", id=f"{uuid_value}-val"),
        ],
        pads=[
            Pad(number="1", pad_type="smd", shape="rect", position=(0.0, 0.0),
                size=(1.0, 1.0), layers=["F.Cu", "F.Mask"], net_name="AGND",
                id=f"{uuid_value}-pad1"),
        ],
    )


def square_board():
    """A 10 x 10 mm square outline with real content inside it.

    One footprint, one track, one filled zone and one silkscreen line, so the
    tests can tell "the whole board was copied" from "the outline was copied".
    """
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=25, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=5, canonical_name="F.SilkS", layer_type="user"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    board.drawings = [
        outline(0.0, 0.0, 10.0, 0.0),
        outline(10.0, 0.0, 10.0, 10.0),
        outline(10.0, 10.0, 0.0, 10.0),
        outline(0.0, 10.0, 0.0, 0.0),
        outline(1.0, 1.0, 2.0, 2.0, layer="F.SilkS"),
    ]
    board.footprints = [part()]
    board.tracks = [
        TrackSegment(start=(2.0, 2.0), end=(8.0, 2.0), width=0.2, layer="F.Cu",
                     net="AGND", uuid="track-1"),
    ]
    board.zones = [
        Zone(
            layers=["F.Cu"], net_name="AGND", id="zone-1",
            polygons=[{"pts": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]}],
            filled_polygons=[
                ZoneFilledPolygon(
                    layer="F.Cu",
                    pts=[(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)],
                )
            ],
        )
    ]
    return board


def dimension(layer="F.SilkS", uuid_value="dim-1"):
    """One dimension, deliberately NOT on a drawing-scale layer.

    The skip is unconditional, so a fixture that put every dimension
    on an excluded layer could not tell the two rules apart.
    """
    return Dimension(
        dim_type="orthogonal", layer=layer, id=uuid_value,
        pts=[(0.0, 0.0), (10.0, 0.0)],
        text=DimensionText(id=uuid_value, content="10.0000"),
    )


def dimension_board():
    """`square_board` plus two dimensions on an ordinary layer."""
    board = square_board()
    board.drawings.append(dimension(uuid_value="dim-1"))
    board.drawings.append(dimension(layer="F.Cu", uuid_value="dim-2"))
    return board


def drawing_board():
    """`square_board` plus a drawing-scale layer that must not be replicated.

    `Dwgs.User` is the canonical name; `User.Drawings` is what KiCad's
    Appearance panel shows and what an engineer writes in a config.
    """
    board = square_board()
    board.layers.append(
        BoardLayer(ordinal=17, canonical_name="Dwgs.User", layer_type="user",
                   user_name="User.Drawings")
    )
    board.drawings.append(outline(-50.0, -50.0, -10.0, -50.0, layer="Dwgs.User",
                                  uuid_value="note-1"))
    board.zones.append(
        Zone(layers=["Dwgs.User"], id="zone-note",
             polygons=[{"pts": [(-50.0, -50.0), (-10.0, -50.0), (-10.0, -40.0)]}])
    )
    return board


class OutlineItemsTest(unittest.TestCase):
    def test_only_edge_cuts_drawings_are_selected(self):
        self.assertEqual(len(outline_items(square_board())), 4)

    def test_trailing_drawings_are_included(self):
        board = square_board()
        board.trailing_drawings = [outline(5.0, 5.0, 6.0, 6.0, uuid_value="late")]
        self.assertEqual(len(outline_items(board)), 5)

    def test_the_layer_is_a_parameter(self):
        self.assertEqual(len(outline_items(square_board(), "F.SilkS")), 1)


class ReplicatedLayersTest(unittest.TestCase):
    def test_the_declaration_is_the_boards_own_layer_table(self):
        self.assertEqual(
            replicated_layers(square_board()),
            frozenset({"F.Cu", "Edge.Cuts", "F.SilkS"}),
        )

    def test_it_never_claims_a_layer_the_board_does_not_have(self):
        board = square_board()
        declared = {layer.canonical_name for layer in board.layers}
        self.assertEqual(replicated_layers(board) - declared, set())

    def test_a_pad_wildcard_is_never_mistaken_for_a_layer(self):
        board = square_board()
        board.footprints[0].pads[0].layers = ["*.Cu", "*.Mask"]
        self.assertNotIn("*.Cu", replicated_layers(board))

    def test_the_geometry_carries_it_for_the_diff_reference(self):
        geometry = panel_geometry(square_board(), 2, 3, 3.0)
        self.assertEqual(geometry.replicated_layers, replicated_layers(square_board()))

    def test_an_excluded_layer_is_not_claimed_as_replicated(self):
        board = drawing_board()
        self.assertEqual(
            replicated_layers(board, frozenset({"Dwgs.User"})),
            frozenset({"F.Cu", "Edge.Cuts", "F.SilkS"}),
        )

    def test_the_geometry_drops_the_excluded_layers_too(self):
        geometry = panel_geometry(
            drawing_board(), 2, 3, 3.0, ["User.Drawings"]
        )
        self.assertNotIn("Dwgs.User", geometry.replicated_layers)

    def test_the_removed_constant_says_where_the_answer_went(self):
        with self.assertRaises(AttributeError) as caught:
            step_and_repeat.REPLICATED_LAYERS  # noqa: B018 - the raise is the test
        self.assertIn("replicated_layers", str(caught.exception))


class ContentCountsTest(unittest.TestCase):
    def test_it_counts_every_content_stream(self):
        counts = ContentCounts.of(square_board())
        self.assertEqual(counts.footprints, 1)
        self.assertEqual(counts.drawings, 5)
        self.assertEqual(counts.tracks, 1)
        self.assertEqual(counts.zones, 1)

    def test_trailing_drawings_count_as_drawings(self):
        board = square_board()
        board.trailing_drawings = [outline(5.0, 5.0, 6.0, 6.0, uuid_value="late")]
        self.assertEqual(ContentCounts.of(board).drawings, 6)

    def test_times_is_what_a_panel_of_n_copies_should_hold(self):
        self.assertEqual(
            ContentCounts.of(square_board()).times(6).footprints, 6
        )


class UnlinkFromSchematicTest(unittest.TestCase):
    def test_the_schematic_linkage_is_cut(self):
        footprint = part("R4")
        unlink_from_schematic(footprint)
        self.assertIsNone(footprint.path)
        self.assertIsNone(footprint.sheetname)
        self.assertIsNone(footprint.sheetfile)

    def test_the_designator_is_left_exactly_as_it_was(self):
        footprint = part("R4")
        unlink_from_schematic(footprint)
        self.assertEqual(footprint.properties[0].value, "R4")

    def test_no_property_is_touched_at_all(self):
        footprint = part("R4")
        before = [(p.name, p.value) for p in footprint.properties]
        unlink_from_schematic(footprint)
        self.assertEqual([(p.name, p.value) for p in footprint.properties], before)


class BuildTest(unittest.TestCase):
    def test_every_instance_gets_the_whole_board(self):
        outcome = build(square_board(), 2, 3, 3.0)
        self.assertEqual(outcome.panel_content, outcome.source_content.times(6))

    def test_footprints_tracks_and_zones_all_replicate(self):
        panel = build(square_board(), 2, 3, 3.0).board
        self.assertEqual(len(panel.footprints), 6)
        self.assertEqual(len(panel.tracks), 6)
        self.assertEqual(len(panel.zones), 6)
        self.assertEqual(len(panel.drawings), 30)

    def test_the_step_is_the_outline_plus_the_gap(self):
        geometry = build(square_board(), 2, 3, 3.0).geometry
        self.assertAlmostEqual(geometry.outline_box.width, 10.0)
        self.assertAlmostEqual(geometry.extent.step_x_mm, 13.0)
        self.assertAlmostEqual(geometry.extent.width_mm, 23.0)
        self.assertAlmostEqual(geometry.extent.height_mm, 36.0)

    def test_instance_zero_stays_where_the_source_sits(self):
        panel = build(square_board(), 2, 1, 3.0).board
        self.assertEqual(panel.drawings[0].start, (0.0, 0.0))

    def test_later_instances_step_by_the_pitch(self):
        panel = build(square_board(), 2, 1, 3.0).board
        self.assertEqual(panel.drawings[5].start, (13.0, 0.0))
        self.assertEqual(panel.footprints[1].at.position, (18.0, 5.0))

    def test_no_two_replicas_share_a_uuid(self):
        panel = build(square_board(), 2, 3, 3.0).board
        ids = (
            [item.id for item in panel.drawings]
            + [track.uuid for track in panel.tracks]
            + [zone.id for zone in panel.zones]
            + [fp.uuid for fp in panel.footprints]
        )
        self.assertEqual(len(set(ids)), len(ids))

    def test_no_two_instances_share_a_net(self):
        panel = build(square_board(), 2, 3, 3.0).board
        self.assertEqual(
            sorted(panel.net_names),
            ["AGND_1", "AGND_2", "AGND_3", "AGND_4", "AGND_5", "AGND_6"],
        )

    def test_a_net_is_renamed_on_every_item_that_holds_one(self):
        panel = build(square_board(), 1, 1, 3.0).board
        self.assertEqual(panel.tracks[0].net, "AGND_1")
        self.assertEqual(panel.zones[0].net_name, "AGND_1")
        self.assertEqual(panel.footprints[0].pads[0].net_name, "AGND_1")

    def test_every_copy_keeps_the_source_designator_verbatim(self):
        # The designator is printed on the silkscreen,
        # so every copy has to read what the design reads.
        panel = build(square_board(), 2, 3, 3.0).board
        refs = [
            prop.value
            for fp in panel.footprints
            for prop in fp.properties
            if prop.name == "Reference"
        ]
        self.assertEqual(refs, ["R1"] * 6)

    def test_no_other_footprint_property_is_edited_either(self):
        panel = build(square_board(), 2, 3, 3.0).board
        values = {prop.value for fp in panel.footprints for prop in fp.properties}
        self.assertEqual(values, {"R1", "10k"})

    def test_no_copy_is_linked_to_the_source_schematic(self):
        panel = build(square_board(), 2, 3, 3.0).board
        self.assertEqual({fp.path for fp in panel.footprints}, {None})

    def test_zone_fill_records_ride_along_verbatim(self):
        # Each copy carries the source's stored fill as it is. Nothing here
        # refills.
        panel = build(square_board(), 2, 1, 3.0).board
        first, second = panel.zones
        self.assertEqual(len(first.filled_polygons), 1)
        self.assertEqual(first.filled_polygons[0].pts[0], (1.0, 1.0))
        self.assertEqual(second.filled_polygons[0].pts[0], (14.0, 1.0))

    def test_the_property_snapshot_comes_across_so_variables_resolve(self):
        # The panel carries the board properties, so ${PCB_DWG_SCALE} in
        # board text resolves against these.
        source = square_board()
        source.properties = [Property(name="PART_ID", value="100-200-3000")]
        panel = build(source, 2, 1, 3.0).board
        self.assertEqual(
            {prop.name: prop.value for prop in panel.properties},
            {"PART_ID": "100-200-3000", PANEL_MARKER: PANEL_MARKER_VALUE},
        )

    def test_the_marker_says_which_document_this_is(self):
        panel = build(square_board(), 2, 1, 3.0).board
        marker = [p for p in panel.properties if p.name == PANEL_MARKER]
        self.assertEqual([p.value for p in marker], [PANEL_MARKER_VALUE])

    def test_the_title_block_stays_behind(self):
        # The title block describes the source design, so a panel that
        # carried it could be mistaken for the design.
        source = square_board()
        source.title_block = TitleBlock(title="100-200-3000", rev="01")
        self.assertIsNone(build(source, 2, 1, 3.0).board.title_block)

    def test_the_layer_table_and_setup_come_across(self):
        panel = build(square_board(), 1, 1, 3.0).board
        self.assertEqual(
            [layer.canonical_name for layer in panel.layers],
            ["F.Cu", "Edge.Cuts", "F.SilkS"],
        )
        self.assertEqual(panel.setup.pad_to_mask_clearance, 0.1)
        self.assertEqual(panel.version, 20260206)
        self.assertEqual(panel.generator_version, "10.0")

    def test_the_source_board_is_never_modified(self):
        source = square_board()
        before = (
            [(item.start, item.end, item.id) for item in source.drawings]
            + [(t.start, t.end, t.net, t.uuid) for t in source.tracks]
        )
        before_ref = source.footprints[0].properties[0].value
        before_path = source.footprints[0].path
        build(source, 2, 3, 3.0)
        after = (
            [(item.start, item.end, item.id) for item in source.drawings]
            + [(t.start, t.end, t.net, t.uuid) for t in source.tracks]
        )
        self.assertEqual(before, after)
        self.assertEqual(source.footprints[0].properties[0].value, before_ref)
        self.assertEqual(source.footprints[0].path, before_path)

    def test_building_twice_gives_the_same_geometry_and_uuids(self):
        first = build(square_board(), 2, 3, 3.0).board
        second = build(square_board(), 2, 3, 3.0).board
        self.assertEqual(first.to_string(), second.to_string())

    def test_the_stroked_extent_adds_the_outline_width_once(self):
        board = square_board()
        for item in outline_items(board):
            item.stroke.width = 0.2
        geometry = build(board, 2, 3, 3.0).geometry
        self.assertAlmostEqual(
            geometry.inked_extent_mm[0], geometry.extent.width_mm + 0.2
        )
        self.assertAlmostEqual(
            geometry.inked_extent_mm[1], geometry.extent.height_mm + 0.2
        )

    def test_a_board_with_no_outline_is_an_error_not_an_empty_panel(self):
        board = square_board()
        board.drawings = [outline(1.0, 1.0, 2.0, 2.0, layer="F.SilkS")]
        with self.assertRaises(ValueError):
            build(board, 2, 3, 3.0)


class IdentityWarningTest(unittest.TestCase):
    def test_a_clean_board_warns_about_nothing(self):
        self.assertEqual(build(square_board(), 2, 3, 3.0).warnings, [])

    def test_a_net_that_collides_with_its_own_suffix_warns_and_still_builds(self):
        board = square_board()
        board.tracks.append(
            TrackSegment(start=(3.0, 3.0), end=(4.0, 3.0), width=0.2,
                         layer="F.Cu", net="AGND_1", uuid="track-2")
        )
        outcome = build(board, 2, 3, 3.0)
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("net name collision", outcome.warnings[0])
        # A check reports; the panel is still built.
        self.assertEqual(len(outcome.board.tracks), 12)

    def test_a_duplicated_designator_is_not_a_warning(self):
        # Designators are duplicated on purpose now, so a source that
        # already looks suffixed says nothing about them.
        board = square_board()
        board.footprints.append(part("R1_1", uuid_value="fp-2"))
        self.assertEqual(build(board, 2, 3, 3.0).warnings, [])


class ResolveLayerNamesTest(unittest.TestCase):
    def test_a_canonical_name_resolves_to_itself(self):
        resolved, unknown = resolve_layer_names(drawing_board(), ["Dwgs.User"])
        self.assertEqual(resolved, frozenset({"Dwgs.User"}))
        self.assertEqual(unknown, [])

    def test_the_display_name_an_engineer_reads_resolves_too(self):
        resolved, _ = resolve_layer_names(drawing_board(), ["User.Drawings"])
        self.assertEqual(resolved, frozenset({"Dwgs.User"}))

    def test_both_spellings_of_one_layer_are_one_layer(self):
        resolved, _ = resolve_layer_names(
            drawing_board(), ["User.Drawings", "Dwgs.User"]
        )
        self.assertEqual(resolved, frozenset({"Dwgs.User"}))

    def test_a_layer_this_board_does_not_have_is_reported_not_raised(self):
        resolved, unknown = resolve_layer_names(
            drawing_board(), ["Dwgs.User", "User.42"]
        )
        self.assertEqual(resolved, frozenset({"Dwgs.User"}))
        self.assertEqual(unknown, ["User.42"])


class ReplicationContentTest(unittest.TestCase):
    def test_no_exclusion_still_hands_back_every_item(self):
        # It is a view rather than the source itself, because the dimension
        # skip is unconditional - but nothing else is dropped.
        board = square_board()
        view, warnings = replication_content(board, frozenset())
        self.assertEqual(ContentCounts.of(view), ContentCounts.of(board))
        self.assertEqual(warnings, [])

    def test_excluded_content_is_left_behind(self):
        view, _ = replication_content(drawing_board(), frozenset({"Dwgs.User"}))
        self.assertEqual(len(view.drawings), 5)
        self.assertEqual(len(view.zones), 1)
        self.assertNotIn("note-1", [item.id for item in view.drawings])

    def test_content_on_every_other_layer_stays(self):
        view, _ = replication_content(drawing_board(), frozenset({"Dwgs.User"}))
        self.assertEqual(len(view.footprints), 1)
        self.assertEqual(len(view.tracks), 1)

    def test_the_layer_table_still_names_the_excluded_layer(self):
        view, _ = replication_content(drawing_board(), frozenset({"Dwgs.User"}))
        self.assertIn("Dwgs.User", [layer.canonical_name for layer in view.layers])

    def test_the_embedded_files_kept_items_need_come_along(self):
        board = drawing_board()
        board.embedded_files = [
            EmbeddedFile(name="font.ttf", file_type="font", data="", checksum="")
        ]
        view, _ = replication_content(board, frozenset({"Dwgs.User"}))
        self.assertEqual([f.name for f in view.embedded_files], ["font.ttf"])

    def test_the_source_board_is_never_modified(self):
        board = drawing_board()
        replication_content(board, frozenset({"Dwgs.User"}))
        self.assertEqual(len(board.drawings), 6)
        self.assertEqual(len(board.zones), 2)

    def test_excluded_footprint_children_are_filtered_on_a_copy(self):
        board = drawing_board()
        board.footprints[0].drawings = [
            outline(0.0, 0.0, 1.0, 1.0, layer="Dwgs.User", uuid_value="fp-note"),
            outline(0.0, 0.0, 1.0, 1.0, layer="F.SilkS", uuid_value="fp-silk"),
        ]
        view, _ = replication_content(board, frozenset({"Dwgs.User"}))
        self.assertEqual([d.id for d in view.footprints[0].drawings], ["fp-silk"])
        self.assertEqual(len(board.footprints[0].drawings), 2)

    def test_a_group_never_keeps_a_reference_to_an_excluded_item(self):
        board = drawing_board()
        board.groups = [PcbGroup(id="grp-1", members=["note-1", "track-1"])]
        view, warnings = replication_content(board, frozenset({"Dwgs.User"}))
        self.assertEqual(view.groups[0].members, ["track-1"])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(board.groups[0].members, ["note-1", "track-1"])

    def test_a_group_left_with_no_members_is_dropped(self):
        board = drawing_board()
        board.groups = [PcbGroup(id="grp-1", members=["note-1"])]
        view, warnings = replication_content(board, frozenset({"Dwgs.User"}))
        self.assertEqual(view.groups, [])
        self.assertIn("dropped", warnings[0])


class ExcludedLayerBuildTest(unittest.TestCase):
    def test_the_panel_carries_none_of_the_excluded_content(self):
        outcome = build(drawing_board(), 2, 3, 3.0,
                        exclude_layers=["User.Drawings"])
        layers = [d.layer for d in outcome.board.drawings]
        self.assertNotIn("Dwgs.User", layers)
        self.assertEqual(len(outcome.board.zones), 6)

    def test_the_source_still_has_it_and_the_counts_say_so(self):
        outcome = build(drawing_board(), 2, 3, 3.0,
                        exclude_layers=["User.Drawings"])
        self.assertEqual(outcome.source_content.drawings, 6)
        self.assertEqual(outcome.instance_content.drawings, 5)
        self.assertEqual(outcome.panel_content, outcome.instance_content.times(6))

    def test_the_excluded_layer_is_still_in_the_panels_table(self):
        outcome = build(drawing_board(), 2, 3, 3.0,
                        exclude_layers=["User.Drawings"])
        self.assertIn(
            "Dwgs.User",
            [layer.canonical_name for layer in outcome.board.layers],
        )

    def test_the_stage_reports_what_it_excluded(self):
        outcome = build(drawing_board(), 2, 3, 3.0,
                        exclude_layers=["User.Drawings"])
        self.assertEqual(outcome.excluded_layers, frozenset({"Dwgs.User"}))

    def test_a_name_this_board_does_not_have_warns_and_still_builds(self):
        outcome = build(drawing_board(), 2, 3, 3.0,
                        exclude_layers=["User.Drawings", "User.42"])
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("User.42", outcome.warnings[0])
        self.assertEqual(outcome.excluded_layers, frozenset({"Dwgs.User"}))

    def test_excluding_nothing_replicates_everything(self):
        outcome = build(drawing_board(), 2, 3, 3.0, exclude_layers=[])
        self.assertEqual(outcome.instance_content, outcome.source_content)

    def test_building_twice_with_an_exclusion_gives_the_same_bytes(self):
        first = build(drawing_board(), 2, 3, 3.0, exclude_layers=["User.Drawings"])
        second = build(drawing_board(), 2, 3, 3.0, exclude_layers=["User.Drawings"])
        self.assertEqual(first.board.to_string(), second.board.to_string())


class DimensionSkipTest(unittest.TestCase):
    """No dimension is ever replicated, on any layer, config or none."""

    def test_a_dimension_is_recognised_and_an_outline_is_not(self):
        self.assertTrue(is_dimension(dimension()))
        self.assertFalse(is_dimension(outline(0, 0, 1, 1)))

    def test_the_content_view_leaves_every_dimension_behind(self):
        view, _ = replication_content(dimension_board(), frozenset())
        self.assertEqual([d.id for d in view.drawings if is_dimension(d)], [])

    def test_it_skips_them_with_no_layer_excluded_at_all(self):
        # The dimension rule is unconditional, so an empty exclusion set must
        # not short-circuit back to the unfiltered source.
        board = dimension_board()
        view, _ = replication_content(board, frozenset())
        self.assertIsNot(view, board)
        self.assertEqual(len(view.drawings), 5)

    def test_the_layer_a_dimension_sits_on_is_irrelevant(self):
        board = square_board()
        board.drawings.append(dimension(layer="Edge.Cuts", uuid_value="dim-edge"))
        view, _ = replication_content(board, frozenset())
        self.assertNotIn("dim-edge", [d.id for d in view.drawings])

    def test_the_panel_holds_none_of_them(self):
        outcome = build(dimension_board(), 2, 3, 3.0)
        self.assertEqual(outcome.source_content.dimensions, 2)
        self.assertEqual(outcome.instance_content.dimensions, 0)
        self.assertEqual(outcome.panel_content.dimensions, 0)
        self.assertEqual(
            [d for d in outcome.board.drawings if is_dimension(d)], []
        )

    def test_everything_else_still_replicates(self):
        outcome = build(dimension_board(), 2, 3, 3.0)
        self.assertEqual(outcome.panel_content, outcome.instance_content.times(6))
        self.assertEqual(len(outcome.board.footprints), 6)
        self.assertEqual(len(outcome.board.tracks), 6)

    def test_the_source_board_keeps_its_dimensions(self):
        board = dimension_board()
        build(board, 2, 3, 3.0)
        self.assertEqual(ContentCounts.of(board).dimensions, 2)

    def test_a_group_never_keeps_a_reference_to_a_skipped_dimension(self):
        # A group names its members by uuid. A skipped dimension left in a
        # group would point at nothing.
        board = dimension_board()
        board.groups = [PcbGroup(id="grp-1", members=["dim-1", "track-1"])]
        view, warnings = replication_content(board, frozenset())
        self.assertEqual(view.groups[0].members, ["track-1"])
        self.assertEqual(len(warnings), 1)

    def test_a_dimension_inside_a_footprint_is_skipped_too(self):
        board = dimension_board()
        board.footprints[0].drawings = [dimension(uuid_value="fp-dim")]
        view, _ = replication_content(board, frozenset())
        self.assertEqual(view.footprints[0].drawings, [])
        self.assertEqual(len(board.footprints[0].drawings), 1)

    def test_building_twice_still_gives_the_same_bytes(self):
        first = build(dimension_board(), 2, 3, 3.0).board
        second = build(dimension_board(), 2, 3, 3.0).board
        self.assertEqual(first.to_string(), second.to_string())


class PanelHalfTest(unittest.TestCase):
    """The build panelizes only when the config says how big the sheet is."""

    def routed_board(self):
        """`square_board` with a toolpath round three of its four sides."""
        board = square_board()
        board.layers.append(
            BoardLayer(ordinal=36, canonical_name="User.36", layer_type="user")
        )
        radius = 0.4445
        corners = [(-radius, -radius), (10 + radius, -radius),
                   (10 + radius, 10 + radius), (-radius, 10 + radius)]
        for index in range(3):
            board.drawings.append(PcbLine(
                start=corners[index], end=corners[index + 1], layer="User.36",
                stroke=Stroke(width=2 * radius), id=f"path-{index}",
            ))
        return board

    def test_no_sheet_margin_leaves_every_instance_outline_alone(self):
        outcome = build(self.routed_board(), 2, 1, 3.0)
        self.assertIsNone(outcome.panelize)
        self.assertEqual(len(outline_items(outcome.board)), 8)

    def test_a_sheet_margin_replaces_them_with_one_panel_outline(self):
        outcome = build(self.routed_board(), 2, 1, 3.0, rails=5.0)
        self.assertIsNotNone(outcome.panelize)
        self.assertEqual(outcome.panelize.outlines_removed, 8)
        self.assertEqual(outcome.panelize.blank_extent_mm, (33.0, 20.0))
        # The sheet, plus one hole where each instance was routed.
        self.assertEqual(outcome.panelize.loops, 3)

    def test_the_content_count_still_measures_replication_alone(self):
        outcome = build(self.routed_board(), 2, 1, 3.0, rails=5.0)
        instance = ContentCounts.of(self.routed_board())
        self.assertEqual(outcome.panel_content.footprints,
                         instance.footprints * 2)
        self.assertEqual(outcome.panel_content.drawings, instance.drawings * 2)

    def test_panelizing_twice_still_gives_the_same_bytes(self):
        first = build(self.routed_board(), 2, 1, 3.0, rails=5.0).board
        second = build(self.routed_board(), 2, 1, 3.0, rails=5.0).board
        self.assertEqual(first.to_string(), second.to_string())

    def tabbed_board(self):
        """`routed_board` with a tab footprint hosting a break contour."""
        board = self.routed_board()
        board.footprints.append(Footprint(
            fp_id="PanelTab:MouseBite", layer="F.Cu", uuid="tab-1",
            at={"position": (5.0, 0.0), "angle": 0.0},
            drawings=[
                PcbLine(start=(4.5, 0.0), end=(5.5, 0.0), layer="Edge.Cuts",
                        stroke=Stroke(width=0.05), id="tab-contour"),
                PcbLine(start=(4.5, 0.0), end=(4.5, 0.5), layer="F.SilkS",
                        stroke=Stroke(width=0.05), id="tab-mark"),
            ],
        ))
        return board

    def test_the_panel_carries_no_edge_cuts_the_instances_brought(self):
        # A fabricator reads `Edge.Cuts` as a cut, so the panel keeps none of
        # the source's: board-level and footprint-hosted alike, only the
        # derived outline survives.
        outcome = build(self.tabbed_board(), 2, 1, 3.0, rails=5.0)
        panel = outcome.board
        hosted = [
            item for footprint in panel.footprints
            for item in footprint.drawings
            if "Edge.Cuts" in item_layers(item)
        ]
        self.assertEqual(hosted, [])
        self.assertEqual(outcome.panelize.strip.footprint_items, 2)
        derived = {item.id for item in outline_items(panel)}
        self.assertTrue(derived)
        self.assertEqual(derived & {"tab-contour"}, set())
        # ...and the rest of the tab footprint is untouched.
        marks = [
            item.id for footprint in panel.footprints
            for item in footprint.drawings
            if "F.SilkS" in item_layers(item)
        ]
        self.assertEqual(len(marks), 2)

    def test_the_panel_holds_no_copper_zone_at_all(self):
        outcome = build(self.routed_board(), 2, 1, 3.0, rails=5.0)
        self.assertEqual(outcome.board.zones, [])
        self.assertEqual(
            [zone for fp in outcome.board.footprints for zone in fp.zones], []
        )
        self.assertEqual(outcome.panelize.zones.zones_removed, 2)

    def keepout_board(self):
        """`routed_board` with FOUR tab footprints, each hosting a keepout.

        The corpus shape in miniature: a 2 x 3 panel of this board carries 24
        footprint-hosted rule areas, which is exactly the count and exactly the
        arrangement that collapses onto instance (0, 0) if a footprint's
        zones do not move with it.
        """
        board = self.routed_board()
        for index, position in enumerate(
            [(2.0, 0.0), (8.0, 0.0), (2.0, 10.0), (8.0, 10.0)]
        ):
            board.footprints.append(Footprint(
                fp_id="PanelTab:MouseBite", layer="F.Cu", uuid=f"tab-{index}",
                at={"position": position, "angle": 0.0},
                zones=[Zone(
                    layer="F.Cu", id=f"tab-keepout-{index}",
                    keepout=ZoneKeepout(tracks=False, vias=False, pads=False,
                                        copper_pour=False, footprints=False),
                    # Board coordinates in the file, offset from the placement.
                    polygons=[{"pts": [
                        (position[0] - 0.5, position[1] - 0.5),
                        (position[0] + 0.5, position[1] - 0.5),
                        (position[0] + 0.5, position[1] + 0.5),
                    ]}],
                )],
            ))
        return board

    def test_every_footprint_rule_area_survives_into_the_panel(self):
        outcome = build(self.keepout_board(), 2, 3, 3.0, rails=5.0)
        hosted = [zone for fp in outcome.board.footprints for zone in fp.zones]
        self.assertEqual(len(hosted), 24)
        self.assertEqual(outcome.panelize.zones.footprint_rule_areas, 24)
        self.assertEqual(len({zone.id for zone in hosted}), 24)

    def test_the_24_footprint_rule_areas_land_at_24_distinct_positions(self):
        # If the footprints moved and their zones did not, all 24 keepouts
        # would stack on instance (0, 0). Positions, not counts, prove that
        # the zones move.
        outcome = build(self.keepout_board(), 2, 3, 3.0, rails=5.0)
        anchors = [
            zone_anchor(zone)
            for footprint in outcome.board.footprints
            for zone in footprint.zones
        ]
        self.assertEqual(len(anchors), 24)
        self.assertEqual(len(set(anchors)), 24)

    def test_each_keepout_keeps_its_offset_from_its_own_footprint(self):
        # Correct positions, not merely distinct ones: every replica's zone
        # sits the same distance from its own placement as the source's did.
        def offsets(board):
            return {
                round(zone_anchor(zone)[axis] - fp.at.position[axis], 6)
                for fp in board.footprints for zone in fp.zones
                for axis in (0, 1)
            }

        source = self.keepout_board()
        expected = offsets(source)
        outcome = build(source, 2, 3, 3.0, rails=5.0)
        self.assertEqual(expected, {-0.5})
        self.assertEqual(offsets(outcome.board), expected)

    def test_every_instance_fill_became_copper_on_the_panel(self):
        outcome = build(self.routed_board(), 2, 1, 3.0, rails=5.0)
        flattened = outcome.panelize.zones
        self.assertEqual(flattened.filled_zones, 2)
        self.assertEqual(flattened.polygons, 2)
        polys = [item for item in outcome.board.drawings
                 if type(item).__name__ == "PcbPoly"]
        self.assertEqual(len(polys), 2)
        self.assertEqual({poly.layer for poly in polys}, {"F.Cu"})
        self.assertEqual({poly.net_name for poly in polys},
                         {"AGND_1", "AGND_2"})
        self.assertEqual(len({poly.id for poly in polys}), 2)

    def test_the_source_board_keeps_its_zones_and_its_outline(self):
        board = self.tabbed_board()
        build(board, 2, 1, 3.0, rails=5.0)
        self.assertEqual(len(board.zones), 1)
        self.assertEqual(len(outline_items(board)), 4)
        self.assertEqual(len(board.footprints[-1].drawings), 2)


class SetBoardPropertyTest(unittest.TestCase):
    def test_a_new_name_is_appended(self):
        board = square_board()
        set_board_property(board, "PART_ID", "748")
        self.assertEqual([(p.name, p.value) for p in board.properties],
                         [("PART_ID", "748")])

    def test_an_existing_name_is_replaced_not_duplicated(self):
        board = square_board()
        board.properties = [Property(name="PART_ID", value="old")]
        set_board_property(board, "PART_ID", "new")
        self.assertEqual([(p.name, p.value) for p in board.properties],
                         [("PART_ID", "new")])


if __name__ == "__main__":
    unittest.main()
