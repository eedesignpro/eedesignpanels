"""The live preview: what it refuses, what it writes, and what it never writes.

Four things are pinned here, and they are the four that would be expensive to
find out later.

**Preview writes nothing until it is told to.** Looking at a board costs the
design nothing and so does editing it: accept, decline and a parameter change
land in the session's held board and config, and only `save` puts them on disk.
Both halves are asserted on FILE BYTES - hashed before and after - rather than
on an intention in a docstring.

**An accept is exactly one line.** The decision lives in the board, so the
accept action adds a `User.38` line at the outline's bounding-box reach and
nothing else; a following prep has to validate it as a
score, which is what proves the edit and the reader agree. Decline removes it.
Both are checked through the file the save wrote.

**The server is a server.** It is started in-process on 127.0.0.1, driven with
`urllib`, and stopped again. No browser is automated here; the real page is
checked by hand in a real browser, which catches rendering defects this suite
cannot.

**A KiCad lock file changes nothing.** What is fenced is a lock sitting beside
the board, the server starting anyway, and a save going through.

**The seam picks the right backend.** The upstream renderer does not exist yet,
so the selection logic is tested against a fake module that has it and one that
does not - which is the only way to know today that it will be preferred
tomorrow.
"""

import hashlib
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eedesigncore import Board, item_layers

from eedesignpanels import prep
from eedesignpanels.config import CONFIG_REVISION, WRITTEN_BY, load_config
from eedesignpanels.preview import (
    actions,
    journal as journal_module,
    labels as labels_module,
    naming,
    page_served,
    render_seam,
    server as server_module,
)
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.stages import board_scores, route_toolpath
from eedesignpanels.stages.panel_scores import keepout_span
from eedesignpanels.stages.route_toolpath import (
    SCORE_LAYER,
    TOOLPATH_LAYER,
    outline_bbox,
    score_line_for,
)
from eedesignpanels.toolpath import blade_extent
from test_vscore import CUTTER, candidates_of, items_on, scoreable_board


# ---------------------------------------------------------------------------
# a project on disk, and a renderer that needs no kicad-cli
# ---------------------------------------------------------------------------


def write_project(folder: Path, board: Board = None) -> Path:
    """A board and its `panel/panel.json`, laid out the way the core expects."""
    board = board if board is not None else scoreable_board()
    board_path = folder / "board.kicad_pcb"
    board.to_file(board_path)
    panel = folder / "panel"
    panel.mkdir(exist_ok=True)
    config_path = panel / "panel.json"
    config_path.write_text(json.dumps({
        # A file this tool wrote: the revision and the version that wrote it
        # lead it.
        "revision": CONFIG_REVISION,
        "written_by": WRITTEN_BY,
        "source_board": "../board.kicad_pcb",
        "grid": {"columns": 1, "rows": 1, "gap_mm": 0.0},
        "prep": {
            "cutter_diameter": CUTTER,
            "vscore": {"detect": "auto", "route_past_score_mm": 1.0},
        },
    }, indent=2) + "\n", encoding="utf-8")
    return config_path


def stub_render(board, layers, viewbox_mm, colors, theme=None, workdir=None,
                content=False, origin=None):
    """A renderer with no kicad-cli behind it, in the seam's own contract.

    One diagonal per layer, in the window the caller asked for. It is enough for
    everything the page does with an SVG - inline it, stack it, swap its
    contents - and it is what lets the server be exercised in a container.

    *content* is the caller saying the layer set holds pads, tracks and zones,
    which decides the real seam's backend and means nothing to a stub. The Holes
    row arrives among *layers* like any other entry, so a page driven by this
    renderer carries one exactly where a rendered board would.
    """
    x, y, width, height = viewbox_mm
    return {
        layer.canonical_name: (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.4f}mm" height="{height:.4f}mm" '
            f'viewBox="{x:.6f} {y:.6f} {width:.6f} {height:.6f}">'
            f'<title>generated {layer.canonical_name}</title>'
            f'<path d="M{x:.4f} {y:.4f} L{x + width:.4f} {y + height:.4f}" '
            'style="stroke:#000000"/></svg>'
        )
        for layer in layers
    }


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def score_items(board_path: Path) -> list:
    return items_on(Board.from_file(board_path), SCORE_LAYER)


def left_blade(board: Board):
    """The blade line of the one candidate on this fixture's left edge."""
    bbox = outline_bbox(board)
    for candidate in candidates_of(board):
        if abs(candidate.start[0]) < 1e-9 and abs(candidate.end[0]) < 1e-9:
            start, end = blade_extent(candidate, bbox)
            return actions.Blade(start=start, end=end)
    raise AssertionError("the fixture has no left-edge candidate")


# ---------------------------------------------------------------------------
# the render seam
# ---------------------------------------------------------------------------


class SeamSelectionTest(unittest.TestCase):
    """Which backend a given library implies - both ways, with a fake module.

    The upstream renderer is not written yet, so this is the only place that can
    say today what will happen when it lands. It is guarded by ATTRIBUTE and
    never by a version, because the library is consumed from a working checkout
    that has no release number to test.
    """

    def test_no_library_at_all_means_kicad_cli(self):
        self.assertEqual(render_seam.select_backend(None), render_seam.CLI_BACKEND)

    def test_a_library_without_the_renderer_means_kicad_cli(self):
        module = SimpleNamespace(Board=object, item_layers=lambda x: x)
        self.assertEqual(
            render_seam.select_backend(module), render_seam.CLI_BACKEND
        )

    def test_a_library_with_the_renderer_is_preferred(self):
        for name in render_seam.LIBRARY_RENDERER_NAMES:
            with self.subTest(name=name):
                module = SimpleNamespace(**{name: lambda *a, **k: {}})
                self.assertEqual(
                    render_seam.select_backend(module),
                    render_seam.LIBRARY_BACKEND,
                )

    def test_a_non_callable_attribute_is_not_a_renderer(self):
        module = SimpleNamespace(render_svg="not a function")
        self.assertEqual(
            render_seam.select_backend(module), render_seam.CLI_BACKEND
        )
        self.assertEqual(render_seam.find_library_renderer(module), (None, None))

    def test_the_first_spelling_present_wins(self):
        first, second = render_seam.LIBRARY_RENDERER_NAMES[:2]
        module = SimpleNamespace(**{
            first: lambda *a, **k: {"a": 1},
            second: lambda *a, **k: {"b": 2},
        })
        name, _ = render_seam.find_library_renderer(module)
        self.assertEqual(name, first)


class SeamContractTest(unittest.TestCase):
    """What each backend has to hand back, whichever one answered."""

    def layers(self):
        return [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")]

    def test_the_library_result_is_keyed_by_canonical_name(self):
        def renderer(board, layers, viewbox, colors):
            return {name: f"<svg>{name}</svg>" for name in layers}

        out = render_seam.render_with_library(
            None, self.layers(), (0.0, 0.0, 1.0, 1.0), {}, renderer
        )
        self.assertEqual(out, {"Edge.Cuts": "<svg>Edge.Cuts</svg>"})

    def test_the_caller_supplies_the_window_and_the_colours(self):
        seen = {}

        def renderer(board, layers, viewbox, colors):
            seen.update(viewbox=viewbox, colors=colors, layers=layers)
            return {}

        render_seam.render_with_library(
            None, self.layers(), (1.0, 2.0, 3.0, 4.0),
            {"Edge.Cuts": "#ff0000"}, renderer,
        )
        self.assertEqual(seen["viewbox"], (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(seen["colors"], {"Edge.Cuts": "#ff0000"})
        self.assertEqual(seen["layers"], ["Edge.Cuts"])

    def test_a_renderer_that_returns_the_wrong_shape_is_refused(self):
        with self.assertRaises(TypeError):
            render_seam.render_with_library(
                None, self.layers(), (0.0, 0.0, 1.0, 1.0), {},
                lambda *a, **k: ["<svg/>"],
            )

    def test_the_cli_backend_will_not_render_without_a_scratch_folder(self):
        # It writes the board to disk, and it must never do that beside the
        # design: kicad-cli drops a .kicad_prl sidecar wherever it renders.
        with self.assertRaises(ValueError):
            render_seam.render_with_cli(
                None, self.layers(), (0.0, 0.0, 1.0, 1.0), {}, workdir=None
            )


class PointPresentationTest(unittest.TestCase):
    """A `point` item is not displayed at all.

    A point never appears in a KiCad board export - `kicad-cli` writes nothing
    for one, because KiCad's plot path holds no branch for the item - so it
    never appears in a preview either. It is a GUI-only marker, and a preview
    that drew it would show the user something no fabrication output will
    carry.

    The library's style policy expresses this directly: "``style_for`` returns
    ``None`` to drop an item, so a caller filters and styles through the same
    function" (`eedesigncore/render/style.py:1-8`), and it is called once per
    RENDER ITEM. So the per-item-kind drop is expressible and there is no
    upstream gap here; the library even ships the identical decision as
    `CliParityStyle` for its own kicad-cli oracle (`style.py:96-113`).
    """

    def item(self, obj, layer="Edge.Cuts"):
        return SimpleNamespace(item=obj, layer=layer, uuid="u")

    def base(self, **colors):
        from eedesigncore.render import LayerStyle

        return LayerStyle(colors=colors or {"Edge.Cuts": "rgb(1, 2, 3)"})

    def test_the_policy_can_express_a_per_item_kind_drop(self):
        # The claim hiding points rests on, asserted against the library:
        # None is the documented answer for "do not draw this one", and the
        # library's own preset uses it for exactly this item.
        from eedesigncore.render import LayerStyle, StylePolicy
        from eedesigncore.render.style import CliParityStyle

        self.assertIsInstance(
            render_seam.PointsHidden(base=LayerStyle(colors={})), StylePolicy
        )
        from eedesigncore import PcbPoint

        parity = CliParityStyle(colors={"Edge.Cuts": "rgb(1, 2, 3)"})
        self.assertIsNone(
            parity.style_for(self.item(PcbPoint(position=(0.0, 0.0), size=1.0)))
        )

    def test_a_point_is_dropped(self):
        from eedesigncore import PcbPoint

        style = render_seam.PointsHidden(base=self.base())
        self.assertIsNone(
            style.style_for(self.item(PcbPoint(position=(0.0, 0.0), size=1.0)))
        )

    def test_everything_that_is_not_a_point_is_left_exactly_alone(self):
        from eedesigncore import PcbLine

        base = self.base()
        style = render_seam.PointsHidden(base=base)
        line = self.item(PcbLine(start=(0.0, 0.0), end=(1.0, 0.0)))
        self.assertEqual(style.style_for(line), base.style_for(line))
        self.assertEqual(style.style_for(line).stroke, "rgb(1, 2, 3)")

    def test_a_layer_the_caller_did_not_name_still_drops(self):
        # Colour absence is the library's filter, and wrapping must not defeat
        # it: an item on a layer this render does not show is not drawn.
        from eedesigncore import PcbLine

        style = render_seam.PointsHidden(base=self.base())
        line = self.item(PcbLine(start=(0.0, 0.0), end=(1.0, 0.0)), layer="B.Cu")
        self.assertIsNone(style.style_for(line))

    def test_the_wrapper_carries_the_base_policy_settings_through(self):
        from eedesigncore.render import LayerStyle

        base = LayerStyle(colors={}, emit_ids=False, point_stroke_width_mm=0.3)
        style = render_seam.PointsHidden(base=base)
        self.assertFalse(style.emit_ids)
        self.assertEqual(style.point_stroke_width_mm, 0.3)

    def test_the_library_call_carries_the_drop_policy(self):
        seen = {}

        def renderer(board, layers, view_box, style=None, content=None, text_context=None):
            seen["style"] = style
            seen["content"] = content
            return {name: "<svg/>" for name in layers}

        render_seam.render_with_library(
            None, [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")],
            (0.0, 0.0, 1.0, 1.0), {"Edge.Cuts": "rgb(1, 2, 3)"}, renderer,
            renderer_name="render_board_svg",
        )
        self.assertIsInstance(seen["style"], render_seam.PointsHidden)
        # ...and the DRAWINGS stream, said out loud rather than left to the
        # library's default. `render_seam.fast_content` holds the choice and
        # the module docstring holds the two measured reasons for it.
        self.assertEqual(
            {member.value for member in seen["content"]}, {"drawings"},
        )


@unittest.skipUnless(
    render_seam.BACKEND == render_seam.LIBRARY_BACKEND,
    "the library renderer is the one that could draw a point at all",
)
class PointRenderTest(unittest.TestCase):
    """No point reaches the page - measured on the markup, not on the policy."""

    SIZE = 1.0
    AT = (4.0, 3.0)

    def rendered(self, style=None):
        from eedesigncore import PcbPoint, Stroke

        board = scoreable_board()
        for item in board.drawings:
            item.stroke = Stroke(width=0.05)
        # A board-level point lives in `Board.points`, not in `drawings` - the
        # library keeps them apart, and the writer only knows the first home.
        board.points.append(
            PcbPoint(position=self.AT, size=self.SIZE, layer="Edge.Cuts")
        )
        module = render_seam._import_library()
        name, renderer = render_seam.find_library_renderer(module)
        return render_seam.render_with_library(
            board, [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")],
            (0.0, 0.0, 10.0, 10.0), {"Edge.Cuts": "rgb(1, 2, 3)"}, renderer,
            renderer_name=name,
        )["Edge.Cuts"]

    def unhidden(self):
        """The same board through the bare policy - the control for the drop."""
        from eedesigncore import PcbPoint, Stroke, render_board_svg
        from eedesigncore.render import LayerStyle, ViewBox

        board = scoreable_board()
        for item in board.drawings:
            item.stroke = Stroke(width=0.05)
        board.points.append(
            PcbPoint(position=self.AT, size=self.SIZE, layer="Edge.Cuts")
        )
        return render_board_svg(
            board, ["Edge.Cuts"], ViewBox(0.0, 0.0, 10.0, 10.0),
            style=LayerStyle(colors={"Edge.Cuts": "rgb(1, 2, 3)"}),
        )["Edge.Cuts"]

    def test_the_renderer_would_draw_it_without_the_policy(self):
        # Otherwise the assertion below proves nothing: it has to be possible
        # for a point to reach the page before hiding it means anything.
        markup = self.unhidden()
        self.assertIn("<circle", markup)
        self.assertIn(f'r="{self.SIZE / 4.0:.4f}"', markup)

    def test_the_marker_geometry_is_nowhere_in_the_render(self):
        markup = self.rendered()
        reach = self.SIZE / 2.0
        # The cross the editor draws, and the ring: neither is emitted.
        self.assertNotIn(
            f'M{self.AT[0] - reach:.4f},{self.AT[1] - reach:.4f}', markup
        )
        self.assertNotIn(f'r="{self.SIZE / 4.0:.4f}"', markup)
        self.assertNotIn("<circle", markup)
        # ...and exactly the marker's two elements are what went missing.
        self.assertEqual(
            markup.count("<path"), self.unhidden().count("<path") - 1
        )

    def test_the_edge_it_shares_a_layer_with_still_draws(self):
        # The drop is per ITEM KIND and not per layer: dropping the layer would
        # take the board outline with it.
        markup = self.rendered()
        self.assertIn("rgb(1, 2, 3)", markup)
        self.assertIn("<path", markup)

    def test_a_session_render_carries_no_point(self):
        # End to end, through the seam the session actually calls - with
        # kicad-cli answered as absent, because the point rule is the LIBRARY
        # path's (KiCad's own plot holds no branch for a point at all) and the
        # session now asks for a content-complete render, which kicad-cli would
        # otherwise take. The fixture is a synthetic board kicad-cli declines to
        # load, so this is also the only way the seam can be driven end to end
        # over it.
        from eedesigncore import PcbPoint, Stroke

        board = scoreable_board()
        for item in board.drawings:
            item.stroke = Stroke(width=0.05)
        # A board-level point lives in `Board.points`, not in `drawings` - the
        # library keeps them apart, and the writer only knows the first home.
        board.points.append(
            PcbPoint(position=self.AT, size=self.SIZE, layer="Edge.Cuts")
        )
        with tempfile.TemporaryDirectory() as folder:
            config_path = write_project(Path(folder), board)
            session = PreviewSession(config_path, Path(folder) / "work")
            with mock.patch.object(render_seam, "cli_available",
                                   return_value=False):
                session.refresh()
            self.assertEqual(session.error, "")
            for markup in session.fragment()["svg"].values():
                self.assertNotIn("<circle", markup)
                self.assertNotIn(f'r="{self.SIZE / 4.0:.4f}"', markup)
            # This session has a real renderer, so it has a tier worker behind
            # it; closed here so nothing is drawing into the folder this block
            # is about to delete.
            session.close()


class ReframeTest(unittest.TestCase):
    """kicad-cli's cropped frame, carried back into board millimetres.

    Two edits, both on the document wrapper: the root viewBox becomes the window
    the caller asked for, and one rigid translate puts the content where the
    board says it is. Nothing drawn is touched, which is the property that lets
    an overlay in board coordinates sit on top of it.
    """

    EXPORT = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="5mm" '
        'viewBox="0.000000 0.000000 10.000000 5.000000">'
        '<path d="M0 0 L10 5"/></svg>'
    )

    def test_the_window_and_the_shift_are_the_callers(self):
        out = render_seam._reframe(self.EXPORT, (100.0, 50.0, 10.0, 5.0),
                                   (100.0, 50.0))
        self.assertIn('viewBox="100.000000 50.000000 10.000000 5.000000"', out)
        self.assertIn('<g transform="translate(100.000000 50.000000)">', out)
        self.assertIn('<path d="M0 0 L10 5"/>', out)
        self.assertTrue(out.rstrip().endswith("</g></svg>"))

    def test_the_original_viewbox_does_not_survive(self):
        out = render_seam._reframe(self.EXPORT, (7.0, 8.0, 1.0, 2.0), (0.0, 0.0))
        self.assertNotIn('viewBox="0.000000 0.000000 10.000000 5.000000"', out)

    def test_something_that_is_not_an_svg_is_refused(self):
        with self.assertRaises(ValueError):
            render_seam._reframe("<html></html>", (0, 0, 1, 1), (0, 0))


# ---------------------------------------------------------------------------
# what a click writes
# ---------------------------------------------------------------------------


class BladeTest(unittest.TestCase):
    """A blade line arrives as geometry, and anything else is refused."""

    def test_a_pair_of_points_is_a_blade(self):
        blade = actions.blade_from({"start": [0.0, 0.0], "end": [0.0, 10.0]})
        self.assertEqual(blade.start, (0.0, 0.0))
        self.assertAlmostEqual(blade.length_mm, 10.0)

    def test_the_line_carries_points_beyond_its_own_ends(self):
        # Extent-tolerant, exactly as the stage's own matching is: what a score
        # line means is WHICH CUT it is, not how far the designer drew it.
        blade = actions.blade_from({"start": [0.0, 0.0], "end": [0.0, 1.0]})
        self.assertTrue(blade.carries((0.0, 99.0)))
        self.assertFalse(blade.carries((1.0, 5.0)))

    def test_rubbish_is_refused_by_name(self):
        for payload in (
            {},
            {"start": [0, 0]},
            {"start": [0, 0], "end": [0, 0]},
            {"start": "0,0", "end": [0, 1]},
            {"start": [0, 0], "end": [float("nan"), 1]},
            {"start": [True, 0], "end": [0, 1]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(actions.ActionError):
                    actions.blade_from(payload)

    def test_a_blade_matching_no_candidate_is_refused(self):
        board = scoreable_board()
        blade = actions.Blade(start=(-5.0, -5.0), end=(-5.0, 5.0))
        with self.assertRaises(actions.ActionError):
            actions.matching_candidate(blade, candidates_of(board))


class AcceptDeclineTest(unittest.TestCase):
    """Accept adds one line at bbox extent AND its copper keepout, IN MEMORY.

    The file is untouched either way. The keepout is pinned in full in
    `test_board_scores.py`; what is pinned here is that a decision writes both
    and takes both away.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        folder = Path(self._folder.name)
        self.config_path = write_project(folder)
        self.config = load_config(self.config_path)
        self.board_path = self.config.source_board_path
        self.board = Board.from_file(self.board_path)
        self.candidates = candidates_of(self.board)
        self.blade = left_blade(self.board)

    def tearDown(self):
        self._folder.cleanup()

    def test_accept_adds_exactly_one_line_at_the_bounding_box_reach(self):
        before = digest(self.board_path)
        written = actions.accept(self.board, self.blade, self.candidates)
        # THE LINE AND ITS KEEPOUT: a decision writes both, so an accept adds
        # two items and not one.
        self.assertEqual((written.added, written.removed), (2, 0))
        self.assertTrue(written.changed)
        self.assertEqual(len(board_scores.keepouts_on(self.board)), 1)

        lines = actions.board_score_lines(self.board)
        self.assertEqual(len(lines), 1)
        expected = score_line_for(
            actions.matching_candidate(self.blade, self.candidates),
            outline_bbox(self.board),
        )
        for got, want in zip(lines[0].start, expected.start):
            self.assertAlmostEqual(got, want, places=6)
        for got, want in zip(lines[0].end, expected.end):
            self.assertAlmostEqual(got, want, places=6)
        # Nothing reached the design.
        self.assertEqual(digest(self.board_path), before)
        self.assertEqual(score_items(self.board_path), [])

    def test_the_line_accept_added_validates_as_a_score_once_saved(self):
        actions.accept(self.board, self.blade, self.candidates)
        actions.save(self.board, self.board_path, {}, self.config_path)
        result = route_toolpath.run(self.config)
        self.assertEqual(len(result.scores), 1)
        self.assertEqual(result.stale_scores, [])
        self.assertTrue(result.scores_untouched)
        # And nothing else on the design's own layer: prep still never writes it.
        self.assertEqual(len(score_items(self.board_path)), 1)

    def test_accepting_twice_changes_nothing_the_second_time(self):
        actions.accept(self.board, self.blade, self.candidates)
        again = actions.accept(self.board, self.blade, self.candidates)
        self.assertEqual((again.added, again.removed), (0, 0))
        self.assertFalse(again.changed)
        self.assertEqual(len(actions.board_score_lines(self.board)), 1)

    def test_decline_removes_the_line_and_only_that_line(self):
        actions.accept(self.board, self.blade, self.candidates)
        written = actions.decline(self.board, self.blade)
        # The line and its keepout, both gone.
        self.assertEqual((written.added, written.removed), (0, 2))
        self.assertEqual(actions.board_score_lines(self.board), [])
        self.assertEqual(board_scores.keepouts_on(self.board), [])
        # The rest of the design came through: the outline is still four items.
        outline = [
            item for item in self.board.drawings
            if "Edge.Cuts" in item_layers(item)
        ]
        self.assertEqual(len(outline), 4)

    def test_declining_nothing_changes_nothing(self):
        written = actions.decline(self.board, self.blade)
        self.assertEqual((written.added, written.removed), (0, 0))
        self.assertFalse(written.changed)


class SetParamTest(unittest.TestCase):
    """One number into the HELD config, validated before anything is kept."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.config_path = write_project(Path(self._folder.name))
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def set(self, name, value):
        return actions.set_param(self.data, self.config_path, name, value)

    def test_the_cutter_diameter_round_trips(self):
        before = digest(self.config_path)
        changed = self.set("cutter_diameter", 1.25)
        self.assertTrue(changed.changed)
        self.assertAlmostEqual(changed.config.prep.cutter_diameter, 1.25)
        self.assertAlmostEqual(changed.data["prep"]["cutter_diameter"], 1.25)
        # Everything else in the data is left exactly as it was...
        self.assertEqual(changed.data["grid"],
                         {"columns": 1, "rows": 1, "gap_mm": 0.0})
        # ...and the file has not been touched at all.
        self.assertEqual(digest(self.config_path), before)
        self.assertAlmostEqual(
            load_config(self.config_path).prep.cutter_diameter, CUTTER
        )

    def test_the_held_data_is_not_edited_in_place(self):
        # A refused save, or a validation failure two keys later, must not leave
        # the session holding a half-edited config.
        original = json.loads(json.dumps(self.data))
        self.set("cutter_diameter", 1.25)
        self.assertEqual(self.data, original)

    def test_the_overshoot_round_trips_through_its_nested_key(self):
        changed = self.set("route_past_score_mm", 0.75)
        self.assertAlmostEqual(
            changed.data["prep"]["vscore"]["route_past_score_mm"], 0.75
        )
        self.assertAlmostEqual(
            changed.config.prep.vscore.route_past_score_mm, 0.75
        )

    def test_the_minimum_tab_is_settable_and_top_level(self):
        changed = self.set("min_tab_mm", 0.8)
        self.assertAlmostEqual(changed.data["min_tab_mm"], 0.8)
        self.assertAlmostEqual(changed.config.min_tab_mm, 0.8)

    def test_a_number_the_config_already_says_is_not_a_change(self):
        changed = self.set("cutter_diameter", CUTTER)
        self.assertFalse(changed.changed)

    def test_an_unknown_parameter_is_refused_by_name(self):
        with self.assertRaises(actions.ActionError) as caught:
            self.set("sheet_margin", 3.0)
        self.assertIn("cutter_diameter", str(caught.exception))

    def test_a_value_the_schema_refuses_is_refused(self):
        for name, value in (("cutter_diameter", -1.0), ("min_tab_mm", 0.01)):
            with self.subTest(name=name):
                with self.assertRaises(actions.ActionError):
                    self.set(name, value)

    def test_a_value_that_is_not_a_number_is_refused(self):
        for value in ("0.9", True, None, [1]):
            with self.subTest(value=value):
                with self.assertRaises(actions.ActionError):
                    self.set("cutter_diameter", value)


class RailParamTest(unittest.TestCase):
    """The rails: one setting, two spellings, and the edit that reshapes it.

    Which spelling the file carries is the unlock state, so a rail edit may
    have to change the SHAPE of `panel.rails` and not only a number in it.
    Unlocking keeps the other three sides where they were, and re-linking
    takes the widest. A config that still carries the old `panel.sheet` key
    is refused.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.base = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def held(self, panel: dict = None):
        data = json.loads(json.dumps(self.base))
        if panel is not None:
            data["panel"] = panel
        return data, self.config_path

    def set(self, panel: dict, name: str, value):
        data, config_path = self.held(panel)
        return actions.set_param(data, config_path, name, value)

    def test_the_linked_rail_writes_one_number(self):
        changed = self.set({"rails": 5.0}, "rail_mm", 7.0)
        self.assertEqual(changed.data["panel"], {"rails": 7.0})
        self.assertTrue(changed.changed)
        self.assertEqual(changed.config.panel.rail_widths,
                         (7.0, 7.0, 7.0, 7.0))

    def test_setting_one_side_expands_the_linked_number_into_four(self):
        # The other three keep the width the one number meant. Defaulting them
        # to nothing would delete three rails to edit one.
        changed = self.set({"rails": 5.0}, "rail_top_mm", 1.0)
        self.assertEqual(changed.data["panel"], {"rails": {
            "top": 1.0, "bottom": 5.0, "left": 5.0, "right": 5.0
        }})
        self.assertFalse(changed.config.panel.rails_linked)

    def test_relinking_collapses_the_four_back_into_one(self):
        changed = self.set(
            {"rails": {"top": 8.0, "bottom": 6.0, "left": 3.0, "right": 0.0}},
            "rail_mm", 8.0,
        )
        self.assertEqual(changed.data["panel"], {"rails": 8.0})

    def test_unlocking_at_the_same_width_is_still_a_change(self):
        # The SHAPE is what says the sides are unlocked, so a save has to carry
        # it even though not one width moved.
        changed = self.set({"rails": 5.0}, "rail_top_mm", 5.0)
        self.assertTrue(changed.changed)

    def test_a_number_the_config_already_says_the_same_way_is_not_a_change(self):
        self.assertFalse(self.set({"rails": 5.0}, "rail_mm", 5.0).changed)
        self.assertFalse(self.set(
            {"rails": {"top": 5.0, "bottom": 5.0, "left": 5.0, "right": 5.0}},
            "rail_top_mm", 5.0,
        ).changed)

    def test_a_retired_key_is_refused_rather_than_migrated(self):
        # `panel.sheet.margin_mm`, the old name for `panel.rails`, is not
        # converted. It is refused like any unknown key, an edit included.
        with self.assertRaises(actions.ActionError) as raised:
            self.set({"sheet": {"margin_mm": 5.0}}, "rail_mm", 6.0)
        self.assertIn("panel.sheet", str(raised.exception))

    def test_a_negative_rail_is_refused_and_keeps_the_held_data(self):
        data, config_path = self.held({"rails": 5.0})
        original = json.loads(json.dumps(data))
        with self.assertRaises(actions.ActionError):
            actions.set_param(data, config_path, "rail_bottom_mm", -1.0)
        self.assertEqual(data, original)

    def test_a_rail_cannot_be_typed_into_a_config_with_no_panel_section(self):
        data, config_path = self.held()
        for name in ("rail_mm", "rail_top_mm"):
            with self.subTest(name=name):
                with self.assertRaises(actions.ActionError) as caught:
                    actions.set_param(data, config_path, name, 5.0)
                self.assertIn("'panel' section", str(caught.exception))

    def test_the_four_widths_read_back_the_same_way_however_they_were_written(self):
        for panel, expected in (
            ({"rails": 5.0}, {"top": 5.0, "bottom": 5.0,
                              "left": 5.0, "right": 5.0}),
            ({"rails": {"top": 1.0, "bottom": 2.0, "left": 3.0, "right": 4.0}},
             {"top": 1.0, "bottom": 2.0, "left": 3.0, "right": 4.0}),
        ):
            with self.subTest(panel=panel):
                data, _ = self.held(panel)
                self.assertEqual(actions.rails_now(data), expected)

    def test_a_retired_key_reads_no_rails_at_all(self):
        # This reader does not convert `panel.sheet.margin_mm` either; the
        # config is refused before `rails_now` would be asked to read it.
        data, _ = self.held({"sheet": {"margin_mm": 4.0}})
        self.assertIsNone(actions.rails_now(data))

    def test_a_config_with_no_panel_has_no_rails_to_read(self):
        data, _ = self.held()
        self.assertIsNone(actions.rails_now(data))


# ---------------------------------------------------------------------------
# preview writes nothing
# ---------------------------------------------------------------------------


class PreviewWritesNothingTest(unittest.TestCase):
    """The invariant the split exists for, asserted on file bytes.

    Looking at a board has to cost the design nothing, or the preview is not a
    preview. Hashes before and after a full cycle - compute, stage the copy,
    render, build the page - and the two have to be the same string.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.board_path = self.config.source_board_path

    def tearDown(self):
        self._folder.cleanup()

    def fingerprint(self):
        return (digest(self.board_path), digest(self.config_path))

    def test_compute_leaves_the_project_byte_identical(self):
        before = self.fingerprint()
        computed = prep.compute(self.config)
        self.assertGreater(computed.result.written, 0)
        self.assertEqual(self.fingerprint(), before)

    def test_the_staged_copy_carries_the_toolpath_and_the_source_does_not(self):
        before = self.fingerprint()
        computed = prep.compute(self.config)
        copy_path = prep.stage_board(computed, self.folder / "scratch")
        self.assertEqual(copy_path.name, self.board_path.name)
        self.assertTrue(items_on(Board.from_file(copy_path), TOOLPATH_LAYER))
        self.assertEqual(items_on(Board.from_file(self.board_path),
                                  TOOLPATH_LAYER), [])
        self.assertEqual(self.fingerprint(), before)

    def test_staging_over_the_design_is_refused(self):
        computed = prep.compute(self.config)
        with self.assertRaises(ValueError):
            prep.stage_board(computed, self.board_path.parent)

    def test_a_whole_session_refresh_leaves_the_project_byte_identical(self):
        before = self.fingerprint()
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        self.assertEqual(session.error, "")
        self.assertEqual(session.generation, 1)
        self.assertIn("<svg", session.page())
        self.assertEqual(self.fingerprint(), before)

    def test_two_refreshes_still_leave_it_alone(self):
        before = self.fingerprint()
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        session.refresh()
        self.assertEqual(session.generation, 2)
        self.assertEqual(self.fingerprint(), before)

    def test_a_board_that_cannot_be_prepared_is_a_state_and_not_a_crash(self):
        # An outline that does not close: the one thing the stage refuses.
        board = scoreable_board()
        board.drawings = board.drawings[:2]
        write_project(self.folder, board)
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        self.assertTrue(session.error)
        self.assertIn("could not be prepared", session.page())


# ---------------------------------------------------------------------------
# what the page carries
# ---------------------------------------------------------------------------


class ServedPageTest(unittest.TestCase):
    """The served host adds handlers; it does not invent a second review page."""

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

    def test_the_shared_components_are_the_ones_on_the_page(self):
        self.assertIn("layerPanel('lp-viewer'", self.markup)
        self.assertIn("class=layerpanel", self.markup)
        self.assertIn("class=viewport", self.markup)

    def test_the_controls_column_is_there_with_its_endpoints(self):
        for token in ("pb-candidates", "pb-cutter", "pb-past", "pb-scorew",
                      "pb-mintab",
                      "/accept", "/decline", "/set_param", "/save", "/reload",
                      "/shutdown", "/events"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_word_commit_is_nowhere_on_the_page(self):
        # The page has one write action, and it is called "Save to Board".
        self.assertNotIn("commit", self.markup.lower())
        self.assertIn("Save to Board", self.markup)

    def test_the_page_references_nothing_outside_itself(self):
        from eedesignpanels.preview.icons import BOARD_ICON
        self.assertIn(BOARD_ICON, self.markup)
        markup = self.markup.replace(BOARD_ICON, "")
        # The page is self-contained: the only thing it fetches is its own
        # server.
        for forbidden in ("<link", "<script src", "@import", "url(http"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, markup)

    def test_a_candidate_carries_the_blade_line_accept_would_write(self):
        board = Board.from_file(self.session.config.source_board_path)
        bbox = outline_bbox(board)
        entries = page_served.proposals_json(self.session.view)
        self.assertTrue(entries)
        lines = [
            score_line_for(candidate, bbox) for candidate in candidates_of(board)
        ]
        for entry in entries:
            line = next(
                one for one in lines
                if abs(one.start[0] - entry["blade"]["start"][0]) < 1e-6
                and abs(one.start[1] - entry["blade"]["start"][1]) < 1e-6
            )
            for got, want in zip(entry["blade"]["end"], line.end):
                self.assertAlmostEqual(got, want, places=6)

    def test_no_hash_id_travels_to_the_page(self):
        # Hash ids stay off the page. The state file keeps them; the served
        # bytes do not.
        entries = page_served.proposals_json(self.session.view)
        self.assertTrue(entries)
        for entry in entries:
            self.assertNotIn("id", entry)
            self.assertNotIn("short_id", entry)
        for proposal in self.session.view.result.proposals:
            self.assertNotIn(proposal.short_id, self.markup)

    def test_a_state_file_may_keep_the_hash_beside_the_number(self):
        entries = page_served.proposals_json(self.session.view, include_ids=True)
        for entry in entries:
            self.assertTrue(entry["id"])
            self.assertTrue(entry["displayId"].startswith("#"))

    def test_every_changing_control_is_marked_as_one(self):
        # `data-act` is what the busy state disables. A control that changes
        # something and is not marked is a control that stays live during a
        # recompute, which is the defect this fences.
        for token in ("id=pb-cutter data-act=param",
                      "id=pb-past data-act=param",
                      "id=pb-scorew data-act=param",
                      "id=pb-mintab data-act=param",
                      "id=pb-apply", "data-act=apply",
                      "id=pb-reload data-act=reload",
                      "id=pb-save data-act=save"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)
        for token in ("accept.dataset.act = 'accept'",
                      "decline.dataset.act = 'decline'",
                      "accept.dataset.off = p.accepted ? '1' : ''",
                      "decline.dataset.off = p.accepted ? '' : '1'"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_stopping_the_server_is_not_a_control_the_busy_state_disables(self):
        # A stuck action must not take the only way out with it.
        button = re.search(r"<button[^>]*id=pb-stop[^>]*>", self.markup)
        self.assertIsNotNone(button)
        self.assertNotIn("data-act", button.group(0))

    def test_the_page_blocks_its_own_controls_for_the_whole_action(self):
        for token in ("function applyBusy()",
                      "el.disabled = busy || el.dataset.off === '1'",
                      "function setBusy(state, what)",
                      "controlsEl.classList.toggle('busy', busy)",
                      "controlsEl.setAttribute('aria-busy'",
                      "setBusy(true, what);",
                      "setBusy(false);",
                      ".controls.busy .body{opacity:.5;pointer-events:none"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_busy_state_is_a_popup_over_the_display(self):
        # Disabled controls say nothing about the picture, so the picture is
        # covered and dimmed while it is stale.
        for token in ("id=pb-veil", "id=pb-veil-what",
                      "veilEl.classList.toggle('on', busy)",
                      "veilEl.setAttribute('aria-hidden'",
                      ".busyveil{position:absolute;inset:0",
                      ".busyveil.on{display:flex}",
                      ".views{position:relative}"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)
        # ...and it is inside the views container, so it covers the viewports
        # and leaves the header and the layer panel reachable.
        views = self.markup.index('<div class="views"')
        veil = self.markup.index("id=pb-veil")
        self.assertLess(views, veil)
        self.assertLess(veil, self.markup.index("class=footer"))

    def test_the_save_button_carries_the_pending_count(self):
        # Each page names its own label - "Save to Board" here, "Save Panel
        # Files" on the panel page - and the counting is one function either
        # way.
        for token in ("function drawPending(count, changes)",
                      "const label = PB.saveLabel || 'Save to Board';",
                      "saveEl.textContent = PB.pending ? label + "
                      "' (' + many + ')' : label;",
                      "saveEl.classList.toggle('dirty', !!PB.pending)",
                      "drawPending(fragment[PB.pendingKey || 'pending'], "
                      "fragment.pending_changes)"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)
        # ...and what THIS page's own button says, on the bytes served.
        self.assertIn('id=pb-save data-act=save>Save to Board</button>',
                      self.markup)

    def test_quitting_with_unsaved_work_asks_three_ways(self):
        for token in ("function askUnsaved(kind)",
                      "if (PB.pending > 0) { askUnsaved('quit'); return; }",
                      "id=pb-ask-save", "id=pb-ask-discard", "id=pb-ask-cancel",
                      "save and quit", "discard and quit", "cancel",
                      "There are unsaved changes"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_reload_asks_the_same_question_in_its_own_words(self):
        # Reload re-reads the files and drops held edits, so it stands
        # behind the unsaved-work guard exactly as quitting does - and the verb
        # in the three answers follows whichever control asked.
        for token in ("if (PB.pending > 0) { askUnsaved('reload'); return; }",
                      "async function reloadFromDisk()",
                      "post('/reload', {}",
                      "verb: 'reload'",
                      "'save and ' + asking.verb",
                      "'discard and ' + asking.verb"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_closing_the_tab_with_unsaved_work_warns(self):
        for token in ("addEventListener('beforeunload', unloadGuard);",
                      "function unloadGuard(event) {",
                      "if (PB.pending > 0) {",
                      "event.preventDefault();",
                      "event.returnValue = '';"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_a_refresh_the_page_asked_for_is_not_a_tab_close(self):
        # A build refreshes in place and does not trip the guard. The one
        # refresh that still has to reload - a changed layer
        # set - goes through `selfReload`, which says so first.
        self.assertIn("if (stopped || reloading) return;", self.markup)
        self.assertIn("function selfReload(why) {", self.markup)
        self.assertIn("reloading = true;", self.markup)

    def test_the_page_shows_no_bare_layer_name(self):
        # No bare layer name, checked on the bytes that are served.
        self.assertEqual(naming.violations(self.markup), [])
        # ...and the board's own display name is what is shown instead.
        self.assertIn("User.38-VScore", self.markup)

    def test_a_click_that_lands_during_an_action_does_not_post(self):
        # Belt to the disabled attributes' braces: a keyboard repeat or a
        # second tab cannot start a second action from this page either.
        self.assertIn("if (busy) {", self.markup)
        self.assertIn("one thing at a time", self.markup)

    def test_a_change_announced_mid_action_is_painted_once_at_the_end(self):
        # Without this, every queued click repaints and the page jumps. The
        # message carries the layer state as well, so the generation is read
        # off it rather than being it.
        for token in ("function generationSeen(data)",
                      "if (busy) { waiting = Math.max(waiting, generation); "
                      "return; }",
                      "if (waiting > PB.generation)"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_a_number_applies_on_enter_and_on_leaving_the_field(self):
        # THE ONE PLACE A SETTING IS WIRED TO ITS PAGE'S APPLY (`bindField`)
        # is shared between both pages, so the Enter/blur wiring reads
        # `apply(...)`, its own parameter name.
        for token in ("function changedParams()",
                      "async function applyParams(cause, quiet)",
                      "if (event.key !== 'Enter') return;",
                      "apply('enter');",
                      "el.addEventListener('blur', () => apply('blur', true));",
                      "applyParams('button')",
                      "post('/set_param', body,",
            "[cutterEl, pastEl, alongEl, tangentEl, scoreWidthEl, minTabEl, healEl, elimEl, "
                      "pocketEl].forEach(",
                      "el => bindField(el, applyParams)"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)
        # The field says so, so nobody has to discover it.
        self.assertIn("or press Enter in a field", self.markup)

    def test_a_number_the_config_already_carries_costs_no_recompute(self):
        self.assertIn(
            "if (isFinite(value) && value !== PB.params[pair[1]]) "
            "body[pair[1]] = value;",
            self.markup,
        )
        for key in ("'cutter_diameter'", "'route_past_score_mm'", "'min_tab_mm'"):
            with self.subTest(key=key):
                self.assertIn(key, self.markup)

    def test_stopping_the_server_ends_the_page_either_way(self):
        for token in ("window.close()",
                      "function deadPage(reason)",
                      "function goQuiet()",
                      "stopped = true;",
                      "eventStream.close()",
                      "setTimeout(() => deadPage(",
                      "document.body.textContent = '';",
                      "the preview server has stopped"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_a_stopped_page_talks_to_nothing(self):
        # Every entry point that would reach the server, fenced by one flag.
        for token in ("async function post(path, body, what) {\n  if (stopped) "
                      "return null;",
                      "async function send(path, body) {\n  if (stopped) "
                      "return null;",
                      "async function refresh(cause) {\n  if (stopped) return;",
                      "async function reportProblem(cause, download) {\n"
                      "  if (stopped) return;",
                      "function logEvent(type, data) {\n  if (stopped) return;"):
            with self.subTest(token=token):
                self.assertIn(token, self.markup)

    def test_the_fragment_carries_content_and_not_whole_documents(self):
        fragment = self.session.fragment()
        self.assertTrue(fragment["ok"])
        self.assertIn("Edge.Cuts", fragment["svg"])
        markup = fragment["svg"]["Edge.Cuts"]
        self.assertNotIn("<svg", markup)
        self.assertIn("<path", markup)
        # The generated <title> carries a render timestamp; the live page drops
        # it for the same reason the frozen one does.
        self.assertNotIn("<title>", markup)


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


class ServerTestCase(unittest.TestCase):
    """A real server on 127.0.0.1, started and stopped in this process."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.before_start()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()

    def before_start(self):
        """Hook: the project exists, the server does not yet. Default: nothing."""

    def tearDown(self):
        self.server.stop()
        self._folder.cleanup()

    def get(self, path: str, timeout: float = 10.0):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as reply:
            return reply.status, reply.read().decode("utf-8")

    def post(self, path: str, payload: dict = None, timeout: float = 20.0,
             headers: dict = None):
        """POST as the served page does: JSON, with the session token.

        *headers* replaces the default headers, for a test that sends what a
        page from somewhere else would send.
        """
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers=(headers if headers is not None else {
                "Content-Type": "application/json",
                server_module.TOKEN_HEADER: self.server.token,
            }),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))


class ServerRoutesTest(ServerTestCase):
    """Every route, answered over a socket rather than through a function call."""

    def test_it_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertTrue(self.url.startswith("http://127.0.0.1:"))

    def test_the_root_serves_the_preview_page(self):
        status, body = self.get("")
        self.assertEqual(status, 200)
        self.assertIn("<title>Preview", body)
        self.assertIn("pb-candidates", body)

    def test_state_is_the_cheap_answer(self):
        status, body = self.get("state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(state["ok"])
        self.assertEqual(state["generation"], 1)

    def test_the_fragment_carries_everything_the_page_redraws(self):
        _, body = self.get("fragment")
        fragment = json.loads(body)
        for key in ("generation", "subtitle", "layers", "svg", "proposals",
                    "params", "warnings"):
            self.assertIn(key, fragment)

    def test_an_unknown_path_is_a_404_and_not_a_file(self):
        # There is no static file serving here at all, so a traversal has
        # nothing to reach: every path that is not a route is a 404.
        status, body = self.post("../../etc/passwd")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_a_request_addressed_to_another_host_is_refused(self):
        # DNS rebinding: binding loopback does not stop a page on another site
        # resolving a name here, so the Host header is checked as well.
        request = urllib.request.Request(self.url)
        request.add_header("Host", "attacker.example")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_body_that_is_not_json_is_refused_without_writing(self):
        before = digest(self.board_path)
        request = urllib.request.Request(
            self.url + "accept", data=b"not json", method="POST",
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(digest(self.board_path), before)


class ServerActionsTest(ServerTestCase):
    """A click, end to end: post, edit in memory, recompute, push."""

    def blade_payload(self):
        blade = left_blade(Board.from_file(self.board_path))
        return {"start": list(blade.start), "end": list(blade.end)}

    def files(self):
        return (digest(self.board_path), digest(self.config_path))

    def test_accept_then_decline_over_http_touches_no_file(self):
        before = self.files()
        status, reply = self.post("accept", self.blade_payload())
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["pending"], 1)
        # The decision is on the held board...
        self.assertEqual(len(actions.board_score_lines(self.session.board)), 1)
        # ...and nowhere on disk.
        self.assertEqual(self.files(), before)
        self.assertEqual(score_items(self.board_path), [])
        # Every edit is followed by a recompute and a push.
        self.assertEqual(reply["generation"], 2)
        _, body = self.get("state")
        self.assertEqual(json.loads(body)["generation"], 2)

        status, reply = self.post("decline", self.blade_payload())
        self.assertEqual(status, 200)
        self.assertEqual(actions.board_score_lines(self.session.board), [])
        self.assertEqual(reply["generation"], 3)
        self.assertEqual(reply["pending"], 2)
        self.assertEqual(self.files(), before)

    def test_an_edit_that_changes_nothing_does_not_count(self):
        self.post("accept", self.blade_payload())
        status, reply = self.post("accept", self.blade_payload())
        self.assertEqual(status, 200)
        self.assertEqual(reply["pending"], 1)
        self.assertIn("already scored", reply["message"])

    def test_the_page_reports_the_score_once_it_is_accepted(self):
        self.post("accept", self.blade_payload())
        _, body = self.get("fragment")
        fragment = json.loads(body)
        scored = [p for p in fragment["proposals"] if p["accepted"]]
        self.assertEqual(len(scored), 1)
        self.assertEqual(scored[0]["verdict"], labels_module.SCORED)
        self.assertIn("v-scored", fragment["subtitle"])

    def test_a_blade_line_that_matches_nothing_is_a_400(self):
        before = self.files()
        status, reply = self.post(
            "accept", {"start": [-9.0, -9.0], "end": [-9.0, 9.0]}
        )
        self.assertEqual(status, 400)
        self.assertIn("no v-score candidate", reply["error"])
        self.assertEqual(self.files(), before)

    def test_set_param_edits_the_held_config_and_recomputes(self):
        before = digest(self.config_path)
        status, reply = self.post("set_param", {"cutter_diameter": 1.0})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(self.session.config.prep.cutter_diameter, 1.0)
        self.assertEqual(reply["pending"], 1)
        # The file still says what it said.
        self.assertEqual(digest(self.config_path), before)
        self.assertAlmostEqual(
            load_config(self.config_path).prep.cutter_diameter, CUTTER
        )
        self.assertEqual(reply["generation"], 2)
        _, body = self.get("fragment")
        self.assertAlmostEqual(
            json.loads(body)["params"]["cutter_diameter"], 1.0
        )

    def test_a_bad_parameter_is_a_400_and_changes_nothing(self):
        before = digest(self.config_path)
        status, reply = self.post("set_param", {"cutter_diameter": -1.0})
        self.assertEqual(status, 400)
        self.assertEqual(digest(self.config_path), before)
        self.assertAlmostEqual(self.session.config.prep.cutter_diameter, CUTTER)
        self.assertFalse(reply["ok"])

    def test_reload_only_bumps_the_generation(self):
        before = self.files()
        status, reply = self.post("reload")
        self.assertEqual(status, 200)
        self.assertEqual(reply["generation"], 2)
        self.assertEqual(self.files(), before)

    def test_reload_drops_unsaved_work_and_re_reads(self):
        # Reload re-reads the files. The three-way
        # question is asked on the PAGE, in front of this endpoint, so what the
        # endpoint does is discard and say how much it discarded.
        self.post("accept", self.blade_payload())
        status, reply = self.post("reload")
        self.assertEqual(status, 200)
        self.assertEqual(reply["pending"], 0)
        self.assertIn("discarded", reply["message"])
        self.assertEqual(actions.board_score_lines(self.session.board), [])

    def test_save_is_the_one_action_that_writes_and_it_writes_both(self):
        self.assertEqual(items_on(Board.from_file(self.board_path),
                                  TOOLPATH_LAYER), [])
        self.post("accept", self.blade_payload())
        self.post("set_param", {"cutter_diameter": 1.0})
        config_before = digest(self.config_path)

        status, reply = self.post("save")
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["pending"], 0)

        saved = Board.from_file(self.board_path)
        self.assertTrue(items_on(saved, TOOLPATH_LAYER))
        self.assertEqual(len(score_items(self.board_path)), 1)
        self.assertNotEqual(digest(self.config_path), config_before)
        self.assertAlmostEqual(
            load_config(self.config_path).prep.cutter_diameter, 1.0
        )
        # ...and the count is cleared, so a second stop asks nothing.
        _, body = self.get("state")
        self.assertEqual(json.loads(body)["pending"], 0)

    def test_a_save_with_no_parameter_change_leaves_the_config_alone(self):
        # A config may be hand-written: rewriting one nobody edited would
        # reformat it for nothing.
        before = digest(self.config_path)
        self.post("accept", self.blade_payload())
        self.post("save")
        self.assertEqual(digest(self.config_path), before)
        self.assertEqual(len(score_items(self.board_path)), 1)

    def test_stopping_without_saving_leaves_both_files_untouched(self):
        before = self.files()
        self.post("accept", self.blade_payload())
        self.post("set_param", {"cutter_diameter": 1.0})
        self.post("shutdown")
        for _ in range(100):
            if server_module.active_server() is None:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.files(), before)

    def test_the_state_says_how_much_is_waiting_and_what(self):
        self.post("accept", self.blade_payload())
        _, body = self.get("state")
        state = json.loads(body)
        self.assertEqual(state["pending"], 1)
        self.assertEqual(len(state["pending_changes"]), 1)
        self.assertIn("scored", state["pending_changes"][0])


class LockFileIsIgnoredTest(ServerTestCase):
    """A KiCad lock beside the board stops nothing.

    The names are the ones KiCad writes - `~<name>.<ext>.lck` beside the file,
    per the KiCad source (10.0.3, `include/lockfile.h` lines 54-55 with
    `common/wildcards_and_files_ext.cpp` lines 126-127) - so what is planted here
    is what a live editor would leave, not an approximation of it. Both the
    board's lock and the project's are planted, because opening a board in KiCad
    opens its project and leaves both.
    """

    def before_start(self):
        for target in (self.board_path,
                       self.board_path.with_suffix(".kicad_pro")):
            target.with_name(f"~{target.name}.lck").write_text(
                json.dumps({"username": "designer", "hostname": "bench"}),
                encoding="utf-8",
            )

    def locks(self):
        return sorted(p.name for p in self.folder.glob("~*.lck"))

    def test_the_server_starts_and_serves_with_the_locks_in_place(self):
        self.assertEqual(
            self.locks(), ["~board.kicad_pcb.lck", "~board.kicad_pro.lck"]
        )
        status, body = self.get("")
        self.assertEqual(status, 200)
        self.assertIn("pb-candidates", body)
        status, body = self.get("state")
        self.assertTrue(json.loads(body)["ok"])

    def test_a_save_writes_through_a_lock(self):
        blade = left_blade(Board.from_file(self.board_path))
        status, _ = self.post(
            "accept", {"start": list(blade.start), "end": list(blade.end)}
        )
        self.assertEqual(status, 200)
        before = digest(self.board_path)

        status, reply = self.post("save")
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["pending"], 0)
        self.assertNotEqual(digest(self.board_path), before)
        self.assertEqual(len(score_items(self.board_path)), 1)
        self.assertTrue(items_on(Board.from_file(self.board_path),
                                 TOOLPATH_LAYER))

    def test_nothing_deletes_or_rewrites_the_lock(self):
        # Ignoring a lock is not taking it over: the file is left exactly as the
        # session that wrote it left it.
        before = {p.name: p.read_bytes() for p in self.folder.glob("~*.lck")}
        self.post("save")
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.folder.glob("~*.lck")}, before
        )


class ServerPushTest(ServerTestCase):
    """Change push: one server-sent event per generation, no polling."""

    def test_the_stream_follows_the_generation_a_change_produced(self):
        """A change made anywhere reaches an open page, and lands on the truth.

        What is asserted is the LAST generation the stream carried, not a
        sequence: a listener that has not read yet coalesces to the newest
        number, which is the right behaviour for a "something changed" signal
        and would make an exact sequence flaky under load.
        """
        events = []
        opened = threading.Event()

        def listen():
            with urllib.request.urlopen(self.url + "events", timeout=20) as stream:
                opened.set()
                while not events or events[-1]["generation"] < 2:
                    line = stream.readline().decode("utf-8").strip()
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:]))

        reader = threading.Thread(target=listen, daemon=True)
        reader.start()
        self.assertTrue(opened.wait(10))
        self.session.refresh()
        reader.join(timeout=15)
        self.assertTrue(events)
        self.assertEqual(events[-1]["generation"], 2)
        self.assertEqual(events[-1]["generation"], self.session.generation)


class GatedRender:
    """A renderer a test can hold open, so an action stays in flight.

    The only way to observe "busy" is to be busy: this stops inside the
    session's own compute, holding the session lock and the action gate exactly
    as a real 800 ms recompute does.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def hold(self):
        self.release.clear()
        self.entered.clear()

    def let_go(self):
        self.release.set()

    def __call__(self, board, layers, viewbox_mm, colors, theme=None,
                 workdir=None, content=False, origin=None):
        self.entered.set()
        self.release.wait(20)
        return stub_render(board, layers, viewbox_mm, colors, theme, workdir,
                           content)


class ActionGateTest(unittest.TestCase):
    """One action at a time, and the second one is refused rather than queued.

    An action writes a board and recomputes, which is most of a second on a
    real board. Queueing a click that arrives inside that window is
    what turned three impatient clicks into three writes and a page that jumped
    twice, so the gate is taken WITHOUT blocking and a duplicate is told so.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        folder = Path(self._folder.name)
        self.config_path = write_project(folder)
        self.session = PreviewSession(self.config_path, folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self._folder.cleanup()

    def test_an_idle_session_is_not_busy(self):
        self.assertFalse(self.session.busy)
        self.assertEqual(self.session.running, "")
        self.assertFalse(self.session.state()["busy"])

    def test_the_gate_admits_one_and_names_what_is_running(self):
        with self.session.action("/accept"):
            self.assertTrue(self.session.busy)
            self.assertEqual(self.session.running, "/accept")
            self.assertTrue(self.session.state()["busy"])
            self.assertEqual(self.session.state()["running"], "/accept")
        self.assertFalse(self.session.busy)

    def test_a_second_action_is_refused_and_never_runs(self):
        ran = []
        with self.session.action("/accept"):
            with self.assertRaises(actions.Busy) as caught:
                with self.session.action("/decline"):
                    ran.append("decline")
            self.assertIn("/accept", str(caught.exception))
        self.assertEqual(ran, [])

    def test_the_gate_is_released_when_the_action_fails(self):
        with self.assertRaises(ValueError):
            with self.session.action("/commit"):
                raise ValueError("the stage refused")
        self.assertFalse(self.session.busy)
        # ...and the next one is admitted, which is the point of asserting it.
        with self.session.action("/commit"):
            pass

    def test_a_refusal_does_not_disturb_the_action_in_flight(self):
        with self.session.action("/reload"):
            with self.assertRaises(actions.Busy):
                with self.session.action("/reload"):
                    pass
            self.assertEqual(self.session.running, "/reload")
        self.assertFalse(self.session.busy)


class ServerBusyTest(unittest.TestCase):
    """The busy window over HTTP: refused duplicates, and what stays reachable.

    The session is driven with a renderer that can be held open, so the window
    a real recompute opens is held for as long as the assertions need it.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.board_path = load_config(self.config_path).source_board_path
        self.record = journal_module.open_session(
            self.config_path, self.folder / "scratch",
            stamp="20260908_121500", backend="test",
        )
        self.render = GatedRender()
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=self.render, journal=self.record)
        self.session.refresh()
        self.server = server_module.PreviewServer(self.session, port=0)
        self.url = self.server.start()
        self._threads = []

    def tearDown(self):
        self.render.let_go()
        for thread in self._threads:
            thread.join(timeout=20)
        self.server.stop()
        self._folder.cleanup()

    def post(self, path, payload=None, timeout=20.0):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: self.server.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def get(self, path, timeout=10.0):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def start_action(self, path="/reload"):
        """Post an action that will block in the render, and wait until it has."""
        self.render.hold()
        out = {}

        def run():
            out["status"], out["reply"] = self.post(path.lstrip("/"))

        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        thread.start()
        self.assertTrue(self.render.entered.wait(10), "the action never started")
        return out

    def test_a_duplicate_arriving_mid_action_is_refused_cleanly(self):
        first = self.start_action()
        status, reply = self.post("reload")
        self.assertEqual(status, 409)
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["busy"])
        self.assertIn("not applied", reply["error"])

        self.render.let_go()
        self._threads[0].join(timeout=20)
        self.assertEqual(first["status"], 200)
        # One action ran, one was refused: the generation moved exactly once.
        self.assertEqual(self.session.generation, 2)

    def test_a_refused_duplicate_writes_nothing(self):
        blade = left_blade(Board.from_file(self.board_path))
        payload = {"start": list(blade.start), "end": list(blade.end)}
        before = digest(self.board_path)
        self.start_action()
        status, _ = self.post("accept", payload)
        self.assertEqual(status, 409)
        self.assertEqual(digest(self.board_path), before)
        self.render.let_go()
        self._threads[0].join(timeout=20)
        self.assertEqual(score_items(self.board_path), [])

    def test_a_rejection_is_journaled_as_a_rejection(self):
        self.start_action()
        self.post("save")
        self.render.let_go()
        self._threads[0].join(timeout=20)
        rejected = [
            record for record in self.record.records()
            if record["kind"] == "server.action" and record["data"]["rejected"]
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["data"]["endpoint"], "/save")
        self.assertFalse(rejected[0]["data"]["ok"])
        # ...and the action that DID run is journaled as the ordinary success.
        ran = [
            record for record in self.record.records()
            if record["kind"] == "server.action"
            and record["data"]["endpoint"] == "/reload"
        ]
        self.assertEqual(len(ran), 1)
        self.assertFalse(ran[0]["data"]["rejected"])

    def test_state_answers_during_an_action_and_says_what_is_running(self):
        # The one answer that must arrive while the lock is held: a busy flag
        # nobody can read until the work finishes reports nothing.
        self.start_action("/reload")
        state = self.get("state", timeout=5.0)
        self.assertTrue(state["busy"])
        self.assertEqual(state["running"], "/reload")
        self.render.let_go()
        self._threads[0].join(timeout=20)
        self.assertFalse(self.get("state")["busy"])

    def test_recording_is_never_gated(self):
        # Watching the user must keep working while they wait; a journal
        # with a hole in it exactly where the page was busy is the wrong log.
        self.start_action()
        status, reply = self.post(
            "journal", {"events": [{"type": "control", "id": "pb-reload"}]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(reply["logged"], 1)

    def test_shutdown_is_never_gated(self):
        # A stuck action must not take the only way out with it.
        self.start_action()
        status, reply = self.post("shutdown")
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])

    def test_two_callers_may_stop_the_same_server(self):
        # The shutdown POST spawns a stop of its own, so whoever launched the
        # server is a second caller by construction. Found by this suite.
        self.post("shutdown")
        for _ in range(100):
            if server_module.active_server() is None:
                break
            threading.Event().wait(0.05)
        self.server.stop()
        self.assertIsNone(server_module.active_server())


class BoardHandoffTest(unittest.TestCase):
    """The board an action edited is the board the recompute reads.

    Measured on the corpus board: parsing it costs 210 ms and an accept did it
    twice - once to write the score, once to recompute - inside one click. The
    board is now parsed ONCE per session and held, so neither parse remains and
    neither does the write between them.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_project(self.folder)
        self.config = load_config(self.config_path)
        self.board_path = self.config.source_board_path

    def tearDown(self):
        self._folder.cleanup()

    def test_compute_uses_the_board_it_was_given(self):
        board = Board.from_file(self.board_path)
        computed = prep.compute(self.config, board)
        self.assertIs(computed.board, board)
        self.assertGreater(computed.result.written, 0)
        # Nothing was parsed, so the load this result reports is the no-op.
        self.assertLess(computed.result.load_seconds, 0.01)

    def test_the_given_board_is_what_is_computed_and_not_the_file(self):
        # Sharpest available proof: hand it a board the file does not carry.
        board = Board.from_file(self.board_path)
        blade = left_blade(board)
        board.drawings.append(route_toolpath.score_line_for(
            actions.matching_candidate(blade, candidates_of(board)),
            outline_bbox(board),
        ))
        self.assertEqual(score_items(self.board_path), [])
        computed = prep.compute(self.config, board)
        self.assertEqual(len(computed.result.scores), 1)

    def test_an_edit_lands_on_the_board_the_session_holds(self):
        board = Board.from_file(self.board_path)
        written = actions.accept(
            board, left_blade(board), candidates_of(board)
        )
        self.assertTrue(written.changed)
        self.assertEqual(len(actions.board_score_lines(board)), 1)

    def test_decline_leaves_the_board_alone_when_there_is_nothing_to_do(self):
        board = Board.from_file(self.board_path)
        written = actions.decline(board, left_blade(board))
        self.assertEqual((written.added, written.removed), (0, 0))
        self.assertEqual(actions.board_score_lines(board), [])

    def test_the_session_parses_the_board_once(self):
        record = journal_module.open_session(
            self.config_path, self.folder / "scratch",
            stamp="20260908_122000", backend="test",
        )
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render, journal=record)
        session.refresh()
        held = session.board
        blade = left_blade(Board.from_file(self.board_path))
        session.accept({"start": list(blade.start), "end": list(blade.end)})
        session.refresh()
        # The same object throughout: neither an accept nor a plain recompute
        # re-reads the board.
        self.assertIs(session.board, held)
        reused = [
            entry["data"]["reused_board"] for entry in record.records()
            if entry["kind"] == "compute"
        ]
        self.assertEqual(reused, [False, True, True])

    def test_reload_always_re_reads_the_board(self):
        # Reload's job is picking up an edit made in
        # KiCad, so it re-reads whether or not anything is pending. The
        # unsaved-work question is the page's, in front of this.
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        held = session.board
        session.reload_from_disk()
        self.assertIsNot(session.board, held)

    def test_reload_drops_the_edits_it_just_threw_away(self):
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        blade = left_blade(Board.from_file(self.board_path))
        session.accept({"start": list(blade.start), "end": list(blade.end)})
        self.assertEqual(session.pending_count, 1)
        reply = session.reload_from_disk()
        self.assertEqual(session.pending_count, 0)
        self.assertIn("discarded", reply["message"])
        self.assertEqual(actions.board_score_lines(session.board), [])

    def test_a_new_keepout_width_redraws_the_keepouts_on_the_board(self):
        # The width is a PREP setting, so changing it is a board change. The
        # strips already on the board were drawn at the old number; redrawing
        # them keeps the config and the board on one width.
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        blade = left_blade(Board.from_file(self.board_path))
        session.accept({"start": list(blade.start), "end": list(blade.end)})
        before = board_scores.keepouts_on(session.board)
        self.assertEqual(len(before), 1)
        self.assertAlmostEqual(keepout_span(before[0])[2], 0.3)

        reply = session.set_param({"score_rule_half_width_mm": 0.55})
        after = board_scores.keepouts_on(session.board)
        self.assertEqual(len(after), 1)
        self.assertAlmostEqual(keepout_span(after[0])[2], 0.55)
        self.assertIn("redrawn at 0.55 mm", reply["message"])
        self.assertEqual(session.config.score_rule_half_width_mm, 0.55)

    def test_a_width_that_did_not_move_redraws_nothing(self):
        # The sync is idempotent, so re-stating the number the board already
        # agrees with is not a change and says nothing.
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        blade = left_blade(Board.from_file(self.board_path))
        session.accept({"start": list(blade.start), "end": list(blade.end)})
        held = board_scores.keepouts_on(session.board)[0]
        reply = session.set_param({"score_rule_half_width_mm": 0.3})
        self.assertNotIn("redrawn", reply["message"])
        self.assertIs(board_scores.keepouts_on(session.board)[0], held)

    def test_an_accepted_score_is_on_the_page_the_action_pushed(self):
        # What the page shows after an accept is a reading of the board the
        # session is editing - not of the file, which has not moved.
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        blade = left_blade(Board.from_file(self.board_path))
        session.accept({"start": list(blade.start), "end": list(blade.end)})
        fragment = session.fragment()
        self.assertEqual(len([p for p in fragment["proposals"] if p["accepted"]]), 1)
        self.assertEqual(score_items(self.board_path), [])
        # ...and it survives a recompute rather than being a one-shot answer.
        session.refresh()
        self.assertEqual(
            len([p for p in session.fragment()["proposals"] if p["accepted"]]), 1
        )

    def test_recomputing_never_stacks_a_second_toolpath_on_the_held_board(self):
        # The held board is mutated by every compute, so idempotence is now a
        # property of the object rather than of a file.
        session = PreviewSession(self.config_path, self.folder / "work",
                                 render=stub_render)
        session.refresh()
        first = len(items_on(session.board, TOOLPATH_LAYER))
        self.assertGreater(first, 0)
        session.refresh()
        session.refresh()
        self.assertEqual(len(items_on(session.board, TOOLPATH_LAYER)), first)


class ServerLifecycleTest(unittest.TestCase):
    """Binding, refusing a second session, and stopping cleanly."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        folder = Path(self._folder.name)
        self.config_path = write_project(folder)
        self.session = PreviewSession(self.config_path, folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self._folder.cleanup()

    def make(self):
        server = server_module.PreviewServer(self.session, port=0)
        self.servers.append(server)
        return server

    def test_a_second_server_in_one_process_is_refused_by_name(self):
        first = self.make()
        first.start()
        second = self.make()
        with self.assertRaises(RuntimeError) as caught:
            second.start()
        self.assertIn("already running", str(caught.exception))

    def test_stopping_releases_the_slot(self):
        first = self.make()
        first.start()
        first.stop()
        self.assertIsNone(server_module.active_server())
        second = self.make()
        second.start()
        self.assertIs(server_module.active_server(), second)

    def test_the_shutdown_endpoint_stops_the_server(self):
        server = self.make()
        url = server.start()
        request = urllib.request.Request(
            url + "shutdown", data=b"{}", method="POST",
            headers={"Content-Type": "application/json",
                     server_module.TOKEN_HEADER: server.token},
        )
        with urllib.request.urlopen(request, timeout=10) as reply:
            self.assertTrue(json.loads(reply.read().decode("utf-8"))["ok"])
        for _ in range(100):
            if server_module.active_server() is None:
                break
            threading.Event().wait(0.05)
        self.assertIsNone(server_module.active_server())
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=5)

    def test_it_will_not_bind_anything_but_loopback(self):
        with self.assertRaises(ValueError):
            server_module.PreviewServer(self.session, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
