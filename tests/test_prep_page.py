"""What the prep review page SAYS, asserted against the bytes it writes.

This file exists because of a real miss. The stage knew a layer could not be
plotted and said so - to stdout, through `log()`. The page said nothing, the
terminal scrolled away, and the report claimed the page carried the notice. A
string in the generator is not a string on the page, so everything here reads
the written HTML back off disk.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from eedesigncore import Board, Footprint, PcbLine, Setup

from eedesignpanels.prep import (
    PROPOSAL_LAYER,
    PROPOSAL_LAYER_DISPLAY,
    REVIEW_LAYERS,
    candidate_notice,
    hole_marks,
    hosted_outline_notice,
    missing_layer_notice,
    proposal_layer,
    score_notice,
    stale_notice,
)
from eedesignpanels.review import overlay, page, render, theme
from eedesignpanels.stages.route_toolpath import (
    OUTLINE_LAYER,
    SCORE_LAYER,
    STOP_LINE_LAYER,
    TOOLPATH_LAYER,
    VERDICT_PROPOSED,
    VERDICT_REJECTED,
    VERDICT_SCORED,
    Proposal,
    candidate_id,
    score_label,
)
from eedesignpanels.toolpath import Line, chain_loop, score_candidates

# The hole colours the PCB editor uses, as KiCad's built-in default theme
# carries them: `plated_hole` is the NON-plated one (color_settings.cpp:136).
NON_PLATED = "rgb(26, 196, 210)"
PLATED = "rgb(227, 183, 46)"
HOLE_THEME = theme.Theme(
    "t",
    {"copper": {"f": "rgb(200, 52, 52)", "b": "rgb(77, 127, 196)"},
     "plated_hole": NON_PLATED, "via_hole": PLATED},
    "rgb(0, 16, 35)",
)

# The smallest thing `inline_svg` accepts: a viewBox, a width and a height.
MINIMAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10.0000mm" '
    'height="10.0000mm" viewBox="0.000000 0.000000 10.000000 10.000000">'
    '<path d="M 0 0 L 10 10" style="stroke:#000000"/></svg>'
)


class HostedOutlineNoticeTest(unittest.TestCase):
    """The page says when part of the edge came out of a footprint.

    On the render a break contour and a board edge are the same black line, and
    which is which decides whether the router is meant to cut it.
    """

    def board_with(self, hosted: int) -> Board:
        return Board(
            version=20260206, generator="pcbnew", generator_version="10.0",
            layers=[], setup=Setup(pad_to_mask_clearance=0.1),
            footprints=[Footprint(
                fp_id="PanelTab:Tab", layer="F.Cu", uuid="fp",
                at={"position": (0.0, 0.0), "angle": 0.0},
                drawings=[
                    PcbLine(start=(0.0, 0.0), end=(1.0, 0.0),
                            layer=OUTLINE_LAYER, id=f"c{index}")
                    for index in range(hosted)
                ],
            )],
        )

    def test_a_board_whose_edge_is_all_its_own_says_nothing(self):
        self.assertEqual(hosted_outline_notice(self.board_with(0)), "")

    def test_a_hosted_contour_is_counted_and_named(self):
        notice = hosted_outline_notice(self.board_with(5))
        self.assertIn("5 break-contour items", notice)
        self.assertIn("tab footprints", notice)


class Stub:
    """Just enough of a `PrepResult` for the sentences that read one."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class ScoreNoticeTest(unittest.TestCase):
    """A scored edge and a missed edge look identical on a plot.

    The toolpath simply is not there in either case, so the page has to say
    which it is - in the designer's terms, not the geometry's.
    """

    def result(self, spans=(), reached=True, past=1.0, standing=0.0):
        """A stub result whose junctions carry the VERDICT as well as the quota.

        The two are different questions: `reached` is whether the
        fab's overshoot was delivered, `normal` is whether the board separates.
        A junction with `standing` material is the one the designer acts on.
        """
        scores = [Stub(length_mm=span) for span in spans]
        junctions = [
            Stub(
                reached=reached,
                past_mm=past if reached else 0.0,
                standing_mm=standing,
                normal=standing <= 0.0,
            )
            for _ in range(2 * len(scores))
        ]
        return Stub(scores=scores, junctions=junctions, route_past_mm=past)

    def test_a_routed_board_says_nothing_about_scores(self):
        self.assertEqual(score_notice(self.result()), "")

    def test_one_scored_edge_names_its_length_and_the_overshoot(self):
        notice = score_notice(self.result([10.5]))
        self.assertIn("1 edge v-scored", notice)
        self.assertIn("10.500 mm", notice)
        self.assertIn("1.0000 mm past each score", notice)

    def test_the_overshoot_is_told_as_where_the_cut_leaves_the_board_edge(self):
        # The number is the cutter's TANGENT POINT, so the page says what it
        # measures. "The route runs N mm past" would read as the outer extreme
        # of the bit's round end.
        self.assertIn(
            "the cut leaving the board edge",
            score_notice(self.result([10.5])),
        )

    def test_several_edges_are_counted_and_listed(self):
        notice = score_notice(self.result([10.5, 6.5]))
        self.assertIn("2 edges v-scored", notice)
        self.assertIn("10.500 mm and 6.500 mm", notice)

    def test_a_junction_the_shape_cut_short_is_told_as_the_shape(self):
        # The overshoot was not delivered and nothing stands, so the
        # page says why - the board's own edge ends there - rather than calling
        # a good corner short.
        notice = score_notice(self.result([6.5], reached=False))
        self.assertIn("the board edge ends at the score", notice)
        self.assertNotIn("standing", notice)

    def test_a_junction_that_leaves_material_standing_is_said_out_loud(self):
        notice = score_notice(
            self.result([6.5], reached=False, standing=1.2245)
        )
        self.assertIn("falls short of the score", notice)
        self.assertIn("1.2245 mm of board edge standing", notice)


class NoticeWordingTest(unittest.TestCase):
    def test_nothing_missing_says_nothing(self):
        self.assertEqual(missing_layer_notice([], 8), "")

    def test_missing_stop_lines_name_the_layer_and_the_count(self):
        notice = missing_layer_notice([STOP_LINE_LAYER], 8)
        self.assertIn(STOP_LINE_LAYER, notice)
        self.assertIn("8", notice)

    def test_it_says_the_toolpath_is_still_right(self):
        # The designer has to be able to tell "the board cannot draw its tabs"
        # from "the toolpath is wrong". Those call for opposite actions.
        notice = missing_layer_notice([STOP_LINE_LAYER], 8).lower()
        self.assertIn("honoured", notice)
        self.assertIn("complete", notice)
        self.assertIn("layer table", notice)

    def test_a_missing_toolpath_layer_is_a_different_and_harder_problem(self):
        notice = missing_layer_notice([TOOLPATH_LAYER], 8)
        self.assertIn("fabrication output", notice)
        self.assertIn(TOOLPATH_LAYER, notice)

    def test_a_missing_score_layer_is_named_too(self):
        notice = missing_layer_notice([SCORE_LAYER], 8)
        self.assertIn(SCORE_LAYER, notice)
        self.assertIn("v-scores", notice)
        self.assertIn("fabrication output", notice)

    def test_a_missing_outline_is_named_too(self):
        self.assertIn(OUTLINE_LAYER, missing_layer_notice([OUTLINE_LAYER], 0))

    def test_several_missing_layers_each_get_a_sentence(self):
        notice = missing_layer_notice([STOP_LINE_LAYER, TOOLPATH_LAYER], 4)
        self.assertIn(STOP_LINE_LAYER, notice)
        self.assertIn(TOOLPATH_LAYER, notice)


class EmittedPageTest(unittest.TestCase):
    """The notice has to survive into the file a browser opens."""

    def build(self, subtitle: str) -> str:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            svg = root / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            layers = [render.LayerSpec(
                ordinal=25, canonical_name=OUTLINE_LAYER,
                layer_type="user", display_name=OUTLINE_LAYER,
            )]
            out = page.build_page(
                out_path=root / "page.html",
                heading="Route toolpath - test",
                subtitle=subtitle,
                layers=layers,
                views=[page.BoardView(
                    title="test", svg_by_layer={OUTLINE_LAYER: svg}
                )],
                theme=theme.load_theme(),
            )
            return out.read_text(encoding="utf-8")

    def test_the_notice_reaches_the_written_html(self):
        notice = missing_layer_notice([STOP_LINE_LAYER], 8)
        markup = self.build(f"{notice}  //  Cutter diameter 0.8890 mm")
        self.assertIn(STOP_LINE_LAYER, markup)
        self.assertIn("Tab stop lines are not shown", markup)
        self.assertIn("All 8 were read and honoured", markup)

    def test_the_notice_is_readable_text_and_not_swallowed_by_escaping(self):
        notice = missing_layer_notice([STOP_LINE_LAYER], 8)
        markup = self.build(notice)
        # `build_page` escapes the subtitle, so an apostrophe or a dash must
        # still come back as something a person reads, not as an entity soup.
        self.assertIn("board&#x27;s layer table", markup)
        self.assertNotIn("&lt;", notice)

    def test_a_page_with_nothing_missing_carries_no_notice(self):
        markup = self.build("Cutter diameter 0.8890 mm - 4 tabs")
        self.assertNotIn("are not shown", markup)
        self.assertIn("Cutter diameter", markup)


# ---------------------------------------------------------------------------
# the proposal: candidates the board does not carry, drawn by the page
# ---------------------------------------------------------------------------

REJECTED_REASON = (
    "the blade line passes through 2 tabs, starting with TAB @ (5.000, 0.000) "
    "- an edge with a tab on it is routed, not scored"
)


def square_proposals(side: float = 10.0) -> list:
    """Four real candidate blade lines, one of each verdict and a spare.

    Real geometry from the real proposer - only the verdicts are staged - so
    what the page draws here is the shape it draws on a board. The three states
    are all present, because the whole reason the page carries this overlay is
    that a reader has to tell them apart.
    """
    corners = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
    chain = chain_loop([
        Line(p0=corners[index], p1=corners[(index + 1) % 4], source=f"e{index}")
        for index in range(4)
    ])
    proposals = []
    for index, candidate in enumerate(score_candidates(chain)):
        if index == 0:
            accepted, proposable = True, True
            verdict = VERDICT_SCORED
            reason, summary = "cut by the saw", "cut by the saw, not the router"
        elif index == 1:
            accepted, proposable = False, True
            verdict = VERDICT_PROPOSED
            reason = "no User.38 line on the board accepts it"
            summary = "proposed; no score line on the board accepts it"
        else:
            accepted, proposable = False, False
            verdict = VERDICT_REJECTED
            reason = REJECTED_REASON
            summary = "the blade line passes through 2 tabs"
        proposals.append(Proposal(
            id=candidate_id(candidate),
            label=score_label(candidate),
            candidate=candidate,
            accepted=accepted,
            verdict=verdict,
            reason=reason,
            summary=summary,
            proposable=proposable,
        ))
    return proposals


class CandidateNoticeTest(unittest.TestCase):
    """The page has to say what was PROPOSED, not only what survived."""

    def result(self, proposals):
        return Stub(proposals=proposals)

    def test_a_board_with_no_candidate_says_nothing(self):
        self.assertEqual(candidate_notice(self.result([])), "")

    def test_the_count_is_of_blade_lines_in_three_states(self):
        notice = candidate_notice(self.result(square_proposals()))
        self.assertIn("4 candidate blade lines found", notice)
        self.assertIn("1 scored", notice)
        self.assertIn("1 proposed and not accepted", notice)
        self.assertIn("2 rejected", notice)

    def test_a_line_the_designer_declined_is_not_called_a_rejection(self):
        # The tool proposes and the designer declines. Saying "rejected" about a
        # person's own choice tells them the tool disagreed with them, which it
        # does not - it has no opinion about which edges get scored.
        notice = candidate_notice(self.result(square_proposals()))
        self.assertIn("proposed and not accepted", notice)
        self.assertIn("no score line on the board accepts it", notice)

    def test_every_rejection_is_named_with_its_reason(self):
        notice = candidate_notice(self.result(square_proposals()))
        for entry in square_proposals():
            if entry.accepted:
                continue
            self.assertIn(entry.short_id, notice)
        self.assertIn("passes through 2 tabs", notice)

    def test_it_points_at_the_overlay_that_shows_them(self):
        notice = candidate_notice(self.result(square_proposals()))
        self.assertIn(PROPOSAL_LAYER_DISPLAY, notice)

    def test_a_long_rejection_list_is_capped_and_says_so(self):
        many = square_proposals() + [
            entry._replace(id=f"{index:032d}", accepted=False,
                           verdict=VERDICT_REJECTED, proposable=False,
                           reason=REJECTED_REASON)
            for index, entry in enumerate(square_proposals())
        ]
        turned_down = sum(1 for entry in many if not entry.accepted)
        notice = candidate_notice(self.result(many))
        self.assertIn(f"and {turned_down - 4} more", notice)


class StaleNoticeTest(unittest.TestCase):
    """A score the outline moved out from under still LOOKS like a score."""

    def result(self, count: int):
        return Stub(stale_scores=[
            Stub(line=Stub(start=(float(index), 1.0), end=(float(index), 9.0)),
                 reason="this score no longer lies on a scoreable edge")
            for index in range(count)
        ])

    def test_a_board_whose_decisions_all_match_says_nothing(self):
        self.assertEqual(stale_notice(self.result(0)), "")

    def test_one_stale_line_is_named_by_position(self):
        notice = stale_notice(self.result(1))
        self.assertIn("1 User.38 line", notice)
        self.assertIn("(0.000, 1.000) to (0.000, 9.000)", notice)
        self.assertIn("the outline changed", notice)

    def test_it_says_the_router_still_cuts_that_edge(self):
        # The one thing a reader must not get wrong about a stale score.
        self.assertIn("router still cuts", stale_notice(self.result(2)))

    def test_a_long_list_is_capped(self):
        self.assertIn("and 2 more", stale_notice(self.result(6)))


class ProposalOverlayTest(unittest.TestCase):
    """The overlay is annotation: our own drawing, in the export's own frame.

    The page draws each candidate the way the live preview does - one line in
    two dash states - so one drawing has one meaning on both, and its captions
    sit outside the board like the preview's. This is ANNOTATION, and the
    render under it is still kicad-cli's unmodified output: no board geometry
    is synthesised either way.
    """

    BOX = overlay.ViewBox(0.0, 0.0, 10.0, 10.0)
    CLEARANCE = overlay.label_clearance_mm(0.889)

    def markup(self, origin=(0.0, 0.0)) -> str:
        return overlay.proposal_svg(
            square_proposals(), origin, self.BOX, self.CLEARANCE,
            along_line=True,
        )

    def blades(self, markup: str) -> list:
        """Every blade path as `(solid, d)`, in the order drawn."""
        return [
            ("stroke-dasharray" not in chunk.split("/>")[0],
             chunk.split('d="')[1].split('"')[0])
            for chunk in markup.split('<path class="pb-blade')[1:]
        ]

    def test_every_candidate_is_drawn_accepted_or_not(self):
        # One line each, cut into its dash states: off the board, on it, off it.
        markup = self.markup()
        self.assertEqual(len(self.blades(markup)), 4 * 3)
        self.assertEqual([solid for solid, _ in self.blades(markup)],
                         [False, True, False] * 4)

    def test_the_contact_stretch_is_solid_and_nothing_is_overdrawn(self):
        # No second, bold stroke: an accepted candidate and a rejected one are
        # the same line, and only the colour tells them apart.
        markup = self.markup()
        self.assertNotIn('class="pb-span"', markup)
        widths = {
            chunk.split('stroke-width="')[1].split('"')[0]
            for chunk in markup.split('<path class="pb-blade')[1:]
        }
        self.assertEqual(len(widths), 1)

    def test_a_caption_sits_outside_the_board_it_names(self):
        # The side rule, on the page that had it wrong: every caption is off the
        # board, on the side its own candidate's material is not on.
        for entry in square_proposals():
            candidate = entry.candidate
            side = overlay.label_side(candidate.unit, candidate.normal)
            drawn = overlay.proposal_svg(
                [entry], (0.0, 0.0), self.BOX, self.CLEARANCE, along_line=True
            )
            head = drawn.split('class="pb-label"')[1].split(">")[0]
            x = float(head.split('x="')[1].split('"')[0])
            y = float(head.split('y="')[1].split('"')[0])
            with self.subTest(at=candidate.midpoint):
                # Its anchor is at least the clearance out from the midpoint, in
                # the outward direction and in no other.
                out = ((x - candidate.midpoint[0]) * side[0]
                       + (y - candidate.midpoint[1]) * side[1])
                self.assertGreaterEqual(out, self.CLEARANCE - 1e-9)

    def test_each_of_the_three_verdicts_has_its_own_colour(self):
        markup = self.markup()
        self.assertIn(overlay.ACCEPTED_COLOR, markup)
        self.assertIn(overlay.PROPOSED_COLOR, markup)
        self.assertIn(overlay.REJECTED_COLOR, markup)

    def test_the_colour_follows_the_state_not_the_wording(self):
        # A verdict is a sentence on a page and sentences get reworded; the
        # colour comes off `accepted`/`proposable` so it cannot drift with them.
        scored, offered, refused = square_proposals()[:3]
        self.assertEqual(overlay.verdict_color(scored), overlay.ACCEPTED_COLOR)
        self.assertEqual(overlay.verdict_color(offered), overlay.PROPOSED_COLOR)
        self.assertEqual(overlay.verdict_color(refused), overlay.REJECTED_COLOR)
        self.assertEqual(
            overlay.verdict_color(offered._replace(verdict="anything at all")),
            overlay.PROPOSED_COLOR,
        )

    def test_every_line_carries_its_verdict_and_its_name(self):
        markup = self.markup()
        for entry in square_proposals():
            self.assertIn(f"{entry.short_id} {entry.verdict}", markup)

    def test_the_off_board_stretches_are_dashed_so_they_read_as_annotation(self):
        self.assertIn("stroke-dasharray", self.markup())
        self.assertIn(False, [solid for solid, _ in self.blades(self.markup())])

    def test_board_coordinates_are_shifted_into_the_cropped_page(self):
        # kicad-cli's --page-size-mode 2 puts the board's bbox corner at the
        # viewBox origin, so a board at x = 195.643 draws at x = 0.
        away = self.markup()
        moved = [
            entry._replace(candidate=entry.candidate._replace(
                start=(entry.candidate.start[0] + 195.643,
                       entry.candidate.start[1] + 123.271),
                end=(entry.candidate.end[0] + 195.643,
                     entry.candidate.end[1] + 123.271),
                spans=tuple(
                    span._replace(
                        start=(span.start[0] + 195.643, span.start[1] + 123.271),
                        end=(span.end[0] + 195.643, span.end[1] + 123.271),
                    )
                    for span in entry.candidate.spans
                ),
            ))
            for entry in square_proposals()
        ]
        shifted = overlay.proposal_svg(
            moved, (195.643, 123.271), self.BOX, self.CLEARANCE, along_line=True
        )
        self.assertEqual(shifted, away)

    def test_it_is_a_standalone_svg_with_the_export_s_viewbox(self):
        markup = self.markup()
        self.assertTrue(markup.startswith("<svg "))
        self.assertIn('viewBox="0.0000 0.0000 10.0000 10.0000"', markup)
        # Portability: a review file is one self-contained file, so nothing
        # is fetched and nothing is linked.
        for banned in ("<script", "href", "url(", "@import"):
            self.assertNotIn(banned, markup)

    def test_a_viewbox_is_read_back_off_an_exported_file(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            self.assertEqual(
                overlay.viewbox_of(svg), overlay.ViewBox(0.0, 0.0, 10.0, 10.0)
            )

    def test_an_svg_with_no_viewbox_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / "empty.svg"
            svg.write_text("<svg></svg>", encoding="utf-8")
            with self.assertRaises(ValueError):
                overlay.viewbox_of(svg)


class ProposalOnThePageTest(unittest.TestCase):
    """THE DEFECT THIS EXISTS FOR: the page showed only the accepted scores.

    A designer reviewing a proposal has to see the candidates that were turned
    down, in the file a browser opens - not in a terminal that has scrolled away.
    """

    CLEARANCE = overlay.label_clearance_mm(0.889)

    def build(self) -> str:
        proposals = square_proposals()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            svg = root / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            drawn = root / "candidates.svg"
            box = overlay.viewbox_of(svg)
            drawn.write_text(
                overlay.proposal_svg(
                    proposals, (0.0, 0.0), box, self.CLEARANCE, along_line=True
                ),
                encoding="utf-8",
            )
            layers = [render.LayerSpec(25, OUTLINE_LAYER, "user", OUTLINE_LAYER)]
            layers = [*layers, proposal_layer(layers)]
            out = page.build_page(
                out_path=root / "page.html",
                heading="Route toolpath - test",
                subtitle=candidate_notice(Stub(proposals=proposals)),
                layers=layers,
                views=[page.BoardView(
                    title="test",
                    svg_by_layer={OUTLINE_LAYER: svg, PROPOSAL_LAYER: drawn},
                )],
                theme=theme.load_theme(),
                start_visible=(PROPOSAL_LAYER,),
                annotations=(PROPOSAL_LAYER,),
                pad_mm=overlay.label_reach_mm(box.larger, self.CLEARANCE),
            )
            return out.read_text(encoding="utf-8")

    def test_the_rejected_candidates_reach_the_written_html(self):
        markup = self.build()
        for entry in square_proposals():
            if entry.accepted:
                continue
            self.assertIn(entry.short_id, markup)
        self.assertIn("rejected", markup)
        self.assertIn("passes through 2 tabs", markup)

    def test_the_captions_have_air_on_the_page_to_be_drawn_in(self):
        # They sit outside the board and kicad-cli's page stops AT the board, so
        # the viewport is padded to hold them - by the same amount on every
        # layer of the stack, or the layers slide apart under pan and zoom.
        boxes = set(re.findall(r'viewBox="([^"]+)"', self.build()))
        self.assertEqual(len(boxes), 1, boxes)
        x, y, width, height = (float(value) for value in boxes.pop().split())
        pad = overlay.label_reach_mm(10.0, self.CLEARANCE)
        self.assertAlmostEqual(-x, pad, places=4)
        self.assertAlmostEqual(-y, pad, places=4)
        self.assertAlmostEqual(width, 10.0 + 2 * pad, places=4)
        self.assertAlmostEqual(height, 10.0 + 2 * pad, places=4)
        self.assertGreater(pad, self.CLEARANCE)

    def test_the_overlay_is_stacked_as_its_own_toggleable_layer(self):
        markup = self.build()
        self.assertIn(f'data-layer="{PROPOSAL_LAYER}"', markup)
        self.assertIn(PROPOSAL_LAYER_DISPLAY, markup)

    def test_the_overlay_opens_visible_but_is_not_a_board_layer(self):
        entry = baked_layers(self.build())[PROPOSAL_LAYER]
        self.assertTrue(entry["start"], "a proposal nobody sees is not reviewed")
        self.assertFalse(
            entry["physical"], "annotation never becomes the finished board"
        )

    def test_the_page_stays_self_contained(self):
        markup = self.build()
        for banned in ("<link", "<script src", "@import", "fetch("):
            self.assertNotIn(banned, markup)


def baked_layers(markup: str) -> dict:
    """The layer table the page's script actually opens with, by canonical name.

    Read out of the emitted HTML rather than asked of the generator, because
    the opening state of the viewer is the thing being checked and only the
    file can answer it.
    """
    found = re.search(r"const LAYERS\s*=\s*(\[.*?\]);", markup, re.S)
    if not found:
        raise AssertionError("the page bakes no LAYERS table into its script")
    return {entry["canonical"]: entry for entry in json.loads(found.group(1))}


class OpeningVisibilityTest(unittest.TestCase):
    """The page has to OPEN showing what its title promises."""

    LAYERS = [
        render.LayerSpec(25, OUTLINE_LAYER, "user", OUTLINE_LAYER),
        render.LayerSpec(53, STOP_LINE_LAYER, "user", "User.8-RouteToolStops"),
        render.LayerSpec(109, TOOLPATH_LAYER, "user", "User.36-RouteToolPath"),
        render.LayerSpec(113, SCORE_LAYER, "user", "User.38-VScore"),
        render.LayerSpec(69, "User.16", "user", "User.16-Dimensions"),
    ]

    def build(self, start_visible) -> str:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            svg = root / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            out = page.build_page(
                out_path=root / "page.html",
                heading="Route toolpath - test",
                subtitle="test",
                layers=self.LAYERS,
                views=[page.BoardView(
                    title="test", svg_by_layer={OUTLINE_LAYER: svg}
                )],
                theme=theme.load_theme(),
                start_visible=start_visible,
            )
            return out.read_text(encoding="utf-8")

    def test_every_stage_layer_opens_visible(self):
        entries = baked_layers(self.build(REVIEW_LAYERS))
        for name in (OUTLINE_LAYER, STOP_LINE_LAYER, TOOLPATH_LAYER, SCORE_LAYER):
            self.assertTrue(entries[name]["start"], f"{name} should open visible")

    def test_the_score_layer_is_one_of_them(self):
        # A page that promises v-scores and opens with the layer hidden behind a
        # toggle has told the reader the board has none.
        self.assertIn(SCORE_LAYER, REVIEW_LAYERS)

    def test_the_stop_line_layer_is_the_one_that_needed_saying(self):
        # Edge.Cuts and User.36 are physical by the generic rule; User.8 is not,
        # so it is the layer this override exists for.
        entries = baked_layers(self.build(REVIEW_LAYERS))
        self.assertFalse(entries[STOP_LINE_LAYER]["physical"])
        self.assertTrue(entries[STOP_LINE_LAYER]["start"])

    def test_layers_the_page_did_not_ask_for_still_open_hidden(self):
        entries = baked_layers(self.build(REVIEW_LAYERS))
        self.assertFalse(entries["User.16"]["start"])

    def test_without_the_override_the_shared_default_is_unchanged(self):
        # Every other page must keep opening exactly as it did.
        entries = baked_layers(self.build(None))
        self.assertFalse(entries[STOP_LINE_LAYER]["start"])
        self.assertTrue(entries[OUTLINE_LAYER]["start"])
        self.assertTrue(entries[TOOLPATH_LAYER]["start"])
        for entry in entries.values():
            self.assertEqual(entry["start"], entry["physical"])


class PrepHoleMarksTest(unittest.TestCase):
    """The toolpath page shows the holes, and it plots copper to get them.

    A tab IS its drill holes, so the page a designer judges a tab on has to
    show them. The copper stack is rendered for that and stays off the page;
    what is asserted here is the step in between, against real kicad-cli
    exports (see `test_review_holes`).
    """

    class Export:
        def __init__(self, svg_by_layer):
            self.svg_by_layer = svg_by_layer

    def setUp(self):
        self.data = Path(__file__).parent / "data"
        self.sources = [
            render.LayerSpec(0, "F.Cu", "signal", "F.Cu"),
            render.LayerSpec(2, "B.Cu", "signal", "B.Cu"),
        ]
        self.theme = theme.Theme(
            "t",
            {"copper": {"f": "rgb(200, 52, 52)", "b": "rgb(77, 127, 196)"}},
            "#000",
        )

    def test_the_marks_are_written_and_say_nothing(self):
        export = self.Export({
            "F.Cu": self.data / "holes_export_F_Cu.svg",
            "B.Cu": self.data / "holes_export_B_Cu.svg",
            OUTLINE_LAYER: self.data / "holes_export_F_Cu.svg",
        })
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "svg" / "holes.svg"
            path, marks = hole_marks(self.sources, export, self.theme, out)
            self.assertEqual(path, out)
            self.assertTrue(out.is_file())
        self.assertEqual(marks.notice, "")
        # The same four holes on both copper layers, drawn once.
        self.assertEqual(marks.elements, 4)

    def test_a_board_that_plots_no_copper_gets_no_row_and_no_sentence(self):
        with tempfile.TemporaryDirectory() as folder:
            path, marks = hole_marks(
                self.sources, self.Export({}), self.theme,
                Path(folder) / "svg" / "holes.svg",
            )
        self.assertIsNone(path)
        self.assertEqual(marks.notice, "")

    def test_the_marks_take_the_editors_hole_colours_from_the_theme(self):
        # The overlay matches the EDITOR, so an
        # NPTH is `board.plated_hole` light blue and not a black void.
        export = self.Export({"F.Cu": self.data / "holes_export_F_Cu.svg"})
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "svg" / "holes.svg"
            path, _ = hole_marks(self.sources, export, HOLE_THEME, out)
            written = path.read_text(encoding="utf-8")
        self.assertIn(f"fill:{NON_PLATED};", written)
        self.assertIn(f"fill:{PLATED};", written)
        self.assertNotIn("fill:#FFFFFF", written)
        self.assertNotIn("fill:#000000", written)


class ToolpathPageHolesTest(unittest.TestCase):
    """The holes on the TOOLPATH page itself, read back off the written file.

    This host and the panel review host share `page.build_page`, and both are
    fenced on their own emitted page: a shared branch is exactly the kind of
    thing that gets changed for one caller and silently changes the other.
    """

    LAYERS = [
        render.LayerSpec(25, OUTLINE_LAYER, "user", OUTLINE_LAYER),
        render.LayerSpec(53, STOP_LINE_LAYER, "user", "User.8-RouteToolStops"),
        render.LayerSpec(109, TOOLPATH_LAYER, "user", "User.36-RouteToolPath"),
    ]

    def build(self) -> str:
        from eedesignpanels.review import holes

        data = Path(__file__).parent / "data"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            svg = root / "edge.svg"
            svg.write_text(MINIMAL_SVG, encoding="utf-8")
            marks, _ = holes.write_beside(
                [("F.Cu", data / "holes_export_F_Cu.svg")], {},
                root / "holes.svg",
                palette=holes.editor_palette(HOLE_THEME),
            )
            layers = [*self.LAYERS, holes.holes_layer(self.LAYERS)]
            out = page.build_page(
                out_path=root / "page.html",
                heading="Route toolpath - test",
                subtitle="test",
                layers=layers,
                views=[page.BoardView(
                    title="test",
                    svg_by_layer={OUTLINE_LAYER: svg,
                                  holes.HOLES_LAYER: marks},
                )],
                theme=holes.with_swatch(HOLE_THEME),
                start_visible=(*REVIEW_LAYERS, holes.HOLES_LAYER),
                on_top=(holes.HOLES_LAYER,),
            )
            return out.read_text(encoding="utf-8")

    def test_the_row_opens_visible_and_stacks_on_top(self):
        from eedesignpanels.review import holes

        entry = baked_layers(self.build())[holes.HOLES_LAYER]
        self.assertTrue(entry["start"])
        self.assertTrue(entry["top"])
        self.assertFalse(entry["physical"])

    def test_H_dims_and_hides_the_holes_with_every_other_layer(self):
        # The holes take no exemption: they dim and hide like any layer.
        markup = self.build()
        self.assertIn(
            "if (on && !isActive && panel.mode === 1) opacity *= 0.22;", markup
        )
        self.assertIn(
            "if (on && !isActive && panel.mode === 2) shown = false;", markup
        )
        self.assertNotIn("!layer.top && panel.mode", markup)

    def test_the_holes_keep_their_top_stacking_order(self):
        self.assertIn("zIndex = layer.top ? 999 : (isActive ? 99 : 1)",
                      self.build())

    def test_the_overlay_on_the_page_is_painted_the_editors_colours(self):
        from eedesignpanels.review import holes

        markup = self.build()
        stack = markup.split(f'data-layer="{holes.HOLES_LAYER}"')[1]
        overlay_svg = stack[:stack.index("</svg>")]
        self.assertIn(f"fill:{NON_PLATED};", overlay_svg)
        self.assertIn(f"fill:{PLATED};", overlay_svg)


if __name__ == "__main__":
    unittest.main()
