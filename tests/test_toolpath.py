"""The offset geometry, checked against closed forms rather than against a render.

`eedesignpanels.toolpath` imports nothing but `math`, so every claim here is a
number with a known right answer. That matters more than usual: an offset that
is wrong by a fraction of a millimetre looks perfect at page scale and cuts
through the board.

These cases started as the quick test's oracle. The three that earned their keep
are the ones that separate a DISTANCE from a length subtracted along the path -
oblique and curved crossings - and the tip boundary, where the nearest part of a
stop line stops being its middle and becomes its end.
"""

import math
import unittest

from eedesignpanels.toolpath import (
    Arc,
    Line,
    OffsetError,
    arc_from_three_points,
    chain_loop,
    chain_loops,
    closest_feature,
    nearest_on_path,
    offset_loop,
    point_segment_distance,
    resolve_offset,
    signed_area,
    stadium_intersections,
    weld_chain,
)

RADIUS = 0.4445  # half of the 0.889 mm cutter the corpus tabs allow


def square(side: float = 10.0) -> list:
    corners = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
    return [
        Line(p0=corners[i], p1=corners[(i + 1) % 4], source=f"s{i}")
        for i in range(4)
    ]


class ChainTest(unittest.TestCase):
    def test_a_square_chains_into_a_closed_loop(self):
        chain = chain_loop(square())
        self.assertEqual(len(chain), 4)
        self.assertEqual(chain[-1].end, chain[0].start)

    def test_an_item_stored_backwards_is_turned_round(self):
        items = square()
        items[1] = items[1]._replace(p0=items[1].p1, p1=items[1].p0)
        chain = chain_loop(items)
        self.assertEqual(chain[-1].end, chain[0].start)

    def test_a_gap_in_the_outline_is_an_error_with_a_location(self):
        items = square()[:3]
        with self.assertRaises(OffsetError) as caught:
            chain_loop(items)
        self.assertIsNotNone(caught.exception.at)

    def test_chain_loop_refuses_more_than_one_loop_and_says_how_many(self):
        # A caller that hands this a whole `Edge.Cuts` layer
        # is asking the wrong question, and the answer names the second loop.
        with self.assertRaises(OffsetError) as caught:
            chain_loop(square() + shifted(square(2.0), 3.0, 3.0, "h"))
        self.assertIn("2 separate closed loops", str(caught.exception))
        self.assertEqual(caught.exception.at, (3.0, 3.0))

    def test_orientation_is_measured_not_assumed(self):
        forward = chain_loop(square())
        self.assertGreater(signed_area(forward), 0.0)
        backward = chain_loop([
            item._replace(p0=item.p1, p1=item.p0) for item in reversed(square())
        ])
        self.assertLess(signed_area(backward), 0.0)

    def test_an_arc_reads_back_the_way_it_was_written(self):
        arc = arc_from_three_points((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0))
        self.assertAlmostEqual(arc.radius, 1.0)
        self.assertAlmostEqual(arc.sweep, math.pi)
        self.assertAlmostEqual(arc.mid[0], 0.0)
        self.assertAlmostEqual(arc.mid[1], 1.0)


def shifted(items: list, dx: float, dy: float, tag: str) -> list:
    """A copy of *items* moved, and renamed so its loop is identifiable."""
    return [
        item._replace(
            p0=(item.p0[0] + dx, item.p0[1] + dy),
            p1=(item.p1[0] + dx, item.p1[1] + dy),
            source=f"{tag}{index}",
        )
        for index, item in enumerate(items)
    ]


class ChainLoopsTest(unittest.TestCase):
    """`Edge.Cuts` is a SET of closed loops.

    A reader that assumed one loop counted the second loop's items as leftovers
    and refused the board at the vertex where the FIRST loop closed - a vertex
    that measures perfect, which is what made the refusal unreadable.
    """

    def two_squares(self) -> list:
        return square() + shifted(square(2.0), 3.0, 3.0, "h")

    def test_two_separate_squares_come_back_as_two_closed_loops(self):
        loops = chain_loops(self.two_squares())
        self.assertEqual([len(loop) for loop in loops], [4, 4])
        for loop in loops:
            self.assertEqual(loop[-1].end, loop[0].start)

    def test_loops_arrive_in_the_order_their_items_do(self):
        loops = chain_loops(self.two_squares())
        self.assertEqual(loops[0][0].source, "s0")
        self.assertEqual(loops[1][0].source, "h0")

    def test_an_open_run_is_still_an_error_that_says_how_much_is_left(self):
        items = square()[:3] + shifted(square(2.0), 3.0, 3.0, "h")
        with self.assertRaises(OffsetError) as caught:
            chain_loops(items)
        self.assertIn("left over", str(caught.exception))
        self.assertIsNotNone(caught.exception.at)

    def test_one_loop_is_one_loop(self):
        self.assertEqual(len(chain_loops(square())), 1)


class SquareOffsetTest(unittest.TestCase):
    """A square's outward offset has a closed form: sides plus quarter circles."""

    def setUp(self):
        self.side, self.radius = 10.0, 1.0
        self.loop = offset_loop(chain_loop(square(self.side)), self.radius)

    def test_four_sides_and_four_round_joins(self):
        lines = [p for p in self.loop if isinstance(p, Line)]
        arcs = [p for p in self.loop if isinstance(p, Arc)]
        self.assertEqual((len(lines), len(arcs)), (4, 4))

    def test_every_join_is_a_quarter_circle_of_the_cutter_radius(self):
        for arc in (p for p in self.loop if isinstance(p, Arc)):
            self.assertAlmostEqual(arc.sweep, math.pi / 2, places=12)
            self.assertAlmostEqual(arc.radius, self.radius, places=12)

    def test_the_total_length_is_the_perimeter_plus_one_full_circle(self):
        expected = 4 * self.side + 2 * math.pi * self.radius
        self.assertAlmostEqual(sum(p.length for p in self.loop), expected, places=9)

    def test_the_enclosed_area_matches_the_closed_form(self):
        expected = (
            self.side ** 2
            + 4 * self.side * self.radius
            + math.pi * self.radius ** 2
        )
        self.assertAlmostEqual(abs(signed_area(self.loop)), expected, places=9)

    def test_a_notch_rounds_its_mouth_and_trims_its_inner_corners(self):
        # A notch cut into the block has corners of both kinds, and which is
        # which is easy to get backwards. Its MOUTH corners are corners of
        # material - the bit swings round them, so they round. Its two INNER
        # corners fold the offsets over each other and are trimmed.
        chain = chain_loop([
            Line(p0=(0.0, 0.0), p1=(4.0, 0.0), source="a"),
            Line(p0=(4.0, 0.0), p1=(4.0, 3.0), source="b"),
            Line(p0=(4.0, 3.0), p1=(6.0, 3.0), source="c"),
            Line(p0=(6.0, 3.0), p1=(6.0, 0.0), source="d"),
            Line(p0=(6.0, 0.0), p1=(10.0, 0.0), source="e"),
            Line(p0=(10.0, 0.0), p1=(10.0, 10.0), source="f"),
            Line(p0=(10.0, 10.0), p1=(0.0, 10.0), source="g"),
            Line(p0=(0.0, 10.0), p1=(0.0, 0.0), source="h"),
        ])
        loop = offset_loop(chain, 0.5)
        joins = [p for p in loop if getattr(p, "role", "") == "join"]
        # Four square corners plus the notch's two mouth corners round; the
        # notch's two inner corners are trimmed and contribute no arc.
        self.assertEqual(len(joins), 6)
        self.assertEqual(len([p for p in loop if isinstance(p, Line)]), 8)


class UnroutableFeatureTest(unittest.TestCase):
    """What a mill does with geometry it cannot follow: it gets as close as it can.

    The offset behaves like the machine in these three cases, rather than
    refusing. A mousebite contour is drawn tighter than any cutter ON PURPOSE,
    and so are plenty of other things a designer will do. The answer is a fillet
    of the bit's radius and a note saying where, not a stop.
    """

    def slot_board(self, width: float = 0.5) -> list:
        left, right = 5.0 - width / 2.0, 5.0 + width / 2.0
        return chain_loop([
            Line(p0=(0.0, 0.0), p1=(left, 0.0), source="a"),
            Line(p0=(left, 0.0), p1=(left, 3.0), source="slot-left"),
            Line(p0=(left, 3.0), p1=(right, 3.0), source="slot-end"),
            Line(p0=(right, 3.0), p1=(right, 0.0), source="slot-right"),
            Line(p0=(right, 0.0), p1=(10.0, 0.0), source="b"),
            Line(p0=(10.0, 0.0), p1=(10.0, 10.0), source="c"),
            Line(p0=(10.0, 10.0), p1=(0.0, 10.0), source="d"),
            Line(p0=(0.0, 10.0), p1=(0.0, 0.0), source="e"),
        ])

    def test_a_slot_narrower_than_the_cutter_is_sealed_by_two_fillets(self):
        # The bit cannot enter, so the cut runs past the mouth: the two mouth
        # corners are rounded at the bit radius and those two fillets meet.
        result = resolve_offset(self.slot_board(0.5), 1.0)
        seals = [
            piece for piece in result.loop
            if isinstance(piece, Arc) and abs(piece.center[1]) < 1e-9
            and 4.0 < piece.center[0] < 6.0
        ]
        self.assertEqual(len(seals), 2)
        for arc in seals:
            self.assertAlmostEqual(arc.radius, 1.0, places=12)
        self.assertAlmostEqual(
            math.dist(seals[0].end, seals[1].start), 0.0, places=9
        )

    def test_the_sealed_slot_says_what_it_left_standing(self):
        result = resolve_offset(self.slot_board(0.5), 1.0)
        self.assertEqual(len(result.unreachable), 1)
        left = result.unreachable[0]
        self.assertEqual(left.items, 3)
        self.assertEqual(
            left.sources, ("slot-left", "slot-end", "slot-right")
        )
        self.assertAlmostEqual(left.fillet_mm, 1.0)
        self.assertAlmostEqual(left.span_mm, 0.5 + 3.0 + 3.0, places=9)

    def test_a_slot_the_cutter_fits_is_cut_and_reported_clean(self):
        result = resolve_offset(self.slot_board(3.0), 1.0)
        self.assertEqual(result.unreachable, [])
        # The bit reaches the bottom of the slot: 1 mm short of its end.
        deepest = max(
            point[1]
            for piece in result.loop
            for point in (piece.start, piece.end)
            if point[1] < 5.0
        )
        self.assertAlmostEqual(deepest, 2.0, places=9)

    def test_an_inward_arc_tighter_than_the_cutter_is_left_standing(self):
        # A rounded notch of radius 0.2 - the arc bulges INTO the material -
        # offered a 1.0 mm radius bit. The concentric offset would need a
        # negative radius, so that arc contributes nothing and the cut walks
        # round the outside of the two tips instead.
        chain = chain_loop([
            Line(p0=(0.0, 0.0), p1=(5.0, 0.0), source="a"),
            arc_from_three_points((5.0, 0.0), (4.8, 0.2), (5.0, 0.4), source="b"),
            Line(p0=(5.0, 0.4), p1=(0.0, 0.4), source="c"),
            Line(p0=(0.0, 0.4), p1=(0.0, 0.0), source="d"),
        ])
        result = resolve_offset(chain, 1.0)
        self.assertEqual([run.sources for run in result.unreachable], [("b",)])
        self.assertEqual(
            [piece.source for piece in result.loop if piece.source == "b"], []
        )

    def test_the_same_arc_bulging_outward_is_fine(self):
        # The mirror image: a rounded bump on the outside grows to R + r and
        # offsets happily, which is what makes the previous test about the
        # cutter rather than about arcs.
        chain = chain_loop([
            Line(p0=(0.0, 0.0), p1=(5.0, 0.0), source="a"),
            arc_from_three_points((5.0, 0.0), (5.2, 0.2), (5.0, 0.4), source="b"),
            Line(p0=(5.0, 0.4), p1=(0.0, 0.4), source="c"),
            Line(p0=(0.0, 0.4), p1=(0.0, 0.0), source="d"),
        ])
        loop = offset_loop(chain, 1.0)
        bump = [p for p in loop if getattr(p, "source", "") == "b"][0]
        self.assertAlmostEqual(bump.radius, 1.2, places=12)


class WeldTest(unittest.TestCase):
    """Joints that a file's nanometre grid left a hair open."""

    def test_a_line_that_stops_short_is_pulled_onto_its_neighbour(self):
        items = square()
        items[1] = items[1]._replace(p0=(10.00000005, 0.0))
        welded = weld_chain(chain_loop(items))
        for index, piece in enumerate(welded):
            self.assertEqual(piece.end, welded[(index + 1) % len(welded)].start)

    def test_an_arc_keeps_its_circle_and_the_line_gives_ground(self):
        # The arc is the authored geometry - a footprint's contour - so the line
        # drawn up to it is what moves.
        arc = arc_from_three_points((5.0, 0.0), (5.2, 0.2), (5.0, 0.4), source="b")
        chain = chain_loop([
            Line(p0=(0.0, 0.0), p1=(5.00000004, 0.0), source="a"),
            arc,
            Line(p0=(5.0, 0.4), p1=(0.0, 0.4), source="c"),
            Line(p0=(0.0, 0.4), p1=(0.0, 0.0), source="d"),
        ])
        welded = weld_chain(chain)
        kept = [p for p in welded if p.source == "b"][0]
        self.assertEqual(kept.radius, arc.radius)
        self.assertEqual(kept.center, arc.center)

    def test_a_loop_that_really_is_open_is_still_an_error(self):
        with self.assertRaises(OffsetError):
            chain_loop(square()[:3])


class NearTangentJointTest(unittest.TestCase):
    """A fillet a few milliradians out of tangent, offset at every cutter size.

    This is the corpus board's 0.6 mm step: a 0.1 mm riser, then a 0.4985 mm
    fillet whose stored endpoints leave it 3 milliradians off tangent to it.
    Below the corner threshold the two offsets are welded rather than joined by
    a corner arc, and an arc can only slide ALONG its own circle - so what is
    left is a radial residue, second order in the joint gap. It reached 3.9 nm
    at a 0.3 mm radius, which is bigger than the tolerance the assembly calls
    one node, and the offset walk stopped without closing. Measured:
    0.5 and 0.6 mm cutters deadlocked while 0.4 and 0.7 mm did not, which is the
    signature of a threshold rather than of geometry.
    """

    def stepped(self) -> list:
        return chain_loop([
            Line(p0=(199.986840, 123.271288), p1=(202.142983, 123.271287),
                 source="top"),
            Line(p0=(202.142983, 123.271287), p1=(202.142983, 123.371288),
                 source="riser"),
            arc_from_three_points(
                (202.142983, 123.371288), (202.288, 123.729),
                (202.642983, 123.871288), source="fillet",
            ),
            Line(p0=(202.642983, 123.871288), p1=(210.0, 123.871288),
                 source="shelf"),
            Line(p0=(210.0, 123.871288), p1=(210.0, 133.0), source="right"),
            Line(p0=(210.0, 133.0), p1=(199.986840, 133.0), source="bottom"),
            Line(p0=(199.986840, 133.0), p1=(199.986840, 123.271288),
                 source="left"),
        ])

    def test_it_offsets_closed_at_every_cutter_in_the_range(self):
        chain = self.stepped()
        for diameter in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.889):
            loop = offset_loop(chain, diameter / 2.0)
            for index, piece in enumerate(loop):
                following = loop[(index + 1) % len(loop)]
                self.assertLessEqual(
                    math.dist(piece.end, following.start), 2e-6,
                    f"{diameter} mm leaves a gap in the offset loop",
                )

    def test_the_line_pays_only_what_the_arc_could_not_absorb(self):
        # The welded joint closes EXACTLY, and the line moved by nanometres
        # rather than by the whole gap - the arc slid first, for free.
        loop = offset_loop(self.stepped(), 0.3)
        moved = min(
            math.dist(piece.end, (202.442983, 123.371288)) for piece in loop
        )
        self.assertLess(moved, 1e-5)


class LevelSetTest(unittest.TestCase):
    """The defining property, asserted without an oracle.

    The boundary of a region grown by a disc of radius r is exactly the set of
    points at distance r from the region. Every point of the offset must sit on
    it - which is a far sharper statement than any picture of the result.
    """

    def points_of(self, loop: list, step: float = 0.01) -> list:
        found = []
        for piece in loop:
            count = max(2, int(piece.length / step) + 1)
            for index in range(count + 1):
                u = index / count
                if isinstance(piece, Line):
                    found.append((
                        piece.p0[0] + (piece.p1[0] - piece.p0[0]) * u,
                        piece.p0[1] + (piece.p1[1] - piece.p0[1]) * u,
                    ))
                else:
                    sign = 1.0 if piece.ccw else -1.0
                    found.append(
                        piece.point_at_angle(piece.a0 + sign * piece.sweep * u)
                    )
        return found

    def check(self, chain: list, radius: float):
        loop = resolve_offset(chain, radius).loop
        for point in self.points_of(loop):
            self.assertAlmostEqual(
                nearest_on_path(point, chain)[1], radius, places=9
            )

    def test_a_square(self):
        self.check(chain_loop(square()), 1.0)

    def test_a_notched_block(self):
        self.check(chain_loop([
            Line(p0=(0.0, 0.0), p1=(4.0, 0.0), source="a"),
            Line(p0=(4.0, 0.0), p1=(4.0, 3.0), source="b"),
            Line(p0=(4.0, 3.0), p1=(6.0, 3.0), source="c"),
            Line(p0=(6.0, 3.0), p1=(6.0, 0.0), source="d"),
            Line(p0=(6.0, 0.0), p1=(10.0, 0.0), source="e"),
            Line(p0=(10.0, 0.0), p1=(10.0, 10.0), source="f"),
            Line(p0=(10.0, 10.0), p1=(0.0, 10.0), source="g"),
            Line(p0=(0.0, 10.0), p1=(0.0, 0.0), source="h"),
        ]), 0.5)

    def test_a_recess_the_cutter_cannot_enter(self):
        self.check(chain_loop([
            Line(p0=(0.0, 0.0), p1=(5.0, 0.0), source="a"),
            arc_from_three_points((5.0, 0.0), (4.8, 0.2), (5.0, 0.4), source="b"),
            Line(p0=(5.0, 0.4), p1=(0.0, 0.4), source="c"),
            Line(p0=(0.0, 0.4), p1=(0.0, 0.0), source="d"),
        ]), 1.0)


class DeterminismTest(unittest.TestCase):
    def test_the_same_outline_offsets_to_the_same_numbers_twice(self):
        chain = chain_loop(square())
        first, second = offset_loop(chain, 1.0), offset_loop(chain, 1.0)
        self.assertEqual(first, second)

    def test_the_loop_starts_at_the_first_outline_item_however_it_was_walked(self):
        # The walk begins wherever the geometry reaches furthest left, which is
        # not a stable place to number output from; the outline's own order is.
        loop = offset_loop(chain_loop(square()), 1.0)
        self.assertEqual(loop[0].source, "s0")


class EdgeContactTest(unittest.TestCase):
    """Where the cutting EDGE reaches a stop line - a distance, not a walk back."""

    def test_a_perpendicular_crossing_backs_off_exactly_one_radius(self):
        run = Line(p0=(-5.0, 0.0), p1=(5.0, 0.0))
        stop = ((0.0, -1.0), (0.0, 1.0))
        points = stadium_intersections(run, *stop, RADIUS)
        self.assertEqual(len(points), 2)
        for point in points:
            self.assertAlmostEqual(abs(point[0]), RADIUS, places=12)

    def test_an_oblique_crossing_backs_off_further_along_the_path(self):
        # The proof that this is a distance and not a subtracted arc length:
        # at 45 degrees the run must give up r / sin(45) = r * sqrt(2).
        run = Line(p0=(-5.0, 0.0), p1=(5.0, 0.0))
        stop = ((-1.0, -1.0), (1.0, 1.0))
        points = stadium_intersections(run, *stop, RADIUS)
        self.assertEqual(len(points), 2)
        for point in points:
            self.assertAlmostEqual(abs(point[0]), RADIUS * math.sqrt(2.0), places=12)

    def test_a_crossing_hosted_on_an_arc_uses_the_true_distance(self):
        arc = Arc(center=(0.0, 0.0), radius=5.0, a0=-math.pi / 3,
                  a1=math.pi / 3, ccw=True)
        stop = ((4.0, 0.0), (6.0, 0.0))
        points = stadium_intersections(arc, *stop, RADIUS)
        self.assertEqual(len(points), 2)
        along = 5.0 * abs(math.atan2(points[0][1], points[0][0]))
        # Along the arc it is 5 * asin(r/5), which is NOT r.
        self.assertAlmostEqual(along, 5.0 * math.asin(RADIUS / 5.0), places=12)
        self.assertNotAlmostEqual(along, RADIUS, places=6)

    def test_every_contact_point_is_exactly_one_radius_from_the_segment(self):
        run = Line(p0=(-5.0, 0.0), p1=(5.0, 0.0))
        for stop in (((0.0, -1.0), (0.0, 1.0)), ((-1.0, -1.0), (1.0, 1.0))):
            for point in stadium_intersections(run, *stop, RADIUS):
                self.assertAlmostEqual(
                    point_segment_distance(point, *stop), RADIUS, places=12
                )


class TipBoundaryTest(unittest.TestCase):
    """At the widest allowed cutter the bit touches the stop line's TIP.

    That is the boundary where the nearest part of the segment stops being the
    perpendicular foot and becomes the end point. The answer has to be the same
    number either side of it, so this walks across and asks for continuity.
    """

    LENGTH = 0.6  # the regenerated tab's stop line

    def trim_for(self, radius: float) -> list:
        stop = ((0.0, 0.0), (0.0, -self.LENGTH))
        run = Line(p0=(-3.0, -radius), p1=(3.0, -radius))
        return sorted(abs(p[0]) for p in stadium_intersections(run, *stop, radius))

    def expected(self, radius: float) -> float:
        overshoot = max(0.0, radius - self.LENGTH)
        return math.sqrt(max(radius * radius - overshoot * overshoot, 0.0))

    def test_inside_the_tip_the_foot_governs(self):
        radius = self.LENGTH * 0.9
        trims = self.trim_for(radius)
        self.assertEqual(len(trims), 2)
        self.assertAlmostEqual(trims[0], self.expected(radius), places=12)
        stop = ((0.0, 0.0), (0.0, -self.LENGTH))
        self.assertEqual(closest_feature((radius, -radius), *stop)[0], "foot")

    def test_at_the_tip_exactly_the_answer_is_still_the_radius(self):
        trims = self.trim_for(self.LENGTH)
        self.assertEqual(len(trims), 2)
        self.assertAlmostEqual(trims[0], self.LENGTH, places=12)

    def test_beyond_the_tip_the_end_point_governs(self):
        radius = self.LENGTH * 1.1
        trims = self.trim_for(radius)
        self.assertEqual(len(trims), 2)
        self.assertAlmostEqual(trims[0], self.expected(radius), places=12)
        stop = ((0.0, 0.0), (0.0, -self.LENGTH))
        self.assertEqual(
            closest_feature((trims[0], -radius), *stop)[0], "endpoint"
        )

    def test_the_trim_does_not_jump_across_the_boundary(self):
        step = 1e-6
        below = self.trim_for(self.LENGTH - step)[0]
        at = self.trim_for(self.LENGTH)[0]
        above = self.trim_for(self.LENGTH + step)[0]
        self.assertLess(abs(at - below), 10 * step)
        self.assertLess(abs(above - at), 10 * step)

    def test_the_run_survives_at_the_boundary(self):
        trims = self.trim_for(self.LENGTH)
        self.assertGreater(trims[1] + trims[0], 0.0)


class ClosestFeatureTest(unittest.TestCase):
    def test_a_perpendicular_drop_inside_the_segment_is_the_foot(self):
        kind, t, margin = closest_feature((1.0, 0.5), (0.0, 0.0), (0.0, 1.0))
        self.assertEqual(kind, "foot")
        self.assertAlmostEqual(t, 0.5)
        self.assertAlmostEqual(margin, 0.5)

    def test_a_drop_past_the_end_is_the_end_point(self):
        kind, t, _ = closest_feature((1.0, 2.0), (0.0, 0.0), (0.0, 1.0))
        self.assertEqual(kind, "endpoint")
        self.assertAlmostEqual(t, 1.0)

    def test_the_margin_is_the_distance_left_before_the_case_flips(self):
        _, _, margin = closest_feature((1.0, 0.9), (0.0, 0.0), (0.0, 1.0))
        self.assertAlmostEqual(margin, 0.1)


class SegmentDistanceTest(unittest.TestCase):
    def test_beside_the_segment_it_is_the_perpendicular(self):
        self.assertAlmostEqual(
            point_segment_distance((2.0, 0.5), (0.0, 0.0), (0.0, 1.0)), 2.0
        )

    def test_past_the_end_it_is_the_distance_to_the_end(self):
        self.assertAlmostEqual(
            point_segment_distance((0.0, 3.0), (0.0, 0.0), (0.0, 1.0)), 2.0
        )

    def test_a_degenerate_segment_is_a_point(self):
        self.assertAlmostEqual(
            point_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)), 5.0
        )


if __name__ == "__main__":
    unittest.main()
