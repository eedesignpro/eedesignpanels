"""Turn gerber files into aligned 1-bit bitmaps.

The backend is `gerbonara` (RS-274X parser and SVG writer) followed by `resvg`
via `resvg-py` (Rust rasterizer, abi3 wheels, no cairo). Both are **dev-side
only** - this module is a verification harness, never part of what the plugin
ships to engineers.

Two properties the comparison depends on, and the reasons they hold:

* **Alignment.** Every layer of both sides is rendered with the same
  ``force_bounds`` and the same DPI, so identical copper lands on identical
  pixels. Nothing is auto-cropped anywhere in this path.
* **No anti-aliasing.** resvg is asked for ``shape_rendering="crisp_edges"``,
  which produces exactly two luminance values. Measured on the reference
  board's F.Cu at 1200 DPI: 2 distinct levels, 0 and 255. The luminance
  threshold below is therefore a belt-and-braces guard, not a load-bearing
  approximation - without it a future backend's soft edges would read as
  differences along every trace.

Renders are byte-identical run to run, so the harness does not itself break
the rule that the same input gives the same output.

**THIS MODULE REQUIRES A PATCHED gerbonara AND THAT IS A WORKAROUND.** See
:data:`WORKAROUND` below. Stock gerbonara 1.6.3 writes every SVG
coordinate with six *significant* digits, a 0.001 mm quantum at board
coordinates, which makes two renders of translated geometry disagree at one
vertex in six. Nothing in this file can compensate for that, so the module
checks the backend's version at import and says so, loudly, when the stock
package is in the environment.
"""

import io
import sys
import warnings
from pathlib import Path

import gerbonara
import resvg_py
from gerbonara import GerberFile
from gerbonara.utils import MM
from PIL import Image, ImageChops, ImageDraw

from eedesignpanels.verify.regions import (
    Bounds,
    Canvas,
    cluster_pixels,
    set_bit_pixels,
)

# A pixel is ink below this luminance. crisp_edges emits only 0 and 255, so
# the value only matters if the backend ever changes.
INK_THRESHOLD = 128

_INK_LUT = [255] * INK_THRESHOLD + [0] * (256 - INK_THRESHOLD)

# ---------------------------------------------------------------------------
# The workaround.
# ---------------------------------------------------------------------------
# gerbonara 1.6.3 formats SVG geometry with `:.6` - six SIGNIFICANT digits, a
# 0.001 mm quantum at board coordinates. The patched build writes `:.6f`, six
# DECIMAL places of a millimetre, which is one nanometre and therefore exactly
# what KiCad's gerbers hold. Seventeen lines in four files, format strings only.
#
# The patched build is a local fork, development-only, and exists to be
# deleted when the equivalent lands upstream. No released gerbonara
# carries the change: 1.6.3 is the newest release on PyPI when this
# was checked), so the `verify` extra installs stock 1.6.3 and this module
# says so at import.
PATCH_MARKER = "+finer-svg"
REQUIRED_BACKEND = f"1.6.3{PATCH_MARKER}.1"
BACKEND_VERSION = getattr(gerbonara, "__version__", "unknown")

# Said in plain terms, because it appears on a page a PCB designer reads. The
# technical version lives in this module's docstring and in the fork's own
# notes; nobody reading a panel check needs either.
WORKAROUND = (
    f"These pictures were drawn with a precision-corrected copy of the tool "
    f"that reads gerber files ({BACKEND_VERSION}) - a workaround. The standard "
    f"copy rounds coordinates to the nearest micrometre as it draws, which is "
    f"invisible in a picture but moves roughly one outline corner in six when "
    f"the same artwork is compared in two places on a panel. The corrected "
    f"copy keeps the nanometre precision the gerber files themselves hold. It "
    f"is used only for checking and is never part of anything sent to a "
    f"fabricator."
)


def backend_is_patched() -> bool:
    """Is the gerbonara in this environment the patched build?"""
    return PATCH_MARKER in BACKEND_VERSION


def backend_banner() -> str:
    """One block for the run log, saying which backend produced the pixels.

    Always printed, never only on failure: a workaround nobody is reminded of
    is a workaround that gets mistaken for the product.
    """
    if backend_is_patched():
        return (
            "  backend      gerbonara " + BACKEND_VERSION + " [PATCHED - "
            "WORKAROUND]\n"
            "               stock 1.6.3 writes SVG coordinates at 6 "
            "significant digits (0.001 mm at\n"
            "               board coordinates); the patch writes 6 decimal "
            "places (1 nm). Format\n"
            "               strings only, dev-only fork, never ships. It goes "
            "when gerbonara releases the change."
        )
    return "\n".join([
        "!" * 78,
        "!!! UNPATCHED gerbonara " + BACKEND_VERSION + " - COMPARISON RESULTS "
        "ARE NOT TRUSTWORTHY",
        "!!! Stock gerbonara writes SVG coordinates at six significant digits, "
        "a 0.001 mm",
        "!!! quantum at board coordinates. One outline vertex in six is then "
        "placed",
        "!!! differently in the two renders, and the difference that reports "
        "is the",
        "!!! renderer's, not the panel's.",
        f"!!! The patched build is {REQUIRED_BACKEND}: stock 1.6.3 with every "
        "`:.6` SVG",
        "!!! number format widened to `:.6f`. No released gerbonara has it "
        "yet.",
        "!" * 78,
    ])


if not backend_is_patched():
    # Loud on two channels, because this one must not be missed: a warning for
    # anything capturing them, and stderr for a human watching a run. It does
    # not raise - a check warns and the build continues - but nothing
    # about it is quiet.
    warnings.warn(
        f"eedesignpanels.verify needs the patched gerbonara "
        f"{REQUIRED_BACKEND}; this environment has {BACKEND_VERSION}. "
        f"Comparison results will carry the renderer's own error.",
        RuntimeWarning,
        stacklevel=2,
    )
    print(backend_banner(), file=sys.stderr)


def bounds_of(gerber: GerberFile):
    """Millimetre bounding box of a parsed gerber, or ``None`` if it is empty."""
    box = gerber.bounding_box(MM)
    if box is None:
        return None
    (min_x, min_y), (max_x, max_y) = box
    return Bounds(float(min_x), float(min_y), float(max_x), float(max_y))


def open_gerber(path: Path) -> GerberFile:
    """Parse one gerber file. Parse errors propagate to the caller."""
    return GerberFile.open(Path(path))


def blank(canvas: Canvas) -> Image.Image:
    """An all-background bitmap, used where one side has no such layer."""
    return Image.new("1", (canvas.width_px, canvas.height_px), 0)


def rasterize(gerber: GerberFile, canvas: Canvas) -> Image.Image:
    """Render one gerber onto the shared canvas as a 1-bit image, ink = 1."""
    svg = str(
        gerber.to_svg(
            force_bounds=(
                (canvas.bounds.min_x_mm, canvas.bounds.min_y_mm),
                (canvas.bounds.max_x_mm, canvas.bounds.max_y_mm),
            ),
            arg_unit=MM,
            svg_unit=MM,
            fg="black",
            bg="white",
        )
    )
    return rasterize_svg(svg, canvas)


def rasterize_svg(svg: str, canvas: Canvas) -> Image.Image:
    """Render one SVG document onto the canvas as a 1-bit image, ink = 1.

    This is the one boundary that speaks in dots per inch: gerbonara writes the
    document in millimetres and resvg needs a scale. ``resvg-py`` 0.5.0 rejects
    its own ``width`` and ``zoom`` options with "SVG has an invalid size", so
    ``dpi`` is also the only sizing control that works. The canvas converts;
    nothing above this line sees the unit.

    resvg composites onto the opaque ``background`` it is handed, so its RGBA
    output carries alpha 255 everywhere, and PIL's RGBA-to-L conversion reads
    only the colour channels. A paste-onto-white flatten before the threshold
    therefore buys nothing but a second full-canvas copy per render; the
    direct ``convert("L")`` produces a pixel-identical mask (measured:
    alpha extrema 255..255, and 0 differing pixels against the
    flattened path on a 57 Mpx panel layer).
    """
    png = resvg_py.svg_to_bytes(
        svg_string=svg,
        background="#ffffff",
        shape_rendering="crisp_edges",
        skip_system_fonts=True,
        dpi=float(canvas.dpi),
    )
    image = Image.open(io.BytesIO(png))
    ink = image.convert("L").point(_INK_LUT).convert("1", dither=Image.Dither.NONE)

    if ink.size != (canvas.width_px, canvas.height_px):
        raise RuntimeError(
            f"rasterizer returned {ink.size}, canvas expects "
            f"({canvas.width_px}, {canvas.height_px}); the two sides would "
            f"not be comparable"
        )
    return ink


def xor(a: Image.Image, b: Image.Image) -> Image.Image:
    """Pixels present in exactly one of the two 1-bit images."""
    if a.size != b.size:
        raise ValueError(f"cannot compare {a.size} against {b.size}")
    return ImageChops.logical_xor(a, b)


def count_ink(image: Image.Image) -> int:
    """How many pixels are set in a 1-bit image."""
    return image.histogram()[255]


def to_png_bytes(image: Image.Image, invert: bool = True) -> bytes:
    """Encode a 1-bit image as PNG. Ink prints black on white by default."""
    out = ImageChops.invert(image) if invert else image
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def only_in_first(a: Image.Image, b: Image.Image) -> Image.Image:
    """Pixels inked in ``a`` and not in ``b``."""
    return ImageChops.logical_and(a, ImageChops.invert(b))


def intersect(a: Image.Image, b: Image.Image) -> Image.Image:
    """Pixels inked in both. The region scoping's `and`."""
    if a.size != b.size:
        raise ValueError(f"cannot intersect {a.size} with {b.size}")
    return ImageChops.logical_and(a, b)


def combine(a: Image.Image, b: Image.Image) -> Image.Image:
    """Pixels inked in either. The region scoping's `or`."""
    if a.size != b.size:
        raise ValueError(f"cannot combine {a.size} with {b.size}")
    return ImageChops.logical_or(a, b)


def outside(image: Image.Image, mask: Image.Image) -> Image.Image:
    """The part of ``image`` that falls outside ``mask``."""
    return only_in_first(image, mask)


# Pillow's one-bit images hold 0 or 255, and a rectangle drawn with `fill=1`
# lands as a literal 1: the logical operators still read it as set, but
# `count_ink` - which counts the 255s - reads the mask as empty, and every
# measurement taken from it silently comes back zero. Measured on
# Pillow 12.3. Masks are therefore drawn at full ink, always.
MASK_INK = 255


def mask_over(canvas: Canvas, boxes) -> Image.Image:
    """A 1-bit mask, inked inside every millimetre rectangle given.

    This is how a region is turned into something the difference can be
    measured against. Rectangles are drawn on the *measurement's own* lattice
    (:meth:`Canvas.mm_box_to_pixels`), so a mask edge lands on a pixel boundary
    and nothing is a half pixel out of register with what it scopes.
    """
    mask = Image.new("1", (canvas.width_px, canvas.height_px), 0)
    draw = ImageDraw.Draw(mask)
    for box in boxes:
        pixels = canvas.mm_box_to_pixels(box)
        if pixels is not None:
            draw.rectangle(list(pixels), fill=MASK_INK)
    return mask


def coarse_features(image: Image.Image, canvas: Canvas, factor: int) -> tuple:
    """Roughly where the ink is, as a handful of boxes. **For naming only.**

    A carrier layer can hold twenty million inked pixels, and walking those in
    Python is what made an unscoped comparison take minutes. So the mask is
    max-pooled first (:func:`reduce_mask` keeps every isolated pixel) and the
    clustering runs on the small copy. What comes back is good enough to count
    features and to size them - which is all :func:`eedesignpanels.verify.
    carrier.describe` needs to name them - and it is never a measurement: every
    number the page reports comes from the full-resolution masks.
    """
    small = reduce_mask(image, factor)
    box = small.getbbox()
    if box is None:
        return ()
    left, top, right, bottom = box
    crop = small.crop(box)
    pixels = [
        (px + left, py + top)
        for px, py in set_bit_pixels(crop.tobytes(), right - left, bottom - top)
    ]
    coarse = Canvas(bounds=canvas.bounds, um_per_pixel=canvas.um_per_pixel * factor)
    return tuple(
        Bounds(
            min_x_mm=region.min_x_mm, min_y_mm=region.min_y_mm,
            max_x_mm=region.max_x_mm, max_y_mm=region.max_y_mm,
        )
        for region in (
            coarse.pixel_box_to_mm(pixel_box)
            for pixel_box in cluster_pixels(pixels, coarse.cell_size_px(1.0))
        )
    )


# The canonical out-of-phase pair. Averaging the
# two at equal weight gives neutral grey, which is what makes agreement read as
# grey rather than as a third colour.
COLOR_A = (255, 0, 0)
COLOR_B = (0, 255, 255)
BACKGROUND = (255, 255, 255)


def blend_overlay(
    a: Image.Image,
    b: Image.Image,
    color_a=COLOR_A,
    color_b=COLOR_B,
    background=BACKGROUND,
) -> Image.Image:
    """One flattened image: A in red, B in cyan, agreement in grey.

    Each side is painted on its own white sheet - ink in its own colour - and
    the two sheets are averaged. That is what produces the four readings
    below, and why the colours must be 180 degrees apart:

    ==================  =========================  ==================
    pixel               arithmetic                 reads as
    ==================  =========================  ==================
    inked in neither    (white + white) / 2        white
    inked in both       (red + cyan) / 2           neutral grey
    inked only in A     (red + white) / 2          light red
    inked only in B     (white + cyan) / 2         light cyan
    ==================  =========================  ==================

    Averaging rather than compositing one over the other is deliberate: source
    over destination would leave overlap tinted by whichever went on last, and
    "both agree" would no longer be neutral.
    """
    if a.size != b.size:
        raise ValueError(f"cannot blend {a.size} against {b.size}")
    sheet_a = Image.new("RGB", a.size, background)
    sheet_a.paste(color_a, mask=a)
    sheet_b = Image.new("RGB", b.size, background)
    sheet_b.paste(color_b, mask=b)
    return ImageChops.blend(sheet_a, sheet_b, 0.5)


def encode_png(image: Image.Image) -> bytes:
    """Encode any image as PNG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# Above this, a single set pixel averages to less than half a level in a "L"
# box reduction and would round away - the one thing a difference image may
# never do. 16 x 16 = 256 boxes, and 255/256 still rounds to 1.
MAX_REDUCTION_FACTOR = 16


def display_factor(size, max_px: int) -> int:
    """Integer reduction that brings an image under ``max_px`` on both axes.

    Clamped at :data:`MAX_REDUCTION_FACTOR`; a caller that hits the clamp gets
    an image larger than it asked for rather than one that lost differences.
    """
    if max_px < 1:
        raise ValueError(f"max_px must be at least 1, got {max_px}")
    longest = max(size)
    factor = -(-longest // max_px)  # ceiling division
    return max(1, min(factor, MAX_REDUCTION_FACTOR))


def reduce_mask(image: Image.Image, factor: int) -> Image.Image:
    """Shrink a 1-bit mask by ``factor``, keeping every isolated pixel.

    Measurement is never downscaled; *display* has to be, because a
    panel canvas is tens of megapixels and the review page carries the picture
    inline. So this is max-pooling, not resampling: an output pixel is set when
    **any** input pixel in its box was. One stray differing pixel in a 51 Mpx
    panel survives to be seen, which is the only property that matters here.
    """
    if factor < 1:
        raise ValueError(f"reduction factor must be at least 1, got {factor}")
    if factor == 1:
        return image
    if factor > MAX_REDUCTION_FACTOR:
        raise ValueError(
            f"a reduction of {factor} would round isolated pixels away; "
            f"{MAX_REDUCTION_FACTOR} is the most that keeps them"
        )
    # "L" so the box reduction averages numerically: any set pixel leaves a
    # non-zero level, which the threshold then lifts back to full ink.
    reduced = image.convert("L").reduce(factor)
    return reduced.point([0] + [255] * 255).convert("1", dither=Image.Dither.NONE)
