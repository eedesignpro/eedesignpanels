"""Compare two gerber sets pixel by pixel. The contract of the harness.

``compare(dir_a, dir_b, um_per_pixel, threshold)`` renders both sets onto one
shared canvas and reports, per layer, what is inked on each side, how much
of it differs, and where.

**This is a tool, not a gate.** A difference is
never an exception and never a nonzero exit code: a build that produces design
files completes and keeps its artifacts whatever this reports. Differences warn
- loudly, in stdout and on the review page - and that is all. Structural
failures are a separate matter and still propagate, because errors are never
swallowed: a
missing directory or an unparseable gerber means the comparison did not happen,
which is not the same as "no differences found".

The result is data, not a verdict, and its shape is designed to grow - see
:mod:`eedesignpanels.verify.results`, which holds the types and depends on
nothing but arithmetic.
"""

import time
from pathlib import Path

from eedesignpanels.verify import carrier as carrier_module
from eedesignpanels.verify import raster
from eedesignpanels.verify.gerbers import collect_gerbers
from eedesignpanels.verify.regions import (
    Canvas,
    Region,
    cluster_regions,
    set_bit_pixels,
    union_bounds,
)
from eedesignpanels.verify.results import DEFAULT_THRESHOLD, CompareResult, LayerDiff

DEFAULT_UM_PER_PIXEL = 5.0
DEFAULT_REGION_CELL_MM = 1.0


def diff_regions(difference, canvas: Canvas, cell_px: int) -> list[Region]:
    """The clusters of differing pixels, **worst first**.

    Only the differing area is scanned: Pillow finds the overall bounding box
    in C, and the per-pixel walk happens inside that crop. On a clean layer the
    walk does not happen at all.

    Ordering is by displacement, largest first, because that is the order an
    engineer wants to read: the first line is the worst thing that happened.
    Ties fall back to position, so two runs over the same input still report
    identically - the same input gives the same output, for this harness's own
    output too.
    """
    box = difference.getbbox()
    if box is None:
        return []
    left, top, right, bottom = box
    crop = difference.crop(box)
    pixels = (
        (px + left, py + top)
        for px, py in set_bit_pixels(crop.tobytes(), right - left, bottom - top)
    )
    regions = [
        canvas.region_of(pixel_box, members)
        for pixel_box, members in cluster_regions(pixels, cell_px)
    ]
    # Hard first: anything wider than one measuring pixel is a real shape,
    # while nothing narrower can be told from where the rasterizer happened to
    # put an edge. Within each class, furthest-moved first.
    limit = canvas.mm_per_pixel * 1.0001
    regions.sort(
        key=lambda region: (
            0 if region.widest_mm > limit else 1,
            -region.displacement_mm, -region.pixels,
            region.min_x_mm, -region.max_y_mm,
        )
    )
    return regions


def render_layer(gerber, canvas: Canvas):
    """One side of one layer. A layer only one side has renders as blank.

    A gerber with no bounding box has nothing to draw, so it is answered with
    a blank rather than sent through the rasterizer. That is not an
    optimisation of convenience: on a panel canvas a full render costs seconds
    and hundreds of megabytes, and a board carries dozens of layers it never
    put anything on.
    """
    if gerber is None or raster.bounds_of(gerber) is None:
        return raster.blank(canvas)
    return raster.rasterize(gerber, canvas)


def measure_layer(
    layer: str,
    image_a,
    image_b,
    difference,
    canvas: Canvas,
    present_in_a: bool,
    present_in_b: bool,
    threshold: int,
    cell_px: int,
    displacement_threshold_mm: float = None,
    judged: bool = True,
    seconds: float = 0.0,
    metrics: dict = None,
    carrier=None,
) -> LayerDiff:
    """Everything measured about one layer, from its two rendered masks.

    Shared by the two comparisons this module offers - export against export,
    and a composed reference against a panel export - so both report the same
    quantities computed the same way. ``difference`` is passed in rather than
    recomputed because the caller needs the same image for its pictures.

    ``difference`` is **what is being judged**, which is the whole XOR unless
    the caller scoped it (:mod:`eedesignpanels.verify.carrier`). Every count
    below is taken against it rather than merely inside its bounding box, so a
    scoped difference reports the region it covers and nothing else; when it is
    the full XOR the two are the same thing and the numbers are unchanged.
    """
    # The difference is sparse and the canvas is tens of megapixels, so both
    # difference counts are taken inside the differing area's own bounding box:
    # outside it the two sides agree by definition, so ``a AND NOT b`` is zero
    # there and the counts are identical to the full-canvas ones. The ink
    # counts below still cover the full canvas - ink is not sparse.
    box = difference.getbbox()
    if box is None:
        differing = 0
        only_a = 0
    else:
        crop = difference.crop(box)
        differing = raster.count_ink(crop)
        only_a = raster.count_ink(
            raster.intersect(
                raster.only_in_first(image_a.crop(box), image_b.crop(box)), crop
            )
        )
    return LayerDiff(
        layer=layer,
        present_in_a=present_in_a,
        present_in_b=present_in_b,
        total_pixels=canvas.total_pixels,
        pixel_area_mm2=canvas.pixel_area_mm2,
        ink_pixels_a=raster.count_ink(image_a),
        ink_pixels_b=raster.count_ink(image_b),
        pixels_differing=differing,
        pixels_only_in_a=only_a,
        pixels_only_in_b=differing - only_a,
        threshold=threshold,
        displacement_threshold_mm=displacement_threshold_mm,
        judged=judged,
        carrier=carrier,
        regions=tuple(
            diff_regions(difference, canvas, cell_px) if differing else ()
        ),
        seconds=seconds,
        metrics=dict(metrics or {}),
    )


def compare(
    dir_a,
    dir_b,
    um_per_pixel: float = DEFAULT_UM_PER_PIXEL,
    threshold: int = DEFAULT_THRESHOLD,
    region_cell_mm: float = DEFAULT_REGION_CELL_MM,
    on_layer=None,
) -> CompareResult:
    """Compare every layer of two gerber directories on one shared canvas.

    ``um_per_pixel`` is adjustable end to end and decides what the harness can
    see. It is stated metrically because the board is: at the default 5 um a
    0.2 mm shift is 40 px wide, and a
    0.1 mm trace is 20 px. ``threshold`` is a count of differing pixels; 0
    means report everything.

    ``on_layer(layer, image_a, image_b, difference, result)`` is called once per
    layer while its bitmaps are still in hand, so a caller that wants pictures
    - the review page does - gets them without a second rasterization pass.
    """
    if threshold < 0:
        raise ValueError(f"threshold must not be negative, got {threshold}")
    started = time.monotonic()
    dir_a, dir_b = Path(dir_a), Path(dir_b)

    files_a = collect_gerbers(dir_a)
    files_b = collect_gerbers(dir_b)
    parsed_a = {key: raster.open_gerber(path) for key, path in files_a.items()}
    parsed_b = {key: raster.open_gerber(path) for key, path in files_b.items()}

    boxes = [
        box
        for gerber in list(parsed_a.values()) + list(parsed_b.values())
        if (box := raster.bounds_of(gerber)) is not None
    ]
    if not boxes:
        raise ValueError(
            f"neither {dir_a} nor {dir_b} holds a gerber with any content"
        )
    canvas = Canvas.over(union_bounds(boxes), um_per_pixel)
    cell_px = canvas.cell_size_px(region_cell_mm)

    layers = []
    for key in sorted(set(parsed_a) | set(parsed_b)):
        layer_started = time.monotonic()
        gerber_a, gerber_b = parsed_a.get(key), parsed_b.get(key)
        image_a = render_layer(gerber_a, canvas)
        image_b = render_layer(gerber_b, canvas)
        difference = raster.xor(image_a, image_b)
        result = measure_layer(
            layer=key,
            image_a=image_a,
            image_b=image_b,
            difference=difference,
            canvas=canvas,
            present_in_a=gerber_a is not None,
            present_in_b=gerber_b is not None,
            threshold=threshold,
            cell_px=cell_px,
            seconds=time.monotonic() - layer_started,
        )
        layers.append(result)
        if on_layer is not None:
            on_layer(key, image_a, image_b, difference, result)

    return CompareResult(
        canvas=canvas,
        um_per_pixel=um_per_pixel,
        threshold=threshold,
        region_cell_mm=region_cell_mm,
        layers=tuple(layers),
        dir_a=dir_a,
        dir_b=dir_b,
        seconds=time.monotonic() - started,
    )


def format_summary(result: CompareResult) -> str:
    """The loud stdout block. Returned, not printed - the caller decides.

    Never phrased as a pass or a failure: this reports, the user judges.
    """
    canvas = result.canvas
    lines = [
        "=" * 78,
        f"GERBER COMPARISON  {result.um_per_pixel:g} um/px  "
        f"threshold {result.threshold} px",
        f"  A: {result.dir_a}",
        f"  B: {result.dir_b}",
        f"  canvas {canvas.width_px} x {canvas.height_px} px "
        f"= {canvas.bounds.width_mm:.3f} x {canvas.bounds.height_mm:.3f} mm",
        "-" * 78,
        f"{'layer':<22}{'moved um':>9}{'diff px':>10}{'diff mm2':>12}"
        f"{'A ink mm2':>12}{'B ink mm2':>12}{'regions':>9}",
        "-" * 78,
    ]
    for layer in result.layers:
        # ".." is measured but not judged: it has numbers and none of them
        # were allowed to decide anything.
        mark = "!!" if layer.over_threshold else ("  " if layer.judged else "..")
        # Keyed off ink, not off whether a gerber file exists: an empty export
        # on both sides is not "present only in B".
        note = ""
        if layer.ink_pixels_a and not layer.ink_pixels_b:
            note = "   [content only in A]"
        elif layer.ink_pixels_b and not layer.ink_pixels_a:
            note = "   [content only in B]"
        moved = (
            f"{layer.worst_displacement_mm * 1000:>9.2f}"
            if layer.regions else f"{'-':>9}"
        )
        lines.append(
            f"{mark}{layer.layer:<20}{moved}{layer.pixels_differing:>10}"
            f"{layer.area_differing_mm2:>12.4f}"
            f"{layer.ink_area_a_mm2:>12.3f}{layer.ink_area_b_mm2:>12.3f}"
            f"{layer.region_count:>9}{note}"
        )
        if layer.has_carrier:
            # Said on the layer, beside its numbers: what was set aside, how
            # much of it there was, and what it is, because a number withheld
            # without a reason hides what the comparison set aside.
            lines.append(
                f"      carrier {layer.carrier_ink_area_mm2:.4f} mm2 of ink, "
                f"{layer.carrier_area_differing_mm2:.4f} mm2 of difference "
                f"NOT judged - {layer.carrier_reason}"
            )
        if layer.carrier_leaked:
            lines.append(carrier_module.leak_warning(layer.layer, layer.carrier))
        if layer.over_threshold:
            lines.append(
                f"      only in A {layer.pixels_only_in_a} px "
                f"({layer.area_only_in_a_mm2:.4f} mm2), "
                f"only in B {layer.pixels_only_in_b} px "
                f"({layer.area_only_in_b_mm2:.4f} mm2), "
                f"{layer.share_of_ink * 100:.4f}% of inked area"
            )
        for index, region in enumerate(layer.regions, 1):
            lines.append(
                f"      region {index}: moved "
                f"{region.displacement_mm * 1000:.2f} um "
                f"(widest {region.widest_mm * 1000:.1f} um) at "
                f"x {region.min_x_mm:.3f}..{region.max_x_mm:.3f} mm, "
                f"y {region.min_y_mm:.3f}..{region.max_y_mm:.3f} mm "
                f"({region.width_mm:.3f} x {region.height_mm:.3f} mm), "
                f"{region.pixels} px = {region.area_mm2:.4f} mm2"
            )
    lines.append("-" * 78)
    if result.carrier_layers:
        lines.append(
            f"Carrier artwork on {len(result.carrier_layers)} layer(s), "
            f"{result.carrier_ink_area_mm2:.3f} mm2 in total: measured and "
            f"reported, never judged. The boards themselves are judged in "
            f"full."
        )
        for layer in result.carrier_layers:
            lines.append(
                f"  {layer.layer:<24}{layer.carrier_ink_area_mm2:>10.4f} mm2  "
                f"{layer.carrier_reason}"
            )
    if result.leaked_layers:
        lines += ["*" * 78, "*** THE WINDOW RULE IS WRONG SOMEWHERE ***"]
        lines += [
            carrier_module.leak_warning(layer.layer, layer.carrier)
            for layer in result.leaked_layers
        ]
        lines.append("*" * 78)
    worst = max(
        (layer.worst_displacement_mm for layer in result.layers), default=0.0
    )
    if result.clean:
        lines.append(
            f"Nothing moved enough to matter. Worst displacement anywhere "
            f"{worst * 1000:.2f} um, over {result.pixels_differing} differing "
            f"pixel(s). {len(result.layers)} layer(s) compared in "
            f"{result.seconds:.2f} s."
        )
    else:
        names = ", ".join(layer.layer for layer in result.layers_over_threshold)
        lines += [
            "*" * 78,
            "*** WARNING: GEOMETRY MOVED ***",
            f"*** {len(result.layers_over_threshold)} layer(s) over "
            f"threshold: {names}",
            f"*** Worst displacement {worst * 1000:.2f} um, over "
            f"{result.pixels_differing} differing pixel(s) "
            f"({result.area_differing_mm2:.4f} mm2 in total).",
            "*** This is a report, not a gate - the build's output stands.",
            "*" * 78,
        ]
    lines.append("=" * 78)
    return "\n".join(lines)
