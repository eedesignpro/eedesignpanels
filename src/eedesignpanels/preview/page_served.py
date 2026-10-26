"""The SERVED preview pages: the review page's components, plus the handlers.

**One server, two pages.** Step 1 is this module - prepare
the board - and step 2 is :mod:`eedesignpanels.preview.page_panel`, which
panelizes it. Everything that is the same on both lives here and is shared
literally, not by resemblance: the CSS, the busy popup, the unsaved-work
prompt, the step links, and the two halves of the script
(:data:`SHARED_JS`, :data:`SHARED_TAIL`) that :func:`page_script` splices a
page's own controls between. A page therefore owns its third column, its
viewports and one function - `applyFragment` - and nothing else.


One component, two hosts. The frozen review
artifact stays exactly what it was - a single portable file with no controls,
no origin and nothing to talk to - and this page is built from the same parts:
:mod:`eedesignpanels.review.page`'s viewport, CSS and pan/zoom, the shared
:mod:`eedesignpanels.review.layerpanel`, and :mod:`eedesignpanels.review.overlay`'s
proposal annotation. What the served host ADDS is a third column: the two
declared numbers as inputs, every candidate as a row with an accept and a
decline button, and the run's notes.

The picture is rendered into the markup server-side, so a page whose script
never runs still shows the board. The controls are drawn by the script from the
JSON the server sends, because they are re-drawn on every change and having one
renderer for the first paint and the tenth is worth more here than degrading
gracefully into a form nobody can submit.

Same-origin throughout, and no external reference of any kind: the SVG, CSS and
JS are inline exactly as they are in the frozen artifact. The only thing this
page fetches is its own server.

**While an action is in flight the controls are dead and the display says so.**
Every control that changes something carries `data-act` and is disabled for the
whole of the recompute; on top of that a POPUP is drawn OVER the viewports,
dimming them, so a picture that is not currently answering is obviously not
answering. A generation announced meanwhile is painted ONCE at the end rather
than as it arrives, so the display does not jump under a rapid series of
clicks. The server refuses a second
action independently (`actions.Busy`), so the popup is what the eye is told and
not the mechanism.

**The controls column is groups, and they fold.** Every
section on both pages is a collapsible group whose heading is the control that
folds it; what is folded is remembered per page in `localStorage`, wrapped in
try/catch at every touch; and a group with a SWITCH wears its on/off in its
heading, so a stage that is running is readable while its fields are folded
away. The write actions do not fold and do not scroll: they live in a footer
(:func:`actions_html`) under the scrolling body, because the buttons a user
reaches for after reading a long column are the ones that must not need a
scroll back.

**A picture says when it is not finished yet.** An edit is
painted in two phases - the fast render at once, the content render behind it -
and the corner of the viewport carries one short line while the second one is
still coming (:func:`await_html`). A corner note and not a banner: the picture is
about to be right on its own.

**One button writes.** "Save to Board" carries the number of changes waiting -
`Save to Board (3 changes)` - and puts the board and the config on disk in one
action. Everything else a click does happens in memory, which is why an accept
costs a recomputation and not a write.

**Unsaved work is not lost quietly.** Stopping the server with changes waiting
raises a three-way prompt - save and stop, discard and stop, cancel - and
`beforeunload` warns a browser that is closing the tab. A session with nothing
pending stops without a word. A refresh the PAGE asked for is not a tab close
and never raises that box (`selfReload`): nothing can be lost
because the server is holding every pending edit and the page reads the count
back on the way in.

**A change is painted, not reloaded.** A fragment carries the frame its render
was drawn in and what that render measures, so a rebuilt picture of a different
size is a content swap, a re-frame and a redrawn readout. The two steps
therefore open in SEPARATE browser tabs (`_steps_html`) and stay open: neither
one replaces the other, and both hold their own event stream over the one
session.

**When the server stops, the page says so.** The shutdown button on THIS page
asks, then tries `window.close()`; a browser that refuses to close a tab it did
not open gets the whole document replaced by a terminal notice instead. A
session that ends any other way - the other tab was the last one and it closed,
or the run ended - replaces the document and never closes the tab: a page
that vanishes takes the reason with it. Either way there is
no control left that could look live.

**Nothing on this page is called `User.38`.** Every layer a sentence mentions is
rewritten into the board's own display name on the way out
(:mod:`eedesignpanels.preview.naming`), and the finished bytes are read back to
check it.

**This page is also instrumented, and the frozen one is not.** A reviewer
looks at a picture faster than anyone can describe one, so the served host
records what the user did: control clicks, layer toggles, view settles
and every action go to `/journal` in throttled batches, and `S` (or the header
button) opens the report dialog, which takes a snapshot of the current view,
the whole backend state, and the one line typed in the dialog. All of it lives
in this module - the shared components in :mod:`eedesignpanels.review` are
untouched, so a frozen artifact is still a file with nothing to talk to.
"""

import html
import json
import re
from pathlib import Path
from typing import NamedTuple

from eedesignpanels import prep
from eedesignpanels.preview import bugreport, icons, naming
from eedesignpanels.review import holes, layerpanel, page
from eedesignpanels.review import theme as theme_module
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import blade_extent

_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.S)


def inner_svg(svg_path: Path) -> str:
    """What is inside one SVG document's root element, read off the disk.

    THE FALLBACK. A fragment is normally served from the
    markup the render produced and never went back to the file for
    (`session._markup`), because on this machine the FIRST read of a freshly
    written file is expensive: reading back the 52 documents a render had just
    written measured 345 ms against 9 ms warm, and every edit paid that inside
    its own first paint. The documents are still written - they are the
    session's artifact and a snapshot reads them - and this is what reads one
    when memory has not got it.
    """
    return inner_of(Path(svg_path).read_text(encoding="utf-8"), svg_path)


def inner_of(text: str, named=None) -> str:
    """What is INSIDE one SVG document's root element.

    The live update swaps contents rather than elements, so the pan/zoom
    handlers - which hold their nodes in a closure - keep pointing at the nodes
    they were bound to. Replacing the elements themselves would leave every
    later drag updating a detached tree, which looks exactly like the viewer
    freezing.

    *named* is only what a failure calls the document, so a bad one is reported
    by path when it came from a path.
    """
    opening = _SVG_OPEN.search(text)
    if opening is None:
        raise ValueError(f"{named or 'this document'} has no <svg> element")
    closed = text.rfind("</svg>")
    if closed < 0:
        raise ValueError(f"{named or 'this document'} has no </svg>")
    # kicad-cli's generated <title> carries the render timestamp. The frozen
    # page drops it so two runs agree; the live one drops it for the same
    # reason, so a fragment is a function of the board and nothing else.
    return page._TITLE.sub("", text[opening.end():closed])


def proposals_json(view, include_ids: bool = False) -> list:
    """Every candidate as the controls column sees it - number, verdict, blade line.

    In PHYSICAL order, numbered `#1`, `#2`, and wearing one of the three words:
    the reader has to be able to match a row to a line on the picture, and a
    uuid prefix does not let anyone do that.

    **The hash id does not travel to the page.** The identity that goes over the
    wire is `blade` - the line ACCEPTING this candidate would write, the
    candidate's line produced to the outline's bounding box, which is
    :func:`route_toolpath.score_line_for`'s own extent - so what names a cut is
    the cut. *include_ids* is for a state FILE, where the internal key beside
    the display number is worth having.
    """
    out = []
    for entry in view.labelled:
        proposal = entry.proposal
        candidate = proposal.candidate
        start, end = blade_extent(candidate, view.bbox)
        row = {
            "number": entry.number,
            "displayId": entry.display_id,
            "verdict": entry.verdict,
            "accepted": bool(proposal.accepted),
            "proposable": bool(proposal.proposable),
            "reason": naming.humanize(proposal.reason, view.layer_names),
            "summary": naming.humanize(proposal.summary, view.layer_names),
            "midpoint": list(candidate.midpoint),
            "length_mm": candidate.length_mm,
            "extent_mm": candidate.extent_mm,
            "spans": len(candidate.spans),
            "blade": {"start": list(start), "end": list(end)},
        }
        if include_ids:
            row["id"] = proposal.id
            row["short_id"] = proposal.short_id
            row["stage_verdict"] = proposal.verdict
        out.append(row)
    return out


def extent_text(view) -> str:
    """What the readout under a viewport says: the subject's own extents.

    The BOARD's extents and not the render window's - the window carries a
    margin so the captions have somewhere to sit, and a readout of that would be
    a number about the picture rather than about the board.

    It travels on the fragment as well as being baked into the page, because a
    picture that is rebuilt in place changes size: a readout left at the last
    build's numbers would be the one thing on screen still describing it.
    """
    if view is None:
        return "no panel built yet"
    bbox = view.bbox
    return f"{bbox[2] - bbox[0]:.2f} x {bbox[3] - bbox[1]:.2f} mm"


def base_box(view) -> str:
    """The frame a viewport draws this view in - the render window, padded.

    Exactly what `page.inline_svg` writes into `data-base`, from the same
    function, so a page told to re-frame itself lands on the frame it would have
    been served with.
    """
    if view is None or not getattr(view, "window", ()):
        return ""
    return page.padded_box(*view.window)


def params_json(config) -> dict:
    """The declared numbers a click may change, plus what the stage is doing."""
    if config is None or config.prep is None:
        return {}
    return {
        "cutter_diameter": config.prep.cutter_diameter,
        "route_past_score_mm": config.prep.vscore.route_past_score_mm,
        "route_along_score_mm": config.prep.vscore.route_along_score_mm,
        "route_tangent_angle_deg": config.prep.vscore.route_tangent_angle_deg,
        "route_end_overrides": config.prep.vscore.route_end_overrides,
        "custom_route_ends": config.prep.vscore.custom_route_ends,
        # DECLARED ON THIS PAGE: accepting a score writes a
        # copper keep-out this wide into the board, so it is a board setting.
        "score_rule_half_width_mm": config.score_rule_half_width_mm,
        "min_tab_mm": float(config.min_tab_mm),
        # THE HEAL, DECLARED. It reads the SOURCE
        # BOARD's outline, so it belongs on this page with the other declared
        # numbers, and it is always sent - a setting the page cannot see is a
        # setting nobody knows is active.
        "outline_heal_mm": float(config.outline_heal_mm),
        "eliminate_slivers": bool(config.eliminate_slivers),
        "edge_plating": bool(config.edge_plating),
        "edge_plating_front_mask": config.edge_plating_front_mask,
        "edge_plating_back_mask": config.edge_plating_back_mask,
        "edge_plating_mask_width_mm": config.edge_plating_mask_width_mm,
        "lock_board_on_save": config.lock_board_on_save,
        # POCKETING, ALWAYS SENT. A switch the page
        # cannot see is a switch nobody knows the state of, and this one decides
        # whether a wide cutout leaves a loose piece in the panel.
        "pocket_cutouts": bool(config.pocket_cutouts),
        "detect": config.prep.vscore.detect,
    }


CONTROLS_CSS = """
.controls{width:330px;flex:none;background:#1c1f25;border:1px solid #2a2f37;
          border-radius:8px;display:flex;flex-direction:column;height:100%;
          color:#e6e8eb;font:13px/1.45 system-ui,sans-serif;overflow:hidden}
.controls header{padding:.5rem .7rem;border-bottom:1px solid #2a2f37;
                 font-weight:600;font-size:12.5px;display:flex;gap:.5rem;
                 align-items:center;justify-content:space-between}
.controls .gen{font:11px ui-monospace,monospace;color:#6e7681}
.controls .body{overflow-y:auto;flex:1}
.controls section{padding:.55rem .7rem;border-bottom:1px solid #2a2f37}
/* THE HANDLE READS AS A HANDLE: larger and bolder
   than the fields it folds, and INVERTED - a light bar with dark text on this
   dark column - so a section head is obvious at a glance, not hunted for. */
.controls h3{margin:0 0 .35rem;font-size:13px;text-transform:uppercase;
             letter-spacing:.06em;color:#c9d1d9;font-weight:700}
.controls .fold{background:#aeb6bf;color:#14161a;border-radius:4px;
                margin:0 -.25rem .35rem;padding:.15rem .45rem}
.controls .fold::before{color:#14161a}
/* COLLAPSIBLE GROUPS. The column is longer than the screen
   on every real board, so every group folds: the heading IS the control, the
   caret says which way it goes, and what is folded is remembered per PAGE
   (`localStorage`, wrapped - a browser may refuse the store entirely).
   A HEADING BAR CARRIES A NAME AND NOTHING ELSE: the switch inside the group
   already says which way the stage is set, twice over is noise. */
.controls .fold{display:flex;align-items:center;gap:.35rem;cursor:pointer;
                user-select:none}
/* The caret is a CSS escape, and the backslash is DOUBLED because this is a
   Python string: `\\25be` is the character, `\\2` alone is an octal escape and
   would put a control character on the page. */
.controls .fold::before{content:'\\25be';font-size:11px;color:#8b939d;
                        width:.8em;flex:none}
.controls [data-collapsed="1"] > .fold::before{content:'\\25b8'}
.controls .fold .nm{flex:1}
/* A STATUS IN THE GROUP BODY, NEVER A CONTROL, and not on the heading bar: it
   belongs with the settings it is about. The dot is the layer panel's own
   `.lp-status`, so the whole page has one visual vocabulary for good and
   bad. Empty until something measures it. */
/* Scoped to a fold body's own child, because the column's FOOTER line is also
   a `.status` and this must not restyle it. */
.controls .foldbody > .status{display:flex;align-items:center;gap:.3rem;
                              margin:.1rem 0 .35rem;padding:0;border:0;
                              min-height:0;font:10px ui-monospace,monospace;
                              color:#8b939d;text-transform:none;
                              letter-spacing:0}
.controls .foldbody > .status[hidden]{display:none}
.controls .fold:focus-visible{outline:1px solid #79b8ff;outline-offset:2px}
.controls .foldbody[hidden]{display:none}
/* THE WRITE ACTIONS NEVER SCROLL AWAY. Save to Board, Build
   Panel, Save Panel Files and Quit Panelizer live under the scrolling body, in a
   footer of their own: the one thing a user reaches for after reading a long
   column must not be the one thing that needs a scroll back. */
.controls .actions{padding:.5rem .7rem;border-top:1px solid #2a2f37;
                   background:#1c1f25;flex:none}
.controls .actions .row:first-child{margin-top:0}
.controls label{display:flex;align-items:center;gap:.4rem;margin:.2rem 0;
                font-size:12px;color:#c9d1d9}
.controls label span{flex:1}
/* EVERY ENTRY FIELD IS A TITLE, A CONTROL AND ONE HELPER LINE. The title is
   bold because it is the name of the setting; the helper is one short line
   under it because a tooltip is a thing nobody hovers over while typing. Two
   rows of a grid: the control sits beside the title, the helper spans the
   width beneath both. */
.controls label.field{display:grid;grid-template-columns:1fr auto;
                      align-items:center;gap:.1rem .5rem;margin:.45rem 0}
.controls label.field .t{grid-column:1;grid-row:1;font-weight:600;
                         font-size:12px;color:#e6e8eb;flex:none}
.controls label.field input{grid-column:2;grid-row:1}
.controls label.field .h{grid-column:1/-1;grid-row:2;font-size:11px;
                         line-height:1.35;color:#8b939d;flex:none}
.controls label.field input[type=checkbox]{width:auto;accent-color:#79b8ff}
/* A DEPENDENT BLOCK IS GREYED, NEVER HIDDEN. The governing setting sits on
   top, and a vertical line to the left of the dependent settings draws the
   hierarchy. A setting nobody can read is a setting nobody can check before
   turning its governor on, and a block that appears and vanishes moves
   everything under it while the user is reading.
   One class, used by every dependent block on both pages, so the hierarchy is
   drawn the same way everywhere. `.off` is the greyed state - dimmed as a whole
   so the labels and the helper lines go with the controls, and still showing
   the values that would apply. The `disabled` attribute on each control is what
   actually stops it being tabbed into, typed in, or read into the config. */
.controls .dependent{border-left:2px solid #2a2f37;padding-left:.5rem;
                     margin:.1rem 0 .2rem .1rem}
.controls .dependent.off{border-left-color:#363d47;opacity:.5;
                         filter:saturate(.35)}
/* A number box dims itself when disabled; inside a block that is already dimmed
   the two would compound into something unreadable, so the block's dimming is
   the only one that applies there. */
.controls .dependent.off input[type=number][disabled]{opacity:1}
/* A DEPENDENT BLOCK INSIDE A DEPENDENT BLOCK dims ONCE. The tooling-hole
   insets are governed by the placement dropdown inside a group governed by
   its own switch, so both blocks wear `.off` at the same time and two 50%
   dimmings would compound into something nobody can read. The inner line still darkens, so the hierarchy is
   still drawn - it is only the dimming that does not stack. */
.controls .dependent.off .dependent.off{opacity:1;filter:none}
/* A CARRIER STAGE. Its switch is the stage, it
   sits directly above the block it governs, and the block is greyed rather than
   removed when the switch is off, as above. */
.controls .grp{border-top:1px solid #23272e;margin-top:.5rem;padding-top:.3rem}
.controls .grp h4{margin:0;font-size:11.5px;font-weight:600;color:#c9d1d9}
.controls label.field.wide{grid-template-columns:1fr}
.controls label.field.wide textarea{grid-column:1;grid-row:2;width:100%;
                                    box-sizing:border-box;resize:vertical;
                                    background:#14161a;color:#e6e8eb;
                                    border:1px solid #363d47;border-radius:4px;
                                    padding:.2rem .35rem;
                                    font:12px ui-monospace,monospace}
.controls label.field.wide .h{grid-row:3}
/* THE SETTINGS IMPORT. A native picker, full width on its
   own row: the browser writes the file's name into the control, which is wider
   than any number box on this page. */
.controls label.field.wide input[type=file]{grid-column:1;grid-row:2;
                                            width:100%;box-sizing:border-box;
                                            background:#14161a;color:#8b939d;
                                            border:1px solid #363d47;
                                            border-radius:4px;
                                            padding:.2rem .35rem;
                                            font:11px system-ui,sans-serif}
.controls label.field select{grid-column:2;grid-row:1;background:#14161a;
                             color:#e6e8eb;border:1px solid #363d47;
                             border-radius:4px;padding:.15rem .25rem;
                             font:12px system-ui,sans-serif}
.controls input[type=number]{width:92px;background:#14161a;color:#e6e8eb;
                             border:1px solid #363d47;border-radius:4px;
                             padding:.15rem .35rem;
                             font:12px ui-monospace,monospace}
.controls .row{display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.4rem}
.controls .btn{background:#262b33;border:1px solid #363d47;color:#c9d1d9;
               border-radius:4px;padding:.2rem .55rem;
               font:11px ui-monospace,monospace;cursor:pointer}
.controls .btn:hover{background:#2f3540;color:#fff}
.controls .btn[disabled]{opacity:.45;cursor:default}
.controls .btn.go{border-color:#2ea04366;color:#7ee2a0}
.controls .btn.no{border-color:#e8a13a66;color:#f0c078}
/* Busy: an action is in flight, so nothing in this column may be clicked. The
   attributes do the blocking (a disabled control cannot be reached by keyboard
   either); this says so, and pointer-events closes the gap while the candidate
   list is being redrawn. */
.controls.busy .body{opacity:.5;pointer-events:none;filter:saturate(.4)}
/* The write actions dim with the rest and are disabled by `data-act` - but they
   keep their pointer events, because `Quit Panelizer` lives here and quitting has
   to work while an action is running. */
.controls.busy .actions{opacity:.6;filter:saturate(.4)}
.controls.busy header .gen::after{content:' - working';color:#f0c078}
.controls input[type=number][disabled]{opacity:.45}
.btn.save{border-color:#3b82f6;color:#cfe3ff;background:#1d2b45}
.btn.save[disabled]{opacity:.5}
.btn.save.dirty{border-color:#e8a13a;color:#f0c078;background:#332a18}
/* THE BUSY POPUP. The controls disable themselves, but a
   disabled column says nothing about the PICTURE - and the picture is what the
   user is reading. So the viewports are dimmed under a centred card, which
   makes a display that cannot be trusted impossible to mistake for one that
   can. It covers the views only: the header, the layer panel and the stop
   button stay reachable. */
.views{position:relative}
.busyveil{position:absolute;inset:0;display:none;align-items:center;
          justify-content:center;background:#0d0f13cc;z-index:50;
          backdrop-filter:saturate(.3) blur(1px)}
.busyveil.on{display:flex}
.busyveil .note{background:#1c1f25;border:1px solid #3b424d;border-radius:10px;
                padding:1rem 1.4rem;text-align:center;color:#e6e8eb;
                font:14px/1.5 system-ui,sans-serif;
                box-shadow:0 10px 40px #000000aa;min-width:16rem}
.busyveil b{display:block;font-size:15px;margin-bottom:.25rem}
.busyveil span{color:#8b939d;font-size:12px}
.busyveil .spin{width:26px;height:26px;margin:0 auto .6rem;border-radius:50%;
                border:3px solid #2a2f37;border-top-color:#79b8ff;
                animation:pbspin 900ms linear infinite}
@keyframes pbspin{to{transform:rotate(360deg)}}
/* THE CORNER NOTE: what this viewport is still waiting for.
   An edit paints the fast render first - graphics exact, no pad, track or zone -
   and the content render replaces those layers a second or two later. That is a
   picture with something missing, so it says so; and it says it in the CORNER,
   because a banner across a picture that is about to be right on its own would
   cost more attention than the wait does. Pointer-events off: a note is not a
   control and must never swallow a pan. */
/* Above every layer: the stack paints an active layer at 99 and a top row at
   999 (`review.page`), and a note under the picture is not a note. */
/* TWICE THE SIZE, WITH A SPINNER: a KiCad fill can take ten seconds, so the
   note carries the busy popup's own spinner and large type, and work in
   progress is seen, not found. */
.awaitnote{position:absolute;right:.5rem;bottom:.5rem;max-width:44ch;
           z-index:1000;display:flex;align-items:center;gap:.6rem;
           background:#14161ae0;border:1px solid #2a2f37;border-radius:8px;
           padding:.5rem .9rem;font:15px/1.4 system-ui,sans-serif;
           color:#aeb6bf;pointer-events:none}
.awaitnote .spin{width:20px;height:20px;flex:none;border-radius:50%;
                 border:3px solid #2a2f37;border-top-color:#79b8ff;
                 animation:pbspin 900ms linear infinite}
.awaitnote.bad .spin{display:none}
.awaitnote[hidden]{display:none}
.awaitnote.bad{color:#f0a3a3;border-color:#5a2f2f}
/* WHY A LAYER IS RED, IN THE CORNER OF THE PICTURE THAT IS RED. A red layer
   with the reason nowhere on the page is a puzzle that sends the user to the
   developer console. Top left,
   so it never sits on the waiting note in the bottom right; pointer-events off,
   because a status is not a control and must not swallow a pan. */
.alarmnote{position:absolute;left:.5rem;top:.5rem;max-width:52ch;z-index:1000;
           background:#1a1114f0;border:1px solid #7a2b2b;border-radius:8px;
           padding:.45rem .8rem;font:14px/1.45 system-ui,sans-serif;
           color:#ff8f8f;pointer-events:none}
.alarmnote div{white-space:normal}
.alarmnote[hidden]{display:none}
/* The row whose swatch is the alarm colour, marked as well as coloured: a
   colour alone is a thing to compare, a mark is a thing to find. */
.layerpanel .lp-row.lp-alarm .lp-nm{color:#ff8f8f}
/* The unsaved-work prompt: a classic three-way question, because a browser
   confirm only offers two and the third answer here is the important one. */
/* THE QUESTION IS OPAQUE, AND IT IS ON TOP. A
   board showing through a question makes both hard to read, and the board has
   nothing to say while one is being asked. The cover is solid, and it sits
   above every other layer this page paints, the busy note included. */
.ask{position:fixed;inset:0;display:none;align-items:center;
     justify-content:center;background:#0d0f13;z-index:1200}
.ask.on{display:flex}
.ask .box{background:#1c1f25;opacity:1;isolation:isolate;
          border:1px solid #3b424d;border-radius:10px;
          padding:1.1rem 1.3rem;max-width:46ch;color:#e6e8eb;
          font:13px/1.55 system-ui,sans-serif;box-shadow:0 10px 40px #000000aa}
.ask h2{margin:0 0 .5rem;font-size:15px}
.ask ul{margin:.4rem 0 .8rem;padding-left:1.1rem;color:#8b939d;font-size:12px}
.ask p{margin:.4rem 0;color:#8b939d;font-size:12px}
.ask code{display:block;margin:.4rem 0 .8rem;padding:.25rem .45rem;
          background:#14161a;border:1px solid #363d47;border-radius:4px;
          font:11px ui-monospace,monospace;color:#c9d1d9;user-select:all;
          overflow-x:auto}
.ask .row{display:flex;gap:.5rem;justify-content:flex-end;flex-wrap:wrap}
/* THE NOTE IS TYPED IN THE DIALOG. It is the only place a
   note is typed, so it gets the full width of the box: the person is
   writing what went wrong, not filling in a field in the header. */
.ask input{display:block;width:100%;box-sizing:border-box;margin:.4rem 0 .8rem;
           background:#14161a;color:#e6e8eb;border:1px solid #363d47;
           border-radius:4px;padding:.3rem .45rem;
           font:12px system-ui,sans-serif}
.ask input::placeholder{color:#6e7681}
.cand{border:1px solid #2a2f37;border-radius:6px;padding:.4rem .5rem;
      margin:.35rem 0;background:#181b20}
.cand .top{display:flex;align-items:center;gap:.4rem}
.cand .dot{width:10px;height:10px;border-radius:50%;flex:none}
.cand .sid{font:11px ui-monospace,monospace;color:#c9d1d9;flex:1}
.cand .vd{font-size:11px;color:#8b939d}
.cand .where{font:11px ui-monospace,monospace;color:#6e7681;margin-top:.2rem}
.cand .why{font-size:11px;color:#8b939d;margin-top:.2rem}
.status{padding:.5rem .7rem;border-top:1px solid #2a2f37;font-size:11.5px;
        color:#8b939d;min-height:2.4rem}
.status.bad{color:#f0a3a3}
.status.good{color:#7ee2a0}
.notes{margin:0;padding-left:1rem;color:#8b939d;font-size:11px}
.notes li.alarm{color:#ff6b6b;font-weight:600}
.notes li{margin:.15rem 0}
/* The session tools: one button, in the header, where the user's hand
   already is. */
.tools{display:flex;gap:.4rem;align-items:center;flex:none}
/* THE HEADER BUTTON IS RED. It is the one control on
   the page that reaches outside this session - it sends a zip to the
   developer - so it must not read as one more session button. The two
   saves INSIDE the dialog keep the blue: by the time they are on screen
   the person has already chosen to report, and the warning is spent. */
.btn.report{border-color:#3b82f680;color:#9ec5ff}
.tools .btn.report{border-color:#f2483c99;color:#ff6b6b}
.tools .btn.report:hover{border-color:#f2483c;color:#f0a3a3;background:#5a2f2f}
/* The mail link in the report dialog. The page sets no link colour
   anywhere else, and a browser default reads as purple on this ground. */
.ask a{color:#9ec5ff}
/* ONE ROW, ONE HEIGHT. The report button and the two step
   links share one line of the header, so all of them are given the same box
   height instead of each sizing to its own text. */
.tools .btn,.steps a,.steps span{height:1.6rem;box-sizing:border-box;
                                 display:flex;align-items:center}
/* ONE CONTROL IN THE HEADER, AND NO PRIVACY SENTENCE. The note box is in
   the report dialog, which is the one place a note is typed. What the report
   holds is said in the report dialog, where the
   person decides whether to make one. */
.footer{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
        font:11px ui-monospace,monospace;color:#6e7681;flex:none}
.footer b{color:#c9d1d9;font-weight:500}
.footer code{color:#8b939d;font:11px ui-monospace,monospace}
/* What is left of the page once the server has stopped. Every control is gone
   with the document that held them, so there is nothing here that could look
   live. */
.dead{max-width:60ch;margin:15vh auto;text-align:center;color:#8b939d;
      font:14px/1.6 system-ui,sans-serif}
.dead h1{font-size:20px;color:#c9d1d9;margin:0 0 .6rem}
.dead code{font:12px ui-monospace,monospace;color:#6e7681}
/* THE TWO STEPS. One server, two pages: prepare the board,
   then panelize it. Each names both steps and marks the one it is, so a reader
   always knows where in the job they are and can take one click to the other. */
.steps{display:flex;gap:.35rem;align-items:center;flex:none;
       font:11px ui-monospace,monospace}
.steps a,.steps span{padding:.15rem .5rem;border:1px solid #363d47;
                     border-radius:4px;color:#8b939d;text-decoration:none}
.steps a:hover{background:#2f3540;color:#fff}
.steps .here{background:#2b3240;border-color:#79b8ff;color:#cfe3ff}
/* CLICKABLE CANDIDATE LABELS. The stacked SVGs are
   pointer-events:none so a press anywhere on them reaches the viewport and
   pans; that stays true. The caption groups alone take pointer events back -
   `all`, so the transparent hit box counts as a target whatever it is painted
   with - and nothing else in the picture becomes clickable. A REJECTED
   candidate carries no group at all, so it has no cursor and no highlight. */
.viewport svg .pb-hit{pointer-events:all;cursor:pointer}
.viewport svg .pb-hit:hover .pb-hitbox{fill:#ffffff26}
.viewport svg .pb-hit:hover .pb-label{fill:#ffffff}
/* THE STALE BANNER. A panel build is heavier than a prep compute, so a
   parameter change does NOT rebuild - it says the picture is out of date and
   waits for one click. */
.stale{display:none;background:#332a18;border:1px solid #e8a13a;
       border-radius:6px;padding:.4rem .7rem;color:#f0c078;font-size:12px;
       flex:none}
.stale.on{display:block}
.stale b{color:#ffd79a}
/* What the panel reflects that the design files do not say yet. */
.unsaved{color:#f0c078}
/* THE FAILED VIEWPORT: a board that could not be
   prepared costs the picture and nothing else on the page, so the reason is
   drawn inside the card where the board would have been. Sized like a plot so
   the page keeps its shape, and the card wears the alarm border because it is
   the one thing on screen that is not what it should be. */
.card.failed{border-color:#5a2f2f}
.card.failed .viewport{display:flex;align-items:center;
                       justify-content:center;background:#14161a}
.failbox{max-width:62ch;padding:1.2rem;color:#e6e8eb;
         font:13px/1.5 system-ui,sans-serif}
.failbox .reason{margin:0 0 .5rem;color:#f0a3a3;font-size:14px}
.failbox .where{margin:0 0 .7rem;font:12px ui-monospace,monospace;
                color:#8b939d}
.failbox .where b{color:#c9d1d9}
.failbox .advice{margin:0 0 .9rem;padding-left:1.1rem;color:#c9d1d9}
.failbox .advice li{margin:.15rem 0}
"""

SHARED_JS = r"""
// Everything the controls column knows arrives as JSON and is re-drawn whole.
// One renderer for the first paint and the tenth: the alternative is markup
// built twice, in two languages, that drift.
//
// THIS HALF IS SHARED BY BOTH PAGES: one server, two steps.
// It knows about the busy state, the action gate, the save button, the event
// stream, the layer sync, the unsaved-work prompt and the whole of the session
// instrumentation - everything that is the same whether the picture is a board
// being prepared or a panel being derived. What each page ADDS is its own
// controls and one function, `applyFragment`, which is how a fragment becomes
// that page's own third column.
let PB = __STATE__;

// THE SESSION TOKEN. The server writes it into the page it serves, and every
// POST carries it back in a header. A page from another site cannot read it,
// and a custom header from another site needs a permission check this server
// never grants, so no other page can drive this one's session.
const PB_TOKEN = (document.querySelector('meta[name=pb-token]') || {}).content
                 || '';
const PB_POST_HEADERS = {'Content-Type': 'application/json',
                         'X-Eedesignpanels-Token': PB_TOKEN};

const statusEl = document.getElementById('pb-status');
const genEl = document.getElementById('pb-gen');
const controlsEl = document.getElementById('pb-controls');
const saveEl = document.getElementById('pb-save');
const veilEl = document.getElementById('pb-veil');
const veilWhatEl = document.getElementById('pb-veil-what');
const askEl = document.getElementById('pb-ask');
const askListEl = document.getElementById('pb-ask-list');
const askLineEl = document.getElementById('pb-ask-line');
const askSaveEl = document.getElementById('pb-ask-save');
const askDiscardEl = document.getElementById('pb-ask-discard');
const reportAskEl = document.getElementById('pb-report-ask');
const reportGoEl = document.getElementById('pb-report-go');
const reportDownloadEl = document.getElementById('pb-report-download');
const reportCancelEl = document.getElementById('pb-report-cancel');

// An action writes a board and recomputes, which is most of a second on a real
// board. For that whole window the controls are DEAD - not slow, dead - because
// the alternative is clicks landing on a picture that is about to be replaced,
// and the display jumping once per queued click.
let busy = false;
// Set once the server has been asked to stop, OR once anything this page said
// to the server went unanswered. Everything that would talk to it checks this
// first, so a hotkey pressed on a dead page does nothing at all.
let stopped = false;
// WHAT A DEAD SERVER SAYS, IN WORDS FOR SOMEBODY WHO DOES NOT PROGRAM. The
// browser's own sentence for a closed server is "Failed to fetch", which is
// written for somebody debugging a browser - it names nothing the user did and
// gives nothing to do. This says what happened, what it means for the user's
// work, and what to do next. No browser error text ever reaches the user.
const SERVER_GONE_LINE =
  'the preview server is no longer running - it was closed, or it stopped on '
  + 'its own. Nothing on this page is live: what you see is the last picture '
  + 'it sent, and no control on it does anything now. Anything you saved is on '
  + 'disk and is safe; anything you had not saved is gone. Close this tab, '
  + 'then start the preview again to carry on.';
// The same ending, in the words of a quit the user asked for. The user has
// already answered the unsaved-work question, so nothing here warns again.
const QUIT_LINE =
  'you stopped the preview server. Nothing on this page is live: what you see '
  + 'is the last picture it sent. Everything you saved is on disk. Close this '
  + 'tab, and start the preview again when you want to carry on.';
// Set the moment the user asks for a stop, so a server that dies before it
// answers is reported as the quit it was and not as a server that vanished.
let quitting = false;
// Set when the PAGE is reloading itself. The unsaved-work guard is about the
// user closing a tab, and a refresh this page asked for is neither.
let reloading = false;
// A generation an event announced while we were busy, applied once we are not.
let waiting = 0;
let eventStream = null;
let pollTimer = null;
// Running while the event stream is trying to reconnect. See `streamBroken`:
// declared up here with the other liveness state, because `goQuiet` clears it
// and `goQuiet` is reachable from the first click on the page.
let streamGraceTimer = null;

// Set once the event queue below exists. Until then nothing may journal: the
// queue is a `let`, and reading one before its declaration throws.
let journalReady = false;

function say(text, kind) {
  if (stopped) return;
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
  statusEl.textContent = text;
  // WHAT THE PAGE TOLD THE PERSON is part of the record, so a problem report
  // carries it. Only the refusals: a running commentary of every "saved"
  // would bury the one line a reader is looking for.
  if (journalReady && kind === 'bad') {
    logEvent('said', {text: String(text).slice(0, 300), page: PB.page || 'prep'});
  }
}

// Every control that CHANGES something carries data-act, and carries its own
// intrinsic disabled state in data-off - so re-enabling never turns on an
// accept button for an edge that is already scored.
function applyBusy() {
  document.querySelectorAll('[data-act]').forEach(el => {
    el.disabled = busy || el.dataset.off === '1';
  });
  controlsEl.classList.toggle('busy', busy);
  controlsEl.setAttribute('aria-busy', busy ? 'true' : 'false');
  // The POPUP over the display. The controls being dead says nothing about the
  // picture, and the picture is what is being read.
  veilEl.classList.toggle('on', busy);
  veilEl.setAttribute('aria-hidden', busy ? 'false' : 'true');
}

function setBusy(state, what) {
  busy = !!state;
  if (busy) veilWhatEl.textContent = what || 'recomputing the toolpath';
  applyBusy();
}

// The save button says how much is waiting, so the number is never somewhere
// the user has to go and look for. Each page names its own write - "Save to
// Board" on the prep page, "Save Panel Files" on the panel page - and the
// counting is one function either way.
function drawPending(count, changes) {
  PB.pending = count || 0;
  if (changes !== undefined) PB.pendingChanges = changes || [];
  const many = PB.pending === 1 ? '1 change' : PB.pending + ' changes';
  const label = PB.saveLabel || 'Save to Board';
  const what = PB.saveTitle || 'write the board and the config';
  const idle = PB.saveIdleTitle || 'write the board as it now stands';
  saveEl.textContent = PB.pending ? label + ' (' + many + ')' : label;
  saveEl.classList.toggle('dirty', !!PB.pending);
  saveEl.title = PB.pending ? what + ' - ' + many + ' waiting' : idle;
}

async function post(path, body, what) {
  if (stopped) return null;
  if (busy) {
    // The controls are disabled, so this is a keyboard repeat or a second tab.
    say('one thing at a time - the last action is still running', '');
    return null;
  }
  setBusy(true, what);
  say('working...', '');
  let reply;
  // THE WHOLE OF WHAT THE USER WAITS FOR, from this side of the wire: the
  // reply, and then the paint behind it.
  // The server's record of the same action stops at the reply, so this is the
  // only place the two can be added up.
  const clicked = (performance && performance.now) ? performance.now() : 0;
  const since = () => Math.round(
    ((performance && performance.now) ? performance.now() : 0) - clicked
  );
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: PB_POST_HEADERS,
      body: JSON.stringify(body || {}),
    });
    reply = await response.json();
  } catch (e) {
    // THE SERVER DID NOT ANSWER, SO THE PAGE IS DEAD, rather than leaving
    // every control looking usable. The thrown value is deliberately not
    // read: it is the browser's wording.
    setBusy(false);
    logEvent('action', {endpoint: path, page: PB.page || 'prep',
                        cause: (body || {}).cause || '',
                        ok: false, post_ms: since(), total_ms: since()});
    serverGone('post ' + path);
    return null;
  }
  const replied = since();
  try {
    if (!reply.ok) {
      say(reply.error || 'refused', 'bad');
      logEvent('action', {endpoint: path, page: PB.page || 'prep',
                          cause: (body || {}).cause || '',
                          ok: false, post_ms: replied, total_ms: since()});
      return reply;
    }
    say(reply.message || 'done', 'good');
    if (reply.pending !== undefined) drawPending(reply.pending);
    await refresh('action');
    logEvent('action', {endpoint: path, page: PB.page || 'prep',
                        cause: (body || {}).cause || '',
                        ok: true, post_ms: replied, total_ms: since()});
    return reply;
  } finally {
    setBusy(false);
    // One repaint, at the end, whatever arrived while we were working.
    if (waiting > PB.generation) { waiting = 0; refresh('waited'); }
    else { waiting = 0; }
  }
}

// A DEPENDENT BLOCK IS GREYED, NEVER HIDDEN. The
// block keeps its place and keeps showing the values that would apply, so what
// a governor would switch on can be read before it is switched on. Greyed means
// DISABLED, on every control in the block: a disabled control cannot be tabbed
// into or typed in, and it is not one this page reads into the config.
// `data-off` is the shared idiom for a control's INTRINSIC disabled state, so
// the busy loop (`applyBusy`) never switches a greyed field back on when an
// action finishes.
function setDependent(el, live) {
  if (!el) return;
  el.classList.toggle('off', !live);
  el.querySelectorAll('input,select,textarea,button').forEach(control => {
    control.dataset.off = live ? '' : '1';
    control.disabled = !live;
  });
}

// A DROPDOWN APPLIES WHEN IT IS PICKED FROM. A mouse-driven control has
// nothing to finish typing, so a pick that waited for Enter would do nothing.
// The test is the CONTROL and not the name the page gives its value: a `select`
// is a select whatever kind of thing it carries, so this covers every dropdown
// on both pages and covers the next one by construction.
function appliesOnChange(el) {
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'select') return true;
  return tag === 'input' && (el.type === 'checkbox' || el.type === 'radio');
}

// THE ONE PLACE A SETTING IS WIRED TO ITS PAGE'S APPLY. *apply* is what that
// page does with a change and *cause* is what it calls this one in the journal.
// A dropdown or a switch applies the moment it moves; a typed field applies on
// Enter and on leaving the field having changed it. Enter does not blur a
// number input, so the two never fire for one edit.
function bindField(el, apply, cause) {
  if (!el) return;
  if (appliesOnChange(el)) {
    el.addEventListener('change', () => apply(cause || 'switch'));
    return;
  }
  el.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    apply('enter');
  });
  el.addEventListener('blur', () => apply('blur', true));
}

// -- each page's own controls are spliced in here -----------------------
"""

# The PREP page's own half: the candidate list, the declared numbers, and the
# clickable captions. Spliced between the two shared halves by
# :func:`page_script`, so what a page adds is the third column and nothing else.
PREP_JS = r"""
const candEl = document.getElementById('pb-candidates');
const notesEl = document.getElementById('pb-notes');
const cutterEl = document.getElementById('pb-cutter');
const pastEl = document.getElementById('pb-past');
const alongEl = document.getElementById('pb-along');
const tangentEl = document.getElementById('pb-tangent');
const scoreWidthEl = document.getElementById('pb-scorew');
const minTabEl = document.getElementById('pb-mintab');
const healEl = document.getElementById('pb-heal');
const elimEl = document.getElementById('pb-elim');
const pocketEl = document.getElementById('pb-pocket');

function dotColor(p) {
  if (p.accepted) return '#2ea043';
  return p.proposable ? '#9aa4ae' : '#e8a13a';
}

// THE CANDIDATE LIST IS HELD ON `PB`, AND THIS IS WHERE IT IS PUT THERE.
// A caption click has to decide accept-or-decline from the CURRENT verdict, and
// it looks the candidate up by number in `PB.proposals`. Drawing the rows
// without storing the list would leave that lookup on the page-load snapshot
// for the life of the tab: a candidate scored by a click would stay
// `accepted:false` in it, so the next click would post the same accept again
// and the caption would never come back to PROPOSED. The rows themselves close
// over their own fresh records. One list, set once, read everywhere.
function drawCandidates(proposals) {
  proposals = proposals || [];
  PB.proposals = proposals;
  candEl.textContent = '';
  if (!proposals.length) {
    const none = document.createElement('div');
    none.className = 'cand';
    // A BOARD THAT COULD NOT BE PREPARED READ NO EDGES, and
    // saying no edge could take a blade over it would be a verdict nothing
    // measured. The list stands either way; only the sentence changes.
    none.textContent = PB.ok === false
      ? 'no candidate is read while the board cannot be prepared'
      : 'no edge on this board could take a blade';
    candEl.appendChild(none);
    return;
  }
  proposals.forEach(p => {
    const box = document.createElement('div');
    box.className = 'cand';

    const top = document.createElement('div');
    top.className = 'top';
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.style.background = dotColor(p);
    const sid = document.createElement('span');
    sid.className = 'sid';
    // The number the overlay writes beside the line, and nothing else: a hash
    // prefix never told anyone which line on the picture this row was.
    sid.textContent = p.displayId;
    const vd = document.createElement('span');
    vd.className = 'vd';
    vd.textContent = p.verdict;
    top.append(dot, sid, vd);

    const where = document.createElement('div');
    where.className = 'where';
    where.textContent =
      '(' + p.midpoint[0].toFixed(3) + ', ' + p.midpoint[1].toFixed(3) + ') mm  '
      + p.length_mm.toFixed(3) + ' mm of edge on a '
      + p.extent_mm.toFixed(3) + ' mm line';

    const why = document.createElement('div');
    why.className = 'why';
    why.textContent = p.accepted
      ? 'the board carries this decision on ' + PB.scoreLayerName
      : (p.summary || p.reason || '');

    const row = document.createElement('div');
    row.className = 'row';
    const accept = document.createElement('button');
    accept.className = 'btn go';
    accept.textContent = 'accept';
    accept.dataset.act = 'accept';
    accept.dataset.off = p.accepted ? '1' : '';
    accept.dataset.sid = p.displayId;
    accept.onclick = () => post('/accept', p.blade, 'accepting ' + p.displayId);
    const decline = document.createElement('button');
    decline.className = 'btn no';
    decline.textContent = 'decline';
    decline.dataset.act = 'decline';
    decline.dataset.off = p.accepted ? '' : '1';
    decline.dataset.sid = p.displayId;
    decline.onclick = () => post('/decline', p.blade, 'declining ' + p.displayId);
    row.append(accept, decline);
    if (!p.proposable) {
      const note = document.createElement('span');
      note.className = 'vd';
      note.textContent = 'the tool will not propose this one';
      row.appendChild(note);
    }

    box.append(top, where, why, row);
    candEl.appendChild(box);
  });
  // These buttons are new elements, so they arrive not knowing about a run in
  // flight. Told immediately, in the same frame they were created in.
  applyBusy();
}

// Alarms first and marked, because they are why the cutter path is drawn in the
// alarm colour: a red picture with no sentence beside it is a puzzle.
// THE RUN NOTES ARE NOT ON THE PAGE, exactly as the panel page's two boxes
// are not: a collapsed box nobody opens adds nothing. The server still builds
// every line and the problem-report zip carries them, so this lookup finds
// nothing and the lines go to the developer console instead.
function drawNotes(warnings, alarms) {
  const said = (alarms || []).map(t => 'THIS TOOLPATH MUST NOT BE CUT: ' + t)
                             .concat(warnings || []);
  if (!notesEl) {
    if (said.length) console.debug('run notes: ' + said.join(' | '));
    return;
  }
  notesEl.textContent = '';
  (alarms || []).forEach(text => {
    const item = document.createElement('li');
    item.className = 'alarm';
    item.textContent = 'THIS TOOLPATH MUST NOT BE CUT: ' + text;
    notesEl.appendChild(item);
  });
  (warnings || []).forEach(text => {
    const item = document.createElement('li');
    item.textContent = text;
    notesEl.appendChild(item);
  });
}

function drawRouteEnds(ends) {
  const select = document.getElementById('pb-route-end');
  if (!select) return;
  const previous = select.value;
  const enabled = !!PB.params.custom_route_ends;
  const toggle = document.getElementById('pb-custom-route-ends');
  if (toggle) toggle.checked = enabled;
  const fields = document.getElementById('pb-route-fields');
  fields.disabled = !enabled;
  fields.style.opacity = enabled ? '1' : '0.45';
  document.querySelectorAll('.route-end-marker').forEach(item => item.remove());
  select.replaceChildren();
  ends.forEach((end, index) => {
    const option = document.createElement('option');
    option.value = end.end_id;
    option.textContent = end.layer + ' end ' + (index + 1) + ' at ' + end.at.map(v => v.toFixed(3)).join(', ') + ' mm';
    select.appendChild(option);
  });
  if (ends.some(end => end.end_id === previous)) select.value = previous;
  select.onchange = () => {
    const value = (PB.params.route_end_overrides || {})[select.value];
    document.getElementById('pb-route-mode').value = value == null ? 'inherit' : value === 0 ? 'none' : 'custom';
    document.getElementById('pb-route-distance').value = value == null ? 0 : value;
    document.querySelectorAll('.route-end-marker').forEach(marker => {
      marker.setAttribute('stroke', enabled && marker.dataset.end === select.value ? '#ff55ff' : '#909090');
    });
  };
  document.querySelectorAll('.viewport').forEach(viewport => {
    const svg = viewport.querySelector('svg[data-overlay]') || viewport.querySelector('svg[data-layer="User.36"]');
    if (!svg) return;
    ends.forEach(end => {
      const marker = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      marker.setAttribute('class', 'route-end-marker');
      marker.dataset.end = end.end_id;
      marker.setAttribute('cx', end.at[0]); marker.setAttribute('cy', end.at[1]);
      marker.setAttribute('r', '0.8'); marker.setAttribute('fill', 'none');
      marker.setAttribute('stroke-width', '0.2');
      marker.setAttribute('opacity', enabled ? '1' : '0.35');
      marker.style.pointerEvents = enabled ? 'all' : 'none';
      marker.style.cursor = enabled ? 'pointer' : 'default';
      marker.addEventListener('pointerdown', event => event.stopPropagation());
      marker.addEventListener('click', event => {
        event.stopPropagation();
        if (!enabled) return;
        select.value = end.end_id;
        select.onchange();
        paintFold(document.getElementById('pb-sec-router'), false);
      });
      svg.appendChild(marker);
    });
  });
  select.onchange();
}
const routeToggle = document.getElementById('pb-custom-route-ends');
if (routeToggle) routeToggle.onchange = () => post('/set_param', {custom_route_ends: routeToggle.checked}, 'updating custom route ends');
const routeApply = document.getElementById('pb-route-apply');
if (routeApply) routeApply.onclick = () => {
  const end = document.getElementById('pb-route-end').value;
  if (!end) return;
  const mode = document.getElementById('pb-route-mode').value;
  const values = {...(PB.params.route_end_overrides || {})};
  if (mode === 'inherit') delete values[end];
  else {
    const value = mode === 'none' ? 0 : Number(document.getElementById('pb-route-distance').value);
    if (!Number.isFinite(value) || value < 0) return;
    values[end] = value;
  }
  post('/set_param', {route_end_overrides: values}, 'updating route end');
};
const routeDistance = document.getElementById('pb-route-distance');
if (routeDistance) {
  routeDistance.addEventListener('input', () => {
    document.getElementById('pb-route-mode').value = 'custom';
  });
  routeDistance.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    event.stopPropagation();
    if (routeDistance.value.trim() && routeDistance.checkValidity()) routeApply.click();
  });
}
const routeReset = document.getElementById('pb-route-reset');
if (routeReset) routeReset.onclick = () => post('/set_param', {route_end_overrides: {}}, 'clearing route end overrides');

// The declared numbers, as the config now reads them. Held on PB so a field can
// be compared against what is actually in the file rather than against what it
// said last time it was drawn.
// The declared settings and the fields that carry them - four numbers and one
// switch. Written out in both functions rather than shared through a const, so
// each one can be lifted out on its own and run against a stated DOM.
function drawParams(params) {
  PB.params = params || {};
  [[cutterEl, 'cutter_diameter'], [pastEl, 'route_past_score_mm'],
   [alongEl, 'route_along_score_mm'], [tangentEl, 'route_tangent_angle_deg'],
   [scoreWidthEl, 'score_rule_half_width_mm'],
   [minTabEl, 'min_tab_mm'], [healEl, 'outline_heal_mm']].forEach(pair => {
    if (pair[0] && document.activeElement !== pair[0]
        && PB.params[pair[1]] != null) {
      pair[0].value = PB.params[pair[1]];
    }
  });
  // A SWITCH IS WRITTEN EVEN WHEN IT IS FALSE. False is a value the config
  // states and null is a value it does not; collapsing the two would leave a
  // box ticked that the file says is clear.
  if (elimEl && document.activeElement !== elimEl
      && PB.params.eliminate_slivers != null) {
    elimEl.checked = !!PB.params.eliminate_slivers;
  }
  const plating = document.getElementById('pb-edge-plating');
  if (plating) plating.checked = !!PB.params.edge_plating;
  ['edge_plating_front_mask', 'edge_plating_back_mask', 'lock_board_on_save'].forEach(name => {
    const control = document.getElementById('pb-' + name);
    if (control) control.checked = !!PB.params[name];
  });
  const maskWidth = document.getElementById('pb-edge_plating_mask_width_mm');
  if (maskWidth && document.activeElement !== maskWidth)
    maskWidth.value = PB.params.edge_plating_mask_width_mm;
  // POCKETING, ON THE SAME TERMS. False is a value
  // the config states, so it is drawn; the box is clear by default and clear is
  // what most board houses want, because they do that CAM job themselves.
  if (pocketEl && document.activeElement !== pocketEl
      && PB.params.pocket_cutouts != null) {
    pocketEl.checked = !!PB.params.pocket_cutouts;
  }
}

// What the fields say that the config does not. A number the config already
// carries is not a change and costs no recompute.
function changedParams() {
  const body = {};
  [[cutterEl, 'cutter_diameter'], [pastEl, 'route_past_score_mm'],
   [alongEl, 'route_along_score_mm'], [tangentEl, 'route_tangent_angle_deg'],
   [scoreWidthEl, 'score_rule_half_width_mm'],
   [minTabEl, 'min_tab_mm'], [healEl, 'outline_heal_mm']].forEach(pair => {
    if (!pair[0]) return;
    const value = parseFloat(pair[0].value);
    if (isFinite(value) && value !== PB.params[pair[1]]) body[pair[1]] = value;
  });
  if (elimEl && elimEl.checked !== !!PB.params.eliminate_slivers) {
    body.eliminate_slivers = elimEl.checked;
  }
  if (pocketEl && pocketEl.checked !== !!PB.params.pocket_cutouts) {
    body.pocket_cutouts = pocketEl.checked;
  }
  return body;
}

// Enter applies. So does the button, and so does leaving a field whose value
// changed - three ways in, one path, because a number typed and not applied is
// a preview showing something the user did not ask for.
async function applyParams(cause, quiet) {
  if (busy || stopped) return null;
  const body = changedParams();
  if (!Object.keys(body).length) {
    if (!quiet) say('those numbers are already what panel.json says', '');
    return null;
  }
  logEvent('param', Object.assign({cause: cause}, body));
  return post('/set_param', body, 'applying ' + Object.keys(body).join(', '));
}

// THE FAILED VIEWPORT, KEPT TRUE. The reason is
// drawn where the board would have been, so a second failure with a different
// reason has to redraw it - and a board that fails after the page was served
// working has no such card to redraw, which is the one case that reloads.
function drawFailure(fragment) {
  const box = document.getElementById('pb-failed-reason');
  if (!box) { selfReload('the board stopped being preparable'); return; }
  const at = fragment.error_at;
  const where = document.getElementById('pb-failed-where');
  let reason = String(fragment.error || '');
  if (where && at && at.length === 2) {
    where.textContent = Number(at[0]).toFixed(4) + ', '
                      + Number(at[1]).toFixed(4) + ' mm';
    // The place has its own line, so it comes off the end of the sentence.
    reason = reason.replace(/\s+at \(-?[\d.]+, -?[\d.]+\) mm\.?$/, '');
  }
  box.textContent = reason || 'the reason was not recorded';
}

// One fragment, this page's third column. The shared half draws everything
// both pages carry and calls this for the rest.
function applyFragment(fragment) {
  PB.ok = !!fragment.ok;
  // EVERY SETTING STAYS LIVE ON A FAILED LOAD:
  // the fields carry what the config holds, a change to one is accepted, and
  // the recompute it triggers is the retry. So the numbers are drawn on both
  // paths - drawn only on the good one, a field would have gone on showing the
  // value the failed computation was asked for.
  drawParams(fragment.params);
  drawRouteEnds(fragment.route_ends || []);
  if (!fragment.ok) {
    drawCandidates([]);
    drawNotes([]);
    drawFailure(fragment);
    return;
  }
  drawCandidates(fragment.proposals);
  drawNotes(fragment.warnings, fragment.alarms);
}

// =========================================================================
// The captions on the picture are controls.
// =========================================================================
// A candidate's caption toggles it between SCORED and PROPOSED, and it posts
// the SAME two actions the accept and decline buttons post - one action path,
// one journal line, and the buttons stay exactly where they were. A REJECTED
// candidate is drawn without a target at all, so there is nothing to click and
// nothing that looks clickable.
//
// The stacked SVGs are `pointer-events:none` so that a press anywhere on the
// picture reaches the viewport and pans; the caption groups take pointer events
// back on their own (see CONTROLS_CSS). A press that MOVED is a pan and never a
// click, however it ends up, so the pointer is remembered and a gesture that
// travelled further than this many pixels is ignored.
//
// THE GESTURE IS READ AT POINTERDOWN.
// The viewport takes POINTER CAPTURE on pointerdown - it has to, or a press
// that lands on an earlier selection starts a native drag and panning locks up
// (review.page) - and a captured pointer retargets every later event of that
// gesture, the `click` included, to the CAPTURE ELEMENT. A click would arrive
// at `.viewport`, `closest('.pb-hit')` would find nothing above it, and a
// caption that highlights on hover would do nothing when pressed. The
// caption is therefore read at POINTERDOWN, which is the last moment the event
// still names it, and the toggle is taken on POINTERUP, which arrives whatever
// the browser decides to do about `click`.
//
// TRAVEL IS SCREEN PIXELS. `clientX`/`clientY` are CSS pixels and do not scale
// with devicePixelRatio, so this threshold means the same thing on a 1.5x
// display as on a 1x one.
const CLICK_SLOP_PX = 4;
// The press this gesture started on: where it was, and the caption under it.
let pressAt = null;
// Set when a pointerup took the toggle, so the click a browser fires after it
// cannot take the same toggle a second time.
let clickTaken = false;

function labelAction(p) {
  if (!p || !p.proposable) return '';
  return p.accepted ? 'decline' : 'accept';
}

function proposalNumbered(number) {
  return (PB.proposals || []).find(p => p.number === number) || null;
}

// Looked up by NUMBER in the state the picture was drawn from, so the body that
// travels is the candidate's own blade line - the identical object the button
// sends.
function clickLabel(number, cause) {
  const p = proposalNumbered(number);
  const act = labelAction(p);
  if (!act) return null;
  logEvent('control', {
    id: 'pb-label', label: p.displayId + ' ' + p.verdict,
    candidate: p.displayId, where: 'label', disabled: false,
    cause: cause || 'label', action: act,
  });
  return post('/' + act, p.blade,
              (act === 'accept' ? 'accepting ' : 'declining ') + p.displayId);
}

function hitUnder(event) {
  return event.target && event.target.closest
    ? event.target.closest('.pb-hit') : null;
}

// How far the pointer travelled, in screen pixels, between the press and now.
function travelPx(from, event) {
  return Math.hypot(event.clientX - from.x, event.clientY - from.y);
}

// The press arms the gesture: which caption it landed on, and where it was.
// Anything but the primary button arms nothing.
function pressLabel(event) {
  clickTaken = false;
  if (event.button !== undefined && event.button !== 0) { pressAt = null; return; }
  const hit = hitUnder(event);
  pressAt = {x: event.clientX, y: event.clientY,
             number: hit ? Number(hit.dataset.number) : 0};
}

// The release takes the toggle, or does not: no caption under the press, or a
// press that travelled, is a pan and leaves the candidate alone.
function releaseLabel(event, cause) {
  const press = pressAt;
  if (!press || !press.number) return null;
  if (travelPx(press, event) > CLICK_SLOP_PX) return null;
  if (event.preventDefault) event.preventDefault();
  return clickLabel(press.number, cause);
}

function wireLabels(root) {
  if (!root) return;
  // All three in the CAPTURE phase, so nothing between the caption and this
  // root can swallow them - and pointerup is what actually decides, because a
  // captured pointer sends the click somewhere else entirely.
  root.addEventListener('pointerdown', pressLabel, true);
  root.addEventListener('pointerup', event => {
    if (releaseLabel(event, 'label')) clickTaken = true;
  }, true);
  // The fallback, for a gesture that produced a click and no pointerup we saw -
  // an assistive click, or a browser with no pointer events at all.
  root.addEventListener('click', event => {
    const taken = clickTaken;
    clickTaken = false;
    if (taken) return;
    releaseLabel(event, 'label click');
    pressAt = null;
  }, true);
}
wireLabels(document.getElementById('views'));

document.getElementById('pb-apply').onclick = () => applyParams('button');
const platingEl = document.getElementById('pb-edge-plating');
['edge_plating_front_mask', 'edge_plating_back_mask', 'lock_board_on_save',
 'edge_plating_mask_width_mm'].forEach(name => {
  const control = document.getElementById('pb-' + name);
  control.onchange = () => post('/set_param', {
    [name]: control.type === 'checkbox' ? control.checked : Number(control.value),
  }, 'updating settings');
});
if (platingEl) platingEl.onchange = () =>
  post('/set_param', {edge_plating: platingEl.checked}, 'updating edge plating');
document.getElementById('pb-generate-edge-plating').onclick = () =>
  post('/generate_edge_plating', {}, 'generating edge plating');
document.getElementById('pb-remove-edge-plating').onclick = () =>
  post('/remove_edge_plating', {}, 'removing edge plating');
// RELOAD ALWAYS RE-READS, so held work is asked about first.
function askReload() {
  if (stopped) return;
  if (PB.pending > 0) { askUnsaved('reload'); return; }
  reloadFromDisk();
}
document.getElementById('pb-reload').onclick = askReload;
// TRY AGAIN, IN THE FAILED VIEWPORT. It is the
// Reload control under another name and in the place a stuck person is looking:
// the usual fix for a board that will not prepare is an edit in KiCad, so the
// retry re-reads the board and the settings from disk and asks the same
// unsaved-work question first. A number changed on the right needs no button -
// it recomputes on its own.
const againEl = document.getElementById('pb-failed-again');
if (againEl) againEl.onclick = () => { logEvent('again', {}); askReload(); };
saveEl.onclick = () => save('button');

// EVERY SETTING ON THIS PAGE IS WIRED THE SAME WAY, through the shared
// `bindField`: a number applies on Enter and on
// leaving the field having changed it, a switch or a dropdown applies the
// moment it moves. One wiring for both pages, so a control that applies here
// applies over there too and neither page can forget half of it.
[cutterEl, pastEl, alongEl, tangentEl, scoreWidthEl, minTabEl, healEl, elimEl, pocketEl].forEach(
  el => bindField(el, applyParams)
);

drawCandidates(PB.proposals);
drawNotes(PB.warnings, PB.alarms);
drawParams(PB.params);
"""

SHARED_TAIL = r"""
// A rebuilt picture is often a different SIZE - a panel two columns wider is
// the ordinary case - and the frame a viewport draws in comes from the render's
// own extents. So a fragment carries the frame it was rendered in and the
// viewports take it in place (`review.page`'s `viewFrames`). That, plus the
// content swap below, is what makes a build a REPAINT and not a page reload.
// The readout under a viewport, redrawn with the picture: a panel two columns
// wider measures differently, and a number that did not follow it would be the
// one thing on screen still describing the last build.
function drawExtents(text) {
  if (!text) return;
  document.querySelectorAll('.hud b.ext').forEach(el => {
    el.textContent = text;
  });
}

// The fragment address, carrying the paint this page is holding. One place, so
// the two pages cannot ask two different ways.
function fragmentUrl(painted) {
  const url = PB.fragmentUrl || '/fragment';
  if (!painted) return url;
  return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'painted=' + painted;
}

function rebaseViews(base) {
  if (!base || typeof viewFrames === 'undefined' || !viewFrames.length) {
    return false;
  }
  const box = String(base).trim().split(/[\s,]+/).map(Number);
  let moved = false;
  viewFrames.forEach(frame => { if (frame.rebase(box)) moved = true; });
  return moved;
}

// The picture is updated by swapping each layer's CONTENTS, so the pan/zoom
// closures keep the nodes they were bound to and the view survives. A change to
// the layer SET is the one case that cannot be done in place - the layer panel's
// list is baked at build time - and it reloads instead, through `selfReload` so
// that a refresh the PAGE asked for never raises the unsaved-work prompt.
//
// An OVERLAY svg wears `data-overlay` and is NOT a layer: it carries annotation
// the page draws over the plots - the clearance marking today - so it is
// neither swapped here nor counted against the layer set. It still lives inside
// the viewport, so the pan/zoom closure drives its viewBox with every layer's.
function paintSvgs(svgs, layers) {
  const present = [...document.querySelectorAll(
      '.viewport svg:not([data-overlay])')].map(el => el.dataset.layer);
  const same = layers.length === present.length
    && layers.every(name => present.includes(name));
  if (!same) { selfReload('the layer set changed'); return false; }
  Object.entries(svgs).forEach(([name, markup]) => {
    const el = document.querySelector(
      '.viewport svg[data-layer="' + CSS.escape(name) + '"]');
    if (el) el.innerHTML = markup;
  });
  return true;
}

// Each page fetches its OWN fragment and hands it to its own `applyFragment`.
// Everything above that line is the same on both: the generation, the pending
// count, the sentence, and the stack of layer pictures.
//
// AND IT IS TIMED, PHASE BY PHASE, because interaction latency is close to
// too long and only a breakdown says where it goes. The server's own chain
// ends at the reply; this is
// the other half of an interaction, and it is the larger half - a panel
// fragment is megabytes of markup, and the fetch, the parse and the swap into
// the document are three different costs with three different fixes. Every
// record carries the generation, which is what joins the two halves in the
// journal (`preview.timing`).
async function refresh(cause) {
  if (stopped) return;
  const t0 = (performance && performance.now) ? performance.now() : 0;
  const mark = () => ((performance && performance.now) ? performance.now() : 0);
  const paintedIn = {};
  let bytes = 0, sent = 0;
  const notePaint = (reason) => {
    paintedIn.total_ms = Math.round(mark() - t0);
    // WHICH PAGE PAID IT. Both tabs repaint on a new generation, and they are
    // two different pictures with two different costs - a table that added
    // them together would name neither.
    logEvent('paint', Object.assign({cause: cause || '', reason: reason,
                                     page: PB.page || 'prep',
                                     bytes: bytes, layers: sent}, paintedIn));
  };
  // WHAT THIS PAGE ALREADY HAS, so the answer carries what has moved and not
  // the forty layers the edit could not have touched. Zero - a page that has
  // just loaded - asks for the whole stack, which is what every page used to
  // get every time.
  // THE FRAGMENT FETCH IS FENCED. Without a guard, a server that went away
  // during a repaint would throw out of an async function and show the user an
  // unhandled rejection in the browser's own words. A fragment that cannot be
  // fetched, read or parsed all mean the same thing to the person reading the page: the server is gone.
  let fetched, read, body, fragment;
  try {
    const response = await fetch(fragmentUrl(PB.paintSeq || 0));
    fetched = mark();
    body = await response.text();
    read = mark();
    fragment = JSON.parse(body);
  } catch (e) {
    serverGone('fragment');
    return;
  }
  paintedIn.fetch_ms = Math.round(fetched - t0);
  paintedIn.read_ms = Math.round(read - fetched);
  paintedIn.parse_ms = Math.round(mark() - read);
  bytes = body.length;
  sent = Object.keys(fragment.svg || {}).length;
  const words = mark();
  PB.generation = fragment.generation;
  genEl.textContent = 'gen ' + fragment.generation;
  drawPending(fragment[PB.pendingKey || 'pending'], fragment.pending_changes);
  // Which phase this picture is in, and therefore what its corner says. Read
  // before the ok test: a fragment that failed still has a phase.
  PB.upgradeSeq = fragment.upgrade_seq || 0;
  drawAwaiting(fragment);
  // RED SURVIVES THE NEXT PAINT. Read before the
  // ok test and on EVERY fragment, the ones that changed nothing included: the
  // state is the session's and this is a paint honouring it, so a background
  // rebuild cannot arrive and quietly drop the warning.
  drawLayerAlarms(fragment);
  if (!fragment.ok) {
    say(fragment.error || PB.notReady || 'the board could not be prepared',
        'bad');
    applyFragment(fragment);
    notePaint('not-ok');
    return;
  }
  // NO SUBTITLE IN THE HEADER. The fragment carries `subtitle`, and the
  // journal and the bug report record it; the page has nowhere to draw it.
  // The frame first, then the contents: the picture arrives already fitted to
  // whatever it now measures, rather than being drawn in the last one's window
  // for a frame.
  rebaseViews(fragment.base);
  drawExtents(fragment.extent);
  const framed = mark();
  paintedIn.words_ms = Math.round(framed - words);
  if (!paintSvgs(fragment.svg, fragment.layers)) { notePaint('reload'); return; }
  // THE PAINT THIS PAGE NOW HOLDS, recorded once the markup is actually in the
  // document and never walked back. Set before the swap it would be a claim
  // about a picture this page has not got, and the next fetch would then skip
  // a layer that never arrived.
  if (fragment.paint_seq && fragment.paint_seq > (PB.paintSeq || 0)) {
    PB.paintSeq = fragment.paint_seq;
  }
  paintedIn.svg_ms = Math.round(mark() - framed);
  const applied = mark();
  applyFragment(fragment);
  if (typeof viewer !== 'undefined' && viewer) viewer.paint();
  paintedIn.apply_ms = Math.round(mark() - applied);
  notePaint('painted');
}

// A reload the PAGE asked for is not the user closing a tab, and it may not
// be answered with the browser's "leave site?" box. Nothing
// can be lost: every pending edit is held by the SERVER, and the page reads the
// count straight back on the way in. The guard below stands down for exactly
// this and for nothing else.
function selfReload(why) {
  reloading = true;
  logEvent('page.reload', {why: why || ''});
  flushEvents(true);
  location.reload();
}

// THE DESIGN's write, and it is the same on both pages: the board, and the
// config when a number moved. Everything else either page does happens in
// memory. The panel page's own button writes the DERIVED files instead
// (`savePanel`), and the unsaved-work prompt asks for THIS one on both pages -
// a panel is regenerable from the board and the config, so it is not work that
// can be lost.
async function save(cause) {
  return post('/save', {cause: cause || 'button'}, 'writing the board');
}

// Everything that talks to the server, silenced in one place. Called before the
// document is replaced, so nothing is left holding a socket or a timer.
function goQuiet() {
  stopped = true;
  if (eventStream) {
    try { eventStream.close(); } catch (e) { /* already gone */ }
    eventStream = null;
  }
  if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
  if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
  streamAlive();
}

// EVERY FAILED REQUEST ENDS HERE. A fetch that
// throws, a reply that cannot be read, a request that cannot be answered, a
// dropped event stream - one ending for all of them, and it is the ending a
// deliberate quit already produced: the page is cleared and it says so. The
// browser's exception is never printed, so a user who closed the server does
// not get "Failed to fetch".
//
// *where* is for the journal, never for the screen. It will not reach the
// server on a server that has gone, but a server that is merely wedged is a
// case worth a line, so the flush is attempted before the page goes quiet.
function serverGone(where) {
  // A page reloading itself aborts its own requests. That is this page's doing
  // and not a server that died, so it never raises the dead page.
  if (stopped || reloading) return;
  // The queue is a `let` further down the file and reading one before its
  // declaration throws, so the journal is asked only once it exists - the same
  // fence `say` carries.
  if (journalReady) {
    logEvent('server.gone', {where: where || '', page: PB.page || 'prep'});
    flushEvents(true);
  }
  deadPage(quitting ? QUIT_LINE : SERVER_GONE_LINE);
}

// The fallback for a browser that will not close a tab it did not open - which
// is most of them, for a page the user typed or clicked a way to. The
// whole document goes, so there is no control left to press and no doubt about
// whether the page is still driving anything.
// THE FIRST ENDING WINS. Several paths race to end the
// page - a quit, a dropped stream, a request nobody answered - and the pending
// 250 ms timers do not know about each other. Without this, the sentence the
// user reads is whichever one landed last rather than the one that names
// what actually happened.
let dead = false;

function deadPage(reason) {
  if (dead) return;
  dead = true;
  goQuiet();
  document.title = 'preview server stopped';
  // THE NOTICE IS NOT CALLED `dead`. A `const` of that name inside this
  // function would shadow the module's `dead` flag for the WHOLE function - so
  // the `if (dead)` on the first line would read a variable in its temporal
  // dead zone and this function would throw before it drew anything. One
  // name, one meaning.
  const notice = document.createElement('div');
  notice.className = 'dead';
  const head = document.createElement('h1');
  head.textContent = 'the preview server has stopped';
  const line = document.createElement('p');
  line.textContent = reason || 'nothing on this page is live - close this tab.';
  const where = document.createElement('p');
  const code = document.createElement('code');
  code.textContent = PB.session || '';
  where.append(code);
  notice.append(head, line, where);
  document.body.textContent = '';
  document.body.appendChild(notice);
}

// =========================================================================
// THE TWO TABS: one session, and one open tab keeps it.
// =========================================================================
// This page adopts its step's stable window name the moment it loads, which is
// what lets the OTHER page's link find it. A tab the operating system's browser
// call opened has no name of its own; adopting one costs nothing and is the
// only chance to make it addressable, because nothing else can name it later.
try { if (PB.stepWindow) window.name = PB.stepWindow; } catch (e) { /* fine */ }

// Windows this page opened, by name. A handle we hold is proof the window is
// reachable from here, which is the one case where `window.open` is guaranteed
// to REUSE the name rather than make a second window.
const stepWindows = {};

// Is the other step open? The SERVER answers, because it is the only thing that
// can see both tabs: each page holds an event stream and that stream IS its
// presence. This is what keeps a click from spawning a duplicate of a page that
// is already on screen in a window this one cannot reach.
async function stepIsOpen(page) {
  if (stopped) return false;
  try {
    const state = await (await fetch('/state')).json();
    return !!(state.pages && state.pages[page]);
  } catch (e) {
    // The only thing that can see both tabs has stopped answering, so there is
    // nothing to jump to and nothing left to ask. Answering "no" here would
    // open a second tab against a dead server.
    serverGone('state');
    return false;
  }
}

// A window we already hold, brought forward. Returns '' when there is none, so
// the caller knows it still has a decision to make.
function focusStep(step) {
  const held = stepWindows[step.window];
  if (!held || held.closed) return '';
  // A named target that exists is reused, never duplicated: this re-aims the
  // same window at the same address and raises it.
  const win = window.open(step.path, step.window);
  const raise = win || held;
  try { if (raise && raise.focus) raise.focus(); } catch (e) { /* refused */ }
  if (win) stepWindows[step.window] = win;
  return 'focused';
}

// THE STEP LINK: jump, never spawn.
//
// Three answers, in order. A window this page opened is focused. A step the
// server says is open is NOT opened again - the browser will not let a page
// raise a tab it is not related to, so the user is told where it is rather
// than handed a third copy of it. Only a step that nothing is holding is
// opened, and it is opened WITH ITS NAME, so every click after that one finds
// it.
async function jumpToStep(step) {
  if (stopped || !step) return '';
  const focused = focusStep(step);
  if (focused) { logEvent('step.focus', {step: step.page}); return focused; }
  if (await stepIsOpen(step.page)) {
    say(step.label + ' is already open - it is in another tab of this window',
        '');
    logEvent('step.already_open', {step: step.page});
    return 'already-open';
  }
  const win = window.open(step.path, step.window);
  if (win) stepWindows[step.window] = win;
  logEvent('step.open', {step: step.page});
  return 'opened';
}

function wireSteps() {
  document.querySelectorAll('a[data-step-window]').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      jumpToStep({
        window: link.dataset.stepWindow, path: link.dataset.stepPath,
        page: link.dataset.stepPage,
        label: (link.textContent || '').trim(),
      });
    });
  });
}

// The server is going, and it said so rather than simply stopping: somebody
// pressed stop, or the run behind it ended.
//
// THIS PAGE NEVER DIES QUIETLY. It does not try `window.close()` here: a
// browser that allows the close takes the reason with it, and the tab the user
// was reading disappears with no explanation. A page that is told the session
// is over SAYS SO and stays. Only the stop button on this page closes this
// page, because the user pressed it and already knows why.
function serverClosing(reason) {
  if (stopped) return;
  logEvent('server.closing', {reason: reason || ''});
  flushEvents(true);
  goQuiet();
  deadPage(
    reason ? 'the preview server stopped: ' + reason
           : 'the preview server stopped. Nothing on this page is live.'
  );
}

async function stopServer() {
  say('stopping the preview server...', '');
  flushEvents(false);
  // ASKED TO STOP IS NOT THE SAME AS DIED. A server that
  // goes down before it can answer its own shutdown request takes `send` into
  // `serverGone`, and the user who just pressed quit would be told the server
  // "is no longer running - it was closed, or it stopped on its own". True,
  // and the wrong sentence for somebody who closed it. The flag lets
  // `serverGone` answer in the words of the thing the user did.
  quitting = true;
  const reply = await send('/shutdown', {});
  if (reply && reply.ok === false) {
    quitting = false;
    say(reply.error || 'the server refused to stop', 'bad');
    return false;
  }
  goQuiet();
  // Close the tab if this browser allows it; the dead page is what the
  // user sees if it does not. The timer never fires on a window that went.
  try { window.close(); } catch (e) { /* refused - the fallback covers it */ }
  setTimeout(() => deadPage(QUIT_LINE), 250);
  return true;
}

// RELOAD: re-read the board and the config from disk.
// The button's whole job is picking up an edit made in KiCad, so it always
// re-reads - and it therefore stands behind the same unsaved-work question
// quitting does, because a reload with edits outstanding loses them.
async function reloadFromDisk() {
  return post('/reload', {}, 're-reading the board and the config');
}

// THE UNSAVED-WORK PROMPT. Three answers, so it is a dialog and not a browser
// confirm: "discard" is the one a two-button box cannot offer, and it is the
// one that loses work if it is guessed at.
//
// TWO CONTROLS ASK IT: Quit Panelizer and Reload. The verb in the
// three buttons and in the sentence above them changes with the one that asked,
// so nobody answers a question about quitting while reloading.
const ASKS = {
  quit: {
    verb: 'quit',
    line: 'Quitting the panelizer now would lose them. The board and the '
        + 'config on disk have not been touched yet.',
    go: () => stopServer(),
    stay: 'still running - nothing was written',
  },
  reload: {
    verb: 'reload',
    line: 'Reloading reads the board and the config off the disk again, which '
        + 'would lose them. Nothing on disk has been touched yet.',
    go: () => reloadFromDisk(),
    stay: 'nothing was re-read - the changes are still held',
  },
};
let asking = ASKS.quit;

function askUnsaved(kind) {
  asking = ASKS[kind] || ASKS.quit;
  askLineEl.textContent = asking.line;
  askSaveEl.textContent = 'save and ' + asking.verb;
  askDiscardEl.textContent = 'discard and ' + asking.verb;
  askListEl.textContent = '';
  (PB.pendingChanges || []).slice(0, 8).forEach(text => {
    const item = document.createElement('li');
    item.textContent = text;
    askListEl.appendChild(item);
  });
  askEl.classList.add('on');
  askEl.setAttribute('aria-hidden', 'false');
  logEvent('prompt', {kind: 'unsaved', asked: asking.verb,
                      pending: PB.pending});
}

function closeAsk() {
  askEl.classList.remove('on');
  askEl.setAttribute('aria-hidden', 'true');
}

askSaveEl.onclick = async () => {
  const answered = asking;
  closeAsk();
  logEvent('prompt.answer', {answer: 'save and ' + answered.verb});
  const reply = await save(answered.verb);
  if (reply && reply.ok === false) return;
  answered.go();
};
askDiscardEl.onclick = () => {
  const answered = asking;
  closeAsk();
  logEvent('prompt.answer', {answer: 'discard and ' + answered.verb});
  answered.go();
};
document.getElementById('pb-ask-cancel').onclick = () => {
  const answered = asking;
  closeAsk();
  logEvent('prompt.answer', {answer: 'cancel'});
  say(answered.stay, '');
};

document.getElementById('pb-stop').onclick = () => {
  if (stopped) return;
  if (PB.pending > 0) { askUnsaved('quit'); return; }
  stopServer();
};

// =========================================================================
// The layer panel is one panel, on two pages.
// =========================================================================
// Which layers are visible, which one is active and how the inactive ones draw
// belong to the SESSION, not to a tab: the two pictures are read against each
// other, so switching a layer off on one has to switch it off on the other. It
// travels through the server, on the event stream both pages already hold.
//
// LAST CHANGE WINS, and there is nothing cleverer to do: two people are not
// using this at once, and the page that spoke most recently is the one whose
// hand is on it.
//
// A page speaks only for the layers IT HAS. The two pages' lists are nearly the
// same but not identical - the prep page carries the candidate overlay, which
// no panel has - so the state names its own layers and the receiver leaves
// anything not on that list exactly as it was.
let layersSeen = PB.layersSeq || 0;

function panelLayerState() {
  if (typeof viewer === 'undefined' || !viewer || typeof LAYERS === 'undefined'
      || !LAYERS.length) {
    return null;
  }
  const active = LAYERS[viewer.active];
  return {
    page: PB.page || 'prep',
    mode: viewer.mode,
    active: active ? active.canonical : '',
    known: LAYERS.map(l => l.canonical),
    visible: LAYERS.map(l => l.canonical)
                   .filter(name => viewer.visible.has(name)),
  };
}

function sameLayerState(one, other) {
  if (!one || !other) return false;
  return JSON.stringify([one.mode, one.active, one.visible])
      === JSON.stringify([other.mode, other.active, other.visible]);
}

// Applied without a repaint of the pictures: a layer toggle is not a
// recomputation and must never cost one.
function applyLayerState(state) {
  const mine = panelLayerState();
  if (!state || !mine || sameLayerState(state, mine)) return false;
  const known = new Set(state.known || state.visible || []);
  const shown = new Set(state.visible || []);
  LAYERS.forEach(layer => {
    if (!known.has(layer.canonical)) return;
    if (shown.has(layer.canonical)) viewer.visible.add(layer.canonical);
    else viewer.visible.delete(layer.canonical);
  });
  const at = LAYERS.findIndex(l => l.canonical === state.active);
  if (at >= 0) viewer.active = at;
  if (typeof state.mode === 'number') viewer.mode = state.mode;
  // So this page's own change detector does not read the applied state as a
  // change and send it straight back.
  lastLayers = JSON.stringify(layerState());
  viewer.paint();
  return true;
}

function pushLayers() {
  const mine = panelLayerState();
  if (!mine || stopped) return;
  send('/layers', mine);
}

// Change push: server-sent events, so a change made anywhere - another tab, a
// reload, the user's own click - reaches every open page without polling.
// A browser that has no EventSource falls back to asking. Either way, a change
// arriving DURING an action is remembered and painted once, at the end: a page
// that repaints mid-action is the jumping the busy state exists to stop.
function generationSeen(data) {
  if (stopped || !data) return;
  const seq = data.layers_seq || 0;
  if (seq > layersSeen) {
    layersSeen = seq;
    applyLayerState(data.layers);
  }
  const generation = data.generation;
  if (!generation) return;
  if (busy) { waiting = Math.max(waiting, generation); return; }
  if (generation !== PB.generation) { refresh('generation'); return; }
  // THE SECOND PUSH ON THE SAME GENERATION: the content render landed. Painted
  // WITHOUT the busy popup - nobody was made to wait for it - and dropped when
  // it belongs to a generation this page has already left behind.
  upgradeSeen(data);
}

// An upgrade this page has not painted yet, or nothing. Lifted out and run,
// because "the stale one is dropped" is an ordering and not a string.
function upgradeSeen(data) {
  const upgrade = (data && data.upgrade) || 0;
  if (!upgrade || upgrade <= (PB.upgradeSeq || 0)) return false;
  if (data.generation !== PB.generation) return false;
  PB.upgradeSeq = upgrade;
  // WHOSE content landed. The counter is the session's and there are two
  // pictures, so the board's content is not news to the panel page: it takes
  // the number and does not fetch a fragment that has not changed.
  const whose = data.upgrade_page || '';
  if (whose && whose !== (PB.page || 'prep')) return false;
  logEvent('upgrade', {generation: data.generation, upgrade: upgrade});
  refresh('upgrade');
  return true;
}

// THE STREAM IS ALSO THIS PAGE'S PRESENCE, so it says which
// step it belongs to: holding it is what tells the server this tab is open, and
// dropping it starts the grace window that tells the server it is not. A reload
// drops it and reconnects with the same name, which is how a refresh is told
// apart from a close.
function eventsUrl() {
  return '/events?page=' + encodeURIComponent(PB.page || 'prep');
}

function streamMessage(data) {
  if (data && data.closing) { serverClosing(data.reason); return; }
  generationSeen(data);
}

// A DROPPED STREAM IS THE SERVER GOING. `EventSource`
// reconnects on its own and says nothing while it does, so a server that has
// died looks exactly like a server that hiccuped, and a page whose server was
// closed would sit there looking live until the user pressed something. Two
// answers, because the browser gives two states. CLOSED means
// the browser has given up and there is nothing to wait for. CONNECTING means
// it is retrying, which is worth a short grace window: a real reconnect lands
// in well under a second, so anything past this is a server that is not there.
const STREAM_GRACE_MS = 4000;

function streamBroken() {
  if (stopped || reloading || streamGraceTimer !== null) return;
  streamGraceTimer = setTimeout(() => {
    streamGraceTimer = null;
    serverGone('event-stream');
  }, STREAM_GRACE_MS);
}

function streamAlive() {
  if (streamGraceTimer === null) return;
  clearTimeout(streamGraceTimer);
  streamGraceTimer = null;
}

if (typeof EventSource !== 'undefined') {
  eventStream = new EventSource(eventsUrl());
  eventStream.onopen = () => streamAlive();
  eventStream.onmessage = event => {
    streamAlive();
    let data = {};
    try { data = JSON.parse(event.data); } catch (e) { return; }
    streamMessage(data);
  };
  eventStream.onerror = () => {
    if (stopped || reloading) return;
    // 2 is CLOSED. Named by number because the constant is on the constructor
    // and this runs in browsers that have been known to hide it.
    if (eventStream && eventStream.readyState === 2) {
      serverGone('event-stream');
      return;
    }
    streamBroken();
  };
} else {
  pollTimer = setInterval(async () => {
    if (stopped) return;
    let data;
    try {
      data = await (await fetch('/state')).json();
    } catch (e) {
      serverGone('state-poll');
      return;
    }
    streamMessage(data);
  }, 1500);
}

// =========================================================================
// COLLAPSIBLE CONTROL GROUPS.
// =========================================================================
// The controls column is longer than the screen on any real board, so every
// group folds and the write actions live in a footer that never scrolls away.
// The heading IS the control: click it, or press it with Enter or Space.
//
// WHAT IS FOLDED IS REMEMBERED PER PAGE, in `localStorage`, wrapped in
// try/catch at every touch - a browser may refuse the store outright and the
// column still has to fold. A group nothing was remembered for keeps the state
// it was SERVED with, so the server decides the first sight of a page and the
// user decides every one after that.
//
// STEP 2 IS AN ACCORDION and remembers ONE name instead -
// see `applyFolds`. The store is keyed per page, so the two shapes never meet.
const FOLD_STORE = 'pb.folds.';

function foldStoreKey() { return FOLD_STORE + (PB.page || 'prep'); }

function readFolds() {
  try {
    const raw = localStorage.getItem(foldStoreKey());
    const held = raw ? JSON.parse(raw) : null;
    return (held && typeof held === 'object') ? held : {};
  } catch (e) {
    return {};
  }
}

function writeFolds(folds) {
  try {
    localStorage.setItem(foldStoreKey(), JSON.stringify(folds));
  } catch (e) {
    // An opaque origin or a full store: the column still folds, it just
    // forgets. Never an error the user has to read.
  }
  return folds;
}

function rememberFold(id, collapsed) {
  const folds = readFolds();
  folds[id] = !!collapsed;
  return writeFolds(folds);
}

// One group, drawn folded or not. THE FOLD IS THE ONLY THING ON THIS PAGE THAT
// STILL HIDES, because folding a section is navigation, not hiding a
// setting. So the fold owns `hidden` on the group's own body, and
// a dependent block inside it is greyed by its governor - two states, two
// owners, so neither can turn the other back on.
function paintFold(group, collapsed) {
  if (!group) return false;
  group.dataset.collapsed = collapsed ? '1' : '0';
  const head = group.querySelector('.fold');
  if (head) head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  const body = group.querySelector('.foldbody');
  if (body) body.hidden = !!collapsed;
  return !!collapsed;
}

function foldGroups() {
  return [...document.querySelectorAll('.controls [data-group]')];
}

// THE ACCORDION, on step 2: every group opens collapsed, and
// opening one closes the others, so at most one is open at any moment. What is
// REMEMBERED is therefore not a state per group but the single open group's
// name - and remembering nothing means the column opens with everything shut
// on a first sight of the page.
const OPEN_KEY = '__open';

function accordion() { return !!PB.accordion; }

function rememberOpen(id) {
  const held = {};
  held[OPEN_KEY] = id || '';
  return writeFolds(held);
}

function applyFolds(folds) {
  const held = folds || readFolds();
  const out = {};
  if (accordion()) {
    const open = held[OPEN_KEY] || '';
    foldGroups().forEach(group => {
      const id = group.dataset.group;
      out[id] = paintFold(group, id !== open);
    });
    return out;
  }
  foldGroups().forEach(group => {
    const id = group.dataset.group;
    const collapsed = (id in held) ? !!held[id]
                                   : group.dataset.collapsed === '1';
    out[id] = paintFold(group, collapsed);
  });
  return out;
}

function toggleFold(id) {
  const group = foldGroups().find(one => one.dataset.group === id);
  if (!group) return null;
  const collapsed = group.dataset.collapsed !== '1';
  if (accordion()) {
    // Opening this one closes every other one, in the same pass, so the column
    // never shows two open groups for even a frame.
    foldGroups().forEach(one => paintFold(
      one, collapsed || one.dataset.group !== id
    ));
    rememberOpen(collapsed ? '' : id);
  } else {
    paintFold(group, collapsed);
    rememberFold(id, collapsed);
  }
  logEvent('fold', {group: id, collapsed: collapsed, page: PB.page || 'prep'});
  return collapsed;
}

function wireFolds() {
  foldGroups().forEach(group => {
    const head = group.querySelector('.fold');
    if (!head || head.dataset.wired === '1') return;
    head.dataset.wired = '1';
    head.addEventListener('click', () => toggleFold(group.dataset.group));
    head.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      toggleFold(group.dataset.group);
    });
  });
  return applyFolds();
}

// WHAT THIS VIEWPORT IS STILL WAITING FOR, in its corner.
// The fast render draws every graphic exactly and no pad, track or zone; the
// content render replaces those layers a second or two later. Empty is the
// ordinary finished picture and draws nothing at all.
function drawAwaiting(fragment) {
  const el = document.getElementById('pb-await');
  if (!el) return '';
  const said = (fragment && fragment.content_note) || '';
  // The words go in their own element, so the spinner beside them survives
  // every redraw.
  const text = document.getElementById('pb-await-text');
  if (text) text.textContent = said; else el.textContent = said;
  el.hidden = !said;
  el.classList.toggle('bad', !!(fragment && fragment.content_error));
  return said;
}

// WHICH LAYERS ARE IN AN ALARM STATE, AND WHY.
// The session holds the state and the fragment carries it, so a paint APPLIES
// it rather than deciding it: the corner of the picture says the reason, the
// layer row is marked and its swatch is forced to the alarm colour, and an
// empty list puts all three back - which happens when the cause clears and for
// no other reason. The picture itself arrives already red, because the server
// recolours the documents (`preview.layer_alarms`); this is the half a reader
// reads.
//
// A page with no alarm element is one this does not speak for - the panel page
// - and it is left exactly as it is: an alarm is a verdict about the board's
// toolpath and says nothing about a panel.
function drawLayerAlarms(fragment) {
  const box = document.getElementById('pb-alarm');
  if (!box) return '';
  const said = (fragment && fragment.alarm_layers) || [];
  box.textContent = '';
  said.forEach(entry => {
    const line = document.createElement('div');
    line.textContent = entry.says || '';
    box.appendChild(line);
  });
  box.hidden = !said.length;
  const red = new Set(said.map(entry => entry.layer));
  const color = PB.alarmColor || '';
  const themed = PB.layerColors || {};
  document.querySelectorAll('.layerpanel .lp-row').forEach(row => {
    const at = Number(row.dataset.i);
    const known = (typeof LAYERS !== 'undefined' && LAYERS[at]) || null;
    const on = !!known && red.has(known.canonical);
    row.classList.toggle('lp-alarm', on);
    const swatch = row.querySelector('.lp-sw');
    // The theme colour is put BACK from what the server stated, not by clearing
    // the style: the swatch is a server-written inline style, and clearing it
    // would leave the row with no colour at all. A row the server said nothing
    // about is left exactly as it is.
    const back = themed[known ? known.canonical : ''];
    if (swatch && color && (on || back)) {
      swatch.style.background = on ? color : back;
    }
  });
  return said.map(entry => entry.says || '').join(' | ');
}

drawPending(PB.pending);
applyBusy();
wireSteps();
wireFolds();
drawAwaiting(PB);
drawLayerAlarms(PB);

// =========================================================================
// Session instrumentation: the journal, the snapshot, the note.
// =========================================================================
// A reviewer looks faster than anyone types, so the page reports what the
// user did instead of asking for it to be written down. Events
// are QUEUED and flushed in batches: a pan produces one settle event after the
// gesture ends, never one per pointer move, because a journal nobody can read
// is the same as no journal.

// THE NOTE FIELD LIVES IN THE REPORT DIALOG. It is the one
// place a note is typed, and its line rides in the report rather than being
// posted on its own. The server still has a `/note` route.
const noteEl = document.getElementById('pb-note');
const shotEl = document.getElementById('pb-shot');
const FLUSH_MS = 700;
const FLUSH_MAX = 40;
const SETTLE_MS = 450;
const REPORT_KEY = '__REPORT_KEY__';
let pending = [];
let flushTimer = null;
// The queue exists from here on. `say` runs long before this line does, and a
// `let` read before its declaration throws rather than reading undefined, so
// the journal is opened explicitly rather than guessed at.
journalReady = true;

function logEvent(type, data) {
  if (stopped) return;
  const event = {type: type, page_ms: Math.round(performance.now()),
                 generation: PB.generation};
  Object.keys(data || {}).forEach(key => { event[key] = data[key]; });
  pending.push(event);
  if (pending.length >= FLUSH_MAX) { flushEvents(false); return; }
  if (flushTimer === null) flushTimer = setTimeout(() => flushEvents(false),
                                                   FLUSH_MS);
}

function flushEvents(keepalive) {
  if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
  if (stopped || !pending.length) return;
  const events = pending;
  pending = [];
  // A dropped batch is a gap in a log, not a failure of the run: never let it
  // reach the user as an error.
  //
  // THE ONE FAILED REQUEST THAT DOES NOT RAISE THE DEAD PAGE. Everything else
  // on this page routes a throw into `serverGone`; this one cannot, because the keepalive flush runs while a tab is closing or
  // reloading and a browser abandons it as a matter of course. Turning that
  // into "the preview server is no longer running" would put the words on
  // screen during a perfectly ordinary refresh.
  fetch('/journal', {
    method: 'POST', headers: PB_POST_HEADERS,
    body: JSON.stringify({events: events}), keepalive: !!keepalive,
  }).catch(() => {});
}

function layerState() {
  if (typeof viewer === 'undefined' || !viewer || typeof LAYERS === 'undefined') {
    return null;
  }
  const hidden = LAYERS.filter(l => !viewer.visible.has(l.canonical))
                       .map(l => l.canonical);
  const active = LAYERS[viewer.active];
  return {mode: viewer.mode, active: active ? active.canonical : '',
          hidden: hidden, shown: LAYERS.length - hidden.length};
}

// Read AFTER the panel's own handler has run, and only report a real change:
// clicking the active layer twice is not an event worth a line.
//
// ONE record per change, and one only. A change is journaled here, by the page
// the hand was on, and the same guard is what stops the other page journaling
// it again when it receives it: `applyLayerState` sets this key before it
// paints, so the receiving page reads no change and reports none.
let lastLayers = '';
function noteLayers(cause) {
  setTimeout(() => {
    const state = layerState();
    if (!state) return;
    const key = JSON.stringify(state);
    if (key === lastLayers) return;
    lastLayers = key;
    logEvent('layers', Object.assign({cause: cause, page: PB.page || 'prep'},
                                     state));
    // ...and tell the other page, which is looking at the same layers.
    pushLayers();
  }, 0);
}

function cardName(card, index) {
  return (card.dataset && card.dataset.viewport) || ('view' + (index || 0));
}

function numbers(text) {
  return String(text || '').trim().split(/[\s,]+/).map(Number);
}

function escapeAttr(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                     .replace(/"/g, '&quot;');
}

// One viewport, serialized as it stands: the layers that are visible, in the
// order they are painted, at the opacity they are painted with, in the frame
// the user has panned and zoomed to. The document is the VISIBLE extent
// rather than the bare viewBox, because xMidYMid meet shows more than the box
// in one axis and a shot has to be what the user was looking at.
function serializeCard(card, index) {
  const viewport = card.querySelector('.viewport');
  if (!viewport) return null;
  const stack = [...viewport.querySelectorAll('svg')];
  if (!stack.length) return null;
  const box = numbers(stack[0].getAttribute('viewBox') || stack[0].dataset.base);
  if (box.length !== 4 || box.some(n => !isFinite(n)) || box[2] <= 0
      || box[3] <= 0) {
    return null;
  }
  const base = numbers(stack[0].dataset.base);
  const rect = viewport.getBoundingClientRect();
  const scale = Math.min(rect.width / box[2], rect.height / box[3]) || 1;
  const wide = rect.width / scale;
  const high = rect.height / scale;
  const view = [box[0] - (wide - box[2]) / 2, box[1] - (high - box[3]) / 2,
                wide, high];
  const background = getComputedStyle(viewport).backgroundColor || '#14161a';
  const serializer = new XMLSerializer();
  const layers = [];
  let body = '';
  stack.map((el, i) => ({el: el, i: i,
                         z: parseInt(el.style.zIndex || '1', 10) || 1}))
       .sort((a, b) => (a.z - b.z) || (a.i - b.i))
       .forEach(entry => {
    const el = entry.el;
    const name = el.dataset.layer || ('layer' + entry.i);
    const shown = el.style.display !== 'none';
    const opacity = el.style.opacity === '' ? 1 : parseFloat(el.style.opacity);
    const known = typeof LAYERS === 'undefined' ? null
                  : LAYERS.find(l => l.canonical === name);
    layers.push({canonical: name, visible: shown, opacity: opacity, z: entry.z,
                 physical: known ? !!known.physical : null,
                 top: known ? !!known.top : null});
    if (!shown) return;
    let inner = '';
    el.childNodes.forEach(node => { inner += serializer.serializeToString(node); });
    body += '<g data-layer="' + escapeAttr(name) + '" opacity="'
          + opacity + '">' + inner + '</g>';
  });
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"'
    + ' width="' + wide.toFixed(4) + 'mm" height="' + high.toFixed(4) + 'mm"'
    + ' viewBox="' + view.map(n => n.toFixed(6)).join(' ') + '">'
    + '<title>' + escapeAttr(cardName(card, index)) + ' - generation '
    + PB.generation + '</title>'
    + '<rect x="' + view[0].toFixed(6) + '" y="' + view[1].toFixed(6)
    + '" width="' + view[2].toFixed(6) + '" height="' + view[3].toFixed(6)
    + '" fill="' + escapeAttr(background) + '"/>'
    + body + '</svg>';
  return {
    name: cardName(card, index),
    svg: svg,
    view: {
      viewBox: view, layer_viewbox: box, fit_viewbox: base,
      zoom_pct: base.length === 4 && box[2] ? +(base[2] / box[2] * 100).toFixed(2)
                                            : null,
      size_px: [Math.round(rect.width), Math.round(rect.height)],
      background: background, layers: layers,
    },
  };
}

async function send(path, body) {
  if (stopped) return null;
  try {
    const response = await fetch(path, {
      method: 'POST', headers: PB_POST_HEADERS,
      body: JSON.stringify(body || {}),
    });
    return await response.json();
  } catch (e) {
    // Same ending as `post` and for the same reason: a
    // request nobody answered means the server is gone, and the browser's
    // exception text is not something the user should ever be shown.
    serverGone('send ' + path);
    return null;
  }
}

// Everything the server needs to take the shot: one serialized document per
// viewport, the layer panel's state, the screen, and the line typed in the
// report dialog. One builder, because the report posts exactly what the
// snapshot posted, plus `download`: true when the person also asked the
// browser for a copy. The server writes the zip in its local folder either way.
function shotBody(cause, download) {
  const viewports = [];
  [...document.querySelectorAll('.card')].forEach((card, i) => {
    const one = serializeCard(card, i);
    if (one) viewports.push(one);
  });
  return {
    cause: cause || 'button',
    download: !!download,
    page: PB.page || 'prep',
    generation: PB.generation,
    panel: layerState(),
    note: noteEl.value.trim(),
    note_box: noteEl.value.trim(),
    screen: {width: innerWidth, height: innerHeight,
             dpr: window.devicePixelRatio || 1},
    viewports: viewports,
  };
}

// A COPY SOMEWHERE ELSE. A page cannot write to a chosen folder, so "Save a
// copy elsewhere" fetches the zip the server just wrote through an anchor
// carrying `download`: the browser then does whatever the person has it set to
// do, which is its own save dialog when "ask where to save each file" is on.
// The report also stays in its local folder, and the status line says where.
function downloadReport(reply) {
  const link = document.createElement('a');
  link.href = reply.download;
  link.download = reply.name;
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

// THE SERVER SAVES IT ON THIS COMPUTER AND THE PAGE SAYS WHERE. The status
// line names the zip and the summary beside it, so the person can open the
// folder or point a coding agent at the file.
async function reportProblem(cause, download) {
  if (stopped) return;
  say('collecting the report...', '');
  flushEvents(false);
  const reply = await send('/bug_report', shotBody(cause, download));
  if (!reply) return;
  if (!reply.ok) { say(reply.error || 'the report was refused', 'bad'); return; }
  PB.shot = reply.shot;
  shotEl.textContent = String(reply.shot).padStart(3, '0');
  noteEl.value = '';
  if (download) {
    downloadReport(reply);
    say(reply.message + ' - saved as ' + reply.file
        + '. Your browser is also saving a copy of ' + reply.name + '.', 'good');
    return;
  }
  say(reply.message + ' - saved as ' + reply.file
      + ', with its summary at ' + reply.summary_file, 'good');
}

// THE POPUP IS THE WHOLE REPORT DIALOG. The instruction,
// the note field and both saves are in it, and NOTHING is collected until one
// of the two save buttons is pressed. Cancel collects nothing at all.
let reportCause = 'button';

function askReport(cause) {
  if (stopped) return;
  reportCause = cause;
  reportAskEl.classList.add('on');
  reportAskEl.setAttribute('aria-hidden', 'false');
  logEvent('prompt', {kind: 'report', asked: cause});
}

function closeReportAsk() {
  reportAskEl.classList.remove('on');
  reportAskEl.setAttribute('aria-hidden', 'true');
}

reportGoEl.onclick = () => {
  closeReportAsk();
  logEvent('prompt.answer', {answer: 'save', kind: 'report'});
  reportProblem(reportCause, false);
};
reportDownloadEl.onclick = () => {
  closeReportAsk();
  logEvent('prompt.answer', {answer: 'copy_elsewhere', kind: 'report'});
  reportProblem(reportCause, true);
};
reportCancelEl.onclick = () => {
  closeReportAsk();
  logEvent('prompt.answer', {answer: 'cancel', kind: 'report'});
  say('nothing was collected', '');
};

document.getElementById('pb-report-btn').onclick = () => askReport('button');

// THE STARTUP WARNING. It is served ALREADY OPEN
// when the settings file would not be read, so the person is told before a
// control is touched, and it hands the existing report dialog rather than
// inventing a second way to report the same thing. Guarded on its element:
// an ordinary launch serves no warning at all.
const startupAskEl = document.getElementById('pb-startup-ask');
if (startupAskEl) {
  const closeStartupAsk = () => {
    startupAskEl.classList.remove('on');
    startupAskEl.setAttribute('aria-hidden', 'true');
  };
  document.getElementById('pb-startup-ok').onclick = () => {
    closeStartupAsk();
    logEvent('prompt.answer', {answer: 'carry_on', kind: 'startup'});
  };
  document.getElementById('pb-startup-report').onclick = () => {
    closeStartupAsk();
    logEvent('prompt.answer', {answer: 'report', kind: 'startup'});
    askReport('startup warning');
  };
}
// ESC CLOSES THE REPORT DIALOG, EXACTLY AS CANCEL DOES. Nothing is collected
// until a save button is pressed, so backing out costs nothing and the key
// everybody reaches for has to work. The unsaved changes dialog keeps its
// buttons: there the choice is what happens to work the user has done, and no
// key may answer that.
addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!reportAskEl.classList.contains('on')) return;
  event.preventDefault();
  closeReportAsk();
  logEvent('prompt.answer', {answer: 'cancel', kind: 'report'});
  say('nothing was collected', '');
});
// ENTER DOES NOT SUBMIT THE DIALOG. The person is writing a
// sentence about what went wrong, and a key that filed the report mid-sentence
// would collect a half line. The two save buttons are the only way out.
noteEl.addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
});

// One unclaimed key. H, V, +, - and L already mean something on this page, and
// the header says what S does so nobody has to remember it.
addEventListener('keydown', event => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  if ((event.key || '').toLowerCase() !== REPORT_KEY) return;
  event.preventDefault();
  askReport('hotkey');
});

// Every button on the page, wherever it lives. Capture phase, so a handler
// that redraws its own button is still logged.
document.addEventListener('click', event => {
  const button = event.target.closest ? event.target.closest('button') : null;
  if (!button) return;
  const where = button.closest('.controls') ? 'controls'
              : button.closest('.layerpanel') ? 'layerpanel'
              : button.closest('.card') ? 'viewport' : 'page';
  logEvent('control', {
    id: button.id || '', label: (button.textContent || '').trim().slice(0, 40),
    candidate: button.dataset.sid || '', where: where,
    disabled: !!button.disabled,
  });
  if (where === 'layerpanel') noteLayers('panel button');
}, true);

// Layer toggles and view modes: read off the panel after its own handler.
const panelRoot = document.getElementById('lp-viewer');
if (panelRoot) panelRoot.addEventListener('click', () => noteLayers('panel'), true);
addEventListener('keydown', event => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  // No L: a served page has one viewport and nothing to lay out.
  if ('hv+-=_'.indexOf((event.key || '').toLowerCase()) < 0) return;
  logEvent('key', {key: event.key});
  noteLayers('key ' + event.key);
});

// Pan and zoom SETTLE, once, after the gesture stops.
document.querySelectorAll('.card').forEach((card, index) => {
  const viewport = card.querySelector('.viewport');
  const svg = viewport ? viewport.querySelector('svg') : null;
  if (!svg) return;
  let timer = null;
  const settle = cause => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const box = numbers(svg.getAttribute('viewBox'));
      const base = numbers(svg.dataset.base);
      logEvent('view', {
        viewport: cardName(card, index), cause: cause, viewBox: box,
        zoom_pct: base.length === 4 && box[2]
                  ? +(base[2] / box[2] * 100).toFixed(2) : null,
      });
    }, SETTLE_MS);
  };
  viewport.addEventListener('wheel', () => settle('wheel'), {passive: true});
  viewport.addEventListener('pointerup', () => settle('pan'));
  viewport.addEventListener('dblclick', () => settle('fit'));
  const fit = card.querySelector('.fit');
  if (fit) fit.addEventListener('click', () => settle('fit'));
});

// Closing the tab with edits waiting is the other way to lose them, and the
// browser owns that gesture: all a page may do is ask it to ask. Chrome and
// Firefox show their own wording, so nothing here tries to write the sentence.
// Named, so it can be lifted out and RUN: what this returns is what the
// user sees, and a wrong confirm-leaving prompt is a defect that no string
// assertion would catch.
function unloadGuard(event) {
  if (stopped || reloading) return;
  logEvent('page.leave', {pending: PB.pending});
  flushEvents(true);
  if (PB.pending > 0) {
    event.preventDefault();
    event.returnValue = '';
    return '';
  }
}
addEventListener('beforeunload', unloadGuard);

logEvent('page.open', {url: location.href, layers: PB.layerCount,
                       page: PB.page || 'prep', session: PB.session});
"""


# What every page's state carries whether or not the page that built it said
# so. One key today: the paint the served stack is (`fragmentUrl`). A page that
# does not state it asks for the whole stack on its first fragment, which is
# correct and costs one fetch.
PAGE_STATE_DEFAULTS = {"paintSeq": 0}


def page_script(own: str, state: dict) -> str:
    """One page's whole script: the shared halves with its own spliced between.

    *own* is the page's third column - :data:`PREP_JS`, or the panel page's
    equivalent - and *state* is what the server already knows, baked in so the
    first paint needs no round trip.

    The order is deliberate and the reason is JavaScript's: everything the
    shared halves call into is a function DECLARATION, so it is hoisted and a
    page's `applyFragment` is reachable from a `refresh` written above it.
    """
    return (SHARED_JS + own + SHARED_TAIL).replace(
        "__STATE__", json.dumps({**PAGE_STATE_DEFAULTS, **state})
    ).replace("__REPORT_KEY__", REPORT_KEY)


# The prep page's whole script, as one string, because that is what it was
# before there were two pages - the node harnesses lift functions out of it by
# name and the frozen-artifact fence reads it for served-only tokens.
CONTROLS_JS = SHARED_JS + PREP_JS + SHARED_TAIL


# The problem-report hotkey. H, V, +, -, L are taken by the layer panel and the
# layout toggle, so S it is - and the header says so, because a hotkey nobody
# is told about is a hotkey nobody presses.
REPORT_KEY = "s"
REPORT_LABEL = "Report a Problem"
# The second save in the report dialog: the same zip, through the browser.
REPORT_COPY_LABEL = "Save a copy elsewhere"


# What the card is called, and therefore what a shot's files are called. One
# viewport today; the panelize preview will name its own.
BOARD_VIEWPORT = "board"


def _tools_html() -> str:
    """The header's session tools: the problem-report button, and only that.

    THE BUTTON COLLECTS A ZIP. It takes a shot of the view and puts it in an
    archive with the board, the panel settings, the session journal, the
    version facts and the KiCad library tables, so somebody else's board can
    be troubleshot away from their machine.

    EXACTLY ONE CONTROL IN THE HEADER. The whole report
    dialog is the popup the button opens (:func:`_report_prompt_html`): the
    note is typed there, next to the instruction that asks for it, and nothing
    is collected until a button in that dialog is pressed.

    NO LAYOUT TOGGLE. Each served page has ONE viewport, so
    "side by side / stacked" arranges nothing - a button that does not visibly
    do anything is worse than no button. It lives with the frozen review page,
    which has two viewports and a real question to answer
    (:data:`eedesignpanels.review.page.LAYOUT_JS`).
    """
    return (
        '<div class=tools>'
        f'<button class="btn report" id=pb-report-btn '
        f'title="collect this run into one zip - the board, the settings, '
        f'the journal and the picture - and save it for the developer. '
        f'Hotkey: {REPORT_KEY.upper()}">'
        f'{html.escape(REPORT_LABEL)}</button>'
        '</div>'
    )


# THE TWO STEPS. One server, two pages, in the order the job
# is done in: a board is prepared, and then it is panelized. Each page names
# both and marks the one it is, so the other is always one click away and a
# reader is never in doubt about which step they are looking at.
#
# Each step carries a STABLE WINDOW NAME, which is the whole mechanism behind
# "the step links jump, never spawn": a page adopts its step's name on load, and
# `window.open(path, name)` addressed to a name that is already open focuses
# that window instead of making another.


class Step(NamedTuple):
    """One step: where it is served, what the session calls it, its tab's name."""

    path: str
    page: str
    window: str
    label: str


PREP_STEP = Step("/", "prep", "pb-step-prep", "Step 1 - prepare the board")
PANEL_STEP = Step("/panel", "panel", "pb-step-panel", "Step 2 - panelize")
STEPS = (PREP_STEP, PANEL_STEP)
STEP_BY_PAGE = {step.page: step for step in STEPS}


def steps_json() -> list:
    """The steps as the page's script reads them: path, page key, window name."""
    return [
        {"path": step.path, "page": step.page, "window": step.window,
         "label": step.label}
        for step in STEPS
    ]


def _steps_html(here: str) -> str:
    """The step links, with *here* - a path - marked as the current one.

    THE LINKS JUMP, THEY NEVER SPAWN. Each link is aimed at
    the other step's stable window NAME, so a browser that can reach that window
    reuses it - the tab the user already has, brought forward, rather than a
    second copy of a page that is already open. The script covers the case
    where the browser cannot help: it asks the server whether that step is
    open before it opens anything (`jumpToStep`).

    There is deliberately no `rel=noopener`. It forces a fresh window and makes
    the name unusable, which would spawn a second copy; the two tabs share
    their state through the session either way, so an opener handle between
    them buys and costs nothing.
    """
    out = ""
    for step in STEPS:
        if step.path == here:
            out += (
                '<span class=here aria-current=page>'
                f'{html.escape(step.label)}</span>'
            )
        else:
            out += (
                f'<a href="{step.path}" target="{step.window}" '
                f'data-step-window="{step.window}" data-step-page="{step.page}" '
                f'data-step-path="{step.path}" '
                f'title="go to {html.escape(step.label)} - its own tab, '
                f'brought forward if it is already open">'
                f"{html.escape(step.label)}</a>"
            )
    return f'<nav class=steps>{out}</nav>'


def _footer_html(session_dir: str, shot: int) -> str:
    """Where this session is being recorded, and what has been saved so far."""
    saved = f"{shot:03d}" if shot else "none"
    place = html.escape(session_dir) if session_dir else "not recording"
    return (
        '<div class=footer>'
        f'<span>session <code id=pb-session>{place}</code></span>'
        f'<span>last shot <b id=pb-shot>{saved}</b></span>'
        f'<span><b>{REPORT_KEY.upper()}</b> reports a problem &middot; '
        'the zip is kept in the session folder</span>'
        '</div>'
    )


def number_field(field_id: str, title: str, helper: str, step: str,
                 minimum: str = "0", css: str = "") -> str:
    """One entry field: bold title, control, helper line.

    ONE builder for both pages, because every field on every page has this
    shape and two builders are two ways to forget half of it. The helper is also
    the tooltip - the same sentence, so a reader who hovers and a reader who
    does not are told the same thing.
    """
    classes = f"field {css}" if css else "field"
    return (
        f'<label class="{classes}">'
        f'<span class=t>{html.escape(title)}</span>'
        f'<input type=number id={field_id} data-act=param step={step} '
        f'min={minimum} title="{html.escape(helper)}">'
        f'<span class=h>{html.escape(helper)}</span></label>'
    )


def check_field(field_id: str, title: str, helper: str) -> str:
    """A checkbox, wearing the same title and helper every entry field wears."""
    return (
        '<label class="field">'
        f'<span class=t>{html.escape(title)}</span>'
        f'<input type=checkbox id={field_id} data-act=param '
        f'title="{html.escape(helper)}">'
        f'<span class=h>{html.escape(helper)}</span></label>'
    )


def choice_field(field_id: str, title: str, helper: str, options) -> str:
    """A dropdown over named choices, wearing the same title and helper line.

    *options* is `(value, label)` pairs. The VALUE is the config's own word and
    the label is the user's, so what the file says and what the page shows
    cannot drift apart. Nothing here is ever a unit: every dimension on both
    pages is millimetres and there is no unit selector anywhere.
    """
    picks = "".join(
        f'<option value="{html.escape(value)}">{html.escape(label)}</option>'
        for value, label in options
    )
    return (
        '<label class="field">'
        f'<span class=t>{html.escape(title)}</span>'
        f'<select id={field_id} data-act=param '
        f'title="{html.escape(helper)}">{picks}</select>'
        f'<span class=h>{html.escape(helper)}</span></label>'
    )


def file_field(field_id: str, title: str, helper: str,
               accept: str = "") -> str:
    """A native file picker, in the same clothes every entry field wears.

    THE BROWSER'S OWN PICKER, and it is the only control that can do this job.
    A browser does not tell a page
    where a chosen file lives - it hands over the CONTENT - and a page that
    asked for a path instead would be asking the user to type the longest
    string on the screen. So the control is a picker and what it posts is bytes.

    Full width on its own row: a picker's label is the file's name plus the
    browser's own wording, which is wider than a number field's box.
    """
    kind = f' accept="{html.escape(accept)}"' if accept else ""
    return (
        '<label class="field wide">'
        f'<span class=t>{html.escape(title)}</span>'
        f'<input type=file id={field_id} data-act=param{kind} '
        f'title="{html.escape(helper)}">'
        f'<span class=h>{html.escape(helper)}</span></label>'
    )


def fold_head(tag: str, group_id: str, title: str, collapsed: bool = False,
              title_html: str = "") -> str:
    """One group's heading, which is also the control that folds it.

    A heading, not a button element: it is a heading and it reads as one, and
    `role=button` with a tab stop is what makes it operable by keyboard as well
    as by pointer.

    A BAR CARRIES A NAME AND NOTHING ELSE. The stage's on/off is said by the
    switch one line below it, so the bar does not repeat it.
    """
    said = title_html or html.escape(title)
    return (
        f'<{tag} class=fold role=button tabindex=0 '
        f'aria-expanded={"false" if collapsed else "true"}>'
        f'<span class=nm>{said}</span></{tag}>'
    )


def status_badge(element_id: str, ok: bool = False, text: str = "") -> str:
    """A green or red dot and two words, in a group's body. NOT a control.

    A status sits in the menu, carries no explanatory note, and nothing about
    it can be clicked. The dot is the layer panel's `.lp-status`, green for
    good and red for bad, so one page never spells the same verdict two ways.
    It sits in the group BODY, with the settings it is about, rather than on
    the heading bar.

    Served HIDDEN with no words, because nothing has measured it yet; the page
    fills it in from the build.
    """
    shut = "" if text else " hidden"
    kind = "ok" if ok else "bad"
    return (
        f'<span class=status id={element_id}{shut}>'
        f'<span class="lp-status {kind}"></span>'
        f'<span class=txt>{html.escape(text)}</span></span>'
    )


def section_html(group_id: str, title: str, body: str,
                 collapsed: bool = False, title_html: str = "") -> str:
    """One COLLAPSIBLE group of the controls column.

    Every section on both pages is one of these. The heading folds it, the fold
    is remembered per page, and what is served is the state a page opens in when
    nothing has been remembered for it yet.

    A section served COLLAPSED is served with its body already hidden, so the
    page does not paint it open for the moment before the fold script runs.
    """
    shut = " hidden" if collapsed else ""
    return (
        f'<section id={group_id} data-group={group_id} '
        f'data-collapsed={"1" if collapsed else "0"}>'
        + fold_head("h3", group_id, title, collapsed, title_html)
        + f'<div class=foldbody{shut}>{body}</div></section>'
    )


def group_html(group_id: str, title: str, switch_id: str, helper: str,
               body: str, note: str = "", collapsed: bool = False,
               top_level: bool = False, switch_label: str = "",
               status: str = "") -> str:
    """One carrier stage: a heading, its on/off, and the fields it switches on.

    THE SWITCH IS THE STAGE. Off is
    `"enabled": false` in that stage's own section of `panel.json` - the config
    is explicit and absence says nothing - and a panel built with it off is
    byte for byte the panel built without the stage.

    *switch_label* overrides the switch's own wording. `Add ` plus the title is
    right for four of the five groups; the V-Scores switch reads `Add extra
    V-scores`, because the panel already carries the scores its boards decided
    and this group adds passes on top of them.

    THE FIELDS ARE GREYED WHEN THE SWITCH IS OFF, NOT HIDDEN. A stage nobody
    can read is a stage nobody can judge before switching it on, and a body
    that appears and vanishes moves every group under it. The switch governs
    the block and so sits directly above it, the vertical line down the block's
    left edge says which fields it governs, and `disabled` on each control is
    what keeps a greyed field out of the config.

    IT ALSO FOLDS, which is navigation and not hiding a
    setting: the FOLD puts the whole group away, the switch greys the stage's
    fields inside it. The switch itself stays exactly where it is - title,
    control, helper line - and the heading bar carries the name alone, because
    the switch one line below it already says which way the stage is set.

    *status* is a verdict inside the group body (:func:`status_badge`), and it
    is NOT a setting: it says something the build measured, it cannot be
    clicked, and it carries no note. It sits with the settings it is about,
    under the switch and outside the greyed block, because it holds whichever
    way the switch is set.

    *top_level* draws it as a SECTION of the controls column rather than a group
    nested inside one. Same switch, same body, same fold - a heading at the
    column's own level instead of one indented under a wrapper that said
    nothing the five groups do not say for themselves.
    """
    said = f'<div class=vd>{html.escape(note)}</div>' if note else ""
    element, tag = ("section", "h3") if top_level else ("div class=grp", "h4")
    shut = " hidden" if collapsed else ""
    return (
        f'<{element} id={group_id} data-group={group_id} '
        f'data-collapsed={"1" if collapsed else "0"}>'
        + fold_head(tag, group_id, title, collapsed)
        + f'<div class=foldbody{shut}>'
        + check_field(switch_id, switch_label or f"Add {title.lower()}", helper)
        # THE STATUS SITS IN THE BODY, UNDER THE SWITCH AND OUTSIDE THE
        # DEPENDENT BLOCK. In the body because it
        # belongs with the settings it is about; outside the block because it
        # states what the build measured whichever way the switch is set, so the
        # switch may not grey it out.
        + status
        # Served GREYED, because the config has not been read yet. The script
        # turns it live the moment it knows which way the switch is set.
        + f'<div class="dependent off" id={group_id}-body>{body}{said}</div>'
        + f'</div></{element.split()[0]}>'
    )


def actions_html(rows: str, backend: str) -> str:
    """THE WRITE ACTIONS, in a footer under the scrolling body.

    Save to Board, Build Panel, Save Panel Files and Quit Panelizer: the buttons a
    user reaches for after reading the column, which is exactly why they may
    not be at the bottom of it. `flex:none` under a `.body` that scrolls is the
    whole mechanism.

    The renderer line rides here because it is about what the last picture cost,
    and this is the one part of the column that is always on screen.
    """
    return (
        '<div class=actions role=group aria-label="write actions">'
        f'{rows}'
        f'<div class=where style="margin-top:.4rem">renderer: '
        f'{html.escape(backend)}</div>'
        '</div>'
    )


def alarm_html(alarm_layers=None) -> str:
    """The alarm status: which layers are red and why, one line each.

    **A STATUS, NOT AN EXPLANATION.** One line per alarmed layer, in the
    reader's own layer names, drawn in the corner of the picture it is about.
    Hidden and empty on an ordinary board, and re-stated by every fragment, so
    the sentence and the colour can never disagree.
    """
    rows = "".join(
        f"<div>{html.escape(str(entry.get('says', '')))}</div>"
        for entry in (alarm_layers or [])
    )
    hidden = "" if rows else " hidden"
    return (
        f'<div class=alarmnote id=pb-alarm role=status aria-live=polite'
        f'{hidden}>{rows}</div>'
    )


def await_html(note: str = "") -> str:
    """The corner note: what this viewport is still waiting for, if anything.

    Carries its own spinner: waiting is work in progress,
    and moving is what the eye finds. The text lives in its own element so a
    redraw never wipes the spinner out.
    """
    hidden = "" if note else " hidden"
    return (
        f'<div class=awaitnote id=pb-await role=status aria-live=polite'
        f'{hidden}><span class=spin aria-hidden=true></span>'
        f'<span id=pb-await-text>{html.escape(note)}</span></div>'
    )


def _controls_html(generation: int, backend: str, score_layer: str,
                   pending: int = 0) -> str:
    """The third column's skeleton. Its contents are drawn by the script.

    Every control that changes something carries `data-act`, which is what the
    busy state disables. `Quit Panelizer` deliberately does not: quitting has to
    work while an action is running.

    *score_layer* is the board's own display name for the v-score layer - the
    naming rule, applied at the one place the heading is
    written.
    """
    counted = "change" if pending == 1 else "changes"
    label = f"Save to Board ({pending} {counted})" if pending else "Save to Board"
    router = (
        number_field(
            "pb-cutter", "Cutter diameter (mm)",
            "The router bit the fab will run. Declared, never inferred.",
            step="0.001", minimum="0.001",
        )
        + number_field(
            "pb-past", "Across score extension (mm)",
            "How far the cut's tangent point runs past a score plane.",
            step="0.01",
        )
        + number_field("pb-along", "Along score extension (mm)",
                       "Extra travel along a near-tangent route end.", step="0.01")
        + number_field("pb-tangent", "Tangent threshold (degrees)",
                       "Angles below this threshold use along-score travel.", step="1")
        + check_field("pb-custom-route-ends", "Enable custom end behavior",
                      "Off uses global distances and keeps saved end settings.")
        + '<fieldset id="pb-route-fields" disabled style="border:0;padding:0;margin:0;opacity:0.45">'
        + '<label class="field"><span class=t>Route end</span><select id="pb-route-end" title="Select the highlighted route end."></select><span class=h>Select the highlighted route end.</span></label>'
        + '<label class="field"><span class=t>End behavior</span><select id="pb-route-mode" title="Use the global setting or override this end."><option value="inherit">Inherit</option><option value="none">No extension</option><option value="custom">Custom distance</option></select><span class=h>Use the global setting or override this end.</span></label>'
        + number_field("pb-route-distance", "End extension (mm)", "Distance for the selected end.", step="0.01")
        + '<button id="pb-route-apply">Apply end setting</button>'
        + '<button id="pb-route-reset">Clear end overrides</button>'
        + '</fieldset>'
        # THE HEAL, ADJUSTABLE. Beside the cutter
        # and the keep-out because it is the third number this page declares
        # about reading the board. The default is KiCad's own figure
        # (`pcbnew/board.h:86-88`, 10.0.3); zero refuses any gap at all.
        + number_field(
            "pb-heal", "Largest outline gap closed (mm)",
            "Zero refuses any gap. Every gap closed is reported.",
            step="0.001",
        )
        + number_field(
            "pb-mintab", "Minimum tab (mm)",
            "Material narrower than this is cut away as swarf.",
            step="0.05", minimum="0.1",
        )
        + check_field(
            "pb-elim", "Eliminate slivers",
            "Route the thin material away. Off leaves it and warns.",
        )
        # POCKETING, with the other routing
        # settings because the one declared cutter above governs it. A name and
        # one line: the GUI carries no prose.
        + check_field(
            "pb-pocket", "Pocket cutouts",
            "Mill out a cutout wider than the cutter. Off leaves the middle.",
        )
        + '<div class=row><button class=btn id=pb-apply data-act=apply>'
        'apply</button>'
        '<span class=vd>or press Enter in a field</span></div>'
    )
    return (
        '<aside class=controls id=pb-controls aria-busy=false>'
        f'<header><span>Decisions</span><span class=gen id=pb-gen>'
        f'gen {generation}</span></header>'
        '<div class=body>'
        + section_html("pb-sec-router", "Router", router)
        + section_html("pb-sec-vscore", "V-score", number_field(
            "pb-scorew", "Score keep-out half width (mm)",
            "How far either side of a score line copper is kept out.",
            step="0.05", minimum="0.01",
        ))
        + section_html(
            "pb-sec-edge-plating", "Edge Plating",
            check_field('pb-edge-plating', 'Enable and synchronize',
                        'Update edge plating when score settings change.')
            + check_field('pb-edge_plating_front_mask', 'F.Mask',
                          'Add a front solder mask opening along the plated edge.')
            + check_field('pb-edge_plating_back_mask', 'B.Mask',
                          'Add a back solder mask opening along the plated edge.')
            + number_field('pb-edge_plating_mask_width_mm', 'Mask width (mm)',
                           'Width of the generated solder mask graphics.',
                           step='0.01', minimum='0.001')
            + '<button id=pb-generate-edge-plating data-act=plating>Generate edge plate</button> '
            + '<button id=pb-remove-edge-plating data-act=plating '
            'title="Remove edge plating and turn synchronization off.">'
            'Remove edge plate</button>', collapsed=True,
        )
        + section_html(
            "pb-sec-candidates", "V-score candidates",
            '<div id=pb-candidates></div>',
            title_html=(
                f"V-score candidates &middot; {html.escape(score_layer)}"
            ),
        )
        # NO "RUN NOTES" SECTION: a collapsed box nobody opens adds nothing.
        # The server still builds every warning and every alarm and the
        # problem-report zip carries them; the page sends them to the developer
        # console (`drawNotes`).
        + '</div>'
        + actions_html(
            check_field('pb-lock_board_on_save', 'Lock all items on save',
                        'Lock all lockable items in the single PCB when saved.')
            + '<div class=row>'
            f'<button class="btn save" id=pb-save data-act=save>'
            f'{html.escape(label)}</button></div>'
            '<div class=row>'
            '<button class=btn id=pb-reload data-act=reload>Reload</button>'
            '<button class=btn id=pb-stop>Quit Panelizer</button></div>',
            backend,
        )
        + '<div class=status id=pb-status>every edit is held in memory - '
        'Save to Board is the only thing that writes</div>'
        '</aside>'
    )


def _busy_popup_html() -> str:
    """The popup drawn OVER the viewports while an action runs.

    Inside the views container, so it dims exactly what it is talking about and
    leaves the header, the layer panel and the stop button reachable.
    """
    return (
        '<div class=busyveil id=pb-veil aria-hidden=true role=status>'
        '<div class=note><div class=spin></div>'
        '<b>working</b><span id=pb-veil-what>recomputing the toolpath</span>'
        '<span> &middot; the display below is one edit behind until this '
        'finishes</span></div></div>'
    )


def _unsaved_prompt_html() -> str:
    """The three-way unsaved-work question, asked by two controls.

    Quit Panelizer asks it, and so does Reload - both throw
    held work away. The verb in the sentence and in the two answer buttons is
    written by the script when the question is asked, so what is served here is
    the quitting wording and never a third neutral phrasing nobody would read as
    either action.
    """
    return (
        '<div class=ask id=pb-ask aria-hidden=true role=dialog '
        'aria-labelledby=pb-ask-title>'
        '<div class=box><h2 id=pb-ask-title>There are unsaved changes</h2>'
        '<div id=pb-ask-line>Quitting the panelizer now would lose them. The '
        'board and the config on disk have not been touched yet.</div>'
        '<ul id=pb-ask-list></ul>'
        '<div class=row>'
        '<button class="btn save" id=pb-ask-save>save and quit</button>'
        '<button class="btn no" id=pb-ask-discard>discard and quit</button>'
        '<button class=btn id=pb-ask-cancel>cancel</button>'
        '</div></div></div>'
    )


def report_folder_text(config) -> str:
    """The local folder a report from this session goes to, as the page says it.

    A folder the setting refuses comes back as the reason, because the dialog
    is where the person learns that the report cannot be written there.
    """
    if config is None:
        return ""
    try:
        return str(bugreport.report_folder(config))
    except bugreport.ReportError as exc:
        return str(exc)


def _report_prompt_html(folder: str = "") -> str:
    """The whole report dialog: what it collects, the note, and the two saves.

    THIS POPUP IS THE ENTIRE REPORT DIALOG. The header has
    one button, and everything else the report needs is here: the instruction,
    the one note field on the page, and the choice of where the zip lands.
    `cancel` collects nothing at all.

    WHAT IT HOLDS AND WHERE IT GOES. The report carries the person's own design
    files, so the dialog says so, and says that it stays on this computer. The
    folder is one line, monospace, selected whole by one drag, so it pastes
    into a file browser or a message to a coding agent unbroken.

    `Save Report` IS THE PRIMARY ANSWER: the server writes the zip and its
    summary into *folder* (:func:`eedesignpanels.preview.bugreport.build`).
    `Save a copy elsewhere` writes the same report there and also hands the zip
    to the browser, which then opens its own save dialog.
    """
    return (
        '<div class=ask id=pb-report-ask aria-hidden=true role=dialog '
        'aria-labelledby=pb-report-ask-title>'
        '<div class=box><h2 id=pb-report-ask-title>'
        f'{html.escape(REPORT_LABEL)}</h2>'
        '<p>This report collects the board, the panel settings, the record of '
        'this session and the picture on screen.</p>'
        '<p>One line about what went wrong makes it much more useful:</p>'
        '<input type=text id=pb-note maxlength=200 autocomplete=off '
        'aria-label="what went wrong" placeholder="what went wrong">'
        '<p>The report is saved on this computer, in this folder:</p>'
        f'<code>{html.escape(folder)}</code>'
        f'<p>{html.escape(bugreport.PRIVACY_LINE)} Nothing is sent anywhere.</p>'
        '<div class=row>'
        '<button class="btn report" id=pb-report-go>Save Report</button>'
        '<button class=btn id=pb-report-download>'
        f'{html.escape(REPORT_COPY_LABEL)}</button>'
        '<button class=btn id=pb-report-cancel>cancel</button>'
        '</div></div></div>'
    )


def _startup_warning_html(message: str) -> str:
    """The popup a launch on standard settings opens with, or nothing at all.

    THE TOOL STARTS AND SAYS SO. A settings file that will not load does not
    stop the launch with a parser's own sentence on a command line. The launch
    happens, and this is what the person reads:
    what went wrong, that the tool is running on standard settings, and that
    their own file has not been touched.

    IT OPENS ALREADY SHOWING. Served with `on`, so the warning does not depend
    on any script having run - and it offers the report dialog that already
    exists rather than inventing a second way to report a problem.
    """
    if not message:
        return ""
    head, _, rest = message.partition(" - ")
    return (
        '<div class="ask on" id=pb-startup-ask role=dialog '
        'aria-labelledby=pb-startup-title>'
        f'<div class=box><h2 id=pb-startup-title>{html.escape(head)}</h2>'
        f'<p>{html.escape(rest or message)}</p>'
        '<div class=row>'
        '<button class="btn report" id=pb-startup-report>'
        f'{html.escape(REPORT_LABEL)}</button>'
        '<button class="btn save" id=pb-startup-ok>carry on</button>'
        '</div></div></div>'
    )


# The pseudo-layers the PAGE draws rather than the board carrying them, in
# stacking order. Both open ON: a proposal nobody sees has not been reviewed,
# and a warning the reader has to switch on is a warning nobody reads. Neither
# layer exists on a board that did not earn it.
PAGE_ANNOTATIONS = (prep.PROPOSAL_LAYER, prep.UNCUTTABLE_LAYER)


def annotation_layers(svg_by_layer) -> tuple:
    """Which page-drawn layers this view has, in stacking order.

    Named once because it answers two questions at the same time: what is
    marked as annotation, and what `start_visible` opens. The frozen review
    page builds the second from the first
    (:func:`eedesignpanels.prep.review`), and the live page does the same, so
    the red layer of uncuttable features arrives switched on rather than
    waiting to be found.
    """
    have = svg_by_layer or {}
    return tuple(layer for layer in PAGE_ANNOTATIONS if layer in have)


def build(config, view, generation: int, backend: str,
          session_dir: str = "", shot: int = 0, pending: int = 0,
          layers_seq: int = 0, content_note: str = "",
          paint_seq: int = 0, startup_warning: str = "",
          error: str = "", error_at=None, alarm_layers=None,
          theme_colors=None,
          layers=None, theme=None, layer_names=None) -> str:
    """The whole served page: viewport, layer panel, controls, handlers.

    *session_dir* is the launch's recording folder, shown in the footer so the
    user can say where a shot went without leaving the page, *shot* is
    the newest shot number already saved into it, and *pending* is how many
    edits are waiting for the save button. *layers_seq* is how many layer
    changes the session has seen, so a page that opens after one does not
    re-apply it.

    THE PAGE HAS THE SAME SHAPE EITHER WAY. *view*
    is None when the board could not be prepared, and then only the viewport
    changes: the failure is drawn in the card the plot would have filled
    (:func:`failed_viewport`) and the header, the step links, the report button,
    the layer panel and every setting come up exactly as they always do. The
    step-2 page works the same way.

    *error* and *error_at* are the reason and the place that card states.

    *alarm_layers* is the session's alarm state
    (:func:`eedesignpanels.preview.layer_alarms.status`): which layers are drawn
    in the alarm colour and, in one line each, why. It is served with the page
    and re-stated by every fragment, because a warning that a repaint can erase
    is worse than no warning at all.

    *theme_colors* is every row's swatch colour as the THEME has it, with no
    alarm substituted into it. It is what lets a paint put a row back when its
    alarm clears: the swatch is a server-written inline style, so a page with no
    statement of the true colour could turn a row red and never turn it back.
    *layers*, *theme* and *layer_names* are the board's own layer table, stated
    by the caller when there is no view to take it off - a board that loaded and
    then failed in prep still has a layer panel. With no layers at all the page
    comes without a viewport and without that panel, exactly as step 2 does.
    """
    built = view is not None
    stack = ""
    drawn = False
    for layer in (view.layers if built else ()):
        svg_path = view.svg_by_layer.get(layer.canonical_name)
        if svg_path is None:
            continue
        embedded = page.inline_svg(
            Path(svg_path), f"svg-0-{layer.ordinal}", layer.canonical_name
        )
        drawn = True
        stack += embedded.markup
    if built and not drawn:
        raise ValueError("no SVG rendered for the preview")

    stem = Path(config.source_board_path).stem if config is not None else ""
    board_name = Path(config.source_board_path).name if config is not None else ""
    heading = f"Preview - {stem}" if stem else "Preview"
    card = (
        f'<div class=card data-viewport="{BOARD_VIEWPORT}">'
        f"<div class=head><h2>Board edge, tab stops and cutter path - "
        f"{html.escape(board_name)}</h2>"
        '<button class="btn fit">fit</button></div>'
        f'<div class=viewport style="background:{view.theme.background}">'
        f"{stack}{await_html(content_note)}{alarm_html(alarm_layers)}</div>"
        '<div class=hud><span>zoom <b class=zoom>100%</b></span>'
        "<span>cursor <b class=pos>-</b> mm</span>"
        f"<span><b class=ext>{html.escape(extent_text(view))}</b></span></div>"
        "</div>"
    ) if built else failed_viewport(error, error_at)

    annotations = annotation_layers(view.svg_by_layer) if built else ()
    # Holes stack above every board layer, active one included: a hole belongs
    # to no layer and the PCB editor draws it last. They open ON - a tab IS its
    # drill holes, and one click hides them again.
    on_top = (
        (holes.HOLES_LAYER,)
        if built and holes.HOLES_LAYER in view.svg_by_layer else ()
    )
    # THE BOARD'S LAYER TABLE, from the view when there is one and from the
    # caller when there is not. The layer panel is a session-wide setting, so it
    # is worth having on a page with no plot under it: what is switched off here
    # is switched off on step 2 as well.
    rows = list(view.layers) if built else list(layers or ())
    swatches = view.theme if built else theme
    names = dict(view.layer_names if built else (layer_names or {}))
    listed = bool(rows) and swatches is not None
    score_layer = names.get(
        route_toolpath.SCORE_LAYER, route_toolpath.SCORE_LAYER
    )
    panel = layerpanel.panel_html(
        layerpanel.ONE_VIEW_PANEL,
        # The row tooltips take the board's display names too: a tooltip reading
        # `User.38 - user` is the UI showing a bare name.
        layerpanel.rows_html(rows, theme=swatches, title_names=names),
    ) if listed else ""
    # The layer component and the viewport wiring go out together or not at all:
    # `layerPanel` returns null when its element is absent and the keyboard
    # handler would then throw on the first keypress. Step 2 works the same way.
    scripts = (
        "<script>"
        + layerpanel.panel_script(
            rows, (*prep.REVIEW_LAYERS, *annotations, *on_top),
            swatches, annotations, on_top,
        )
        + f"</script>\n<script>{page.JS}</script>"
    ) if listed else ""
    state = {
        "page": PREP_STEP.page,
        # The name this tab adopts, so the other page's link can find it.
        "stepWindow": PREP_STEP.window,
        "steps": steps_json(),
        "fragmentUrl": "/fragment",
        "generation": generation,
        "backend": backend,
        "scoreLayerName": score_layer,
        # THE ALARM STATE, AND THE TWO COLOURS A PAINT NEEDS TO APPLY IT.
        # The state is re-stated by every
        # fragment; these two never move, so they are baked in once.
        "alarm_layers": list(alarm_layers or []),
        "alarmColor": theme_module.ALARM_COLOR,
        "layerColors": dict(theme_colors or {}),
        # WHETHER THERE IS A BOARD ON SCREEN AT ALL, read by the page exactly as
        # a fragment's own `ok` is: a served failure and a
        # failure that arrives later look the same to the script.
        "ok": built,
        "error": error,
        "proposals": proposals_json(view) if built else [],
        "params": params_json(config),
        "warnings": list(view.notes) if built else ([error] if error else []),
        # Why the cutter path is painted in the alarm colour, when it is
        # (`checks.toolpath_alarms`). Empty is the ordinary board.
        "alarms": list(view.alarms) if built else [],
        "session": session_dir,
        "shot": shot,
        "pending": pending,
        "pendingChanges": [],
        "layersSeq": layers_seq,
        "layerCount": len(rows),
        # What the corner of the viewport says on the first paint, before any
        # fragment has arrived to keep it true.
        "content_note": content_note,
        "upgradeSeq": 0,
        # WHICH PAINT THE STACK ABOVE IS, so this page's first fragment asks
        # for what has moved since it was served rather than for all of it
        # (`fragmentUrl`). A page served without it asks for everything once,
        # which is correct and merely not free.
        "paintSeq": paint_seq,
    }
    return f"""<!doctype html><meta charset=utf-8>
<title>{html.escape(heading)}</title>
{icons.BOARD_ICON}
<style>{page.CSS}{layerpanel.PANEL_CSS}{CONTROLS_CSS}</style>
<div class=main>
  <div class=titlebar>
    <div class=grow><h1>{html.escape(heading)}</h1>
      <div class=sub>active layer: <b class=lp-active></b></div></div>
    {_steps_html(PREP_STEP.path)}
    {_tools_html()}
  </div>
  <div class="views" id=views>{card}{_busy_popup_html()}</div>
  {_footer_html(session_dir, shot)}
</div>
{panel}
{_controls_html(generation, backend, score_layer, pending)}
{_unsaved_prompt_html()}
{_report_prompt_html(report_folder_text(config))}
{_startup_warning_html(startup_warning)}
{scripts}
<script>{page_script(PREP_JS, state)}</script>
"""


FAILURE_TITLE = "The board could not be prepared"
# What to do about it, in the order a person can act. The card says what
# failed, where, and what to try; the first two are the board's own words, and
# these lines are the third.
FAILURE_ADVICE = (
    "Open the board in KiCad and look at that point on the outline.",
    "A number changed on the right is tried straight away.",
    "Try again re-reads the board and the settings from disk.",
)
# The coordinate a stage names, as it appears in its own sentence. Lifted out of
# the reason so the place gets a line of its own, and left in place when the
# sentence does not carry one.
_AT_MM = re.compile(r"\s+at \(-?[\d.]+, -?[\d.]+\) mm\.?$")


def failure_words(error: str, at=None) -> dict:
    """A failed board load in plain words: what failed, and where.

    `reason` is the stage's own sentence with the coordinate taken off the end,
    `where` is that coordinate in millimetres, and either may be empty - a
    failure that named no place gets no place line rather than a made-up one.
    """
    reason = (error or "").strip() or "the reason was not recorded"
    trimmed = _AT_MM.sub("", reason)
    where = ""
    if at is not None and len(tuple(at)) == 2:
        point = tuple(at)
        where = f"{float(point[0]):.4f}, {float(point[1]):.4f} mm"
    if where:
        reason = trimmed
    return {"reason": reason, "where": where}


def failed_viewport(error: str, at=None) -> str:
    """THE FAILURE IS DRAWN WHERE THE BOARD WOULD HAVE BEEN.

    Only the board view is affected by a failed board load. Everything else on
    the page still comes up, so the report button and the heal setting can be
    seen and adjusted - a failed load is the one state in which a person needs
    the rest of the tool most. So this is a CARD, not a page.

    The card holds a `.viewport` with no plot in it on purpose: the shared
    viewport wiring (`review.page`) walks every `.card`, finds no SVG under this
    one and leaves it alone, and a shot serializes nothing from it.
    """
    words = failure_words(error, at)
    where = (
        f'<div class=where>where <b id=pb-failed-where>'
        f'{html.escape(words["where"])}</b> on the board</div>'
        if words["where"] else ""
    )
    advice = "".join(
        f"<li>{html.escape(line)}</li>" for line in FAILURE_ADVICE
    )
    return (
        f'<div class="card failed" data-viewport="{BOARD_VIEWPORT}">'
        f"<div class=head><h2>{html.escape(FAILURE_TITLE)}</h2></div>"
        '<div class=viewport><div class=failbox>'
        f'<p class=reason id=pb-failed-reason>'
        f'{html.escape(words["reason"])}</p>'
        f"{where}"
        f"<ul class=advice>{advice}</ul>"
        '<button class=btn id=pb-failed-again data-act=again>try again'
        "</button>"
        "</div></div></div>"
    )
