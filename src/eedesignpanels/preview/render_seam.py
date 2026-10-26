"""One call that turns a board into per-layer SVG - today by kicad-cli, later not.

The live preview re-renders on every interaction, and kicad-cli costs a process
round trip plus a write of the whole board to disk before it will look at it.
The faster road is the library-side renderer in `eedesigncore`, which draws
straight from the `Board` object. This module is the seam both backends sit
behind.

**Feature detection, never a version pin.** The backend is chosen by asking the
module whether it has the function, at import, once - so any `eedesigncore`
checkout that carries the renderer uses it, with no version number to test.

The contract both backends keep:

* the caller supplies the **viewBox**, in board millimetres, so a board and a
  panel can be drawn into one frame - kicad-cli's crop-to-content is not wanted
  here and is normalised away;
* the caller supplies the **colours**, because the panel builder derives them
  from the user's KiCad theme;
* the result is **one document per layer**, mirroring `--mode-multi`, so the
  existing stacked viewport drives them with no special case;
* coordinates come out in board millimetres.

The one thing the kicad-cli backend cannot keep is the colours: it has no
per-layer colour argument, only `--theme`. It therefore renders with the theme
the caller's colours came from, which is the same answer by a longer road; a
caller that passes colours from anywhere else gets the theme and is told so by
this docstring rather than by a surprise on the page. That, and the translate
below, are why the frozen review artifact still takes the untouched kicad-cli
path: a review render embeds exactly what kicad-cli plotted, because a view
assembled by this project's own code could look right while the real output is
wrong. This seam serves the live preview only.

**CONTENT DECIDES THE BACKEND.** The library renderer draws DRAWINGS - board
graphics, footprint graphics, points, the stroke-font TEXT and the DIMENSIONS -
and draws no pad, no track and no zone; measured on the corpus board, `F.Cu`
comes back holding its graphics and none of its copper where kicad-cli writes
all of it. On a copper, mask or paste layer that picture would be wrong, which
is why :func:`render_layers` takes `content=True` and answers it with kicad-cli
whenever kicad-cli is there. The fast path stays the answer for the layer sets
it is faithful to, and remains the answer on a machine with no kicad-cli -
where :func:`content_notice` is the sentence the page owes its reader. The gap
itself belongs to the library and is not worked around here.

**THE COPPER OPT-IN IS DECLINED, MEASURED.** `render_board_svg` takes a
`content=` argument, so a caller may ask for copper tracks, vias and zones from
the fast path. This seam asks for :data:`FAST_CONTENT` - the drawings stream
alone - and states why at the call site, for two reasons that both point the
same way:

1. **It would draw a copper layer with no pads in it.** The library's traversal
   walks a footprint's drawings, points and fields and never its `pads`
   (`eedesigncore/render/traversal.py`), and its geometry holds no pad branch
   (`eedesigncore/render/geometry.py`), so `F.Cu` would come back with every
   track and not one pad. A half-drawn copper layer reads as a defect in the
   BOARD, which is the one failure the two-phase design exists to avoid: the fast phase is
   exact for what it draws and honestly empty for what it does not
   (:data:`content_notice`, `session.AWAITING_NOTE`).
2. **It costs more than the win it would spend.** Measured on the corpus panel,
   best of three: the drawings stream is 196 ms and 3.10 MB of markup, and the
   copper opt-in takes that to 282 ms and 4.18 MB. The fragment fetch is already
   the largest remaining slice of a panel edit's first paint, so +86 ms of
   render and +1.08 MB of page would be paid twice.

The missing pad is the library's to close, not this project's to work around.
Until it does, copper on a preview layer means kicad-cli.

**TWO PHASES, NOT ONE CHOICE.** Because the two backends
differ in what they draw AND in what they cost, an edit uses both: the fast one
paints immediately - graphics exact, content layers empty - and the content one
runs behind it and replaces the layers when it is ready.
:func:`two_phase_available` is the whole test for whether that is possible here,
and :func:`renderer_label` is what the page says about it. The session owns the
ordering (:mod:`eedesignpanels.preview.session`); this module only answers which
backend is which.

**HOLES ARE A ROW, NOT A LAYER.** A hole belongs to no layer - the PCB editor
draws it last, over everything - so a caller asks for one more entry named
`Holes.Drill` and gets one more document to stack on top, the same entry the
frozen review pages carry. Each backend produces it its own way: the library
draws its `Holes.Plated` / `Holes.NonPlated` pseudo-layers, and kicad-cli's
marks are lifted out of its copper exports by
:mod:`eedesignpanels.review.holes`, which is the road the review pages already
take. The two hole colours travel in *colors* under those two pseudo-layer
names.
"""

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from eedesignpanels import kicad_install
from eedesignpanels.review import holes as holes_module, render, theme as theme_module

# What the library renderer might be called. Today it is `render_board_svg`,
# with the contract above behind a ViewBox + StylePolicy signature; the other
# names stay as fallbacks so a rename upstream degrades to detection failure,
# never to a wrong call.
LIBRARY_RENDERER_NAMES = (
    "render_board_svg",
    "render_svg_layers",
    "render_layers_svg",
    "render_svg",
    "board_to_svg",
)

CLI_BACKEND = "kicad-cli"
LIBRARY_BACKEND = "eedesigncore"

# The row the drill marks stack on, shared with the frozen review pages so the
# two artifacts name the same thing the same way.
HOLES_LAYER = holes_module.HOLES_LAYER

# The library's own hole pseudo-layers. They are also the keys a caller puts the
# two hole COLOURS under, whichever backend answers - one spelling for both.
PLATED_HOLES = "Holes.Plated"
NON_PLATED_HOLES = "Holes.NonPlated"

# The name of the library's drawings stream, and the whole of what the fast
# path asks for. See "THE COPPER OPT-IN IS DECLINED" above: it is stated here
# rather than left to the library's default so that a later change to that
# default cannot quietly put half-drawn copper into the first paint.
FAST_CONTENT_STREAM = "drawings"

# What an SVG document has to contain before it is worth stacking. A layer with
# nothing on it still comes back as a document - a wrapper group and no
# elements - and a Holes row with no marks in it is a row that lies.
_DRAWN = re.compile(r"<(?:circle|path|rect|ellipse|polygon|polyline|line)\b")

# ---------------------------------------------------------------------------
# `point` items are not displayed at all
# ---------------------------------------------------------------------------
# A point never appears in a KiCad board export, so it never appears in a
# preview either. It is a GUI-only marker - `kicad-cli` exports nothing for one
# because KiCad's plot path holds no branch for the item - and a preview that
# drew it would be showing the user something no fabrication output will ever
# carry.
#
# This is a caller's presentation choice and not an upstream defect. The
# library's style policy is documented as doing both jobs through one hook -
# "``style_for`` returns ``None`` to drop an item, so a caller filters and
# styles through the same function" (`eedesigncore/render/style.py:1-8`) - and
# it is called per RENDER ITEM, so a per-item-kind drop is exactly expressible.
# The library ships the same decision as a preset for its own oracle,
# `CliParityStyle`, which drops `PcbPoint` for this very reason
# (`style.py:96-113`). :class:`PointsHidden` wraps whatever policy the caller
# built rather than using that preset, because the preset also pins opacity to
# 1.0 and turns off element ids, and the preview needs the theme's alphas.
#
# The two backends therefore now agree: neither draws a point.

# What the protocol asks a policy for when it DOES draw a marker. Nothing here
# ever draws one; the value is carried so the wrapper still satisfies
# `eedesigncore.render.StylePolicy`.
POINT_STROKE_MM = 0.05

_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.S)
_VIEWBOX = re.compile(r'viewBox="[^"]*"')


def find_library_renderer(module):
    """The upstream renderer on *module*, as `(name, callable)`, or `(None, None)`.

    Guarded by ATTRIBUTE, not by version: the library is consumed from a working
    checkout, so "does it have the function" is the only question that has an
    answer.
    """
    for name in LIBRARY_RENDERER_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return name, candidate
    return None, None


def _import_library():
    """`eedesigncore`, or None when it cannot be imported at all."""
    try:
        import eedesigncore
    except ImportError:
        return None
    return eedesigncore


def select_backend(module) -> str:
    """Which backend a given library module implies. Pure, so it is testable.

    *module* is `eedesigncore`, or None. This is the whole of the selection
    rule, kept apart from the import so a test can hand it a module that has the
    renderer and one that does not.
    """
    if module is None:
        return CLI_BACKEND
    name, _ = find_library_renderer(module)
    return LIBRARY_BACKEND if name else CLI_BACKEND


# Decided once, at import: the backend cannot change while the process runs.
BACKEND = select_backend(_import_library())


def refresh_backend() -> str:
    """Re-run the detection. For a session that installed the library since import."""
    global BACKEND
    BACKEND = select_backend(_import_library())
    return BACKEND


def cli_available(cli_path=None) -> bool:
    """Is kicad-cli where it is expected to be? Asked, never assumed."""
    return Path(cli_path or render.DEFAULT_KICAD_CLI).is_file()


def choose_backend(content: bool = False, cli_path=None) -> str:
    """Which backend answers a given request. Pure, so it is testable.

    *content* is the caller saying "these layers hold pads, tracks and zones" -
    which the library renderer does not draw. It is answered by kicad-cli when
    kicad-cli is installed, and by the detected backend when it is not: a
    picture missing its copper beats no picture at all, and
    :func:`content_notice` is what says so on the page.
    """
    if content and cli_available(cli_path):
        return CLI_BACKEND
    return BACKEND


def preview_backend() -> str:
    """The backend the LIVE PREVIEW renders with, which is what its pages say.

    The preview draws every layer the board declares, so it always asks for
    content: naming the detected backend in the footer while kicad-cli drew the
    picture would be the page reporting something that did not happen.
    """
    return choose_backend(content=True)


def fast_backend(cli_path=None) -> str:
    """The backend that answers WITHOUT content - the one an edit paints with.

    It is the detected backend, always: the fast path is the library renderer,
    and on a machine that has no library renderer it is kicad-cli, which is the
    same answer as the content one.
    """
    return choose_backend(content=False, cli_path=cli_path)


def two_phase_available(cli_path=None) -> bool:
    """Are the two backends DIFFERENT - is there a fast phase to paint first?

    THE WHOLE OF THE TWO-PHASE TEST. A content render costs a process round
    trip plus a write of the board: 0.84 s for the corpus board and 2.1 s for
    its panel, measured, against 0.03 s and 0.13 s for the library. So an edit
    paints the library's answer at once and the content render replaces it
    behind that.

    When the two backends are the SAME - no library renderer, so kicad-cli
    answers both - there is nothing to paint first and nothing to upgrade to, and
    a caller renders once as it always did.
    """
    return fast_backend(cli_path) != choose_backend(True, cli_path)


def tiered_available(cli_path=None) -> bool:
    """Is there a GRAPHICS tier to paint before the lettering one?

    Only the LIBRARY backend can be asked to leave the lettering out - it is a
    style policy, and kicad-cli takes no such argument - so this is the whole
    test for whether the fast phase can be two tiers. On a machine where
    kicad-cli answers the fast phase there is one tier, exactly as before.
    """
    return fast_backend(cli_path) == LIBRARY_BACKEND


def renderer_label(cli_path=None) -> str:
    """What the page says drew it. Both backends, when both of them did.

    A footer reading `kicad-cli` while the picture on screen came from the
    library is the page reporting something that did not happen - which is the
    same defect :func:`content_notice` exists for, one phase earlier.
    """
    content = choose_backend(True, cli_path)
    if not two_phase_available(cli_path):
        return content
    return f"{fast_backend(cli_path)} first, then {content} for the content"


def fast_content(render_module):
    """What the fast path asks `render_board_svg` for, or None when it cannot ask.

    Detected by ATTRIBUTE, like the renderer itself: an `eedesigncore` without
    `BoardContent` draws the drawings stream anyway because that is all it
    has. Returning None there means "pass no
    `content` argument", which is the only call an older renderer accepts.

    Named rather than taken from the library's `DEFAULT_CONTENT` so that the
    choice is THIS project's and stays put if upstream's default moves.
    """
    content = getattr(render_module, "BoardContent", None)
    if content is None:
        return None
    return frozenset({content(FAST_CONTENT_STREAM)})


def content_notice(backend: str) -> str:
    """What the PAGE says when the renderer that answered cannot draw content.

    Empty for kicad-cli, which draws everything. A shortfall a reader is not
    told about is a picture the reader will believe.
    """
    if backend == CLI_BACKEND:
        return ""
    return (
        f"copper, mask and paste are drawn EMPTY in this picture: kicad-cli "
        f"was not found at {render.DEFAULT_KICAD_CLI} or anywhere else this "
        f"tool looks ({kicad_install.SEARCH_TEXT}), so the board was drawn "
        f"by {backend}, which draws graphics, text, dimensions and holes, and "
        f"no pad, track or zone"
    )


def hole_palette(colors) -> dict:
    """The plotter-colour to editor-colour map, from the caller's own colours.

    kicad-cli paints a drill mark black or white and nothing else, so the lift
    needs to know what each of those two becomes. The caller states them under
    the library's pseudo-layer names, which is the one spelling both backends
    read.
    """
    colors = colors or {}
    palette = {}
    plated = colors.get(PLATED_HOLES)
    non_plated = colors.get(NON_PLATED_HOLES)
    if plated:
        palette[holes_module.KNOCKOUT_COLOR] = plated
    if non_plated:
        palette[holes_module.MARK_COLOR] = non_plated
    return palette


def has_drawing(markup: str) -> bool:
    """Does this document draw anything at all?"""
    return bool(markup) and bool(_DRAWN.search(markup))


def split_holes(layers) -> tuple:
    """`(board layers, wants holes)` - the Holes row taken out of a layer list.

    The row travels WITH the layers because that is how a caller says where it
    goes in the stack; it is not a layer of the board, so no backend may be
    asked to plot it.
    """
    wanted = [
        layer for layer in layers if layer.canonical_name != HOLES_LAYER
    ]
    return wanted, len(wanted) != len(layers)


@contextlib.contextmanager
def held(lock):
    """*lock*, held for this block - or nothing at all when there is none.

    The kicad-cli backend MUTATES the caller's board for the length of one write
    (see :func:`values_hidden`), and a caller whose board is read by another
    thread meanwhile would see those flags. So a caller that shares its board
    says how to make that window safe, and one that does not shares nothing.
    """
    if lock is None:
        yield
        return
    with lock:
        yield


@contextlib.contextmanager
def values_hidden(board):
    """*board* with every footprint Value hidden, and put back afterwards.

    kicad-cli has no equivalent of the PCB editor's Objects panel, so every
    Value the user keeps switched off comes back in the export and buries the
    geometry the page is about. The frozen review page does this on a COPY; here the flag is turned off, the
    board is written, and the flag is put back - which is the same picture
    without a second parse of a panel-sized board on every recomputation. The
    caller holds the session lock, and the restore is in a `finally`, so what
    the session holds is never left changed.
    """
    hidden = [
        prop
        for footprint in board.footprints
        for prop in footprint.properties
        if prop.name == "Value" and prop.visible
    ]
    for prop in hidden:
        prop.visible = False
    try:
        yield board
    finally:
        for prop in hidden:
            prop.visible = True


def _reframe(markup: str, viewbox_mm: tuple, translate: tuple) -> str:
    """Put a kicad-cli export into board millimetres, in the caller's window.

    kicad-cli's `--page-size-mode 2` crops to the board's bounding box, so its
    documents are board millimetres shifted to that box's corner. Two edits, both
    on the document WRAPPER and neither on a drawn coordinate: the root viewBox
    becomes the window the caller asked for, and the content is carried back by
    one rigid translation. The picture is the one kicad-cli drew.
    """
    x, y, width, height = viewbox_mm
    box = f'viewBox="{x:.6f} {y:.6f} {width:.6f} {height:.6f}"'
    opening = _SVG_OPEN.search(markup)
    if opening is None:
        raise ValueError("not an SVG document - no <svg> element")
    head = opening.group(0)
    head = _VIEWBOX.sub(box, head) if _VIEWBOX.search(head) else (
        head[:-1] + f" {box}>"
    )
    body = markup[opening.end():]
    closed = body.rfind("</svg>")
    if closed < 0:
        raise ValueError("not an SVG document - no </svg>")
    inner = body[:closed]
    return (
        f"{head}"
        f'<g transform="translate({translate[0]:.6f} {translate[1]:.6f})">'
        f"{inner}</g></svg>"
    )


def _export_frame(svg_path: Path) -> tuple:
    """The `(x, y)` corner of one export's own viewBox."""
    from eedesignpanels.review.overlay import viewbox_of

    box = viewbox_of(svg_path)
    return (box.x, box.y)


def render_with_cli(board, layers, viewbox_mm, colors, theme=None,
                    workdir=None, holes: bool = False, origin=None,
                    board_lock=None, project=None, filename=None) -> dict:
    """The content-complete backend: write the board to *workdir* and plot it.

    *workdir* is required - kicad-cli reads files, and this must never write
    beside the design (it drops a `.kicad_prl` sidecar next to whatever it
    renders).

    *holes* asks for the drill marks as one more document, keyed
    :data:`HOLES_LAYER`. They are lifted out of the copper exports this call
    already produced - the marks kicad-cli itself drew, never marks this code
    invented - and re-coloured through :func:`hole_palette`.

    *origin* is the BOARD POINT the export's own origin stands for, and it is
    the whole of the alignment. MEASURED: with `--page-size-mode 2` kicad-cli
    crops to the board outline's bounding box and writes `viewBox="0 0 w h"`,
    so an exported coordinate is `board - outline_bbox_min`. Carrying the
    content back by the caller's WINDOW origin instead would land every layer
    short by exactly the window margin, five millimetres on the corpus board,
    and put the plots out of register with the overlays drawn in board
    coordinates. The default is the board's own outline box, which is what
    KiCad crops to.

    *board_lock* is held for the WRITE and for nothing else. That write is the
    one moment this function MUTATES the caller's board - every visible `Value`
    is switched off and switched back on in a `finally` - so a caller
    whose board another thread may serialize meanwhile has to be able to close
    the window. The live preview's second phase passes its session lock: it
    renders for seconds without it and takes it for the fraction of a second
    anyone else could see. Without one, a panel derived inside that window would
    carry hidden Value fields into the file a fabricator gets.
    """
    if workdir is None:
        raise ValueError(
            "the kicad-cli backend needs a scratch folder to write the board "
            "into - it renders files, and it must not write beside the design"
        )
    # See `render_with_library`: the Holes row is not a layer to plot.
    layers, _ = split_holes(layers)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    board_path = workdir / "preview.kicad_pcb"
    from eedesigncore import TextResolutionContext, resolve_document_text

    with held(board_lock), values_hidden(board):
        display = resolve_document_text(board, TextResolutionContext(
            project=project, filename=filename,
            project_name=Path(filename).stem if filename else None,
        )).document
        display.to_file(board_path)
    project_path = board_path.with_suffix(".kicad_pro")
    if project is not None:
        project.to_file(project_path)
    else:
        project_path.unlink(missing_ok=True)

    name = theme or theme_module.active_theme_name()
    export = render.export_svgs(
        board_path, list(layers), workdir / "svg", theme=name
    )

    if origin is None:
        from eedesignpanels.stages.route_toolpath import outline_bbox

        origin = outline_bbox(board)[:2]

    out = {}
    shift = None
    for layer in layers:
        svg_path = export.svg_by_layer.get(layer.canonical_name)
        if svg_path is None:
            continue
        corner = _export_frame(svg_path)
        # export = board - origin + corner, so board = export + shift.
        shift = (origin[0] - corner[0], origin[1] - corner[1])
        out[layer.canonical_name] = _reframe(
            Path(svg_path).read_text(encoding="utf-8"), viewbox_mm, shift
        )
    if holes and shift is not None:
        marks = holes_module.merge(
            holes_module.copper_sources(layers, export.svg_by_layer),
            layer_colors=dict(colors or {}),
            palette=hole_palette(colors),
        )
        if marks.markup:
            # Every export of one call is cropped to the same box, so the shift
            # measured for a layer is the shift for the marks lifted out of it.
            out[HOLES_LAYER] = _reframe(marks.markup, viewbox_mm, shift)
    return out


# ---------------------------------------------------------------------------
# THE GRAPHICS TIER: the same fast render, without a letter in it
# ---------------------------------------------------------------------------
# MEASURED on the corpus, best of three, and it is why this exists: the fast
# render of the panel is 233 ms with the lettering and 104 ms without it, and
# the markup is 3.40 MB against 1.63 MB. Text and dimensions are more than half
# of both numbers and they are drawn on THREE layers (`F.Fab`, `B.Fab` and the
# panel's own dimension layer).
#
# The lettering in the fast path costs a carrier edit's first paint about
# 110 ms, and the interaction latency is already on the edge - so the lettering
# is neither kept in the first paint nor given up: the graphics paint AT ONCE
# and the lettering lands behind them, tens of milliseconds later, under the
# staleness discipline the content upgrade already has (`preview.session`, the
# tier passes).
#
# Dropping an item through `style_for` is what makes it CHEAP rather than merely
# absent: the library asks the policy first and only then builds the shapes
# (`eedesigncore/render/__init__.py`), so a dropped text costs no font metrics,
# no stroke geometry and no markup.
#
# There is no content stream to ask for instead. A board's streams are
# `drawings`, `tracks` and `zones` (`eedesigncore/render/traversal.py`), and text,
# fields and dimensions all ride the drawings stream - which the preview needs
# for every graphic it draws. So the tier is expressed where the library says a
# caller may express it: one policy, wrapping another.

# The item kinds whose shape is a font. Recognised by type NAME, exactly as
# :class:`PointsHidden` recognises a point: the renderer is detected at runtime
# and this must not turn a missing internal module into an import error at the
# seam.
LETTERING_ITEMS = ("PcbText", "PcbTextBox", "Property", "Dimension")


@dataclass(frozen=True)
class LetteringHidden:
    """*base*'s policy, with every text, field and dimension dropped.

    Everything else is *base*'s answer untouched, so the graphics tier is
    EXACTLY the lettered one minus the letters - which is what lets the second
    tier be a swap of three layers rather than a different picture.
    """

    base: object

    @property
    def emit_ids(self) -> bool:
        return getattr(self.base, "emit_ids", True)

    @property
    def point_stroke_width_mm(self) -> float:
        return getattr(self.base, "point_stroke_width_mm", POINT_STROKE_MM)

    def style_for(self, render_item):
        if type(render_item.item).__name__ in LETTERING_ITEMS:
            return None
        return self.base.style_for(render_item)


@dataclass(frozen=True)
class PointsHidden:
    """*base*'s policy, with every `point` item dropped.

    Everything that is not a point is *base*'s answer, untouched - so a layer
    with no colour still filters out, and a colour and an alpha a caller gave
    still reach the page. A point is answered `None`, which is the library's own
    documented way of saying "do not draw this one".

    The item is recognised by type NAME rather than by importing the class, the
    way :mod:`eedesignpanels.preview.actions` recognises a line: the renderer is
    detected at runtime and this must not turn a missing internal module into
    an import error at the seam.
    """

    base: object

    @property
    def emit_ids(self) -> bool:
        return getattr(self.base, "emit_ids", True)

    @property
    def point_stroke_width_mm(self) -> float:
        return getattr(self.base, "point_stroke_width_mm", POINT_STROKE_MM)

    def style_for(self, render_item):
        if type(render_item.item).__name__ == "PcbPoint":
            return None
        return self.base.style_for(render_item)


def merge_documents(documents) -> str:
    """Several SVG documents drawn in ONE frame, stacked into one document.

    Used for the Holes row: the library draws plated and non-plated holes as two
    documents, and the page wants one entry. Every document came from one call
    with one view box, so the merge is the first one's wrapper with every body
    inside it - no coordinate is touched.
    """
    documents = [doc for doc in documents if doc]
    if not documents:
        return ""
    opening = _SVG_OPEN.search(documents[0])
    if opening is None:
        raise ValueError("not an SVG document - no <svg> element")
    bodies = []
    for document in documents:
        start = _SVG_OPEN.search(document)
        closed = document.rfind("</svg>")
        if start is None or closed < 0:
            raise ValueError("not an SVG document - no <svg> ... </svg>")
        bodies.append(document[start.end():closed])
    return f"{opening.group(0)}{''.join(bodies)}</svg>"


def render_with_library(board, layers, viewbox_mm, colors, renderer,
                        renderer_name=None, theme=None,
                        holes: bool = False, lettering: bool = True,
                        project=None, filename=None) -> dict:
    """The library renderer, called on the contract this module states.

    `render_board_svg` takes the library's own ViewBox and
    a StylePolicy; `LayerStyle` is its general policy and carries the caller's
    colours and opacities verbatim, and :class:`PointsHidden` wraps it so a
    `point` item is not drawn at all. Any other detected name gets the generic
    keyword call. Either way the result is `{canonical name: svg string}`.

    That call also draws the board's stroke-font TEXT and its DIMENSIONS, both
    of which ride the drawings stream and need no argument here. That is what
    puts the panel's own dimensions into the FIRST paint instead of two seconds
    later, when the kicad-cli phase would be the first thing that could draw
    them. It asks :func:`fast_content` for the streams, which is drawings and
    nothing else.

    *holes* asks for the drill marks as one more document, keyed
    :data:`HOLES_LAYER`. The library draws them itself, through its two hole
    pseudo-layers, so the marks are the renderer's own and
    the two documents are stacked into one row (:func:`merge_documents`). Only
    `render_board_svg` is known to have them; any other spelling is asked for
    the board's layers alone.
    """
    # The Holes row is never a layer to plot, whichever door this was entered
    # by: `render_layers` takes it out, and a caller reaching straight for a
    # backend gets the same answer rather than an empty document named after it.
    layers, _ = split_holes(layers)
    names = [layer.canonical_name for layer in layers]
    colors = dict(colors or {})
    if renderer_name == "render_board_svg":
        from eedesigncore import render as render_module
        from eedesigncore.render import LayerStyle, ViewBox

        asked = [*names, PLATED_HOLES, NON_PLATED_HOLES] if holes else names
        drawn = LayerStyle(colors=colors)
        # THE GRAPHICS TIER, when the caller asked for it: the same picture
        # without a letter in it, painted at once, with the lettering swapped in
        # behind it (see "THE GRAPHICS TIER" above).
        style = PointsHidden(
            base=drawn if lettering else LetteringHidden(base=drawn)
        )
        # DRAWINGS ONLY, said out loud. The module docstring holds the two
        # measured reasons; the short one is that the copper stream draws no
        # pad, and a copper layer missing its pads is a picture of a board
        # nobody has.
        content = fast_content(render_module)
        asked_content = {} if content is None else {"content": content}
        from eedesigncore import TextResolutionContext

        text_context = TextResolutionContext(
            project=project, filename=filename,
            project_name=Path(filename).stem if filename else None,
        )
        produced = renderer(
            board, asked, ViewBox(*viewbox_mm), style=style,
            text_context=text_context, **asked_content
        )
    else:
        produced = renderer(
            board,
            layers=names,
            viewbox=tuple(viewbox_mm),
            colors=colors,
        )
    if not isinstance(produced, dict):
        raise TypeError(
            f"{renderer!r} returned {type(produced).__name__}, not a mapping of "
            f"layer name to SVG document"
        )
    out = {name: produced[name] for name in names if name in produced}
    if holes:
        # Plated first, so a non-plated mark is never painted over by one: the
        # holes a panel is fixtured by are the ones being looked for.
        drawn = [
            produced.get(PLATED_HOLES, ""), produced.get(NON_PLATED_HOLES, "")
        ]
        if any(has_drawing(document) for document in drawn):
            out[HOLES_LAYER] = merge_documents(drawn)
    return out


def render_layers(board, layers, viewbox_mm, colors, theme=None,
                  workdir=None, content: bool = False, origin=None,
                  board_lock=None, lettering: bool = True,
                  project=None, filename=None) -> dict:
    """Per-layer SVG for *board*, in board millimetres, in the caller's window.

    *layers* are :class:`review.render.LayerSpec` records; *viewbox_mm* is
    `(x, y, width, height)` in board millimetres; *colors* maps a canonical
    layer name to a stroke colour. Returns `{canonical name: svg document}`,
    omitting any layer the backend produced nothing for.

    A :data:`HOLES_LAYER` entry among *layers* asks for the drill marks as one
    more document; it is not a layer and is never plotted as one.

    *content* says the layer set holds pads, tracks and zones - see the module
    docstring - and is what sends the call to kicad-cli.

    *origin* is the board point a kicad-cli export's own origin stands for; the
    library backend draws in board coordinates already and ignores it.

    *board_lock* is held for the kicad-cli backend's board WRITE, which is the
    only moment either backend touches the caller's board (see
    :func:`render_with_cli`). The library backend does not write and does not
    take it.

    *lettering* off is the GRAPHICS TIER - the same picture with no text, field
    or dimension in it, which is half the render and half the markup (see "THE
    GRAPHICS TIER" above). It applies to the LIBRARY backend, which is the one
    that paints first; kicad-cli has no such control and draws everything,
    which is exactly what the content tier is for.
    """
    wanted, holes = split_holes(layers)
    if choose_backend(content) == LIBRARY_BACKEND:
        module = _import_library()
        name, renderer = (
            find_library_renderer(module) if module else (None, None)
        )
        if renderer is not None:
            return render_with_library(
                board, wanted, viewbox_mm, colors, renderer,
                renderer_name=name, theme=theme, holes=holes,
                lettering=lettering, project=project, filename=filename,
            )
    return render_with_cli(
        board, wanted, viewbox_mm, colors, theme=theme, workdir=workdir,
        holes=holes, origin=origin, board_lock=board_lock, project=project,
        filename=filename,
    )
