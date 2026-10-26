"""Three things the preview says with COLOUR, and all three are fenced on bytes.

**RED marks a toolpath that would damage the board.** Some check
outcomes do not merely warn: they say the path that was computed would destroy
the board it is meant to free. `checks.toolpath_alarms` is the one place that
set is named, and when it is non-empty the preview paints the cutter path in a
theme-INDEPENDENT alarm colour - render and layer-panel swatch together, because
a picture that shouts and a swatch that does not is a picture the reader argues
with. An ordinary run is theme-derived and untouched by any of this.

**AND RED SURVIVES THE NEXT PAINT.** kicad-cli takes no per-layer colour, so
the content render that lands a second after the fast picture draws the
theme's own colour. An alarm colour set inside one computation would be
painted over by that render. The alarm state lives on the SESSION
(`preview.layer_alarms`) and every render is recoloured on the way out, so the
cases below drive a two-phase session through that background rebuild and
read the emitted page bytes afterwards.

**Alpha is applied ONCE, on the stacked layer.** The theme gives the cutter path
an alpha of 0.702; handed to a renderer as `rgba` it is applied per ITEM, so two
subsegments of one path that overlap composite twice and the overlap reads
darker than the rest of the same layer. So the renderer is handed OPAQUE
colours and the page sets the layer group's opacity - which is how the frozen
review pages draw it - and the assertions
here are on the emitted document: no per-element alpha anywhere in the stack,
the layer's own alpha stated once, and still 0.702.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eedesignpanels import checks, cuttability, prep
from eedesignpanels.preview import layer_alarms, page_served
from eedesignpanels.preview.session import PreviewSession
from eedesignpanels.review import overlay, theme as theme_module
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import Corner
from test_preview import write_project
from test_preview_journal import js_function, run_js
from test_vscore import scoreable_board

# What the fixture's tabs allow, and a bit that does not fit through it.
TAB_CEILING = 0.8923
TOO_WIDE = 1.2

# A theme with the alpha the real one carries on the cutter path, so the test
# does not depend on the colours this machine happens to have installed.
THEME = theme_module.Theme(
    name="test",
    colors={
        "edge_cuts": "rgb(208, 210, 205)",
        "user_8": "rgb(255, 0, 0)",
        "user_36": "rgba(194, 194, 194, 0.702)",
        "user_38": "rgb(0, 200, 100)",
    },
    background="rgb(0, 16, 35)",
)


def color_render(board, layers, viewbox_mm, colors, theme=None, workdir=None,
                 content=False, origin=None):
    """A renderer that PAINTS with the colours it was given, as the library does.

    `test_preview.stub_render` ignores them, which is right for everything that
    is about layout; nothing but this can say what colour reached the renderer.
    """
    x, y, width, height = viewbox_mm
    return {
        layer.canonical_name: (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.4f}mm" height="{height:.4f}mm" '
            f'viewBox="{x:.6f} {y:.6f} {width:.6f} {height:.6f}">'
            f'<path d="M{x:.4f} {y:.4f} L{x + width:.4f} {y + height:.4f}" '
            f'stroke="{colors.get(layer.canonical_name, "none")}"/>'
            f'<path d="M{x:.4f} {y + height:.4f} L{x + width:.4f} {y:.4f}" '
            f'stroke="{colors.get(layer.canonical_name, "none")}"/></svg>'
        )
        for layer in layers
    }


def project(folder: Path, cutter: float) -> Path:
    """The preview fixture with a stated cutter, and nothing else changed."""
    config_path = write_project(folder, scoreable_board())
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["prep"]["cutter_diameter"] = cutter
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return config_path


# One feature the declared cutter cannot make, and one it can. Built here rather
# than measured, because what is under test is the alarm rule and not the
# geometry that reaches a verdict - `test_cuttability.py` pins that.
UNCUTTABLE = cuttability.Job(
    index=1, kind=cuttability.HOLE, loop=(), rides_inside=True,
    verdict=cuttability.NOT_MADE, reason="a 2.0000 mm cutter does not fit it",
    bbox=(9.35, 9.35, 10.65, 10.65), number=1,
)
CUTTABLE = UNCUTTABLE._replace(verdict=cuttability.CUT, reason="")
# ...AND ONE THAT IS CUT WITH A CORNER THE BIT CANNOT REACH. Cut, so it carries
# no reason; red all the same, because what leaves the router is a rounded
# corner where a sharp one was drawn.
ROUNDED = CUTTABLE._replace(rounded=(Corner(
    at=(10.0, 7.0), turn_deg=90.0, radius_mm=1.0, sources=("a", "b"),
),))


def result_like(**fields):
    """The least of a `PrepResult` the alarm rule reads."""
    return SimpleNamespace(**{
        "cutter_diameter_mm": 0.889,
        "tool_max_mm": TAB_CEILING,
        "rout_through": (),
        "routed_scores": (),
        "protected_tabs": (),
        "junctions": (),
        "bridges": (),
        "jobs": (),
        **fields,
    })


class AlarmSetTest(unittest.TestCase):
    """WHICH outcomes invalidate a toolpath. The set is small on purpose."""

    def test_an_ordinary_result_raises_none(self):
        self.assertEqual(checks.toolpath_alarms(result_like()), ())

    def test_a_cutter_over_the_tab_ceiling_does(self):
        alarms = checks.toolpath_alarms(result_like(cutter_diameter_mm=TOO_WIDE))
        self.assertEqual(len(alarms), 1)
        self.assertIn("exceeds", alarms[0])
        self.assertIn("1.2000", alarms[0])

    def test_a_cutter_exactly_at_the_ceiling_does_not(self):
        self.assertEqual(
            checks.toolpath_alarms(result_like(cutter_diameter_mm=TAB_CEILING)),
            (),
        )

    def test_a_board_whose_tabs_declare_no_ceiling_cannot_exceed_one(self):
        self.assertEqual(
            checks.toolpath_alarms(
                result_like(tool_max_mm=None, cutter_diameter_mm=9.0)
            ),
            (),
        )

    def test_a_cut_running_through_a_tab_does(self):
        alarms = checks.toolpath_alarms(result_like(
            rout_through=(SimpleNamespace(label="tab TOP"),)
        ))
        self.assertEqual(alarms, ("the finished cut runs through tab tab TOP",))

    def test_a_cut_running_along_a_scored_edge_does(self):
        alarms = checks.toolpath_alarms(result_like(
            routed_scores=(SimpleNamespace(label="v-score @ left"),)
        ))
        self.assertEqual(alarms,
                         ("the finished cut runs along v-score @ left",))

    def test_a_tab_narrower_than_the_minimum_tab_does_NOT(self):
        # The design wins over the setting and the tab stands - a report about a
        # valid path, which is exactly what the alarm colour is not for.
        self.assertEqual(
            checks.toolpath_alarms(result_like(
                protected_tabs=(SimpleNamespace(label="tab TOP"),)
            )),
            (),
        )

    def test_a_junction_that_falls_short_does_NOT(self):
        self.assertEqual(
            checks.toolpath_alarms(result_like(
                junctions=(SimpleNamespace(reached=False),)
            )),
            (),
        )

    def test_the_order_is_fixed_so_one_result_always_reads_the_same(self):
        result = result_like(
            cutter_diameter_mm=TOO_WIDE,
            rout_through=(SimpleNamespace(label="tab A"),),
            routed_scores=(SimpleNamespace(label="score B"),),
        )
        self.assertEqual(
            [alarm.split()[0] for alarm in checks.toolpath_alarms(result)],
            ["the", "the", "the"],
        )
        self.assertEqual(checks.toolpath_alarms(result),
                         checks.toolpath_alarms(result))


class OpaqueColorTest(unittest.TestCase):
    """Dropping the alpha channel, and leaving everything else alone."""

    def test_an_rgba_colour_loses_its_alpha(self):
        self.assertEqual(
            theme_module.opaque("rgba(194, 194, 194, 0.702)"),
            "rgb(194, 194, 194)",
        )

    def test_an_rgb_colour_is_returned_as_it_was(self):
        self.assertEqual(
            theme_module.opaque("rgb(208, 210, 205)"), "rgb(208, 210, 205)"
        )

    def test_spacing_and_case_do_not_matter(self):
        self.assertEqual(
            theme_module.opaque("RGBA( 1,2 , 3 ,0.5 )"), "rgb(1, 2, 3)"
        )

    def test_anything_else_is_left_exactly_as_it_is(self):
        for value in ("#c2c2c2", "none", "", None, 7):
            self.assertEqual(theme_module.opaque(value), value)

    def test_the_alarm_colour_carries_no_alpha_of_its_own(self):
        self.assertEqual(
            theme_module.opaque(theme_module.ALARM_COLOR),
            theme_module.ALARM_COLOR,
        )

    def test_the_themes_own_alpha_is_untouched_by_it(self):
        # The swatch and the viewer still read the theme, not the colour the
        # renderer was handed.
        self.assertEqual(THEME.layer_alpha(route_toolpath.TOOLPATH_LAYER), 0.702)


class SessionColorTest(unittest.TestCase):
    """What the session paints with, and what the page it emits then says."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.painted = {}
        self._theme = mock.patch.object(
            theme_module, "load_theme", return_value=THEME
        )
        self._theme.start()

    def tearDown(self):
        self._theme.stop()
        self._folder.cleanup()

    def render(self, board, layers, viewbox_mm, colors, theme=None,
               workdir=None, content=False, origin=None):
        self.painted.update(colors)
        return color_render(board, layers, viewbox_mm, colors, theme, workdir)

    def session(self, cutter: float) -> PreviewSession:
        config_path = project(self.folder, cutter)
        session = PreviewSession(config_path, self.folder / "work",
                                 render=self.render)
        session.refresh()
        self.assertEqual(session.error, "")
        return session

    def layer_entry(self, markup: str, canonical: str) -> dict:
        """One row of the layer table the emitted page hands its viewer."""
        head = markup.split("const LAYERS = ", 1)[1]
        table = json.loads(head[:head.index("\n")].rstrip(";"))
        return next(row for row in table if row["canonical"] == canonical)

    # -- the ordinary board ------------------------------------------------

    def test_an_ordinary_run_paints_the_theme_colour(self):
        session = self.session(0.889)
        self.assertEqual(session.view.alarms, ())
        self.assertEqual(self.painted[route_toolpath.TOOLPATH_LAYER],
                         "rgb(194, 194, 194)")

    def test_an_ordinary_run_keeps_the_theme_swatch(self):
        session = self.session(0.889)
        self.assertEqual(
            session.view.theme.layer_color(route_toolpath.TOOLPATH_LAYER),
            "rgba(194, 194, 194, 0.702)",
        )
        # The alarm colour is a constant every page carries, because a PAINT
        # applies the alarm from the session's state. So the fence is not
        # "the string appears nowhere on the page" - it is no red swatch, no
        # red paint, and no alarm state.
        markup = session.page()
        self.assertNotIn(f"background:{theme_module.ALARM_COLOR}", markup)
        self.assertNotIn(f'stroke="{theme_module.ALARM_COLOR}"', markup)
        self.assertEqual(session.fragment()["alarm_layers"], [])

    # -- the alarm ---------------------------------------------------------

    def test_a_cutter_over_the_ceiling_paints_the_toolpath_red(self):
        session = self.session(TOO_WIDE)
        self.assertTrue(session.view.alarms)
        self.assertEqual(self.painted[route_toolpath.TOOLPATH_LAYER],
                         theme_module.ALARM_COLOR)

    def test_the_swatch_matches_the_render(self):
        session = self.session(TOO_WIDE)
        markup = session.page()
        self.assertEqual(
            session.view.theme.layer_color(route_toolpath.TOOLPATH_LAYER),
            theme_module.ALARM_COLOR,
        )
        # ...and it is on the page, in the row's own swatch.
        self.assertIn(f"background:{theme_module.ALARM_COLOR}", markup)

    def test_only_the_toolpath_layer_turns_red(self):
        self.session(TOO_WIDE)
        for name, color in self.painted.items():
            if name != route_toolpath.TOOLPATH_LAYER:
                self.assertNotEqual(color, theme_module.ALARM_COLOR)

    def test_the_alarm_is_at_full_strength_not_at_the_themes_alpha(self):
        session = self.session(TOO_WIDE)
        entry = self.layer_entry(session.page(), route_toolpath.TOOLPATH_LAYER)
        self.assertEqual(entry["alpha"], 1.0)

    def test_the_page_says_why_it_is_red(self):
        session = self.session(TOO_WIDE)
        state = json.loads(
            session.page().split("let PB = ", 1)[1].split("\n")[0].rstrip(";")
        )
        self.assertTrue(state["alarms"])
        self.assertIn("exceeds", state["alarms"][0])
        self.assertEqual(session.fragment()["alarms"], state["alarms"])

    def test_an_ordinary_run_emits_no_alarm_state(self):
        session = self.session(0.889)
        state = json.loads(
            session.page().split("let PB = ", 1)[1].split("\n")[0].rstrip(";")
        )
        self.assertEqual(state["alarms"], [])

    # -- one alpha, once ---------------------------------------------------

    def test_the_renderer_is_handed_no_alpha_at_all(self):
        self.session(0.889)
        for name, color in self.painted.items():
            with self.subTest(layer=name):
                self.assertNotIn("rgba", color)

    def test_the_stack_the_page_emits_carries_no_per_element_alpha(self):
        # The overlap darkening, fenced where it was visible: every drawn item
        # is opaque, so two of them on one layer cannot composite twice.
        markup = self.session(0.889).page()
        stack = markup.split('<div class=viewport', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("rgba(", stack)
        self.assertNotIn("fill-opacity", stack)
        self.assertNotIn("stroke-opacity", stack)

    def test_the_layer_alpha_is_stated_once_and_is_still_the_themes(self):
        markup = self.session(0.889).page()
        entry = self.layer_entry(markup, route_toolpath.TOOLPATH_LAYER)
        self.assertEqual(entry["alpha"], 0.702)
        # Once: the layer table is the only place the number appears, and the
        # viewer applies it to the whole stacked layer element.
        self.assertEqual(markup.count('"alpha": 0.702'), 1)

    def test_every_other_layer_keeps_its_own_alpha(self):
        markup = self.session(0.889).page()
        for name in (route_toolpath.OUTLINE_LAYER,
                     route_toolpath.STOP_LINE_LAYER,
                     route_toolpath.SCORE_LAYER):
            with self.subTest(layer=name):
                self.assertEqual(self.layer_entry(markup, name)["alpha"], 1.0)


# What the theme paints the cutter path, opaque, as a renderer is handed it.
THEME_TOOLPATH = "rgb(194, 194, 194)"


def layer_markup(markup: str, canonical: str) -> str:
    """One layer's embedded document, cut out of an emitted page by its name.

    The assertion has to be about THAT layer: a page carries fifty documents and
    a colour found anywhere in it says nothing about which one is red.
    """
    at = markup.index(f'data-layer="{canonical}"')
    opened = markup.rindex("<svg", 0, at)
    closed = markup.index("</svg>", at)
    return markup[opened:closed]


class AlarmedSetTest(unittest.TestCase):
    """The session's own fact: which layers are alarmed, and why."""

    def test_an_ordinary_result_alarms_no_layer(self):
        self.assertEqual(layer_alarms.of_prep_result(result_like()), {})

    def test_a_cutter_over_the_ceiling_alarms_the_toolpath_layer(self):
        alarmed = layer_alarms.of_prep_result(
            result_like(cutter_diameter_mm=TOO_WIDE)
        )
        self.assertEqual(list(alarmed), [route_toolpath.TOOLPATH_LAYER])
        self.assertIn(
            "exceeds", alarmed[route_toolpath.TOOLPATH_LAYER][0]
        )

    def test_it_carries_every_reason_the_check_set_names(self):
        alarmed = layer_alarms.of_prep_result(result_like(
            cutter_diameter_mm=TOO_WIDE,
            rout_through=(SimpleNamespace(label="tab A"),),
        ))
        self.assertEqual(
            alarmed[route_toolpath.TOOLPATH_LAYER],
            checks.toolpath_alarms(result_like(
                cutter_diameter_mm=TOO_WIDE,
                rout_through=(SimpleNamespace(label="tab A"),),
            )),
        )

    def test_no_result_at_all_alarms_nothing(self):
        self.assertEqual(layer_alarms.of_prep_result(None), {})

    def test_an_uncuttable_feature_alarms_its_own_layer(self):
        # A feature that cannot be cut is reported, and its highlight is red.
        # Its own layer, so the red lands on the feature and not on the whole
        # board outline.
        alarmed = layer_alarms.of_prep_result(result_like(jobs=(UNCUTTABLE,)))
        self.assertEqual(list(alarmed), [prep.UNCUTTABLE_LAYER])
        self.assertIn("cannot be cut", alarmed[prep.UNCUTTABLE_LAYER][0])

    def test_a_feature_that_cuts_alarms_nothing(self):
        self.assertEqual(
            layer_alarms.of_prep_result(result_like(jobs=(CUTTABLE,))), {})

    def test_a_corner_the_bit_cannot_reach_alarms_the_same_layer(self):
        # An uncuttable feature and a rounded corner go through one alarm
        # state, on one layer.
        alarmed = layer_alarms.of_prep_result(result_like(jobs=(ROUNDED,)))
        self.assertEqual(list(alarmed), [prep.UNCUTTABLE_LAYER])
        said = alarmed[prep.UNCUTTABLE_LAYER][0]
        self.assertIn("(10.0000, 7.0000) mm", said)
        self.assertIn("rounded to 1.0000 mm", said)
        # The sentence reads "cutter too large to cut feature at (X,Y) - it
        # comes out rounded to R mm", which names the radius the part gets.
        self.assertIn("cutter too large to cut feature at", said)

    def test_the_two_alarms_are_two_layers_and_two_headlines(self):
        alarmed = layer_alarms.of_prep_result(result_like(
            cutter_diameter_mm=TOO_WIDE, jobs=(UNCUTTABLE,)))
        self.assertEqual(
            sorted(alarmed),
            sorted([route_toolpath.TOOLPATH_LAYER, prep.UNCUTTABLE_LAYER]))
        said = {entry["layer"]: entry["says"]
                for entry in layer_alarms.status(alarmed)}
        self.assertIn("MUST NOT BE CUT", said[route_toolpath.TOOLPATH_LAYER])
        self.assertIn("CANNOT BE CUT", said[prep.UNCUTTABLE_LAYER])

    def test_the_marks_are_drawn_red_over_the_features(self):
        markup = overlay.uncuttable_svg(
            [UNCUTTABLE], (0.0, 0.0), overlay.ViewBox(0.0, 0.0, 40.0, 20.0),
            2.0,
        )
        self.assertIn(theme_module.ALARM_COLOR, markup)
        self.assertIn("1.30 x 1.30 mm", markup)
        self.assertEqual(markup.count("<rect"), 1)

    def test_a_corner_is_marked_with_the_tool_and_no_box(self):
        markup = overlay.uncuttable_svg(
            [ROUNDED], (0.0, 0.0), overlay.ViewBox(0.0, 0.0, 40.0, 20.0), 2.0)
        self.assertIn(theme_module.ALARM_COLOR, markup)
        self.assertEqual(markup.count("<circle"), 1)
        # The feature IS cut, so nothing boxes it and nothing states its size.
        self.assertNotIn("<rect", markup)
        self.assertNotIn("mm<", markup)

    def test_a_board_with_nothing_uncuttable_draws_an_empty_document(self):
        markup = overlay.uncuttable_svg(
            [], (0.0, 0.0), overlay.ViewBox(0.0, 0.0, 40.0, 20.0), 2.0)
        self.assertNotIn("<rect", markup)
        self.assertIn("<svg", markup)


class RedLayerOpensOnTest(unittest.TestCase):
    """The red layer is switched on when the page opens.

    `start_visible` is built from the page-drawn annotation layers. The red
    layer is one of them, so what the check marked is visible without a search.
    """

    def test_a_board_with_neither_overlay_gets_neither_row(self):
        self.assertEqual(page_served.annotation_layers({"Edge.Cuts": "x"}), ())
        self.assertEqual(page_served.annotation_layers(None), ())

    def test_the_red_layer_is_named_when_the_view_carries_it(self):
        self.assertEqual(
            page_served.annotation_layers({prep.UNCUTTABLE_LAYER: "x"}),
            (prep.UNCUTTABLE_LAYER,),
        )

    def test_the_red_layer_stacks_after_the_proposals(self):
        # Nothing paints over the red: it is the last annotation in the list,
        # and the list is the stacking order.
        self.assertEqual(
            page_served.annotation_layers({
                prep.UNCUTTABLE_LAYER: "x", prep.PROPOSAL_LAYER: "y",
            }),
            (prep.PROPOSAL_LAYER, prep.UNCUTTABLE_LAYER),
        )

    def test_the_frozen_page_and_the_live_page_name_the_same_two(self):
        # One list for both: `prep.review` opens whatever it called annotation,
        # so the two pages cannot disagree about the red layer.
        self.assertEqual(
            page_served.PAGE_ANNOTATIONS,
            (prep.PROPOSAL_LAYER, prep.UNCUTTABLE_LAYER),
        )

    def test_a_cut_feature_with_a_rounded_corner_is_still_drawn(self):
        # What `prep.uncuttable_overlay` now hands the drawing code. It used to
        # hand it the jobs that were NOT made, so a board whose only red fact
        # was a corner earned the layer and then got an empty document.
        drawn = cuttability.marked([ROUNDED])
        self.assertEqual(len(drawn), 1)
        markup = overlay.uncuttable_svg(
            drawn, (0.0, 0.0), overlay.ViewBox(0.0, 0.0, 40.0, 20.0), 2.0)
        self.assertEqual(markup.count("<circle"), 1)

    def test_a_theme_is_substituted_only_for_an_alarmed_layer(self):
        themed = layer_alarms.themed(
            THEME, {route_toolpath.TOOLPATH_LAYER: ("because",)}
        )
        self.assertEqual(
            themed.layer_color(route_toolpath.TOOLPATH_LAYER),
            theme_module.ALARM_COLOR,
        )
        self.assertEqual(themed.layer_color("Edge.Cuts"),
                         THEME.layer_color("Edge.Cuts"))

    def test_an_empty_set_returns_the_theme_itself(self):
        self.assertIs(layer_alarms.themed(THEME, {}), THEME)

    def test_the_status_line_names_the_layer_as_a_reader_calls_it(self):
        said = layer_alarms.status(
            {route_toolpath.TOOLPATH_LAYER: ("the cutter is too wide",)},
            {route_toolpath.TOOLPATH_LAYER: "User.36-Route"},
        )
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["layer"], route_toolpath.TOOLPATH_LAYER)
        self.assertEqual(said[0]["display"], "User.36-Route")
        self.assertEqual(
            said[0]["says"],
            "User.36-Route MUST NOT BE CUT - the cutter is too wide",
        )

    def test_an_empty_set_says_nothing(self):
        self.assertEqual(layer_alarms.status({}, {}), [])


class RepaintedDocumentTest(unittest.TestCase):
    """Forcing the alarm colour onto markup, whichever backend drew it."""

    def test_a_style_paint_is_forced(self):
        self.assertEqual(
            layer_alarms.repainted('<g style="stroke:#c2c2c2;fill:#c2c2c2">'),
            f'<g style="stroke:{theme_module.ALARM_COLOR};'
            f'fill:{theme_module.ALARM_COLOR}">',
        )

    def test_an_attribute_paint_is_forced(self):
        self.assertEqual(
            layer_alarms.repainted('<path stroke="rgb(1, 2, 3)"/>'),
            f'<path stroke="{theme_module.ALARM_COLOR}"/>',
        )

    def test_a_paint_that_draws_nothing_is_left_alone(self):
        # Turning `fill:none` red would fill every stroked outline solid.
        for text in ('<g style="fill:none;stroke:#ccc">', '<path fill="none"/>'):
            with self.subTest(markup=text):
                self.assertNotIn("fill:" + theme_module.ALARM_COLOR,
                                 layer_alarms.repainted(text))
                self.assertNotIn(f'fill="{theme_module.ALARM_COLOR}"',
                                 layer_alarms.repainted(text))

    def test_a_width_a_linecap_and_an_opacity_are_not_paints(self):
        text = ('<path stroke-width="0.1" stroke-linecap="round" '
                'fill-opacity="0.5" d="M0 0 L1 1"/>')
        self.assertEqual(layer_alarms.repainted(text), text)

    def test_the_geometry_is_untouched(self):
        text = '<path d="M0 0 L1 1" style="stroke:#c2c2c2"/>'
        self.assertIn('d="M0 0 L1 1"', layer_alarms.repainted(text))

    def test_only_an_alarmed_layers_document_is_recoloured(self):
        produced = {
            route_toolpath.TOOLPATH_LAYER: '<g style="stroke:#c2c2c2"/>',
            "Edge.Cuts": '<g style="stroke:#c2c2c2"/>',
        }
        out = layer_alarms.documents_repainted(
            produced, {route_toolpath.TOOLPATH_LAYER: ("because",)}
        )
        self.assertIn(theme_module.ALARM_COLOR,
                      out[route_toolpath.TOOLPATH_LAYER])
        self.assertEqual(out["Edge.Cuts"], produced["Edge.Cuts"])

    def test_no_alarm_returns_the_documents_as_they_are(self):
        produced = {"Edge.Cuts": '<g style="stroke:#c2c2c2"/>'}
        self.assertIs(layer_alarms.documents_repainted(produced, {}), produced)


class TwoBackendRender:
    """The two phases as they really differ.

    The FAST phase paints the colours it is handed, as the library renderer
    does. The CONTENT phase paints the THEME's colour whatever it was handed,
    because kicad-cli has no per-layer colour argument and renders from
    `--theme` alone (`preview.render_seam`). A stub that honoured the colours
    in both phases could not show whether the red survives the second one.
    """

    def __init__(self):
        self.phases = []

    def __call__(self, board, layers, viewbox_mm, colors, theme=None,
                 workdir=None, content=False, origin=None, board_lock=None):
        self.phases.append(bool(content))
        painted = (
            # Opaque, because a kicad-cli export is: the alpha is the page's to
            # apply to the whole stacked layer, once (see `theme.opaque`).
            {layer.canonical_name: theme_module.opaque(
                THEME.layer_color(layer.canonical_name)
            ) for layer in layers}
            if content else colors
        )
        return color_render(board, layers, viewbox_mm, painted, theme, workdir)


class RedSurvivesTheRepaintTest(unittest.TestCase):
    """The red alarm, driven through the session and read off the bytes.

    A warning that erases itself is worse than no warning, because the user
    has already looked away by then. So the test drives the whole sequence -
    set a diameter the tool refuses, let the background rebuild land, and
    read the page it emits afterwards.
    """

    WAIT = 30.0

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self._theme = mock.patch.object(
            theme_module, "load_theme", return_value=THEME
        )
        self._theme.start()
        self.render = TwoBackendRender()
        self.session = PreviewSession(
            project(self.folder, TOO_WIDE), self.folder / "work",
            render=self.render, two_phase=True,
        )
        self.session.refresh()
        self.assertEqual(self.session.error, "")

    def tearDown(self):
        self.session.close()
        self._theme.stop()
        self._folder.cleanup()

    def toolpath(self, markup: str = "") -> str:
        """The cutter path's own document, out of the page the session emits."""
        return layer_markup(markup or self.session.page(),
                            route_toolpath.TOOLPATH_LAYER)

    def alarm_box(self, markup: str = "") -> str:
        """The status the page states in the corner of the picture."""
        text = markup or self.session.page()
        at = text.index("id=pb-alarm")
        return text[text.rindex("<div", 0, at):text.index("</div>", at)]

    # -- before the background task ----------------------------------------

    def test_the_first_paint_is_red(self):
        self.assertTrue(self.session.layer_alarms)
        self.assertIn(theme_module.ALARM_COLOR, self.toolpath())

    # -- and after it ------------------------------------------------------

    def test_the_content_render_cannot_put_the_theme_colour_back(self):
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        # The background task ran, and it is the one that used to erase this.
        self.assertIn(True, self.render.phases)
        drawn = self.toolpath()
        self.assertIn(theme_module.ALARM_COLOR, drawn)
        self.assertNotIn(THEME_TOOLPATH, drawn)

    def test_and_the_page_still_says_why(self):
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        markup = self.session.page()
        said = self.alarm_box(markup)
        self.assertIn(layer_alarms.ALARM_HEADLINE, said)
        self.assertIn("exceeds", said)
        self.assertNotIn("hidden", said)

    def test_the_fragment_restates_it_on_every_paint(self):
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        said = self.session.fragment()["alarm_layers"]
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["layer"], route_toolpath.TOOLPATH_LAYER)
        self.assertIn("exceeds", said[0]["says"])
        # A fragment that changed nothing says it again, because the page
        # applies this state rather than remembering it.
        self.assertEqual(self.session.fragment()["alarm_layers"], said)

    def test_only_the_alarmed_layer_is_red_after_the_repaint(self):
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        markup = self.session.page()
        for name in (route_toolpath.OUTLINE_LAYER,
                     route_toolpath.STOP_LINE_LAYER):
            with self.subTest(layer=name):
                self.assertNotIn(theme_module.ALARM_COLOR,
                                 layer_markup(markup, name))

    # -- and it clears when its cause clears, not when a rebuild finishes --

    def test_a_diameter_that_works_clears_it(self):
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        self.session.set_param({"cutter_diameter": 0.889})
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        self.assertEqual(self.session.layer_alarms, {})
        drawn = self.toolpath()
        self.assertNotIn(theme_module.ALARM_COLOR, drawn)
        self.assertIn(THEME_TOOLPATH, drawn)

    def test_the_status_goes_with_it(self):
        self.session.set_param({"cutter_diameter": 0.889})
        self.assertTrue(self.session.wait_for_upgrades(self.WAIT))
        self.assertEqual(self.session.fragment()["alarm_layers"], [])
        self.assertIn("hidden", self.alarm_box())

    def test_the_row_is_told_the_colour_to_go_back_to(self):
        # The swatch is a server-written inline style, so a page told only the
        # alarm colour could turn a row red and never turn it back.
        state = json.loads(
            self.session.page().split("let PB = ", 1)[1]
            .split(chr(10))[0].rstrip(";")
        )
        self.assertEqual(state["alarmColor"], theme_module.ALARM_COLOR)
        self.assertEqual(
            state["layerColors"][route_toolpath.TOOLPATH_LAYER],
            THEME.layer_color(route_toolpath.TOOLPATH_LAYER),
        )
        self.assertEqual(state["alarm_layers"][0]["layer"],
                         route_toolpath.TOOLPATH_LAYER)


# ---------------------------------------------------------------------------
# the page's own half, RUN
# ---------------------------------------------------------------------------

ALARM_HARNESS = r"""
%(functions)s
const BOX = {lines: [], hidden: true, textContent: '',
             appendChild(node) { BOX.lines.push(node.textContent); }};
Object.defineProperty(BOX, 'textContent', {
  set(value) { if (!value) BOX.lines.length = 0; }, get() { return ''; }});
function row(index, color) {
  return {dataset: {i: String(index)}, marked: false,
          swatch: {style: {background: color}},
          classList: {toggle(name, state) { this.owner.marked = !!state; }},
          querySelector(what) { return what === '.lp-sw' ? this.swatch : null; }};
}
const ROWS = [row(0, 'rgba(194, 194, 194, 0.702)'), row(1, 'rgb(0, 200, 100)')];
ROWS.forEach(r => { r.classList.owner = r; });
globalThis.LAYERS = [{canonical: 'User.36'}, {canonical: 'User.38'}];
globalThis.PB = {alarmColor: %(color)s, layerColors: %(colors)s};
globalThis.document = {
  getElementById: id => id === 'pb-alarm' ? BOX : null,
  createElement: () => ({textContent: ''}),
  querySelectorAll: () => ROWS,
};
const out = {};
out.said = drawLayerAlarms(%(fragment)s);
out.lines = BOX.lines;
out.hidden = BOX.hidden;
out.rows = ROWS.map(r => ({marked: r.marked,
                          swatch: r.swatch.style.background}));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientAlarmPaintTest(unittest.TestCase):
    """The paint, RUN: it APPLIES the state and it puts it back.

    The alarm state comes from the session, so what is fenced here is that
    this function states what the fragment says and nothing else - including
    when the fragment says nothing.
    """

    THEME_SWATCH = "rgba(194, 194, 194, 0.702)"

    def drawn(self, fragment: dict) -> dict:
        return run_js(ALARM_HARNESS % {
            "functions": js_function(page_served.CONTROLS_JS,
                                     "drawLayerAlarms"),
            "fragment": json.dumps(fragment),
            "color": json.dumps(theme_module.ALARM_COLOR),
            "colors": json.dumps({"User.36": self.THEME_SWATCH,
                                  "User.38": "rgb(0, 200, 100)"}),
        })

    def alarmed(self, layer: str = "User.36") -> dict:
        return {"alarm_layers": [{
            "layer": layer, "display": layer,
            "says": f"{layer} MUST NOT BE CUT - the cutter is too wide",
        }]}

    def test_the_reason_is_stated_where_the_picture_is_red(self):
        out = self.drawn(self.alarmed())
        self.assertEqual(out["lines"],
                         ["User.36 MUST NOT BE CUT - the cutter is too wide"])
        self.assertFalse(out["hidden"])

    def test_the_alarmed_row_is_marked_and_its_swatch_turns_red(self):
        out = self.drawn(self.alarmed())
        self.assertTrue(out["rows"][0]["marked"])
        self.assertEqual(out["rows"][0]["swatch"], theme_module.ALARM_COLOR)

    def test_no_other_row_moves(self):
        out = self.drawn(self.alarmed())
        self.assertFalse(out["rows"][1]["marked"])
        self.assertEqual(out["rows"][1]["swatch"], "rgb(0, 200, 100)")

    def test_an_empty_state_puts_the_row_back_to_the_theme(self):
        out = self.drawn({"alarm_layers": []})
        self.assertEqual(out["lines"], [])
        self.assertTrue(out["hidden"])
        self.assertFalse(out["rows"][0]["marked"])
        self.assertEqual(out["rows"][0]["swatch"], self.THEME_SWATCH)

    def test_a_fragment_that_says_nothing_clears_it_too(self):
        # Every paint applies the state, and a fragment with no statement in it
        # is a board with no alarm - which is what a rebuild used to imply and
        # never said.
        out = self.drawn({})
        self.assertTrue(out["hidden"])
        self.assertFalse(out["rows"][0]["marked"])


class PageWiringTest(unittest.TestCase):
    """The served script paints the alarm on every fragment, not once."""

    def test_the_shared_half_calls_it_from_the_fragment_path(self):
        script = page_served.CONTROLS_JS
        for token in ("function drawLayerAlarms(fragment)",
                      "drawLayerAlarms(fragment);",
                      "drawLayerAlarms(PB);"):
            with self.subTest(token=token):
                self.assertIn(token, script)

    def test_it_is_called_before_the_ok_test(self):
        # A fragment that failed still has an alarm state to clear, and a call
        # after the early return would leave the last board's red on screen.
        after = page_served.CONTROLS_JS.split("drawAwaiting(fragment);", 1)[1]
        self.assertLess(after.index("drawLayerAlarms(fragment);"),
                        after.index("if (!fragment.ok)"))

    def test_the_status_carries_no_control(self):
        # A status is not a control and must never swallow a pan.
        rule = page_served.CONTROLS_CSS.split(".alarmnote{", 1)[1]
        self.assertIn("position:absolute", rule.split("}", 1)[0])
        self.assertIn("pointer-events:none", rule.split("}", 1)[0])



if __name__ == "__main__":
    unittest.main()
