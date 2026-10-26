"""The layer panel: one component, used by every review artifact.

KiCad's Appearance panel is the thing an engineer already knows how to read -
board order, display names, a mark per row, the active layer highlighted - so
both review artifacts present their layers exactly that way and neither invents
its own list. The panel review page instantiates it to switch layers on and
off; the gerber diff page instantiates it to choose which layer's comparison is
on screen.

What differs between the two is only what a row *marks*:

* the **viewer** carries the KiCad theme's colour swatch and a visibility eye;
* the **diff** carries a pass/fail dot against the comparison threshold, and no
  swatch at all - the diff images use one fixed colour scheme for every layer
  (grey where the two agree, the complementary pair where they do not), so a
  per-layer colour beside them would be a second meaning for colour on the
  same screen.

Everything here is markup, CSS and JS as plain strings. It imports no
rasterizer and no board reader, so any artifact can carry it.
"""

import html
import json
from typing import NamedTuple

from eedesignpanels.review.render import LayerSpec
from eedesignpanels.review.theme import Theme

# A layer's comparison status, as the diff panel's mark. "none" is the honest
# third state: the layer was not compared, which is not a pass.
STATUS_OK = "ok"
STATUS_BAD = "bad"
# Inside the allowance, but holding at least one difference WIDER THAN A
# SINGLE MEASURING PIXEL - a real shape rather than the instrument's own
# rounding. Its own mark because its presence is worth looking at, and not
# BAD because nothing moved far enough to fail.
STATUS_WATCH = "watch"
# Measured, reported, and deliberately not judged: a documentation layer is
# replicated onto the panel but is not what the bare board is built from.
STATUS_INFO = "info"
STATUS_NONE = "none"

_STATUS_LABEL = {
    STATUS_OK: "matches",
    STATUS_BAD: "MOVED TOO FAR",
    STATUS_WATCH: "has a difference wider than one pixel",
    STATUS_INFO: "shown for information only",
    STATUS_NONE: "not compared",
}

# Which layers drive physical PCB construction. Written as a RULE over the
# board's own layer table rather than a list of today's names, so a board
# carrying a v-score or route layer gets it right with nobody editing this
# file.
_PHYSICAL_SUFFIXES = (".cu", ".mask", ".silks", ".silkscreen", ".paste")
_PHYSICAL_TOKENS = ("edgecuts", "routetoolpath", "vscore", "edgeplating")


def is_physical_layer(layer: LayerSpec) -> bool:
    """Does this layer end up as something on the finished board?

    Copper, solder mask, solder paste, silkscreen and the board outline do, and
    so do the routing and scoring layers that tell a fabricator where to cut -
    whatever a given board happens to name them. Courtyards, fabrication notes,
    dimensions and user drawings do not.

    **One rule, two consumers.** It decides which
    layers a review page shows by default, and which layers a comparison is
    allowed to *judge*. Both are the same question - does this become the
    board - so they get the same answer from the same place instead of
    drifting apart as two lists. Documentation layers are still replicated onto
    the panel and still measured; they simply do not set a verdict.
    """
    canonical = layer.canonical_name.lower()
    if any(canonical.endswith(suffix) for suffix in _PHYSICAL_SUFFIXES):
        return True
    # Matched with separators stripped, so `User.38-VScore`, `V.Score` and
    # `VSCORE` all answer the same.
    flattened = "".join(
        character
        for character in f"{layer.canonical_name}{layer.display_name}".lower()
        if character.isalnum()
    )
    return any(token in flattened for token in _PHYSICAL_TOKENS)


class PanelSpec(NamedTuple):
    """One instance of the panel: its id, its heading and what its rows mark."""

    panel_id: str
    heading: str
    modebar: str
    legend: str


PANEL_CSS = """
.layerpanel{width:255px;flex:none;background:#1c1f25;border:1px solid #2a2f37;
            border-radius:8px;display:flex;flex-direction:column;height:100%;
            color:#e6e8eb;font:13px/1.45 system-ui,sans-serif}
.layerpanel header{padding:.5rem .7rem;border-bottom:1px solid #2a2f37;
                   font-weight:600;font-size:12.5px}
.layerpanel .lp-mode{padding:.4rem .7rem;border-bottom:1px solid #2a2f37;
                     color:#8b939d;font-size:11px;display:flex;
                     justify-content:space-between;align-items:center;gap:.4rem}
.layerpanel .lp-mode b{color:#79b8ff;font-family:ui-monospace,monospace;
                       overflow:hidden;text-overflow:ellipsis}
.layerpanel .lp-list{overflow-y:auto;flex:1;padding:.2rem 0}
.layerpanel .lp-row{display:flex;align-items:center;gap:.45rem;
                    padding:.11rem .7rem;cursor:pointer;white-space:nowrap;
                    font-size:12px;border-left:3px solid transparent}
.layerpanel .lp-row:hover{background:#232830}
.layerpanel .lp-row.active{background:#2b3240;border-left-color:#79b8ff}
.layerpanel .lp-row.off .lp-nm{color:#5b636d;text-decoration:line-through}
.layerpanel .lp-eye{width:13px;text-align:center;color:#8b939d;flex:none;
                    font-size:11px}
.layerpanel .lp-sw{width:13px;height:13px;border-radius:2px;flex:none;
                   border:1px solid #00000060}
.layerpanel .lp-nm{overflow:hidden;text-overflow:ellipsis;flex:1}
.layerpanel .lp-count{flex:none;font:11px ui-monospace,monospace;color:#c9d1d9;
                      font-variant-numeric:tabular-nums}
.layerpanel .lp-count.zero{color:#4b535d}
.layerpanel .lp-legend{padding:.5rem .7rem;border-top:1px solid #2a2f37;
                       color:#6e7681;font-size:11px;line-height:1.5}
.layerpanel .lp-note{color:#79b8ff}
.layerpanel .btn{background:#262b33;border:1px solid #363d47;color:#c9d1d9;
                 border-radius:4px;padding:.15rem .5rem;
                 font:11px ui-monospace,monospace;cursor:pointer}
.layerpanel .btn:hover{background:#2f3540;color:#fff}
.layerpanel kbd{background:#262b33;border:1px solid #363d47;border-radius:3px;
                padding:0 .25rem;font:10px ui-monospace,monospace;color:#c9d1d9}
.lp-status{width:12px;height:12px;border-radius:50%;flex:none;
           border:1px solid #00000060;display:inline-block}
.lp-status.ok{background:#2ea043}
.lp-status.bad{background:#f2483c;box-shadow:0 0 0 2px #f2483c40}
.lp-status.watch{background:#e8a13a;border-color:#8a5a10}
.lp-status.info{background:#3f4c5c;border-color:#79b8ff}
.lp-status.none{background:#20242a;border-color:#4b535d}
"""

# `LAYERS` is substituted by :func:`layers_json`. Everything below reads it and
# nothing else from the page, which is what lets two different artifacts host
# the same component.
PANEL_JS = r"""
const LAYERS = __LAYERS__;
const INACTIVE_MODES = ['normal', 'dimmed', 'hidden'];
const EDGE = Math.max(0, LAYERS.findIndex(l => l.canonical === 'Edge.Cuts'));

function remember(key, value) {
  // file:// is an opaque origin in Chrome and localStorage throws there.
  try { localStorage.setItem(key, value); } catch (e) { /* not fatal */ }
}
function recall(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch (e) {
    return fallback;
  }
}

// V pairs a layer with its opposite side. Derived from the names: F.Cu <-> B.Cu,
// and for user layers the display name carries the token
// (User.6-F.ConformalCoat <-> User.7-B.ConformalCoat).
function flipped(index) {
  const layer = LAYERS[index];
  // Front to back, or back to front, without a two-step that could swap
  // a name back to itself.
  const swap = s => {
    const toBack = s.replace(/(^|[.\-])F\./, '$1B.');
    if (toBack !== s) return toBack;
    return s.replace(/(^|[.\-])B\./, '$1F.');
  };
  const canonical = swap(layer.canonical);
  if (canonical !== layer.canonical) {
    const i = LAYERS.findIndex(l => l.canonical === canonical);
    if (i >= 0) return i;
  }
  const bare = s => s.replace(/^User\.\d+-/, '');
  const want = swap(bare(layer.display));
  if (want !== bare(layer.display)) {
    const i = LAYERS.findIndex(l => bare(l.display) === want);
    if (i >= 0) return i;
  }
  return -1;
}

// One panel. `options.onChange(panel)` is the host's whole interface to it:
// the component owns the list and the selection, the host owns what a
// selection means.
function layerPanel(id, options) {
  const root = document.getElementById(id);
  if (!root) return null;
  const rows = [...root.querySelectorAll('.lp-row')];
  const panel = {
    active: EDGE,
    mode: 0,
    // Only the layers that become physical board start on, plus any the page
    // asked for by name. Stacking forty SVGs is what makes the preview crawl,
    // and the rest get switched off by hand anyway. `all` is one button away.
    visible: new Set(
      LAYERS.filter(l => !options.eyes || l.start).map(l => l.canonical)
    ),
    eyes: !!options.eyes,
    anchor: EDGE,
  };

  panel.paint = () => {
    rows.forEach((row, i) => {
      const on = panel.visible.has(LAYERS[i].canonical);
      row.classList.toggle('active', i === panel.active);
      row.classList.toggle('off', panel.eyes && !on);
      const eye = row.querySelector('.lp-eye');
      if (eye) eye.textContent = on ? '◉' : '○';
    });
    const mode = root.querySelector('.lp-modename');
    if (mode) mode.textContent = INACTIVE_MODES[panel.mode];
    document.querySelectorAll('.lp-active').forEach(
      el => { el.textContent = LAYERS[panel.active].display; });
    // The host's onChange is what reveals content, so nothing above it may
    // throw first and leave the page showing nothing at all.
    try {
      if (rows[panel.active]) {
        rows[panel.active].scrollIntoView({block: 'nearest'});
      }
    } catch (e) { /* scrolling is a convenience, never a precondition */ }
    options.onChange(panel);
  };

  const setVisible = (index, on) => {
    const name = LAYERS[index].canonical;
    if (on) panel.visible.add(name); else panel.visible.delete(name);
  };

  rows.forEach((row, i) => {
    row.onclick = event => {
      if (panel.eyes && event.target.classList.contains('lp-eye')) {
        // Shift+click paints the whole run from the last eye clicked to this
        // one with the state this click is applying - the same gesture a file
        // manager uses, so a block of layers is one click and not twenty.
        const on = !panel.visible.has(LAYERS[i].canonical);
        if (event.shiftKey) {
          const from = Math.min(panel.anchor, i);
          const to = Math.max(panel.anchor, i);
          for (let j = from; j <= to; j++) setVisible(j, on);
        } else {
          setVisible(i, on);
        }
        panel.anchor = i;
      } else {
        panel.active = i;
      }
      panel.paint();
    };
  });
  const all = root.querySelector('.lp-all');
  if (all) all.onclick = () => {
    LAYERS.forEach(l => panel.visible.add(l.canonical));
    panel.paint();
  };
  const none = root.querySelector('.lp-none');
  if (none) none.onclick = () => {
    panel.visible.clear();
    panel.visible.add(LAYERS[panel.active].canonical);
    panel.paint();
  };

  // Select by canonical name or by the gerber key - the diff report knows
  // layers by the latter.
  panel.select = name => {
    const i = LAYERS.findIndex(l => l.canonical === name || l.key === name);
    if (i >= 0) { panel.active = i; panel.paint(); }
  };
  // `+` AND `-` STEP TO THE NEXT VISIBLE LAYER.
  // They walked board order including the layers switched off, so a press
  // moved the active layer onto something nothing draws and the page did not
  // change - a key that reads as broken. Only a visible layer can be active
  // now. With nothing else visible the keys stay put, so the search ends
  // rather than circling the list forever.
  const nextVisible = delta => {
    const count = LAYERS.length;
    for (let n = 1; n <= count; n++) {
      const i = ((panel.active + delta * n) % count + count) % count;
      if (!panel.eyes || panel.visible.has(LAYERS[i].canonical)) return i;
    }
    return panel.active;
  };
  panel.key = event => {
    const key = event.key.toLowerCase();
    if (key === 'h') panel.mode = (panel.mode + 1) % 3;
    else if (key === 'v') { const i = flipped(panel.active);
                            if (i >= 0) panel.active = i; }
    else if (event.key === '+' || event.key === '=')
      panel.active = nextVisible(1);
    else if (event.key === '-' || event.key === '_')
      panel.active = nextVisible(-1);
    else return false;
    panel.paint();
    return true;
  };
  return panel;
}
"""

# The one line that is about the PAGE rather than the layers: it belongs to a
# page with two viewports, and there is exactly one of those.
_LAYOUT_KEY_LINE = "<kbd>L</kbd> side by side / stacked<br>"

VIEWER_PANEL = PanelSpec(
    panel_id="lp-viewer",
    heading="Appearance",
    modebar=(
        "<span>inactive layers</span><b class=lp-modename>normal</b>"
        '<span><button class="btn lp-all">all</button> '
        '<button class="btn lp-none">none</button></span>'
    ),
    legend=(
        "<kbd>H</kbd> inactive: normal / dimmed / hidden<br>"
        "<kbd>V</kbd> flip front &harr; back<br>"
        "<kbd>+</kbd> <kbd>-</kbd> step active layer<br>"
        + _LAYOUT_KEY_LINE +
        "click a name to activate, &#9673; to hide<br>"
        "<kbd>shift</kbd>+&#9673; toggles the whole range<br>"
        "scroll over a board to zoom &middot; double-click fits<br>"
        "<span class=lp-note>fabrication layers start visible</span>"
    ),
)

# The same panel on a page that shows ONE board. The served
# preview pages have a single viewport each, so the layout key is not offered
# there - a legend that lists a key which does nothing is a legend nobody
# trusts.
ONE_VIEW_PANEL = VIEWER_PANEL._replace(
    legend=VIEWER_PANEL.legend.replace(_LAYOUT_KEY_LINE, "")
)

DIFF_PANEL = PanelSpec(
    panel_id="lp-diff",
    heading="Layers &middot; comparison",
    modebar="<span>showing</span><b class=lp-active></b>",
    legend=(
        '<span class="lp-status ok"></span> matches &middot; '
        '<span class="lp-status bad"></span> moved too far<br>'
        '<span class="lp-status watch"></span> has a difference wider than '
        "one pixel<br>"
        '<span class="lp-status info"></span> shown for information only<br>'
        "the number is that layer's differing pixels<br>"
        "<kbd>+</kbd> <kbd>-</kbd> step layer &middot; click a name to show it"
        "<br>full figures for every layer are at the foot of the page"
    ),
)


def layers_json(layers: list[LayerSpec], start_visible=None, theme=None,
                annotations=None, on_top=None) -> str:
    """The layer table as the component's JS sees it, in board order.

    ``key`` is the name kicad-cli gives the layer's exported files - display
    name, dots to underscores - which is also how the comparison reports it, so
    a row matches its diff section without a second lookup table.

    ``physical`` and ``start`` are deliberately two fields. ``physical`` answers
    "does this become the finished board", which is the shared rule the
    comparison also judges verdicts by. ``start`` answers the narrower question
    "is this switched on when the page opens", and defaults to the same answer.

    A page built around one stage may name extra layers in *start_visible* when
    its whole subject is a layer the generic rule does not call physical - a
    page promising tab stop lines has to open showing them - and doing it here
    rather than by widening the rule keeps the verdict set where it was.

    *annotations* names pseudo-entries the PAGE draws rather than the board
    carries - a v-score proposal overlay, say. They are never physical whatever
    they are called: an annotation does not become the finished board, and the
    name test would otherwise call "V-score candidates" a fabrication layer.
    Their opening state comes from *start_visible* alone.

    *on_top* names entries that stack ABOVE every board layer, active one
    included. That is for one thing only: holes. A hole belongs to no layer -
    the PCB editor draws it last, over everything - and stacking it in board
    order means whatever is plotted after it paints the marks out. Like an
    annotation it is not a layer of the board, so it is never physical. It buys
    stacking order and nothing else: an on-top entry still dims and hides with
    the rest when H changes the view mode.
    """
    also_on = set(start_visible or ())
    drawn_by_page = set(annotations or ())
    above_everything = set(on_top or ())

    def physical(layer: LayerSpec) -> bool:
        return (
            layer.canonical_name not in drawn_by_page
            and layer.canonical_name not in above_everything
            and is_physical_layer(layer)
        )

    return json.dumps([
        {
            "canonical": layer.canonical_name,
            "display": layer.display_name,
            "type": layer.layer_type,
            "key": layer.svg_suffix,
            # Decided here rather than in the browser: it is a property of the
            # board, and it is testable in Python.
            "physical": physical(layer),
            "start": physical(layer) or layer.canonical_name in also_on,
            # The theme's alpha channel, applied by the viewer because
            # kicad-cli exports every layer opaque regardless of the theme.
            "alpha": theme.layer_alpha(layer.canonical_name) if theme else 1.0,
            # Drawn above everything, whatever the active layer is.
            "top": layer.canonical_name in above_everything,
        }
        for layer in layers
    ])


def panel_script(layers: list[LayerSpec], start_visible=None, theme=None,
                 annotations=None, on_top=None) -> str:
    """The component's JS with this board's layer table baked in."""
    return PANEL_JS.replace(
        "__LAYERS__",
        layers_json(layers, start_visible, theme, annotations, on_top),
    )


def rows_html(
    layers: list[LayerSpec],
    theme: Theme = None,
    status_by_layer: dict = None,
    counts_by_layer: dict = None,
    title_names: dict = None,
) -> str:
    """The rows, board order. A theme gives swatches; a status map gives marks.

    ``counts_by_layer`` puts each layer's differing-pixel count on its row, so
    the panel answers "where is the difference" without a trip to the table.

    ``title_names`` maps a canonical name to what the row's TOOLTIP should call
    it. The served preview passes the board's display names, because a tooltip
    reading `User.38 - user` is the UI showing a bare canonical name; omitting
    it keeps the tooltip the frozen artifacts already carry.

    Raises:
        ValueError: when given neither or both. A row marks one thing, and
            which one is the difference between the two artifacts.
    """
    if (theme is None) == (status_by_layer is None):
        raise ValueError(
            "a layer panel row marks either a theme colour or a comparison "
            "status - give exactly one of theme and status_by_layer"
        )

    out = ""
    for i, layer in enumerate(layers):
        # Plain text: this is a `title` attribute, escaped once on the way
        # out. Building it with HTML entities got them escaped too, and the
        # tooltip read "F.Cu &middot; signal".
        named = (title_names or {}).get(
            layer.canonical_name, layer.canonical_name
        )
        title = f"{named} - {layer.layer_type}"
        if theme is not None:
            mark = (
                "<span class=lp-eye>&#9673;</span>"
                f'<span class=lp-sw style="background:'
                f'{theme.layer_color(layer.canonical_name)}"></span>'
            )
        else:
            status = status_by_layer.get(layer.canonical_name, STATUS_NONE)
            title += f" - {_STATUS_LABEL[status]}"
            mark = f'<span class="lp-status {status}"></span>'
        count = ""
        if counts_by_layer is not None:
            differing = counts_by_layer.get(layer.canonical_name)
            if differing:
                count = f'<span class="lp-count">{differing:,}</span>'
                title += f" - {differing:,} differing pixels"
            else:
                count = '<span class="lp-count zero">0</span>'
        out += (
            f'<div class=lp-row data-i="{i}" title="{html.escape(title)}">'
            f"{mark}"
            f"<span class=lp-nm>{html.escape(layer.display_name)}</span>"
            f"{count}</div>"
        )
    return out


def panel_html(spec: PanelSpec, rows: str) -> str:
    """One instance of the panel, ready to place beside a viewport."""
    return (
        f'<aside class=layerpanel id="{spec.panel_id}">'
        f"<header>{spec.heading}</header>"
        f"<div class=lp-mode>{spec.modebar}</div>"
        f"<div class=lp-list>{rows}</div>"
        f"<div class=lp-legend>{spec.legend}</div>"
        "</aside>"
    )
