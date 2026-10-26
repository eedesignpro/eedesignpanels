"""The comparison's picture, as vectors rather than pixels.

The check page used to embed a rasterised blend per layer. It now embeds the
two sides as SVG and lets the browser blend them, which means the picture is
sharp at any magnification instead of turning into squares - the user zooms
into a difference to judge it, and a blurred difference is not evidence.

Measured before committing. On this board's
panel the worst layer, `F.Cu`, is 5,033 elements and holds a locked 60 fps
(median 16.7 ms per frame, 119 of 119 frames at vsync), so browser performance
is not a constraint at board scale. Bytes are a different matter and vary by
an order of magnitude between layers, which is what :func:`cost` and the
caller's cap are for.

**The blend is an average, exactly as the raster one was.** A white sheet, the
expected artwork on it, and the built artwork over that at half opacity with
its own white behind it:

===================  =========================  ==================
pixel                arithmetic                 reads as
===================  =========================  ==================
in neither           (white + white) / 2        white
in both              (red + cyan) / 2           neutral grey
expected only        (red + white) / 2          light red
built only           (white + cyan) / 2         light cyan
===================  =========================  ==================

**The expected side is placed by reference, not repeated.** Six `<use>`
elements point at one copy of the artwork. That is a sixth of the bytes, and
it is also the more truthful statement: the reference *is* the same board at
six recorded positions. The rule against `<use>` clones governs the
board plot review page, where the artifact must embed the plotter's own output
unaltered so a view assembled by our code cannot look right while the output
is wrong. This page is the instrument's own composition of its own measured
inputs, and it says so - the same reasoning that let it downscale rasters.
"""

import html
import re
from typing import NamedTuple

from gerbonara.utils import MM

from eedesignpanels.verify.regions import Canvas, set_bit_pixels

# Matched against the SVG the backend writes, so the two cannot drift.
_ELEMENT = re.compile(r"<(path|circle|rect|polygon|polyline)\b")
# The whole drawing group INCLUDING its transform. That transform is the flip
# from gerber coordinates, where y runs up, into document coordinates, where y
# runs down. Capturing only what was inside it dropped the flip, put the
# artwork at the wrong end of the window, and sent five of the six placements
# off the canvas - which read on screen as "the expected side does not paint".
_BODY = re.compile(r"(<g transform=[^>]*>.*</g>)\s*</svg>", re.S)
_VIEWBOX = re.compile(r'viewBox="([^"]+)"')
# The backend's own opening <svg> tag, kept verbatim for a standalone
# document: it carries the width/height in millimetres and the viewBox the
# rasterizer sizes the canvas from.
_DOCUMENT_TAG = re.compile(r"<svg\b[^>]*>")

# The out-of-phase pair the raster blend uses, so a layer that falls back to
# raster reads identically to one that did not.
EXPECTED_COLOUR = "#ff0000"
BUILT_COLOUR = "#00ffff"
# Measured differences are pooled into cells this many pixels square before
# being drawn. At 5 um per pixel that is a 20 um marker - small enough to sit
# on the feature that moved, large enough not to emit one element per pixel.
DEFAULT_POOL_PX = 4

# Marker strokes. Darkened members of the two signal colours, so a thin stroke
# reads on white while still saying the same thing the blend says. Ink would
# not: near-black markers bury the drawing they exist to point at.
MARKER_EXPECTED = "#cc0000"
MARKER_BUILT = "#0097a7"
# Screen pixels, once the effect is applied per element. Millimetres if it is
# not, which is the whole of the defect this constant is documented against.
MARKER_STROKE_PX = 1.5


class VectorLayer(NamedTuple):
    """One layer's picture, ready to embed, with what it costs to embed it."""

    markup: str
    view_box: str
    bytes: int
    elements: int

    @property
    def kilobytes(self) -> float:
        return self.bytes / 1024


def _svg_of(gerber, canvas: Canvas, colour: str) -> str:
    """One side as SVG over the comparison window - the raster's own call."""
    bounds = canvas.bounds
    return str(
        gerber.to_svg(
            force_bounds=((bounds.min_x_mm, bounds.min_y_mm),
                          (bounds.max_x_mm, bounds.max_y_mm)),
            arg_unit=MM, svg_unit=MM, fg=colour, bg="white",
        )
    )


def _inner(svg: str) -> str:
    """The drawing, still carrying its own coordinate transform.

    What is dropped is the *document* the backend wrapped it in - the `<svg>`
    element - and nothing else. The transform inside stays: it is the flip
    from gerber coordinates into document ones, and both sheets and every
    placement offset are expressed in the space it establishes.
    """
    body = _BODY.search(svg)
    return body.group(1) if body else ""


def _open_tag(svg: str, class_name: str) -> str:
    """The backend's own `<svg>` opening tag, restyled for stacking.

    The document's own `width`, `height` and background are dropped: the
    element is positioned by the page, and the background has to come from the
    page too or the top sheet would paint over the one beneath it.
    """
    view_box = _VIEWBOX.search(svg)
    box = view_box.group(1) if view_box else "0 0 1 1"
    return (
        f'<svg class="{class_name}" viewBox="{box}" data-base="{box}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg">'
    )


def reference_id(key: str) -> str:
    """The document-wide unique id for one layer's reference artwork.

    **Every layer's SVG is inlined into one HTML document, so ids are shared
    across all of them.** A fixed id meant all nineteen layers declared the
    same one; every `<use>` on the page then resolved to the first, and each
    layer showed one correct instance of its own artwork plus six ghosts of
    another layer's - offset and mirrored, because that layer's own flip came
    along with it. Seen on a delivered page. The layer key is what
    makes it unique.
    """
    safe = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in key
    )
    return f"expected-{safe}"


def placed_reference(gerber, canvas: Canvas, offsets, key: str,
                     colour=EXPECTED_COLOUR) -> str:
    """The expected side: the source's artwork at every recorded placement.

    The artwork is declared once inside `<defs>`, where it does not draw, and
    placed by one `<use>` per placement - so the number of instances on screen
    is exactly the number of placements. Declared outside `<defs>` it also drew
    itself in place, which is one instance too many.

    A `<use>` wraps its target in a translate applied *outside* the target's
    own transform, so the offsets are in the document's own space, where the
    gerber's Y flip has already happened and a placement that moves an
    instance down the panel moves it down the page.
    """
    svg = _svg_of(gerber, canvas, colour)
    element_id = reference_id(key)
    uses = "".join(
        f'<use href="#{element_id}" x="{dx:.6f}" y="{-dy:.6f}"/>'
        for dx, dy in offsets
    )
    return (
        _open_tag(svg, "sheet expected")
        + f'<defs><g id="{element_id}">{_inner(svg)}</g></defs>{uses}</svg>'
    )


def placed_reference_document(gerber, canvas: Canvas, offsets,
                              colour: str = "black") -> str:
    """The source's artwork at every placement, as ONE standalone document.

    The rasterizer's own input for the composed reference: the artwork is
    declared once in ``<defs>`` and placed by one
    ``<use>`` per placement, so the whole reference costs a single render of
    the panel canvas instead of one full-canvas render per placement. The
    backend's own ``<svg>`` opening tag is kept verbatim - it carries the
    millimetre size and viewBox the rasterizer scales by - and the offsets are
    written at six decimal places of a millimetre, which is exact for the
    nanometre-derived values placements are made of.

    Each ``<use>`` translate applies *outside* the artwork's own transform, so
    the offsets live in document space, where the gerber Y flip has already
    happened - hence ``-dy``, exactly as :func:`placed_reference` writes them.

    Measured against the ORed per-placement renders it replaces, on this
    corpus at 5 um/px: 264 of 57 million pixels differ on the densest layer,
    single-pixel edge effects in the rasterizer's transform pipeline, a
    ~2.5 um-class reading well under both the 10 um verdict threshold and the
    arc-sliver floor the displacement metric already absorbs.
    """
    svg = _svg_of(gerber, canvas, colour)
    opening = _DOCUMENT_TAG.search(svg)
    if opening is None:
        raise ValueError("the backend's SVG has no <svg> opening tag")
    uses = "".join(
        f'<use href="#placed" x="{dx:.6f}" y="{-dy:.6f}"/>'
        for dx, dy in offsets
    )
    return (
        opening.group(0)
        + f'<defs><g id="placed">{_inner(svg)}</g></defs>{uses}</svg>'
    )


def empty_sheet(canvas: Canvas, class_name: str) -> str:
    """A sheet with nothing on it, over the same window.

    A layer one side does not have still needs its side of the blend, or the
    stack would show the other side alone and call it agreement.
    """
    bounds = canvas.bounds
    box = (f"{bounds.min_x_mm:.6f} {bounds.min_y_mm:.6f} "
           f"{bounds.width_mm:.6f} {bounds.height_mm:.6f}")
    return (
        f'<svg class="sheet {class_name}" viewBox="{box}" data-base="{box}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg"></svg>'
    )


def placed_reference_empty(canvas: Canvas) -> str:
    """The expected side of a layer this stage does not replicate."""
    return empty_sheet(canvas, "expected")


def built_panel(gerber, canvas: Canvas, colour=BUILT_COLOUR) -> str:
    """The built side, over the same window so the two stack in register."""
    svg = _svg_of(gerber, canvas, colour)
    return _open_tag(svg, "sheet built") + _inner(svg) + "</svg>"


def _cells(image, canvas: Canvas, pool_px: int) -> str:
    """Pooled rectangles for one side's differing pixels, in millimetres."""
    box = image.getbbox()
    if box is None:
        return ""
    left, top, right, bottom = box
    crop = image.crop(box)
    cells = set()
    for px, py in set_bit_pixels(crop.tobytes(), right - left, bottom - top):
        cells.add(((px + left) // pool_px, (py + top) // pool_px))

    step = canvas.mm_per_pixel * pool_px
    bounds = canvas.bounds
    return "".join(
        f'<rect x="{bounds.min_x_mm + cx * step:.4f}" '
        # Image rows run down and gerber Y runs up; the document has already
        # been flipped, so this is its own y.
        f'y="{bounds.min_y_mm + cy * step:.4f}" '
        f'width="{step:.4f}" height="{step:.4f}"/>'
        for cx, cy in sorted(cells)
    )


def delta_markers(only_expected, only_built, canvas: Canvas,
                  pool_px: int = DEFAULT_POOL_PX) -> str:
    """The measured differences, drawn at their true millimetre positions.

    These are the comparison's own output, not a redrawing of the artwork:
    every marker sits where a pixel actually differed. They are pooled into
    cells so the count stays sane, and split by which side protruded, so the
    markers say what the blend beneath them says - red where the expected
    panel carries artwork the built one does not, cyan the other way round.

    **The stroke has to be sized in screen pixels, not millimetres**, which
    takes ``vector-effect: non-scaling-stroke``. That property **does not
    inherit**: set on the group it applies to nothing, every rectangle
    computes ``vector-effect: none``, and a width of 1.5 is then 1.5 *user
    units* - 1.5 mm - so at a fit view every marker paints as a fat blob that
    buries the drawing it exists to point at. Measured on a delivered page.
    It is applied to the rectangles themselves, from :data:`CSS`.

    Returns an empty string when nothing differed, so a clean layer carries no
    overlay at all.
    """
    groups = ""
    for side, image in (("expected", only_expected), ("built", only_built)):
        rects = _cells(image, canvas, pool_px)
        if rects:
            groups += f'<g class="delta delta-{side}">{rects}</g>'
    return groups


def blend(expected: str, built: str, markers: str, caption: str = "") -> VectorLayer:
    """Stack the two sheets so the browser averages them.

    The base sheet is white and carries the expected artwork; the built sheet
    sits over it at half opacity with white of its own behind, which is what
    makes agreement average to neutral grey rather than tint towards whichever
    went on last.
    """
    view_box = _VIEWBOX.search(expected)
    box = view_box.group(1) if view_box else "0 0 1 1"
    overlay = (
        f'<svg class="sheet delta-sheet" viewBox="{box}" data-base="{box}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg">{markers}</svg>'
        if markers else ""
    )
    # The two artwork sheets share a wrapper so the whole blend can be faded
    # as one thing. Fading them separately would break the average that makes
    # agreement read as neutral grey.
    markup = (
        '<div class="vblend">'
        '<div class="vartwork">'
        f'<div class="vsheet base">{expected}</div>'
        f'<div class="vsheet over">{built}</div>'
        "</div>"
        f'<div class="vsheet marks">{overlay}</div>'
        '<div class="deltanote">measured differences, over a faint copy of '
        "both panels for position</div>"
        + (f'<div class="vcaption">{html.escape(caption)}</div>'
           if caption else "")
        + "</div>"
    )
    return VectorLayer(
        markup=markup,
        view_box=box,
        bytes=len(markup.encode("utf-8")),
        elements=len(_ELEMENT.findall(markup)),
    )


# THE MARKERS' STROKE IS SIZED IN SCREEN PIXELS, and this is where that is
# arranged. `vector-effect` does not inherit, so the selector has to reach the
# rectangles THEMSELVES: on the group it applies to nothing, the width falls
# back to user units - millimetres here - and every marker paints as a fat
# blob that buries the drawing it exists to point at. Measured on a delivered
# page. Keep this selector on the primitives.
MARKER_CSS = (
    f".diffroot .delta rect {{ vector-effect: non-scaling-stroke;"
    f" stroke-width: {MARKER_STROKE_PX}; fill: none; }}\n"
    f".diffroot .delta-expected rect {{ stroke: {MARKER_EXPECTED}; }}\n"
    f".diffroot .delta-built rect {{ stroke: {MARKER_BUILT}; }}\n"
)

CSS = MARKER_CSS + """
/* White here, so anything faded over it composites onto white rather than
   onto the page's own grey. */
.diffroot .vblend { position: absolute; inset: 0; background: #fff; }
.diffroot .vartwork { position: absolute; inset: 0; }
.diffroot .vsheet { position: absolute; inset: 0; }
/* The average that makes agreement read as neutral grey: an opaque white
   sheet carrying the expected artwork, and the built artwork over it at half
   opacity with white of its own behind. */
.diffroot .vsheet.base { background: #fff; }
.diffroot .vsheet.over { background: #fff; opacity: .5; }
.diffroot .vsheet.marks { background: none; }
.diffroot .vsheet svg { position: absolute; inset: 0; width: 100%;
                        height: 100%; display: block; pointer-events: none; }

/* Either side on its own, and the measured differences on their own. The
   evidence behind the blend, which used to be three more pictures to embed
   and is now three ways of looking at the ones already here. */
.diffroot .vblend.only-expected .vsheet.over { display: none; }
.diffroot .vblend.only-built .vsheet.base svg { display: none; }
.diffroot .vblend.only-built .vsheet.over { opacity: 1; }
/* Differences mode keeps the blend, faintly. Markers alone on white are the
   instrument's evidence and nothing else - true, and useless, because a
   difference with no artwork around it has no position you can judge. The
   markers stay full strength and dominate; the artwork is there to say where
   they are. */
.diffroot .vblend.only-delta .vartwork { opacity: .18; }
.diffroot .vblend .deltanote { display: none; }
.diffroot .vblend.only-delta .deltanote { display: block; position: absolute;
    left: .4rem; bottom: .4rem; font-size: .7rem; color: #475569;
    background: #ffffffcc; border-radius: 3px; padding: .1rem .35rem; }
.diffroot .vshow { display: inline-flex; gap: .25rem; margin-left: .5rem; }
.diffroot .vshow button { font: inherit; font-size: .72rem; cursor: pointer;
                          border: 1px solid #94a3b8; background: #f1f5f9;
                          border-radius: 3px; padding: 0 .4rem; }
.diffroot .vshow button:hover { background: #e2e8f0; }
.diffroot .vshow button.on { background: #0f172a; color: #fff;
                             border-color: #0f172a; }
"""

# The four ways of looking at one layer: the blend, either side alone, and the
# measured differences alone. Each is a class on the stack, so nothing extra is
# embedded to provide them.
VIEWS = (
    ("", "blend"),
    ("only-expected", "expected"),
    ("only-built", "built"),
    ("only-delta", "differences"),
)


def view_controls() -> str:
    """The buttons that switch between those four views."""
    buttons = "".join(
        f'<button type="button" data-view="{name}"'
        f'{" class=on" if not name else ""}>{label}</button>'
        for name, label in VIEWS
    )
    return f'<span class="vshow">show: {buttons}</span>'


JS = """
Array.prototype.forEach.call(
  document.querySelectorAll('.diffroot .vshow'), function (bar) {
    var stack = bar.closest('section.layer').querySelector('.vblend');
    Array.prototype.forEach.call(bar.querySelectorAll('button'),
      function (btn) {
        btn.addEventListener('click', function () {
          if (!stack) { return; }
          stack.classList.remove('only-expected', 'only-built', 'only-delta');
          if (btn.dataset.view) { stack.classList.add(btn.dataset.view); }
          Array.prototype.forEach.call(bar.querySelectorAll('button'),
            function (other) { other.classList.remove('on'); });
          btn.classList.add('on');
        });
      });
  });
"""
