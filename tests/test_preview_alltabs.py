"""Both tabs open ready, and both of them list every layer the board has.

**The panel is derived before the browser opens.** A launch computes the board
and then derives the panel in memory, so step 2 opens showing a panel instead
of an empty viewport. A config whose panel cannot be built opens stating why,
and the reason is on the bytes the page is served as, not in a log.

**Both tabs list ALL of the board's layers.** A list filtered down to the four
layers prep is about hides the tooling holes: their mask apertures are on a
layer that filter drops, and there is no Holes row. So the layer panel is the
board's whole table in board order, what starts VISIBLE is the physical layers
plus each page's own subject, and the Holes overlay is on both pages exactly as
it is on the frozen review pages - drill marks on top, in the editor's colours.

**Content decides the renderer.** A page that lists F.Cu and draws nothing on it
is a worse defect than the one being fixed, and the fast library renderer draws
no pad, track or zone (measured: 299 bytes against kicad-cli's 74 005). So the
preview asks for a content-complete render and kicad-cli answers it, which is
also what puts the holes and the mask apertures on the page. The two backends
are asserted to place geometry at the SAME board coordinates, because that
register is what an overlay drawn in board millimetres depends on.
"""

import json
import re
import tempfile
import unittest
import urllib.request
from pathlib import Path

from eedesigncore import Board, BoardLayer

from eedesignpanels import prep
from eedesignpanels.config import load_config
from eedesignpanels.preview import (
    page_panel,
    panelbuild,
    render_seam,
    server as server_module,
    session as session_module,
)
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.review import holes as holes_module, layerpanel, render
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.stages import panel_dimensions, route_toolpath
from test_panel_page import write_panel_project
from test_preview import stub_render

CLI = render_seam.cli_available()
LIBRARY = render_seam.BACKEND == render_seam.LIBRARY_BACKEND
HOLE_DIAMETER = 3.175


def with_tooling_holes(config_path: Path, diameter: float = HOLE_DIAMETER,
                       rails: float = 8.0) -> Path:
    """The same project, with the four tooling holes switched on."""
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["panel"] = {
        "rails": rails,
        "tooling_holes": {"diameter_mm": diameter},
    }
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return config_path


def baked_layers(markup: str) -> list:
    """The layer table the emitted page handed its own layer panel."""
    head = markup.split("const LAYERS = ", 1)[1]
    return json.loads(head[:head.index("\n")].rstrip(";"))


def stacked(markup: str) -> list:
    """Every layer the page stacked an SVG element for, in document order.

    Read off the DOCUMENT and not off its scripts: the layer panel's own JS
    names the attribute too, and a fence that counted those would be reading
    the code rather than the page.
    """
    body = re.sub(r"<script[^>]*>.*?</script>", " ", markup, flags=re.S)
    return re.findall(r'data-layer="([^"]+)"', body)


def notes_of(markup: str) -> str:
    """Every note the served state carries, joined into one string.

    There is no visible subtitle span. A build reason still reaches the served
    bytes - it rides in the
    embedded `PB` state's own `notes`, and the client sends it to the
    developer console (`drawNotes`) rather than painting a sentence over the
    viewport.
    """
    state = json.loads(
        re.search(r"let PB = (\{.*?\});\n", markup, re.S).group(1)
    )
    return " ".join(state.get("notes", []))


def placed(markup: str, layer: str, min_radius: float = 0.0) -> list:
    """The circles of one stacked layer, in BOARD millimetres.

    The stacked element carries kicad-cli's own content under one rigid
    translate, so a mark's board position is its own coordinate plus that
    translate - which is the register the whole page depends on.
    """
    # The stack ends at the next `<svg`, at the viewport's own `</div>`, or at
    # the corner note that sits over the last layer.
    found = re.search(
        r'<svg[^>]*data-layer="%s"[^>]*>(.*?)</svg>\s*(?=<svg|<div|</div>)'
        % re.escape(layer),
        markup, re.S,
    )
    if found is None:
        return []
    body = found.group(1)
    moved = re.search(r'translate\(([-\d.]+) ([-\d.]+)\)', body)
    dx, dy = (
        (float(moved.group(1)), float(moved.group(2))) if moved else (0.0, 0.0)
    )
    return [
        (float(x) + dx, float(y) + dy, float(r))
        for x, y, r in re.findall(
            r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="([-\d.]+)"', body
        )
        if float(r) >= min_radius
    ]


# ---------------------------------------------------------------------------
# the panel is derived at startup
# ---------------------------------------------------------------------------


class StartupPanelTest(unittest.TestCase):
    """Prep compute, then panel derive, and only then is a page worth opening."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)

    def tearDown(self):
        self._folder.cleanup()

    def session(self, config_path: Path = None) -> PreviewSession:
        session = PreviewSession(config_path or self.config_path,
                                 self.folder / "work", render=stub_render)
        session.refresh()
        return session

    def test_a_fresh_session_has_no_panel_until_it_is_asked_for_one(self):
        # The control for everything below: nothing about loading a config
        # derives a panel by itself.
        session = self.session()
        self.assertIsNone(session.panel)
        self.assertEqual(session.panel_stale, [panelbuild.NEVER_BUILT])

    def test_startup_derives_the_panel_and_the_page_shows_it(self):
        session = self.session()
        session.start_panel()
        self.assertIsNotNone(session.panel)
        self.assertEqual(session.panel_error, "")
        self.assertEqual(session.panel_stale, [])
        markup = session.panel_page()
        self.assertNotIn(panelbuild.NEVER_BUILT, notes_of(markup))
        self.assertIn(session.panel.subtitle.split(" - ")[0], markup)
        # ...and the viewport is a picture, not the empty stack a page carries
        # before its first build.
        self.assertTrue(stacked(markup))
        self.assertIn("<path", markup)

    def test_the_board_is_computed_before_the_panel_is_derived(self):
        # The order matters: a panel is derived FROM the prepared
        # board, so a panel derived first would be a panel of the old toolpath.
        session = PreviewSession(self.config_path, self.folder / "order",
                                 render=stub_render)
        session.start_panel()
        self.assertIsNone(session.panel)
        self.assertIn("could not be prepared", session.panel_error)

    def test_the_rails_are_left_unfilled_like_every_other_picture(self):
        session = self.session()
        session.start_panel()
        self.assertFalse(session.panel.fill_rails)

    def test_after_startup_a_change_recalculates_instead_of_waiting(self):
        # `panel_live` is what turns the banner into a recalculation, and the
        # startup derive is what sets it.
        session = self.session()
        session.start_panel()
        self.assertTrue(session.panel_live)
        first = session.panel
        session.set_param({"columns": 3})
        self.assertIsNot(session.panel, first)
        self.assertEqual(session.panel_stale, [])

    def test_a_panel_that_cannot_be_built_opens_stating_the_reason(self):
        # NEVER A SILENT EMPTY PAGE. A config that panelizes nothing is the
        # cheapest example of a panel that cannot be derived at all.
        path = write_panel_project(self.folder / "plain", stage="passthrough")
        session = self.session(path)
        session.start_panel()
        self.assertIsNone(session.panel)
        self.assertIn("passthrough", session.panel_error)
        markup = session.panel_page()
        # The reason is on the served bytes, in the state's own notes.
        self.assertIn("passthrough", notes_of(markup))
        self.assertNotIn(panelbuild.NEVER_BUILT, notes_of(markup))

    def test_a_board_that_cannot_be_prepared_says_that_and_not_press_build(self):
        session = self.session()
        session.view = None
        session.error = "the outline does not close"
        session.start_panel()
        self.assertEqual(session.panel_error, "the outline does not close")
        self.assertIn("the outline does not close",
                      notes_of(session.panel_page()))

    def test_starting_the_panel_writes_nothing(self):
        before = {
            path: path.read_bytes()
            for path in self.folder.rglob("*") if path.is_file()
        }
        session = self.session()
        session.start_panel()
        after = {
            path: path.read_bytes()
            for path in self.folder.rglob("*")
            if path.is_file() and "work" not in path.parts
        }
        for path, data in after.items():
            self.assertIn(path, before, f"{path} was created by a startup derive")
            self.assertEqual(data, before[path], f"{path} was rewritten")

    def test_the_panel_is_journaled_as_a_startup_derive(self):
        written = []
        session = self.session()
        session.log = lambda kind, **data: written.append((kind, data))
        session.start_panel()
        kinds = [kind for kind, _ in written]
        self.assertIn("panel.start", kinds)
        record = dict(written[kinds.index("panel.start")][1])
        self.assertTrue(record["ok"])
        self.assertFalse(record["fill_rails"])

    def test_prep_is_idempotent_across_the_startup_sequence(self):
        # The startup derive reads the held board; recomputing after it must
        # leave exactly the same board, or a panel derived at startup and one
        # derived by the next edit would differ over nothing.
        session = self.session()
        first = session.board.to_string()
        session.start_panel()
        session.refresh()
        self.assertEqual(session.board.to_string(), first)


class StartupServerTest(unittest.TestCase):
    """The two routes a browser opens, answered by a server in this process."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()
        self.live = server_module.PreviewServer(self.session, port=0)
        self.url = self.live.start()

    def tearDown(self):
        self.live.stop()
        self.session.close()
        self._folder.cleanup()

    def get(self, route: str) -> str:
        with urllib.request.urlopen(self.url + route, timeout=30) as reply:
            self.assertEqual(reply.status, 200)
            return reply.read().decode("utf-8")

    def test_both_tabs_answer_and_the_panel_one_carries_a_panel(self):
        self.assertIn("id=pb-controls", self.get(""))
        panel = self.get("panel")
        self.assertIn("pb-build", panel)
        self.assertNotIn(panelbuild.NEVER_BUILT, notes_of(panel))

    def test_the_panel_fragment_is_ready_before_anything_is_clicked(self):
        fragment = json.loads(self.get("panel/fragment"))
        self.assertTrue(fragment["ok"])
        self.assertEqual(fragment["stale"], [])
        self.assertTrue(fragment["layers"])
        self.assertTrue(fragment["report"])


# ---------------------------------------------------------------------------
# every layer, on both tabs
# ---------------------------------------------------------------------------


class EveryLayerTest(unittest.TestCase):
    """The layer panel is the board's own table, on step 1 and on step 2."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self._folder.cleanup()

    def declared(self) -> list:
        return [layer.canonical_name for layer in self.session.board.layers]

    def test_the_selection_is_the_whole_table_and_not_a_filter(self):
        names = [
            layer.canonical_name
            for layer in self.session._shown_layers(self.session.board)
        ]
        self.assertEqual(names, self.declared())
        # The four layers prep is about are in there, and so is everything else.
        for name in prep.REVIEW_LAYERS:
            self.assertIn(name, names)
        self.assertIn("F.Cu", names)
        self.assertIn("B.Cu", names)

    def test_a_board_with_no_layers_at_all_is_refused_by_name(self):
        board = self.session.board.model_copy(deep=True)
        board.layers = []
        with self.assertRaises(ValueError) as raised:
            self.session._shown_layers(board, subject="panel")
        self.assertIn("panel", str(raised.exception))
        self.assertIn("no layers", str(raised.exception))

    def test_the_prep_page_lists_every_layer(self):
        listed = [row["canonical"] for row in baked_layers(self.session.page())]
        for name in self.declared():
            self.assertIn(name, listed)

    def test_the_panel_page_lists_every_layer(self):
        listed = [
            row["canonical"] for row in baked_layers(self.session.panel_page())
        ]
        for name in self.declared():
            self.assertIn(name, listed)

    def test_both_pages_list_the_same_layers_in_the_same_order(self):
        # Layer visibility travels between the tabs by canonical name, so two
        # lists that disagree are two panels rather than one shared one.
        prep_rows = [
            row["canonical"] for row in baked_layers(self.session.page())
            if row["canonical"] != prep.PROPOSAL_LAYER
        ]
        panel_rows = [
            row["canonical"] for row in baked_layers(self.session.panel_page())
        ]
        self.assertEqual(prep_rows, panel_rows)

    def test_every_listed_layer_has_an_element_to_toggle(self):
        # A row with no element behind it is a switch that does nothing.
        for markup in (self.session.page(), self.session.panel_page()):
            rows = [row["canonical"] for row in baked_layers(markup)]
            self.assertEqual(sorted(rows), sorted(set(stacked(markup))))

    def test_the_physical_layers_start_visible_and_the_rest_do_not(self):
        for markup in (self.session.page(), self.session.panel_page()):
            for row in baked_layers(markup):
                spec = LayerSpec(0, row["canonical"], row["type"],
                                 row["display"])
                if row["canonical"] in (
                    *prep.REVIEW_LAYERS, prep.PROPOSAL_LAYER,
                    holes_module.HOLES_LAYER,
                ):
                    self.assertTrue(row["start"], row["canonical"])
                else:
                    self.assertEqual(
                        row["start"], layerpanel.is_physical_layer(spec),
                        row["canonical"],
                    )

    def test_copper_and_mask_are_switched_on_when_the_page_opens(self):
        # The tooling holes were invisible with these off: a hole shows on a
        # page through its mask aperture and its drill mark.
        for markup in (self.session.page(), self.session.panel_page()):
            on = {row["canonical"] for row in baked_layers(markup) if row["start"]}
            self.assertIn("F.Cu", on)
            self.assertIn("B.Cu", on)

    def test_the_shared_layer_panel_takes_the_whole_list(self):
        # Layer sync posts every known name; a table this size must not trip the
        # request guard, and it must come back on the cheap poll both tabs read.
        names = self.declared()
        self.assertLessEqual(len(names), 128)
        self.session.set_layer_visibility(visible=names[:3], known=names,
                                          active=names[0], mode=1)
        state = self.session.state()
        self.assertEqual(state["layers"]["known"], names)
        self.assertEqual(state["layers"]["visible"], names[:3])
        self.assertEqual(
            self.session.panel_fragment()["layers_seq"], state["layers_seq"]
        )


class DimensionsOpenVisibleTest(unittest.TestCase):
    """The panel opens its dimensions layer. The single-board default is unchanged."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        board_path = self.folder / "board.kicad_pcb"
        board = Board.from_file(board_path)
        # The dimensions layer as the corpus board declares it:
        # `(69 "User.16" user "User.16-Dimensions")`.
        board.layers.append(BoardLayer(
            ordinal=69, canonical_name=panel_dimensions.DIMENSION_LAYER,
            layer_type="user", user_name="User.16-Dimensions",
        ))
        board.to_file(board_path)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self._folder.cleanup()

    def start_state(self, markup: str) -> bool:
        for row in baked_layers(markup):
            if row["canonical"] == panel_dimensions.DIMENSION_LAYER:
                return row["start"]
        raise AssertionError("the dimensions layer is not in this page's table")

    def test_the_panel_page_opens_the_dimensions_layer_visible(self):
        self.assertTrue(self.start_state(self.session.panel_page()))

    def test_the_layer_remains_in_the_panel_tab_for_user_control(self):
        names = [row["canonical"]
                 for row in baked_layers(self.session.panel_page())]
        self.assertIn(panel_dimensions.DIMENSION_LAYER, names)

    def test_the_prep_page_leaves_it_under_the_physical_rule(self):
        self.assertFalse(self.start_state(self.session.page()))

    def test_a_config_with_no_panel_asks_for_no_extra_layer(self):
        # `build` reads the dimensions layer off the config; a config that
        # panelizes nothing has no dimension settings to read.
        config_path = write_panel_project(self.folder / "flat", panelize=False)
        session = PreviewSession(config_path, self.folder / "flatwork",
                                 render=stub_render)
        session.refresh()
        markup = session.panel_page()
        for row in baked_layers(markup):
            if row["canonical"] == panel_dimensions.DIMENSION_LAYER:
                self.assertFalse(row["start"])


class HolesRowTest(unittest.TestCase):
    """The Holes overlay, on both live pages exactly as on the frozen ones."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = with_tooling_holes(
            write_panel_project(self.folder)
        )
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()
        self.session.start_panel()

    def tearDown(self):
        self._folder.cleanup()

    def row(self, markup: str) -> dict:
        for row in baked_layers(markup):
            if row["canonical"] == holes_module.HOLES_LAYER:
                return row
        raise AssertionError("no Holes row in this page's layer table")

    def test_both_pages_carry_a_holes_row(self):
        for markup in (self.session.page(), self.session.panel_page()):
            self.assertEqual(self.row(markup)["display"],
                             holes_module.HOLES_LAYER_DISPLAY)

    def test_the_holes_row_stacks_above_everything_and_opens_on(self):
        for markup in (self.session.page(), self.session.panel_page()):
            row = self.row(markup)
            self.assertTrue(row["top"], "a hole belongs to no layer")
            self.assertTrue(row["start"], "a hole nobody sees is not reviewed")
            self.assertFalse(row["physical"], "the row is not a board layer")

    def test_the_marks_are_stacked_as_their_own_element(self):
        for markup in (self.session.page(), self.session.panel_page()):
            self.assertIn(holes_module.HOLES_LAYER, stacked(markup))

    def test_the_row_carries_the_two_colour_swatch(self):
        # No board layer supplies one, and a row with no swatch reads as a layer
        # the theme forgot.
        theme = self.session.panel.theme
        self.assertNotEqual(
            theme.layer_color(holes_module.HOLES_LAYER), ""
        )

    def test_a_board_with_no_holes_gets_no_row(self):
        # Silence means "this board has no drilled holes", which is the honest
        # ordinary answer and the one the frozen pages give.
        def no_holes(board, layers, viewbox_mm, colors, theme=None,
                     workdir=None, content=False, origin=None):
            produced = stub_render(board, layers, viewbox_mm, colors, theme,
                                   workdir, content, origin)
            produced.pop(holes_module.HOLES_LAYER, None)
            return produced

        session = PreviewSession(self.config_path, self.folder / "dry",
                                 render=no_holes)
        session.refresh()
        session.start_panel()
        for markup in (session.page(), session.panel_page()):
            listed = [row["canonical"] for row in baked_layers(markup)]
            self.assertNotIn(holes_module.HOLES_LAYER, listed)
            self.assertNotIn(holes_module.HOLES_LAYER, stacked(markup))

    def test_the_hole_colours_handed_to_the_renderer_are_the_editors(self):
        seen = {}

        def watch(board, layers, viewbox_mm, colors, theme=None, workdir=None,
                  content=False, origin=None):
            seen.update(colors)
            return stub_render(board, layers, viewbox_mm, colors, theme,
                               workdir, content, origin)

        session = PreviewSession(self.config_path, self.folder / "colors",
                                 render=watch)
        session.refresh()
        palette = holes_module.editor_palette(session.view.theme)
        self.assertEqual(seen[render_seam.PLATED_HOLES],
                         palette[holes_module.KNOCKOUT_COLOR])
        self.assertEqual(seen[render_seam.NON_PLATED_HOLES],
                         palette[holes_module.MARK_COLOR])


# ---------------------------------------------------------------------------
# the seam: which backend, and what it draws
# ---------------------------------------------------------------------------


class ContentBackendTest(unittest.TestCase):
    """Content decides the renderer, and the shortfall is always stated."""

    def test_a_content_render_goes_to_kicad_cli_when_it_is_there(self):
        with unittest.mock.patch.object(render_seam, "cli_available",
                                        return_value=True):
            self.assertEqual(
                render_seam.choose_backend(content=True), render_seam.CLI_BACKEND
            )

    def test_without_kicad_cli_the_detected_backend_answers(self):
        with unittest.mock.patch.object(render_seam, "cli_available",
                                        return_value=False):
            self.assertEqual(
                render_seam.choose_backend(content=True), render_seam.BACKEND
            )

    def test_a_render_that_needs_no_content_keeps_the_fast_path(self):
        with unittest.mock.patch.object(render_seam, "cli_available",
                                        return_value=True):
            self.assertEqual(render_seam.choose_backend(), render_seam.BACKEND)

    def test_kicad_cli_draws_content_so_it_owes_no_notice(self):
        self.assertEqual(
            render_seam.content_notice(render_seam.CLI_BACKEND), ""
        )

    def test_the_fast_path_says_what_it_did_not_draw(self):
        notice = render_seam.content_notice(render_seam.LIBRARY_BACKEND)
        self.assertIn("copper", notice)
        self.assertIn("EMPTY", notice)
        self.assertIn(str(render.DEFAULT_KICAD_CLI), notice)

    def test_the_preview_backend_is_the_content_answer(self):
        self.assertEqual(
            render_seam.preview_backend(),
            render_seam.choose_backend(content=True),
        )

    def test_the_holes_row_is_taken_out_of_the_layer_list(self):
        layers = [
            LayerSpec(0, "F.Cu", "signal", "F.Cu"),
            holes_module.holes_layer([LayerSpec(0, "F.Cu", "signal", "F.Cu")]),
        ]
        wanted, holes = render_seam.split_holes(layers)
        self.assertTrue(holes)
        self.assertEqual([layer.canonical_name for layer in wanted], ["F.Cu"])
        # ...and a list without it asks for none.
        self.assertEqual(render_seam.split_holes(wanted), (wanted, False))

    def test_the_palette_is_read_off_the_two_pseudo_layer_names(self):
        palette = render_seam.hole_palette({
            render_seam.PLATED_HOLES: "#111111",
            render_seam.NON_PLATED_HOLES: "#222222",
        })
        self.assertEqual(palette[holes_module.KNOCKOUT_COLOR], "#111111")
        self.assertEqual(palette[holes_module.MARK_COLOR], "#222222")
        self.assertEqual(render_seam.hole_palette({}), {})

    def test_documents_drawn_in_one_frame_merge_into_one(self):
        first = '<svg viewBox="0 0 1 1"><circle r="1"/></svg>'
        second = '<svg viewBox="0 0 1 1"><circle r="2"/></svg>'
        merged = render_seam.merge_documents([first, second])
        self.assertEqual(merged.count("<circle"), 2)
        self.assertEqual(merged.count("<svg"), 1)
        self.assertEqual(merged.count("</svg>"), 1)
        self.assertEqual(render_seam.merge_documents(["", None]), "")

    def test_an_empty_document_is_not_worth_stacking(self):
        self.assertFalse(render_seam.has_drawing('<svg><g></g></svg>'))
        self.assertTrue(render_seam.has_drawing('<svg><circle r="1"/></svg>'))

    def test_values_are_hidden_for_the_render_and_put_back_after(self):
        from test_vscore import scoreable_board

        board = scoreable_board()
        board.footprints[0].properties[0].name = "Value"
        board.footprints[0].properties[0].visible = True
        with render_seam.values_hidden(board):
            self.assertFalse(board.footprints[0].properties[0].visible)
        self.assertTrue(board.footprints[0].properties[0].visible)

    def test_the_flag_is_put_back_even_when_the_write_fails(self):
        from test_vscore import scoreable_board

        board = scoreable_board()
        board.footprints[0].properties[0].name = "Value"
        board.footprints[0].properties[0].visible = True
        with self.assertRaises(RuntimeError):
            with render_seam.values_hidden(board):
                raise RuntimeError("the write failed")
        self.assertTrue(board.footprints[0].properties[0].visible)


@unittest.skipUnless(LIBRARY, "the library renderer draws the hole layers")
class LibraryHolesTest(unittest.TestCase):
    """The fast path's own holes: the pseudo-layers the library landed."""

    def board(self):
        from eedesigncore import DrillInfo, Footprint, Pad, PadShape, PadType

        from test_vscore import scoreable_board

        board = scoreable_board()
        board.footprints.append(Footprint(
            fp_id="eedesignpanels:Hole", layer="F.Cu", uuid="hole-fp",
            at={"position": (5.0, 5.0), "angle": 0.0},
            pads=[Pad(
                number="", pad_type=PadType.np_thru_hole,
                shape=PadShape.circle, position=(0.0, 0.0),
                size=(2.0, 2.0), drill=DrillInfo(width=2.0),
                layers=["*.Cu", "*.Mask"], id="hole-pad",
            )],
        ))
        return board

    def rendered(self):
        import eedesigncore

        layers = [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")]
        return render_seam.render_with_library(
            self.board(),
            [*layers, holes_module.holes_layer(layers)],
            (0.0, 0.0, 10.0, 10.0), {
                "Edge.Cuts": "#ffffff",
                render_seam.PLATED_HOLES: "#e3b72e",
                render_seam.NON_PLATED_HOLES: "#c83434",
            },
            eedesigncore.render_board_svg, renderer_name="render_board_svg",
            holes=True,
        )

    def test_the_marks_come_back_as_one_holes_document(self):
        produced = render_seam.render_layers(
            self.board(),
            [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts"),
             holes_module.holes_layer(
                 [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")])],
            (0.0, 0.0, 10.0, 10.0),
            {"Edge.Cuts": "#ffffff",
             render_seam.NON_PLATED_HOLES: "#c83434"},
        )
        self.assertIn(render_seam.HOLES_LAYER, produced)
        self.assertIn("<circle", produced[render_seam.HOLES_LAYER])

    def test_the_mark_is_where_the_hole_is_and_the_size_of_the_drill(self):
        document = self.rendered()[render_seam.HOLES_LAYER]
        found = re.findall(
            r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="([-\d.]+)"', document
        )
        self.assertEqual(len(found), 1)
        x, y, radius = (float(value) for value in found[0])
        self.assertAlmostEqual(x, 5.0, places=3)
        self.assertAlmostEqual(y, 5.0, places=3)
        self.assertAlmostEqual(radius, 1.0, places=3)

    def test_the_mark_is_painted_the_editors_non_plated_colour(self):
        self.assertIn("#c83434", self.rendered()[render_seam.HOLES_LAYER])

    def test_a_board_with_no_holes_gets_no_document(self):
        from test_vscore import scoreable_board

        import eedesigncore

        layers = [LayerSpec(0, "Edge.Cuts", "user", "Edge.Cuts")]
        produced = render_seam.render_with_library(
            scoreable_board(), [*layers, holes_module.holes_layer(layers)],
            (0.0, 0.0, 10.0, 10.0), {"Edge.Cuts": "#ffffff"},
            eedesigncore.render_board_svg, renderer_name="render_board_svg",
            holes=True,
        )
        self.assertNotIn(render_seam.HOLES_LAYER, produced)


@unittest.skipUnless(CLI, "no kicad-cli to render content with")
class RenderRegisterTest(unittest.TestCase):
    """kicad-cli's cropped export, carried back to BOARD coordinates.

    Measured: the export is cropped to the outline box and starts at
    `viewBox="0 0 w h"`, so carrying it back by the caller's WINDOW origin put
    every plot short by the window margin - five millimetres on the corpus
    board - and out of register with the overlays, which are drawn in board
    millimetres. The two backends drawing the same board into the same window
    must land on the same coordinates, and that is what this asserts.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        config = load_config(self.config_path)
        from eedesigncore import Board

        self.board = Board.from_file(config.source_board_path)
        self.bbox = route_toolpath.outline_bbox(self.board)
        self.window = (self.bbox[0] - 5.0, self.bbox[1] - 5.0,
                       self.bbox[2] - self.bbox[0] + 10.0,
                       self.bbox[3] - self.bbox[1] + 10.0)
        self.layers = render.select_layers(
            render.layers_of(self.board), ["Edge.Cuts"]
        )

    def tearDown(self):
        self._folder.cleanup()

    def extent(self, document: str) -> tuple:
        moved = re.search(r'translate\(([-\d.]+) ([-\d.]+)\)', document)
        dx, dy = (
            (float(moved.group(1)), float(moved.group(2)))
            if moved else (0.0, 0.0)
        )
        points = [
            (float(x) + dx, float(y) + dy)
            for x, y in re.findall(
                r'[ML,]\s*([-\d.]+)[ ,]([-\d.]+)', document
            )
        ]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def test_the_plot_lands_on_the_boards_own_coordinates(self):
        produced = render_seam.render_with_cli(
            self.board, self.layers, self.window, {"Edge.Cuts": "#ffffff"},
            theme="user", workdir=self.folder / "cli",
            origin=(self.bbox[0], self.bbox[1]),
        )
        for measured, expected in zip(
            self.extent(produced["Edge.Cuts"]), self.bbox
        ):
            self.assertAlmostEqual(measured, expected, places=2)

    def test_the_default_origin_is_the_outline_box(self):
        # A caller that states nothing gets the same register: kicad-cli crops
        # to the board outline, so that box is what its origin stands for.
        stated = render_seam.render_with_cli(
            self.board, self.layers, self.window, {"Edge.Cuts": "#ffffff"},
            theme="user", workdir=self.folder / "stated",
            origin=(self.bbox[0], self.bbox[1]),
        )["Edge.Cuts"]
        default = render_seam.render_with_cli(
            self.board, self.layers, self.window, {"Edge.Cuts": "#ffffff"},
            theme="user", workdir=self.folder / "default",
        )["Edge.Cuts"]
        self.assertEqual(self.extent(stated), self.extent(default))

    @unittest.skipUnless(LIBRARY, "no second backend to agree with")
    def test_both_backends_place_the_geometry_alike(self):
        import eedesigncore

        cli = render_seam.render_with_cli(
            self.board, self.layers, self.window, {"Edge.Cuts": "#ffffff"},
            theme="user", workdir=self.folder / "both",
            origin=(self.bbox[0], self.bbox[1]),
        )["Edge.Cuts"]
        library = render_seam.render_with_library(
            self.board, self.layers, self.window, {"Edge.Cuts": "#ffffff"},
            eedesigncore.render_board_svg, renderer_name="render_board_svg",
        )["Edge.Cuts"]
        for measured, expected in zip(self.extent(cli), self.extent(library)):
            # A kicad-cli page is sized in whole mils, so the two agree to
            # rather better than one of them (0.0254 mm).
            self.assertAlmostEqual(measured, expected, places=2)


@unittest.skipUnless(CLI, "no kicad-cli to draw holes and mask apertures with")
class ToolingHolesOnThePageTest(unittest.TestCase):
    """THE ACCEPTANCE: tooling holes visible on the panel page as it opens.

    Rendered for real, because what was wrong was what reached the page. Both
    halves of a hole are asserted - the drill mark on the Holes row, and the
    mask aperture the NPTH pad opens - at the coordinates the stage says the
    holes are at, on the bytes the panel page is served as.
    """

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        cls.config_path = with_tooling_holes(write_panel_project(folder))
        cls.session = PreviewSession(cls.config_path, folder / "work")
        cls.session.refresh()
        cls.session.start_panel()
        # The content arrives in the second phase. The page
        # is served with the library's picture - every graphic exact, and no
        # pad, track or zone - and the kicad-cli render replaces those layers
        # behind it. A drill mark and a mask aperture are pad content, so this
        # acceptance waits for the phase that draws them: the session's own
        # bounded wait, not a poll of its files.
        cls.upgraded = cls.session.wait_for_upgrades(120.0)
        cls.markup = cls.session.panel_page()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls._folder.cleanup()

    def holes(self) -> list:
        made = self.session.panel.outcome.panelize
        self.assertIsNotNone(made.holes, "this config asked for tooling holes")
        return list(made.holes.holes)

    def test_the_panel_carries_the_four_holes_the_config_asked_for(self):
        self.assertEqual(len(self.holes()), 4)

    def test_the_content_render_landed_before_any_of_this_was_read(self):
        # The rest of this class is about what the CONTENT phase draws, so a
        # failure there has to be about the drawing and not about the wait.
        self.assertTrue(self.upgraded, "the content render never finished")
        self.assertEqual(self.session.panel.content, session_module.FULL)

    def test_every_hole_has_a_drill_mark_on_the_page(self):
        marks = placed(self.markup, holes_module.HOLES_LAYER,
                       min_radius=HOLE_DIAMETER / 2.0 - 0.001)
        self.assertTrue(marks, "the Holes row drew nothing at all")
        for hole in self.holes():
            near = [
                mark for mark in marks
                if abs(mark[0] - hole.at[0]) < 0.05
                and abs(mark[1] - hole.at[1]) < 0.05
            ]
            self.assertTrue(near, f"{hole.designator} has no drill mark")
            self.assertAlmostEqual(near[0][2], HOLE_DIAMETER / 2.0, places=2)

    def test_every_hole_has_its_mask_aperture_on_the_page(self):
        apertures = placed(self.markup, "F.Mask",
                           min_radius=HOLE_DIAMETER / 2.0 - 0.001)
        for hole in self.holes():
            near = [
                mark for mark in apertures
                if abs(mark[0] - hole.at[0]) < 0.05
                and abs(mark[1] - hole.at[1]) < 0.05
            ]
            self.assertTrue(near, f"{hole.designator} has no mask aperture")

    def test_the_layers_that_carry_them_are_on_when_the_page_opens(self):
        rows = {row["canonical"]: row for row in baked_layers(self.markup)}
        self.assertTrue(rows[holes_module.HOLES_LAYER]["start"])
        self.assertTrue(rows[holes_module.HOLES_LAYER]["top"])
        self.assertTrue(rows["F.Mask"]["start"])
        self.assertTrue(rows["B.Mask"]["start"])

    def test_the_copper_layers_are_drawn_with_their_content(self):
        # The reason the renderer is kicad-cli: a page that lists F.Cu and
        # draws nothing on it is a worse defect than the one being fixed.
        drawn = Path(self.session.panel.svg_by_layer["F.Mask"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("<circle", drawn)

    def test_the_page_owes_no_notice_about_a_renderer_that_drew_it_all(self):
        self.assertEqual(render_seam.content_notice(
            render_seam.preview_backend()), "")
        self.assertNotIn("drawn EMPTY", self.markup)


class PanelPageStructureTest(unittest.TestCase):
    """The page's shape either way - built, and not built at all."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.session = PreviewSession(self.config_path, self.folder / "work",
                                      render=stub_render)
        self.session.refresh()

    def tearDown(self):
        self._folder.cleanup()

    def test_the_empty_frame_lists_the_same_rows_a_build_will_fill(self):
        # A build that changes the page's STRUCTURE is a build that reloads it.
        before = [
            row["canonical"] for row in baked_layers(self.session.panel_page())
        ]
        self.session.start_panel()
        after = [
            row["canonical"] for row in baked_layers(self.session.panel_page())
        ]
        self.assertEqual(before, after)

    def test_the_empty_frame_carries_no_proposal_row(self):
        # The candidate overlay is step 1's, and there is nothing to propose on
        # a panel.
        listed = [
            row["canonical"] for row in baked_layers(self.session.panel_page())
        ]
        self.assertNotIn(prep.PROPOSAL_LAYER, listed)

    def test_a_page_built_over_a_frame_keeps_the_holes_row(self):
        self.session.start_panel()
        page = self.session.panel_page()
        self.assertIn(holes_module.HOLES_LAYER,
                      [row["canonical"] for row in baked_layers(page)])
        self.assertIn(holes_module.HOLES_LAYER, stacked(page))

    def test_the_page_says_nothing_retired_and_no_bare_layer_name(self):
        self.session.start_panel()
        page = self.session.panel_page()
        self.assertEqual(page_panel.__name__, "eedesignpanels.preview.page_panel")
        from eedesignpanels.preview import naming

        self.assertEqual(naming.retired_words(page), [])


if __name__ == "__main__":
    unittest.main()
