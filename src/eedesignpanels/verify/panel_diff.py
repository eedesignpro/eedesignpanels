"""The `build_panel` comparison: a placed reference against the built panel.

This is the comparison every panel build produces. It answers
one question - *did the builder put the board where the grid says it should
go* - and it answers it without consulting the builder's own geometry:

1. The **source board** is exported to gerbers and its artwork **placed at
   every position the grid asks for inside one composed document** - declared
   once, instanced by ``<use>`` at each placement's exact offset (see
   :func:`eedesignpanels.verify.vector.placed_reference_document`). Placements
   come from :mod:`eedesignpanels.placement` and the config.
2. That document is rendered once per layer into the reference panel.
3. The **built panel** is exported to gerbers and rendered on the same canvas.
4. The two are compared with the existing harness, and the result lands on the
   diff page.

Placing the artwork rather than pasting one render at pixel offsets is what
makes the reference exact: a real board's step is not a whole number of
pixels, so a paste is always a fraction of a pixel out. Every instance here is
rasterized at its true sub-pixel position, so there is no tolerance to set and
nothing to round. The instances are composed into one document and rendered
once, instead of one full-canvas render per placement, because a placement
render's cost is the canvas, not the artwork. The
measured cost of composition is 264 differing pixels in 57 million on the
densest corpus layer - single-pixel edge effects an order of magnitude below
the verdict threshold.

**It is a report, never a gate.** Every outcome here - a difference,
a canvas too large to compare - warns and returns; nothing raises and nothing
stops the build. A structural failure (an unreadable gerber, a missing board)
still propagates, because "the comparison did not happen" must never look like
"no differences found".

**Layers the stage does not replicate get an empty reference.** `build_panel`
replicates the outline and nothing else, so the reference for `F_Cu` is blank
by construction and the panel's `F_Cu` is blank in fact. That reads as zero,
and the page says in words which layers were actually placed, so a zero on a
layer nobody replicated cannot be mistaken for verified copper.
"""

import threading
import time
from concurrent import futures
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.review import layerpanel
from eedesignpanels.review.render import LayerSpec
from eedesignpanels.verify import carrier, raster, reference, vector
from eedesignpanels.verify import workers as pool_size
from eedesignpanels.verify.compare import (
    DEFAULT_REGION_CELL_MM,
    DEFAULT_UM_PER_PIXEL,
    measure_layer,
    render_layer,
)
from eedesignpanels.verify.gerbers import collect_gerbers, export_gerbers
from eedesignpanels.verify.regions import Canvas, union_bounds
from eedesignpanels.verify.report import (
    DiffRun,
    build_diff_page,
    compared_specs,
    data_uri,
    differing_counts,
    status_marks,
)
from eedesignpanels.verify.results import DEFAULT_THRESHOLD, CompareResult, LayerDiff

# Generous for a board-sized panel and honest about what it costs: the
# reference board's 2x3 panel is 51 Mpx at 5 um/px, and peak working set runs
# around 16 MB per megapixel, so this cap is roughly 1.6 GB.
DEFAULT_MAX_CANVAS_MPIXELS = 100.0
# The inline pictures are downscaled to this; the measurement never is. At
# 8000 the reference board's panel needs no reduction at all, so what is on
# the page is the
# measurement's own pixels rather than a pooled copy of them.
DEFAULT_DISPLAY_MAX_PX = 8000
# How far outside the board outline the comparison still looks. Enough to catch
# a fault that pushes copper off the edge, small enough that fabrication text
# parked far from the board does not set the canvas size.
DEFAULT_CLIP_MARGIN_MM = 1.0
# The verdict threshold, in the unit the verdict is now judged in: how far the
# geometry moved at the worst place on a layer.
#
# Chosen between two measured scales, roughly a decade clear of each:
#   * the rasterizer's own floor - the arc slivers left by the patched
#     gerbonara - measures well under 0.001 mm of band width;
#   * the fault the harness was proven on is a 0.2 mm track translation, and a
#     real fault of interest is that order.
# 0.01 mm sits ten times above the floor and twenty times below the fault, so
# noise stays green and anything an engineer would call a defect goes red.
DEFAULT_DISPLACEMENT_THRESHOLD_MM = 0.01
# Which layers may set a verdict. "physical" reuses the review page's own
# default-visibility rule - the layers that become the bare board - so the two
# answers come from one place.
DEFAULT_COMPARED_LAYERS = "physical"
# The vector guardrail. Vector is the default because a difference has to stay
# sharp when it is zoomed into; these caps only catch a layer denser than
# anything measured, and it falls back to pixels alone rather than spoiling
# the page. The worst layer on the reference panel is 946 KB / 5,033 elements.
DEFAULT_VECTOR_MAX_KB = 4096.0
DEFAULT_VECTOR_MAX_ELEMENTS = 40000


class Skipped(NamedTuple):
    """Why no comparison happened. Loud, specific, and never fatal."""

    reason: str
    details: tuple = ()

    def banner(self) -> str:
        """The block that has to be impossible to scroll past."""
        lines = [
            "*" * 78,
            "*** GERBER COMPARISON SKIPPED - the panel was NOT verified ***",
            f"*** {self.reason}",
        ]
        lines += [f"***   {detail}" for detail in self.details]
        lines += [
            "*** The build's output stands; nothing was blocked and nothing "
            "was rounded.",
            "*" * 78,
        ]
        return "\n".join(lines)


class PanelDiff(NamedTuple):
    """The outcome: a result and a page, or a reason there is neither."""

    result: CompareResult = None
    page: Path = None
    skipped: Skipped = None
    canvas: Canvas = None
    offsets: tuple = ()
    seconds: float = 0.0

    @property
    def ran(self) -> bool:
        return self.result is not None


def gerber_key_of(layer: LayerSpec) -> str:
    """The filename key kicad-cli gives a layer: display name, dots to
    underscores. The same rule the SVG export follows."""
    return layer.svg_suffix


def panel_owned_keys(layers, panel_owned) -> dict:
    """The layers the PANEL derives, and the sentence that says why each is not
    judged.

    A stage that steps and repeats a board can be checked by stamping the board
    at every placement and comparing. Once the panel derives a layer of its own
    - the outline is the sheet minus the routed channels, the score lines are
    one pass of a blade across the whole panel - that reference does not exist:
    there is nothing per-instance to stamp. Measuring it is still worth doing
    and is still done; what would be wrong is calling the difference a fault.
    """
    wanted = set(panel_owned or ())
    return {
        gerber_key_of(layer): (
            f"{layer.canonical_name} is derived for the panel as a whole, so "
            f"the board stamped at every placement is not what it should look "
            f"like - measured and reported, never judged"
        )
        for layer in layers
        if layer.canonical_name in wanted
    }


def judged_keys(layers, compared) -> set:
    """Which layers' results are allowed to set a verdict.

    ``compared`` is the config's ``verify.compared_layers``:

    * ``"physical"`` (the default) - the layers that drive physical
      construction of the bare board, decided by the same rule that drives the
      review page's default visibility, :func:`layerpanel.is_physical_layer`.
      One rule, two consumers, no second list to drift out of step.
    * ``"all"`` - judge everything that was compared.
    * a list of canonical names - judge exactly those.

    Everything else is still replicated, still rendered and still measured. It
    simply does not decide anything: fabrication and documentation layers are
    not what the bare board is built from, and a build that reported them as
    faults on every run would be a report nobody reads.

    Raises:
        ValueError: when an explicit list names a layer the board does not
            have. Silently judging nothing is the failure worth avoiding here.
    """
    if compared == "all":
        return {gerber_key_of(layer) for layer in layers}
    if compared is None or compared == "physical":
        return {
            gerber_key_of(layer)
            for layer in layers
            if layerpanel.is_physical_layer(layer)
        }
    wanted = set(compared)
    by_name = {layer.canonical_name: layer for layer in layers}
    missing = wanted - set(by_name)
    if missing:
        raise ValueError(
            f"verify.compared_layers names layer(s) the board does not have: "
            f"{', '.join(sorted(missing))}"
        )
    return {gerber_key_of(by_name[name]) for name in wanted}


def replicated_specs(layers, canonical_names) -> list[LayerSpec]:
    """The layer specs a stage promises to replicate, in board order.

    Raises:
        ValueError: when the stage names a layer the board does not have,
            which means the stage and the board disagree about what exists.
    """
    wanted = set(canonical_names)
    found = [layer for layer in layers if layer.canonical_name in wanted]
    missing = wanted - {layer.canonical_name for layer in found}
    if missing:
        raise ValueError(
            f"the stage replicates layer(s) the board does not have: "
            f"{', '.join(sorted(missing))}"
        )
    return found


def _reference_boxes(parsed_source: dict, replicated_keys: set, offsets) -> list:
    """Gerber-space extents the placed reference will cover."""
    boxes = []
    for key in sorted(replicated_keys):
        gerber = parsed_source.get(key)
        box = raster.bounds_of(gerber) if gerber is not None else None
        if box is not None:
            boxes.append(reference.covered_bounds(box, offsets))
    return boxes


def comparison_window(
    parsed_source: dict,
    parsed_panel: dict,
    outline_key: str,
    replicated_keys: set,
    offsets,
    margin_mm: float,
    log=print,
) -> tuple:
    """The window both sides are rendered in: the board outline, plus a margin.

    **The comparison is scoped to the board area, and that is a deliberate
    narrowing.** Once the stage injects whole boards the content
    extent is no longer the board: fabrication and silkscreen text sits far
    outside the outline, and taking the window from it made the reference
    board's own 1x1 case a 106 Mpx canvas that could not be compared at all.
    What ships is the board area, so that is what is measured.

    Both sides are clipped by the *same* window - the panel's outline and the
    source's outline placed at every placement, unioned - so nothing is
    compared against a blank, and the two describe one pixel lattice.

    Returns ``(bounds, clipped)``. ``clipped`` is False when either side has no
    outline to clip to, in which case this falls back to the full content
    extent and says so: guessing a board area from silkscreen would be worse
    than admitting the window is wide.
    """
    source_outline = raster.bounds_of(parsed_source[outline_key]) \
        if outline_key in parsed_source else None
    panel_outline = raster.bounds_of(parsed_panel[outline_key]) \
        if outline_key in parsed_panel else None

    if source_outline is not None and panel_outline is not None:
        window = union_bounds([
            reference.covered_bounds(source_outline, offsets),
            panel_outline,
        ])
        return reference.expand_bounds(window, margin_mm), True

    missing = "source" if source_outline is None else "built panel"
    log(f"  WARNING: the {missing} has no {outline_key} content, so the "
        f"comparison window cannot be clipped to the board outline and falls "
        f"back to the full content extent - fabrication text outside the board "
        f"is included and the canvas will be larger than the board.")
    boxes = _reference_boxes(parsed_source, replicated_keys, offsets)
    boxes += [
        box
        for gerber in parsed_panel.values()
        if (box := raster.bounds_of(gerber)) is not None
    ]
    return (union_bounds(boxes) if boxes else None), False


def render_reference(gerber, canvas: Canvas, offsets):
    """One layer of the reference: every placement, composed, ONE render.

    The artwork is declared once and instanced by ``<use>`` at each
    placement's exact offset inside a single document
    (:func:`vector.placed_reference_document`), so the whole reference costs
    one render of the panel canvas instead of one per placement. A render's
    cost is dominated by the canvas, not the artwork - measured: a
    *blank* 57 Mpx render already costs 0.94 s of the 1.2 s a placement render
    took - so this is where the comparison's time went. Placement stays exact:
    the ``<use>`` offsets are the same nanometre-derived values the
    per-placement windows encoded, written exactly. Against one render per
    placement, the measured difference is 264 pixels in 57 million on the
    densest corpus layer. A layer with nothing on it is answered with one
    blank.
    """
    if raster.bounds_of(gerber) is None:
        return raster.blank(canvas)
    return raster.rasterize_svg(
        vector.placed_reference_document(gerber, canvas, offsets), canvas
    )


def _log_windows(canvas: Canvas, offsets, log) -> None:
    """Print the transform gerbonara will emit, and whether it is exact.

    The defect this guards against is invisible in the pictures - a fifth of a
    pixel of y registration - so the evidence that it is not happening belongs
    in the log rather than in anybody's confidence. With the composed
    reference there is ONE render window, the panel's own, shared by both
    sides; each placement is a ``<use>`` offset written at six decimal places
    of a millimetre, exact for the nanometre-derived values placements are
    made of - and the worst formatting error is printed so "exact" stays a
    measurement rather than a belief.
    """
    window_error = abs(reference.transform_error_mm(canvas.bounds))
    offset_error = max(
        (
            abs(float(f"{value:.6f}") - value)
            for offset in offsets
            for value in offset
        ),
        default=0.0,
    )
    log(f"  transform    window y rounding {window_error * 1e6:.4g} nm; "
        f"worst <use> offset rounding {offset_error * 1e6:.4g} nm over "
        f"{len(offsets)} placement(s)")
    first, top, third, bottom = reference.transform_strings(canvas.bounds)
    log(f"    window        translate({first} {top}) scale(1 -1) "
        f"translate({third} {bottom})")


def _layer_vector(key, source_gerber, panel_gerber, only_expected, only_built,
                  canvas, offsets, max_kb: float, max_elements: int):
    """One layer's picture as vectors, or ``None`` if it is too heavy.

    The cap is the guardrail, not the intent: vector is the default because a
    difference has to stay sharp when the user zooms into it to judge it.
    A layer that outgrows the cap - a denser panel than anything measured -
    falls back to pixels for that layer alone rather than making the whole
    page unusable.
    """
    expected = (
        vector.placed_reference(source_gerber, canvas, offsets, key)
        if source_gerber is not None
        else vector.placed_reference_empty(canvas)
    )
    built = (
        vector.built_panel(panel_gerber, canvas)
        if panel_gerber is not None
        else vector.empty_sheet(canvas, "built")
    )
    drawn = vector.blend(
        expected, built,
        vector.delta_markers(only_expected, only_built, canvas),
    )
    if drawn.kilobytes > max_kb or drawn.elements > max_elements:
        return None
    return drawn


def _layer_pictures(reference_image, panel_image, difference, factor: int) -> dict:
    """The four pictures of one layer, downscaled for the page.

    Downscaling is max-pooling (:func:`raster.reduce_mask`), so a single
    differing pixel on a 51 Mpx panel still shows. The numbers beside them come
    from the full-resolution masks - display shrinks, measurement never does.
    """
    small_a = raster.reduce_mask(reference_image, factor)
    small_b = raster.reduce_mask(panel_image, factor)
    small_diff = raster.reduce_mask(difference, factor)
    return {
        "blend": raster.encode_png(raster.blend_overlay(small_a, small_b)),
        "a": raster.to_png_bytes(small_a),
        "b": raster.to_png_bytes(small_b),
        "diff": raster.to_png_bytes(small_diff),
    }


def scope_difference(
    key: str,
    image_a,
    image_b,
    difference,
    canvas: Canvas,
    windows,
):
    """Split one layer's difference into what is judged and what is carrier.

    The rule and the reasons are in :mod:`eedesignpanels.verify.carrier`; this
    is the arithmetic. Returns ``(judged_difference, carrier)``, and
    ``(difference, None)`` whenever the layer holds no carrier artwork - which
    is every layer of a panel that has no carrier, so the scoping costs those
    runs their numbers unchanged.
    """
    if not windows:
        return difference, None
    window_mask = raster.mask_over(canvas, carrier.windows_of(windows))
    only_built = raster.only_in_first(image_b, image_a)
    carrier_ink = raster.outside(only_built, window_mask)
    ink_pixels = raster.count_ink(carrier_ink)
    if not carrier.is_artwork(ink_pixels, canvas.pixel_area_mm2):
        # Nothing out there but the drawing's own rounding. This layer is
        # measured exactly as it was before there was any such thing as a
        # carrier.
        return difference, None

    only_expected = raster.only_in_first(image_a, image_b)
    board_mask = raster.mask_over(canvas, carrier.boards_of(windows))
    # Judged: everything the reference has and the panel does not, wherever it
    # is - nothing missing is ever excused - plus every difference at all
    # inside a board's own outline extent.
    judged = raster.combine(
        only_expected, raster.intersect(difference, board_mask)
    )
    carrier_difference = raster.outside(difference, judged)
    features = raster.coarse_features(
        carrier_ink, canvas, raster.MAX_REDUCTION_FACTOR
    )
    return judged, carrier.CarrierInk(
        ink_pixels=ink_pixels,
        differing_pixels=raster.count_ink(carrier_difference),
        only_expected_pixels=raster.count_ink(
            raster.outside(only_expected, window_mask)
        ),
        reason=carrier.describe(key, features, carrier.block_of(windows)),
        features=features,
        pixel_area_mm2=canvas.pixel_area_mm2,
    )


class LayerJob(NamedTuple):
    """One layer's comparison, as picklable work for a pool process.

    Gerbers travel as **paths**, never parsed objects: a worker parses its own
    file (tens of milliseconds) rather than pickling a parsed gerber across
    the process boundary, and on Windows the pool is a fresh interpreter that
    re-imports this module (spawn semantics), so everything here must be plain
    values.
    """

    key: str
    source_gerber: Path
    panel_gerber: Path
    placed: bool
    present_in_panel: bool
    canvas: Canvas
    offsets: tuple
    threshold: int
    displacement_threshold_mm: float
    judged: bool
    cell_px: int
    display_factor: int
    vector_max_kb: float
    vector_max_elements: int
    blend_path: Path
    # Where the boards are, so this layer's judgment can be scoped to them.
    # Empty means no scoping at all: the whole canvas is judged, which is what
    # a comparison that could not find the board outline has to fall back to.
    windows: tuple = ()


class LayerOutcome(NamedTuple):
    """What one layer job produced - small and picklable, gathered in order."""

    key: str
    result: LayerDiff
    drawn: object = None
    images: dict = None
    drawn_by: str = None


def _reference_metrics(placed: bool, key: str, placements: int) -> dict:
    """The metrics block every layer result carries, one wording for all."""
    return {
        "reference": (
            f"source {key} rendered at {placements} placement(s)"
            if placed
            else "empty - this stage does not replicate the layer"
        ),
        "placements": placements,
    }


def blank_layer(
    key: str,
    canvas: Canvas,
    placed: bool,
    present_in_panel: bool,
    threshold: int,
    displacement_threshold_mm: float,
    judged: bool,
    placements: int,
) -> LayerDiff:
    """A layer with no ink on either side, answered without touching a pixel.

    Rendering two blank canvases only to XOR and count them cost real time at
    panel scale - 30 of the corpus board's 49 layers are empty - and every
    number is known by construction: a gerber with no bounding box rasterizes
    blank (:func:`compare.render_layer` says so), so every count is zero and
    there are no regions. The fields match what :func:`measure_layer` returns
    for two blanks, exactly.
    """
    return LayerDiff(
        layer=key,
        present_in_a=placed,
        present_in_b=present_in_panel,
        total_pixels=canvas.total_pixels,
        pixel_area_mm2=canvas.pixel_area_mm2,
        ink_pixels_a=0,
        ink_pixels_b=0,
        pixels_differing=0,
        pixels_only_in_a=0,
        pixels_only_in_b=0,
        threshold=threshold,
        displacement_threshold_mm=displacement_threshold_mm,
        judged=judged,
        regions=(),
        metrics=_reference_metrics(placed, key, placements),
    )


def compare_layer_job(job: LayerJob) -> LayerOutcome:
    """Compare one layer end to end. Runs in a pool process or in-line.

    The per-layer loop: render the composed reference, render the panel, XOR,
    measure, and draw
    the layer's picture. The full-resolution blend PNG is encoded and written
    on a thread so it overlaps the vector work - the encoder is C and the
    write is I/O - and the thread is joined before returning, because a worker
    must not exit with a write in flight and an encode error must surface.
    """
    layer_started = time.monotonic()
    source = (
        raster.open_gerber(job.source_gerber)
        if job.placed and job.source_gerber is not None
        else None
    )
    panel = (
        raster.open_gerber(job.panel_gerber)
        if job.panel_gerber is not None
        else None
    )
    if source is not None:
        reference_image = render_reference(source, job.canvas, job.offsets)
    else:
        reference_image = raster.blank(job.canvas)
    panel_image = render_layer(panel, job.canvas)
    difference = raster.xor(reference_image, panel_image)
    # Judgment is scoped to the boards before anything is measured: a panel's
    # carrier is artwork the reference cannot have, so judging it literally
    # only ever says the same wrong thing. What is set aside is
    # measured and named rather than dropped.
    judged_difference, carrier_ink = scope_difference(
        job.key, reference_image, panel_image, difference, job.canvas,
        job.windows,
    )

    result = measure_layer(
        layer=job.key,
        image_a=reference_image,
        image_b=panel_image,
        difference=judged_difference,
        canvas=job.canvas,
        present_in_a=job.placed,
        present_in_b=job.present_in_panel,
        threshold=job.threshold,
        displacement_threshold_mm=job.displacement_threshold_mm,
        judged=job.judged,
        cell_px=job.cell_px,
        seconds=time.monotonic() - layer_started,
        metrics=_reference_metrics(job.placed, job.key, len(job.offsets)),
        carrier=carrier_ink,
    )

    drawn = None
    images = None
    drawn_by = None
    # A layer both sides left blank has no picture worth carrying: the numbers
    # say everything, and 49 white panel-sized pictures would not.
    if result.ink_pixels_a or result.ink_pixels_b:
        failures = []

        def write_blend():
            # Full resolution, on disk, whichever picture the page embeds:
            # the picture openable on its own when the page's copy is not
            # enough.
            try:
                job.blend_path.write_bytes(
                    raster.encode_png(
                        raster.blend_overlay(reference_image, panel_image)
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                failures.append(exc)

        writer = threading.Thread(target=write_blend)
        writer.start()
        try:
            # Which side protruded, so the markers can say the same thing the
            # blend beneath them says rather than one undifferentiated colour.
            # Scoped to what was JUDGED: a marker is the comparison pointing at
            # something, and pointing at every pixel of a rail pour points at
            # nothing. The pour is still there to see - it is drawn in the
            # built sheet underneath, in its own colour.
            drawn = _layer_vector(
                job.key,
                source if job.placed else None,
                panel,
                raster.intersect(
                    raster.only_in_first(reference_image, panel_image),
                    judged_difference,
                ),
                raster.intersect(
                    raster.only_in_first(panel_image, reference_image),
                    judged_difference,
                ),
                job.canvas,
                job.offsets,
                job.vector_max_kb,
                job.vector_max_elements,
            )
            if drawn is not None:
                drawn_by = "vector"
            else:
                # Over the cap: this one layer falls back to a picture made of
                # pixels, and the page says so on the layer itself.
                drawn_by = "raster"
                pictures = _layer_pictures(
                    reference_image, panel_image, judged_difference,
                    job.display_factor,
                )
                images = {
                    name: data_uri(png) for name, png in pictures.items()
                }
        finally:
            writer.join()
        if failures:
            raise failures[0]
    return LayerOutcome(
        key=job.key, result=result, drawn=drawn, images=images,
        drawn_by=drawn_by,
    )


def compare_panel(
    source_board: Path,
    panel_board: Path,
    layers,
    replicated,
    placements,
    work_dir: Path,
    page_path: Path,
    title: str,
    um_per_pixel: float = DEFAULT_UM_PER_PIXEL,
    threshold: int = DEFAULT_THRESHOLD,
    region_cell_mm: float = DEFAULT_REGION_CELL_MM,
    max_canvas_mpixels: float = DEFAULT_MAX_CANVAS_MPIXELS,
    display_max_px: int = DEFAULT_DISPLAY_MAX_PX,
    outline_layer: str = "Edge.Cuts",
    clip_margin_mm: float = DEFAULT_CLIP_MARGIN_MM,
    displacement_threshold_mm: float = DEFAULT_DISPLACEMENT_THRESHOLD_MM,
    compared_layers=DEFAULT_COMPARED_LAYERS,
    panel_owned=(),
    vector_max_kb: float = DEFAULT_VECTOR_MAX_KB,
    vector_max_elements: int = DEFAULT_VECTOR_MAX_ELEMENTS,
    workers: int = 0,
    log=print,
) -> PanelDiff:
    """Compare a placed reference against the built panel. Never raises on a
    difference; returns :class:`PanelDiff` describing what happened.

    ``workers`` is the size of the per-layer process pool: 0 (the default)
    sizes it from this machine's CPU count and available RAM
    (:func:`eedesignpanels.verify.workers.default_worker_count`), 1 runs
    serially in-process. The pool changes when a layer is computed, never what
    - results are gathered in layer order, so the same input gives the same
    output whatever the pool size.
    """
    started = time.monotonic()
    work_dir = Path(work_dir)
    # Which renderer produced the pixels, said out loud on every run. It is a
    # patched build and that is a workaround; an unpatched one warns
    # here and again at import, and its numbers are not trustworthy.
    log(raster.backend_banner())
    # Every instance is placed at its TRUE nanometre offset - no lattice, no
    # rounding. That is only possible because the backend is patched: stock
    # gerbonara cannot print a window at nanometre precision, so with it the
    # placements would have to snap onto a micrometre grid and wear a 1-2 nm
    # error. The patch writes six decimal places of a millimetre, so the exact
    # placement survives into the SVG and the reference lands where the panel
    # actually is.
    offsets = tuple(reference.gerber_offset_mm(p) for p in placements)
    if not offsets:
        return PanelDiff(
            skipped=Skipped("the config places no board instances"),
            seconds=time.monotonic() - started,
        )

    replicated_layers = replicated_specs(layers, replicated)
    unjudged_because = panel_owned_keys(layers, panel_owned)
    judged = judged_keys(layers, compared_layers) - set(unjudged_because)
    outline_spec = next(
        (spec for spec in layers if spec.canonical_name == outline_layer), None
    )
    if outline_spec is None:
        raise ValueError(
            f"the comparison clips to the board outline, but {outline_layer} "
            f"is not among the layers being compared"
        )
    source_dir = work_dir / "gerbers" / "source"
    panel_dir = work_dir / "gerbers" / "panel"
    # The outline is exported whether or not the stage replicates it: it is
    # what the comparison window is clipped to, so it has to be measurable.
    source_layers = list(replicated_layers)
    if outline_spec not in source_layers:
        source_layers = [
            spec for spec in layers
            if spec in replicated_layers or spec is outline_spec
        ]
    source_export = export_gerbers(source_board, source_layers, source_dir)
    panel_export = export_gerbers(panel_board, layers, panel_dir)
    log(f"  gerbers      source {len(source_export.gerber_by_layer)} layer(s) in "
        f"{source_export.seconds:.2f}s, panel "
        f"{len(panel_export.gerber_by_layer)} layer(s) in "
        f"{panel_export.seconds:.2f}s")
    for export, side in ((source_export, "source"), (panel_export, "panel")):
        if export.unmapped:
            log(f"  WARNING: no {side} gerber came back for "
                f"{', '.join(export.unmapped)}")

    files_source = collect_gerbers(source_dir)
    files_panel = collect_gerbers(panel_dir)
    parsed_source = {
        key: raster.open_gerber(path) for key, path in files_source.items()
    }
    parsed_panel = {
        key: raster.open_gerber(path) for key, path in files_panel.items()
    }
    replicated_keys = {gerber_key_of(layer) for layer in replicated_layers}

    window, clipped = comparison_window(
        parsed_source, parsed_panel, gerber_key_of(outline_spec),
        replicated_keys, offsets, clip_margin_mm, log,
    )
    if window is None:
        return PanelDiff(
            offsets=offsets,
            seconds=time.monotonic() - started,
            skipped=Skipped(
                "neither the reference nor the panel has any content to compare"
            ),
        )
    # Snapped outward onto the pixel lattice. Padding a comparison window costs
    # blank canvas and nothing else, and it keeps the canvas a whole number of
    # pixels on both axes.
    canvas = reference.snap_canvas(Canvas.over(window, um_per_pixel))

    # Gate two: the canvas the comparison would need. Above the cap it warns
    # and stops rather than coarsening. A tiled comparison would lift this
    # limit and is not built yet.
    megapixels = canvas.total_pixels / 1e6
    if megapixels > max_canvas_mpixels:
        return PanelDiff(
            offsets=offsets,
            canvas=canvas,
            seconds=time.monotonic() - started,
            skipped=Skipped(
                reason=(
                    f"the comparison canvas is {megapixels:.1f} Mpx "
                    f"({canvas.width_px} x {canvas.height_px} at "
                    f"{um_per_pixel:g} um/px), over the "
                    f"{max_canvas_mpixels:g} Mpx cap"
                ),
                details=(
                    "raise verify.max_canvas_mpixels to compare it anyway, or "
                    "raise verify.um_per_pixel to measure it more coarsely - "
                    "the harness will not change its own resolution.",
                ),
            ),
        )

    # Where each board sits on the panel, taken from the SOURCE outline and the
    # build's own placements - the same data the reference is composed from.
    # Everything outside these windows is carrier, and carrier is measured and
    # named rather than judged. No outline means no windows and no
    # scoping: the whole canvas is judged.
    outline_key = gerber_key_of(outline_spec)
    source_outline = (
        raster.bounds_of(parsed_source[outline_key])
        if outline_key in parsed_source else None
    )
    windows = (
        carrier.instance_windows(source_outline, placements, clip_margin_mm)
        if source_outline is not None else ()
    )

    factor = raster.display_factor((canvas.width_px, canvas.height_px),
                                   display_max_px)
    cell_px = canvas.cell_size_px(region_cell_mm)
    log(f"  canvas       {canvas.width_px} x {canvas.height_px} px "
        f"= {megapixels:.1f} Mpx at {um_per_pixel:g} um/px, "
        f"images shown 1:{factor}")
    log(f"  reference    {len(offsets)} placement(s) per replicated layer, "
        f"composed and rendered once: {len(replicated_keys)} layer(s)")
    log(f"  window       clipped to the {outline_layer} extent of both sides "
        f"+ {clip_margin_mm:g} mm"
        if clipped else
        "  window       NOT clipped - full content extent, see the warning above")
    log(f"  judged       {len(judged & (set(files_source) | set(files_panel)))}"
        f" of {len(set(files_source) | set(files_panel))} layer(s) decide the "
        f"result (verify.compared_layers = {compared_layers!r}); the rest are "
        f"measured and reported for information")
    if unjudged_because:
        log(f"  panel-owned  {', '.join(sorted(panel_owned))} - derived for the "
            f"panel as a whole, so the placed reference is not what they should "
            f"look like; measured and reported, never judged")
    if windows:
        board = windows[0].board
        log(f"  scope        {len(windows)} instance window(s) judged in full: "
            f"each board's {board.width_mm:.3f} x {board.height_mm:.3f} mm "
            f"outline extent + {clip_margin_mm:g} mm. Anything the panel adds "
            f"outside them is carrier artwork - measured and named, never "
            f"judged; anything the board should have and the panel has not is "
            f"judged wherever it is")
    else:
        log("  scope        NOT scoped - no source outline to place, so the "
            "whole canvas is judged and a panel carrier would read as a fault")
    log("  placement    exact, at full nanometre precision - nothing snapped, "
        "nothing rounded")
    _log_windows(canvas, offsets, log)

    image_dir = work_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    every_key = sorted(set(files_source) | set(files_panel))
    jobs = []
    blanks: dict = {}
    for key in every_key:
        placed = key in replicated_keys and key in parsed_source
        source_inked = (
            placed and raster.bounds_of(parsed_source[key]) is not None
        )
        panel_gerber = parsed_panel.get(key)
        panel_inked = (
            panel_gerber is not None
            and raster.bounds_of(panel_gerber) is not None
        )
        if not source_inked and not panel_inked:
            blanks[key] = blank_layer(
                key=key,
                canvas=canvas,
                placed=placed,
                present_in_panel=key in parsed_panel,
                threshold=threshold,
                displacement_threshold_mm=displacement_threshold_mm,
                judged=key in judged,
                placements=len(offsets),
            )
            continue
        jobs.append(LayerJob(
            key=key,
            source_gerber=files_source.get(key) if placed else None,
            panel_gerber=files_panel.get(key),
            placed=placed,
            present_in_panel=key in parsed_panel,
            canvas=canvas,
            offsets=offsets,
            threshold=threshold,
            displacement_threshold_mm=displacement_threshold_mm,
            judged=key in judged,
            cell_px=cell_px,
            display_factor=factor,
            vector_max_kb=vector_max_kb,
            vector_max_elements=vector_max_elements,
            blend_path=image_dir / f"{key}_blend.png",
            windows=windows,
        ))

    pool_workers = workers if workers > 0 else pool_size.default_worker_count(
        total_pixels=canvas.total_pixels, job_count=len(jobs)
    )
    pool_workers = max(1, min(pool_workers, len(jobs))) if jobs else 1
    log(f"  workers      {pool_workers} process(es) for {len(jobs)} inked "
        f"layer(s) ({'verify.workers' if workers > 0 else 'auto-sized'}); "
        f"{len(blanks)} blank layer(s) answered without rendering")
    # Jobs are submitted and gathered in layer order, so the pool decides only
    # WHEN a layer is computed, never what - two runs still report identically.
    if pool_workers > 1:
        with futures.ProcessPoolExecutor(max_workers=pool_workers) as pool:
            outcomes = list(pool.map(compare_layer_job, jobs))
    else:
        outcomes = [compare_layer_job(job) for job in jobs]

    outcome_by_key = {outcome.key: outcome for outcome in outcomes}
    images: dict = {}
    vectors: dict = {}
    drawn_by: dict = {}
    results = []
    for key in every_key:
        measured = (
            blanks[key] if key in blanks else outcome_by_key[key].result
        )
        if key in unjudged_because:
            measured = replace(measured, unjudged_reason=unjudged_because[key])
        results.append(measured)
        if key in blanks:
            continue
        outcome = outcome_by_key[key]
        if outcome.drawn is not None:
            vectors[key] = outcome.drawn
        if outcome.images is not None:
            images[key] = outcome.images
        if outcome.drawn_by is not None:
            drawn_by[key] = outcome.drawn_by

    result = CompareResult(
        canvas=canvas,
        um_per_pixel=um_per_pixel,
        threshold=threshold,
        region_cell_mm=region_cell_mm,
        layers=tuple(results),
        dir_a=source_dir,
        dir_b=panel_dir,
        seconds=time.monotonic() - started,
        metrics={
            "reference": "source board rendered once per placement",
            "placements": len(offsets),
            "replicated_layers": sorted(replicated_keys),
            "display_reduction": factor,
            "window_clipped_to_outline": clipped,
            "clip_margin_mm": clip_margin_mm,
            "displacement_threshold_mm": displacement_threshold_mm,
            "instance_windows": len(windows),
            "instance_window_margin_mm": clip_margin_mm if windows else None,
        },
    )
    for measured in results:
        if measured.carrier_leaked:
            log(carrier.leak_warning(measured.layer, measured.carrier))

    copies = f"{len(offsets)} cop{'y' if len(offsets) == 1 else 'ies'}"
    carried = result.carrier_layers
    run = DiffRun(
        name=f"{title} - the panel against the board it was built from",
        description=(
            f"The panel should be {copies} of the board, each in the place the "
            f"grid puts it. To check that, the board's own fabrication artwork "
            f"is drawn once for every position and compared against the "
            f"artwork the panel actually contains. Nothing in this check "
            f"consults the panel builder's own arithmetic, so a mistake in it "
            f"cannot hide itself."
            + (
                f" The panel also carries "
                f"{result.carrier_ink_area_mm2:.1f} square millimetres of "
                f"carrier artwork across {len(carried)} layer(s) - rails, "
                f"holes and the panel's own edge, which no board can account "
                f"for. That is measured, named and shown, and it is never "
                f"allowed to decide anything."
                if carried else ""
            )
        ),
        result=result,
        images=images,
        vectors=vectors,
        label_a="expected",
        label_b="built panel",
    )
    page = build_diff_page(
        [run],
        page_path,
        # The board's own layer table drives the panel, so the page lists
        # layers in exactly the order KiCad does - but only the ones this
        # comparison actually covered.
        layers=compared_specs(result, layers),
        status_by_layer=status_marks(result, layers),
        counts_by_layer=differing_counts(result, layers),
        extra_css=vector.CSS,
        title=f"Panel check - {title}",
        lede=(
            f"Both the expected panel and the built one were drawn at "
            f"{um_per_pixel:g} micrometres per pixel over the board area "
            f"({canvas.bounds.width_mm:.1f} x {canvas.bounds.height_mm:.1f} mm)"
            + (f", taken from the board outline plus {clip_margin_mm:g} mm - "
               f"fabrication notes parked away from the board are not part of "
               f"what gets made, so they are outside the check. "
               if clipped else
               ". The outline could not be found on one side, so the whole "
               "drawing area is included and this is wider than the board. ")
            + (
                f"Each board is judged over its own area plus "
                f"{clip_margin_mm:g} mm around it. What the panel adds outside "
                f"those areas is carrier - the rails, the tooling holes, the "
                f"panel's own edge - and it is measured, named and shown but "
                f"never judged, because the board it was built from cannot "
                f"account for it. Anything the board should have and the panel "
                f"has not is judged wherever it lands. "
                if windows else
                "The board outline could not be placed, so nothing is scoped "
                "to the boards and any carrier the panel carries reads as a "
                "difference. "
            )
            + f"What decides a layer is HOW FAR its artwork moved at the worst "
            f"single place, allowed up to "
            f"{displacement_threshold_mm * 1000:g} micrometres. Counts of "
            f"differing pixels are given for scale, not as the answer: they "
            f"grow simply with how long an edge is, so a hair-thin edge effect "
            f"traced right around an outline can add up to more of them than a "
            f"genuinely shifted track. Layers that do not become part of the "
            f"bare board - fabrication and documentation - are drawn and "
            f"measured, and shown for information only. "
            f"Pictures on this page are shown at one pixel in {factor}, kept "
            f"so that even a single differing pixel still appears; the figures "
            f"come from the full-size drawings. "
            f"This page reports what it found and stops nothing. "
            f"{raster.WORKAROUND}"
        ),
    )
    return PanelDiff(
        result=result,
        page=page,
        canvas=canvas,
        offsets=offsets,
        seconds=time.monotonic() - started,
    )
