"""The controls column: collapsible groups, and a footer that never scrolls.

The column is longer than the screen on any real board, so every group folds -
and the write actions come out of it entirely, into a footer under the
scrolling body, because the buttons a user reaches for after reading a long
column are the ones the user must not have to scroll back for.

Three things are asserted, in the three ways they can be got wrong:

* THE STRUCTURE, on the bytes both pages are served as. A group is a
  `data-group` container, a heading that folds it, and a `foldbody`; a
  heading bar carries a name only, because the switch inside the group
  already shows the on/off state; and the write buttons are in the footer and
  NOT in the scrolling body. A builder that says so is not a page that says
  so. A group may be served FOLDED (the settings import is), so what is
  fenced is that the container, the heading and the body agree about which
  way it is set - not that they are all open.
* THE FOLD LOGIC, RUN under node against a stated DOM and a stated store. "It
  remembers" is behaviour, and a string cannot tell a store that was written
  from one that threw - which is the case Chrome produces on an opaque origin
  and the case this has to survive.
* NOTHING WAS LOST. Every control that was on either page before this is still
  on it, by id and by label.
"""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from eedesignpanels.preview import page_panel, page_served
from eedesignpanels.preview.session import PreviewSession
from test_panel_page import controls_of, js_slice, write_panel_project
from test_preview import stub_render, write_project
from test_preview_journal import js_function, run_js

# Every control that was on the prep page before the column folded, by id.
PREP_CONTROLS = ("pb-cutter", "pb-past", "pb-scorew", "pb-mintab", "pb-apply",
                 "pb-save", "pb-reload", "pb-stop", "pb-note", "pb-report-btn")
# ...and on the panel page.
PANEL_CONTROLS = ("pb-columns", "pb-rows", "pb-gapx", "pb-gapy", "pb-rail",
                  "pb-rail-unlock", "pb-rail-top", "pb-rail-bottom",
                  "pb-rail-left", "pb-rail-right", "pb-holes-on", "pb-hole-d",
                  "pb-hole-place", "pb-copper-on", "pb-copper-clear",
                  "pb-scores-on", "pb-score-inter-col", "pb-score-inter-row",
                  "pb-score-rails-h", "pb-score-rails-v",
                  "pb-check", "pb-build", "pb-save", "pb-stop", "pb-note",
                  "pb-report-btn")
# The write buttons: they live in the footer on one page or the other, and
# not in the part of the column that scrolls.
FOOTER_BUTTONS = ("pb-save", "pb-stop", "pb-reload", "pb-build")

BODY = re.compile(r"<div class=body>(.*?)</div>\s*<div class=actions",
                  re.S)
ACTIONS = re.compile(r"<div class=actions .*?</div>\s*<div class=status", re.S)
GROUPS = re.compile(r"<(section|div)[^>]*data-group=(\S+?)[ >]")


def body_of(markup: str) -> str:
    """The part of the controls column that SCROLLS."""
    column = controls_of(markup)
    found = BODY.search(column)
    assert found, "the controls column has no scrolling body"
    return found.group(1)


def actions_of(markup: str) -> str:
    """The footer, between the body and the status line."""
    column = controls_of(markup)
    found = ACTIONS.search(column)
    assert found, "the controls column has no write-actions footer"
    return found.group(0)


class PageBytesTestCase(unittest.TestCase):
    """Both pages, served for real from a session over the stub renderer."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        (folder / "prep").mkdir()
        (folder / "panel").mkdir()
        cls.session = PreviewSession(
            write_project(folder / "prep"), folder / "prepwork",
            render=stub_render,
        )
        cls.session.refresh()
        cls.prep = cls.session.page()
        cls.panel_session = PreviewSession(
            write_panel_project(folder / "panel"), folder / "panelwork",
            render=stub_render,
        )
        cls.panel_session.refresh()
        cls.panel_session.start_panel()
        cls.panel = cls.panel_session.panel_page()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.panel_session.close()
        cls._folder.cleanup()

    def pages(self) -> dict:
        return {"prep": self.prep, "panel": self.panel}


class GroupStructureTest(PageBytesTestCase):
    """Every group on both pages is one shape, and the bytes say so."""

    def test_every_section_of_the_column_is_a_group_that_folds(self):
        for name, markup in self.pages().items():
            body = body_of(markup)
            sections = re.findall(r"<section[^>]*>", body)
            self.assertTrue(sections, f"{name} has no sections at all")
            for section in sections:
                with self.subTest(page=name, section=section):
                    self.assertIn("data-group=", section)
                    self.assertIn("data-collapsed=", section)

    def test_every_group_carries_a_heading_that_is_the_control(self):
        for name, markup in self.pages().items():
            body = body_of(markup)
            heads = re.findall(r"<h[34] class=fold[^>]*>", body)
            groups = GROUPS.findall(body)
            with self.subTest(page=name):
                self.assertEqual(len(heads), len(groups))
                for head in heads:
                    self.assertIn("role=button", head)
                    self.assertIn("tabindex=0", head)
                    self.assertRegex(head, r"aria-expanded=(true|false)")

    def test_every_group_has_exactly_one_body_to_fold(self):
        for name, markup in self.pages().items():
            body = body_of(markup)
            with self.subTest(page=name):
                self.assertEqual(len(GROUPS.findall(body)),
                                 len(re.findall(r"<div class=foldbody\b",
                                                body)))

    def test_a_heading_and_its_body_never_disagree_about_the_fold(self):
        # A group may be served folded: the settings import opens that way,
        # because it is an occasional action and the column is longer than
        # the screen. So the fence is that the three things saying which way a group is set say the same
        # thing: the container, the heading a screen reader reads, and whether
        # the body is hidden before any script has run.
        pattern = re.compile(
            r"<(?:section|div)[^>]*data-collapsed=(\d)[^>]*>\s*"
            r"<h[34] class=fold[^>]*aria-expanded=(true|false)[^>]*>.*?"
            r"<div class=foldbody(\s+hidden)?>",
            re.S,
        )
        for name, markup in self.pages().items():
            found = pattern.findall(body_of(markup))
            with self.subTest(page=name):
                self.assertEqual(len(found), len(GROUPS.findall(
                    body_of(markup))))
                for collapsed, expanded, hidden in found:
                    shut = collapsed == "1"
                    self.assertEqual(expanded, "false" if shut else "true")
                    self.assertEqual(bool(hidden.strip()), shut)

    def test_the_prep_groups_are_the_ones_it_had(self):
        # The note field lives in the report popup, not in a group of its
        # own.
        heads = re.findall(
            r"<h3 class=fold[^>]*><span class=nm>([^<]+)</span>", self.prep
        )
        self.assertEqual(len(heads), 4)
        self.assertEqual(heads[:3], ["Router", "V-score", "Edge Plating"])
        self.assertTrue(heads[3].startswith("V-score candidates"))

    def test_a_carrier_stage_is_a_group_with_its_switch_in_the_heading(self):
        # The heading bar carries a name only - the switch one line below it
        # shows the on/off state, so there is no `-state` badge. Each stage
        # is a SECTION of the column, not a group nested inside a Carrier
        # wrapper, and it is served collapsed.
        for group in ("pb-holes", "pb-fid", "pb-copper", "pb-scores"):
            with self.subTest(group=group):
                self.assertIn(
                    f"<section id={group} data-group={group} "
                    f"data-collapsed=1>",
                    self.panel,
                )
                self.assertNotIn(f'id={group}-state', self.panel)

    def test_the_switch_itself_did_not_move_out_of_the_group_body(self):
        # The switch keeps its title and helper line, inside the body.
        for switch, group in (("pb-holes-on", "pb-holes"),
                              ("pb-fid-on", "pb-fid"),
                              ("pb-copper-on", "pb-copper"),
                              ("pb-scores-on", "pb-scores")):
            with self.subTest(switch=switch):
                block = self.panel.split(f"data-group={group}", 1)[1]
                block = block.split("</section>", 1)[0]
                self.assertIn("<div class=foldbody hidden>", block)
                self.assertLess(block.index("<div class=foldbody hidden>"),
                                block.index(f"id={switch}"))
                self.assertIn(f'<input type=checkbox id={switch}', block)

    def test_the_fold_state_and_the_switch_state_are_two_hidden_things(self):
        # The fold hides the whole group; the switch GREYS the stage's fields
        # inside it, never hides them. Two owners, or one turns the other
        # back on.
        block = self.panel.split("data-group=pb-holes", 1)[1]
        self.assertLess(block.index("<div class=foldbody hidden>"),
                        block.index('<div class="dependent off" '
                                   "id=pb-holes-body"))
        self.assertIn('<div class="dependent off" id=pb-holes-body>', block)

    def test_the_column_says_how_a_folded_group_is_drawn(self):
        for token in (".controls .foldbody[hidden]{display:none}",
                      ".controls .fold{display:flex",
                      "[data-collapsed=\"1\"] > .fold::before"):
            with self.subTest(token=token):
                self.assertIn(token, page_served.CONTROLS_CSS)

    def test_the_caret_is_a_css_escape_and_not_a_control_character(self):
        # CAUGHT BEFORE IT SHIPPED. `'\25be'` in a Python string is the OCTAL
        # escape `\2` followed by 'be', so the page carried a control character
        # where a caret was meant. The backslash has to survive to the browser.
        self.assertIn(r"content:'\25be'", page_served.CONTROLS_CSS)
        self.assertIn(r"content:'\25b8'", page_served.CONTROLS_CSS)
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIsNone(
                    re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", markup)
                )


class WriteActionsFooterTest(PageBytesTestCase):
    """The write actions are under the scroll, not in it."""

    def test_the_named_buttons_are_in_the_footer_and_not_in_the_body(self):
        for name, markup in self.pages().items():
            body, footer = body_of(markup), actions_of(markup)
            for button in FOOTER_BUTTONS:
                if f"id={button}" not in controls_of(markup):
                    continue
                with self.subTest(page=name, button=button):
                    self.assertIn(f"id={button}", footer)
                    self.assertNotIn(f"id={button}", body)

    def test_the_footer_does_not_scroll_with_the_body(self):
        self.assertIn(".controls .body{overflow-y:auto;flex:1}",
                      page_served.CONTROLS_CSS)
        self.assertIn(".controls .actions{", page_served.CONTROLS_CSS)
        self.assertIn("flex:none}", page_served.CONTROLS_CSS.split(
            ".controls .actions{", 1)[1].split("}", 1)[0] + "}")

    def test_the_footer_sits_between_the_body_and_the_status_line(self):
        for name, markup in self.pages().items():
            column = controls_of(markup)
            with self.subTest(page=name):
                self.assertLess(column.index("<div class=body>"),
                                column.index("<div class=actions "))
                self.assertLess(column.index("<div class=actions "),
                                column.index("<div class=status "))

    def test_stopping_the_server_stays_reachable_while_an_action_runs(self):
        # The body is pointer-events:none while busy; the footer is not, and
        # `Quit Panelizer` lives there now. A stuck action must not take the
        # only way out with it.
        css = page_served.CONTROLS_CSS
        self.assertIn(".controls.busy .body{opacity:.5;pointer-events:none", css)
        dimmed = css.split(".controls.busy .actions{", 1)[1].split("}", 1)[0]
        self.assertNotIn("pointer-events", dimmed)
        for markup in self.pages().values():
            button = re.search(r"<button[^>]*id=pb-stop[^>]*>", markup)
            self.assertNotIn("data-act", button.group(0))

    def test_the_renderer_line_is_in_the_footer_where_it_is_always_read(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn("renderer:", actions_of(markup))

    def test_no_control_was_removed_or_renamed(self):
        for button, label in (("pb-save", "Save to Board"),
                              ("pb-reload", "Reload"),
                              ("pb-stop", "Quit Panelizer")):
            with self.subTest(control=button):
                self.assertIn(f"id={button}", self.prep)
                self.assertIn(label, self.prep)
        for control in PREP_CONTROLS:
            with self.subTest(page="prep", control=control):
                self.assertIn(f"id={control}", self.prep)
        for control in PANEL_CONTROLS:
            with self.subTest(page="panel", control=control):
                self.assertIn(f"id={control}", self.panel)
        for label in ("Build Panel", "Save Panel Files",
                      "compare with the source", "Quit Panelizer"):
            with self.subTest(label=label):
                self.assertIn(label, self.panel)


# ---------------------------------------------------------------------------
# the fold logic, RUN
# ---------------------------------------------------------------------------

FOLD_HARNESS = r"""
%(store)s
%(functions)s
const out = {};
// The store, modelled: a map that can be told to THROW, which is what Chrome
// does on an opaque origin and what every touch of it has to survive.
let THROWS = false;
const STORE = {};
globalThis.localStorage = {
  getItem(key) { if (THROWS) throw new Error('opaque origin'); return STORE[key] === undefined ? null : STORE[key]; },
  setItem(key, value) { if (THROWS) throw new Error('opaque origin'); STORE[key] = String(value); },
};
// The DOM, modelled: groups with a heading and a body. A heading bar carries
// a name only.
function group(id, collapsed) {
  const head = {tag: 'h3', dataset: {}, attrs: {}, handlers: {},
                setAttribute(name, value) { this.attrs[name] = value; },
                addEventListener(name, fn) { this.handlers[name] = fn; }};
  const body = {tag: 'div', className: 'foldbody', hidden: false};
  return {
    id: id, dataset: {group: id, collapsed: collapsed ? '1' : '0'},
    head: head, body: body,
    querySelector(what) {
      if (what === '.fold') return head;
      if (what === '.foldbody') return body;
      return null;
    },
  };
}
const GROUPS = [group('pb-sec-numbers', false), group('pb-sec-candidates', false),
                group('pb-holes', true)];
globalThis.document = {
  querySelectorAll: () => GROUPS,
};
let PB = {page: '%(page)s', accordion: %(accordion)s};
const logged = [];
function logEvent(type, data) { logged.push({type: type, data: data}); }

out.key = foldStoreKey();
// -- what a page opens as, with nothing remembered ------------------------
out.served = wireFolds();
out.wired = GROUPS.map(one => typeof one.head.handlers.click === 'function');
out.hiddenAtStart = GROUPS.map(one => one.body.hidden);

// -- a heading, pressed ---------------------------------------------------
GROUPS[0].head.handlers.click();
out.afterClick = {collapsed: GROUPS[0].dataset.collapsed,
                  hidden: GROUPS[0].body.hidden,
                  aria: GROUPS[0].head.attrs['aria-expanded']};
out.remembered = JSON.parse(STORE[foldStoreKey()] || '{}');
out.logged = logged;

// ...and pressed again.
GROUPS[0].head.handlers.click();
out.afterSecond = {collapsed: GROUPS[0].dataset.collapsed,
                   hidden: GROUPS[0].body.hidden};
out.rememberedAgain = JSON.parse(STORE[foldStoreKey()] || '{}');

// -- the keyboard, which is the same control ------------------------------
let prevented = 0;
GROUPS[1].head.handlers.keydown({key: 'Enter',
                                preventDefault() { prevented += 1; }});
GROUPS[1].head.handlers.keydown({key: 'x', preventDefault() { prevented += 1; }});
out.keyboard = {collapsed: GROUPS[1].dataset.collapsed, prevented: prevented};
// WHICH GROUPS ARE OPEN after that keyboard press. On an accordion page the
// answer has to be exactly one name; on the folding page it is whatever the
// clicks left behind.
out.openAfterKeyboard = GROUPS.filter(one => one.dataset.collapsed !== '1')
                              .map(one => one.dataset.group);

// -- a page opened again, reading what was remembered ---------------------
GROUPS.forEach(one => { one.dataset.collapsed = '0'; one.body.hidden = false; });
out.applied = applyFolds();
out.reopened = GROUPS.map(one => one.body.hidden);

// -- a store that refuses ------------------------------------------------
THROWS = true;
out.readWhenRefused = readFolds();
GROUPS[0].head.handlers.click();
out.foldedAnyway = {collapsed: GROUPS[0].dataset.collapsed,
                    hidden: GROUPS[0].body.hidden};
console.log(JSON.stringify(out));
"""

# The page's fold functions, lifted out by name for the node harness.
FOLD_FUNCTIONS = ("foldStoreKey", "readFolds", "writeFolds", "rememberFold",
                  "accordion", "rememberOpen",
                  "paintFold", "foldGroups", "applyFolds", "toggleFold",
                  "wireFolds")


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientFoldTest(unittest.TestCase):
    """The fold, RUN: what it paints, what it remembers, and what it survives."""

    @classmethod
    def setUpClass(cls):
        cls.out = cls.run_for("prep")

    @staticmethod
    def run_for(page: str, accordion: bool = False) -> dict:
        return run_js(FOLD_HARNESS % {
            # The store's key prefix and the accordion's own key are DATA and
            # are lifted verbatim: a harness that restated them would be
            # testing its own copy of the page.
            "store": js_slice(page_served.CONTROLS_JS,
                              "const FOLD_STORE =", "'pb.folds.';")
            + "\n" + js_slice(page_served.CONTROLS_JS,
                               "const OPEN_KEY =", "= '__open';"),
            "functions": "\n".join(
                js_function(page_served.CONTROLS_JS, name)
                for name in FOLD_FUNCTIONS
            ),
            "page": page,
            "accordion": "true" if accordion else "false",
        })

    def test_a_page_that_remembers_nothing_opens_as_it_was_served(self):
        self.assertEqual(self.out["hiddenAtStart"], [False, False, True])
        self.assertEqual(self.out["served"],
                         {"pb-sec-numbers": False, "pb-sec-candidates": False,
                          "pb-holes": True})

    def test_every_heading_is_wired_as_the_control_that_folds_it(self):
        self.assertEqual(self.out["wired"], [True, True, True])

    def test_pressing_a_heading_folds_it_and_says_so(self):
        self.assertEqual(
            self.out["afterClick"],
            {"collapsed": "1", "hidden": True, "aria": "false"},
        )
        self.assertEqual(self.out["afterSecond"],
                         {"collapsed": "0", "hidden": False})

    def test_a_fold_is_remembered_and_an_unfold_is_remembered_too(self):
        self.assertEqual(self.out["remembered"], {"pb-sec-numbers": True})
        self.assertEqual(self.out["rememberedAgain"],
                         {"pb-sec-numbers": False})

    def test_the_keyboard_folds_the_same_group_the_pointer_does(self):
        self.assertEqual(self.out["keyboard"],
                         {"collapsed": "1", "prevented": 1})

    def test_a_page_opened_again_is_folded_the_way_it_was_left(self):
        # `pb-holes` was never touched, so it keeps what it was SERVED as.
        self.assertEqual(self.out["applied"],
                         {"pb-sec-numbers": False, "pb-sec-candidates": True,
                          "pb-holes": False})
        self.assertEqual(self.out["reopened"], [False, True, False])

    def test_a_fold_is_journaled_with_the_group_it_folded(self):
        folds = [one for one in self.out["logged"] if one["type"] == "fold"]
        self.assertTrue(folds)
        self.assertEqual(folds[0]["data"]["group"], "pb-sec-numbers")
        self.assertTrue(folds[0]["data"]["collapsed"])
        self.assertEqual(folds[0]["data"]["page"], "prep")

    def test_a_store_that_throws_costs_the_fold_nothing(self):
        # Chrome treats file:// as an opaque origin and throws on any touch of
        # localStorage. The column still folds; it just forgets.
        self.assertEqual(self.out["readWhenRefused"], {})
        self.assertEqual(self.out["foldedAnyway"],
                         {"collapsed": "1", "hidden": True})

    def test_the_two_pages_remember_their_own_folds(self):
        panel = self.run_for("panel")
        self.assertEqual(self.out["key"], "pb.folds.prep")
        self.assertEqual(panel["key"], "pb.folds.panel")
        self.assertNotEqual(self.out["key"], panel["key"])


@unittest.skipUnless(shutil.which("node"), "no node to run the page's own script")
class ClientAccordionTest(unittest.TestCase):
    """ONE OPEN AT A TIME, RUN.

    Step 2 opens with every group collapsed and opening one closes the rest, so
    what is remembered is the name of the single open group. Run rather than
    read: "only one" is behaviour over three groups and a store, and a string in
    the page cannot show that the second click shut the first group.
    """

    @classmethod
    def setUpClass(cls):
        cls.out = ClientFoldTest.run_for("panel", accordion=True)

    def test_a_page_that_remembers_nothing_opens_with_every_group_shut(self):
        self.assertEqual(self.out["hiddenAtStart"], [True, True, True])
        self.assertEqual(self.out["served"],
                         {"pb-sec-numbers": True, "pb-sec-candidates": True,
                          "pb-holes": True})

    def test_opening_one_closes_the_others(self):
        # The first click OPENS the first group - every group was served shut -
        # and the keyboard then opens the second, which has to shut the first.
        self.assertEqual(self.out["afterClick"],
                         {"collapsed": "0", "hidden": False, "aria": "true"})
        self.assertEqual(self.out["openAfterKeyboard"], ["pb-sec-candidates"])

    def test_what_is_remembered_is_which_one_is_open(self):
        self.assertEqual(self.out["remembered"], {"__open": "pb-sec-numbers"})
        # Pressing the open group again shuts it, and then nothing is open.
        self.assertEqual(self.out["rememberedAgain"], {"__open": ""})

    def test_a_page_opened_again_opens_the_one_that_was_open(self):
        self.assertEqual(self.out["applied"],
                         {"pb-sec-numbers": True, "pb-sec-candidates": False,
                          "pb-holes": True})
        self.assertEqual(self.out["reopened"], [True, False, True])

    def test_a_store_that_refuses_still_folds(self):
        self.assertEqual(self.out["readWhenRefused"], {})
        self.assertEqual(self.out["foldedAnyway"],
                         {"collapsed": "0", "hidden": False})


class FoldWiringOnThePageTest(PageBytesTestCase):
    """The page that is served actually calls the thing that folds it."""

    def test_both_pages_wire_their_folds_on_load(self):
        for name, markup in self.pages().items():
            with self.subTest(page=name):
                self.assertIn("wireFolds();", markup)
                self.assertIn("function toggleFold(id)", markup)
                self.assertIn("const FOLD_STORE = 'pb.folds.';", markup)

    def test_the_store_is_touched_nowhere_without_a_guard(self):
        # Every read and every write, wrapped, so the page keeps working on an
        # origin that refuses to have a store at all.
        script = page_served.CONTROLS_JS
        for touch in ("localStorage.getItem", "localStorage.setItem"):
            with self.subTest(touch=touch):
                before = script[:script.index(touch)]
                self.assertIn("try {", before[-200:])

    def test_the_panel_page_greys_a_stage_s_body_true_from_the_config(self):
        # The switch's state reaches the page as the dependent block's grey,
        # not as a heading badge.
        self.assertIn("setDependent(body, carrier && on);", page_panel.PANEL_JS)


if __name__ == "__main__":
    unittest.main()
