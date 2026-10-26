"""Entry point for ``python -m eedesignpanels.review <config.json>``.

Renders the panel a build already produced, **beside the source board it came
from**, and writes the page the user opens. One layer panel drives both
viewports, so a layer is switched on in the source and the panel at the same
moment and the two can be read against each other.

It also produces the second artifact of every review cycle: the gerber diff
page, comparing the built panel against the source board rendered at every
placement the config asks for. That comparison reports and never blocks: when
it cannot run it says so loudly and the review still lands.

It **never builds**. ``python -m eedesignpanels <config>`` writes
``panel/panel.kicad_pcb``; this reads it. Keeping the two apart means a review
can be regenerated without touching a design file, and a build never depends on
a KiCad install being present.

Stdout contract: the **last** line is the absolute path of
the review page, and the diff page - when one was produced - is on the line
before it, prefixed ``DIFF_REVIEW: ``. A caller opens the last line without
parsing anything else.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from eedesigncore import Board

from eedesignpanels import __version__
from eedesignpanels.checks import compare_extents, svg_extent_mm
from eedesignpanels.config import ConfigError, PanelConfig, load_config
from eedesignpanels.review import holes, page, render, theme
from eedesignpanels.stages import panel_scores, panelize
from eedesignpanels.stages.step_and_repeat import OUTLINE_LAYER, panel_geometry

SCRATCH_ENV = "EEDESIGNPANELS_SCRATCH"
# The KiCad plugin folder inside a source checkout, used only to find its
# scratch/ folder when the package is running from a source checkout.
PLUGIN_PROJECT = "kicad_plugin"
DIFF_PAGE_PREFIX = "DIFF_REVIEW: "


def scratch_root(config: PanelConfig) -> Path:
    """Where review folders are written, in order of preference.

    1. ``EEDESIGNPANELS_SCRATCH`` - an explicit answer always wins.
    2. The plugin project's ``scratch/`` when this package is imported from the
       repository checkout, which is the development loop.
    3. ``<panel folder>/review`` - true for an installed wheel with no
       repository anywhere, and still inside the folder the builder owns.
    """
    override = os.environ.get(SCRATCH_ENV)
    if override:
        return Path(override)

    # <checkout>/src/eedesignpanels/review/__main__.py
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:
        repo = parents[3]
        project = repo / PLUGIN_PROJECT
        if (project / "metadata.json").is_file():
            return project / "scratch"

    return config.panel_dir / "review"


def geometry_of(config: PanelConfig, source: Board):
    """The stage's own grid arithmetic, recomputed read-only for the review.

    Returns ``None`` for stages that do not step and repeat, which have no
    grid and therefore no reference panel to compose.
    """
    if config.stage != "build_panel":
        return None
    return panel_geometry(
        source,
        config.grid.columns,
        config.grid.rows,
        config.grid.gap_across_mm,
        # A layer the stage was told not to replicate must not be compared as
        # if it had been, or the whole layer reads as a difference.
        config.exclude_layers,
        config.grid.gap_down_mm,
    )


def expected_extents(config: PanelConfig, geometry) -> tuple:
    """How big the built panel should measure, outline and stroked.

    The grid of instances when the build stops there; the BLANK the instances
    were cut out of once the config panelizes, because the panel's own outline
    is then the blank rather than the boards. One derivation, shared with the
    stage (`panelize.blank_extent_mm`), so the prediction and the panel cannot
    be grown from two different rules.
    """
    outline = geometry.outline_extent_mm
    inked = geometry.inked_extent_mm
    if config.panel is None:
        return outline, inked
    stroke = (inked[0] - outline[0], inked[1] - outline[1])
    # THE RAIL SCORE SETBACK GROWS THE PANEL, so a
    # prediction that ignored it would disagree with every panel that uses one.
    # Read through `Settings.governed`, the one place a dependant whose rail
    # score is off is taken to zero.
    scores = config.panel.score_settings
    grown = panelize.blank_extent_mm(
        outline, config.rail_widths,
        score_setbacks=(
            panel_scores.Settings.of(scores).governed.setbacks
            if scores is not None else None
        ),
    )
    return grown, (grown[0] + stroke[0], grown[1] + stroke[1])


def render_view(board_path: Path, layers, out_dir: Path, tag: str,
                theme_name: str, log=print):
    """Prepare and plot one board. One kicad-cli call, every layer."""
    plot_board = render.prep_for_render(board_path, out_dir / f"render_{tag}")
    export = render.export_svgs(
        plot_board, layers, out_dir / f"svg_{tag}", theme=theme_name
    )
    log(f"  {tag:<12} {len(export.plotted)} svg in "
        f"{export.seconds_to_done:.2f}s, mapped {len(export.svg_by_layer)}")
    if export.unmapped:
        log(f"  UNMAPPED ({tag}): {', '.join(export.unmapped)}")
    return export


def hole_marks(layers, export, active, out_dir: Path, tag: str, log=print):
    """This board's drill marks, lifted out of its own copper exports.

    Returns the SVG path to stack, or ``None``, and whatever the page has to
    say about it. A board with no drilled holes yields neither, which is not a
    failure and gets no sentence.

    Painted in the editor's hole colours, taken from the active theme -
    editor-look over plot-look (see :mod:`.holes`).
    """
    path, marks = holes.write_beside(
        holes.copper_sources(layers, export.svg_by_layer),
        {
            layer.canonical_name: active.layer_color(layer.canonical_name)
            for layer in layers
        },
        out_dir / f"svg_{tag}" / "holes.svg",
        palette=holes.editor_palette(active),
    )
    if marks.notice:
        log(f"  NOT SHOWN    {marks.notice}")
    if path is not None:
        log(f"  holes ({tag}) {marks.elements} drill marks from "
            f"{', '.join(marks.sources)}")
    return path, marks.notice


def dimension_layers(config: PanelConfig) -> tuple:
    """The layer the panel's dimensions are on, or nothing when there are none.

    Read from the config rather than from the stage's default, so a panel that
    put its dimensions somewhere else still opens showing them.
    """
    if config.panel is None:
        return ()
    settings = config.panel.dimension_settings
    return () if settings is None else (settings.layer,)


def panel_owned_layers(config: PanelConfig) -> frozenset:
    """Which layers the comparison measures and reports but never judges.

    `panelize.PANEL_OWNED_LAYERS` plus **the dimensions layer**, which is
    panel-owned for the same reason and is not in that constant because the
    config moves it (`panel.dimensions.layer`). The panel states its own size;
    the source board stamped at every placement states nothing, so every
    dimension stroke would read as a difference the panel invented. That is a
    true statement about the layer and a false statement about the panel.

    This matters even though a numbered user layer is not
    :func:`review.layerpanel.is_physical_layer` and so is already unjudged
    under the default `verify.compared_layers`: `"all"` judges everything the
    physical rule would have spared, and `panel_owned` is subtracted from the
    judged set afterwards (`verify.panel_diff`). Naming the layer here is what
    makes the fence hold whatever the config compares - and it puts the REASON
    in the report instead of silence.
    """
    if config.panel is None:
        return frozenset()
    return frozenset(panelize.PANEL_OWNED_LAYERS) | frozenset(
        dimension_layers(config)
    )


def run_comparison(config: PanelConfig, geometry, layers, out_dir: Path,
                   project: str, log=print):
    """The gerber comparison, if the stage defines one. Returns a page path.

    Imported here rather than at module scope: the comparison backend is the
    ``verify`` extra, a development-only dependency, and a review
    render must still work without it.
    """
    if not config.verify.enabled:
        log("  comparison   disabled in the config (verify.enabled = false)")
        return None
    if geometry is None:
        log(f"  comparison   no comparison is defined for stage "
            f"{config.stage!r}; the byte check in the build covers it")
        return None

    from eedesignpanels.verify.panel_diff import compare_panel

    outcome = compare_panel(
        source_board=config.source_board_path,
        panel_board=config.output_board_path,
        layers=layers,
        # Which layers the stage replicates is a property of the BOARD now that
        # it injects whole copies, so the stage answers it per board and the
        # answer is narrowed to the layers actually being compared.
        replicated=geometry.replicated_layers.intersection(
            spec.canonical_name for spec in layers
        ),
        placements=geometry.placements,
        work_dir=out_dir / "verify",
        page_path=out_dir / f"{project}_GERBER_DIFF_REVIEW.html",
        title=project,
        um_per_pixel=config.verify.um_per_pixel,
        threshold=config.verify.threshold,
        region_cell_mm=config.verify.region_cell_mm,
        max_canvas_mpixels=config.verify.max_canvas_mpixels,
        display_max_px=config.verify.display_max_px,
        outline_layer=OUTLINE_LAYER,
        clip_margin_mm=config.verify.clip_margin_mm,
        displacement_threshold_mm=config.verify.displacement_threshold_mm,
        compared_layers=config.verify.compared_layers,
        # The panel derives these itself, so there is no per-instance reference
        # to hold them to - they are measured and reported, never judged.
        # A config that does not panelize still replicates its
        # instances' outlines, and those ARE judged, as they always were.
        panel_owned=panel_owned_layers(config),
        vector_max_kb=config.verify.vector_max_kb,
        vector_max_elements=config.verify.vector_max_elements,
        workers=config.verify.workers,
        log=log,
    )
    if outcome.skipped is not None:
        log(outcome.skipped.banner())
        return None

    from eedesignpanels.verify.compare import format_summary

    log(format_summary(outcome.result))
    log(f"  diff page    {outcome.page.stat().st_size / 1e6:.2f} MB in "
        f"{outcome.seconds:.1f}s")
    return outcome.page


def main(argv: list = None) -> int:
    """Render source and panel, write both artifacts. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="eedesignpanels.review",
        description="Render the built panel beside its source and review both.",
    )
    parser.add_argument("config", help="path to panel/panel.json")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    board_path = config.output_board_path
    if not board_path.is_file():
        print(
            f"no panel to review at {board_path} - run "
            f"'python -m eedesignpanels {config.config_path}' first",
            file=sys.stderr,
        )
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = scratch_root(config) / f"{stamp}_review_{config.stage}"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_path = config.source_board_path
    project = source_path.stem
    print(f"eedesignpanels.review {__version__}")
    print(f"  config       {config.config_path}")
    print(f"  source board {source_path}")
    print(f"  panel board  {board_path}")
    print(f"  artifact     {out_dir}")

    # Provenance: the exact config this artifact was rendered from.
    shutil.copyfile(config.config_path, out_dir / config.config_path.name)

    # The layer table comes from the source board - it is the design document,
    # and the panel scaffold copies its table verbatim, so one list
    # drives both viewports and the Appearance panel that controls them.
    source = Board.from_file(source_path)
    layers = render.select_layers(render.layers_of(source), config.review.layers)
    active = theme.load_theme()
    print(f"  {len(layers)} layers, theme {active.name}")

    source_export = render_view(source_path, layers, out_dir, "source", active.name)
    panel_export = render_view(board_path, layers, out_dir, "panel", active.name)

    geometry = geometry_of(config, source)
    # Reads on the page, so it is written for a PCB designer: how the panel is
    # laid out and how big it came out, not which code path produced it.
    detail = config.grid.summary
    outline_svg = panel_export.svg_by_layer.get(OUTLINE_LAYER)
    if outline_svg is not None:
        # kicad-cli crops to the board area with --page-size-mode 2, so the
        # SVG's own mm size is KiCad's independent measurement of our panel.
        rendered = svg_extent_mm(outline_svg.read_text(encoding="utf-8"))
        print(f"  rendered {OUTLINE_LAYER} extent "
              f"{rendered[0]:.4f} x {rendered[1]:.4f} mm")
        detail += f" - panel measures {rendered[0]:.2f} x {rendered[1]:.2f} mm"
        if geometry is not None:
            expected_outline, expected_inked = expected_extents(config, geometry)
            outline_check = compare_extents(expected_outline, rendered)
            inked_check = compare_extents(expected_inked, rendered)
            print("checks")
            print(f"  extents vs outline centrelines: {outline_check.summary}")
            print(f"  extents vs stroked outline:     {inked_check.summary}")
            # EITHER reading is a pass. The render crops to the INK, so what
            # kicad-cli reports lands at or beyond the stroked extent rather
            # than at the centreline one, and the two predictions are one
            # stroke apart - measured on the panelized corpus, the crop is
            # 0.0064 mm inside the stroked width and 0.0496 mm outside the
            # stroked height. Insisting on the centreline figure would call a
            # panel wrong for being drawn with a stroke; both numbers are
            # printed above, so nothing is hidden by taking the better one.
            detail += (
                f", expected {expected_outline[0]:.2f} x "
                f"{expected_outline[1]:.2f} mm "
                + ("- agrees" if outline_check.agrees or inked_check.agrees
                   else "- DOES NOT AGREE")
            )

    diff_page = run_comparison(config, geometry, layers, out_dir, project)

    # The holes, stacked above every layer in both viewports. kicad-cli plots
    # them into each copper export and the viewer's board-order stack paints
    # them out, so they are lifted from its own output and re-stacked.
    source_stack = dict(source_export.svg_by_layer)
    panel_stack = dict(panel_export.svg_by_layer)
    source_holes, source_notice = hole_marks(
        layers, source_export, active, out_dir, "source")
    panel_holes, panel_notice = hole_marks(
        layers, panel_export, active, out_dir, "panel")
    page_layers, on_top = list(layers), ()
    if source_holes or panel_holes:
        page_layers.append(holes.holes_layer(layers))
        on_top = (holes.HOLES_LAYER,)
        active = holes.with_swatch(active)
        if source_holes:
            source_stack[holes.HOLES_LAYER] = source_holes
        if panel_holes:
            panel_stack[holes.HOLES_LAYER] = panel_holes
    # The notice leads: a reader who stops after one sentence has to be told
    # that something they expect to see is absent, and why.
    subtitle = detail
    for absent in dict.fromkeys(n for n in (panel_notice, source_notice) if n):
        subtitle = f"{absent}  //  {subtitle}"

    out = page.build_page(
        out_path=out_dir / f"{project}_PANEL_REVIEW.html",
        heading=f"eedesignpanels - {project}",
        subtitle=subtitle,
        layers=page_layers,
        views=[
            page.BoardView(
                title=f"THE BOARD - {source_path.name}",
                svg_by_layer=source_stack,
            ),
            page.BoardView(
                title=f"THE PANEL - {detail}",
                svg_by_layer=panel_stack,
            ),
        ],
        theme=active,
        # THE DIMENSIONS LAYER OPENS OFF. The frozen page follows the live
        # panel tab: the
        # dimensions are in the panel and one click away, and a review opens as
        # the fabrication picture rather than as the drawing.
        start_visible=on_top,
        on_top=on_top,
    )
    print(f"  review page  {out.stat().st_size / 1024:.0f} KB")
    if diff_page is not None:
        print(f"{DIFF_PAGE_PREFIX}{diff_page.resolve()}")
    # Last line, always: the page the caller opens.
    print(out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
