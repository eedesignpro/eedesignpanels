"""Snapshot SVG to PNG, sized for a reader that pays per pixel.

The vector file is the exact artifact; this is the one a vision model, a chat
message or a mail client can read without an SVG renderer. On a 4K monitor a
screen-sized raster would be both huge and pointless:
the long edge is capped at :data:`LONG_EDGE_PX` (about 1600), which is enough
to judge a panel and cheap enough to hand to a model.

**Optional by design.** `resvg_py` and `Pillow` are dev-side extras
(`pip install -e .[verify]`) and the preview is dev tooling, so they are
imported here and here only. When they are absent the snapshot still saves its
SVG and its state file, the PNG is skipped, and the reason is recorded in the
shot and in the journal - a missing dev extra must never lose the user's
snapshot.

resvg-py 0.5.0 rejects its own `width` and `zoom` options ("SVG has an invalid
size"), so **dpi is the only sizing control that works** - the same finding
:mod:`eedesignpanels.verify.raster` is built on. The snapshot document carries
its size in millimetres, so the dpi that lands the long edge on the cap is
arithmetic; Pillow then trims the rounding.
"""

import io
import re
from pathlib import Path
from typing import NamedTuple

try:  # dev extra, not a runtime dependency - see the module docstring
    import resvg_py
    from PIL import Image

    AVAILABLE = True
    UNAVAILABLE_REASON = ""
except ImportError as exc:  # pragma: no cover - exercised by the fallback test
    resvg_py = None
    Image = None
    AVAILABLE = False
    UNAVAILABLE_REASON = (
        f"no rasterizer: {exc}. Install the dev extra "
        f"(pip install -e '.[verify]') for snapshot PNGs; the SVG and the "
        f"state file are saved either way."
    )

# About 1600 px on the long edge: ingestible by a vision model at modest token
# cost, and still enough to see a 5 mm tab on a 300 mm panel.
LONG_EDGE_PX = 1600
# The review pages' own background, so a shot reads alone rather than as a
# transparent PNG on whatever is behind it.
BACKGROUND = "#14161a"
MM_PER_INCH = 25.4

_LENGTH = {
    name: re.compile(rf'\b{name}="([0-9.eE+-]+)\s*(mm|px|in|cm)?"')
    for name in ("width", "height")
}
_VIEWBOX = re.compile(
    r'viewBox="\s*([-\d.eE+]+)[,\s]+([-\d.eE+]+)[,\s]+'
    r'([-\d.eE+]+)[,\s]+([-\d.eE+]+)'
)
_UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": MM_PER_INCH, "px": MM_PER_INCH / 96.0}


class RasterUnavailable(RuntimeError):
    """No rasterizer in this interpreter. The vector artifact is unaffected."""


class Raster(NamedTuple):
    """One PNG written: where it went and how big it turned out."""

    path: Path
    width_px: int
    height_px: int
    long_edge_px: int
    dpi: float
    backend: str = "resvg_py"


def _length(markup: str, name: str) -> float:
    """One `width=`/`height=` attribute in millimetres, or 0.0 when absent."""
    found = _LENGTH[name].search(markup)
    if not found:
        return 0.0
    return float(found.group(1)) * _UNIT_MM.get(found.group(2) or "px", 1.0)


def document_size_mm(markup: str) -> tuple:
    """The document's `(width, height)` in millimetres.

    `width`/`height` when the document declares them - the snapshot writer
    always does - and the viewBox extent otherwise, which is the same number
    for a document whose user unit is the millimetre. Anything else raises:
    guessing a size means guessing a resolution.
    """
    width, height = _length(markup, "width"), _length(markup, "height")
    if width > 0.0 and height > 0.0:
        return (width, height)
    box = _VIEWBOX.search(markup)
    if box:
        width, height = float(box.group(3)), float(box.group(4))
        if width > 0.0 and height > 0.0:
            return (width, height)
    raise ValueError(
        "this SVG declares no usable size - a raster needs one, and a "
        "guessed size is a guessed resolution"
    )


def dpi_for(long_edge_mm: float, long_edge_px: int = LONG_EDGE_PX) -> float:
    """The dpi that puts *long_edge_mm* on *long_edge_px* pixels."""
    if long_edge_mm <= 0.0:
        raise ValueError("a document with no extent has no dpi")
    return long_edge_px * MM_PER_INCH / long_edge_mm


def rasterize(markup: str, out_path: Path, long_edge_px: int = LONG_EDGE_PX,
              background: str = BACKGROUND) -> Raster:
    """Write *markup* as a PNG, longest edge at most *long_edge_px*.

    Raises :class:`RasterUnavailable` when the dev extras are not installed -
    the caller saves the vector artifact and reports the skip.
    """
    if not AVAILABLE:
        raise RasterUnavailable(UNAVAILABLE_REASON)
    if long_edge_px < 1:
        raise ValueError("a raster needs at least one pixel on its long edge")

    width_mm, height_mm = document_size_mm(markup)
    dpi = dpi_for(max(width_mm, height_mm), long_edge_px)
    png = resvg_py.svg_to_bytes(
        svg_string=markup,
        background=background,
        skip_system_fonts=True,
        dpi=float(dpi),
    )
    image = Image.open(io.BytesIO(png))
    # resvg composites onto the opaque background it was handed, so RGB is the
    # whole picture; a PNG with no alpha is also what a chat client shows
    # correctly.
    image = image.convert("RGB")
    # The dpi is exact in millimetres and resvg rounds to whole pixels, so an
    # edge can land one pixel over the cap. Trim rather than declare the cap
    # approximate.
    if max(image.size) > long_edge_px:
        scale = long_edge_px / max(image.size)
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG", optimize=True)
    return Raster(
        path=out_path,
        width_px=image.width,
        height_px=image.height,
        long_edge_px=long_edge_px,
        dpi=round(dpi, 3),
    )
