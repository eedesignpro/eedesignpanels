"""What eedesigncore 0.16.0 answered, and what it left open. Both fenced.

Version 0.16.0 addressed the items this project reported to the board library.
Four of the eight were answered and four were not, and the difference
matters to code in this repository: an answered item DELETES something here or
upgrades what a picture tells the truth about, and an unanswered one keeps a
fence that asserts the wrong value on purpose, so the fence fails the day the
library changes and the local code it justifies can be reviewed.

So each item gets a test, and the test is written to FAIL when the item's state
changes in either direction:

| Item | State at 0.16.0 | What this file fences |
|---|---|---|
| `__version__` | delivered | the pin is asserted against the module actually imported |
| `(tenting none)` | fixed | KiCad's own reproduction board loads |
| dimensions in the renderer | delivered | the fast paint draws a dimension AND its text |
| `to_string` | fixed | not here - it is a measurement, see the review page |
| point marker styling | open | cross and ring still share one style |
| pad angle normalisation | open | pinned in `test_placement_suite.py` |
| `hatch_position` | open | it is still transformed |
| `draw_angle` primitive | open | `stages/upright.py` is still the only port |

The copper opt-in 0.16.0 added is fenced here too: the fast path asks for the
drawings stream and nothing else (see `CopperOptInDeclinedTest` and
`preview/render_seam.py`).
"""

import os
import unittest
from pathlib import Path

import eedesigncore
from eedesigncore import Board
from eedesigncore.render import ALL_CONTENT, LayerStyle, ViewBox, render_board_svg

from eedesignpanels.preview import render_seam
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.stages import upright

# The floor this project needs. 0.16.0 is where the renderer learned to draw a
# dimension, which is what puts the panel's own measurements into the FIRST
# paint rather than into the kicad-cli upgrade two seconds later.
MINIMUM_LIBRARY = (0, 16, 0)

# KiCad's own reproduction for `(tenting none)`, from a clone of the KiCad
# source.
# KICAD_SOURCE_DIR names the clone's top folder. Unset, or a machine with no
# clone - a CI container is one - and the test that reads it skips rather than
# fails.
TENTING_BOARD = (
    Path(os.environ.get("KICAD_SOURCE_DIR", "").strip() or "no-kicad-source")
    / "qa" / "data" / "pcbnew" / "issue22826" / "issue22826.kicad_pcb"
)


def version_tuple(text: str) -> tuple:
    """`"0.16.0"` as `(0, 16, 0)`. Trailing non-numeric parts are dropped."""
    parts = []
    for piece in str(text).split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


# A board with one aligned dimension, its cached measurement text, one plain
# `gr_text`, and an outline to give the view box something to be about. Copied
# in SHAPE from the corpus panel's own dimensions (User.16, aligned, text below
# the crossbar), not in content: the numbers here are round so a failure reads.
DIMENSION_BOARD = """(kicad_pcb (version 20241229) (generator "pcbnew")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)
          (39 "F.SilkS" user "F.Silkscreen") (72 "User.16" user "Dimensions"))
  (setup (pad_to_mask_clearance 0))
  (gr_line (start 0 0) (end 40 0) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "33333333-3333-3333-3333-333333333333"))
  (gr_line (start 40 0) (end 40 40) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "44444444-4444-4444-4444-444444444444"))
  (gr_line (start 40 40) (end 0 40) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "55555555-5555-5555-5555-555555555555"))
  (gr_line (start 0 40) (end 0 0) (stroke (width 0.05) (type solid))
    (layer "Edge.Cuts") (uuid "66666666-6666-6666-6666-666666666666"))
  (gr_text "PANEL"
    (at 20 45 0)
    (layer "F.SilkS")
    (uuid "77777777-7777-7777-7777-777777777777")
    (effects (font (size 2 2) (thickness 0.3)))
  )
  (dimension
    (type aligned)
    (layer "User.16")
    (uuid "88888888-8888-8888-8888-888888888888")
    (pts (xy 0 0) (xy 40 0))
    (height -10)
    (format (prefix "") (suffix "") (units 2) (units_format 1) (precision 3)
      (suppress_zeroes yes))
    (style (thickness 0.1) (arrow_length 1.27) (text_position_mode 1)
      (arrow_direction outward) (extension_height 0.58642)
      (extension_offset 0.5) (keep_text_aligned yes))
    (gr_text "40 mm"
      (at 20 -10 0)
      (layer "User.16")
      (uuid "88888888-8888-8888-8888-888888888888")
      (effects (font (size 2 2) (thickness 0.3)))
    )
  )
)
"""

# One pad and nothing else on F.Cu. The reason the copper opt-in is declined.
PAD_ONLY_BOARD = DIMENSION_BOARD.replace(
    '  (gr_text "PANEL"',
    '  (footprint "test:part"\n'
    '    (layer "F.Cu")\n'
    '    (uuid "99999999-9999-9999-9999-999999999999")\n'
    '    (at 20 20)\n'
    '    (pad "1" smd rect (at 0 0) (size 4 4) (layers "F.Cu")\n'
    '      (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))\n'
    '  )\n'
    '  (gr_text "PANEL"',
)

# One copper track, for the statement from the caller's side.
TRACK_BOARD = DIMENSION_BOARD.replace(
    '  (gr_text "PANEL"',
    '  (segment (start 5 5) (end 35 5) (width 1) (layer "F.Cu")\n'
    '    (net 0) (uuid "cccccccc-cccc-cccc-cccc-cccccccccccc"))\n'
    '  (gr_text "PANEL"',
)

# A board whose only silkscreen is one footprint's fields, for the hidden-field
# check: the Reference sits on F.SilkS and nothing else does.
FIELD_BOARD = DIMENSION_BOARD.replace(
    '  (gr_text "PANEL"\n'
    '    (at 20 45 0)\n'
    '    (layer "F.SilkS")\n'
    '    (uuid "77777777-7777-7777-7777-777777777777")\n'
    '    (effects (font (size 2 2) (thickness 0.3)))\n'
    '  )\n',
    '  (footprint "test:part"\n'
    '    (layer "F.Cu")\n'
    '    (uuid "99999999-9999-9999-9999-999999999999")\n'
    '    (at 20 20)\n'
    '    (property "Reference" "R1"\n'
    '      (at 0 -3 0)\n'
    '      (layer "F.SilkS")\n'
    '      (uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")\n'
    '      (effects (font (size 1 1) (thickness 0.15)))\n'
    '    )\n'
    '  )\n',
)

WINDOW = (-15.0, -15.0, 70.0, 70.0)
LAYER_COLORS = {
    "Edge.Cuts": "rgb(208, 208, 0)",
    "F.SilkS": "rgb(255, 255, 255)",
    "User.16": "rgb(194, 194, 194)",
    "F.Cu": "rgb(200, 52, 52)",
}


def layer_specs(names) -> list:
    """The seam takes `LayerSpec` records, so a test hands it real ones."""
    return [
        LayerSpec(ordinal=index, canonical_name=name, layer_type="user",
                  display_name=name)
        for index, name in enumerate(names)
    ]


def draw(board, names, colors=None, **kwargs) -> dict:
    """One fast-path render through the seam, exactly as the preview calls it."""
    return render_seam.render_with_library(
        board, layer_specs(names), WINDOW, colors or LAYER_COLORS,
        render_board_svg, renderer_name="render_board_svg", **kwargs,
    )


# ---------------------------------------------------------------------------
# `__version__` - delivered
# ---------------------------------------------------------------------------


class LibraryPinTest(unittest.TestCase):
    """The version the CODE reports, which is what the item asked for.

    `importlib.metadata` answers for whatever distribution resolves, which on an
    editable install, a `pip install --target` tree or a shadowed `sys.path` can
    be a different thing from the module that was imported. `__version__`
    travels with the code, so it is the value a golden-file suite may lean on.
    """

    def test_the_imported_module_reports_its_own_version(self):
        self.assertTrue(hasattr(eedesigncore, "__version__"))
        self.assertIsInstance(eedesigncore.__version__, str)

    def test_the_version_is_at_or_above_the_floor_this_project_needs(self):
        found = version_tuple(eedesigncore.__version__)
        self.assertGreaterEqual(
            found, MINIMUM_LIBRARY,
            f"eedesigncore {eedesigncore.__version__} is below the "
            f"{'.'.join(str(n) for n in MINIMUM_LIBRARY)} this project needs: "
            f"below it the renderer draws no dimension, so the preview's first "
            f"paint cannot show the panel's own measurements.",
        )

    def test_version_tuple_reads_a_release_string(self):
        self.assertEqual(version_tuple("0.16.0"), (0, 16, 0))
        self.assertEqual(version_tuple("1.2.3rc1"), (1, 2, 3))
        self.assertEqual(version_tuple("2.0"), (2, 0))


# ---------------------------------------------------------------------------
# `(tenting none)` - fixed
# ---------------------------------------------------------------------------


class TentingLoadTest(unittest.TestCase):
    """A board KiCad wrote with `(tenting (front none) (back none))` loads.

    The reproduction is KiCad's own test board, and the fault was larger than
    this project reported: the library's own assessment found the value form
    raising on 22 of KiCad's 119 modern-format boards, and a second, legacy
    bare-word spelling loading SILENTLY as untented on 35 more. Nothing in the
    panel builder depended on either. The fence is here because the item was
    ours and a regression would be invisible from this side otherwise.
    """

    @unittest.skipUnless(TENTING_BOARD.is_file(),
                         "the read-only KiCad source clone is not on this machine")
    def test_kicads_own_tenting_board_loads(self):
        board = Board.from_file(TENTING_BOARD)
        self.assertTrue(board.footprints)


# ---------------------------------------------------------------------------
# dimensions in the renderer - delivered: the fast paint is honest about
# dimensions and text
# ---------------------------------------------------------------------------


class FastPaintDrawsLetteringTest(unittest.TestCase):
    """The first paint draws a dimension and its text, not an empty layer.

    Before 0.16.0 `render_board_svg` skipped anything needing font metrics, so
    the panel's self-dimensions appeared only when the kicad-cli content phase
    replaced the layer about two seconds later. The user saw a panel with no
    measurements on it for that whole time. These are the fences that say the
    first paint now tells the truth.
    """

    def setUp(self):
        self.board = Board.from_string(DIMENSION_BOARD)

    def test_a_dimension_layer_is_no_longer_an_empty_document(self):
        produced = draw(self.board, ["User.16"])
        self.assertIn("User.16", produced)
        self.assertTrue(
            render_seam.has_drawing(produced["User.16"]),
            "REGRESSED: the dimension layer came back with nothing "
            "drawn on it, so the preview's first paint is blank where the "
            "panel's measurements belong.",
        )

    def test_the_dimension_draws_its_line_work_and_its_lettering(self):
        # The line work alone is a handful of segments; the "40 mm" label is
        # stroke glyphs on top of it. A count well past the line work is the
        # cheapest way to say the text is there without pinning glyph shapes.
        produced = draw(self.board, ["User.16"])
        drawn = produced["User.16"].count("<path")
        self.assertGreater(
            drawn, 8,
            f"only {drawn} drawn elements on the dimension layer - the "
            f"crossbar, two extension lines and two arrows account for most of "
            f"that, so the measurement TEXT is missing",
        )

    def test_board_text_draws_too(self):
        produced = draw(self.board, ["F.SilkS"])
        self.assertTrue(render_seam.has_drawing(produced["F.SilkS"]))

    def test_a_hidden_field_still_draws_nothing(self):
        # Why the fast path needs no hide step of its own. kicad-cli has no
        # Objects panel, so every Value the user keeps switched off has to be
        # switched off in a copy of the file before the export
        # (`render_seam.values_hidden`). The library honours the stored
        # `visible` flag itself, so its text drawing does NOT put hidden
        # Values back into the first paint.
        shown = Board.from_string(FIELD_BOARD)
        hidden = Board.from_string(FIELD_BOARD)
        # The fixture is built by substitution, so it is worth saying out loud
        # that the substitution took: without the footprint this test passes on
        # an empty layer and proves nothing.
        self.assertTrue(hidden.footprints[0].properties)
        for prop in hidden.footprints[0].properties:
            prop.visible = False

        self.assertTrue(render_seam.has_drawing(draw(shown, ["F.SilkS"])["F.SilkS"]))
        self.assertFalse(
            render_seam.has_drawing(draw(hidden, ["F.SilkS"])["F.SilkS"]),
            "a footprint field with `(hide yes)` was drawn anyway, so the fast "
            "paint now shows every Value the user keeps switched off",
        )


# ---------------------------------------------------------------------------
# the copper opt-in - offered by 0.16.0, declined here, measured
# ---------------------------------------------------------------------------


class CopperOptInDeclinedTest(unittest.TestCase):
    """The fast path asks for drawings and nothing else, and says why.

    0.16.0 added `content=` so a caller can have copper tracks, vias and zones
    from the fast renderer. The preview does not use it. Before 0.18.0 the
    library drew no PAD, so a copper layer came back with every track and not
    one pad. The copper also costs time and size: +86 ms and +1.08 MB on the
    corpus panel.

    From 0.18.0 the library draws pads, so only the cost remains as a reason.
    Adding copper to the fast paint would need that cost measured again. The
    seam asks for the drawings stream alone, which is what the first two tests
    fence, and the third checks that the library draws pads.
    """

    def test_the_seam_asks_for_the_drawings_stream_alone(self):
        from eedesigncore import render as render_module

        content = render_seam.fast_content(render_module)
        self.assertEqual(
            {member.value for member in content}, {"drawings"},
            "the fast paint is asking for more than the drawings stream",
        )

    def test_an_older_renderer_is_asked_for_no_content_at_all(self):
        class Ancient:
            """A library checkout from before `BoardContent` existed."""

        self.assertIsNone(render_seam.fast_content(Ancient()))

    def test_the_library_draws_pads_now_and_the_reason_needs_re_stating(self):
        # The library draws pads from 0.18.0. This fence keeps that fact
        # checked, so the seam's docstring about the copper opt-in cannot
        # drift from what the library does.
        board = Board.from_string(PAD_ONLY_BOARD)
        produced = render_board_svg(
            board, ["F.Cu"], ViewBox(*WINDOW),
            style=LayerStyle(colors=LAYER_COLORS), content=ALL_CONTENT,
        )
        self.assertTrue(
            render_seam.has_drawing(produced["F.Cu"]),
            "eedesigncore has stopped drawing pads again, which would put the "
            "seam's original reason back",
        )

    def test_a_track_is_not_drawn_through_the_seam(self):
        # The same statement from the caller's side: whatever the library CAN
        # draw, what the preview's first paint ASKS for holds no copper. The
        # track is drawn when the same board is rendered with `ALL_CONTENT`,
        # which is what makes this a statement about the seam and not about an
        # empty board.
        board = Board.from_string(TRACK_BOARD)
        self.assertTrue(board.tracks)
        everything = render_board_svg(
            board, ["F.Cu"], ViewBox(*WINDOW),
            style=LayerStyle(colors=LAYER_COLORS), content=ALL_CONTENT,
        )
        self.assertTrue(render_seam.has_drawing(everything["F.Cu"]))
        self.assertFalse(
            render_seam.has_drawing(draw(board, ["F.Cu"]).get("F.Cu", "")),
            "the fast paint drew a copper track, so it is now painting a "
            "copper layer that has no pads in it",
        )


# ---------------------------------------------------------------------------
# still open at 0.16.0 - the fences that keep local code honest
# ---------------------------------------------------------------------------


class StillOpenUpstreamTest(unittest.TestCase):
    """Four filed items 0.16.0 did not answer. Each keeps something here alive.

    Every assertion below is the WRONG answer asserted on purpose, exactly as
    `test_placement_suite.UpstreamDivergenceTest` does for pad angle
    normalisation. When one of these fails, the library has changed and the
    local code it justifies can go.
    """

    def test_there_is_still_no_draw_rotation_primitive(self):
        missing = [
            name for name in ("draw_angle", "resolve_keep_upright")
            if hasattr(eedesigncore, name)
        ]
        self.assertEqual(
            missing, [],
            f"The upstream draw-rotation primitive has landed: eedesigncore now exports {missing}. "
            f"`eedesignpanels.stages.upright` adopts the primitive and keeps its "
            f"own fold only as the fence that checks the two agree.",
        )

    def test_the_local_fold_is_the_only_port_of_it(self):
        # The port itself, checked against KiCad's window: the fold lands in
        # (-90, 90] and is idempotent.
        for angle in (0.0, 89.0, 90.0, 91.0, 180.0, 270.0, 359.0, -270.0):
            folded = upright.draw_angle(angle)
            self.assertGreater(folded, -90.0)
            self.assertLessEqual(folded, 90.0)
            self.assertEqual(upright.draw_angle(folded), folded)

    def test_a_hatch_phase_is_still_moved_with_the_zone(self):
        from eedesigncore import (
            Zone, ZoneLayerProperty, ZoneOutlinePolygon, translate_items,
        )

        zone = Zone(
            layer="F.Cu",
            polygons=[ZoneOutlinePolygon(pts=[(0.0, 0.0), (1.0, 0.0)])],
            layer_properties=[
                ZoneLayerProperty(layer="F.Cu", hatch_position=(3.0, 4.0)),
            ],
        )
        translate_items([zone], (10.0, 0.0))
        self.assertEqual(
            zone.layer_properties[0].hatch_position, (13.0, 4.0),
            "The upstream defect has been FIXED: `hatch_position` is a phase used mod grid "
            "pitch and KiCad's ZONE::Move leaves it alone. Nothing here works "
            "around it, so this fence is all there is to delete.",
        )

    def test_a_point_marker_still_takes_one_style_for_both_shapes(self):
        # The cross is `LAYER_POINTS` in the editor and the ring is the item's
        # own layer colour; one `style_for` per item cannot say that. The
        # preview does not draw points at all, so nothing here is worked
        # around - this only checks that the library still lacks the option.
        from eedesigncore.render.style import StylePolicy

        self.assertFalse(
            hasattr(StylePolicy, "point_ring_stroke"),
            "The upstream option has landed: a policy can now colour a point's ring apart "
            "from its cross.",
        )


if __name__ == "__main__":
    unittest.main()
