"""Build the panel review page: one self-contained HTML file.

Two viewports side by side - the **source board** and the **panel** built from
it - driven by one layer panel, so a layer is switched on in both at the same
moment and the two can be read against each other. The panel is the
shared component in :mod:`eedesignpanels.review.layerpanel`; the gerber diff
page instantiates the same one.

The page embeds kicad-cli's own plots. It never synthesises BOARD geometry and
never collapses repeated boards into `<use>` clones - a view assembled by our
own code can look right while the output is wrong, and that is the one failure
the review loop exists to catch. ANNOTATION is the deliberate exception, and it
is a different thing: the comparison page's delta markers and the prep page's
v-score proposal overlay say something ABOUT the board that is nowhere in it,
and they arrive as their own stack entry, marked as annotation and switchable
off. See :mod:`eedesignpanels.review.overlay`.

Only the SVG *document wrapper* is adapted for inlining: the XML declaration,
doctype and generated `<title>` are dropped, `width`/`height` give way to the
viewport, and the `viewBox` is padded. Not one drawn coordinate is touched.

Portability rules that are easy to regress and are kept here deliberately: no
external references of any kind, system fonts only, `localStorage` wrapped in
try/catch (Chrome treats `file://` as an opaque origin and throws), and
pan/zoom driven through the `viewBox` rather than a CSS transform, so the page
stays vector-crisp for whoever it is mailed to.
"""

import html
import re
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.pagezoom import REVIEW_STEP, wheel_factor_js
from eedesignpanels.review import layerpanel
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.review.theme import Theme

VIEWBOX_PAD_FRACTION = 0.02

_XML_DECL = re.compile(r"<\?xml.*?\?>", re.S)
_DOCTYPE = re.compile(r"<!DOCTYPE.*?>", re.S)
_TITLE = re.compile(r"<title>.*?</title>", re.S)
_VIEWBOX = re.compile(
    r'viewBox="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"'
)
_WIDTH = re.compile(r'\swidth="[^"]*"')
_HEIGHT = re.compile(r'\sheight="[^"]*"')


class BoardView(NamedTuple):
    """One viewport on the page: a caption and its per-layer SVG files."""

    title: str
    svg_by_layer: dict


class InlineSvg(NamedTuple):
    """An SVG ready to embed, with the board extents it reported."""

    markup: str
    width_mm: float
    height_mm: float


def padded_box(x: float, y: float, width: float, height: float,
               pad_mm: float = 0.0) -> str:
    """The viewBox a viewport is drawn in: an export's own box, with air round it.

    ONE formula, two callers. :func:`inline_svg` uses it on the bytes it embeds,
    and a served page uses it to tell a viewport that is already on screen what
    frame the next render arrived in (the panel page rebuilds in place rather
    than reloading). Two spellings of this arithmetic would be two frames that
    drift, and a picture drawn in the wrong one is a picture in the wrong place.
    """
    pad = max(max(width, height) * VIEWBOX_PAD_FRACTION, pad_mm)
    return (
        f"{x - pad:.4f} {y - pad:.4f} "
        f"{width + 2 * pad:.4f} {height + 2 * pad:.4f}"
    )


def empty_svg(element_id: str, canonical_name: str, box: str) -> str:
    """One layer's element with nothing in it yet, in the frame *box* names.

    The served pages update a viewport by swapping each layer's CONTENTS, so a
    page that has no picture yet still needs the elements to swap - the panel
    page is served before anything has been built. An empty stack is what turns
    the first build from a page reload into a paint: a build refreshes in place
    and never trips the unsaved-work guard.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'viewBox="{box}" data-base="{box}" id="{element_id}" '
        f'data-layer="{html.escape(canonical_name, quote=True)}" '
        'preserveAspectRatio="xMidYMid meet"></svg>'
    )


def overlay_svg(element_id: str, box: str) -> str:
    """An empty ANNOTATION element in the frame *box* names, stacked last.

    NOT a layer, and it says so: `data-overlay` keeps it out of the layer-set
    check a served page makes before it repaints (`page_served.paintSvgs`) and
    out of the layer panel, which has no row for it and should not have one.

    What it carries is drawn by the PAGE, in BOARD MILLIMETRES - the frame the
    render seam normalises every export into - so there is no transform
    anywhere. The clearance marking is its one user: the
    contents are the page's, the position is the viewport's, and no board data
    is involved either way.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'viewBox="{box}" data-base="{box}" id="{element_id}" '
        'data-overlay="1" preserveAspectRatio="xMidYMid meet"></svg>'
    )


def inline_svg(svg_path: Path, element_id: str, canonical_name: str,
               pad_mm: float = 0.0) -> InlineSvg:
    """Adapt one exported SVG for embedding. Geometry is left alone.

    The exported viewBox is exactly the board bounding box, so a stroke centred
    on the outline loses its outer half to the viewport edge. Padding by 2% of
    the larger dimension keeps the outline visible in a browser, which - unlike
    Inkscape - clips at the canvas.

    *pad_mm* is a FLOOR under that fraction, in millimetres, for a page carrying
    annotation that sits outside the board: the v-score captions are held a
    router diameter off the outline, which on a small board is several times the
    2%, and a caption drawn past the viewBox is a caption nobody sees. It is
    millimetres because the thing it has to hold is - the cutter does not scale
    with the board.
    """
    text = svg_path.read_text(encoding="utf-8")
    text = _XML_DECL.sub("", text)
    text = _DOCTYPE.sub("", text)
    # The generated <title> carries the render timestamp; drop it so the page
    # does not change from run to run.
    text = _TITLE.sub("", text)

    found = _VIEWBOX.search(text)
    if not found:
        raise ValueError(f"{svg_path} has no viewBox - not a kicad-cli export?")
    x, y, width, height = (float(value) for value in found.groups())
    box = padded_box(x, y, width, height, pad_mm)

    text = _WIDTH.sub("", text, count=1)
    text = _HEIGHT.sub("", text, count=1)
    text = text.replace(
        found.group(0),
        f'viewBox="{box}" data-base="{box}" id="{element_id}" '
        f'data-layer="{html.escape(canonical_name, quote=True)}" '
        'preserveAspectRatio="xMidYMid meet"',
        1,
    )
    return InlineSvg(text.strip(), width, height)


CSS = """
*{box-sizing:border-box}
body{font:13px/1.45 system-ui,sans-serif;background:#14161a;color:#e6e8eb;
     margin:0;padding:1rem;display:flex;gap:1rem;align-items:flex-start;
     height:100vh}
.main{flex:1;min-width:0;display:flex;flex-direction:column;gap:.8rem;
      height:100%}
h1{font-size:1.05rem;margin:0}
.sub{color:#8b939d;font-size:12px}
.views{display:flex;gap:.8rem;flex:1;min-height:0}
.views.stacked{flex-direction:column}
.titlebar{display:flex;align-items:flex-start;gap:.8rem}
.titlebar .grow{flex:1;min-width:0}
/* The whole card is unselectable, not only its viewport: selecting the title
   or the readout and then pressing into the image starts a native drag that
   eats pointermove, and panning looks frozen until you click away. */
.card{background:#1c1f25;border:1px solid #2a2f37;border-radius:8px;
      overflow:hidden;flex:1;display:flex;flex-direction:column;min-width:0;
      user-select:none;-webkit-user-select:none}
.head{display:flex;align-items:center;gap:.5rem;padding:.45rem .7rem;
      border-bottom:1px solid #2a2f37}
.head h2{font-size:12.5px;margin:0;font-weight:600;flex:1;white-space:nowrap;
         overflow:hidden;text-overflow:ellipsis}
.btn{background:#262b33;border:1px solid #363d47;color:#c9d1d9;
     border-radius:4px;padding:.15rem .5rem;font:11px ui-monospace,monospace;
     cursor:pointer}
.btn:hover{background:#2f3540;color:#fff}
.viewport{position:relative;flex:1;overflow:hidden;cursor:grab;
          touch-action:none;user-select:none;-webkit-user-select:none}
.viewport.drag{cursor:grabbing}
/* pointer-events off: every handler is bound to .viewport, and a press that
   lands on the svg lets the browser start a native selection drag, which
   swallows pointermove and freezes panning. */
.viewport svg{position:absolute;inset:0;width:100%;height:100%;display:block;
              pointer-events:none;-webkit-user-drag:none}
.hud{display:flex;gap:.9rem;padding:.3rem .7rem;
     font:11px ui-monospace,monospace;color:#8b939d;
     border-top:1px solid #2a2f37}
.hud b{color:#c9d1d9;font-weight:500}
"""

JS = r"""
const views = document.getElementById('views');

// The panel owns the list and the selection; this owns what a selection means.
const viewer = layerPanel('lp-viewer', {
  eyes: true,
  onChange: panel => {
    LAYERS.forEach((layer, i) => {
      const on = panel.visible.has(layer.canonical);
      const isActive = i === panel.active;
      let shown = on, opacity = layer.alpha ?? 1;
      // H dims or hides every layer that is not active, holes included:
      // dimmed dims them, hidden hides them. Being
      // drawn on top is about stacking order, not about being exempt.
      if (on && !isActive && panel.mode === 1) opacity *= 0.22;
      if (on && !isActive && panel.mode === 2) shown = false;
      document
        .querySelectorAll('svg[data-layer="' + CSS.escape(layer.canonical) + '"]')
        .forEach(el => {
          el.style.display = shown ? '' : 'none';
          el.style.opacity = opacity;
          // Holes are holes: they belong to no layer and the PCB editor draws
          // them last, over everything. Anything else stacks in board order,
          // with the active layer lifted clear of it.
          el.style.zIndex = layer.top ? 999 : (isActive ? 99 : 1);
        });
    });
  },
});

addEventListener('keydown', event => {
  if (event.target.tagName === 'INPUT') return;
  if (!viewer.key(event)) return;
  event.preventDefault();
});

// Every viewport on this page, and how a host re-frames one in place. A frozen
// artifact never calls it - its picture cannot change - but the served preview
// re-renders under a page that is already open, and a rebuilt panel is usually
// a different size. Handing the frame over here is what lets a build repaint
// instead of reloading the document.
const viewFrames = [];

// Pan and zoom drive the viewBox, so the render stays vector-crisp.
document.querySelectorAll('.card').forEach(card => {
  const viewport = card.querySelector('.viewport');
  const svgs = [...viewport.querySelectorAll('svg')];
  if (!svgs.length) return;
  let base = svgs[0].dataset.base.split(' ').map(Number);
  let view = base.slice();

  const apply = () => {
    const box = view.map(n => n.toFixed(4)).join(' ');
    svgs.forEach(el => el.setAttribute('viewBox', box));
    card.querySelector('.zoom').textContent =
      (base[2] / view[2] * 100).toFixed(0) + '%';
  };
  const metrics = () => {
    const rect = viewport.getBoundingClientRect();
    const scale = Math.min(rect.width / view[2], rect.height / view[3]);
    return {rect, scale,
            ox: (rect.width - view[2] * scale) / 2,
            oy: (rect.height - view[3] * scale) / 2};
  };
  const toBoard = (cx, cy) => {
    const {rect, scale, ox, oy} = metrics();
    return [view[0] + (cx - rect.left - ox) / scale,
            view[1] + (cy - rect.top - oy) / scale];
  };

  // A plain wheel over the board zooms, the way it does in KiCad. The default
  // is suppressed only when a zoom actually happens: at the limits, and
  // anywhere off the board, the page scrolls as it normally would.
  // The factor multiplies the view's EXTENT - see eedesignpanels.pagezoom.
  viewport.addEventListener('wheel', event => {
    const k = __WHEEL_ZOOM__;
    const ratio = view[2] * k / base[2];
    if (ratio > 40 || ratio < 0.002) return;
    event.preventDefault();
    const [bx, by] = toBoard(event.clientX, event.clientY);
    const {rect, scale, ox, oy} = metrics();
    view = [bx - (event.clientX - rect.left - ox) / (scale / k),
            by - (event.clientY - rect.top - oy) / (scale / k),
            view[2] * k, view[3] * k];
    apply();
  }, {passive: false});

  let drag = null;
  // Suppress the browser's own drag. Without it, a press that lands on the
  // selection an earlier pan left behind starts a native drag-and-drop; that
  // eats pointermove and panning appears to lock up until you click away.
  viewport.addEventListener('dragstart', event => event.preventDefault());
  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    drag = {x: event.clientX, y: event.clientY, vx: view[0], vy: view[1],
            scale: metrics().scale};
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('drag');
  });
  viewport.addEventListener('pointermove', event => {
    const [bx, by] = toBoard(event.clientX, event.clientY);
    card.querySelector('.pos').textContent =
      bx.toFixed(3) + ', ' + by.toFixed(3);
    if (!drag) return;
    view[0] = drag.vx - (event.clientX - drag.x) / drag.scale;
    view[1] = drag.vy - (event.clientY - drag.y) / drag.scale;
    apply();
  });
  const release = () => { drag = null; viewport.classList.remove('drag'); };
  viewport.addEventListener('pointerup', release);
  viewport.addEventListener('pointercancel', release);
  // Fit, three ways to ask for it: the button, a double-click on the board,
  // and the same gesture the comparison page uses - one convention, both pages.
  const fit = () => { view = base.slice(); apply(); };
  card.querySelector('.fit').onclick = fit;
  viewport.addEventListener('dblclick', fit);
  // The frame this viewport is drawn in, and the one way to change it. A frame
  // that has not moved is not a change: re-fitting on every refresh would throw
  // away a zoom the user set, so the answer says whether anything happened.
  viewFrames.push({
    card: card,
    box: () => base.slice(),
    rebase: next => {
      if (!next || next.length !== 4 || next.some(n => !isFinite(n))
          || next[2] <= 0 || next[3] <= 0) {
        return false;
      }
      if (next.every((n, i) => n === base[i])) return false;
      base = next.slice();
      const text = base.map(n => n.toFixed(4)).join(' ');
      svgs.forEach(el => { el.dataset.base = text; });
      view = base.slice();
      apply();
      return true;
    },
  });
  apply();
});

viewer.paint();
""".replace(
    "__WHEEL_ZOOM__", wheel_factor_js("event.deltaY", REVIEW_STEP)
)

# THE LAYOUT TOGGLE, AND ONLY WHERE THERE IS A LAYOUT. This
# page shows the source board and the panel side by side, so how the two sit is
# a real question and `L` answers it. The SERVED pages have one viewport each,
# where the same button toggles nothing and reads as a control that does not
# work - so it is not in the shared script at all, it is here, and only the
# frozen artifact loads it.
LAYOUT_JS = r"""
const layoutButton = document.getElementById('layout');
function setLayout(stacked) {
  views.classList.toggle('stacked', stacked);
  layoutButton.textContent = stacked ? 'stacked' : 'side by side';
  remember('pb-stacked', stacked ? '1' : '0');
}
layoutButton.onclick = () => setLayout(!views.classList.contains('stacked'));
setLayout(recall('pb-stacked', '0') === '1');

addEventListener('keydown', event => {
  if (event.target.tagName === 'INPUT') return;
  if (event.key.toLowerCase() !== 'l') return;
  setLayout(!views.classList.contains('stacked'));
  event.preventDefault();
});
"""


def _card(view: BoardView, layers: list[LayerSpec], index: int,
          background: str, pad_mm: float = 0.0) -> str:
    """One viewport: every layer of one board, stacked in draw order.

    One *pad_mm* for the whole card, because the layers are stacked over one
    another and are panned and zoomed as one: a viewBox that differed between
    them would slide the stack apart.
    """
    stack = ""
    extents = None
    for layer in layers:
        svg_path = view.svg_by_layer.get(layer.canonical_name)
        if svg_path is None:
            continue
        embedded = inline_svg(
            Path(svg_path), f"svg-{index}-{layer.ordinal}",
            layer.canonical_name, pad_mm=pad_mm,
        )
        extents = extents or (embedded.width_mm, embedded.height_mm)
        stack += embedded.markup
    if extents is None:
        raise ValueError(f"no SVG rendered for view {view.title!r}")

    return (
        "<div class=card>"
        f"<div class=head><h2>{html.escape(view.title)}</h2>"
        '<button class="btn fit">fit</button></div>'
        f'<div class=viewport style="background:{background}">{stack}</div>'
        '<div class=hud><span>zoom <b class=zoom>100%</b></span>'
        "<span>cursor <b class=pos>-</b> mm</span>"
        f"<span><b>{extents[0]:.2f} x {extents[1]:.2f} mm</b></span></div>"
        "</div>"
    )


def build_page(
    out_path: Path,
    heading: str,
    subtitle: str,
    layers: list[LayerSpec],
    views: list[BoardView],
    theme: Theme,
    start_visible=None,
    annotations=None,
    on_top=None,
    pad_mm=0.0,
) -> Path:
    """Write the self-contained review page. Returns the path written.

    ``start_visible`` names layers that are switched on when the page opens in
    addition to the physical ones. A page whose subject is a layer the generic
    rule does not call physical has to open showing it; see
    :func:`layerpanel.layers_json`.

    ``annotations`` names entries in *layers* that the page DREW rather than
    plotted - a proposal overlay. They stack and toggle like any other layer and
    are marked as what they are, so nothing downstream mistakes one for board
    data.

    ``on_top`` names entries that stack above every board layer including the
    active one. Holes, and only holes: they belong to no layer, KiCad draws
    them last, and stacked in board order they get painted out by whatever
    comes after them (:mod:`eedesignpanels.review.holes`). Stacking order is
    ALL it buys them - they dim and hide with the other inactive layers, and
    their own eye is what switches them off.

    ``pad_mm`` is the least air, in millimetres, to leave round the board in
    every viewport. Annotation drawn OUTSIDE the board needs it; see
    :func:`inline_svg`.
    """
    if not views:
        raise ValueError("a review page needs at least one board view")

    cards = "".join(
        _card(view, layers, i, theme.background, pad_mm=pad_mm)
        for i, view in enumerate(views)
    )
    panel = layerpanel.panel_html(
        layerpanel.VIEWER_PANEL, layerpanel.rows_html(layers, theme=theme)
    )
    script = layerpanel.panel_script(
        layers, start_visible, theme, annotations, on_top
    )
    page = f"""<!doctype html><meta charset=utf-8>
<title>{html.escape(heading)}</title>
<style>{CSS}{layerpanel.PANEL_CSS}</style>
<div class=main>
  <div class=titlebar>
    <div class=grow><h1>{html.escape(heading)}</h1>
      <div class=sub>{html.escape(subtitle)} &middot; active layer:
        <b class=lp-active></b></div></div>
    <button class=btn id=layout>side by side</button>
  </div>
  <div class="views" id=views>{cards}</div>
</div>
{panel}
<script>{script}</script>
<script>{JS}</script>
<script>{LAYOUT_JS}</script>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path
