"""The prep stage, checked on a board assembled in memory - no file, no KiCad.

What matters here is not the offset arithmetic, which `test_toolpath.py` pins
against closed forms, but the stage's contract: that it owns `User.36` and
therefore repeats itself exactly, that a tab's stop lines arrive in board
coordinates however the footprint is turned, that the cutter is checked against
what each tab declares, and that the toolpath is not one of the layers
step-and-repeat leaves behind.
"""

import math
import unittest

from eedesigncore import (
    Board,
    BoardLayer,
    Footprint,
    PcbArc,
    PcbCircle,
    PcbLine,
    PcbPoint,
    Property,
    Setup,
    item_layers,
)

from eedesignpanels.config import DEFAULT_EXCLUDED_LAYERS, PanelConfig, PrepConfig
from eedesignpanels.stages import route_toolpath
from eedesignpanels.stages.route_toolpath import (
    MIN_ITEM_MM,
    MIN_SAGITTA_MM,
    MOUTH_POINT_LAYER,
    STOP_LINE_LAYER,
    TAB_TOOL_MAX_FIELD,
    TOOLPATH_LAYER,
    boundary_loop,
    bridge_mouths,
    build,
    clear_toolpath,
    mouth_is_continuous,
    mouth_points,
    outline_primitives,
    pair_mouths,
    place_on_board,
    rotate_kicad,
    rout_through,
    string_between,
    tabs_of,
)
from eedesignpanels.toolpath import Arc, Line, OffsetError, arc_from_three_points

CUTTER = 0.889
RADIUS = CUTTER / 2.0
STOP_LENGTH = 0.6  # the regenerated tab footprint's stop line


def edge(x0, y0, x1, y1, layer="Edge.Cuts", uuid_value=None):
    return PcbLine(
        start=(x0, y0), end=(x1, y1), layer=layer,
        id=uuid_value or f"e-{x0}-{y0}-{x1}-{y1}",
    )


def staple(centre_x=0.0, half_gap=0.41, length=STOP_LENGTH, tag="s"):
    """One staple in footprint-local coordinates: base plus two legs.

    The base lies on the board edge (local y = 0) and the legs stand outward
    from its ends, which is the shape the tab generator draws on `User.8`.
    """
    left, right = centre_x - half_gap, centre_x + half_gap
    return [
        PcbLine(start=(left, 0.0), end=(left, -length),
                layer=STOP_LINE_LAYER, id=f"{tag}-leg0"),
        PcbLine(start=(left, 0.0), end=(right, 0.0),
                layer=STOP_LINE_LAYER, id=f"{tag}-base"),
        PcbLine(start=(right, 0.0), end=(right, -length),
                layer=STOP_LINE_LAYER, id=f"{tag}-leg1"),
    ]


def mouth_point(x, y, tag="m"):
    """One `(point ...)` on `User.8`, in BOARD coordinates - see MouthPoint."""
    return PcbPoint(position=(x, y), size=1.0, layer=MOUTH_POINT_LAYER,
                    id=f"pt-{tag}")


def tab_footprint(position=(5.0, 0.0), angle=0.0, tool_max="0.8923",
                  uuid_value="tab-1", half_gap=0.41, length=STOP_LENGTH,
                  staples=None, points=None):
    """A tab footprint carrying one staple, or as many as asked for."""
    properties = [Property(name="Reference", value="TAB**", id=f"{uuid_value}-ref")]
    if tool_max is not None:
        properties.append(Property(
            name=TAB_TOOL_MAX_FIELD, value=tool_max, id=f"{uuid_value}-max"
        ))
    if staples is None:
        staples = staple(0.0, half_gap, length, tag=uuid_value)
    return Footprint(
        fp_id="PanelTab:Panel_Tab_test",
        layer="F.Cu",
        uuid=uuid_value,
        at={"position": position, "angle": angle},
        properties=properties,
        drawings=list(staples),
        points=list(points or []),
    )


# The corpus tab's mouth: the contour meets the ideal edge 0.8562 mm either side
# of the tab origin.
MOUTH_HALF = 0.8562


def contour(centre_x=0.0, half=MOUTH_HALF, depth=0.1667, tag="c"):
    """A break contour spanning a tab's mouth, in footprint-local coordinates.

    It dips INTO the board, which is what makes it unroutable at any cutter and
    is the whole reason the toolpath wants the ideal edge instead.
    """
    left, right = centre_x - half, centre_x + half
    inner = half * 0.389
    return [
        PcbLine(start=(left, 0.0), end=(centre_x - inner, depth),
                layer="Edge.Cuts", id=f"{tag}-in"),
        PcbLine(start=(centre_x - inner, depth), end=(centre_x + inner, depth),
                layer="Edge.Cuts", id=f"{tag}-flat"),
        PcbLine(start=(centre_x + inner, depth), end=(right, 0.0),
                layer="Edge.Cuts", id=f"{tag}-out"),
    ]


def contoured_board(side=10.0, declare=True, centre_x=5.0):
    """A square whose top edge dead-ends at one tab's contoured mouth.

    This is the board-drawing method the mouth points exist for: board-level
    `Edge.Cuts` runs up to the mouth and stops, the tab footprint carries the
    contour across it, and the two points say where the ideal edge resumes.
    `declare=False` leaves the points off, which is the same board drawn by a
    designer who has not declared its mouth.
    """
    points = (
        [mouth_point(centre_x - MOUTH_HALF, 0.0, "left"),
         mouth_point(centre_x + MOUTH_HALF, 0.0, "right")]
        if declare else []
    )
    board = square_board(side=side, tabs=[tab_footprint(
        position=(centre_x, 0.0), points=points,
        staples=staple(0.0, tag="tab-1") + contour(tag="tab-1"),
    )])
    board.drawings[0] = edge(0.0, 0.0, centre_x - MOUTH_HALF, 0.0,
                             uuid_value="top-left")
    board.drawings.insert(1, edge(centre_x + MOUTH_HALF, 0.0, side, 0.0,
                                  uuid_value="top-right"))
    return board


def square_board(side: float = 10.0, tabs=None) -> Board:
    """A square outline with the toolpath layer declared, plus any tabs.

    The outline runs clockwise in stored numbers so the interior is y > 0 near
    the top edge, which is where the tabs sit.
    """
    board = Board(
        version=20260206,
        generator="pcbnew",
        generator_version="10.0",
        layers=[
            BoardLayer(ordinal=0, canonical_name="F.Cu", layer_type="signal"),
            BoardLayer(ordinal=25, canonical_name="Edge.Cuts", layer_type="user"),
            BoardLayer(ordinal=53, canonical_name="User.8", layer_type="user",
                       user_name="User.8-PanelEdgeCuts"),
            BoardLayer(ordinal=109, canonical_name=TOOLPATH_LAYER,
                       layer_type="user", user_name="User.36-RouteToolPath"),
        ],
        setup=Setup(pad_to_mask_clearance=0.1),
    )
    board.drawings = [
        edge(0.0, 0.0, side, 0.0),
        edge(side, 0.0, side, side),
        edge(side, side, 0.0, side),
        edge(0.0, side, 0.0, 0.0),
    ]
    board.footprints = list(tabs or [])
    return board


def toolpath_items(board: Board) -> list:
    return [
        item
        for item in (*board.drawings, *board.trailing_drawings)
        if TOOLPATH_LAYER in item_layers(item)
    ]


class RotateKicadTest(unittest.TestCase):
    def test_the_quadrants_take_an_exact_branch(self):
        self.assertEqual(rotate_kicad((1.0, 2.0), 0.0), (1.0, 2.0))
        self.assertEqual(rotate_kicad((1.0, 2.0), 90.0), (2.0, -1.0))
        self.assertEqual(rotate_kicad((1.0, 2.0), 180.0), (-1.0, -2.0))
        self.assertEqual(rotate_kicad((1.0, 2.0), 270.0), (-2.0, 1.0))

    def test_a_negative_angle_normalises(self):
        self.assertEqual(rotate_kicad((1.0, 2.0), -90.0), (-2.0, 1.0))

    def test_a_general_angle_agrees_with_the_quadrant_branch(self):
        turned = rotate_kicad((1.0, 0.0), 45.0)
        self.assertAlmostEqual(turned[0], math.sqrt(0.5))
        self.assertAlmostEqual(turned[1], -math.sqrt(0.5))


class TabsOfTest(unittest.TestCase):
    def test_a_footprint_with_no_stop_lines_is_not_a_tab(self):
        board = square_board()
        board.footprints = [Footprint(fp_id="R", layer="F.Cu", uuid="r1",
                                      at={"position": (1.0, 1.0), "angle": 0.0})]
        self.assertEqual(tabs_of(board), [])

    def test_stop_lines_arrive_in_board_coordinates(self):
        board = square_board(tabs=[tab_footprint(position=(5.0, 0.0))])
        feet = sorted(s.p0[0] for s in tabs_of(board)[0].stop_lines)
        self.assertAlmostEqual(feet[0], 4.59)
        self.assertAlmostEqual(feet[1], 5.41)
        for stop in tabs_of(board)[0].stop_lines:
            # Stored foot first, so a leg always runs away from the edge.
            self.assertAlmostEqual(stop.p0[1], 0.0)
            self.assertAlmostEqual(stop.p1[1], -STOP_LENGTH)

    def test_the_tab_origin_is_the_base_midpoint(self):
        board = square_board(tabs=[tab_footprint(position=(5.0, 0.0))])
        tab = tabs_of(board)[0]
        self.assertAlmostEqual(tab.position[0], 5.0)
        self.assertAlmostEqual(tab.position[1], 0.0)

    def test_one_footprint_may_define_several_staples(self):
        # The tab count is the STAPLE count, not the footprint count.
        board = square_board(tabs=[tab_footprint(
            position=(5.0, 0.0),
            staples=staple(-2.0, tag="a") + staple(2.0, tag="b"),
        )])
        tabs = tabs_of(board)
        self.assertEqual(len(tabs), 2)
        self.assertEqual(
            sorted(round(tab.position[0], 3) for tab in tabs), [3.0, 7.0]
        )

    def test_every_staple_of_one_footprint_takes_its_cutter_ceiling(self):
        board = square_board(tabs=[tab_footprint(
            position=(5.0, 0.0), tool_max="0.75",
            staples=staple(-2.0, tag="a") + staple(2.0, tag="b"),
        )])
        self.assertEqual(
            [tab.tool_max_mm for tab in tabs_of(board)], [0.75, 0.75]
        )

    def test_a_segment_belonging_to_no_staple_is_reported_not_used(self):
        board = square_board(tabs=[tab_footprint(position=(5.0, 0.0))])
        board.footprints[0].drawings.append(
            PcbLine(start=(3.0, -2.0), end=(3.5, -2.0),
                    layer=STOP_LINE_LAYER, id="orphan")
        )
        warnings = []
        tabs = tabs_of(board, warnings)
        self.assertEqual(len(tabs), 1)
        self.assertTrue(any("belong to no staple" in w for w in warnings))

    def test_a_turned_tab_turns_its_whole_staple_with_it(self):
        board = square_board(tabs=[tab_footprint(position=(5.0, 10.0), angle=180.0)])
        tab = tabs_of(board)[0]
        self.assertAlmostEqual(tab.position[1], 10.0)
        for stop in tab.stop_lines:
            # A tab on the far edge points its legs the other way, still outward.
            self.assertAlmostEqual(stop.p1[1], 10.0 + STOP_LENGTH)

    def test_the_declared_cutter_ceiling_is_read(self):
        board = square_board(tabs=[tab_footprint()])
        self.assertAlmostEqual(tabs_of(board)[0].tool_max_mm, 0.8923)

    def test_a_tab_with_no_ceiling_field_reads_none(self):
        board = square_board(tabs=[tab_footprint(tool_max=None)])
        self.assertIsNone(tabs_of(board)[0].tool_max_mm)


class OutlineTest(unittest.TestCase):
    def test_a_board_with_no_outline_is_an_error(self):
        board = square_board()
        board.drawings = []
        with self.assertRaises(ValueError):
            outline_primitives(board)

    def test_a_shape_the_stage_cannot_offset_is_an_error_not_a_guess(self):
        board = square_board()
        board.drawings.append(PcbArc(
            start=(0.0, 0.0), mid=(1.0, 1.0), end=(2.0, 0.0),
            layer="Edge.Cuts", id="arc-1",
        ))
        # An arc is fine; a polygon would not be. Check the arc path works and
        # the message names the type for anything else.
        self.assertEqual(len(outline_primitives(board)), 5)


class PlaceOnBoardTest(unittest.TestCase):
    """The footprint-child transform, checked against KiCad's own arithmetic.

    KiCad's parser turns a footprint child about the origin by the footprint
    orientation and then moves it by the footprint position
    (`pcb_io_kicad_sexpr_parser.cpp:3609-3613`); the turn is `RotatePoint`
    (`libs/kimath/src/trigo.cpp:229-264`), which is `x' = y sin + x cos`,
    `y' = y cos - x sin` on a Y-DOWN axis, and which rounds to the nanometre
    grid. Getting the sign of that wrong turns a tab contour inside out, so it
    is pinned rather than assumed.
    """

    def test_a_quarter_turn_matches_kicad_rotate_point(self):
        self.assertEqual(place_on_board((1.0, 2.0), (0.0, 0.0), 90.0), (2.0, -1.0))
        self.assertEqual(place_on_board((1.0, 2.0), (0.0, 0.0), 270.0), (-2.0, 1.0))

    def test_a_half_turn_is_exact_and_carries_the_position(self):
        self.assertEqual(
            place_on_board((0.8562, 0.0), (200.842984, 133.771285), 180.0),
            (199.986784, 133.771285),
        )

    def test_an_unturned_child_is_just_moved(self):
        self.assertEqual(
            place_on_board((-0.8562, 0.0), (200.842984, 123.271289), 0.0),
            (199.986784, 123.271289),
        )

    def test_a_general_angle_lands_on_the_nanometre_grid(self):
        # KiCad rounds every rotated coordinate with KiROUND, so a placed
        # contour lands on exactly the coordinates a board-level line drawn up
        # to it would carry - no sub-nanometre dust in the join.
        turned = place_on_board((1.0, 0.0), (10.0, 20.0), 45.0)
        self.assertEqual(turned, (10.707107, 19.292893))


class FlattenOutlineTest(unittest.TestCase):
    """The outline is board-level Edge.Cuts PLUS every footprint's, in memory.

    A board can be drawn two ways: define
    the outline and its tabs in one footprint, or define each tab as a footprint
    and stitch them together with board-level lines. Neither is a loop on its
    own, so the stage assembles both - and never writes the result back.
    """

    def footprint_square(self, side=10.0, position=(0.0, 0.0), angle=0.0,
                         uuid_value="outline-fp"):
        """The same square, drawn inside a footprint at *position* and *angle*."""
        local = []
        corners = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
        for index in range(4):
            start, end = corners[index], corners[(index + 1) % 4]
            back = rotate_kicad(
                (start[0] - position[0], start[1] - position[1]), -angle
            )
            ahead = rotate_kicad(
                (end[0] - position[0], end[1] - position[1]), -angle
            )
            local.append(PcbLine(
                start=back, end=ahead, layer="Edge.Cuts", id=f"fp-edge-{index}"
            ))
        return Footprint(
            fp_id="PanelTab:Outline", layer="F.Cu", uuid=uuid_value,
            at={"position": position, "angle": angle}, drawings=local,
        )

    def test_a_footprint_hosted_outline_is_read_into_board_coordinates(self):
        board = square_board()
        board.drawings = []
        board.footprints = [self.footprint_square()]
        primitives = outline_primitives(board)
        self.assertEqual(len(primitives), 4)
        corners = sorted(
            (round(p.p0[0], 6), round(p.p0[1], 6)) for p in primitives
        )
        self.assertEqual(
            corners, [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0)]
        )

    def test_a_turned_footprint_puts_its_outline_back_where_it_belongs(self):
        # Same square, defined in a footprint turned through each quarter and
        # placed to land in the same place. The toolpath must not notice.
        reference = build(square_board(tabs=[tab_footprint()]), CUTTER)
        for angle in (0.0, 90.0, 180.0, 270.0):
            board = square_board(tabs=[tab_footprint()])
            board.drawings = []
            board.footprints.append(
                self.footprint_square(position=(3.0, 4.0), angle=angle)
            )
            result = build(board, CUTTER)
            self.assertEqual(
                len(result.run_lengths), len(reference.run_lengths),
                f"{angle} deg",
            )
            for got, want in zip(result.run_lengths, reference.run_lengths):
                self.assertAlmostEqual(got, want, places=9, msg=f"{angle} deg")

    def test_a_footprint_arc_keeps_its_radius_and_its_direction_when_turned(self):
        # Three points transformed one at a time is exact and orientation-safe;
        # a centre and a sweep would not be.
        board = square_board()
        board.drawings = []
        board.footprints = [Footprint(
            fp_id="PanelTab:Arc", layer="F.Cu", uuid="arc-fp",
            at={"position": (5.0, 5.0), "angle": 90.0},
            drawings=[PcbArc(start=(1.0, 0.0), mid=(0.0, 1.0), end=(-1.0, 0.0),
                             layer="Edge.Cuts", id="fp-arc")],
        )]
        arc = outline_primitives(board)[0]
        self.assertAlmostEqual(arc.radius, 1.0, places=12)
        self.assertAlmostEqual(arc.center[0], 5.0, places=9)
        self.assertAlmostEqual(arc.center[1], 5.0, places=9)
        # (0, 1) turned by 90 degrees on a Y-down axis is (1, 0).
        self.assertAlmostEqual(arc.mid[0], 6.0, places=9)
        self.assertAlmostEqual(arc.mid[1], 5.0, places=9)

    def test_both_ways_of_drawing_the_same_board_give_the_same_toolpath(self):
        # Method 1: the whole outline at board level. Method 2: two of its
        # edges moved into a footprint. Same board, same cut.
        whole = build(square_board(tabs=[tab_footprint()]), CUTTER)

        split = square_board(tabs=[tab_footprint()])
        moved = split.drawings[2:]
        split.drawings = split.drawings[:2]
        split.footprints.append(Footprint(
            fp_id="PanelTab:Half", layer="F.Cu", uuid="half-fp",
            at={"position": (2.0, 3.0), "angle": 0.0},
            drawings=[
                PcbLine(
                    start=(item.start[0] - 2.0, item.start[1] - 3.0),
                    end=(item.end[0] - 2.0, item.end[1] - 3.0),
                    layer="Edge.Cuts", id=f"moved-{index}",
                )
                for index, item in enumerate(moved)
            ],
        ))
        halves = build(split, CUTTER)
        self.assertEqual(halves.outline_items, whole.outline_items)
        self.assertEqual(len(halves.items), len(whole.items))
        for got, want in zip(halves.run_lengths, whole.run_lengths):
            self.assertAlmostEqual(got, want, places=9)

    def test_a_shape_with_no_extent_at_all_is_named_wherever_it_lives(self):
        # Any shape KiCad accepts on `Edge.Cuts` - line,
        # arc, rectangle, circle, polygon, curve - is read here too. What KiCad
        # itself rejects is a shape with no extent - a zero-radius circle - and
        # that is the one case left to name.
        board = square_board()
        board.footprints = [Footprint(
            fp_id="PanelTab:Bad", layer="F.Cu", uuid="bad-fp",
            at={"position": (0.0, 0.0), "angle": 0.0},
            drawings=[PcbCircle(center=(1.0, 1.0), end=(1.0, 1.0),
                                layer="Edge.Cuts", id="fp-circle")],
        )]
        with self.assertRaises(ValueError) as caught:
            outline_primitives(board)
        self.assertIn("fp-circle", str(caught.exception))

    def test_the_stage_never_moves_a_footprint_item(self):
        # The flatten is in memory. The file keeps its footprints.
        board = square_board(tabs=[tab_footprint()])
        board.footprints.append(self.footprint_square(position=(3.0, 4.0)))
        before = [
            [(type(item).__name__, item.id) for item in footprint.drawings]
            for footprint in board.footprints
        ]
        build(board, CUTTER)
        after = [
            [(type(item).__name__, item.id) for item in footprint.drawings]
            for footprint in board.footprints
        ]
        self.assertEqual(before, after)
        self.assertEqual(
            [item.id for item in board.drawings if "Edge.Cuts" in item_layers(item)],
            [f"e-{x}" for x in ("0.0-0.0-10.0-0.0", "10.0-0.0-10.0-10.0",
                                "10.0-10.0-0.0-10.0", "0.0-10.0-0.0-0.0")],
        )


class MouthPointTest(unittest.TestCase):
    """The two `User.8` POINTS that say where a tab's ideal edge resumes.

    A point is a `(point ...)` item, not a segment, so nothing has to classify
    geometry to tell a mouth marker from a stop line. And a footprint-hosted
    one's `(at ...)` is stored in BOARD coordinates: KiCad's formatter prints
    `PCB_POINT::GetPosition()` with no footprint frame applied
    (`pcb_io_kicad_sexpr.cpp:1147`, 10.0.3), unlike the `fp_line` beside it.
    Getting that wrong would put every mouth a footprint-position away from the
    edge it belongs to, so it is pinned here.
    """

    def test_a_hosted_point_is_read_in_board_coordinates_not_local_ones(self):
        board = square_board(tabs=[tab_footprint(
            position=(5.0, 0.0), points=[mouth_point(4.1438, 0.0, "left")]
        )])
        found = mouth_points(board)
        self.assertEqual([point.position for point in found], [(4.1438, 0.0)])

    def test_a_turned_footprint_does_not_turn_its_points(self):
        # Same stored position, footprint at another place and another angle.
        # A transform applied here would move it; none is.
        board = square_board(tabs=[tab_footprint(
            position=(200.842984, 133.771285), angle=180.0,
            points=[mouth_point(4.1438, 0.0, "left")],
        )])
        self.assertEqual(mouth_points(board)[0].position, (4.1438, 0.0))

    def test_a_board_level_point_counts_too(self):
        # Membership is not what pairs a point to a tab; position is.
        board = square_board(tabs=[tab_footprint()])
        board.points = [mouth_point(4.1438, 0.0, "loose")]
        self.assertEqual(len(mouth_points(board)), 1)

    def test_a_point_on_another_layer_is_not_a_mouth_marker(self):
        board = square_board(tabs=[tab_footprint()])
        board.points = [
            PcbPoint(position=(4.1438, 0.0), size=1.0, layer="F.Cu", id="stray")
        ]
        self.assertEqual(mouth_points(board), [])

    def test_the_stop_lines_are_still_segments_and_still_found(self):
        board = contoured_board()
        self.assertEqual(len(tabs_of(board)[0].stop_lines), 2)
        self.assertEqual(len(mouth_points(board)), 2)


class PairMouthTest(unittest.TestCase):
    """A point belongs to the staple whose base line it sits on, nearest per side."""

    def test_a_tab_takes_the_point_on_each_side_of_its_base(self):
        board = contoured_board()
        tabs = pair_mouths(tabs_of(board), mouth_points(board), [])
        self.assertEqual(
            [point.position for point in tabs[0].mouth],
            [(5.0 - MOUTH_HALF, 0.0), (5.0 + MOUTH_HALF, 0.0)],
        )

    def test_two_staples_on_one_line_each_take_their_own_pair(self):
        # 1:many, the way the corpus board will grow: several tabs along one
        # straight edge, every point on the same supporting line. Nearest per
        # side is what keeps them apart.
        board = square_board(tabs=[tab_footprint(
            position=(5.0, 0.0),
            staples=staple(-2.0, tag="a") + staple(2.0, tag="b"),
            points=[
                mouth_point(3.0 - MOUTH_HALF, 0.0, "a-left"),
                mouth_point(3.0 + MOUTH_HALF, 0.0, "a-right"),
                mouth_point(7.0 - MOUTH_HALF, 0.0, "b-left"),
                mouth_point(7.0 + MOUTH_HALF, 0.0, "b-right"),
            ],
        )])
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        self.assertEqual(warnings, [])
        got = sorted(
            tuple(round(point.position[0], 4) for point in tab.mouth)
            for tab in tabs
        )
        self.assertEqual(got, [
            (round(3.0 - MOUTH_HALF, 4), round(3.0 + MOUTH_HALF, 4)),
            (round(7.0 - MOUTH_HALF, 4), round(7.0 + MOUTH_HALF, 4)),
        ])

    def test_a_point_on_no_base_line_is_reported_and_ignored(self):
        board = square_board(tabs=[tab_footprint()])
        board.points = [mouth_point(3.0, 4.0, "adrift")]
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        self.assertEqual(tabs[0].mouth, ())
        self.assertEqual(len(warnings), 1)
        self.assertIn("3.0000, 4.0000", warnings[0])

    def test_one_point_of_two_is_reported_and_the_mouth_left_alone(self):
        board = square_board(tabs=[tab_footprint(
            points=[mouth_point(5.0 - MOUTH_HALF, 0.0, "lonely")]
        )])
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        self.assertEqual(tabs[0].mouth, ())
        self.assertTrue(any("only one of its two" in w for w in warnings))

    def test_a_tab_with_no_points_keeps_an_empty_mouth(self):
        board = square_board(tabs=[tab_footprint()])
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        self.assertEqual(tabs[0].mouth, ())
        self.assertEqual(warnings, [])


class BridgeMouthTest(unittest.TestCase):
    """The outline is bridged STRAIGHT between a tab's two points."""

    def prepared(self, declare=True):
        board = contoured_board(declare=declare)
        drawn = outline_primitives(board)
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        kept, mouths = bridge_mouths(drawn, tabs, warnings)
        return drawn, kept, mouths, warnings

    def test_the_contour_is_replaced_by_one_straight_chord(self):
        drawn, kept, mouths, warnings = self.prepared()
        self.assertEqual(warnings, [])
        # Three contour items out, one chord in.
        self.assertEqual(len(kept), len(drawn) - 3 + 1)
        chord = [piece for piece in kept if piece.role == "mouth"]
        self.assertEqual(len(chord), 1)
        self.assertEqual(chord[0].p0, (5.0 - MOUTH_HALF, 0.0))
        self.assertEqual(chord[0].p1, (5.0 + MOUTH_HALF, 0.0))

    def test_the_leftovers_are_counted_and_attributed_to_the_tab(self):
        _, _, mouths, _ = self.prepared()
        self.assertEqual(len(mouths), 1)
        self.assertEqual(mouths[0].leftovers, 3)
        self.assertEqual(mouths[0].tab, 0)
        self.assertAlmostEqual(mouths[0].width_mm, 2 * MOUTH_HALF, places=12)
        self.assertGreater(mouths[0].leftover_mm, mouths[0].width_mm)

    def test_the_chord_identity_is_derived_from_the_two_points(self):
        # Nothing is minted, so two runs over the same board bridge the same way.
        _, kept, _, _ = self.prepared()
        chord = next(piece for piece in kept if piece.role == "mouth")
        self.assertEqual(chord.source, "mouth:pt-left>pt-right")

    def test_the_string_between_two_points_is_the_short_way_round(self):
        board = contoured_board()
        drawn = outline_primitives(board)
        order = string_between(
            drawn, (5.0 - MOUTH_HALF, 0.0), (5.0 + MOUTH_HALF, 0.0)
        )
        self.assertEqual(len(order), 3)
        self.assertEqual(
            sorted(drawn[index].source for index in order),
            ["tab-1-flat", "tab-1-in", "tab-1-out"],
        )

    def test_a_mouth_that_is_already_ideal_edge_gets_no_second_chord(self):
        # Declaring points on a straight mouth must not put two items on the
        # same run of edge - that outline would branch instead of closing.
        board = square_board(tabs=[tab_footprint(points=[
            mouth_point(5.0 - MOUTH_HALF, 0.0, "left"),
            mouth_point(5.0 + MOUTH_HALF, 0.0, "right"),
        ])])
        drawn = outline_primitives(board)
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        kept, mouths = bridge_mouths(drawn, tabs, warnings)
        self.assertEqual(len(kept), len(drawn))
        self.assertFalse(mouths[0].bridged)
        self.assertEqual(warnings, [])

    def test_a_dangling_mouth_is_closed_by_the_bridge_and_said_so(self):
        # No contour at all: the outline stops at each point. The chord is the
        # only thing that closes the loop, and that is worth one line.
        board = contoured_board()
        board.footprints[0].drawings = [
            item for item in board.footprints[0].drawings
            if "Edge.Cuts" not in item_layers(item)
        ]
        drawn = outline_primitives(board)
        warnings = []
        tabs = pair_mouths(tabs_of(board), mouth_points(board), warnings)
        kept, mouths = bridge_mouths(drawn, tabs, warnings)
        self.assertEqual(len(kept), len(drawn) + 1)
        self.assertTrue(mouths[0].bridged)
        self.assertEqual(mouths[0].leftovers, 0)
        self.assertTrue(any("runs nowhere between" in w for w in warnings))

    def test_a_string_longer_than_half_the_outline_is_the_board_not_a_contour(self):
        # The dangling mouth has exactly one run between its two points, and it
        # is the whole rest of the board. Taking it would leave the outline as
        # the chord alone.
        board = contoured_board()
        board.footprints[0].drawings = [
            item for item in board.footprints[0].drawings
            if "Edge.Cuts" not in item_layers(item)
        ]
        drawn = outline_primitives(board)
        self.assertEqual(
            string_between(drawn, (5.0 - MOUTH_HALF, 0.0),
                           (5.0 + MOUTH_HALF, 0.0)),
            [],
        )

    def test_two_tabs_claiming_one_item_is_an_error_naming_both(self):
        # A defensive check on the one thing bridging must never do: hand the
        # same outline item to two tabs. Pairing keeps two staples off one
        # another's points, so this is reached by handing `bridge_mouths` two
        # tabs that were never paired - which is exactly what it guards.
        board = contoured_board()
        drawn = outline_primitives(board)
        first = pair_mouths(tabs_of(board), mouth_points(board), [])[0]
        second = first._replace(label="TWIN", owner="TWIN")
        with self.assertRaises(ValueError) as caught:
            bridge_mouths(drawn, [first, second], [])
        self.assertIn("both", str(caught.exception))
        self.assertIn("TWIN", str(caught.exception))

    def test_an_unattributable_leftover_is_still_a_hard_error_with_a_location(self):
        # Bridging removes what a tab accounts for. Anything else left over means
        # the outline is not the loop it looks like, and that is not a warning.
        board = contoured_board()
        board.drawings.append(edge(2.0, 5.0, 3.0, 5.0, uuid_value="stray"))
        with self.assertRaises(OffsetError) as caught:
            build(board, CUTTER)
        self.assertIn("left over", str(caught.exception))
        self.assertIsNotNone(caught.exception.at)


class SeparateLoopTest(unittest.TestCase):
    """A second closed shape on `Edge.Cuts` is reported, never a refusal.

    THE BUG THIS PINS: the stage handed every `Edge.Cuts` primitive
    to a chainer written for ONE loop. It walked the boundary, came back to
    where it started, and then called every item of every other loop a
    leftover - so a board with four 1.3 mm cutouts was refused at the vertex
    where its boundary CLOSED, a vertex that measures perfect. Each circle
    reads as two half-arcs (`outline_shapes._circle`), which is why the count
    in that refusal was eight.
    """

    def circled_board(self, count: int = 4):
        board = square_board()
        for index in range(count):
            board.drawings.append(PcbCircle(
                center=(2.0 + index, 5.0), end=(2.65 + index, 5.0),
                layer="Edge.Cuts", id=f"hole-{index}",
            ))
        return board

    def test_four_cutouts_are_not_a_broken_outline(self):
        made = build(self.circled_board(), CUTTER)
        named = [w for w in made.warnings if "a job of its own" in w]
        self.assertEqual(len(named), 4)
        self.assertGreater(len(made.run_lengths), 0)

    def test_the_cutouts_are_reported_in_the_order_they_are_drawn(self):
        made = build(self.circled_board(), CUTTER)
        named = [w for w in made.warnings if "a job of its own" in w]
        self.assertEqual(
            [w.split("centred at ")[1].split(",")[0] for w in named],
            ["(2.0000", "(3.0000", "(4.0000", "(5.0000"],
        )

    def test_every_loop_is_a_routing_job(self):
        # Each closed loop becomes its own toolpath job.
        made = build(self.circled_board(), CUTTER)
        self.assertEqual(len(made.jobs), 5)
        self.assertEqual(sum(1 for job in made.jobs if job.is_boundary), 1)
        self.assertTrue(all(job.made for job in made.jobs))

    def test_a_cuttable_hole_gets_a_channel_of_its_own(self):
        plain = build(square_board(), CUTTER)
        holed = build(self.circled_board(count=1), CUTTER)
        self.assertGreater(len(holed.items), len(plain.items))

    def test_a_hole_smaller_than_the_cutter_is_reported_not_skipped(self):
        # An FPC case: 1.3 mm holes against a bit that will not go in them.
        made = build(self.circled_board(count=1), 2.0)
        self.assertEqual(len(made.uncuttable), 1)
        self.assertEqual(
            [w for w in made.warnings if w.startswith("CANNOT BE CUT")].__len__(), 1)
        self.assertIn("1.3000 by 1.3000 mm", made.uncuttable[0].where)

    def test_a_hole_the_cutter_cannot_make_leaves_the_board_items_alone(self):
        plain = build(square_board(), 2.0)
        holed = build(self.circled_board(count=1), 2.0)
        self.assertEqual(len(holed.items), len(plain.items))

    def test_the_cutter_still_follows_the_boundary(self):
        plain = build(square_board(), CUTTER)
        with_holes = build(self.circled_board(), CUTTER)
        self.assertEqual(with_holes.run_lengths, plain.run_lengths)

    def test_a_cutout_is_named_by_its_size_and_where_it_is(self):
        warnings = []
        boundary_loop(
            outline_primitives(self.circled_board(count=1)), warnings, CUTTER)
        self.assertIn("1.3000 by 1.3000 mm", warnings[0])
        self.assertIn("(2.0000, 5.0000) mm", warnings[0])

    def test_a_cutout_is_named_a_hole_in_the_board(self):
        # The parity count in `material.loop_is_hole` decides that the shape
        # is a hole in the board, so the message can name it.
        warnings = []
        boundary_loop(
            outline_primitives(self.circled_board(count=1)), warnings, CUTTER)
        self.assertIn("form a hole in the board", warnings[0])

    def test_a_hole_is_routed_from_inside_it(self):
        # WHICH SIDE IS MATERIAL DECIDES WHERE THE TOOL RIDES.
        # A hole is cut from the air inside it; the boundary is cut
        # from the air outside the board.
        made = build(self.circled_board(count=1), CUTTER)
        hole = [job for job in made.jobs if not job.is_boundary][0]
        self.assertTrue(hole.rides_inside)
        self.assertFalse(made.jobs[0].rides_inside)

    def test_a_second_piece_beside_the_board_is_named_a_separate_piece(self):
        board = square_board()
        for x0, y0, x1, y1 in (
            (20.0, 0.0, 26.0, 0.0), (26.0, 0.0, 26.0, 6.0),
            (26.0, 6.0, 20.0, 6.0), (20.0, 6.0, 20.0, 0.0),
        ):
            board.drawings.append(edge(x0, y0, x1, y1))
        warnings = []
        boundary_loop(outline_primitives(board), warnings, CUTTER)
        self.assertEqual(len(warnings), 1)
        self.assertIn("form a separate piece", warnings[0])

    def test_a_separate_piece_is_routed_from_outside_it(self):
        board = square_board()
        for x0, y0, x1, y1 in (
            (20.0, 0.0, 26.0, 0.0), (26.0, 0.0, 26.0, 6.0),
            (26.0, 6.0, 20.0, 6.0), (20.0, 6.0, 20.0, 0.0),
        ):
            board.drawings.append(edge(x0, y0, x1, y1))
        piece = [job for job in build(board, CUTTER).jobs
                 if not job.is_boundary][0]
        self.assertFalse(piece.rides_inside)
        self.assertEqual(piece.label, "piece 1")

    def test_the_boundary_is_the_largest_loop_whatever_the_order(self):
        board = self.circled_board(count=1)
        board.drawings.insert(0, board.drawings.pop())
        loop = boundary_loop(outline_primitives(board), [], CUTTER)
        self.assertEqual(len(loop), 4)

    def test_the_same_board_reports_the_same_cutouts_in_the_same_order(self):
        first = build(self.circled_board(), CUTTER).warnings
        second = build(self.circled_board(), CUTTER).warnings
        self.assertEqual(first, second)


class MouthContinuityTest(unittest.TestCase):
    """A contoured mouth with no points is reported; a straight one is not."""

    def test_a_straight_mouth_reads_as_continuous(self):
        board = square_board(tabs=[tab_footprint()])
        tab = tabs_of(board)[0]
        self.assertTrue(mouth_is_continuous(outline_primitives(board), tab))

    def test_a_contoured_mouth_does_not(self):
        board = contoured_board(declare=False)
        tab = tabs_of(board)[0]
        self.assertFalse(mouth_is_continuous(outline_primitives(board), tab))

    def test_a_contoured_mouth_without_points_warns_with_its_position(self):
        made = build(contoured_board(declare=False), CUTTER)
        note = [w for w in made.warnings if "declares no" in w]
        self.assertEqual(len(note), 1)
        self.assertIn("5.0000, 0.0000", note[0])
        # The check reports, it does not block, and the board still routes.
        self.assertTrue(made.items)

    def test_declaring_the_mouth_silences_it_and_changes_the_geometry(self):
        made = build(contoured_board(declare=True), CUTTER)
        self.assertEqual(made.warnings, [])
        self.assertEqual(len(made.mouths), 1)
        self.assertAlmostEqual(made.bridges[0].material_mm, 0.82, places=9)

    def test_a_continuous_outline_board_needs_no_points_at_all(self):
        # Both construction methods stay valid: this one has never had points
        # and must keep building exactly as it did.
        made = build(square_board(tabs=[tab_footprint()]), CUTTER)
        self.assertEqual(made.warnings, [])
        self.assertEqual(made.mouths, [])
        self.assertAlmostEqual(made.bridges[0].material_mm, 0.82, places=9)


class RoutThroughGuardTest(unittest.TestCase):
    """The last check: the swept cut must not reach a staple base's interior.

    It reads the RESULT rather than the reasoning, so it catches a tab the
    router drives through however that came about.
    """

    def test_a_correct_toolpath_clears_every_base(self):
        made = build(contoured_board(), CUTTER)
        self.assertEqual(made.rout_through, [])

    def test_a_cut_that_never_stops_is_caught_and_named(self):
        # Stop lines too short for the bit to reach: no stop is found, so the
        # path is the whole closed loop and it runs over every base.
        board = square_board(tabs=[tab_footprint(length=0.2)])
        made = build(board, CUTTER)
        self.assertEqual(len(made.rout_through), 1)
        hit = made.rout_through[0]
        self.assertLessEqual(hit.clearance_mm, hit.radius_mm + 1e-9)
        self.assertTrue(any("ROUT-THROUGH" in w for w in made.warnings))
        self.assertIn(hit.label, [w for w in made.warnings if "ROUT" in w][0])
        # Again the check is loud, and it still writes the path it computed.
        self.assertTrue(made.items)

    def test_the_guard_measures_the_swept_cut_not_the_centreline(self):
        # A run one radius off the base is tangent to it along its whole length,
        # which is a cut through the tab even though nothing crosses anything.
        base = route_toolpath.Line(p0=(4.59, 0.0), p1=(5.41, 0.0))
        tab = route_toolpath.Tab(
            label="T", position=(5.0, 0.0), base=base, tool_max_mm=None,
            stop_lines=[], owner="T",
        )
        over = [[route_toolpath.Line(p0=(0.0, -RADIUS), p1=(10.0, -RADIUS))]]
        clear = [[route_toolpath.Line(
            p0=(0.0, -RADIUS - 0.001), p1=(10.0, -RADIUS - 0.001)
        )]]
        self.assertEqual(len(rout_through(over, [tab], RADIUS)), 1)
        self.assertEqual(rout_through(clear, [tab], RADIUS), [])

    def test_tangency_at_the_cut_faces_is_allowed(self):
        # The two cut faces are discs tangent to the base line AT its ends. That
        # contact is the design working, not a violation.
        base = route_toolpath.Line(p0=(4.59, 0.0), p1=(5.41, 0.0))
        tab = route_toolpath.Tab(
            label="T", position=(5.0, 0.0), base=base, tool_max_mm=None,
            stop_lines=[], owner="T",
        )
        faces = [
            [route_toolpath.Line(p0=(0.0, -RADIUS), p1=(4.59, -RADIUS))],
            [route_toolpath.Line(p0=(5.41, -RADIUS), p1=(10.0, -RADIUS))],
        ]
        self.assertEqual(rout_through(faces, [tab], RADIUS), [])

    def test_a_board_with_no_cutting_runs_reports_nothing(self):
        self.assertEqual(rout_through([], [], RADIUS), [])


class ClearToolpathTest(unittest.TestCase):
    def test_it_removes_board_level_items_and_counts_them(self):
        board = square_board()
        board.drawings.append(edge(1.0, 1.0, 2.0, 2.0, layer=TOOLPATH_LAYER,
                                   uuid_value="old-1"))
        board.trailing_drawings = [edge(3.0, 3.0, 4.0, 4.0, layer=TOOLPATH_LAYER,
                                        uuid_value="old-2")]
        self.assertEqual(clear_toolpath(board), 2)
        self.assertEqual(toolpath_items(board), [])

    def test_it_leaves_every_other_layer_alone(self):
        board = square_board()
        before = len(board.drawings)
        self.assertEqual(clear_toolpath(board), 0)
        self.assertEqual(len(board.drawings), before)

    def test_a_footprint_item_on_the_same_layer_is_not_the_stages_to_delete(self):
        # Those belong to the footprint library, not to prep.
        footprint = tab_footprint()
        footprint.drawings.append(
            PcbLine(start=(0.0, 0.0), end=(1.0, 0.0), layer=TOOLPATH_LAYER,
                    id="fp-toolpath")
        )
        board = square_board(tabs=[footprint])
        self.assertEqual(clear_toolpath(board), 0)
        self.assertEqual(len(board.footprints[0].drawings), 4)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.board = square_board(tabs=[tab_footprint()])
        self.result = build(self.board, CUTTER)
        made = self.result
        (self.tabs, self.stops, self.removed, self.outline_count,
         self.loop_count, self.items, self.runs, self.bridges,
         self.tool_max, self.warnings) = (
            made.tabs, made.stops, made.removed, made.outline_items,
            made.loop_items, made.items, made.run_lengths, made.bridges,
            made.tool_max_mm, made.warnings,
        )

    def test_both_stop_lines_are_found(self):
        self.assertEqual(len(self.stops), 2)

    def test_the_toolpath_is_written_on_the_toolpath_layer(self):
        self.assertTrue(self.items)
        self.assertEqual({item.layer for item in self.items}, {TOOLPATH_LAYER})

    def test_the_stroke_width_is_the_cutter_diameter(self):
        self.assertEqual({item.stroke.width for item in self.items}, {CUTTER})

    def test_one_staple_leaves_one_bridge_and_one_run(self):
        # The guarantee the retired nearest-span fix existed for, now falling
        # out of the base rule: with one tab both candidate spans end on the
        # same two stop lines, and only one of them lies over the base.
        self.assertEqual(len(self.bridges), 1)
        self.assertEqual(len(self.runs), 1)

    def test_two_staples_on_one_footprint_leave_two_bridges_and_two_runs(self):
        # 1:many. Pairing by footprint membership made two staples in one
        # footprint indistinguishable from a single four-legged tab; the base
        # decides now, so the tab count follows the staples.
        board = square_board(tabs=[tab_footprint(
            position=(5.0, 0.0),
            staples=staple(-2.0, tag="a") + staple(2.0, tag="b"),
        )])
        made = build(board, CUTTER)
        bridges, runs, warnings = made.bridges, made.run_lengths, made.warnings
        self.assertEqual(len(bridges), 2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(warnings, [])
        for bridge in bridges:
            self.assertAlmostEqual(bridge.material_mm, 0.82, places=9)

    def test_the_tab_material_is_exactly_the_stop_spacing(self):
        # The number a designer judges: what is left holding the part in. Each
        # cut face lands on its stop line, so the material between them is the
        # stop spacing - measured off the staple's base, not derived.
        bridge = self.bridges[0]
        self.assertAlmostEqual(bridge.material_mm, 0.82, places=9)
        self.assertAlmostEqual(bridge.stop_spacing_mm, 0.82, places=9)
        self.assertTrue(bridge.agrees)

    def test_the_centreline_gap_is_the_material_plus_one_whole_cutter(self):
        # The router stops when its
        # EDGE reaches the stop line, so the centreline gap is wider than the
        # material by one radius on each side.
        bridge = self.bridges[0]
        self.assertAlmostEqual(bridge.centreline_mm, 0.82 + 2 * RADIUS, places=9)
        self.assertAlmostEqual(
            bridge.centreline_mm - CUTTER, bridge.material_mm, places=12
        )

    def test_the_material_identity_holds_for_a_wider_bit_too(self):
        # Stop-line LENGTH must not move the geometry and cutter size must not
        # move the material, so both are swept against the same identity.
        for cutter in (0.4, 0.6, 0.8, 0.889):
            for length in (0.5, 0.6, 1.2):
                board = square_board(tabs=[tab_footprint(length=length)])
                bridges = build(board, cutter).bridges
                self.assertAlmostEqual(bridges[0].material_mm, 0.82, places=9)
                self.assertTrue(bridges[0].agrees, f"{cutter} / {length}")

    def test_every_run_end_sits_one_radius_from_a_stop_line(self):
        # The physical claim, read off the items that were actually written: a
        # free end is where the bit's rim just touched the line it stopped at.
        from eedesignpanels.toolpath import point_segment_distance

        stops = [stop for tab in self.tabs for stop in tab.stop_lines]
        ends = []
        for item in self.items:
            for point in (item.start, item.end):
                touching = sum(
                    1
                    for other in self.items
                    for spot in (other.start, other.end)
                    if math.dist(spot, point) <= 1e-9
                )
                if touching == 1:
                    ends.append(point)
        self.assertEqual(len(ends), 2)  # one run, two free ends
        for point in ends:
            nearest = min(
                point_segment_distance(point, stop.p0, stop.p1) for stop in stops
            )
            self.assertAlmostEqual(nearest, RADIUS, places=9)

    def test_a_clean_board_warns_about_nothing(self):
        self.assertEqual(self.warnings, [])

    def test_building_twice_gives_the_same_uuids(self):
        again = build(square_board(tabs=[tab_footprint()]), CUTTER)
        self.assertEqual(
            [item.id for item in self.items],
            [item.id for item in again.items],
        )

    def test_running_on_an_already_prepared_board_replaces_rather_than_adds(self):
        count = len(toolpath_items(self.board))
        second = build(self.board, CUTTER)
        self.assertEqual(second.removed, count)  # removed what was there
        self.assertEqual(len(toolpath_items(self.board)), count)

    def test_the_result_is_byte_stable_across_a_reprep(self):
        first = self.board.to_string()
        build(self.board, CUTTER)
        self.assertEqual(self.board.to_string(), first)


class WarningTest(unittest.TestCase):
    def test_a_cutter_wider_than_a_tab_allows_warns_and_still_builds(self):
        board = square_board(tabs=[tab_footprint(tool_max="0.5")])
        made = build(board, CUTTER)
        self.assertTrue(any("exceeds what tab" in w for w in made.warnings))
        self.assertAlmostEqual(made.tool_max_mm, 0.5)
        # A check reports, it does not block.
        self.assertTrue(made.items)

    def test_a_tab_with_no_declared_ceiling_warns(self):
        board = square_board(tabs=[tab_footprint(tool_max=None)])
        warnings = build(board, CUTTER).warnings
        self.assertTrue(any(TAB_TOOL_MAX_FIELD in w for w in warnings))

    def test_a_stop_line_too_short_to_reach_the_path_warns_and_leaves_it_uncut(self):
        board = square_board(tabs=[tab_footprint(length=0.2)])
        made = build(board, CUTTER)
        self.assertTrue(any("does not reach" in w for w in made.warnings))
        self.assertEqual(made.bridges, [])
        self.assertTrue(made.items)

    def test_an_undeclared_toolpath_layer_warns_that_it_will_not_plot(self):
        board = square_board(tabs=[tab_footprint()])
        board.layers = [
            layer for layer in board.layers
            if layer.canonical_name != TOOLPATH_LAYER
        ]
        warnings = build(board, CUTTER).warnings
        self.assertTrue(any("will not plot" in w for w in warnings))

    def slotted_board(self):
        """A square with a 0.5 mm slot cut into the edge the tabs are on."""
        board = square_board(tabs=[tab_footprint()])
        board.drawings = [
            edge(0.0, 0.0, 4.75, 0.0, uuid_value="a"),
            edge(4.75, 0.0, 4.75, 3.0, uuid_value="slot-left"),
            edge(4.75, 3.0, 5.25, 3.0, uuid_value="slot-end"),
            edge(5.25, 3.0, 5.25, 0.0, uuid_value="slot-right"),
            edge(5.25, 0.0, 10.0, 0.0, uuid_value="b"),
            edge(10.0, 0.0, 10.0, 10.0, uuid_value="c"),
            edge(10.0, 10.0, 0.0, 10.0, uuid_value="d"),
            edge(0.0, 10.0, 0.0, 0.0, uuid_value="e"),
        ]
        return board

    def test_a_slot_the_cutter_cannot_enter_is_routed_round_and_reported(self):
        # A slot narrower than the bit is not an
        # error - the machine seals it with two fillets - so the stage builds
        # the path and says, once, what it left standing.
        board = self.slotted_board()
        board.footprints = []
        made = build(board, 2.0)
        self.assertTrue(made.items)
        left = [w for w in made.warnings if "tighter than the cutter" in w]
        self.assertEqual(len(left), 1)
        self.assertIn("1.0000 mm fillet", left[0])
        self.assertIn("6.500 mm of outline", left[0])

    def test_an_unreachable_recess_inside_a_tab_says_nothing(self):
        # The break contour of a tab IS a recess tighter than any cutter, and it
        # lies in the span the router never enters. Warning about it would fire
        # on every tab of every board.
        board = square_board(tabs=[tab_footprint()])
        board.footprints[0].drawings.extend([
            PcbArc(start=(-0.8562, 0.0), mid=(-0.6, 0.2), end=(-0.3332, 0.1667),
                   layer="Edge.Cuts", id="tab-arc-left"),
            PcbLine(start=(-0.3332, 0.1667), end=(0.3332, 0.1667),
                    layer="Edge.Cuts", id="tab-line"),
            PcbArc(start=(0.3332, 0.1667), mid=(0.6, 0.2), end=(0.8562, 0.0),
                   layer="Edge.Cuts", id="tab-arc-right"),
        ])
        board.drawings[0] = edge(0.0, 0.0, 5.0 - 0.8562, 0.0, uuid_value="e0a")
        board.drawings.insert(
            1, edge(5.0 + 0.8562, 0.0, 10.0, 0.0, uuid_value="e0b")
        )
        made = build(board, CUTTER)
        self.assertTrue(made.items)
        self.assertEqual(len(made.bridges), 1)
        self.assertEqual(
            [w for w in made.warnings if "tighter than the cutter" in w], []
        )

    def test_an_outline_that_is_not_a_loop_still_raises_with_a_location(self):
        # The errors that remain are the ones a stage genuinely cannot answer.
        board = square_board()
        board.drawings = board.drawings[:3]
        with self.assertRaises(OffsetError) as caught:
            build(board, CUTTER)
        self.assertIsNotNone(caught.exception.at)
        self.assertEqual(toolpath_items(board), [])


def footprint_blocks(text: str) -> list:
    """Every `(footprint …)` block of a serialised board, verbatim and in order."""
    blocks, cursor = [], 0
    while True:
        start = text.find("(footprint ", cursor)
        if start < 0:
            return blocks
        depth, index = 0, start
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        blocks.append(text[start:index + 1])
        cursor = index + 1


class WriteScopeTest(unittest.TestCase):
    """Prep owns board-level `User.36`. Everything else it reads, it leaves.

    The flatten reads a footprint's `Edge.Cuts` items and maps them into board
    coordinates to build the outline. It does that in memory, so the file comes
    back with its footprints byte for byte as the designer placed them.
    """

    def prepared(self):
        board = square_board(tabs=[tab_footprint()])
        board.footprints[0].drawings.extend([
            PcbLine(start=(-0.8562, 0.0), end=(0.8562, 0.0),
                    layer="Edge.Cuts", id="tab-contour"),
        ])
        board.drawings[0] = edge(0.0, 0.0, 5.0 - 0.8562, 0.0, uuid_value="e0a")
        board.drawings.insert(
            1, edge(5.0 + 0.8562, 0.0, 10.0, 0.0, uuid_value="e0b")
        )
        before = board.to_string()
        build(board, CUTTER)
        return before, board.to_string()

    def test_every_footprint_comes_back_byte_for_byte(self):
        before, after = self.prepared()
        self.assertEqual(footprint_blocks(before), footprint_blocks(after))
        self.assertTrue(footprint_blocks(before))

    def test_taking_back_what_it_wrote_gives_the_file_back(self):
        # The sharpest way to say "it changed nothing else": undo prep's one
        # documented effect and the bytes are the bytes it started with.
        board = square_board(tabs=[tab_footprint()])
        before = board.to_string()
        build(board, CUTTER)
        self.assertNotEqual(board.to_string(), before)
        clear_toolpath(board)
        self.assertEqual(board.to_string(), before)


class PanelHandoffTest(unittest.TestCase):
    def test_the_toolpath_layer_is_not_a_drawing_scale_layer(self):
        # If it were excluded, a prepped board would panelize without its
        # toolpaths and nothing would say so.
        self.assertNotIn(TOOLPATH_LAYER, DEFAULT_EXCLUDED_LAYERS)

    def test_step_and_repeat_replicates_the_toolpath_layer(self):
        from eedesignpanels.stages.step_and_repeat import replicated_layers

        board = square_board(tabs=[tab_footprint()])
        build(board, CUTTER)
        self.assertIn(TOOLPATH_LAYER, replicated_layers(board))


class PrepConfigTest(unittest.TestCase):
    def test_a_cutter_diameter_is_required(self):
        with self.assertRaises(Exception):
            PrepConfig()

    def test_a_cutter_diameter_must_be_positive(self):
        with self.assertRaises(Exception):
            PrepConfig(cutter_diameter=0.0)

    def test_prep_is_absent_unless_the_config_says_so(self):
        config = PanelConfig(
            source_board="../b.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 0.0},
        )
        self.assertIsNone(config.prep)

    def test_running_prep_without_a_prep_section_is_an_error(self):
        config = PanelConfig(
            source_board="../b.kicad_pcb",
            grid={"columns": 1, "rows": 1, "gap_mm": 0.0},
        )
        with self.assertRaises(ValueError):
            route_toolpath.run(config)


def straight(x0, x1, source="s", role="line"):
    """One horizontal piece of a run, travelled left to right."""
    return Line(p0=(x0, 0.0), p1=(x1, 0.0), source=source, role=role)


class EmissionTest(unittest.TestCase):
    """What reaches the file, and what the degeneracy guard keeps out of it.

    Two degeneracies, and they are independent: a splinter is too short to
    mean anything, and a near-collinear arc is unstorable at any length. Each
    is tested on geometry that has only the one fault, so neither rule can be
    credited with the other's catch.
    """

    def test_the_sagitta_is_the_bow_off_the_chord(self):
        # A quarter circle of unit radius bows by 1 - cos(45 deg).
        arc = Arc(center=(0.0, 0.0), radius=1.0, a0=0.0, a1=math.pi / 2.0,
                  ccw=True)
        self.assertAlmostEqual(
            route_toolpath.sagitta_mm(arc), 1.0 - math.cos(math.pi / 4.0),
            places=12,
        )

    def test_a_splinter_is_dropped_and_its_neighbours_welded(self):
        dust = MIN_ITEM_MM / 10.0
        run = [straight(0.0, 1.0, "a"), straight(1.0, 1.0 + dust, "dust"),
               straight(1.0 + dust, 2.0, "b")]
        kept, dropped, dropped_mm = route_toolpath.emittable(run)
        self.assertEqual(dropped, 1)
        self.assertAlmostEqual(dropped_mm, dust, places=12)
        self.assertEqual([piece.source for _, piece in kept], ["a", "b"])
        # Welded: the gap the splinter left is closed, not left open.
        self.assertEqual(kept[0][1].end, kept[1][1].start)

    def test_a_dropped_splinter_does_not_renumber_its_neighbours(self):
        # The determinism claim. A surviving item's uuid is derived from its
        # position in the run, so renumbering after a drop would rename items
        # that never changed - and a re-prep would not be byte-identical.
        dust = MIN_ITEM_MM / 10.0
        run = [straight(0.0, 1.0, "a"), straight(1.0, 1.0 + dust, "dust"),
               straight(1.0 + dust, 2.0, "b")]
        kept, _, _ = route_toolpath.emittable(run)
        self.assertEqual([index for index, _ in kept], [0, 2])
        emitted = route_toolpath.to_board_items([run], CUTTER)
        self.assertEqual(
            emitted.items[1].id, route_toolpath.derived_uuid("b|line|0|2")
        )

    def test_nothing_is_welded_where_nothing_was_dropped(self):
        run = [straight(0.0, 1.0, "a"), straight(1.0, 2.0, "b")]
        kept, dropped, dropped_mm = route_toolpath.emittable(run)
        self.assertEqual((dropped, dropped_mm), (0, 0.0))
        self.assertEqual([piece for _, piece in kept], run)

    def test_a_piece_at_the_floor_is_kept(self):
        run = [straight(0.0, MIN_ITEM_MM, "a")]
        kept, dropped, _ = route_toolpath.emittable(run)
        self.assertEqual((len(kept), dropped), (1, 0))

    def test_a_near_collinear_arc_is_written_as_a_line(self):
        # A metre of radius swept through a microradian: a millimetre long, so
        # the length floor never sees it, and it bows by 0.1 nm - which the
        # file's nanometre grid cannot store, so its centre reads back as any
        # radius at all. That is the spurious circle the guard exists for.
        flat = Arc(center=(0.0, -1.0e6), radius=1.0e6,
                   a0=math.pi / 2.0 - 5.0e-7, a1=math.pi / 2.0 + 5.0e-7,
                   ccw=True, source="flat")
        self.assertAlmostEqual(flat.length, 1.0, places=9)
        self.assertLess(route_toolpath.sagitta_mm(flat), MIN_SAGITTA_MM)
        emitted = route_toolpath.to_board_items([[flat]], CUTTER)
        self.assertEqual(emitted.straightened, 1)
        self.assertEqual(len(emitted.items), 1)
        item = emitted.items[0]
        self.assertIsInstance(item, PcbLine)
        self.assertEqual((item.start, item.end), (flat.start, flat.end))
        # The whole geometry change is the bow it no longer has.
        self.assertAlmostEqual(
            emitted.straightened_mm, route_toolpath.sagitta_mm(flat), places=15
        )

    def test_a_real_fillet_stays_an_arc(self):
        # The cutter's own corner radius, a quarter turn: 0.13 mm of bow, a
        # hundred and thirty times the threshold. No real arc is straightened.
        fillet = Arc(center=(0.0, 0.0), radius=RADIUS, a0=0.0,
                     a1=math.pi / 2.0, ccw=True, source="fillet")
        emitted = route_toolpath.to_board_items([[fillet]], CUTTER)
        self.assertEqual(emitted.straightened, 0)
        self.assertIsInstance(emitted.items[0], PcbArc)

    def test_the_thresholds_sit_between_the_two_measured_populations(self):
        # Measured on the corpus board: dust up to 1.726 um with
        # the next piece at 85.09 um, and degenerate arcs bowing 1 nm against
        # 15.9 um for the flattest real one. Both floors have to sit in those
        # gaps or they are catching the wrong thing.
        self.assertLess(0.001726, MIN_ITEM_MM)
        self.assertLess(MIN_ITEM_MM, 0.08509)
        self.assertLess(1e-6, MIN_SAGITTA_MM)
        self.assertLess(MIN_SAGITTA_MM, 0.0159)

    def test_the_built_board_carries_no_degenerate_item(self):
        # The end-to-end claim, on the stage's own output rather than on a
        # fixture: nothing on the toolpath layer is shorter than the floor,
        # and every arc that reached the file has a storable bow.
        board = square_board(tabs=[tab_footprint()])
        build(board, CUTTER)
        for item in toolpath_items(board):
            if isinstance(item, PcbLine):
                self.assertGreaterEqual(
                    math.dist(item.start, item.end), MIN_ITEM_MM
                )
                continue
            arc = arc_from_three_points(item.start, item.mid, item.end)
            self.assertGreaterEqual(arc.length, MIN_ITEM_MM)
            self.assertGreaterEqual(
                route_toolpath.sagitta_mm(arc), MIN_SAGITTA_MM
            )


if __name__ == "__main__":
    unittest.main()
