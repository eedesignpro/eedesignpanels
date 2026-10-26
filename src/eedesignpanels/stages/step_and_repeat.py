"""`build_panel` stage: step and repeat the whole board into a panel.

Every placement gets a copy of the source board's content - footprints, tracks,
vias, zones with their stored fill records untouched, and graphics on every
layer the config does not exclude - injected by `eedesigncore.inject_board`.
The outline-only panel this stage used to build is gone; an outline was what
could be replicated before the library shipped board-into-board injection, not
what a panel is.

The output is a PROJECT, not a board file: `panel.kicad_pcb` and
`panel.kicad_pro` together (`eedesignpanels.project`). The net classes and the
text variables live in the project file, so a panel without one is a panel whose
`${…}` text does not resolve.

Five properties this stage is built to keep:

* **Every copy is a RIGID transform of the source: a quarter turn about the
  board origin, then a move.** That is the order `inject_board` applies, and
  `eedesignpanels.placement` computes it, so nothing re-derives a placement.
  Text KiCad holds upright is the ONE EXCEPTION and it is deliberate
  (:mod:`eedesignpanels.stages.upright`): it is resolved on
  the copy before the turn, at the angle and the justification KiCad's own turn
  would leave, so it reads upright and its ink sits where KiCad puts it rather
  than turning over with the copy. Without any resolution the draw-time fold
  puts every rotated designator back where the source had it. Nothing is mirrored:
  that needs a layer-flipping transform `eedesigncore` does not expose, and
  the config refuses it by name.
* **Every replica's uuid is derived, not generated.** The grid cell is the key,
  so the same board and the same config give the same uuids on every run and on
  every machine. `inject_board(instance_key=...)` does it.
* **Every copy is its own electrical thing.** Net names take the instance index
  as a suffix, so no copy is wired to another - a net is a NAME in KiCad 10, and
  six copies naming `AGND` are one net. Reference designators are NOT touched:
  the silkscreen says `R1` on every copy because that is what an assembler
  reads. The schematic linkage is cut instead. See
  `eedesignpanels.identity`.
* **Placement is relative.** Instance (0, 0) sits exactly where the source board
  sits; the rest step off it. The panel is never re-origined, because moving it
  would be a second thing that could be wrong and nothing needs it - the review
  render crops to the board extent anyway.
* **Drawing-scale layers are not repeated.** The fabrication drawing frame, its
  dimensions and its notes describe the design once, so six copies of them is
  six copies of one drawing. `config.exclude_layers` names them; the layers stay
  in the panel's layer table and only their content stays behind.
* **No dimension is ever replicated,** on any layer, config or no config. A
  dimension is documentation, and its file identity does not survive KiCad's
  loader. See `replication_content`.

Zone fills ride along verbatim. `inject_board` deep-copies the stored
`(filled_polygon ...)` records and `translate_board` moves them with the
outline, so a panel instance plots the copper its source plotted. Nothing here
refills a zone, and nothing may.
"""

import time
from pathlib import Path
from typing import NamedTuple

from eedesigncore import (
    Board,
    Dimension,
    ProjectFile,
    Property,
    Setup,
    board_bbox,
    inject_board,
    item_layers,
    items_on_layers,
    new_board_from,
)
from eedesigncore.common.kimath import normalize_angle

from eedesignpanels import (
    identity,
    mintab,
    placement as placement_math,
    plotparams,
    project,
)
from eedesignpanels.config import PanelConfig
from eedesignpanels.stages import (
    panel_scores,
    panelize,
    panel_text,
    prune_members,
    route_toolpath,
    sha256_of,
    upright,
)

OUTLINE_LAYER = "Edge.Cuts"


def __getattr__(name: str):
    """Name the one attribute that was removed, rather than 404 on it.

    `REPLICATED_LAYERS` was a module constant while this stage replicated one
    layer. Full injection replicates content on every layer the board has, and
    which layers those are is a property of the BOARD, so the answer moved to
    :func:`replicated_layers` and to `PanelGeometry.replicated_layers`.
    """
    if name == "REPLICATED_LAYERS":
        raise AttributeError(
            "step_and_repeat.REPLICATED_LAYERS is gone: this stage replicates "
            "every layer the source board has, so the set depends on the board. "
            "Call replicated_layers(source) or read "
            "panel_geometry(...).replicated_layers instead."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ContentCounts(NamedTuple):
    """How much board content one number counts - a source, or a whole panel.

    Reported so the panel can be checked against arithmetic rather than
    against a picture: a 2x3 panel holds exactly six times the source's
    footprints, tracks and zones, and a build that does not is wrong in a way
    no render shows.
    """

    footprints: int
    drawings: int
    points: int
    tracks: int
    zones: int
    groups: int
    # A subset of `drawings`, counted separately because the panel must hold
    # NONE of them however many the source has.
    dimensions: int

    @classmethod
    def of(cls, board: Board) -> "ContentCounts":
        drawings = (*board.drawings, *board.trailing_drawings)
        return cls(
            footprints=len(board.footprints),
            drawings=len(drawings),
            points=len(board.points),
            tracks=len(board.tracks),
            zones=len(board.zones),
            groups=len(board.groups),
            dimensions=sum(1 for item in drawings if is_dimension(item)),
        )

    def times(self, instances: int) -> "ContentCounts":
        """What a panel of *instances* copies of this content should hold."""
        return ContentCounts(*(value * instances for value in self))


class PanelGeometry(NamedTuple):
    """The grid arithmetic of one panel, and the outline it was measured from.

    Computed in one place because two callers need identical numbers: the
    stage, which translates by them, and the review, which renders a
    reference at them. A second derivation of the same step would be a second thing that
    could be wrong.
    """

    items: list
    outline_box: object
    inked_box: object
    extent: object
    columns: int
    rows: int
    replicated_layers: frozenset
    # THE ARRANGEMENT (`placement.Arrangement`): where every copy sits, how it
    # is turned, and the rectangle it landed in. One derivation, held, because
    # a placement computed twice is a placement that can disagree with itself -
    # the injection moves by it and the carrier measures against it.
    arrangement: object = None
    # THE SOURCE BOARD'S WHOLE OUTLINE as pure geometry in the source's own
    # frame - board-level `Edge.Cuts` plus every footprint's, so a tab's break
    # contour is in it. Held because the carrier needs the boards' SHAPE to keep
    # the rail pour off them and the panel cannot
    # recover it: the instances' outlines are stripped when the panel edge is
    # derived. Empty when the outline could not be read as geometry, which costs
    # the shape and nothing else - the keep-outs fall back to rectangles.
    source_outline: tuple = ()

    @property
    def placements(self) -> list:
        """Where every instance sits, row-major, in board millimetres."""
        return list(self.arrangement.placements)

    @property
    def instance_boxes(self) -> list:
        """Each board instance's own rectangle, `(min_x, min_y, max_x, max_y)`.

        The source outline's centreline box, TURNED and stepped to every
        placement. The panel half needs these and cannot measure them: by the
        time it runs the instances' outlines have been replaced by the panel's
        one outline, and which piece of it was which board is no longer written
        down anywhere.
        """
        return list(self.arrangement.boxes)

    @property
    def instance_extent_mm(self) -> tuple:
        """One instance's own extent AS IT SITS - swapped by a quarter turn."""
        return (self.arrangement.instance_width_mm,
                self.arrangement.instance_height_mm)

    @property
    def turns(self) -> bool:
        """Does any copy in this arrangement take a turn?"""
        return any(place.turns for place in self.arrangement.placements)

    @property
    def inked_extent_mm(self) -> tuple:
        """Panel extent measured to the outside of the outline stroke.

        The stroke pad is the same on both axes - it is half a line width - so a
        turned instance takes the pad off the axis its own extent came from.
        """
        pads = (self.inked_box.width - self.outline_box.width,
                self.inked_box.height - self.outline_box.height)
        swapped = self.arrangement.instance_width_mm != self.outline_box.width
        across, down = (pads[1], pads[0]) if swapped else pads
        return (self.extent.width_mm + across, self.extent.height_mm + down)

    @property
    def outline_extent_mm(self) -> tuple:
        return (self.extent.width_mm, self.extent.height_mm)


class BuildOutcome(NamedTuple):
    """A built panel board and everything measured while building it."""

    board: Board
    geometry: PanelGeometry
    source_content: ContentCounts
    instance_content: ContentCounts
    panel_content: ContentCounts
    excluded_layers: frozenset
    nets_per_instance: int
    warnings: list
    # What the panel half of the build did, or None when the config has no
    # `panel` section and the build stops at six copies in a grid.
    panelize: object = None
    # THE STEPPED BUILD THIS WAS FINISHED FROM (:class:`SteppedBuild`), whose
    # own board is the panel BEFORE any carrier feature. Carried so a caller
    # that means to build this layout again with different carrier settings can
    # keep it and skip the injection - which is what the live preview does
    # (`preview.panelbuild.SteppedCache`). Ignoring it costs nothing.
    stepped: object = None


class SteppedBuild(NamedTuple):
    """A panel built as far as the carrier: the copies, and the panel round them.

    :func:`stepped_build` produces one; :func:`finish` turns one into a
    :class:`BuildOutcome` by putting the carrier features on it. The split
    exists because the two halves cost very different amounts and depend on
    very different things (MEASURED on the corpus): stepping six
    copies of the board is 301 ms of a 367 ms build and depends on the BOARD and
    the LAYOUT, while the carrier features are the rest and depend on their own
    config sections. A tooling hole diameter therefore needs no injection at all
    - see `preview.panelbuild`, which keeps one of these between edits.

    `stepped` is the panel half's own measurements (`panelize.SteppedPanel`), or
    None when the config named no rails and the build stops at a grid of copies.
    """

    board: Board
    geometry: PanelGeometry
    source_content: ContentCounts
    instance_content: ContentCounts
    panel_content: ContentCounts
    excluded_layers: frozenset
    nets_per_instance: int
    # Everything the first half had to say. The carrier's warnings follow these.
    warnings: tuple
    stepped: object = None


def cheap_copy(board: Board) -> Board:
    """*board* with lists of its own and the SAME items in them.

    **The enabling change for reusing a stepped panel**, and the
    whole of why it is safe is one sentence: the carrier stages only ever APPEND
    to `footprints`, `drawings` and `zones`, so a copy that owns its lists and
    shares their contents is a panel they can be run onto without the original
    seeing any of it.

    Measured on the corpus panel: under a millisecond, against 119-142 ms for
    `model_copy(deep=True)` and 301 ms to step the copies again. A deep copy
    would be correct too, and it is what to reach for the day a stage starts
    changing an item it did not create - a shared item mutated in place would be
    a change to a panel somebody else is holding, which is the one failure this
    shape can have. It is fenced from two sides: every carrier stage is read
    (they append), and every edit kind is compared BYTE FOR BYTE against a
    from-scratch build (`tests/test_panel_cache.py`).

    Every list-valued and dict-valued field is replaced, not only the three the
    carrier touches, because "which fields does a stage append to" is a fact
    about today's stages and this is meant to outlive them.

    ONE CONSEQUENCE FOR THE LIVE PREVIEW, stated because it is not obvious: two
    panels made this way share their footprints, so the content render's
    Value-hiding write (`preview.render_seam.values_hidden`) turns a flag off on
    items the KEPT panel holds as well. That is safe for exactly one reason -
    the seam hides, writes and restores inside the session lock, and every
    reader of a panel takes that lock - which is the reason the lock was given
    to the seam in the first place.
    """
    copy = board.model_copy(deep=False)
    for name, value in list(copy.__dict__.items()):
        if isinstance(value, list):
            copy.__dict__[name] = list(value)
        elif isinstance(value, dict):
            copy.__dict__[name] = dict(value)
    return copy


class StepAndRepeatResult(NamedTuple):
    """What one `build_panel` build produced, in millimetres and seconds."""

    output_path: Path
    output_sha256: str
    project_path: Path
    project_sha256: str
    instances: int
    items_per_instance: int
    source_content: ContentCounts
    instance_content: ContentCounts
    panel_content: ContentCounts
    expected_content: ContentCounts
    nets_per_instance: int
    replicated_layers: frozenset
    excluded_layers: frozenset
    warnings: list
    source_size_mm: tuple
    step_mm: tuple
    outline_extent_mm: tuple
    inked_extent_mm: tuple
    load_seconds: float
    build_seconds: float
    write_seconds: float
    panelize: object = None
    # The layers a fab run reading the panel's OWN plot settings will ink, by
    # canonical name, in board order. Reported because it is what the
    # downstream CICD reads and nothing else in the run output shows it.
    plot_layers: tuple = ()

    @property
    def content_matches(self) -> bool:
        """True when the panel holds exactly one replicated copy per cell."""
        return self.panel_content == self.expected_content


def outline_items(board: Board, layer: str = OUTLINE_LAYER) -> list:
    """The board's drawings on *layer*, in board-file order.

    `trailing_drawings` is included: it holds drawings that appeared after
    `(embedded_fonts ...)` in the source, and an outline is an outline wherever
    the file happened to put it. The multi-layer rule - a shape that names
    `(layers ...)` instead of `(layer ...)` - belongs to `eedesigncore` and is
    applied by its `items_on_layers`.
    """
    return items_on_layers((*board.drawings, *board.trailing_drawings), {layer})


def replicated_layers(source: Board, excluded: frozenset = frozenset()) -> frozenset:
    """Every layer this stage puts a copy of *source* on.

    The board's own layer table, minus whatever the config excluded. Full
    injection copies all content, so any layer the board declares can carry
    replicated geometry. Declaring the whole table rather than only the layers
    that happen to hold something today is deliberate: a source layer that is
    empty is compared empty against a panel layer that is empty, which reads as
    verified rather than as "present only in the panel". A comparison that
    left the layer out would hide the fact that it was checked at all.

    An EXCLUDED layer is left out for the same reason it was excluded:
    the panel is not supposed to carry its content, so comparing it would report
    the whole layer as a difference and bury every real one.

    The board's table is used rather than the layers its items name, because a
    pad names layer WILDCARDS - `*.Cu`, `*.Mask` - which are not layer names and
    would make this set claim layers no board has.
    """
    return frozenset(
        layer.canonical_name
        for layer in source.layers
        if layer.canonical_name not in excluded
    )


def resolve_layer_names(source: Board, names) -> tuple:
    """Canonical names for *names*, plus the ones this board does not have.

    An entry may be a canonical name or the display name KiCad's Appearance
    panel shows - `User.41` and `User.41-DwgCommon` are the same layer, and
    `User.Drawings` is what `Dwgs.User` is called on screen. A canonical name
    wins if a board ever gives one layer the display name of another.

    A name the board does not have resolves to nothing and is returned as
    unknown. It is a warning rather than an error: a default exclusion list is
    written for a family of boards, and a board that never used `User.42` is
    not a misconfigured board.
    """
    by_name = {}
    for layer in source.layers:
        if layer.user_name:
            by_name[layer.user_name] = layer.canonical_name
    for layer in source.layers:
        by_name[layer.canonical_name] = layer.canonical_name

    resolved, unknown = set(), []
    for name in names:
        canonical = by_name.get(name)
        if canonical is None:
            unknown.append(name)
        else:
            resolved.add(canonical)
    return frozenset(resolved), unknown


def _on_excluded_layer(item, excluded: frozenset) -> bool:
    """True when *item* names any excluded layer. `item_layers` owns the rule."""
    return not excluded.isdisjoint(item_layers(item))


def is_dimension(item) -> bool:
    """True for a `(dimension ...)` item, which is never replicated.

    Two reasons, and either alone would be enough. A dimension is
    DOCUMENTATION: it measures the design once, and six copies of one
    measurement is not six measurements.

    And its identity does not survive KiCad. `parseDIMENSION` hands the same
    object to `parsePCB_TEXT`, so the `(dimension)` node's own uuid is
    OVERWRITTEN by its inner text's. On a KiCad-written file the two are
    already identical and nothing shows - all three dimensions on the reference
    board have `id == text.id`. It appears the moment we write a replicated
    dimension whose two uuids were derived independently: KiCad silently keeps
    one, and any report joining on the file's dimension uuid loses every
    dimension.

    Skipping them is the caller's filter, applied to the content handed to
    `inject_board`. The library is not asked to know what a panel wants.
    """
    return isinstance(item, Dimension)


def _prune_members(owners: list, removed: set) -> tuple:
    """Drop references to removed items; drop an owner left with none.

    The rule lives in `eedesignpanels.stages.prune_members`, because panelizing
    removes items too and one silently-dropped group reference is enough.
    """
    return prune_members(owners, removed)


def replication_content(source: Board, excluded: frozenset) -> tuple:
    """The board `inject_board` copies: *source* minus what a panel must not repeat.

    Two filters, and every stream passes both. A layer the config excluded,
    and EVERY dimension whatever layer it sits on - the
    dimension rule is unconditional, so this always builds a view even when the
    exclusion set is empty.

    Returns `(view, warnings)`. The view holds REFERENCES to the source's items,
    never copies - `inject_board` deep-copies everything it injects, so the
    source is not modified by building the view or by using it. A footprint that
    holds filtered content is the one exception and is copied before its
    children are filtered.
    """
    view = new_board_from(source)
    # `new_board_from` carries settings only, and an embedded file is content
    # that kept items still need - a font a text renders with, a model a
    # footprint names. `inject_board` copies the ones its items need from here.
    view.embedded_files = source.embedded_files

    removed = set()

    def drop(item) -> bool:
        return is_dimension(item) or _on_excluded_layer(item, excluded)

    def keep(items: list) -> list:
        out = []
        for item in items:
            if drop(item):
                uuid = getattr(item, "id", None) or getattr(item, "uuid", None)
                if uuid is not None:
                    removed.add(uuid)
            else:
                out.append(item)
        return out

    # A trailing drawing is a position in the SOURCE file, not a property of the
    # item, and `inject_board` merges the two streams anyway.
    view.drawings = keep([*source.drawings, *source.trailing_drawings])
    view.points = keep(source.points)
    view.tracks = keep(source.tracks)
    view.zones = keep(source.zones)
    view.targets = keep(source.targets)

    footprints = []
    for footprint in source.footprints:
        children = (*footprint.drawings, *footprint.zones)
        if any(drop(child) for child in children):
            footprint = footprint.model_copy(deep=True)
            footprint.drawings = keep(footprint.drawings)
            footprint.zones = keep(footprint.zones)
        footprints.append(footprint)
    view.footprints = footprints

    view.groups, group_pruned, group_dropped = _prune_members(source.groups, removed)
    view.generators, gen_pruned, gen_dropped = _prune_members(source.generators, removed)

    warnings = []
    pruned, dropped = group_pruned + gen_pruned, group_dropped + gen_dropped
    if pruned:
        warnings.append(
            f"layer exclusion removed items that {pruned} group(s) or "
            f"generator(s) referenced; those references were pruned and "
            f"{dropped} owner(s) left empty were dropped"
        )
    return view, warnings


def panel_geometry(
    source: Board, columns: int, rows: int, gap_mm: float, exclude_layers=(),
    gap_y_mm: float = None, rotation_deg: float = 0.0, alternate: str = "none",
    nest_across_mm: float = 0.0, nest_down_mm: float = 0.0, placements=None,
) -> PanelGeometry:
    """Measure the source outline and derive the arrangement. Reads, never writes.

    `rotation_deg`, `alternate` and the two nest shifts are the placement suite,
    and every one of them defaults to the panel this
    built before they existed. `placements` is a PLACEMENT FILE's rows -
    `(across_mm, down_mm, angle_deg)` triples in row-major cell order - which
    replace the pattern entirely.

    Raises:
        ValueError: when the source board has no `Edge.Cuts` geometry, which
            means there is no outline to repeat and no panel to build; when a
            turn is not a quarter turn; or when a placement file's row count is
            not the grid's cell count.
    """
    items = outline_items(source)
    # Keep source items for counts. Measure placed footprints in board coordinates.
    for footprint in source.footprints:
        items.extend(items_on_layers(footprint.drawings, {OUTLINE_LAYER}))
    if not items:
        raise ValueError(
            f"the source board has no {OUTLINE_LAYER} drawings, so there is no "
            f"outline to step and repeat"
        )

    # The centreline box is the board size an engineer designed to: a 15.5 mm
    # board is 15.5 mm between outline centres, and the outline's 12.7 um stroke
    # is a drawing convention, not part of the board. So the gap is clear space
    # between centrelines, and the stroked extent is reported separately.
    box = board_bbox(source, layers={OUTLINE_LAYER}, include_width=False)
    inked = board_bbox(source, layers={OUTLINE_LAYER}, include_width=True)
    rect = (box.min_x, box.min_y, box.max_x, box.max_y)
    # THE WHOLE OUTLINE, footprint-hosted break contours included, for the rail
    # pour's keep-outs. A board whose `Edge.Cuts` holds a shape with no extent
    # at all cannot be read as geometry; that is a prep failure and prep says so
    # first, so here it degrades to no outline rather than stopping a panel that
    # does not need one.
    try:
        source_outline = tuple(route_toolpath.outline_primitives(source))
    except ValueError:
        source_outline = ()
    arrangement = (
        placement_math.placed_arrangement(rect, columns, rows, placements)
        if placements is not None else
        placement_math.arrangement(
            rect, columns, rows, gap_mm, gap_y_mm,
            rotation_deg=rotation_deg, alternate=alternate,
            nest_across_mm=nest_across_mm, nest_down_mm=nest_down_mm,
        )
    )
    return PanelGeometry(
        items=items,
        source_outline=source_outline,
        outline_box=box,
        inked_box=inked,
        extent=arrangement.extent,
        columns=columns,
        rows=rows,
        replicated_layers=replicated_layers(
            source, resolve_layer_names(source, exclude_layers)[0]
        ),
        arrangement=arrangement,
    )


def unlink_from_schematic(footprint) -> None:
    """Cut one injected footprint loose from the design's schematic, in place.

    `path`, `sheetname` and `sheetfile` are CLEARED. They point at the symbol in
    the source design's schematic, six copies would point at the same one, and
    an update-from-schematic against a panel would then match one arbitrary copy
    and orphan the rest. A panel is not schematic-linked.

    The REFERENCE DESIGNATOR is not touched. Every copy carries the source's,
    verbatim, so six `R1`s is six `R1`s: the
    designator is printed on the silkscreen, and a board that says `R1_4` where
    the design says `R1` is a board an assembler cannot read.
    """
    footprint.path = None
    footprint.sheetname = None
    footprint.sheetfile = None


def set_board_property(board: Board, name: str, value: str) -> None:
    """Set one board property, replacing any entry of that name, in place."""
    for prop in board.properties:
        if prop.name == name:
            prop.value = value
            return
    board.properties.append(Property(name=name, value=value))


def build(
    source: Board,
    columns: int,
    rows: int,
    gap_mm: float,
    exclude_layers=(),
    rails=None,
    min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM,
    gap_y_mm: float = None,
    tooling_holes=None,
    scores=None,
    rail_copper=None,
    dimensions=None,
    fiducials=None,
    fill_rails: bool = True,
    rotation_deg: float = 0.0,
    alternate: str = "none",
    nest_across_mm: float = 0.0,
    nest_down_mm: float = 0.0,
    placements=None,
    score_half_width_mm: float = panel_scores.DEFAULT_RULE_HALF_WIDTH_MM,
    score_setbacks=None,
) -> BuildOutcome:
    """Build the panel board from *source*, which is never modified.

    `rails` runs the PANEL half as well: the instances' own board outlines are
    replaced by one panel outline derived from the blank minus the routed
    channels, and their score lines merge into panel-spanning passes
    (:mod:`eedesignpanels.stages.panelize`). It is four rail widths, or one
    number meaning all four sides. None stops at the grid of copies, which is
    what this stage meant before there was a panel half.

    `min_tab_mm` is the minimum tab the panel half enforces between channels.

    `tooling_holes`, `scores`, `rail_copper` and `fiducials` are the carrier
    features, each None unless the config asked for it. `dimensions` is the
    one feature that is
    ON unless a config declines it, so None here means DECLINED rather than
    unasked (`config.PanelizeConfig.dimension_settings`); a caller passing plain
    values gets no dimensions unless it says so. `fill_rails` says whether the
    rail pour is actually filled, which costs a KiCad process; a caller that
    only wants to see where the copper goes passes False.

    The two halves are :func:`stepped_build` and :func:`finish`, and this is
    both of them in order. A caller that builds the same layout repeatedly with
    different carrier settings uses them separately.
    """
    return finish(
        stepped_build(source, columns, rows, gap_mm,
                      exclude_layers=exclude_layers, rails=rails,
                      min_tab_mm=min_tab_mm, gap_y_mm=gap_y_mm,
                      rotation_deg=rotation_deg, alternate=alternate,
                      nest_across_mm=nest_across_mm,
                      nest_down_mm=nest_down_mm, placements=placements,
                      score_setbacks=score_setbacks),
        tooling_holes=tooling_holes,
        scores=scores,
        rail_copper=rail_copper,
        dimensions=dimensions,
        fiducials=fiducials,
        fill_rails=fill_rails,
        score_half_width_mm=score_half_width_mm,
    )


def stepped_build(
    source: Board,
    columns: int,
    rows: int,
    gap_mm: float,
    exclude_layers=(),
    rails=None,
    min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM,
    gap_y_mm: float = None,
    rotation_deg: float = 0.0,
    alternate: str = "none",
    nest_across_mm: float = 0.0,
    nest_down_mm: float = 0.0,
    placements=None,
    score_setbacks=None,
) -> SteppedBuild:
    """Step and repeat *source*, and panelize the copies. NO carrier features.

    The expensive half of a build and the one that depends on the board and the
    layout: the injection of every copy, and then the panel round them - blank,
    derived outline, merged saw passes, flattened copper. *source* is never
    modified.

    `rails` None stops at the grid of copies, exactly as :func:`build` does.

    `score_setbacks` is the ONE score setting this half needs: the rail score
    setback GROWS THE PANEL rather than moving the
    saw line, so it is a fact about the blank and the blank is measured here.
    Everything else in the scores section belongs to the carrier half.

    **A copy that TURNS is injected from RESOLVED content**
    (`stages.upright`): keep-upright text is written at the angle and the
    justification KiCad's own turn would leave it at, and unlocked, so nothing
    folds it again. ONE VIEW PER TURN ANGLE. A copy at 0
    degrees is injected from the plain content and is the copy this stage built
    before the placement suite existed, to the byte.
    """
    geometry = panel_geometry(
        source, columns, rows, gap_mm, exclude_layers, gap_y_mm,
        rotation_deg=rotation_deg, alternate=alternate,
        nest_across_mm=nest_across_mm, nest_down_mm=nest_down_mm,
        placements=placements,
    )
    excluded, unknown = resolve_layer_names(source, exclude_layers)
    content, warnings = replication_content(source, excluded)
    warnings += list(geometry.arrangement.warnings)

    panel = new_board_from(source)
    from eedesignpanels.fingerprints import remove_stamp
    remove_stamp(panel)
    # The board PROPERTIES come with the panel: they are the board's snapshot of
    # the project's text variables, and a panel without them plots the literal
    # `${PCB_DWG_SCALE}` where the design plots `10`. They arrive with
    # `new_board_from`; the marker says which document this is.
    # The TITLE BLOCK stays behind. It feeds the drawing sheet, which is where
    # "this IS the design" reads loudest.
    panel.title_block = None
    set_board_property(panel, project.PANEL_MARKER, project.PANEL_MARKER_VALUE)

    net_names = sorted(content.net_names)
    instances = columns * rows
    warnings += _identity_warnings(net_names, instances)
    if unknown:
        warnings.append(
            f"{len(unknown)} excluded layer name(s) are not on this board, so "
            f"they exclude nothing: {', '.join(unknown)}"
        )

    # RESOLVED CONTENT, ONE VIEW PER TURN ANGLE. The
    # resolution depends on the turn: KiCad swaps a text's justifications when
    # the turn carries it past 180 degrees, so the view resolved for 180 is the
    # wrong view for 90. The plain view still serves every copy that stays
    # upright. One view for every turn would put the ink in the wrong place on
    # a turned copy.
    resolved_views = {}
    for placement in geometry.placements:
        if not placement.turns:
            continue
        key = normalize_angle(placement.rotation_deg)
        if key not in resolved_views:
            resolved_views[key] = upright.resolved_content(
                content, placement.rotation_deg
            )
    # The same set of texts for every angle, so the count is said once.
    counted = next(
        (view for view in resolved_views.values() if view.items), None
    )
    if counted is not None:
        warnings.append(
            f"{counted.items} text item(s) on {counted.footprints} "
            f"footprint(s) are held upright by KiCad, and every turned copy "
            f"carries them unlocked, at the angle and the justification KiCad "
            f"would draw them at after that turn - so a copy turned again "
            f"inside the panel turns its text with it instead of keeping it "
            f"readable"
        )

    score_copies = []
    for placement in geometry.placements:
        index = identity.instance_index(placement.column, placement.row, columns)
        added = inject_board(
            panel,
            resolved_views[normalize_angle(placement.rotation_deg)].board
            if placement.turns else content,
            offset=(placement.dx_mm, placement.dy_mm),
            # THE TURN CENTRE IS THE ORIGIN, always. A centre that is not a
            # whole number of internal units pushes every rotated coordinate off
            # the nanometre lattice; the origin cannot.
            rotation=placement.rotation_deg,
            rotation_center=(0.0, 0.0),
            instance_key=identity.instance_key(placement.column, placement.row),
            uuid_namespace=identity.INSTANCE_UUID_NAMESPACE,
            net_map=identity.net_map(net_names, index),
        )
        for footprint in added.footprints:
            unlink_from_schematic(footprint)
        score_copies.append([
            item for item in items_on_layers(added.drawings, {"User.38"})
            if type(item).__name__ == "PcbLine"
        ])

    warnings.extend(panelize.score_alignment_warnings(
        score_copies, geometry.placements,
    ))

    # Counted BEFORE panelizing: the content check is about replication - six
    # copies of one board - and the panel's own outline is not a copy of
    # anything, so it belongs to a different question.
    panel_content = ContentCounts.of(panel)
    stepped = None
    if rails is not None:
        stepped = panelize.stepped(panel, rails, min_tab_mm,
                                   score_setbacks=score_setbacks)
        warnings += list(stepped.warnings)

    # The drill/place and grid origins belong to the finished panel.
    # KiCad's downward Y axis puts the bottom edge at the maximum Y value.
    if stepped is not None:
        origin = (stepped.blank.min_x, stepped.blank.max_y)
    else:
        boxes = geometry.instance_boxes
        origin = (min(box[0] for box in boxes), max(box[3] for box in boxes))
    if panel.setup is None:
        panel.setup = Setup()
    panel.setup.aux_axis_origin = origin
    panel.setup.grid_origin = origin

    return SteppedBuild(
        board=panel,
        geometry=geometry,
        source_content=ContentCounts.of(source),
        instance_content=ContentCounts.of(content),
        panel_content=panel_content,
        excluded_layers=excluded,
        nets_per_instance=len(net_names),
        warnings=tuple(warnings),
        stepped=stepped,
    )


def finish(built: SteppedBuild, tooling_holes=None, scores=None,
           rail_copper=None, dimensions=None, fiducials=None,
           fill_rails: bool = True,
           cli_path=None, work_dir=None,
           score_half_width_mm: float =
           panel_scores.DEFAULT_RULE_HALF_WIDTH_MM,
           text_settings=None, text_content="", text_warning="") -> BuildOutcome:
    """Put the carrier features on a stepped build. *built* is NOT modified.

    The cheap half, and the one that depends only on the carrier's own config
    sections. **The features go on a CHEAP COPY** of `built.board`
    (:func:`cheap_copy`), which is what makes one stepped build finishable any
    number of times: the panel that comes back is the caller's, and the one
    inside *built* is still the panel before any carrier feature. That property
    is the whole basis of the live preview's cache, so it lives here rather
    than in the caller.

    A build whose config named no rails has nothing to put features on and comes
    back as the grid of copies it is.
    """
    panel = cheap_copy(built.board)
    panelized = None
    warnings = list(built.warnings)
    if built.stepped is not None:
        panelized = panelize.carrier(
            panel, built.stepped,
            tooling_holes=tooling_holes,
            scores=scores,
            rail_copper=rail_copper,
            dimensions=dimensions,
            fiducials=fiducials,
            instance_boxes=built.geometry.instance_boxes,
            source_outline=built.geometry.source_outline,
            placements=built.geometry.placements,
            fill_rails=fill_rails,
            cli_path=cli_path,
            work_dir=work_dir,
            score_half_width_mm=score_half_width_mm,
            text_settings=text_settings, text_content=text_content,
            text_warning=text_warning,
        )
        # The carrier's own warnings only: the first half's are already here,
        # and `panelize.carrier` returns both so that its result reads as one
        # panelize pass.
        warnings += list(panelized.warnings)[len(built.stepped.warnings):]
        _select_dimensions_for_plotting(panel, panelized, warnings)

    return BuildOutcome(
        board=panel,
        geometry=built.geometry,
        source_content=built.source_content,
        instance_content=built.instance_content,
        panel_content=built.panel_content,
        excluded_layers=built.excluded_layers,
        nets_per_instance=built.nets_per_instance,
        warnings=warnings,
        panelize=panelized,
        stepped=built,
    )


def _select_dimensions_for_plotting(panel: Board, panelized, warnings: list):
    """Put the dimensions layer in the panel's stored plot layer selection.

    The dimensions layer is plotted. The panel says how big it is, and a
    statement only the panel builder can see is not a statement to a
    fabricator.

    The selection the panel inherits is the DESIGN's, and a design does not plot
    a layer that only a panel draws on - the corpus board's stored selection is
    copper, mask, paste and `Edge.Cuts`, and `User.16` is not in it. So the one
    layer the panel added is added to the selection too, by canonical name, and
    nothing is ever removed: the fab run the design configured is the fab run
    the panel is plotted for.

    Why the BOARD and not the `.kicad_pro`: the selection lives in
    `(setup (pcbplotparams (layerselection …)))`, and the project file carries
    no layer selection at all - see :mod:`eedesignpanels.plotparams` for the
    citations and for what reads it downstream.

    Nothing happens when the config switched the dimensions off: there is then
    no dimension to plot, and widening the selection would only ink an empty
    layer.
    """
    drawn = getattr(panelized, "dimensions", None)
    if drawn is None or not drawn.count:
        return
    changed = plotparams.select_layers(panel, [drawn.layer])
    if changed is None:
        warnings.append(
            f"the panel carries no (setup …) block, so it has no stored plot "
            f"layer selection to put {drawn.layer} in - a fab run reading the "
            f"board's own plot settings will not ink its "
            f"{drawn.count} dimension(s)"
        )


def _identity_warnings(net_names, instances: int) -> list:
    """Net suffix collisions, named so they can never happen silently.

    A source holding both `AGND` and `AGND_1` maps two distinct nets onto one
    panel name, which JOINS them electrically. The build still completes and
    still writes - every check warns and none blocks - but it says so
    in the run output, because the alternative is a panel that is wrong in a way
    only a netlist comparison would find.

    Designators are not suffixed, so they cannot collide.
    """
    warnings = []
    net_clash = identity.collisions(net_names, instances)
    if net_clash:
        warnings.append(
            f"net name collision: suffixing {len(net_clash)} net name(s) lands "
            f"on a name the source already uses, which MERGES them in the "
            f"panel: {', '.join(net_clash)}"
        )
    return warnings


def write_project(config: PanelConfig, warnings: list) -> Path | None:
    """Write `panel.kicad_pro` beside the board, from the design's own.

    Returns the path, or None when the design has no project file - which is a
    warning and not a failure: the board is still a valid panel, its `${…}` text
    just has nothing to resolve against.
    """
    source_path = config.source_project_path
    if not source_path.is_file():
        warnings.append(
            f"the design has no project file at {source_path.name}, so the "
            f"panel has none either - its text variables will not resolve and "
            f"it carries no net classes"
        )
        return None

    output = config.output_project_path
    panel = project.build_project(
        ProjectFile.from_file(source_path), filename=output.name
    )
    panel.to_file(output)
    return output


def build_from(config: PanelConfig, source: Board,
               fill_rails: bool = True) -> BuildOutcome:
    """Build the panel from a board ALREADY IN MEMORY, and from this config.

    The in-memory entry, and the seam the live preview builds through: the panel
    page derives its panel from the board the prep page is editing - unsaved
    score decisions and the toolpath included - so nothing has to reach the disk
    for a panel to exist.

    :func:`run` calls exactly this after loading the file, so the two paths are
    one build and not two. A board whose file bytes match therefore gives a
    panel whose file bytes match, which is the property the preview is tested
    against.

    *source* is never modified: `inject_board` deep-copies everything it
    injects, and the panel half works on the panel board.

    THE CONFIG SAYS EVERYTHING IT MEANS: every
    group is written and every group carries its own `enabled`, so there is no
    retired key to translate and no note saying which spelling won. A file that
    is not in that shape is refused by the schema rather than read as something
    it does not say.

    `fill_rails=False` skips the KiCad refill of the rail pour, which is what a
    caller that wants the panel's geometry without a several-second process
    asks for. The zones are there either way; they are simply empty.

    The two halves are :func:`stepped_from` and :func:`finish_from`, which is
    the same split as :func:`stepped_build` and :func:`finish` read off a
    config. A caller keeping a stepped build between edits uses those.
    """
    return finish_from(config, stepped_from(config, source),
                       fill_rails=fill_rails)


def stepped_from(config: PanelConfig, source: Board) -> SteppedBuild:
    """The layout half of :func:`build_from`: this config's grid, panelized.

    Everything the CONFIG's layout says - the grid, the gaps, the placement
    suite, the rails, the minimum tab and the excluded layers - and nothing its
    carrier sections do. *source* is never modified.

    A live PLACEMENT FILE overrides the pattern settings, and the settings it
    overrides are NAMED in the build's warnings rather than silently dropped: a
    config that states two arrangements has to say which one won.
    """
    from eedesignpanels import rigid_flex
    if rigid_flex.is_rigid_flex(source, config.source_project_path):
        source = source.model_copy(deep=True)
        rigid_flex.mark_construction(source)
    grid = config.grid
    scores = config.panel.score_settings if config.panel is not None else None
    built = stepped_build(
        source,
        grid.columns,
        grid.rows,
        grid.gap_across_mm,
        exclude_layers=config.exclude_layers,
        rails=config.rail_widths,
        min_tab_mm=config.min_tab_mm,
        gap_y_mm=grid.gap_down_mm,
        rotation_deg=grid.rotation_deg,
        alternate=grid.alternate,
        nest_across_mm=grid.nest_across_mm,
        nest_down_mm=grid.nest_down_mm,
        placements=(
            grid.placement.triples if grid.placement is not None else None
        ),
        # THE ONE SCORE SETTING THE LAYOUT HALF NEEDS: the rail score setback
        # grows the panel, so it decides the
        # blank and not the saw line. Read through `Settings.governed`, the one
        # place that zeroes a dependant whose rail score is off, so a setback
        # typed under a switched-off rail score grows nothing.
        score_setbacks=(
            panel_scores.Settings.of(scores).governed.setbacks
            if scores is not None else None
        ),
    )
    overridden = grid.unused_by_the_placement_file
    if overridden:
        return built._replace(warnings=(
            f"a placement file is active ({len(grid.placement.rows)} "
            f"placements), so it says where every copy goes and these settings "
            f"are not used: {', '.join(overridden)}",
            *built.warnings,
        ))
    return built


def finish_from(config: PanelConfig, built: SteppedBuild,
                fill_rails: bool = True) -> BuildOutcome:
    """The carrier half of :func:`build_from`: this config's features, on *built*.

    *built* is not modified - see :func:`finish` - so one stepped build serves
    as many carrier settings as a designer types.
    """
    holes, scores, rail_copper = config.carrier_features
    text_settings = config.panel.text if config.panel is not None else None
    text_content, text_warning = "", ""
    if text_settings is not None and text_settings.enabled and text_settings.rails != "none":
        text_content, text_warning = panel_text.read_sspn(config.source_project_path)
    outcome = finish(
        built,
        tooling_holes=holes,
        scores=scores,
        rail_copper=rail_copper,
        dimensions=(
            config.panel.dimension_settings if config.panel is not None else None
        ),
        # ONE QUESTION FOR EVERY CARRIER STAGE: the section
        # is always in the file and `enabled` inside it is the switch, so
        # `fiducial_settings` answers exactly as the scores and the pour do.
        fiducials=(
            config.panel.fiducial_settings if config.panel is not None else None
        ),
        fill_rails=fill_rails,
        # THE ONE KEEP-OUT WIDTH THIS CONFIG DECLARES, and it is a prep setting:
        # the board's own score keepouts were written at it,
        # and the panel has to guard at the same width to recognise them.
        score_half_width_mm=config.score_rule_half_width_mm,
        text_settings=text_settings, text_content=text_content,
        text_warning=text_warning,
    )
    return outcome


class WrittenPanel(NamedTuple):
    """The files one panel write put in the panel folder, and their hashes."""

    output_path: Path
    output_sha256: str
    project_path: Path
    project_sha256: str
    warnings: list


def write_panel(config: PanelConfig, outcome: BuildOutcome) -> WrittenPanel:
    """Put the built panel in the panel folder: the board, and its project.

    The folder is created when it is missing. Both files are generated output
    and are overwritten; nothing else in the folder is touched, and the config
    is not this function's business.
    """
    output = config.output_board_path
    output.parent.mkdir(parents=True, exist_ok=True)
    outcome.board.to_file(output)
    warnings = list(outcome.warnings)
    # A `.kicad_pcb` alone is not a project, and the panel is one.
    project_path = write_project(config, warnings)
    return WrittenPanel(
        output_path=output,
        output_sha256=sha256_of(output),
        project_path=project_path,
        project_sha256=sha256_of(project_path) if project_path else "",
        warnings=warnings,
    )


def result_for(config: PanelConfig, outcome: BuildOutcome,
               written: WrittenPanel, load_seconds: float = 0.0,
               build_seconds: float = 0.0,
               write_seconds: float = 0.0) -> StepAndRepeatResult:
    """One build's numbers, as the run report and the preview both read them."""
    geometry = outcome.geometry
    instances = config.grid.columns * config.grid.rows
    return StepAndRepeatResult(
        output_path=written.output_path,
        output_sha256=written.output_sha256,
        project_path=written.project_path,
        project_sha256=written.project_sha256,
        instances=instances,
        items_per_instance=len(geometry.items),
        source_content=outcome.source_content,
        instance_content=outcome.instance_content,
        panel_content=outcome.panel_content,
        expected_content=outcome.instance_content.times(instances),
        nets_per_instance=outcome.nets_per_instance,
        replicated_layers=geometry.replicated_layers,
        excluded_layers=outcome.excluded_layers,
        warnings=written.warnings,
        source_size_mm=(geometry.outline_box.width, geometry.outline_box.height),
        step_mm=(geometry.extent.step_x_mm, geometry.extent.step_y_mm),
        outline_extent_mm=geometry.outline_extent_mm,
        inked_extent_mm=geometry.inked_extent_mm,
        load_seconds=load_seconds,
        build_seconds=build_seconds,
        write_seconds=write_seconds,
        panelize=outcome.panelize,
        plot_layers=plotparams.plotted_layers(outcome.board),
    )


def run(config: PanelConfig) -> StepAndRepeatResult:
    """Build the panel from the config. Writes only inside panel/.

    Load, build, write - and the build is :func:`build_from`, the same call the
    live preview makes on a board it already holds.
    """
    from eedesignpanels import fingerprints
    started = time.monotonic()
    evidence = None
    fingerprint_note = ""
    try:
        evidence = fingerprints.capture(config.source_board_path, config.config_path,
                                        config.model_dump(mode="json", exclude={"config_path"}))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        fingerprint_note = str(exc)
    source = (Board.from_string(evidence["raw"][config.source_board_path.resolve()].decode("utf-8-sig"))
              if evidence is not None else Board.from_file(config.source_board_path))
    loaded = time.monotonic()

    outcome = build_from(config, source)
    built = time.monotonic()

    written = write_panel(config, outcome)
    try:
        if evidence is not None:
            fingerprints.stamp_saved(written.output_path, evidence)
            written = written._replace(output_sha256=sha256_of(written.output_path))
        elif fingerprint_note:
            written.warnings.append("Fingerprint unverified: " + fingerprint_note)
    except fingerprints.Unverified as exc:
        written.warnings.append(str(exc))
    finished = time.monotonic()

    return result_for(
        config, outcome, written,
        load_seconds=loaded - started,
        build_seconds=built - loaded,
        write_seconds=finished - built,
    )
