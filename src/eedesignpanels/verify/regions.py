"""Canvas arithmetic and difference-region clustering - pure, no I/O.

Two jobs, both plain arithmetic so they are testable by inspection:

* :class:`Canvas` is the shared pixel grid. Alignment is the whole game in a
  raster comparison - if the two sides do not land on the same grid, identical
  copper reads as a wall of differences. One canvas is derived from the union
  of both gerber sets and handed to every render, so pixel *(px, py)* means the
  same millimetre position in A and in B.
* :func:`cluster_pixels` turns a scatter of differing pixels into a handful of
  bounding boxes a user can act on. A translated track produces two thin
  slivers a fraction of a millimetre apart; they are one fault, so pixels are
  binned into cells and touching cells are merged.

Coordinates are millimetres in the **gerber file's** coordinate system, which
is what the compared artifacts are written in. Gerber Y runs up, image rows run
down, and :meth:`Canvas.pixel_box_to_mm` is the one place that flip happens.
"""

from typing import NamedTuple

MM_PER_INCH = 25.4


class Bounds(NamedTuple):
    """An axis-aligned millimetre rectangle, gerber orientation (Y up)."""

    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float

    @property
    def width_mm(self) -> float:
        return self.max_x_mm - self.min_x_mm

    @property
    def height_mm(self) -> float:
        return self.max_y_mm - self.min_y_mm


def union_bounds(boxes) -> Bounds:
    """Smallest rectangle containing every box. Raises if given none."""
    boxes = list(boxes)
    if not boxes:
        raise ValueError("cannot take the union of no bounding boxes")
    return Bounds(
        min_x_mm=min(box.min_x_mm for box in boxes),
        min_y_mm=min(box.min_y_mm for box in boxes),
        max_x_mm=max(box.max_x_mm for box in boxes),
        max_y_mm=max(box.max_y_mm for box in boxes),
    )


class PixelBox(NamedTuple):
    """Inclusive pixel bounds of one difference region, plus its pixel count."""

    min_px: int
    min_py: int
    max_px: int
    max_py: int
    pixels: int


class Region(NamedTuple):
    """One difference region, reported to the caller in millimetres.

    ``pixels`` counts the differing pixels inside the box; ``area_mm2`` is that
    count in real area. The bounding box is usually much larger than the area,
    because a translated trace differs along a thin outline inside a wide box -
    which is why both numbers are reported rather than one derived from the
    other.

    **The number that decides a verdict is** :attr:`displacement_mm`. Area
    and pixel counts answer "how much differs", which scales
    with how long an edge is and says nothing about whether anything is wrong.
    Displacement answers "how far did it move", which is the question an
    engineer is actually asking: a hair-thin sliver a thousand times round an
    outline is rendering noise, and a 0.2 mm shift of one track is a defect,
    even though the first can carry more total area than the second.
    """

    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float
    pixels: int
    area_mm2: float
    perimeter_mm: float = 0.0
    widest_mm: float = 0.0

    @property
    def width_mm(self) -> float:
        return self.max_x_mm - self.min_x_mm

    @property
    def height_mm(self) -> float:
        return self.max_y_mm - self.min_y_mm

    @property
    def bbox_area_mm2(self) -> float:
        return self.width_mm * self.height_mm

    @property
    def displacement_mm(self) -> float:
        """How far the geometry moved here: the mean width of the band.

        A difference between two renders of the same shape is a band along an
        edge, of area *A* and length *L*, and what moved is its width
        *A / L*. The pixel set's perimeter *P* is about *2L* for such a band,
        so the width is ``2A / P`` - no assumption about the band being
        straight, and no need to trace it.

        For a blob rather than a band the same formula returns roughly its
        inscribed radius, which is the conservative reading.
        """
        if self.perimeter_mm <= 0:
            return 0.0
        return 2.0 * self.area_mm2 / self.perimeter_mm

    def to_dict(self) -> dict:
        return {
            "min_x_mm": self.min_x_mm,
            "min_y_mm": self.min_y_mm,
            "max_x_mm": self.max_x_mm,
            "max_y_mm": self.max_y_mm,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "pixels": self.pixels,
            "area_mm2": self.area_mm2,
            "perimeter_mm": self.perimeter_mm,
            "displacement_mm": self.displacement_mm,
            "widest_mm": self.widest_mm,
        }


class Canvas(NamedTuple):
    """The pixel grid both sides of a comparison are rendered onto.

    Resolution is stated **metrically, as micrometres per pixel**. It is the
    adjustable knob the whole harness turns on,
    carried here rather than baked into a renderer, so one value flows from the
    caller through raster and reporting.

    Nothing in our own interfaces speaks in dots per inch. :attr:`dpi` exists
    only because the rasterizer's document is sized in millimetres and it is
    told the scale in DPI - it is the conversion at that one boundary, and the
    default 5 um/px is exactly 5080 DPI.
    """

    bounds: Bounds
    um_per_pixel: float

    @classmethod
    def over(cls, bounds: Bounds, um_per_pixel: float) -> "Canvas":
        if um_per_pixel <= 0:
            raise ValueError(f"um_per_pixel must be positive, got {um_per_pixel}")
        if bounds.width_mm <= 0 or bounds.height_mm <= 0:
            raise ValueError(f"canvas has no area: {bounds}")
        return cls(bounds=bounds, um_per_pixel=um_per_pixel)

    @property
    def mm_per_pixel(self) -> float:
        return self.um_per_pixel / 1000.0

    @property
    def dpi(self) -> float:
        """Backend-facing only. Never put this in an interface of ours."""
        return MM_PER_INCH * 1000.0 / self.um_per_pixel

    @property
    def width_px(self) -> int:
        """Pixel width, matching how the rasterizer converts a mm document.

        Spelled the rasterizer's way - millimetres to inches to dots - so our
        expected size and the size resvg computes cannot drift apart in the
        last bit and leave the two sides a pixel out of register.
        """
        return max(1, round(self.bounds.width_mm / MM_PER_INCH * self.dpi))

    @property
    def height_px(self) -> int:
        return max(1, round(self.bounds.height_mm / MM_PER_INCH * self.dpi))

    @property
    def total_pixels(self) -> int:
        return self.width_px * self.height_px

    @property
    def pixel_area_mm2(self) -> float:
        """Real area one pixel stands for - the bridge from counts to mm2."""
        return self.mm_per_pixel * self.mm_per_pixel

    def region_of(self, box: PixelBox, pixels) -> Region:
        """A region with its band measurements taken from its own pixels."""
        perimeter_px, widest_px = band_metrics(pixels)
        return self.pixel_box_to_mm(
            box,
            perimeter_mm=perimeter_px * self.mm_per_pixel,
            widest_mm=widest_px * self.mm_per_pixel,
        )

    def pixel_box_to_mm(
        self, box: PixelBox, perimeter_mm: float = 0.0, widest_mm: float = 0.0
    ) -> Region:
        """Inclusive pixel bounds to the millimetre rectangle they cover.

        The box spans whole pixels, so it runs from the leading edge of the
        first to the trailing edge of the last. Image row 0 is the *top*, which
        is the largest gerber Y - hence the flip.
        """
        step = self.mm_per_pixel
        min_x = self.bounds.min_x_mm + box.min_px * step
        max_x = self.bounds.min_x_mm + (box.max_px + 1) * step
        max_y = self.bounds.max_y_mm - box.min_py * step
        min_y = self.bounds.max_y_mm - (box.max_py + 1) * step
        return Region(
            min_x_mm=min_x,
            min_y_mm=min_y,
            max_x_mm=max_x,
            max_y_mm=max_y,
            pixels=box.pixels,
            area_mm2=box.pixels * self.pixel_area_mm2,
            perimeter_mm=perimeter_mm,
            widest_mm=widest_mm,
        )

    def mm_box_to_pixels(self, box: Bounds):
        """A millimetre rectangle as inclusive pixel bounds, clipped to the
        canvas - or ``None`` when none of it is on the canvas at all.

        The inverse of :meth:`pixel_box_to_mm`, and it has to stay that way:
        the region scoping draws these rectangles onto the same lattice the
        measurement is taken on, so a box that came from a pixel box has to
        come back as the pixels it was made of.
        """
        step = self.mm_per_pixel
        min_px = round((box.min_x_mm - self.bounds.min_x_mm) / step)
        max_px = round((box.max_x_mm - self.bounds.min_x_mm) / step) - 1
        min_py = round((self.bounds.max_y_mm - box.max_y_mm) / step)
        max_py = round((self.bounds.max_y_mm - box.min_y_mm) / step) - 1
        min_px, max_px = max(min_px, 0), min(max_px, self.width_px - 1)
        min_py, max_py = max(min_py, 0), min(max_py, self.height_px - 1)
        if min_px > max_px or min_py > max_py:
            return None
        return (min_px, min_py, max_px, max_py)

    def cell_size_px(self, cell_mm: float) -> int:
        """Region-clustering cell size in pixels, never smaller than one."""
        if cell_mm <= 0:
            raise ValueError(f"cell_mm must be positive, got {cell_mm}")
        return max(1, round(cell_mm / self.mm_per_pixel))


def band_metrics(pixels) -> tuple:
    """``(perimeter_px, widest_px)`` of a set of differing pixels.

    Both are what :attr:`Region.displacement_mm` and
    :attr:`Region.widest_mm` are built from, and both are plain counting:

    * **perimeter** is the number of pixel edges with nothing on the other
      side - the 4-neighbour boundary of the set.
    * **widest** is the thickest point, estimated as the largest
      ``min(horizontal run, vertical run)`` through any pixel. It is an
      estimate and named as one: a band running at 45 degrees reads up to a
      factor of root two wide, which errs toward reporting a fault rather than
      hiding one.
    """
    pixels = set(pixels)
    if not pixels:
        return (0, 0)

    perimeter = 0
    for px, py in pixels:
        for neighbour in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
            if neighbour not in pixels:
                perimeter += 1

    # Run lengths along each axis, one pass per axis, so the thickest point
    # costs O(n log n) rather than a walk out from every pixel.
    runs = [{}, {}]
    for axis, key in enumerate((lambda p: (p[1], p[0]), lambda p: (p[0], p[1]))):
        ordered = sorted(pixels, key=key)
        start = 0
        while start < len(ordered):
            end = start + 1
            while end < len(ordered):
                previous, current = key(ordered[end - 1]), key(ordered[end])
                if current[0] != previous[0] or current[1] != previous[1] + 1:
                    break
                end += 1
            length = end - start
            for pixel in ordered[start:end]:
                runs[axis][pixel] = length
            start = end

    widest = max(
        min(runs[0][pixel], runs[1][pixel]) for pixel in pixels
    )
    return (perimeter, widest)


def cluster_regions(pixels, cell_px: int) -> list:
    """Group differing pixels into regions, keeping each region's pixels.

    Same clustering as :func:`cluster_pixels` - see it for the binning rule -
    but the pixels come back with the box, because the displacement metric
    needs the shape of a region and not only how many pixels it holds.
    """
    grouped = _cluster(pixels, cell_px)
    return [
        (PixelBox(*box), tuple(members)) for box, members in grouped
    ]


def cluster_pixels(pixels, cell_px: int) -> list[PixelBox]:
    """Group differing pixels into regions by binning and merging cells.

    Pixels land in ``cell_px``-square cells; cells touching on an edge or a
    corner join the same region. Each region reports the exact bounding box of
    its pixels, not of its cells, so the numbers stay honest even though the
    grouping is coarse.

    Returned top-to-bottom then left-to-right, so two runs over the same input
    report regions in the same order: the same input gives the same output,
    for this harness's own output too.
    """
    return [PixelBox(*box) for box, _ in _cluster(pixels, cell_px)]


def _cluster(pixels, cell_px: int) -> list:
    """The clustering itself: ``[(box_list, [pixels]), ...]``, ordered."""
    if cell_px < 1:
        raise ValueError(f"cell_px must be at least 1, got {cell_px}")

    cells: dict[tuple[int, int], list] = {}
    members: dict[tuple[int, int], list] = {}
    for px, py in pixels:
        key = (px // cell_px, py // cell_px)
        box = cells.get(key)
        if box is None:
            cells[key] = [px, py, px, py, 1]
            members[key] = [(px, py)]
        else:
            if px < box[0]:
                box[0] = px
            if py < box[1]:
                box[1] = py
            if px > box[2]:
                box[2] = px
            if py > box[3]:
                box[3] = py
            box[4] += 1
            members[key].append((px, py))
    if not cells:
        return []

    parent = {key: key for key in cells}

    def find(key):
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:  # path compression
            parent[key], key = root, parent[key]
        return root

    neighbours = ((1, 0), (0, 1), (1, 1), (1, -1))
    for cx, cy in cells:
        for dx, dy in neighbours:
            other = (cx + dx, cy + dy)
            if other in cells:
                a, b = find((cx, cy)), find(other)
                if a != b:
                    parent[a] = b

    merged: dict[tuple[int, int], list] = {}
    merged_members: dict[tuple[int, int], list] = {}
    for key, box in cells.items():
        root = find(key)
        grown = merged.get(root)
        if grown is None:
            merged[root] = list(box)
            merged_members[root] = list(members[key])
        else:
            grown[0] = min(grown[0], box[0])
            grown[1] = min(grown[1], box[1])
            grown[2] = max(grown[2], box[2])
            grown[3] = max(grown[3], box[3])
            grown[4] += box[4]
            merged_members[root].extend(members[key])

    grouped = [(merged[root], merged_members[root]) for root in merged]
    grouped.sort(key=lambda pair: (pair[0][1], pair[0][0], pair[0][3], pair[0][2]))
    return grouped


def set_bit_pixels(packed: bytes, width_px: int, height_px: int):
    """Yield ``(px, py)`` for every set bit of a 1-bit-per-pixel bitmap.

    This is Pillow's mode ``"1"`` packing: rows are padded up to a byte
    boundary, most significant bit leftmost. Padding bits are never yielded.
    """
    row_bytes = (width_px + 7) // 8
    expected = row_bytes * height_px
    if len(packed) != expected:
        raise ValueError(
            f"bitmap is {len(packed)} bytes, expected {expected} for "
            f"{width_px}x{height_px}"
        )
    for index, value in enumerate(packed):
        if not value:
            continue
        py, column = divmod(index, row_bytes)
        base = column * 8
        for bit in range(8):
            if value & (0x80 >> bit):
                px = base + bit
                if px < width_px:
                    yield px, py
