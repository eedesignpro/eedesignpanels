"""What the preview CALLS things: `#1`, three words, and no bare `User.##`.

Each naming behavior below is pinned here.

**A candidate is numbered, not hashed.** `#1`, `#2`, ... clockwise from the top
left of the outline's bounding box, so a row in the controls column and a
caption on the picture name the same line and a reader can match them. Board
millimetres put +y down, which is why clockwise is an *increasing* bearing; the
ordering is asserted on a synthetic board whose four candidates sit on the four
sides, where the right answer is stateable rather than merely plausible.

**Three verdict words, and only three.** SCORED, REJECTED, PROPOSED - derived
from the two booleans, so the stage's own longer wording (which the frozen
review page carries) and the page's wording cannot silently swap.

**A label sits clear of the routed channel, OUTSIDE the board**: one router
diameter and a stated margin off the outline, measured square off the blade
line, on the side the material is NOT on - never on a side the stored line's
start/end order chose. "Top of a horizontal line, right of a vertical one" is
the tie-break for a line neither side of which is board; used alone it would
put captions inside the outline, over board content. The window the preview renders grows to
hold the caption, and the frozen page pads its viewport for the same reason.

**A candidate is one line in two dash states.** Dashed where the blade runs off
the board, solid along the stretch it contacts the board edge, for all three
verdicts alike, with no heavier second stroke. Both pages draw it the same
way: one drawing, one meaning.

**No bare `User.##` reaches a reader.** The mapping comes from the board's own
layer table and the rule is checked on finished markup.

The frozen review artifact is checked from the other side too: it keeps the
stage's own wording and sets its captions ALONG the line they name, which is the
only thing about it that differs from the live preview.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from eedesignpanels import prep
from eedesignpanels.preview import labels, naming, page_served
from eedesignpanels.preview import session as session_module
from eedesignpanels.preview.session import PreviewSession, view_window
from eedesignpanels.review import overlay
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.stages import route_toolpath
from test_preview import stub_render, write_project
from test_vscore import candidates_of, scoreable_board


def open_square(side: float = 10.0):
    """A square with no tabs, so all four of its edges are candidates.

    The synthetic multi-candidate board the ordering rule is stated against:
    one candidate per side, at a known midpoint, so "clockwise from the top
    left" has exactly one right answer.
    """
    board = scoreable_board(side)
    board.footprints = []
    return board


def proposal(midpoint, accepted=False, proposable=True, name=""):
    """The least of a proposal the labelling needs: a midpoint and two flags."""
    return SimpleNamespace(
        id=name or f"id-{midpoint}",
        accepted=accepted,
        proposable=proposable,
        candidate=SimpleNamespace(midpoint=midpoint),
    )


# ---------------------------------------------------------------------------
# the reading order
# ---------------------------------------------------------------------------


class PhysicalOrderTest(unittest.TestCase):
    """Clockwise from the top left, on a board where that is stateable."""

    BBOX = (0.0, 0.0, 10.0, 10.0)

    def test_the_four_sides_come_out_clockwise_from_the_top_left(self):
        # Top, then right, then bottom, then left: the order a person reads a
        # board in, starting where they start.
        entries = labels.in_physical_order(
            [proposal((0.0, 5.0), name="left"),
             proposal((5.0, 10.0), name="bottom"),
             proposal((10.0, 5.0), name="right"),
             proposal((5.0, 0.0), name="top")],
            self.BBOX,
        )
        self.assertEqual([entry.id for entry in entries],
                         ["top", "right", "bottom", "left"])

    def test_the_numbering_follows_that_order(self):
        marked = labels.labelled(
            [proposal((0.0, 5.0), name="left"), proposal((5.0, 0.0), name="top")],
            self.BBOX,
        )
        self.assertEqual([entry.display_id for entry in marked], ["#1", "#2"])
        self.assertEqual([entry.number for entry in marked], [1, 2])
        self.assertEqual([entry.proposal.id for entry in marked],
                         ["top", "left"])

    def test_the_order_does_not_depend_on_the_order_it_was_given(self):
        given = [proposal((0.0, 5.0), name="left"),
                 proposal((5.0, 10.0), name="bottom"),
                 proposal((10.0, 5.0), name="right"),
                 proposal((5.0, 0.0), name="top")]
        first = [e.id for e in labels.in_physical_order(given, self.BBOX)]
        second = [e.id for e in labels.in_physical_order(given[::-1], self.BBOX)]
        self.assertEqual(first, second)

    def test_two_candidates_on_one_bearing_are_ordered_and_never_arbitrary(self):
        # A tie is broken by distance from the centre and then by id, so the
        # numbering is a function of the geometry and never of a dict order.
        near = proposal((5.0, 4.0), name="b-near")
        far = proposal((5.0, 0.0), name="a-far")
        self.assertEqual(
            [e.id for e in labels.in_physical_order([far, near], self.BBOX)],
            ["b-near", "a-far"],
        )

    def test_the_start_is_the_top_left_corner_and_not_an_axis(self):
        # Measured: the sweep starts at the bounding box's top-left corner, so
        # a candidate just clockwise of that corner is #1 whatever the aspect.
        wide = (0.0, 0.0, 40.0, 10.0)
        key_top = labels.order_key(proposal((20.0, 0.0)), wide)
        key_left = labels.order_key(proposal((0.0, 5.0)), wide)
        self.assertLess(key_top[0], key_left[0])
        corner = math.atan2(-5.0, -20.0)
        self.assertAlmostEqual(
            key_top[0],
            (math.atan2(-5.0, 0.0) - corner) % (2.0 * math.pi),
            places=8,
        )

    def test_a_real_board_numbers_its_four_edges_the_same_way(self):
        board = open_square()
        bbox = route_toolpath.outline_bbox(board)
        entries = labels.in_physical_order(
            [proposal(candidate.midpoint) for candidate in candidates_of(board)],
            bbox,
        )
        self.assertEqual(
            [entry.candidate.midpoint for entry in entries],
            [(5.0, 0.0), (10.0, 5.0), (5.0, 10.0), (0.0, 5.0)],
        )


class VerdictWordTest(unittest.TestCase):
    """Exactly three words, and they come off the booleans."""

    def test_the_three_words_are_the_three_words(self):
        self.assertEqual(
            (labels.SCORED, labels.REJECTED, labels.PROPOSED),
            ("SCORED", "REJECTED", "PROPOSED"),
        )

    def test_accepted_is_scored_whatever_else_is_true(self):
        self.assertEqual(
            labels.verdict_word(proposal((0.0, 0.0), accepted=True,
                                         proposable=False)),
            labels.SCORED,
        )

    def test_a_line_the_tool_will_not_offer_is_rejected(self):
        self.assertEqual(
            labels.verdict_word(proposal((0.0, 0.0), proposable=False)),
            labels.REJECTED,
        )

    def test_everything_else_is_proposed(self):
        self.assertEqual(
            labels.verdict_word(proposal((0.0, 0.0))), labels.PROPOSED
        )

    def test_a_caption_is_the_number_and_the_word(self):
        marked = labels.labelled([proposal((5.0, 0.0), accepted=True)],
                                 (0.0, 0.0, 10.0, 10.0))
        self.assertEqual(labels.captions(marked), ["#1 SCORED"])

    def test_the_words_never_reach_the_stage(self):
        # The stage's own strings are what the frozen review page carries, and
        # this rewording must not have touched them.
        self.assertEqual(route_toolpath.VERDICT_SCORED, "scored")
        self.assertEqual(route_toolpath.VERDICT_PROPOSED,
                         "proposed, not accepted")
        self.assertEqual(route_toolpath.VERDICT_REJECTED, "rejected")


# ---------------------------------------------------------------------------
# where a label sits
# ---------------------------------------------------------------------------


# The four edge directions, as a candidate carries them: the way the line runs,
# the normal pointing INTO the material, and the side a caption must therefore
# go. Written out rather than derived, so the table is the statement of the rule.
EDGES = (
    ("top", (1.0, 0.0), (0.0, 1.0), (0.0, -1.0)),
    ("right", (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)),
    ("bottom", (-1.0, 0.0), (0.0, -1.0), (0.0, 1.0)),
    ("left", (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)),
)


class LabelSideTest(unittest.TestCase):
    """OUT of the material - and the picture's own preference only breaks a tie.

    Used alone, "top of a horizontal line, right of a vertical one" puts two
    corpus captions 1.389 mm INSIDE the outline, over board content. A caption
    must not overlap the board or the toolpath, so the side is the one the
    material is not on, and the top/right preference decides only when neither
    side is material.

    The stored line's start/end order is irrelevant either way: it is an
    accident of how the line was drawn.
    """

    def test_a_caption_goes_to_the_side_the_material_is_not_on(self):
        for name, unit, normal, side in EDGES:
            with self.subTest(edge=name):
                self.assertEqual(overlay.label_side(unit, normal), side)

    def test_the_side_is_the_same_whichever_way_the_line_was_drawn(self):
        for name, unit, normal, side in EDGES:
            with self.subTest(edge=name):
                self.assertEqual(
                    overlay.label_side((-unit[0], -unit[1]), normal), side
                )

    def test_a_diagonal_edge_is_answered_squarely_off_its_own_line(self):
        root = math.sqrt(0.5)
        # Material up and to the right of the line, so the caption goes down and
        # to the left of it.
        side = overlay.label_side((root, root), (-root, root))
        self.assertAlmostEqual(math.hypot(*side), 1.0)
        self.assertAlmostEqual(side[0], root)
        self.assertAlmostEqual(side[1], -root)

    def test_a_blade_with_no_material_side_falls_back_to_top_and_right(self):
        # The tie-break, and the case it is for: a line neither side of which is
        # board. A horizontal one takes the top; a vertical one takes the right.
        for normal in (None, (0.0, 0.0)):
            with self.subTest(normal=normal):
                self.assertEqual(overlay.label_side((1.0, 0.0), normal),
                                 (0.0, -1.0))
                self.assertEqual(overlay.label_side((-1.0, 0.0), normal),
                                 (0.0, -1.0))
                self.assertEqual(overlay.label_side((0.0, 1.0), normal),
                                 (1.0, 0.0))
                self.assertEqual(overlay.label_side((0.0, -1.0), normal),
                                 (1.0, 0.0))

    def test_the_tie_break_takes_whichever_normal_points_up(self):
        root = math.sqrt(0.5)
        side = overlay.label_side((root, root))
        self.assertLess(side[1], 0.0)
        self.assertAlmostEqual(math.hypot(*side), 1.0)
        self.assertEqual(side, overlay.label_side((-root, -root)))

    def test_a_degenerate_direction_does_not_divide_by_zero(self):
        self.assertEqual(overlay.label_side((0.0, 0.0)), (0.0, -1.0))

    def test_the_clearance_is_one_router_diameter_and_the_stated_margin(self):
        self.assertAlmostEqual(
            overlay.label_clearance_mm(0.889),
            0.889 + overlay.LABEL_CLEAR_MARGIN_MM,
        )

    def test_the_reach_is_the_clearance_plus_the_caption_itself(self):
        # What a page has to leave in air: the clearance places the BASELINE, so
        # the caption stands one cap height further out again.
        larger, clearance = 20.0, overlay.label_clearance_mm(0.889)
        font = larger * overlay.LABEL_FRACTION * overlay.OUTSIDE_LABEL_FACTOR
        self.assertAlmostEqual(
            overlay.label_reach_mm(larger, clearance),
            (clearance + font * overlay.LABEL_CAP_FRACTION)
            * overlay.LABEL_FIT_HEADROOM,
        )
        self.assertGreater(overlay.label_reach_mm(larger, clearance), clearance)


class LabelPlacementTest(unittest.TestCase):
    """A caption sits clear of the routed channel, OUTSIDE the board it names.

    Stated on a real square board, whose four candidates come out of the real
    proposer with the four edge directions and the four normals - so "outward"
    is the board's own answer and not one invented in a fixture.
    """

    SIDE = 10.0
    CUTTER = 0.889
    CAPTION = "#1 PROPOSED"

    def clearance(self) -> float:
        return overlay.label_clearance_mm(self.CUTTER)

    def box(self) -> overlay.ViewBox:
        # Air on every side, so nothing under test is placed off the frame. The
        # frame is also what sizes the type, so it is stated once, here.
        return overlay.ViewBox(-5.0, -5.0, self.SIDE + 10.0, self.SIDE + 10.0)

    def bbox(self) -> tuple:
        return (0.0, 0.0, self.SIDE, self.SIDE)

    def candidates(self) -> list:
        """The four edges of a bare square, as the stage derives them."""
        return candidates_of(open_square(self.SIDE))

    def render(self, candidate, caption=None, **kwargs) -> str:
        entry = SimpleNamespace(
            id="0123456789abcdef", verdict="scored", accepted=False,
            proposable=True, candidate=candidate,
        )
        box = self.box()
        # `caption=False` asks for the caller's OWN default wording.
        if caption is not False:
            kwargs["captions"] = [self.CAPTION if caption is None else caption]
        # The origin is the viewBox corner itself, so what is drawn is in board
        # millimetres and the board's own numbers are the answer.
        return overlay.proposal_svg(
            [entry], (box.x, box.y), box, self.clearance(), **kwargs
        )

    def label(self, markup: str):
        """The one caption's attributes, read back off the markup."""
        head = markup.split('class="pb-label"')[1].split(">")[0]
        turned = "rotate(" in head
        return SimpleNamespace(
            x=float(head.split('x="')[1].split('"')[0]),
            y=float(head.split('y="')[1].split('"')[0]),
            size=float(head.split('font-size="')[1].split('"')[0]),
            place=head.split('text-anchor="')[1].split('"')[0],
            angle=float(head.split("rotate(")[1].split()[0]) if turned else 0.0,
            turned=turned,
        )

    def rect(self, label, caption: str) -> tuple:
        """The caption's own box in board millimetres, however it is set.

        Its baseline is at `y` and its capitals rise above it, so in its own
        frame the box runs one cap height up from the anchor; the anchoring says
        where it sits across that, and the rotation carries it back into board
        coordinates.
        """
        width = len(caption) * overlay.LABEL_CHAR_WIDTH * label.size
        height = label.size * overlay.LABEL_CAP_FRACTION
        left = {"start": 0.0, "end": -width, "middle": -width / 2.0}[label.place]
        turn = math.radians(label.angle)
        points = [
            (label.x + x * math.cos(turn) - y * math.sin(turn),
             label.y + x * math.sin(turn) + y * math.cos(turn))
            for x, y in ((left, -height), (left + width, -height),
                         (left + width, 0.0), (left, 0.0))
        ]
        return (min(point[0] for point in points),
                min(point[1] for point in points),
                max(point[0] for point in points),
                max(point[1] for point in points))

    def gap(self, rect: tuple, side: tuple) -> float:
        """How far the caption's NEAREST face lies outside the board, along *side*."""
        def corners(box):
            return ((box[0], box[1]), (box[2], box[1]),
                    (box[2], box[3]), (box[0], box[3]))

        face = max(side[0] * x + side[1] * y for x, y in corners(self.bbox()))
        near = min(side[0] * x + side[1] * y for x, y in corners(rect))
        return near - face

    def test_every_edge_is_labelled_exactly_a_clearance_outside_the_board(self):
        # Measured: whichever edge it names and whichever way that
        # edge was drawn, the caption's nearest face stands one router diameter
        # and the margin off the board, in the air the material is not in.
        for candidate in self.candidates():
            side = overlay.label_side(candidate.unit, candidate.normal)
            label = self.label(self.render(candidate))
            with self.subTest(at=candidate.midpoint):
                self.assertAlmostEqual(
                    self.gap(self.rect(label, self.CAPTION), side),
                    self.clearance(), places=4,
                )

    def test_the_four_edges_go_above_right_below_and_left(self):
        # The corpus board's four captions, named: #1 above the top edge, #2
        # right of the right edge, #3 BELOW the bottom edge, #4 LEFT of the left
        # edge. The last two are the ones the literal reading put over the board.
        placed = {
            candidate.midpoint: self.label(self.render(candidate))
            for candidate in self.candidates()
        }
        self.assertLess(placed[(5.0, 0.0)].y, 0.0)
        self.assertGreater(placed[(10.0, 5.0)].x, self.SIDE)
        self.assertGreater(placed[(5.0, 10.0)].y, self.SIDE)
        self.assertLess(placed[(0.0, 5.0)].x, 0.0)

    def test_no_caption_overlaps_the_board_at_all(self):
        board = self.bbox()
        for candidate in self.candidates():
            rect = self.rect(self.label(self.render(candidate)), self.CAPTION)
            with self.subTest(at=candidate.midpoint):
                self.assertTrue(
                    rect[2] <= board[0] or rect[0] >= board[2]
                    or rect[3] <= board[1] or rect[1] >= board[3],
                    f"caption {rect} lies over the board {board}",
                )

    def test_a_caption_beside_a_line_runs_away_from_it(self):
        # Set off to one side, it has to read outward: `start` on the right of
        # the board, `end` on the left, or it runs back over the material.
        placed = {
            candidate.midpoint: self.label(self.render(candidate))
            for candidate in self.candidates()
        }
        self.assertEqual(placed[(10.0, 5.0)].place, "start")
        self.assertEqual(placed[(0.0, 5.0)].place, "end")
        self.assertEqual(placed[(5.0, 0.0)].place, "middle")
        self.assertEqual(placed[(5.0, 10.0)].place, "middle")

    def test_a_caption_below_the_board_is_dropped_by_its_own_height(self):
        # The subtle one: the clearance places the BASELINE, and glyphs rise
        # above it, so a caption set below the board would grow back over it.
        label = self.label(self.render(
            next(candidate for candidate in self.candidates()
                 if candidate.midpoint == (5.0, self.SIDE))
        ))
        self.assertAlmostEqual(
            label.y,
            self.SIDE + self.clearance()
            + label.size * overlay.LABEL_CAP_FRACTION,
            places=4,
        )

    def test_the_side_does_not_follow_the_stored_line_order(self):
        # The same line, drawn the other way: the caption does not move.
        edge = SimpleNamespace(
            start=(10.0, 0.0), end=(0.0, 0.0), midpoint=(5.0, 0.0),
            unit=(-1.0, 0.0), normal=(0.0, 1.0), spans=[],
        )
        backward = SimpleNamespace(
            start=(0.0, 0.0), end=(10.0, 0.0), midpoint=(5.0, 0.0),
            unit=(1.0, 0.0), normal=(0.0, 1.0), spans=[],
        )
        first, second = self.label(self.render(edge)), self.label(
            self.render(backward))
        self.assertEqual((first.x, first.y), (second.x, second.y))

    def test_a_blade_the_board_does_not_touch_takes_the_top(self):
        # The tie-break, drawn: a line off in the air, no material either side.
        away = SimpleNamespace(
            start=(0.0, -4.0), end=(10.0, -4.0), midpoint=(5.0, -4.0),
            unit=(1.0, 0.0), normal=(0.0, 0.0), spans=[],
        )
        label = self.label(self.render(away))
        self.assertAlmostEqual(label.y, -4.0 - self.clearance(), places=4)
        self.assertEqual(label.place, "middle")

    def test_the_caption_clears_the_channel_the_router_cuts(self):
        # The whole point of the clearance: the channel reaches one diameter out
        # from the edge, so the caption is a stated margin beyond its far face.
        gap = self.clearance() - self.CUTTER
        self.assertAlmostEqual(gap, overlay.LABEL_CLEAR_MARGIN_MM)
        self.assertGreater(gap, 0.0)

    def test_the_caption_stays_inside_the_rendered_window(self):
        # The margin the session renders has to hold the clearance AND the whole
        # caption, or the label is drawn where nobody can
        # see it. `#12 PROPOSED` is the widest one this can produce for a board
        # with fewer than a hundred candidates, and the cutter is what sets the
        # clearance - so a big bit on a small board is the case that binds.
        caption = "#12 PROPOSED"
        for side, cutter in ((10.0, 0.4), (10.0, 3.175), (200.0, 0.889)):
            board = open_square(side)
            bbox = route_toolpath.outline_bbox(board)
            clearance = overlay.label_clearance_mm(cutter)
            window = view_window(bbox, clearance)
            box = overlay.ViewBox(*window)
            for candidate in candidates_of(board):
                entry = SimpleNamespace(id="x", verdict="scored", accepted=False,
                                        proposable=True, candidate=candidate)
                markup = overlay.proposal_svg(
                    [entry], (box.x, box.y), box, clearance, captions=[caption],
                )
                label = self.label(markup)
                low, top, high, base = self.rect(label, caption)
                with self.subTest(side=side, cutter=cutter,
                                  at=candidate.midpoint):
                    self.assertGreaterEqual(low, window[0])
                    self.assertLessEqual(high, window[0] + window[2])
                    self.assertGreaterEqual(top, window[1])
                    self.assertLessEqual(base, window[1] + window[3])

    def test_every_caption_is_set_at_the_outside_size(self):
        # Every caption is outside the board now, so every caption has to fit in
        # the air the view leaves; the picture is vector, so smaller costs
        # nothing a reader cannot zoom back.
        self.assertLess(overlay.OUTSIDE_LABEL_FACTOR, 1.0)
        expected = (
            self.box().larger * overlay.LABEL_FRACTION
            * overlay.OUTSIDE_LABEL_FACTOR
        )
        candidate = self.candidates()[0]
        for along in (False, True):
            with self.subTest(along_line=along):
                self.assertAlmostEqual(
                    self.label(self.render(candidate, along_line=along)).size,
                    expected, places=6,
                )


class FrozenPageCaptionTest(LabelPlacementTest):
    """The frozen page sets its captions ALONG the line, and still outside it.

    Its wording is the stage's own and it is long, so upright it would run out
    into the margin instead of lying beside the edge it names. The SIDE rule is
    the same one.
    """

    def render(self, candidate, caption=None, **kwargs) -> str:
        kwargs.setdefault("along_line", True)
        return super().render(candidate, caption=caption, **kwargs)

    def test_every_caption_is_turned_to_read_along_its_line(self):
        for candidate in self.candidates():
            label = self.label(self.render(candidate))
            with self.subTest(at=candidate.midpoint):
                self.assertTrue(label.turned)
                # Upright either way: never upside down, never past a right angle.
                self.assertGreater(label.angle, -90.0)
                self.assertLessEqual(label.angle, 90.0)

    def test_a_caption_beside_a_line_runs_away_from_it(self):
        # Turned along the line it runs ALONG the edge, never across it, so it
        # is centred on the line whichever side of the board it names.
        for candidate in self.candidates():
            with self.subTest(at=candidate.midpoint):
                self.assertEqual(
                    self.label(self.render(candidate)).place, "middle"
                )

    def test_it_keeps_the_stages_own_wording_for_a_candidate(self):
        # The frozen page names a candidate by short id and the stage's verdict;
        # the numbering and the three words belong to the live preview.
        markup = self.render(self.candidates()[0], caption=False)
        self.assertIn(">01234567 scored<", markup)
        self.assertNotIn("#1", markup)


class CandidateLineTest(unittest.TestCase):
    """One line, two dash states - and no bold overdraw anywhere.

    A contact stretch drawn a second time, twice as thick, reads as a different
    kind of line rather than as the same line over board edge. So the produced
    blade line is cut into stretches: dashed off the board, solid along it, one
    width and one colour, for a scored, a proposed and a rejected candidate
    alike.

    The frozen review page draws the same way: one drawing with two meanings
    is the defect, so `along_line` changes
    how a CAPTION is set and nothing about the line.
    """

    CLEARANCE = overlay.label_clearance_mm(0.889)

    def paths(self, markup: str) -> list:
        """Every blade path as `(solid, width, d)`, in the order drawn."""
        out = []
        for chunk in markup.split('<path class="pb-blade')[1:]:
            body = chunk.split("/>")[0]
            out.append((
                "stroke-dasharray" not in body,
                float(body.split('stroke-width="')[1].split('"')[0]),
                body.split('d="')[1].split('"')[0],
            ))
        return out

    def render(self, candidate, accepted=False, proposable=True, **kwargs):
        entry = SimpleNamespace(
            id="0123456789abcdef", verdict="scored", accepted=accepted,
            proposable=proposable, candidate=candidate,
        )
        box = overlay.ViewBox(-3.0, -3.0, 16.0, 16.0)
        return overlay.proposal_svg(
            [entry], (box.x, box.y), box, self.CLEARANCE,
            captions=["#1 PROPOSED"], **kwargs
        )

    def one_edge(self):
        """The bottom edge of a bare square: one candidate, one contact span."""
        board = open_square()
        return [
            candidate for candidate in candidates_of(board)
            if abs(candidate.midpoint[1]) < 1e-9
        ][0]

    def two_spans(self):
        """A candidate interrupted by a notch: two contact spans on one line."""
        from test_vscore import notched_board

        board = notched_board()
        return [
            candidate for candidate in candidates_of(board)
            if len(candidate.spans) == 2
        ][0]

    def stretches(self, spans, first=(0.0, 0.0), last=(10.0, 0.0)):
        return overlay.contact_stretches(
            [((low, 0.0), (high, 0.0)) for low, high in spans], first, last
        )

    def test_a_span_is_measured_in_the_frame_the_line_is_drawn_in(self):
        # THE DEFECT THIS CAUGHT: the produced line is carried into the export's
        # coordinates and the spans were still in board millimetres, so on a
        # board whose bounding box is not at the origin - every real one - every
        # contact stretch projected off the end of the line and vanished. The
        # overlay drew one dashed line and nothing was solid anywhere.
        box = overlay.ViewBox(0.0, 0.0, 16.0, 16.0)
        candidate = self.one_edge()
        here = self.paths(self.render(candidate))
        moved = SimpleNamespace(
            start=(candidate.start[0] + 195.643, candidate.start[1] + 123.271),
            end=(candidate.end[0] + 195.643, candidate.end[1] + 123.271),
            unit=candidate.unit, normal=candidate.normal,
            midpoint=(candidate.midpoint[0] + 195.643,
                      candidate.midpoint[1] + 123.271),
            spans=[
                SimpleNamespace(start=(span.start[0] + 195.643,
                                       span.start[1] + 123.271),
                                end=(span.end[0] + 195.643,
                                     span.end[1] + 123.271))
                for span in candidate.spans
            ],
        )
        entry = SimpleNamespace(id="0123456789abcdef", verdict="scored",
                                accepted=False, proposable=True,
                                candidate=moved)
        there = self.paths(overlay.proposal_svg(
            [entry], (195.643, 123.271), box, self.CLEARANCE,
            captions=["#1 PROPOSED"],
        ))
        self.assertEqual([solid for solid, _, _ in there],
                         [False, True, False])
        self.assertEqual([solid for solid, _, _ in here],
                         [solid for solid, _, _ in there])

    def test_a_line_that_touches_nothing_is_one_dashed_stretch(self):
        self.assertEqual(self.stretches([]), [(0.0, 10.0, False)])

    def test_the_stretches_cover_the_line_end_to_end_with_no_gap(self):
        drawn = self.stretches([(2.0, 4.0), (6.0, 7.0)])
        self.assertEqual(drawn[0][0], 0.0)
        self.assertEqual(drawn[-1][1], 10.0)
        for before, after in zip(drawn, drawn[1:]):
            self.assertEqual(before[1], after[0])

    def test_two_spans_that_meet_are_one_solid_stretch(self):
        # One line, and a dash state that flickered on and off across a joint
        # would read as two.
        self.assertEqual(
            self.stretches([(2.0, 4.0), (4.0, 6.0)]),
            [(0.0, 2.0, False), (2.0, 6.0, True), (6.0, 10.0, False)],
        )

    def test_a_span_given_backwards_is_the_same_stretch(self):
        self.assertEqual(self.stretches([(4.0, 2.0)]), self.stretches([(2.0, 4.0)]))

    def test_the_contact_stretch_is_solid_and_the_rest_is_dashed(self):
        paths = self.paths(
            self.render(self.one_edge())
        )
        self.assertEqual([solid for solid, _, _ in paths],
                         [False, True, False])

    def test_every_stretch_of_one_candidate_is_drawn_at_one_width(self):
        widths = {width for _, width, _ in self.paths(
            self.render(self.one_edge())
        )}
        self.assertEqual(len(widths), 1)

    def test_nothing_is_overdrawn_on_the_contact_stretch(self):
        markup = self.render(self.one_edge())
        self.assertNotIn("pb-span", markup)

    def test_the_solid_stretch_is_exactly_the_span_it_contacts(self):
        # The origin passed is the viewBox corner itself, so what is drawn is in
        # board millimetres and the span's own ends are the answer.
        candidate = self.one_edge()
        solid = [
            drawn for solid, _, drawn in self.paths(
                self.render(candidate)
            ) if solid
        ]
        self.assertEqual(len(solid), 1)
        span = candidate.spans[0]
        drawn = [
            float(value) for value in
            solid[0].replace("M", "").replace("L", " ").split()
        ]
        self.assertAlmostEqual(min(drawn[0], drawn[2]),
                               min(span.start[0], span.end[0]), places=3)
        self.assertAlmostEqual(max(drawn[0], drawn[2]),
                               max(span.start[0], span.end[0]), places=3)

    def test_a_candidate_broken_by_a_notch_is_solid_only_where_it_touches(self):
        paths = self.paths(
            self.render(self.two_spans())
        )
        self.assertEqual([solid for solid, _, _ in paths],
                         [False, True, False, True, False])

    def test_all_three_verdicts_are_drawn_the_same_way(self):
        candidate = self.one_edge()
        states = [
            self.paths(self.render(candidate, accepted=True)),
            self.paths(self.render(candidate)),
            self.paths(self.render(candidate, proposable=False)),
        ]
        for drawn in states:
            self.assertEqual([solid for solid, _, _ in drawn],
                             [False, True, False])
        # ...and only the colour differs, which is what the verdict is for.
        self.assertEqual(
            len({tuple(entry[2] for entry in drawn) for drawn in states}), 1
        )

    def test_the_frozen_page_draws_the_same_line_as_the_preview(self):
        # A heavy second stroke over the contact stretches would make the same
        # candidate read one way on the frozen page and another way in the live
        # preview. Both draw the stretches, and `along_line` touches only the
        # caption.
        frozen = self.paths(self.render(self.one_edge(), along_line=True))
        live = self.paths(self.render(self.one_edge()))
        self.assertEqual(frozen, live)
        self.assertEqual([solid for solid, _, _ in frozen],
                         [False, True, False])
        self.assertNotIn("pb-span",
                         self.render(self.one_edge(), along_line=True))


class ViewWindowTest(unittest.TestCase):
    """The preview renders air round the board, and says how much."""

    def test_the_window_is_the_box_with_a_margin_on_every_side(self):
        window = view_window((0.0, 0.0, 10.0, 20.0))
        margin = -window[0]
        self.assertAlmostEqual(window[1], -margin)
        self.assertAlmostEqual(window[2], 10.0 + 2 * margin)
        self.assertAlmostEqual(window[3], 20.0 + 2 * margin)
        self.assertGreaterEqual(
            margin, 20.0 * session_module.VIEW_MARGIN_FRACTION
        )

    def test_the_margin_is_of_the_LARGER_dimension_so_it_reads_the_same(self):
        # A long thin board must not get a margin so small on one axis that a
        # caption falls off it.
        window = view_window((0.0, 0.0, 100.0, 5.0))
        self.assertAlmostEqual(window[0], window[1])
        self.assertGreaterEqual(
            -window[0], 100.0 * session_module.VIEW_MARGIN_FRACTION
        )

    def test_the_fraction_is_a_floor_and_the_clearance_can_raise_it(self):
        # A 3 mm bit on a 10 mm coupon: the clearance is millimetres and the
        # margin is a fraction, so the fraction alone would clip the caption.
        plain = view_window((0.0, 0.0, 10.0, 10.0))
        grown = view_window((0.0, 0.0, 10.0, 10.0),
                            overlay.label_clearance_mm(3.175))
        self.assertGreater(-grown[0], -plain[0])

    def test_the_margin_holds_the_clearance_and_the_whole_caption(self):
        # The arithmetic the constants have to satisfy, stated once and checked
        # against the window the session actually renders.
        widest = overlay.LABEL_CAPTION_CHARS
        for larger, clearance in ((10.0, 0.0), (10.0, 3.675), (200.0, 1.389)):
            window = view_window((0.0, 0.0, larger, larger), clearance)
            margin = -window[0]
            font = (
                overlay.LABEL_FRACTION * overlay.OUTSIDE_LABEL_FACTOR
                * max(window[2], window[3])
            )
            room = widest * overlay.LABEL_CHAR_WIDTH * font
            with self.subTest(larger=larger, clearance=clearance):
                self.assertGreaterEqual(margin + 1e-9, clearance + room)


# ---------------------------------------------------------------------------
# the naming rule
# ---------------------------------------------------------------------------


class LayerNameTest(unittest.TestCase):
    """The mapping comes off the board, and nothing is hardcoded."""

    def layers(self):
        return [
            LayerSpec(25, "Edge.Cuts", "user", "Edge.Cuts"),
            LayerSpec(53, "User.8", "user", "User.8-RouteToolStops"),
            LayerSpec(113, "User.38", "user", "User.38-VScore"),
            LayerSpec(115, "User.39", "user", "User.39"),
        ]

    def test_the_mapping_is_the_boards_own_display_names(self):
        self.assertEqual(
            naming.layer_names(self.layers())["User.38"], "User.38-VScore"
        )

    def test_a_sentence_is_rewritten_into_them(self):
        names = naming.layer_names(self.layers())
        self.assertEqual(
            naming.humanize("2 User.38 lines and one User.8 stop", names),
            "2 User.38-VScore lines and one User.8-RouteToolStops stop",
        )

    def test_a_display_name_that_already_carries_one_is_left_alone(self):
        names = naming.layer_names(self.layers())
        self.assertEqual(
            naming.humanize("the User.38-VScore line", names),
            "the User.38-VScore line",
        )

    def test_a_layer_with_no_display_name_of_its_own_is_shown_as_it_is(self):
        # Inventing a name would be worse than showing the one that exists.
        names = naming.layer_names(self.layers())
        self.assertEqual(naming.humanize("on User.39", names), "on User.39")

    def test_a_layer_the_board_never_declared_is_left_alone(self):
        self.assertEqual(naming.humanize("on User.44", {}), "on User.44")


class NamingFenceTest(unittest.TestCase):
    """The rule, read back off finished markup."""

    def test_a_bare_name_in_text_is_a_violation(self):
        self.assertEqual(
            naming.violations("<div>the User.38 line</div>"), ["User.38"]
        )

    def test_a_display_name_is_not(self):
        self.assertEqual(
            naming.violations("<div>the User.38-VScore line</div>"), []
        )

    def test_a_canonical_name_in_a_machine_key_is_not(self):
        # `data-layer` is how the SVG stack is keyed and how the layer panel
        # selects; renaming those would break the page rather than name it.
        self.assertEqual(
            naming.violations('<svg data-layer="User.38"></svg>'), []
        )

    def test_a_script_body_is_not_read_as_text(self):
        self.assertEqual(
            naming.violations(
                '<script>const L=[{"canonical":"User.38"}];'
                "s.replace(/^User\\.\\d+-/, '')</script>"
            ),
            [],
        )

    def test_a_tooltip_IS_read_as_text(self):
        self.assertEqual(
            naming.violations('<div title="User.38 - user"></div>'), ["User.38"]
        )
        self.assertEqual(
            naming.violations('<input placeholder="type User.36">'), ["User.36"]
        )

    def test_a_state_string_the_page_shows_IS_read(self):
        state = {"warnings": ["the User.36 layer is not declared"],
                 "proposals": [{"summary": "on User.38", "blade": "User.9"}]}
        self.assertEqual(
            naming.violations("<div></div>", state), ["User.36", "User.38"]
        )

    def test_a_state_key_that_is_a_machine_key_is_not(self):
        self.assertEqual(
            naming.violations("<div></div>", {"layers": ["User.38"]}), []
        )


class ServedNamingTest(unittest.TestCase):
    """The whole served page, audited - and the sentences it gets it from."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        folder = Path(self._folder.name)
        self.config_path = write_project(folder)
        self.session = PreviewSession(self.config_path, folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.markup = self.session.page()

    def tearDown(self):
        self._folder.cleanup()

    def state_of(self, markup: str) -> dict:
        head = markup.split("let PB = ", 1)[1]
        return json.loads(head[:head.index("\n")].rstrip(";"))

    def test_the_served_page_carries_no_bare_layer_name(self):
        self.assertEqual(
            naming.violations(self.markup, self.state_of(self.markup)), []
        )

    def test_the_subtitle_and_the_notes_are_translated(self):
        fragment = self.session.fragment()
        self.assertEqual(naming.BARE_LAYER.findall(fragment["subtitle"]), [])
        for note in fragment["warnings"]:
            with self.subTest(note=note):
                self.assertEqual(naming.BARE_LAYER.findall(note), [])

    def test_the_heading_uses_the_boards_own_word_for_the_score_layer(self):
        self.assertIn("V-score candidates &middot; User.38-VScore", self.markup)

    def test_the_layer_panel_tooltips_use_them_too(self):
        self.assertIn('title="User.38-VScore - user"', self.markup)
        self.assertNotIn('title="User.38 - user"', self.markup)

    def test_an_action_message_is_translated_before_it_is_shown(self):
        board = self.session.board
        blade = next(
            entry for entry in page_served.proposals_json(self.session.view)
        )["blade"]
        reply = self.session.accept(blade)
        self.assertEqual(naming.BARE_LAYER.findall(reply["message"]), [])
        self.assertIn("User.38-VScore", reply["message"])
        self.assertIsNotNone(board)

    def test_the_frozen_prep_subtitle_still_says_the_canonical_name(self):
        # The other half of the rule: it applies to the UI, and the frozen
        # artifact's wording is unchanged.
        subtitle = prep.subtitle_for(self.session.view.result)
        self.assertIn(route_toolpath.SCORE_LAYER, subtitle)


if __name__ == "__main__":
    unittest.main()
