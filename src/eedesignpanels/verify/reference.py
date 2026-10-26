"""Compose the reference panel raster: one render per placement.

The reference the panel is judged against is not another export of the panel:
it is the *source board* rendered once for every placement the config asks
for, each render aimed so the instance lands where the grid says it should.
Nothing about the panel builder's own geometry code takes part in building it,
so a stepping mistake cannot cancel itself out - the reference comes from the
source board's own gerbers and the grid arithmetic alone.

**Why the renderer is aimed rather than the pixels moved.** The obvious
approach - render once, paste the bitmap at each placement's pixel offset - is
exact only when a placement is a whole number of pixels, and a real board's
never is. Measured on the reference board: its outline extent is
15.499999 x 10.500001 mm, so with a 3 mm gap the step is 18.499999 x
13.500001 mm, which at 5 um/px is 3699.9998 x 2700.0002 px. Those offsets share
a 1 nm divisor, so no usable resolution makes them integral, and pasting left
672 differing pixels on `Edge.Cuts` that belonged to the reference rather than
to the panel.

Aiming the renderer removes the problem instead of tolerating it. Each instance
is rasterized at its true sub-pixel position - exactly what the panel's own
render does to the same geometry - so the reference is exact for any grid, at
any resolution, with no tolerance to set and nothing to round. The cost is one
render per placement per replicated layer instead of one.

Two coordinate facts this module is built on, both verified against a real
export:

* **Gerber Y is the board's Y negated.** KiCad plots with no origin offset, so
  a gerber point is ``(x, -y)`` of the board point. The reference board's
  ``Edge.Cuts`` runs y 123.271..133.771 mm on the board and -133.777..-123.264
  in the gerber.
* **Moving content is aiming the window the other way.** A render is bounded by
  a millimetre window; shifting that window by ``-offset`` puts the content at
  ``+offset`` on the pixels. The window keeps its width and height to the bit,
  so every placement's render is the same pixel size as the panel's.
"""

from typing import NamedTuple

from PIL import Image, ImageChops

from eedesignpanels.verify.regions import Bounds, Canvas

# gerbonara writes its viewport transform with `:.6` - six *significant*
# digits. For a board sitting at y = -134 mm that is a 0.001 mm quantum, so a
# window coordinate survives the round trip only if it is already a whole
# number of micrometres. Everything below works in whole nanometres to keep it
# that way.
NM_PER_MM = 1_000_000
NM_PER_UM = 1000
# Above this, six significant digits no longer resolve one micrometre.
SIX_DIGIT_LIMIT_MM = 1000.0


def _nm(value_mm: float) -> int:
    """Millimetres to whole nanometres - the resolution KiCad's gerbers use."""
    return round(value_mm * NM_PER_MM)


def pixel_nm(um_per_pixel: float) -> int:
    """One pixel in whole nanometres."""
    step = round(um_per_pixel * NM_PER_UM)
    if step <= 0:
        raise ValueError(f"um_per_pixel must be positive, got {um_per_pixel}")
    return step


def transform_strings(bounds: Bounds) -> tuple:
    """The four numbers gerbonara will print for this window, as it prints them.

    Reproduced here rather than assumed: this is
    ``gerbonara.cam.CamFile.to_svg``'s viewport transform. **It must track the
    backend's own format spec** - the patched build writes ``:.6f``, six
    decimal places of a millimetre, and a diagnostic still modelling stock
    gerbonara's ``:.6`` would report a rounding that no longer happens.
    """
    min_x, min_y, height = bounds.min_x_mm, bounds.min_y_mm, bounds.height_mm
    return (
        f"{min_x:.6f}", f"{min_y + height:.6f}",
        f"{-min_x:.6f}", f"{-min_y:.6f}",
    )


def transform_error_mm(bounds: Bounds) -> float:
    """How far that transform moves content in y. Zero when it is exact.

    The net y mapping is ``round6(min_y + h) + round6(min_y) - y`` where the
    exact one is ``(min_y + h) + min_y - y``, so the error is the sum of two
    independent roundings - which is why neither can cancel the other.
    """
    strings = transform_strings(bounds)
    min_y, height = bounds.min_y_mm, bounds.height_mm
    top = float(strings[1]) - (min_y + height)
    bottom = -float(strings[3]) - min_y
    return top + bottom


def snap_bounds(bounds: Bounds, um_per_pixel: float) -> Bounds:
    """Grow a window outward onto the pixel lattice, in whole nanometres.

    Padding a comparison window is free - it adds blank canvas and never
    content - so the window is grown outward rather than nudged to the nearest
    lattice point. Landing on the lattice is what lets gerbonara's six-digit
    transform reproduce the window exactly, provided a pixel is a whole number
    of micrometres and the board sits inside +-1000 mm.
    """
    step = pixel_nm(um_per_pixel)
    min_x = (_nm(bounds.min_x_mm) // step) * step
    min_y = (_nm(bounds.min_y_mm) // step) * step
    max_x = -((-_nm(bounds.max_x_mm)) // step) * step
    max_y = -((-_nm(bounds.max_y_mm)) // step) * step
    return Bounds(
        min_x_mm=min_x / NM_PER_MM,
        min_y_mm=min_y / NM_PER_MM,
        max_x_mm=max_x / NM_PER_MM,
        max_y_mm=max_y / NM_PER_MM,
    )


def snap_canvas(canvas: Canvas) -> Canvas:
    """The same canvas, grown outward onto its own pixel lattice."""
    return Canvas(
        bounds=snap_bounds(canvas.bounds, canvas.um_per_pixel),
        um_per_pixel=canvas.um_per_pixel,
    )


def lattice_is_exact(um_per_pixel: float, bounds: Bounds) -> bool:
    """Can a snapped window be printed exactly at six significant digits?

    Two conditions, both cheap to state and both real: a pixel has to be a
    whole number of micrometres, and the board has to sit where six digits
    still resolve one - inside 1000 mm of the plot origin.
    """
    return pixel_nm(um_per_pixel) % NM_PER_UM == 0 and max(
        abs(bounds.min_x_mm), abs(bounds.min_y_mm),
        abs(bounds.max_x_mm), abs(bounds.max_y_mm),
    ) < SIX_DIGIT_LIMIT_MM


class SnappedOffset(NamedTuple):
    """A placement expressed in whole pixels, and what that cost.

    Whole pixels put an instance's window on the same lattice as the panel's,
    which is what keeps both transforms exact. The price is that a placement
    not already on the lattice is moved onto it, and :attr:`residual_mm` is
    exactly how far - reported, never absorbed.
    """

    dx_px: int
    dy_px: int
    dx_mm: float
    dy_mm: float
    residual_mm: float

    @property
    def offset_mm(self) -> tuple:
        return (self.dx_mm, self.dy_mm)


def snap_offset(offset: tuple, um_per_pixel: float) -> SnappedOffset:
    """Put a gerber-space placement on the pixel lattice, keeping the change."""
    step = pixel_nm(um_per_pixel)
    dx_nm, dy_nm = _nm(offset[0]), _nm(offset[1])
    dx_px, dy_px = round(dx_nm / step), round(dy_nm / step)
    snapped_x, snapped_y = dx_px * step, dy_px * step
    return SnappedOffset(
        dx_px=dx_px,
        dy_px=dy_px,
        dx_mm=snapped_x / NM_PER_MM,
        dy_mm=snapped_y / NM_PER_MM,
        residual_mm=max(abs(snapped_x - dx_nm), abs(snapped_y - dy_nm))
        / NM_PER_MM,
    )


def gerber_offset_mm(placement) -> tuple:
    """A board-space placement as a gerber-space translation: Y is negated."""
    return (placement.dx_mm, -placement.dy_mm)


def placement_bounds(bounds: Bounds, offset: tuple) -> Bounds:
    """The render window that puts content at ``offset`` in gerber space.

    The window moves opposite to the content, and every corner is computed in
    whole nanometres from the window's own corners, so a window that started on
    the micrometre lattice stays on it and gerbonara's six-digit transform
    reproduces it unchanged. The size is carried across by construction, so a
    placement can never change the canvas by a pixel and leave the two sides
    out of register.
    """
    dx, dy = offset
    min_x = _nm(bounds.min_x_mm) - _nm(dx)
    min_y = _nm(bounds.min_y_mm) - _nm(dy)
    return Bounds(
        min_x_mm=min_x / NM_PER_MM,
        min_y_mm=min_y / NM_PER_MM,
        max_x_mm=(min_x + _nm(bounds.width_mm)) / NM_PER_MM,
        max_y_mm=(min_y + _nm(bounds.height_mm)) / NM_PER_MM,
    )


def placement_canvas(canvas: Canvas, offset: tuple) -> Canvas:
    """The same canvas, aimed so an instance lands at ``offset``."""
    return Canvas(
        bounds=placement_bounds(canvas.bounds, offset),
        um_per_pixel=canvas.um_per_pixel,
    )


def expand_bounds(bounds: Bounds, margin_mm: float) -> Bounds:
    """Grow a window by a margin on every side, in whole nanometres.

    Raises:
        ValueError: for a negative margin, which would *shrink* the window and
            drop content out of the comparison without saying so.
    """
    if margin_mm < 0:
        raise ValueError(f"margin must not be negative, got {margin_mm}")
    margin = _nm(margin_mm)
    return Bounds(
        min_x_mm=(_nm(bounds.min_x_mm) - margin) / NM_PER_MM,
        min_y_mm=(_nm(bounds.min_y_mm) - margin) / NM_PER_MM,
        max_x_mm=(_nm(bounds.max_x_mm) + margin) / NM_PER_MM,
        max_y_mm=(_nm(bounds.max_y_mm) + margin) / NM_PER_MM,
    )


def covered_bounds(bounds: Bounds, offsets) -> Bounds:
    """The gerber-space extent one layer covers once placed everywhere.

    Raises:
        ValueError: when handed no offsets, which would mean a panel with no
            instances - a caller error, not an empty answer.
    """
    offsets = list(offsets)
    if not offsets:
        raise ValueError("cannot place a layer at no placements")
    return Bounds(
        min_x_mm=bounds.min_x_mm + min(dx for dx, _ in offsets),
        min_y_mm=bounds.min_y_mm + min(dy for _, dy in offsets),
        max_x_mm=bounds.max_x_mm + max(dx for dx, _ in offsets),
        max_y_mm=bounds.max_y_mm + max(dy for _, dy in offsets),
    )


def union(images) -> Image.Image:
    """Every instance's render, ORed into one reference mask.

    Overlapping ink stays ink and nothing erases what an earlier instance put
    down - the panel is a union of boards, not a stack of them.

    Raises:
        ValueError: for an empty sequence, or for anything but 1-bit images of
            one size. Two sizes here would mean two render windows disagreed,
            which is exactly the misregistration this module exists to avoid.
    """
    combined = None
    for image in images:
        if image.mode != "1":
            raise ValueError(
                f"the reference is composed from 1-bit masks, got {image.mode}"
            )
        if combined is None:
            combined = image
        elif combined.size != image.size:
            raise ValueError(
                f"placement renders disagree on size: {combined.size} and "
                f"{image.size}; the two sides would not be comparable"
            )
        else:
            combined = ImageChops.logical_or(combined, image)
    if combined is None:
        raise ValueError("cannot compose a reference from no placements")
    return combined
