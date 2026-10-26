"""The offset checked against GEOS, which is a mill's answer by common consent.

`eedesignpanels.toolpath` claims to produce the boundary of the outline's
Minkowski dilation by the cutter disc - the same curve Clipper and GEOS
`buffer` produce, and the same curve a round tool actually leaves. That claim is
worth exactly as much as an independent kernel agreeing with it, so this module
asks one: shapely, over GEOS.

**Shapely is a test dependency and nothing more.** Nothing in `eedesignpanels`
imports it, and these cases skip when it is absent, which is the ordinary case
in CI and in the working interpreter. They were run against
`shapely 2.1.2 / GEOS 3.13.1` in a separate virtual environment:

    <venv>/Scripts/python.exe -m pytest tests/test_toolpath_oracle.py

Both kernels are compared as curves, not as vertex lists. GEOS works on
polygons, so the comparison carries a discretization floor: the outline is
densified to `STEP_MM` before buffering and the buffer's own joins are drawn
with `QUAD_SEGMENTS` per quarter turn, which together put a sagitta of about a
nanometre between a true arc and its polygon. Measured agreement is at that
level - 1 to 20 nanometres on every case here save one.

The exception is worth stating plainly, because it runs the other way. On a slot
three millimetres wide offered a two millimetre cutter, GEOS puts the trimmed
inside corner 1.5 um clear of where it belongs, while the analytic answer is the
exact intersection of the two wall offsets. Which is right is not a matter of
opinion: every point of the offset must be exactly one radius from the outline,
and that is checked here directly, oracle or no oracle. So each case is judged
twice - loosely against GEOS, to confirm the SHAPE is the one a mill leaves, and
tightly against the level set, to confirm the POSITION is exact.
"""

import json
import math
import unittest
from pathlib import Path

from eedesignpanels.toolpath import (
    Arc,
    Line,
    arc_from_three_points,
    chain_loop,
    distance_to_primitive,
    nearest_on_path,
    resolve_offset,
    signed_area,
)

try:
    from shapely.geometry import Polygon
except ImportError:  # pragma: no cover - the ordinary case off the oracle venv
    Polygon = None

# How finely a true curve is chopped before GEOS sees it. A 2 um chord on the
# tightest radius here leaves a sagitta under a nanometre.
STEP_MM = 0.002
# GEOS's own arc resolution, in segments per quarter turn.
QUAD_SEGMENTS = 512
# What counts as agreement with GEOS. Measured worst case is 16 nm except at a
# trimmed inside corner, where GEOS itself is 1.5 um out; this is above that and
# far below anything that changes a cut.
AGREE_MM = 2e-3
# What counts as being ON the offset: every point exactly one radius from the
# outline. The only give in it is the sub-micrometre weld that closes a
# near-tangent joint, measured at 47 nm on a real board outline.
EXACT_MM = 1e-4

# THE OUTLINE UNDER TEST IS SYNTHETIC. It is the outer
# `Edge.Cuts` loop of `synthetic_board.kicad_pcb`, frozen in loop order by
# `data/make_synthetic_board.py`. It replaced a real board's outline. Measured
# with coverage over these cases: it reaches every line and branch of
# `eedesignpanels/toolpath.py` the real outline reached, and more.
FIXTURE = Path(__file__).parent / "data" / "synthetic_outline.json"
BOARD = Path(__file__).parent / "data" / "synthetic_board.kicad_pcb"


def densify(primitive, step: float = STEP_MM) -> list:
    """*primitive* as a run of points no further apart than *step*."""
    count = max(2, int(math.ceil(primitive.length / step)) + 1)
    if isinstance(primitive, Line):
        return [
            (
                primitive.p0[0] + (primitive.p1[0] - primitive.p0[0]) * i / (count - 1),
                primitive.p0[1] + (primitive.p1[1] - primitive.p0[1]) * i / (count - 1),
            )
            for i in range(count)
        ]
    sign = 1.0 if primitive.ccw else -1.0
    return [
        primitive.point_at_angle(primitive.a0 + sign * primitive.sweep * i / (count - 1))
        for i in range(count)
    ]


def polygon_of(chain: list):
    """The chain as a shapely polygon, densified."""
    points = []
    for primitive in chain:
        points.extend(densify(primitive)[:-1])
    return Polygon(points)


def geos_offset(chain: list, radius: float):
    """The GEOS buffer of the chain's region - the oracle's outer boundary."""
    region = polygon_of(chain)
    if not region.is_valid:
        region = region.buffer(0)
    grown = region.buffer(radius, quad_segs=QUAD_SEGMENTS, join_style="round")
    return grown.exterior


def worst_deviation(loop: list, oracle) -> float:
    """The furthest either curve strays from the other, in millimetres."""
    from shapely.geometry import Point

    worst = 0.0
    for primitive in loop:
        for point in densify(primitive):
            worst = max(worst, oracle.distance(Point(point)))
    for point in oracle.coords:
        worst = max(
            worst, min(distance_to_primitive(point, piece) for piece in loop)
        )
    return worst


def off_the_level_set(loop: list, chain: list, radius: float) -> float:
    """The furthest any point of *loop* is from being one radius off *chain*.

    This is the definition of the offset written out as an assertion, and it
    needs no second kernel to check: the boundary of a region dilated by a disc
    of radius r is exactly the set of points at distance r from the region.
    """
    return max(
        abs(nearest_on_path(point, chain)[1] - radius)
        for primitive in loop
        for point in densify(primitive)
    )


def corpus_chain() -> list:
    """The synthetic board's outer outline, frozen as a fixture."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    primitives = []
    for entry in data["items"]:
        if entry["kind"] == "line":
            primitives.append(Line(
                p0=tuple(entry["start"]), p1=tuple(entry["end"]),
                source=entry["source"],
            ))
        else:
            primitives.append(arc_from_three_points(
                tuple(entry["start"]), tuple(entry["mid"]), tuple(entry["end"]),
                source=entry["source"],
            ))
    return chain_loop(primitives)


def square(side: float = 10.0) -> list:
    corners = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
    return chain_loop([
        Line(p0=corners[i], p1=corners[(i + 1) % 4], source=f"s{i}")
        for i in range(4)
    ])


def slot(width: float = 0.5, depth: float = 3.0) -> list:
    """A square with a slot cut into its bottom edge."""
    left, right = 5.0 - width / 2.0, 5.0 + width / 2.0
    return chain_loop([
        Line(p0=(0.0, 0.0), p1=(left, 0.0), source="a"),
        Line(p0=(left, 0.0), p1=(left, depth), source="slot-left"),
        Line(p0=(left, depth), p1=(right, depth), source="slot-end"),
        Line(p0=(right, depth), p1=(right, 0.0), source="slot-right"),
        Line(p0=(right, 0.0), p1=(10.0, 0.0), source="b"),
        Line(p0=(10.0, 0.0), p1=(10.0, 10.0), source="c"),
        Line(p0=(10.0, 10.0), p1=(0.0, 10.0), source="d"),
        Line(p0=(0.0, 10.0), p1=(0.0, 0.0), source="e"),
    ])


def rounded_recess(radius: float = 0.2) -> list:
    """A block with a circular bite taken out of its right-hand end."""
    return chain_loop([
        Line(p0=(0.0, 0.0), p1=(5.0, 0.0), source="a"),
        arc_from_three_points(
            (5.0, 0.0), (5.0 - radius, radius), (5.0, 2.0 * radius), source="b"
        ),
        Line(p0=(5.0, 2.0 * radius), p1=(0.0, 2.0 * radius), source="c"),
        Line(p0=(0.0, 2.0 * radius), p1=(0.0, 0.0), source="d"),
    ])


@unittest.skipIf(Polygon is None, "shapely is not installed in this interpreter")
class GeosAgreementTest(unittest.TestCase):
    """Every case the offset has to survive, judged by GEOS rather than by us."""

    def check(self, chain: list, radius: float, note: str = "") -> float:
        result = resolve_offset(chain, radius)
        worst = worst_deviation(result.loop, geos_offset(chain, radius))
        self.assertLess(worst, AGREE_MM, f"{note}: {worst * 1000:.4f} um from GEOS")
        astray = off_the_level_set(result.loop, chain, radius)
        self.assertLess(
            astray, EXACT_MM,
            f"{note}: {astray * 1e6:.1f} nm off the offset distance",
        )
        return worst

    def test_a_square_agrees(self):
        self.check(square(), 1.0, "square")

    def test_the_board_outline_agrees_at_the_configured_cutter(self):
        # The board this project is built against, contour and all, at the
        # cutter its tabs allow.
        self.check(corpus_chain(), 0.4445, "board outline")

    def test_a_slot_narrower_than_the_cutter_agrees(self):
        # The case that used to be a refusal. GEOS seals the slot with two
        # fillets of the bit radius; so must we.
        self.check(slot(width=0.5), 1.0, "narrow slot")

    def test_a_slot_wider_than_the_cutter_agrees(self):
        # The other side of the same boundary: the bit goes in and the slot is
        # cut, so nothing is sealed.
        self.check(slot(width=3.0), 1.0, "wide slot")

    def test_a_slot_the_width_of_the_cutter_agrees(self):
        # The knife edge between "the bit goes in" and "the slot is sealed",
        # approached from a ten-thousandth of a micrometre away.
        self.check(slot(width=2.0000001), 1.0, "slot at the cutter width")

    def test_a_recess_tighter_than_the_cutter_agrees(self):
        self.check(rounded_recess(0.2), 1.0, "tight recess")

    def test_a_recess_the_cutter_fits_agrees(self):
        self.check(rounded_recess(1.5), 1.0, "roomy recess")

    def test_agreement_holds_across_the_cutter_range(self):
        # The kernels have to agree about the bit, not about one bit.
        chain = corpus_chain()
        for diameter in (0.4, 0.8, 0.889, 1.2, 2.0, 3.175):
            self.check(chain, diameter / 2.0,
                       f"board outline at {diameter} mm")


@unittest.skipIf(Polygon is None, "shapely is not installed in this interpreter")
class MillShapeTest(unittest.TestCase):
    """What the resolved offset says about the geometry, in plain numbers."""

    def test_the_sealed_slot_is_two_fillets_of_the_bit_radius(self):
        loop = resolve_offset(slot(width=0.5), 1.0).loop
        arcs = [
            piece for piece in loop
            if isinstance(piece, Arc) and abs(piece.center[1]) < 1e-9
            and 4.0 < piece.center[0] < 6.0
        ]
        self.assertEqual(len(arcs), 2)
        for arc in arcs:
            self.assertAlmostEqual(arc.radius, 1.0, places=12)

    def test_the_area_grows_by_the_dilation_and_no_more(self):
        # A closed form for the square: perimeter band plus one whole disc.
        loop = resolve_offset(square(10.0), 1.0).loop
        expected = 100.0 + 4 * 10.0 * 1.0 + math.pi
        self.assertAlmostEqual(abs(signed_area(loop)), expected, places=9)

    def test_an_unreachable_recess_is_named_not_dropped_in_silence(self):
        result = resolve_offset(slot(width=0.5), 1.0)
        self.assertEqual(len(result.unreachable), 1)
        run = result.unreachable[0]
        self.assertEqual(run.items, 3)  # both walls and the end
        self.assertAlmostEqual(run.fillet_mm, 1.0)

    def test_a_recess_the_cutter_fits_leaves_nothing_unreachable(self):
        self.assertEqual(resolve_offset(slot(width=3.0), 1.0).unreachable, [])


class FrozenOutlineTest(unittest.TestCase):
    """The frozen outline is the board file's outline, item for item.

    Runs without shapely. It is what keeps the fixture and the board from
    drifting apart if either one is made again.
    """

    def test_every_frozen_item_is_an_edge_cuts_item_of_the_board(self):
        from eedesigncore import Board

        board = Board.from_file(BOARD)
        on_board = set()
        for drawing in board.drawings:
            if getattr(drawing, "layer", None) != "Edge.Cuts":
                continue
            ends = (tuple(drawing.start), tuple(drawing.end))
            on_board.add(ends)
            on_board.add(ends[::-1])
        frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]
        self.assertGreater(len(frozen), 30)
        for entry in frozen:
            with self.subTest(source=entry["source"]):
                self.assertIn((tuple(entry["start"]), tuple(entry["end"])),
                              on_board)

    def test_the_frozen_outline_closes_into_one_loop(self):
        self.assertEqual(len(corpus_chain()),
                         len(json.loads(FIXTURE.read_text("utf-8"))["items"]))


if __name__ == "__main__":
    unittest.main()
