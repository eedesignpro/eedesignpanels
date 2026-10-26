"""Entry point for ``python -m eedesignpanels.prep <config.json>``.

Prepares the SOURCE board the config names: it draws the router's path onto
`User.36` and writes the board back. That is the one place in this project where
a stage modifies the input design, and it is deliberate - the toolpath
belongs to the design, not to the panel, and rides ordinary step-and-repeat into
the panel afterwards.

**`User.38` is the designer's layer and prep never writes it.** The v-scores on
it are DECISIONS: a line on a candidate's blade line accepts that candidate, and
its absence declines it. Prep reads them, validates them against the candidate
set the current shape yields, and honours the ones that still match - a line
that matches none is reported as stale and suppresses nothing.

Prep is idempotent. It owns `User.36` at board level, deleting what is there
before writing what it computed, so running it twice on an unchanged board
leaves byte-identical bytes. The run says how many items it removed.

It then renders the single-board review page - the board edge, the tab stop
lines, the new toolpath and the scores in one viewport - into `scratch/`, the
same way `eedesignpanels.review` does for a panel.

The page also carries the PROPOSAL: every candidate blade line the v-score
proposer found, with its verdict, drawn as an annotation overlay. The accepted
ones are real `User.38` geometry and are plotted like anything else; the ones
nobody accepted exist nowhere in the file, so the page draws them. The review
page may draw annotation, never board data - see
`eedesignpanels.review.overlay`.

Stdout contract, matching the other entries: the **last** line is the absolute
path of the review page.

**Two modes, one computation.** `--preview` works everything out in memory,
renders it from a scratch COPY of the board and writes nothing at all into the
source project - not `User.36`, not the config, not a sidecar. Commit mode is
what this module has always done: the same computation, written to the board.
The split exists for the live preview server (:mod:`eedesignpanels.preview`),
which recomputes on every interaction and must never leave a trace of a view.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board, item_layers

from eedesignpanels import __version__, cuttability
from eedesignpanels.config import ConfigError, PanelConfig, load_config
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import OffsetError

SCRATCH_ENV = "EEDESIGNPANELS_SCRATCH"
# The KiCad plugin folder of a source checkout, used only to find its
# scratch/ folder: <checkout>/kicad_plugin beside src/.
PLUGIN_PROJECT = "kicad_plugin"

# What the review page shows: the edge the cutter follows, the lines that stop
# it, the path itself, and the edges a saw takes instead.
REVIEW_LAYERS = (
    route_toolpath.OUTLINE_LAYER,
    route_toolpath.STOP_LINE_LAYER,
    route_toolpath.TOOLPATH_LAYER,
    route_toolpath.SCORE_LAYER,
)

# The proposal overlay's entry in the layer panel. Not a board layer and never
# plotted - the page draws it (see `eedesignpanels.review.overlay`) - so it takes
# a name no board can collide with, and `build_page` is told it is annotation.
PROPOSAL_LAYER = "Proposals.VScore"
PROPOSAL_LAYER_DISPLAY = "V-score candidates"

# THE FEATURES THE DECLARED CUTTER CANNOT MAKE, ON A LAYER OF THEIR OWN, and
# highlighted red. Annotation, like the candidates: these
# marks are drawn over a shape the board really carries, to say the router will
# not produce it. Its own layer so the red lands on the features and not on the
# whole outline, and so one click switches the marks off.
UNCUTTABLE_LAYER = "Features.Uncuttable"
UNCUTTABLE_LAYER_DISPLAY = "Features the cutter cannot make"

# How far the overlay may disagree with the render before it is dropped instead
# of drawn crooked. kicad-cli sizes the cropped page in whole mils, so it can
# read up to one mil (0.0254 mm) short of the board's own bounding box; anything
# beyond that means the two are not measuring the same thing.
OVERLAY_ALIGN_TOL_MM = 0.03


def hosted_outline_notice(board: Board) -> str:
    """What the PAGE says when part of the outline lives inside a footprint.

    A reader looking at the board edge cannot tell which items are the board's
    own and which the tab footprints brought with them - and the difference
    matters, because those are the break contours the router is meant to stop
    short of. One clause, only when there are any.
    """
    hosted = sum(
        1
        for footprint in board.footprints
        for item in footprint.drawings
        if route_toolpath.OUTLINE_LAYER in item_layers(item)
    )
    if not hosted:
        return ""
    return (
        f"the outline drawn below is the board's own edge together with "
        f"{hosted} break-contour items carried by its tab footprints"
    )


def missing_layer_notice(missing: list, stops_read: int) -> str:
    """What the PAGE says when a layer this stage depends on cannot be plotted.

    A layer absent from the board's table is not plotted (KiCad 10.0, *Plotting
    options*: "Disabled layers will not be plotted"), so it simply does not
    appear in the viewport. A designer looking at a page with no stop lines on
    it cannot tell "this board has no tabs" from "this board cannot draw its
    tabs", and those call for opposite actions. Saying it in the run output is
    not enough - the page is what gets looked at, and it outlives the terminal -
    so the notice travels with the page.

    Returns "" when every layer is declared, which is the ordinary case.
    """
    if not missing:
        return ""

    parts = []
    for name in missing:
        if name == route_toolpath.STOP_LINE_LAYER:
            parts.append(
                f"Tab stop lines are not shown: they exist on {name}, but this "
                f"board's layer table does not declare that layer, so KiCad "
                f"cannot plot it. All {stops_read} were read and honoured, so "
                f"the cutter path below is complete - the gap is the board's "
                f"layer table, not the geometry."
            )
        elif name == route_toolpath.TOOLPATH_LAYER:
            parts.append(
                f"The cutter path was written to {name}, but this board's layer "
                f"table does not declare that layer, so KiCad will not plot it "
                f"here or in any fabrication output. Add {name} to the board's "
                f"layer table."
            )
        elif name == route_toolpath.SCORE_LAYER:
            parts.append(
                f"The v-scores live on {name}, but this board's layer table does "
                f"not declare that layer, so KiCad cannot plot them here or in "
                f"any fabrication output. Add {name} to the board's layer table."
            )
        elif name == route_toolpath.OUTLINE_LAYER:
            parts.append(
                f"The board outline is not shown: {name} is not declared in "
                f"this board's layer table, so KiCad cannot plot it."
            )
        else:
            parts.append(
                f"{name} is not declared in this board's layer table, so it "
                f"cannot be plotted or shown."
            )
    return " ".join(parts)


def score_notice(result) -> str:
    """What the PAGE says about the edges a saw takes instead of the router.

    A scored edge looks like an un-routed one on a plot - the toolpath simply
    is not there - and the difference between "this edge is v-scored" and "the
    router missed this edge" is the whole board. So it is said in words, in the
    designer's terms: how many edges, how long, and how far past the blade's
    line the cut leaves the board edge. The number is the cutter's tangent
    point, not the outer extreme of its round end.

    A junction that did not get the declared overshoot is only worth a sentence
    when the designer can act on it. Where the board's own edge ends
    at the score the shape says so, not the toolpath, and the page says that
    plainly instead of calling it short.

    Returns "" when nothing is scored, which is the ordinary routed board.
    """
    if not result.scores:
        return ""
    spans = " and ".join(f"{score.length_mm:.3f} mm" for score in result.scores)
    edges = "edge" if len(result.scores) == 1 else "edges"
    past = (
        f"{result.route_past_mm:.4f} mm"
        if result.route_past_mm is not None else "an undeclared distance"
    )
    notice = (
        f"{len(result.scores)} {edges} v-scored ({spans}), with the cut leaving "
        f"the board edge {past} past each score"
    )
    # Judged on standing material. A junction where the board's own
    # edge ends at the score gets no overshoot and is normal, so the two are
    # said differently: one is a fact about the shape, the other is a defect.
    standing = [
        junction for junction in result.junctions if not junction.normal
    ]
    tangent = [
        junction for junction in result.junctions
        if junction.normal and not junction.reached
    ]
    if tangent:
        notice += (
            f" - except at {len(tangent)} junction(s), where the board edge "
            f"ends at the score, so the cut reaches the plane and there is "
            f"nothing further to take to depth"
        )
    if standing:
        worst = max(junction.standing_mm for junction in standing)
        notice += (
            f". At {len(standing)} junction(s) the route falls short of the "
            f"score and leaves up to {worst:.4f} mm of board edge standing"
        )
    return notice


def candidate_notice(result, name_for=None) -> str:
    """What the PAGE says about the PROPOSAL - every candidate, not the survivors.

    A page that shows only the accepted scores cannot be reviewed: the reader
    has no way to ask "what about that edge?" and no way to tell a line nobody
    proposed from one that was proposed and declined. So the count is of
    candidate LINES, in three states, and the words keep a choice the DESIGNER
    made apart from a line the TOOL will not propose.

    *name_for* is how one candidate is named in the sentence, given its
    proposal. It defaults to the short id, which is what the frozen review page
    shows; the live preview passes its own `#1`, `#2` numbering, so hash ids
    stay off the page.

    Returns "" when the proposer found nothing, which is a board with no
    straight edge a blade could reach.
    """
    name_for = name_for or (lambda entry: entry.short_id)
    if not result.proposals:
        return ""
    accepted = [entry for entry in result.proposals if entry.accepted]
    offered = [
        entry for entry in result.proposals
        if not entry.accepted and entry.proposable
    ]
    refused = [entry for entry in result.proposals if not entry.proposable]
    turned_down = offered + refused
    lines = "line" if len(result.proposals) == 1 else "lines"
    notice = (
        f"{len(result.proposals)} candidate blade {lines} found, "
        f"{len(accepted)} scored by the board's own "
        f"{route_toolpath.SCORE_LAYER} lines, {len(offered)} proposed and not "
        f"accepted, {len(refused)} rejected"
    )
    if turned_down:
        # The SHORT reason here. The whole finding, with the name of the thing
        # in the way, is in the run output; a subtitle that carries all of it
        # stops being read.
        listed = "; ".join(
            f"{name_for(entry)} at ({entry.candidate.midpoint[0]:.3f}, "
            f"{entry.candidate.midpoint[1]:.3f}) - "
            f"{entry.summary or entry.reason}"
            for entry in turned_down[:4]
        )
        notice += f" ({listed}"
        if len(turned_down) > 4:
            notice += f"; and {len(turned_down) - 4} more"
        notice += ")"
    notice += (
        f" - every candidate is drawn on the '{PROPOSAL_LAYER_DISPLAY}' "
        f"overlay, dashed: green scored, grey proposed, amber rejected"
    )
    return notice


def stale_notice(result) -> str:
    """What the PAGE says about a score line that matches no current candidate.

    The decision model's expected failure, and the one a reader must not have to
    hunt for: a line on `User.38` that the outline has moved out from under
    still LOOKS like a score on the plot, and it is not one - the router cuts
    that edge. Named by position, because that is all about it that still means
    anything.

    Returns "" when every score line still lies on a candidate blade line.
    """
    if not result.stale_scores:
        return ""
    listed = "; ".join(
        f"({entry.line.start[0]:.3f}, {entry.line.start[1]:.3f}) to "
        f"({entry.line.end[0]:.3f}, {entry.line.end[1]:.3f})"
        for entry in result.stale_scores[:4]
    )
    if len(result.stale_scores) > 4:
        listed += f"; and {len(result.stale_scores) - 4} more"
    lines = "line" if len(result.stale_scores) == 1 else "lines"
    verb = "lies" if len(result.stale_scores) == 1 else "lie"
    return (
        f"{len(result.stale_scores)} {route_toolpath.SCORE_LAYER} {lines} no "
        f"longer {verb} on a scoreable edge - the outline changed? - so they "
        f"score nothing and the router still cuts those edges ({listed})"
    )


def overlay_origin(board: Board) -> tuple:
    """The board millimetre point kicad-cli puts at the cropped page's corner.

    `--page-size-mode 2` crops to the board's own bounding box, so the export's
    coordinates are board millimetres shifted by that box's minimum corner. It
    is measured off the same outline the stage read - board-level `Edge.Cuts`
    plus the footprints' - with arcs measured exactly, since a fillet's bulge is
    not either of its endpoints. It is the same box a v-score line reaches,
    which is why both come from one function.
    """
    return route_toolpath.outline_bbox(board)


def proposal_overlay(result, board: Board, svg_path: Path,
                     out_path: Path) -> tuple:
    """Draw the proposal overlay beside an exported SVG. Returns `(notice, pad)`.

    A non-empty *notice* is what the PAGE has to say instead: an overlay that
    does not line up with the render is worse than no overlay, so it is dropped
    and the reason travels with the page.

    *pad* is how much air in millimetres the viewport has to leave round the
    board for the captions to be seen. They sit a router diameter and a margin
    OUTSIDE the outline, because a caption never overlaps the board or the
    toolpath, and kicad-cli's cropped page ends at the outline, so
    without it they would be drawn where the viewport clips them.
    """
    from eedesignpanels.review import overlay

    box = overlay.viewbox_of(svg_path)
    bbox = overlay_origin(board)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if (abs(width - box.width) > OVERLAY_ALIGN_TOL_MM
            or abs(height - box.height) > OVERLAY_ALIGN_TOL_MM):
        return (
            f"The v-score candidates are not drawn: the render measures "
            f"{box.width:.4f} x {box.height:.4f} mm while the board outline "
            f"measures {width:.4f} x {height:.4f} mm, so an overlay drawn on it "
            f"would not line up with the plot underneath.",
            0.0,
        )
    clearance = overlay.label_clearance_mm(result.cutter_diameter_mm)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        overlay.proposal_svg(
            result.proposals, (bbox[0], bbox[1]), box, clearance,
            # The frozen page sets its captions along the line they name: the
            # stage's own wording is long, and upright it would run out into the
            # margin instead of lying beside the edge it belongs to.
            along_line=True,
        ),
        encoding="utf-8",
    )
    return "", overlay.label_reach_mm(box.larger, clearance)


def uncuttable_overlay(result, board: Board, svg_path: Path,
                       out_path: Path) -> str:
    """Draw the uncuttable-feature marks beside an exported SVG. `""` when drawn.

    The frozen page's half of the uncuttable-feature report. Same alignment
    contract as
    :func:`proposal_overlay` - an overlay that does not line up with the render
    is worse than no overlay - and the same board bounding box, so the two
    annotation layers sit in one frame.

    WHAT IS MARKED IS ASKED IN ONE PLACE: this drew only the jobs
    that were not made, so a feature that IS cut and has a corner the bit
    cannot reach - which the boundary of nearly every board has - earned a
    layer from `cuttability.marked` and then a page with nothing red on it.
    :func:`eedesignpanels.cuttability.marked` is the one answer, and the live
    preview has always used it.
    """
    from eedesignpanels.review import overlay

    box = overlay.viewbox_of(svg_path)
    bbox = overlay_origin(board)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if (abs(width - box.width) > OVERLAY_ALIGN_TOL_MM
            or abs(height - box.height) > OVERLAY_ALIGN_TOL_MM):
        return (
            f"The features the cutter cannot make are not marked: the render "
            f"measures {box.width:.4f} x {box.height:.4f} mm while the board "
            f"outline measures {width:.4f} x {height:.4f} mm, so a mark drawn "
            f"on it would not line up with the plot underneath."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        overlay.uncuttable_svg(
            cuttability.marked(result.jobs),
            (bbox[0], bbox[1]), box, result.cutter_diameter_mm,
        ),
        encoding="utf-8",
    )
    return ""


def proposal_layer(layers: list):
    """The overlay's row in the layer panel: last in the list, so it draws on top."""
    from eedesignpanels.review.render import LayerSpec

    return LayerSpec(
        ordinal=max((layer.ordinal for layer in layers), default=0) + 1,
        canonical_name=PROPOSAL_LAYER,
        layer_type="annotation",
        display_name=PROPOSAL_LAYER_DISPLAY,
    )


def uncuttable_layer(layers: list):
    """The uncuttable marks' row: last of all, so nothing draws over the red."""
    from eedesignpanels.review.render import LayerSpec

    return LayerSpec(
        ordinal=max((layer.ordinal for layer in layers), default=0) + 1,
        canonical_name=UNCUTTABLE_LAYER,
        layer_type="annotation",
        display_name=UNCUTTABLE_LAYER_DISPLAY,
    )


def hole_marks(hole_sources, export, active, out_path: Path):
    """The board's drill marks, lifted from the copper exports it plotted.

    The copper stack is rendered for this and nothing else - the page shows the
    edge, the stops and the cutter path - so the sources are named separately
    from the layers on show. Returns ``(path or None, marks)``; see
    :func:`eedesignpanels.review.holes.write_beside`.

    Painted in the editor's hole colours from the active theme, the same as the
    panel review page: the editor look, not the plot look.
    """
    from eedesignpanels.review import holes

    return holes.write_beside(
        holes.copper_sources(hole_sources, export.svg_by_layer),
        {
            layer.canonical_name: active.layer_color(layer.canonical_name)
            for layer in hole_sources
        },
        out_path,
        palette=holes.editor_palette(active),
    )


def scratch_root(config: PanelConfig) -> Path:
    """Where review folders are written, in order of preference."""
    override = os.environ.get(SCRATCH_ENV)
    if override:
        return Path(override)

    # <checkout>/src/eedesignpanels/prep.py
    parents = Path(__file__).resolve().parents
    if len(parents) > 2:
        repo = parents[2]
        project = repo / PLUGIN_PROJECT
        if (project / "metadata.json").is_file():
            return project / "scratch"

    return config.panel_dir / "review"


class Computed(NamedTuple):
    """One prep computation, held in memory: nothing has been written.

    `board` is the loaded design with the toolpath already on it - `build` works
    in place - and `source_path` is where it came from, which is the file this
    mode is careful NOT to touch.
    """

    board: Board
    result: object
    source_path: Path


def prep_result(board_path: Path, made, cutter_diameter_mm: float,
                board_sha256: str = "", load_seconds: float = 0.0,
                build_seconds: float = 0.0, write_seconds: float = 0.0,
                scores_untouched: bool = True):
    """A `BuildResult` presented as a `PrepResult`, for a run that wrote nothing.

    The page and the report read a `PrepResult`, and preview mode has one of
    everything they want except the three numbers a write produces. Those are
    named rather than faked: the hash of a board on disk is empty when no board
    was put there.
    """
    return route_toolpath.PrepResult(
        board_path=board_path,
        board_sha256=board_sha256,
        cutter_diameter_mm=cutter_diameter_mm,
        tabs=made.tabs,
        stops=made.stops,
        removed=made.removed,
        outline_items=made.outline_items,
        loop_items=made.loop_items,
        written=len(made.items),
        run_lengths=made.run_lengths,
        bridges=made.bridges,
        tool_max_mm=made.tool_max_mm,
        warnings=made.warnings,
        load_seconds=load_seconds,
        build_seconds=build_seconds,
        write_seconds=write_seconds,
        mouths=made.mouths,
        rout_through=made.rout_through,
        candidates=made.candidates,
        scores=made.scores,
        junctions=made.junctions,
        routed_scores=made.routed_scores,
        route_past_mm=made.route_past_mm,
        proposals=made.proposals,
        score_lines=made.score_lines,
        stale_scores=made.stale_scores,
        scores_untouched=scores_untouched,
        dropped=made.dropped,
        dropped_mm=made.dropped_mm,
        straightened=made.straightened,
        straightened_mm=made.straightened_mm,
        min_tab_mm=made.min_tab_mm,
        joins=made.joins,
        protected_tabs=made.protected_tabs,
        eliminate_slivers=made.eliminate_slivers,
        covering=made.covering,
        standing=made.standing,
        jobs=tuple(made.jobs) + tuple(job for rigid in made.rigid_results for job in rigid.jobs),
        heals=made.heals,
        heal_gap_mm=made.heal_gap_mm,
        pocket_cutouts=made.pocket_cutouts,
        toolpath_layer=made.toolpath_layer,
        rigid_results=made.rigid_results,
    )


def compute(config: PanelConfig, board: Board = None) -> Computed:
    """Everything one prep run works out, with nothing written anywhere.

    The preview half of the split. It loads the board, runs the same `build`
    commit mode runs, and hands back the mutated board object - so the caller
    may render it from a copy - together with the result the page reads.

    *board* lets a caller that ALREADY HOLDS the board skip the load. It is for
    one case and it is a measured one: a preview action writes the board and
    then recomputes, so the file is parsed twice within one click - 210 ms of a
    1.03 s accept on the corpus board. The board handed in must be
    what the source file now says, because everything downstream treats this
    result as a reading of that file. It is MUTATED here, exactly as a loaded
    one would be, so a caller may not write it afterwards.

    Raises the same things :func:`route_toolpath.run` does, for the same
    reasons: a stage that cannot complete says so.
    """
    if config.prep is None:
        raise ValueError(
            "this config has no 'prep' section, so there is no cutter diameter "
            "to prepare with. A cutter diameter is declared, never inferred."
        )

    source_path = config.source_board_path
    started = time.monotonic()
    board = Board.from_file(source_path) if board is None else board
    loaded = time.monotonic()

    before = route_toolpath.score_signature(board)
    from eedesignpanels import rigid_flex
    if rigid_flex.is_rigid_flex(board, config.source_project_path):
        made = rigid_flex.build(board, config)
    else:
        made = route_toolpath.build(board, config.prep.cutter_diameter,
                                    config.prep.vscore, config.min_tab_mm,
                                    heal_gap_mm=config.outline_heal_mm,
                                    pocket_cutouts=config.pocket_cutouts)
    untouched = route_toolpath.score_signature(board) == before
    from eedesignpanels.stages import edge_plating
    if config.edge_plating:
        edge_plating.generate(board, config, result=made)
    built = time.monotonic()

    return Computed(
        board=board,
        result=prep_result(
            source_path, made, config.prep.cutter_diameter,
            load_seconds=loaded - started, build_seconds=built - loaded,
            scores_untouched=untouched,
        ),
        source_path=source_path,
    )


def stage_board(computed: Computed, out_dir: Path) -> Path:
    """Write the computed board to a scratch COPY, under its own name.

    Under its own name so the exported SVG filenames and the page's title still
    match the design; in `out_dir` so the source project never sees it, and
    neither does the `.kicad_prl` sidecar kicad-cli drops beside whatever it
    renders.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    copy_path = out_dir / computed.source_path.name
    if copy_path.resolve() == computed.source_path.resolve():
        raise ValueError(
            f"the preview copy would overwrite the design: "
            f"{computed.source_path}"
        )
    computed.board.to_file(copy_path)
    return copy_path


class PreviewResult(NamedTuple):
    """What preview mode produced. Nothing in the source project changed."""

    result: object
    board_path: Path
    page_path: Path


def preview(config: PanelConfig, out_dir: Path, log=print) -> PreviewResult:
    """Prep the board WITHOUT writing to it, and render the review page."""
    computed = compute(config)
    board_path = stage_board(computed, out_dir / "board")
    page_path = review(board_path, out_dir, computed.result, log=log)
    return PreviewResult(computed.result, board_path, page_path)


def review(board_path: Path, out_dir: Path, result, log=print) -> Path:
    """Render the prepared board and write the single-board review page."""
    from eedesignpanels.review import holes, overlay, page, render, theme

    board = Board.from_file(board_path)
    declared = {layer.canonical_name for layer in board.layers}
    # A layer absent from the board's table is not plotted at all (KiCad 10.0,
    # Plotting options: "Disabled layers will not be plotted"), so the page can
    # only show what the board admits to having. Say which, loudly, rather than
    # producing a page that silently lacks the thing being reviewed.
    missing = [name for name in REVIEW_LAYERS if name not in declared]
    notice = missing_layer_notice(missing, len(result.stops))
    if notice:
        log(f"  NOT SHOWN    {notice}")

    wanted = [name for name in REVIEW_LAYERS if name in declared]
    if not wanted:
        raise ValueError(
            "the board declares none of the layers this review shows: "
            + ", ".join(REVIEW_LAYERS)
        )
    layers = render.select_layers(render.layers_of(board), wanted)
    # The copper stack is plotted too, and it is not shown: it is where KiCad
    # puts the drill marks, and a page about where the cutter goes has to show
    # the holes the tabs are made of. One call, all layers - the marginal cost
    # of a layer is milliseconds, a second invocation is a fifth of a second.
    hole_sources = [
        layer for layer in render.layers_of(board)
        if layer.canonical_name.endswith(".Cu")
    ]
    active = theme.load_theme()
    plot_board = render.prep_for_render(board_path, out_dir / "render")
    export = render.export_svgs(
        plot_board, [*layers, *hole_sources], out_dir / "svg", theme=active.name
    )
    log(f"  render       {len(export.plotted)} svg in "
        f"{export.seconds_to_done:.2f}s, mapped {len(export.svg_by_layer)}")

    # The proposal overlay: every candidate blade line, accepted or not, drawn
    # by the page over the plots. It goes LAST in the layer list so it
    # stacks on top, and it needs a rendered SVG to take its coordinates from.
    svg_by_layer = dict(export.svg_by_layer)
    annotations, overlay_notice, overlay_pad = (), "", 0.0
    if result.proposals and svg_by_layer:
        reference = svg_by_layer[next(iter(svg_by_layer))]
        overlay_path = out_dir / "svg" / "vscore-candidates.svg"
        overlay_notice, overlay_pad = proposal_overlay(
            result, board, reference, overlay_path
        )
        if overlay_notice:
            log(f"  NOT SHOWN    {overlay_notice}")
        else:
            layers = [*layers, proposal_layer(layers)]
            svg_by_layer[PROPOSAL_LAYER] = overlay_path
            annotations = (PROPOSAL_LAYER,)
            # The panel's swatch comes from the theme by canonical name, and no
            # theme has an entry for something the board does not have. The
            # overlay now draws three colours, so the row takes the NEUTRAL one:
            # a swatch is a label, and painting it amber would say the layer
            # itself is a problem.
            active = active._replace(colors={
                **active.colors,
                PROPOSAL_LAYER.lower().replace(".", "_"): overlay.PROPOSED_COLOR,
            })

    # ...and the features the declared cutter cannot make, in red, over the top
    # of them. An empty list draws nothing: an
    # ordinary board never gains a layer for a problem it does not have.
    uncut = cuttability.marked(result.jobs)
    if uncut and svg_by_layer:
        reference = svg_by_layer[next(iter(svg_by_layer))]
        marks_path = out_dir / "svg" / "uncuttable-features.svg"
        marks_notice = uncuttable_overlay(result, board, reference, marks_path)
        if marks_notice:
            log(f"  NOT SHOWN    {marks_notice}")
        else:
            layers = [*layers, uncuttable_layer(layers)]
            svg_by_layer[UNCUTTABLE_LAYER] = marks_path
            annotations = (*annotations, UNCUTTABLE_LAYER)
            active = active._replace(colors={
                **active.colors,
                UNCUTTABLE_LAYER.lower().replace(".", "_"): theme.ALARM_COLOR,
            })

    # The holes, above everything, from kicad-cli's own copper plots.
    on_top = ()
    holes_path, marks = hole_marks(hole_sources, export, active,
                                   out_dir / "svg" / "holes.svg")
    if marks.notice:
        log(f"  NOT SHOWN    {marks.notice}")
    if holes_path is not None:
        layers = [*layers, holes.holes_layer(layers)]
        svg_by_layer[holes.HOLES_LAYER] = holes_path
        on_top = (holes.HOLES_LAYER,)
        active = holes.with_swatch(active)
        log(f"  holes        {marks.elements} drill marks from "
            f"{', '.join(marks.sources)}")
    holes_notice = marks.notice

    subtitle = subtitle_for(result, hosted=hosted_outline_notice(board))
    # The notice leads. A reader who stops after one sentence has to have been
    # told that something they expect to see is absent and why.
    for absent in (holes_notice, overlay_notice, notice):
        if absent:
            subtitle = f"{absent}  //  {subtitle}"

    return page.build_page(
        out_path=out_dir / f"{board_path.stem}_TOOLPATH_REVIEW.html",
        heading=f"Route toolpath - {board_path.stem}",
        subtitle=subtitle,
        layers=layers,
        views=[page.BoardView(
            title=f"Board edge, tab stops and cutter path - {board_path.name}",
            svg_by_layer=svg_by_layer,
        )],
        theme=active,
        annotations=annotations,
        # The title promises three layers, so the page opens showing three.
        # `User.8` is not something the generic "becomes the finished board"
        # rule calls physical, so without this it starts hidden behind a toggle
        # a reader has no reason to go looking for. The proposal overlay opens
        # with them: a proposal nobody sees has not been reviewed. So do the
        # holes: a tab IS its drill holes, and one click hides them again.
        start_visible=(*REVIEW_LAYERS, *annotations, *on_top),
        on_top=on_top,
        # Air for the captions, which sit outside the board the render stops at.
        pad_mm=overlay_pad,
    )


def subtitle_for(result, hosted: str = "", name_for=None) -> str:
    """Everything one prep result says about itself, in the order it matters.

    Shared by the frozen review page and the live preview, so the two say the
    same sentences about the same board. What is NOT here is anything about the
    render - a layer that could not be plotted, an overlay that would not line
    up - because those are facts about a particular picture and each host has
    its own.

    *name_for* names a candidate. The default is the short id the frozen page
    carries; the live preview hands in its `#1`, `#2` numbering.
    """
    ceiling = (
        f"{result.tool_max_mm:.4f} mm" if result.tool_max_mm is not None
        else "not declared"
    )
    # Material first, toolpath second. What holds the part in is the number a
    # designer judges; the cutter is only how it came to be that width.
    widths = {round(bridge.material_mm, 4) for bridge in result.bridges}
    if not result.bridges:
        material = "no tabs hold the board"
    elif len(widths) == 1:
        material = (
            f"{len(result.bridges)} tabs hold the board, each "
            f"{result.narrowest_tab_mm:.4f} mm wide at its thinnest"
        )
    else:
        material = (
            f"{len(result.bridges)} tabs hold the board, the thinnest "
            f"{result.narrowest_tab_mm:.4f} mm wide"
        )
    # Material first, and a scored edge is material: it holds the board in until
    # somebody snaps it, exactly as a tab does.
    clauses = [material]
    scored = score_notice(result)
    if scored:
        clauses.append(scored)
    # ...and the proposal right behind it, because "why is THAT edge not scored"
    # is the next question and the answer is not on any layer.
    candidates = candidate_notice(result, name_for)
    if candidates:
        clauses.append(candidates)
    stale = stale_notice(result)
    jobs = getattr(result, "jobs", ()) or ()
    if len(jobs) > 1:
        cut = sum(1 for job in jobs if job.made)
        clauses.append(
            f"{len(jobs)} closed loops are routed as {len(jobs)} job(s) with "
            f"the one declared cutter, {cut} cut and {len(jobs) - cut} not"
        )
    clauses.append(
        f"cut with a {result.cutter_diameter_mm:.4f} mm router "
        f"bit, the widest these tabs allow being {ceiling} - the drawn path is "
        f"the bit's centreline, {result.cutter_diameter_mm / 2.0:.4f} mm "
        f"outside the board edge - {len(result.stops)} stops, "
        f"{len(result.run_lengths)} cutting runs totalling "
        f"{result.cut_length_mm:.3f} mm"
    )
    # EVERY HEAL IS STATED WHERE A PERSON ALREADY READS. One line per heal -
    # where it is and how far the outline fell
    # short - because the tool's job is to help detect why a bad outline is bad
    # rather than to work around it quietly. A board with no gap says nothing.
    for heal in getattr(result, "heals", ()) or ():
        clauses.append(heal.sentence)
    subtitle = " - ".join(clauses)
    if hosted:
        subtitle += f" - {hosted}"
    if result.warnings:
        subtitle += f" - {len(result.warnings)} note(s) in the run output"
    # A stale decision is a line that LOOKS like a score on the plot and is not
    # one, so it goes ahead of the ordinary sentences and behind the two that
    # say the board is not held together.
    if stale:
        subtitle = f"STALE DECISION - {stale}  //  {subtitle}"
    # A tab the cut runs through does not hold anything, so the page says so
    # before it says anything else. This is the one result a reader must not
    # have to go looking for.
    if result.routed_scores:
        names = ", ".join(hit.label for hit in result.routed_scores)
        subtitle = (
            f"THE CUT RUNS ALONG {len(result.routed_scores)} SCORED EDGE(S) - "
            f"{names} - so that edge is being machined away instead of scored"
            f"  //  {subtitle}"
        )
    if result.rout_through:
        names = ", ".join(hit.label for hit in result.rout_through)
        subtitle = (
            f"THE CUT RUNS THROUGH {len(result.rout_through)} TAB(S) - {names} - "
            f"so the board is not held by them  //  {subtitle}"
        )
    # A FEATURE THE CUTTER CANNOT MAKE GOES FIRST OF ALL. It is the one thing
    # on this page that says the board leaves the router a different shape from
    # the one that was drawn.
    uncut = getattr(result, "uncuttable", ())
    if uncut:
        names = ", ".join(job.label for job in uncut)
        subtitle = (
            f"{len(uncut)} FEATURE(S) CANNOT BE CUT with the declared "
            f"{result.cutter_diameter_mm:.4f} mm cutter - {names} - so the "
            f"router does not make them  //  {subtitle}"
        )
    return subtitle


_VERDICT_TAG = {
    route_toolpath.VERDICT_SCORED: "SCORED    ",
    route_toolpath.VERDICT_PROPOSED: "proposed  ",
    route_toolpath.VERDICT_REJECTED: "rejected  ",
}


def _report_jobs(result) -> None:
    """Every closed loop as a routing job: what was cut, and what was not.

    The report says what was cut and what was not. The boundary is listed with
    the rest rather than assumed, because "how many jobs" is the number a
    reader wants first, and a count that quietly omits one is the
    kind of report that gets believed.
    """
    jobs = getattr(result, "jobs", ()) or ()
    if not jobs:
        return
    made = [job for job in jobs if job.made]
    print(f"  routing jobs {len(jobs)} closed loop(s) on "
          f"{route_toolpath.OUTLINE_LAYER}, one declared "
          f"{result.cutter_diameter_mm:.4f} mm cutter: {len(made)} cut, "
          f"{len(jobs) - len(made)} left uncut")
    for job in jobs:
        rides = "inside it" if job.rides_inside else "outside it"
        verdict = "CUT" if job.made else "NOT CUT"
        named = "" if job.is_boundary else job.kind
        print(f"    {job.label:20s} {named:22s} tool rides {rides:10s} "
              f"{verdict:7s} {job.where}")
        if job.path:
            print(f"      path       {job.path_length_mm:.4f} mm of centreline, "
                  f"{len(job.path)} item(s)")
        if job.reason:
            print(f"      reason     {job.reason}")
        # WHICH CUTOUTS WERE POCKETED AND WHICH WERE LEFT, EITHER WAY. The line
        # is on the job itself, where a reader is
        # already looking at that cutout's size.
        if job.pocketed:
            print(f"      pocketed   {len(job.pocket)} pass(es) inside the "
                  f"wall clear the middle, so no loose piece is left")
        if job.pocket_note:
            print(f"      not pocketed  {job.pocket_note}")
        if job.leaves_slug:
            print(f"      loose piece {job.slug_mm[0]:.4f} by "
                  f"{job.slug_mm[1]:.4f} mm left in the middle, attached to "
                  f"nothing - pocketing is switched off")
        if job.rounded:
            first = job.rounded[0]
            print(f"      rounded    {len(job.rounded)} inside corner(s) the "
                  f"cutter cannot reach, first at ({first.at[0]:.4f}, "
                  f"{first.at[1]:.4f}) mm, each left as a "
                  f"{first.radius_mm:.4f} mm fillet")
        for run in job.unreachable:
            print(f"      unreached  {run.span_mm:.4f} mm of edge at "
                  f"({run.at[0]:.4f}, {run.at[1]:.4f}) mm, left as a "
                  f"{run.fillet_mm:.4f} mm fillet")


def _report_decisions(result) -> None:
    """What the designer drew on `User.38`, and whether it still means anything.

    The decisions come FIRST, before the candidate table, because they are the
    input: the candidate set is derived from the shape and this is the only
    thing about the v-scores a human chose.
    """
    if not result.score_lines and not result.candidates:
        return
    print(f"  decisions    {len(result.score_lines)} board-level "
          f"{route_toolpath.SCORE_LAYER} line(s) read (designer-owned; prep "
          f"never writes them): {len(result.scores)} validated, "
          f"{len(result.stale_scores)} on no candidate")
    for line in result.score_lines:
        print(f"    ({line.start[0]:.4f}, {line.start[1]:.4f}) -> "
              f"({line.end[0]:.4f}, {line.end[1]:.4f})  "
              f"{line.length_mm:.4f} mm  {line.id or '(no uuid)'}")
    for entry in result.stale_scores:
        print(f"    STALE ({entry.line.start[0]:.4f}, "
              f"{entry.line.start[1]:.4f}) -> ({entry.line.end[0]:.4f}, "
              f"{entry.line.end[1]:.4f}): {entry.reason}")


def _report_scores(result) -> None:
    """Every candidate LINE a saw could take, what became of it, and why.

    One row per candidate, not per span: the blade cuts a line, so that is the
    thing a designer counts, and the spans on it are detail underneath. Every
    candidate is printed, in all three states. "Which edges could have been
    v-scored, and why was this one not?" is the question a designer asks of a
    panel, and an answer that only lists what was accepted cannot be checked.
    """
    if not result.candidates:
        return
    accepted = sum(1 for entry in result.proposals if entry.accepted)
    offered = sum(
        1 for entry in result.proposals
        if not entry.accepted and entry.proposable
    )
    refused = sum(1 for entry in result.proposals if not entry.proposable)
    print(f"  candidates   {len(result.candidates)} blade line(s): {accepted} "
          f"scored, {offered} proposed and not accepted, {refused} rejected")
    for entry in result.proposals:
        candidate = entry.candidate
        verdict = _VERDICT_TAG.get(entry.verdict, f"{entry.verdict:<10}")
        spans = "span" if len(candidate.spans) == 1 else "spans"
        print(f"    {verdict} {entry.short_id}  "
              f"({candidate.start[0]:.4f}, {candidate.start[1]:.4f}) -> "
              f"({candidate.end[0]:.4f}, {candidate.end[1]:.4f})  "
              f"{len(candidate.spans)} {spans}, {candidate.length_mm:.4f} mm of "
              f"edge over a {candidate.extent_mm:.4f} mm line, straight to "
              f"{candidate.straightness_mm * 1000.0:.1f} um")
        if len(candidate.spans) > 1:
            for index, span in enumerate(candidate.spans):
                print(f"        span {index + 1}  "
                      f"({span.start[0]:.4f}, {span.start[1]:.4f}) -> "
                      f"({span.end[0]:.4f}, {span.end[1]:.4f})  "
                      f"{span.length_mm:.4f} mm")
        if entry.accepted:
            # Which score this is, found on the BLADE LINE and never by the id
            # of the outline item behind it: a rectangle outline
            # gives all four of its edges one id, so an id lookup reported one
            # edge's dropped material four times.
            score = route_toolpath.score_on(candidate, result.scores)
            print(f"        {score.dropped} offset item(s) / "
                  f"{score.dropped_mm:.4f} mm dropped from the toolpath")
        else:
            print(f"        {entry.verdict}: {entry.reason}")
    for junction in result.junctions:
        verdict = "normal" if junction.normal else "WARN"
        print(f"    junction ({junction.at[0]:.4f}, {junction.at[1]:.4f}) "
              f"{junction.action} by {junction.change_mm:.4f} mm; "
              f"{junction.standing_mm:.4f} mm standing, cut leaves the board "
              f"edge {junction.past_mm:.4f} mm past the plane "
              f"({junction.wanted_mm:.4f} mm asked)")
        print(f"      {verdict}: {junction.reason}")


def _report(result) -> None:
    """Everything one prep run measured, in the order it gets doubted."""
    radius = result.cutter_diameter_mm / 2.0
    print(f"  board        {result.board_path}")
    print(f"  cutter       {result.cutter_diameter_mm:.4f} mm "
          f"(offset {radius:.4f} mm)")
    print(f"  outline      {result.outline_items} items -> "
          f"{result.loop_items} offset items")
    # THE HEAL IS DECLARED AND EVERY USE OF IT IS STATED. The setting is
    # printed whether or not it was needed, so a
    # reader can see what this run would have closed; each gap it did close is
    # named under it by coordinate and distance.
    heals = getattr(result, "heals", ()) or ()
    gap_mm = getattr(result, "heal_gap_mm", 0.0)
    print(f"  heal         largest gap closed {gap_mm:.6f} mm "
          f"({gap_mm * 1000.0:.3f} um), {len(heals)} gap(s) closed on this "
          f"outline")
    for heal in heals:
        print(f"    {heal.sentence}")
    if result.mouths:
        leftovers = sum(mouth.leftovers for mouth in result.mouths)
        bridged = sum(1 for mouth in result.mouths if mouth.bridged)
        print(f"  mouths       {len(result.mouths)} declared, {bridged} bridged "
              f"straight, {leftovers} break-contour item(s) attributed to a tab")
        for mouth in result.mouths:
            how = (
                f"{mouth.width_mm:.4f} mm chord replaces {mouth.leftovers} "
                f"item(s), {mouth.leftover_mm:.4f} mm of contour"
                if mouth.bridged
                else f"{mouth.width_mm:.4f} mm of ideal edge already, "
                     f"nothing to bridge"
            )
            print(f"    {mouth.label}: {how}")
    print(f"  removed      {result.removed} existing "
          f"{route_toolpath.TOOLPATH_LAYER} item(s)")
    _report_jobs(result)
    _report_decisions(result)
    _report_scores(result)
    print(f"  tabs         {len(result.tabs)}")
    for tab in result.tabs:
        allowed = (
            f"{tab.tool_max_mm:.4f} mm" if tab.tool_max_mm is not None
            else "not declared"
        )
        print(f"    {tab.label}  allows {allowed}  "
              f"{len(tab.stop_lines)} stop lines")
    if result.tool_max_mm is not None:
        verdict = "OK" if result.cutter_diameter_mm <= result.tool_max_mm else "EXCEEDS"
        print(f"  tool ceiling {result.tool_max_mm:.4f} mm -> configured "
              f"{result.cutter_diameter_mm:.4f} mm {verdict}")
    expected = sum(len(tab.stop_lines) for tab in result.tabs)
    print(f"  stops found  {len(result.stops)} of {expected}")
    for stop in result.stops:
        print(f"    {stop.label} stop {stop.stop + 1}: centre crosses "
              f"({stop.point[0]:.4f}, {stop.point[1]:.4f}) on a "
              f"{stop.hosted_on} at {stop.angle_deg:.2f} deg; edge touches "
              f"{stop.back_mm:.4f} / {stop.forward_mm:.4f} mm back and forward")
        kinds = "/".join(case[0] for case in stop.touch_case)
        margin = min(case[2] for case in stop.touch_case)
        print(f"      touch case {kinds}, nearest point {margin * 1000:.1f} um "
              f"from the stop line's tip")
    for bridge in result.bridges:
        agree = "matches the stop spacing" if bridge.agrees else "DISAGREES with"
        print(f"  tab {bridge.tab + 1}        {bridge.material_mm:.4f} mm of "
              f"material at the thinnest point ({agree} "
              f"{bridge.stop_spacing_mm:.4f} mm); toolpath centreline gap "
              f"{bridge.centreline_mm:.4f} mm")
    print(f"  runs         {len(result.run_lengths)} totalling "
          f"{result.cut_length_mm:.4f} mm, {result.written} items written")
    enforced = "eliminated" if result.eliminate_slivers else "LEFT STANDING"
    print(f"  min tab      {result.min_tab_mm:.4f} mm, slivers {enforced}; "
          f"{len(result.joins)} run-end sliver(s), {len(result.covering)} "
          f"covering pass(es), {len(result.protected_tabs)} declared tab(s) "
          f"narrower than it")
    for join in result.joins:
        print(f"    run ends   {join.material_mm:.4f} mm of material at "
              f"({join.at[0]:.4f}, {join.at[1]:.4f}) mm; centreline gap "
              f"{join.gap_mm:.4f} mm joined by a {join.length_mm:.4f} mm cut")
    for join in result.covering:
        print(f"    covering   {join.material_mm:.4f} mm of material at "
              f"({join.at[0]:.4f}, {join.at[1]:.4f}) mm; a "
              f"{join.crossing.length:.4f} mm pass of a "
              f"{join.radius_mm * 2.0:.4f} mm bit, on "
              f"{route_toolpath.TOOLPATH_LAYER}")
    for ribbon in result.standing:
        print(f"    standing   {ribbon.material_mm:.4f} mm of material runs "
              f"{ribbon.length_mm:.3f} mm at ({ribbon.middle[0]:.4f}, "
              f"{ribbon.middle[1]:.4f}) mm and is NOT routed off")
    if result.dropped or result.straightened:
        print(f"  degenerate   {result.dropped} splinter(s) under "
              f"{route_toolpath.MIN_ITEM_MM * 1000:.0f} um dropped and welded "
              f"over, {result.dropped_mm * 1000:.2f} um in all; "
              f"{result.straightened} arc(s) flatter than "
              f"{route_toolpath.MIN_SAGITTA_MM * 1000:.0f} um written as lines, "
              f"{result.straightened_mm * 1000:.2f} um of bow")
    for warning in result.warnings:
        print(f"  WARNING      {warning}")


def _preview_run(config: PanelConfig) -> int:
    """`--preview`: compute, render, review, and leave the project untouched."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = scratch_root(config) / f"{stamp}_prep_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.config_path, out_dir / config.config_path.name)

    try:
        made = preview(config, out_dir)
    except (ValueError, OffsetError) as exc:
        print(f"prep cannot run: {exc}", file=sys.stderr)
        return 1

    _report(made.result)
    print("checks")
    print(f"  {'PASS' if made.result.widths_agree else 'FAIL'}  every tab's "
          f"width equals its own stop-line spacing")
    print(f"  {'PASS' if made.result.guard_clear else 'FAIL'}  the finished cut "
          f"runs through no tab")
    print(f"  {'PASS' if made.result.min_tab_clear else 'WARN'}  every declared "
          f"tab is wider than the {made.result.min_tab_mm:.4f} mm minimum tab")
    print(f"  {'PASS' if made.result.features_cuttable else 'WARN'}  every "
          f"feature on this board can be cut with the declared cutter")
    print(f"  {'PASS' if made.result.scores_untouched else 'FAIL'}  prep wrote "
          f"no {route_toolpath.SCORE_LAYER}")
    print(f"  artifact     {out_dir}")
    print(f"  preview      board copy {made.board_path}, source untouched")
    print(f"  review page  {made.page_path.stat().st_size / 1024:.0f} KB")
    print(made.page_path.resolve())
    return 0 if made.result.scores_untouched else 1


def main(argv: list = None) -> int:
    """Prepare the source board and review it. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="eedesignpanels.prep",
        description="Draw the router's toolpath onto the source board.",
    )
    parser.add_argument("config", help="path to panel/panel.json")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="work everything out and render it without writing to the board",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if config.prep is None:
        print(
            f"config error: {config.config_path} has no 'prep' section, so "
            f"there is no cutter diameter to prepare with. A cutter diameter "
            f"is declared, never inferred.",
            file=sys.stderr,
        )
        return 2

    print(f"eedesignpanels.prep {__version__}")
    print(f"  config       {config.config_path}")
    print(f"  mode         {'preview (writes nothing)' if args.preview else 'commit'}")

    if args.preview:
        return _preview_run(config)

    try:
        first = route_toolpath.run(config)
    except (ValueError, OffsetError) as exc:
        # A stage that cannot complete says so and writes nothing.
        print(f"prep cannot run: {exc}", file=sys.stderr)
        return 1

    _report(first)
    print(f"  timing       load {first.load_seconds:.2f}s, "
          f"build {first.build_seconds:.2f}s, write {first.write_seconds:.2f}s")

    # Prep owns its layer, so preparing an already-prepared board must land on
    # the same bytes. That is the whole idempotence claim, and it costs one run.
    second = route_toolpath.run(config)
    reproducible = first.board_sha256 == second.board_sha256
    print("checks")
    print(f"  {'PASS' if first.widths_agree else 'FAIL'}  every tab's width "
          f"equals its own stop-line spacing (cut faces land on the stop lines)")
    print(f"  {'PASS' if first.guard_clear else 'FAIL'}  the finished cut runs "
          f"through no tab (swept cut clears every staple base)")
    # Warn, never fail: a declared tab wins over the setting, and the report is
    # how the disagreement reaches the designer.
    print(f"  {'PASS' if first.min_tab_clear else 'WARN'}  every declared tab is "
          f"wider than the {first.min_tab_mm:.4f} mm minimum tab "
          f"({len(first.protected_tabs)} narrower)")
    # The decision model's own invariant: the designer's layer came back exactly
    # as it was read. Measured on the board object, not asserted in a comment.
    print(f"  {'PASS' if first.features_cuttable else 'WARN'}  every feature "
          f"on this board can be cut with the declared cutter "
          f"({len(first.uncuttable)} cannot)")
    # WHICH CUTOUTS WERE POCKETED AND WHICH WERE LEFT. Both counts on one line,
    # next to the setting that decided
    # them, because "0 pocketed" only reads as a decision beside the switch.
    pocketed = sum(1 for job in first.jobs if job.pocketed)
    left = sum(1 for job in first.jobs if job.leaves_slug)
    if pocketed or left:
        print(f"  cutouts      pocketing "
              f"{'ON' if first.pocket_cutouts else 'OFF'}; {pocketed} pocketed "
              f"out, {left} left with a loose piece in the middle")
    # ...and every inside corner the bit cannot reach, which is RED: a fab
    # receives a rounded corner where a sharp
    # one was drawn, so it is a change to the part rather than a note.
    corners = sum(len(job.rounded) for job in first.jobs)
    if corners:
        print(f"  WARN  {corners} inside corner(s) come out rounded to the "
              f"tool's {first.cutter_diameter_mm / 2.0:.4f} mm radius rather "
              f"than sharp")
    print(f"  {'PASS' if first.scores_untouched else 'FAIL'}  prep wrote no "
          f"{route_toolpath.SCORE_LAYER}: the {len(first.score_lines)} score "
          f"line(s) on this board are the designer's and came back unchanged")
    if first.stale_scores:
        print(f"  WARN  {len(first.stale_scores)} {route_toolpath.SCORE_LAYER} "
              f"line(s) lie on no candidate blade line, so they score nothing")
    if first.scores:
        print(f"  {'PASS' if first.scores_clear else 'FAIL'}  the finished cut "
              f"runs along no scored edge (the saw's line is left alone)")
        # Judged on standing material, not on the quota: a corner
        # whose board edge ENDS at the score achieves no overshoot and needs
        # none, so `junctions_reached` would call a good board short.
        print(f"  {'PASS' if first.junctions_clear else 'WARN'}  every run "
              f"meeting a score severs the board edge up to its plane, with the "
              f"declared {first.route_past_mm:.4f} mm wherever the edge has it "
              f"to give ({len(first.junctions)} junction(s))")
    print(f"  {'PASS' if reproducible else 'FAIL'}  prep is idempotent "
          f"({second.board_sha256[:16]}..., {second.removed} item(s) replaced "
          f"on the second run)")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = scratch_root(config) / f"{stamp}_prep_toolpath"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.config_path, out_dir / config.config_path.name)
    print(f"  artifact     {out_dir}")

    out = review(first.board_path, out_dir, first)
    print(f"  review page  {out.stat().st_size / 1024:.0f} KB")
    print(out.resolve())
    return 0 if reproducible and first.scores_untouched else 1


if __name__ == "__main__":
    sys.exit(main())
