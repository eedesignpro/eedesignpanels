"""Panelize: turn the stepped-and-repeated instances into ONE panel.

Step and repeat makes six copies of a board. This is what makes them a panel:
a blank of material with channels routed out of it, scored where a saw takes
over, and a single `Edge.Cuts` outline that says what the fabricator actually
cuts. It runs on the panel board in memory, after injection and before the
write, and it owns exactly four things.

**1. EVERY `Edge.Cuts` item the instances brought goes** - board-level and
footprint-hosted alike, on every build. A fabricator reads `Edge.Cuts` as "cut
here", so a tab's break contour left inside a panel is a cut nobody asked for,
and a panel that keeps them cannot be fabricated. The panel's outline is
DERIVED and nothing else is allowed on the layer;
what the tab declares about the break shape is the tab footprint's business on
the SOURCE board, which this stage never touches.

**2. The panel outline is derived, in one boolean.** The blank is the instance
grid grown by its four rails (`panel.rails`); the channels are every `User.36`
item dilated by half its own stroke, which is the material the cutter removed;
the outline is the boundary of the blank minus the channels
(:mod:`eedesignpanels.channels`). Nothing is placed, nothing is drawn by hand,
and the loop count is a measurement rather than a design - a panel whose
channels cut it into pieces reports the pieces.

**THE INVARIANT: every routed section of
`Edge.Cuts` is the outline of the toolpath layer's ink.** Only the blank's own
edges are exempt, because no cutter made them. So any pass that shapes the
outline exists on `User.36` FIRST and the outline is derived from it - never
beside it. A panel needs two kinds of pass the stepped boards cannot bring: the
crossings that consume a web of material between two NEIGHBOURING instances,
which is an accident of the arrangement and exists in no board; and a pinch cut,
where two passes touch and the material has no width at all. Both are written to
the panel's own `User.36` before the boundary is written to `Edge.Cuts`
(:func:`to_toolpath_items`). A sliver INSIDE a board is the board's and is cut
at prep time, so it arrives on the layer already, stepped like any other ink.

**3. Scores merge across instances.** Six instance `User.38` lines on two blade
lines are two saw passes, not six, so collinear lines merge and the merged line
is produced to the blank's own edge - one pass of a blade entering at one side
of the panel and leaving at the other.

**3a. JUMP SCORING stops those passes short of the frame.** Two distances, one
for horizontal passes and one for vertical ones, each applied at both ends of
every merged board-defined pass, so the panel keeps a continuous rim and stays
rigid. It runs in the CARRIER half (:func:`trim_jumps`), BEFORE the keep-outs
are drawn: the keep-out follows the shortened line, and the way to make
that true once is to shorten the line first and let the one keep-out mechanism
read the panel as it then stands. Zero is the full-span pass, to the byte.

A JUMP SHORTENS EVERY SCORE EXCEPT A RAIL SCORE. This module shortens the lines
the boards decided; the passes
between rows and columns are shortened where they are drawn
(:func:`panel_scores.apply`), because that is where they get their ends. A RAIL
score is never shortened: it runs along the rim the jump exists to keep whole.

A DEPENDANT WITH ITS GOVERNOR OFF IS ZERO: each jump distance
hangs off a rail score, and :attr:`panel_scores.Settings.governed` is what this
half reads, so a distance whose rail score is off shortens nothing whatever the
file says. The run's report names it.

**2a. No accidental sliver of material survives between two channels.** Two
instances' channels, or any two channels at all, that approach until the board
between them is thinner than `min_tab_mm` are ONE cut, and the cut region is
made continuous there before the boolean runs (:func:`channels.close_slivers`,
:mod:`eedesignpanels.mintab`). The tabs the instances brought declare their own
gaps and are protected; anything else the arrangement left is swarf, and it is
consumed and reported. This is also what retires the tangential pinch point -
two channel walls touching at exactly one point leave a sliver of width zero,
which no minimum tab tolerates, so the walk never reaches a node of degree four.

**4. The copper zones flatten and go; every rule area stays.** A copper zone in
a panel is a live object with a cached fill, and a panel is a fabrication
artifact: the fill becomes plain copper graphics and the zone is dropped, so
nothing downstream can refill it against a shape the designer never approved. A
rule area pours nothing, so that hazard does not touch it and the designer's
keepout survives into the panel unconditionally
(:mod:`eedesignpanels.stages.zones`).

**5. The carrier features run last, each only if it was asked for.** Tooling
holes (:mod:`eedesignpanels.stages.tooling_holes`), the panel's own v-scores and
the DRC rule areas that guard every score on the panel
(:mod:`eedesignpanels.stages.panel_scores`), and the rail pour
(:mod:`eedesignpanels.stages.rail_copper`), and the panel's optical targets
(:mod:`eedesignpanels.stages.fiducials`) - in that order, after the outline
exists, because each is measured against it. A config that names none of them
builds exactly the panel it built before they were written, to the byte.

The fiducials run AFTER the tooling holes, because the holes are the datum they
are offset from: a corner with no hole gets no fiducial, by name.

**THE RAIL POUR'S COPPER IS THE LAST THING THE PANEL GETS.**
The pour's outline is made in the order above, but KiCad fills a BOARD and not
a zone - whatever is on the panel when the sandbox is written is what the
filler holds off. So the fill is taken at the very end of the chain, once every
stage that can put copper down has run. Filling it in place flooded the
fiducials, which are added after it: their pads came back with no clearance
ring while the tooling holes, added before it, had one.

**6. The panel dimensions come last, and they are not opt-in.** The panel's
overall width and length, and nested inside them ONE BOARD's own width and
length, as native KiCad aligned dimension items outside the material
(:mod:`eedesignpanels.stages.panel_dimensions`). Last because they measure
everything above them; not opt-in because a fabrication panel that does not say
how big it is is a panel somebody has to measure. `panel.dimensions.enabled =
false` is how a config declines them.

`Edge.Cuts` and `User.38` therefore become PANEL-OWNED: their geometry is
derived from the whole panel and no per-instance reference exists for them.
That is why `PANEL_OWNED_LAYERS` is exported - the gerber comparison reads it
and reports those layers rather than judging them.
"""

import math
import uuid
from typing import NamedTuple

from eedesigncore import Board, PcbArc, PcbLine, Stroke, item_layers, items_on_layers

from eedesignpanels import channels, mintab
from eedesignpanels.outline_shapes import shape_primitives
from eedesignpanels.stages import fiducials as fiducials_stage
from eedesignpanels.stages import panel_dimensions as panel_dimensions_stage
from eedesignpanels.stages import panel_scores as panel_scores_stage
from eedesignpanels.stages import prune_members
from eedesignpanels.stages import rail_copper as rail_copper_stage
from eedesignpanels.stages import tooling_holes as tooling_holes_stage
from eedesignpanels.stages import zones
from eedesignpanels.toolpath import (
    COLLINEAR_TOL_MM,
    Line,
    chain_bbox,
    clip_to_bbox,
)

OUTLINE_LAYER = "Edge.Cuts"
TOOLPATH_LAYER = "User.36"
SCORE_LAYER = "User.38"

# The layers whose panel geometry is DERIVED from the whole panel, so that
# stamping the source board at every placement is not a reference for them.
# They are still exported, still rendered and still measured; what they no
# longer do is decide a verdict.
PANEL_OWNED_LAYERS = frozenset({OUTLINE_LAYER, SCORE_LAYER})

# The stroke a panel outline is drawn at when the instances carried no outline
# to take one from. KiCad's own default for `Edge.Cuts`.
DEFAULT_OUTLINE_WIDTH_MM = 0.05
# ...and for a score line, which means nothing physical either way.
DEFAULT_SCORE_WIDTH_MM = 0.1

# Two score lines are one blade pass when their directions agree to this. Unit
# vectors, so it is the sine of the angle between them - 0.06 degrees.
BLADE_PARALLEL_TOL = 1e-3

# The shortest saw pass jump scoring will leave standing, and the arithmetic
# that shortens one. BOTH LIVE IN `panel_scores`, because one rule covers both
# cases - a jump shortens the lines the boards decided AND the passes between
# rows and columns - and one rule gets one floor and one function. Re-exported
# under the names this module uses, so a caller and a test read them here too.
MIN_JUMPED_PASS_MM = panel_scores_stage.MIN_JUMPED_PASS_MM
jumped_span = panel_scores_stage.jumped_span

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so that the same panel built
# twice on two machines carries the same identities.
EDGE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-edge"
)
SCORE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-score"
)
# ...and the passes the panel itself has to route: the crossings between two
# neighbouring instances, and any pinch cut. Their own namespace, because they
# are cutter path rather than board edge and a uuid says which layer a thing
# belongs to as much as it says which thing it is.
TOOLPATH_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-toolpath"
)

class BladeLine(NamedTuple):
    """One saw pass across the finished panel, and the lines it replaces."""

    start: tuple
    end: tuple
    width_mm: float
    sources: tuple

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)


class ScoreMerge(NamedTuple):
    """What the merge did to the instances' score lines."""

    blades: tuple
    replaced: int
    kept: int

    @property
    def merged_count(self) -> int:
        return len(self.blades)


class JumpedPass(NamedTuple):
    """One saw pass stopped short of the panel edge, before and after."""

    # "horizontal" or "vertical" - the LINE'S own direction, which is what
    # chooses the distance.
    axis: str
    distance_mm: float
    was: tuple
    now: tuple

    @property
    def length_mm(self) -> float:
        return math.dist(self.now[0], self.now[1])


class JumpScoring(NamedTuple):
    """What jump scoring did to the merged board-defined passes."""

    horizontal_mm: float
    vertical_mm: float
    trimmed: tuple = ()
    # Passes the distances did NOT touch, each with the sentence saying why: a
    # pass too short to survive the trim, or one running down neither axis.
    declined: tuple = ()

    @property
    def count(self) -> int:
        return len(self.trimmed)

    @property
    def asked(self) -> bool:
        """Did the config ask for a jump at all?"""
        return self.horizontal_mm > 0.0 or self.vertical_mm > 0.0


class OutlineStrip(NamedTuple):
    """Every `Edge.Cuts` item the instances brought, and where it came from.

    Counted by SOURCE and by KIND rather than totalled, because what matters is
    one particular class of item surviving in one particular place, and a total
    cannot show that.
    """

    board_items: int
    footprint_items: int
    footprints_touched: int
    # (("PcbArc", 72), ("PcbLine", 228)) - the classes removed, in name order.
    by_kind: tuple
    # Items that named `Edge.Cuts` ALONGSIDE another layer, and so took their
    # other layer's geometry with them when they went.
    multi_layer: int
    groups_pruned: int
    groups_dropped: int

    @property
    def total(self) -> int:
        return self.board_items + self.footprint_items


class SteppedPanel(NamedTuple):
    """The panel BEFORE any carrier feature, and everything measured making it.

    The blank, the derived outline, the merged saw passes and the flattened
    copper - the four things :func:`stepped` does - and nothing a config's
    carrier sections asked for. It exists because that half is the expensive
    half and does not depend on the carrier at all: a tooling hole diameter
    moves nothing above this line, so a caller that knows the board and the
    layout have not moved may keep one of these and run only :func:`carrier`
    against it (`preview.panelbuild`).

    Nothing here holds the panel BOARD. What it holds is measurements and pure
    geometry, so one of these can be used against a copy of the board it was
    measured from without two callers sharing anything they could change.
    """

    blank: channels.Rect
    instance_bbox: tuple
    rails: channels.Rails
    strip: OutlineStrip
    zones: zones.ZoneFlatten
    channel_runs: int
    channel_items: int
    channel_length_mm: float
    channel_dust: int
    worst_dust_mm: float
    boundary: channels.Boundary
    outline_width_mm: float
    scores: ScoreMerge
    warnings: tuple
    min_tab_mm: float
    joins: tuple
    protected_tabs: tuple
    # The derived boundary's pieces, flattened - what the carrier features are
    # measured against. Kept here because the panel no longer says it: by the
    # time a hole asks how close it came to an edge, the boundary walk is over.
    outline: tuple
    # EVERY CHANNEL THE BOOLEAN WAS GIVEN: the toolpath's own runs, the
    # minimum-tab crossings and any pinch cut. The board only holds the first of
    # the three, so a check that measures this outline against the material it
    # was derived from has to be handed the set - see
    # :mod:`eedesignpanels.outline_check`.
    cut_channels: tuple = ()
    # Where two cutter passes touched and the material had to be cut through.
    pinches: tuple = ()
    # THE PASSES THE PANEL ADDED TO ITS OWN TOOLPATH LAYER, and the ink they
    # became. `cut_channels` above is now exactly what that layer says, because
    # these were written to it before the outline was derived.
    added_channels: tuple = ()
    added_items: tuple = ()
    # Every ribbon of thin material left standing, and the switch that left it.
    eliminate_slivers: bool = True
    standing_slivers: tuple = ()
    # WHAT THE RAIL SCORE SETBACK GREW THIS BLANK BY. Held so a caller can say
    # why the panel is bigger than the
    # rails alone make it, and so a cached stepped panel can be seen to have
    # been measured for these two numbers and not some others.
    score_setbacks: object = panel_scores_stage.Setbacks()

    def result(self, warnings, holes=None, panel_scores=None,
               rail_copper=None, dimensions=None,
               fiducials=None, jumps=None) -> "PanelizeResult":
        """This half plus the carrier half, as ONE reported result.

        One place builds a :class:`PanelizeResult`, so the split cannot make a
        panel that reports differently from the panel it used to report as.
        """
        return PanelizeResult(
            blank=self.blank,
            instance_bbox=self.instance_bbox,
            rails=self.rails,
            strip=self.strip,
            zones=self.zones,
            channel_runs=self.channel_runs,
            channel_items=self.channel_items,
            channel_length_mm=self.channel_length_mm,
            channel_dust=self.channel_dust,
            worst_dust_mm=self.worst_dust_mm,
            boundary=self.boundary,
            outline_width_mm=self.outline_width_mm,
            scores=self.scores,
            warnings=warnings,
            min_tab_mm=self.min_tab_mm,
            joins=self.joins,
            protected_tabs=self.protected_tabs,
            cut_channels=self.cut_channels,
            pinches=self.pinches,
            added_channels=self.added_channels,
            added_items=self.added_items,
            eliminate_slivers=self.eliminate_slivers,
            standing_slivers=self.standing_slivers,
            holes=holes,
            panel_scores=panel_scores,
            rail_copper=rail_copper,
            dimensions=dimensions,
            fiducials=fiducials,
            jumps=jumps,
        )


class PanelizeResult(NamedTuple):
    """One panelize pass, measured. Every number here is reported, not assumed."""

    blank: channels.Rect
    instance_bbox: tuple
    rails: channels.Rails
    strip: OutlineStrip
    zones: zones.ZoneFlatten
    channel_runs: int
    channel_items: int
    channel_length_mm: float
    # Splinters of cutter path too short to be geometry, dropped on the way in
    # (channels.MIN_ITEM_MM), and the longest one that went.
    channel_dust: int
    worst_dust_mm: float
    boundary: channels.Boundary
    outline_width_mm: float
    scores: ScoreMerge
    warnings: list
    # THE MINIMUM TAB: what it was set to, the accidental slivers it consumed,
    # and every declared tab it would have eaten and did not.
    min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM
    joins: tuple = ()
    protected_tabs: tuple = ()
    # Every channel the boolean was given, and every place the material was
    # severed and had to be cut through. Carried so a check can measure this
    # outline against the material it came from - the board holds the toolpath
    # and nothing else (:mod:`eedesignpanels.outline_check`).
    cut_channels: tuple = ()
    pinches: tuple = ()
    # What the panel added to its OWN toolpath layer so that the outline below
    # is the outline of that layer's ink.
    added_channels: tuple = ()
    added_items: tuple = ()
    eliminate_slivers: bool = True
    standing_slivers: tuple = ()
    # THE CARRIER FEATURES, each None when the config did not ask for it. A
    # None here is the stage not having run, which is why they are reported as
    # objects rather than as counts that would read as zero either way.
    holes: object = None
    panel_scores: object = None
    rail_copper: object = None
    # The panel's own dimensions. None only when the config switched them off:
    # a panel says how big it is unless somebody said not to.
    dimensions: object = None
    # The panel's optical targets. None when no `fiducials` section asked for
    # them.
    fiducials: object = None
    # JUMP SCORING. None when the `panel.scores` group is off, because the
    # two distances live in that section - so a panel with no
    # section reads as "nobody said", which is not the same sentence as "asked
    # for zero".
    jumps: object = None

    @property
    def loops(self) -> int:
        return self.boundary.loop_count

    @property
    def outlines_removed(self) -> int:
        """Every `Edge.Cuts` item removed, wherever it lived."""
        return self.strip.total

    @property
    def blank_extent_mm(self) -> tuple:
        return self.blank.extent_mm

    @property
    def min_tab_clear(self) -> bool:
        """No declared tab in the panel is narrower than the minimum tab."""
        return not self.protected_tabs


def blank_rails(rails, score_setbacks=None) -> channels.Rails:
    """The four widths the BLANK is actually grown by: the rails plus the setback.

    **THE RAIL SCORE SETBACK GROWS THE PANEL.** The setback is a buffer
    added to the board: each side gains that much plain material, so the panel
    grows by twice it on that axis and the rail keeps the dimension the user
    typed - panel edge to rail score.

    WHICH SETBACK GROWS WHICH WAY IS EASY TO SWAP, so it is said plainly. `Rail
    scores, horizontal` are the passes along the TOP AND BOTTOM rails, so the
    HORIZONTAL setback grows the panel VERTICALLY - it is added to `top` and
    `bottom`. The VERTICAL setback grows it ACROSS, into `left` and `right`.

    A setback whose own rail score is off is zero before it gets here
    (:attr:`panel_scores.Settings.governed`), so a panel with the rail scores
    switched off does not grow at all - a greyed setting contributes nothing.
    """
    sides = channels.Rails.of(rails)
    back = panel_scores_stage.Setbacks.of(score_setbacks)
    if not back.asked:
        return sides
    return sides._replace(
        top=sides.top + back.horizontal_mm,
        bottom=sides.bottom + back.horizontal_mm,
        left=sides.left + back.vertical_mm,
        right=sides.right + back.vertical_mm,
    )


def blank_extent_mm(instance_extent_mm: tuple, rails,
                    score_setbacks=None) -> tuple:
    """How big the blank is, from the grid extent and the rails. One rule.

    The stage grows a measured bounding box and the review predicts an extent;
    both come through here, so the panel's size cannot be derived twice and
    disagree. *rails* is a :class:`channels.Rails` or one number for all four.

    *score_setbacks* is what the rail scores add to the panel
    (:func:`blank_rails`). None is no setback.
    """
    sides = blank_rails(rails, score_setbacks)
    return (
        instance_extent_mm[0] + sides.left + sides.right,
        instance_extent_mm[1] + sides.top + sides.bottom,
    )


# ---------------------------------------------------------------------------
# reading the panel
# ---------------------------------------------------------------------------


def primitives_of(item, source: str = "") -> list:
    """One board drawing as the offsettable primitives it contributes.

    Every shape KiCad accepts on a board, through the one
    catalogue in `eedesignpanels.outline_shapes` - the same one
    `stages/route_toolpath.py` reads, because a shape this stage refuses and
    that one accepts just moves the hole. A rectangle is four runs, a circle
    two, so this returns a LIST.

    An arc whose three stored points are collinear is read as the line between
    its ends: it is what the file says, and a circumcircle through three
    collinear points is not.
    """
    return shape_primitives(item, source=source)


def primitive_of(item, source: str = ""):
    """The single primitive a line or an arc contributes. Lines and arcs only.

    Kept for the callers that measure a layer THIS TOOL writes, where a shape
    is a line or an arc by construction. Anything else goes through
    :func:`primitives_of`.

    Raises:
        ValueError: for any shape that is not one run of a boundary.
    """
    runs = primitives_of(item, source)
    if len(runs) != 1:
        kind = type(item).__name__
        raise ValueError(
            f"the panel holds a {kind} on a layer this stage measures one run "
            f"at a time, and it covers {len(runs)} runs of the boundary. Read "
            f"it with primitives_of instead"
        )
    return runs[0]


def board_items_on(board: Board, layer: str) -> list:
    """Every BOARD-LEVEL drawing on *layer*, in file order.

    Footprint-hosted items are deliberately not here: they belong to the
    footprint library, and this stage owns the board level only.
    """
    return items_on_layers((*board.drawings, *board.trailing_drawings), {layer})


def footprint_items_on(board: Board, layer: str) -> list:
    """Every FOOTPRINT-hosted drawing on *layer*, in footprint order."""
    return [
        item
        for footprint in board.footprints
        for item in items_on_layers(footprint.drawings, {layer})
    ]


def instance_outline_bbox(board: Board) -> tuple:
    """The extent of the instances' own outlines, arcs measured exactly.

    Board-level `Edge.Cuts` plus every footprint's, in board coordinates - the
    same whole-outline rule prep offsets from, so the blank is grown from the
    shape the boards actually are.
    """
    from eedesignpanels.stages.route_toolpath import outline_primitives

    return chain_bbox(outline_primitives(board))


def channels_of(board: Board, warnings: list) -> channels.RunSet:
    """The routed channels: every board-level `User.36` item, chained into runs.

    The stroke width IS the cutter diameter (prep writes it that way), so the
    ribbon each item removes is itself dilated by half of it.
    """
    from eedesignpanels.rigid_flex import route_layer
    layer = route_layer(board)
    items = board_items_on(board, layer)
    if not items:
        warnings.append(
            f"the panel has no {layer} geometry, so nothing is routed "
            f"out of the material and the panel outline is the bare rectangle"
        )
        return channels.RunSet(channels=(), dropped=0, worst_dropped_mm=0.0)
    paths = []
    for item in items:
        width = item.stroke.width if item.stroke is not None else 0.0
        if width <= 0.0:
            raise ValueError(
                f"a {layer} item has no stroke width, so how much "
                f"material it removes is not written down anywhere"
            )
        paths.append((primitive_of(item, item.id or ""), width / 2.0))
    return channels.chain_runs(paths)


def outline_width_of(items: list) -> float:
    """The stroke the panel outline is drawn at: what the instances used.

    The commonest width among the outlines being replaced, so the panel is
    drawn the way the design was. Ties go to the thinner one, and a panel with
    no outline to learn from takes KiCad's own default.
    """
    counts: dict = {}
    for item in items:
        if item.stroke is None:
            continue
        counts[item.stroke.width] = counts.get(item.stroke.width, 0) + 1
    if not counts:
        return DEFAULT_OUTLINE_WIDTH_MM
    return min(counts, key=lambda width: (-counts[width], width))


# ---------------------------------------------------------------------------
# writing the panel
# ---------------------------------------------------------------------------


def _coordinate(value: float) -> str:
    """One millimetre coordinate, at the file's own precision, sign-normalised."""
    rounded = round(value, 6)
    return f"{rounded + 0.0:.6f}" if rounded else "0.000000"


def edge_key(piece) -> str:
    """A boundary item's identity, derived from the geometry and nothing else.

    Not from its position in a list: the arrangement is walked in whatever
    order the crossings came out, and an identity that depended on that would
    change when nothing about the panel had.
    """
    if isinstance(piece, Line):
        points = (piece.p0, piece.p1)
        kind = "line"
    else:
        points = (piece.start, piece.mid, piece.end)
        kind = "arc"
    return "|".join(
        [kind] + [_coordinate(value) for point in points for value in point]
    )


def to_board_items(loops, width: float, layer: str = OUTLINE_LAYER) -> list:
    """Every loop of the boundary as stroked lines and true arcs."""
    items = []
    for loop in loops:
        for piece in loop:
            stroke = Stroke(width=width)
            identity = str(uuid.uuid5(EDGE_NAMESPACE, edge_key(piece)))
            if isinstance(piece, Line):
                items.append(PcbLine(
                    start=piece.p0, end=piece.p1, layer=layer,
                    stroke=stroke, id=identity,
                ))
            else:
                items.append(PcbArc(
                    start=piece.start, mid=piece.mid, end=piece.end,
                    layer=layer, stroke=stroke, id=identity,
                ))
    return items


def to_toolpath_items(added, layer: str = TOOLPATH_LAYER) -> list:
    """Every pass the panel had to add, as toolpath ink at the cutter's width.

    THE FIRST HALF OF THE INVARIANT. A minimum-tab crossing and a pinch cut are
    passes of a real bit, and until they are on this layer the outline derived
    from them is an outline no program cuts. The stroke width IS the cutter
    diameter, exactly as prep writes
    it, so the panel reads its own additions back the same way it reads the
    boards'.

    Identities come from the geometry and the width, never from position in a
    list: the passes are computed from an arrangement, and a panel built twice
    has to name them the same both times.
    """
    items = []
    for channel in added:
        width = channel.radius * 2.0
        for piece in channel.path:
            stroke = Stroke(width=width)
            identity = str(uuid.uuid5(
                TOOLPATH_NAMESPACE, f"{edge_key(piece)}|{_coordinate(width)}"
            ))
            if isinstance(piece, Line):
                items.append(PcbLine(
                    start=piece.p0, end=piece.p1, layer=layer,
                    stroke=stroke, id=identity,
                ))
            else:
                items.append(PcbArc(
                    start=piece.start, mid=piece.mid, end=piece.end,
                    layer=layer, stroke=stroke, id=identity,
                ))
    return items


def clear_board_layer(board: Board, layer: str, keep=None) -> int:
    """Delete board-level drawings on *layer*. Returns how many went.

    `keep` is a predicate for items to leave behind. Footprint-level items are
    never touched: this stage owns the board level and the footprint library
    owns its own.
    """
    def drop(item) -> bool:
        if layer not in item_layers(item):
            return False
        return keep is None or not keep(item)

    before = len(board.drawings) + len(board.trailing_drawings)
    board.drawings = [item for item in board.drawings if not drop(item)]
    board.trailing_drawings = [
        item for item in board.trailing_drawings if not drop(item)
    ]
    return before - len(board.drawings) - len(board.trailing_drawings)


def strip_outline(board: Board, warnings: list) -> OutlineStrip:
    """Remove EVERY `Edge.Cuts` item on the panel, wherever it lives.

    Board level and inside every footprint, on every build. The panel's outline
    is derived from the toolpaths and the carrier, and anything else on the
    layer is a cut the fabricator was not asked to make.

    Only `Edge.Cuts` drawings leave a footprint: its pads, its other graphics,
    its fields, its zones and its own placement are not touched, so a footprint
    that hosted no outline comes through byte-identical.
    """
    kinds: dict = {}
    removed: set = set()
    multi = 0

    def drop(item) -> bool:
        nonlocal multi
        layers = item_layers(item)
        if OUTLINE_LAYER not in layers:
            return False
        kinds[type(item).__name__] = kinds.get(type(item).__name__, 0) + 1
        identity = getattr(item, "id", None)
        if identity is not None:
            removed.add(identity)
        if len(layers) > 1:
            multi += 1
        return True

    before = len(board.drawings) + len(board.trailing_drawings)
    board.drawings = [item for item in board.drawings if not drop(item)]
    board.trailing_drawings = [
        item for item in board.trailing_drawings if not drop(item)
    ]
    board_items = before - len(board.drawings) - len(board.trailing_drawings)

    footprint_items = touched = 0
    for footprint in board.footprints:
        kept = [item for item in footprint.drawings if not drop(item)]
        gone = len(footprint.drawings) - len(kept)
        if gone:
            footprint.drawings = kept
            footprint_items += gone
            touched += 1

    pruned = dropped = 0
    board.groups, group_pruned, group_dropped = prune_members(board.groups, removed)
    pruned, dropped = pruned + group_pruned, dropped + group_dropped
    board.generators, gen_pruned, gen_dropped = prune_members(
        board.generators, removed
    )
    pruned, dropped = pruned + gen_pruned, dropped + gen_dropped
    for footprint in board.footprints:
        footprint.groups, fp_pruned, fp_dropped = prune_members(
            footprint.groups, removed
        )
        pruned, dropped = pruned + fp_pruned, dropped + fp_dropped

    if multi:
        warnings.append(
            f"{multi} removed item(s) named {OUTLINE_LAYER} alongside another "
            f"layer, so their geometry left that layer too - a shape drawn on "
            f"two layers at once cannot be removed from one of them"
        )
    if dropped:
        warnings.append(
            f"{dropped} group(s) or generator(s) held nothing but "
            f"{OUTLINE_LAYER} items and were dropped with them"
        )

    return OutlineStrip(
        board_items=board_items,
        footprint_items=footprint_items,
        footprints_touched=touched,
        by_kind=tuple(sorted(kinds.items())),
        multi_layer=multi,
        groups_pruned=pruned,
        groups_dropped=dropped,
    )


# ---------------------------------------------------------------------------
# merging the instances' v-scores into panel passes
# ---------------------------------------------------------------------------


def _canonical_unit(start: tuple, end: tuple) -> tuple:
    """The line's direction, pointed the same way however it was drawn.

    A score is a cut, not a journey: two designers drawing one blade line in
    opposite directions drew the same pass, so the direction is normalised
    before anything is compared or written.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        raise ValueError("a score line of no length has no direction")
    unit = (dx / length, dy / length)
    if unit[0] < 0.0 or (unit[0] == 0.0 and unit[1] < 0.0):
        return (-unit[0], -unit[1])
    return unit


def _on_same_blade(anchor: tuple, unit: tuple, start: tuple, end: tuple,
                   other_unit: tuple) -> bool:
    """Is this line the same saw pass as the one through *anchor*?"""
    if abs(unit[0] * other_unit[1] - unit[1] * other_unit[0]) > BLADE_PARALLEL_TOL:
        return False
    return all(
        abs((point[0] - anchor[0]) * unit[1] - (point[1] - anchor[1]) * unit[0])
        <= COLLINEAR_TOL_MM
        for point in (start, end)
    )


def blade_lines(lines: list, blank: channels.Rect) -> list:
    """Group score lines onto blade passes and produce each to the blank's edge.

    One entry per blade line, in a fixed order. The extent is the blank's,
    because a saw enters at one edge of the panel and leaves at the other - the
    first-pass rule, and the one a runway calculation will later refine.
    """
    groups: list = []
    for item in lines:
        unit = _canonical_unit(item.start, item.end)
        for group in groups:
            if _on_same_blade(group["anchor"], group["unit"],
                              item.start, item.end, unit):
                group["items"].append(item)
                break
        else:
            groups.append({"anchor": item.start, "unit": unit, "items": [item]})

    blades = []
    for group in groups:
        unit = group["unit"]
        anchor = group["anchor"]
        span = clip_to_bbox(anchor, unit, blank.bbox)
        if span is None:
            # A blade line outside the blank cuts nothing. Left at its own
            # extent rather than dropped, so it can be seen and questioned.
            first = group["items"][0]
            low, high = 0.0, math.dist(first.start, first.end)
        else:
            low, high = span
        blades.append(BladeLine(
            start=(anchor[0] + unit[0] * low, anchor[1] + unit[1] * low),
            end=(anchor[0] + unit[0] * high, anchor[1] + unit[1] * high),
            width_mm=_common_width(group["items"], DEFAULT_SCORE_WIDTH_MM),
            sources=tuple(item.id for item in group["items"] if item.id),
        ))
    blades.sort(key=lambda blade: (blade.start, blade.end))
    return blades


def _common_width(items: list, fallback: float) -> float:
    counts: dict = {}
    for item in items:
        if item.stroke is not None:
            counts[item.stroke.width] = counts.get(item.stroke.width, 0) + 1
    if not counts:
        return fallback
    return min(counts, key=lambda width: (-counts[width], width))


def score_alignment_warnings(copies, placements):
    """Warn when scores along a panel row or column do not align."""
    mismatches = set()
    for index, lines in enumerate(copies):
        for line in lines:
            if math.dist(line.start, line.end) <= COLLINEAR_TOL_MM:
                continue
            unit = _canonical_unit(line.start, line.end)
            horizontal = abs(unit[0]) >= abs(unit[1])
            for other, placed in enumerate(placements):
                if index == other:
                    continue
                first = placements[index]
                if horizontal and first.row != placed.row:
                    continue
                if not horizontal and first.column != placed.column:
                    continue
                if any(
                    math.dist(candidate.start, candidate.end) > COLLINEAR_TOL_MM
                    and _on_same_blade(
                        line.start, unit, candidate.start, candidate.end,
                        _canonical_unit(candidate.start, candidate.end),
                    )
                    for candidate in copies[other]
                ):
                    continue
                mismatches.add(tuple(sorted((index, other))))
    if not mismatches:
        return []

    def location(index):
        placed = placements[index]
        return f"row {placed.row + 1}, column {placed.column + 1}"

    examples = ". ".join(
        f"{location(first)} and {location(second)}"
        for first, second in sorted(mismatches)[:3]
    )
    return [
        "V-score alignment warning: scores on repeated boards do not share "
        f"one straight saw path. Affected board pairs include {examples}. "
        "Check the source score angle and panel placement. The score lines were not adjusted."
    ]


def merge_scores(board: Board, blank: channels.Rect, warnings: list) -> ScoreMerge:
    """Replace the instances' score lines with one line per blade pass."""
    items = board_items_on(board, SCORE_LAYER)
    lines = [item for item in items if type(item).__name__ == "PcbLine"]
    others = [item for item in items if type(item).__name__ != "PcbLine"]
    if others:
        kinds = ", ".join(sorted({type(item).__name__ for item in others}))
        warnings.append(
            f"{len(others)} item(s) on {SCORE_LAYER} are not lines ({kinds}), "
            f"so they are left exactly as they are and merge with nothing - a "
            f"v-score is a straight blade travelling in a straight line"
        )
    if not lines:
        return ScoreMerge(blades=(), replaced=0, kept=len(others))

    blades = blade_lines(lines, blank)
    replaced = clear_board_layer(
        board, SCORE_LAYER, keep=lambda item: type(item).__name__ != "PcbLine"
    )
    for blade in blades:
        board.drawings.append(PcbLine(
            start=blade.start,
            end=blade.end,
            layer=SCORE_LAYER,
            stroke=Stroke(width=blade.width_mm),
            id=str(uuid.uuid5(SCORE_NAMESPACE, "|".join(sorted(blade.sources)))),
        ))
    return ScoreMerge(blades=tuple(blades), replaced=replaced, kept=len(others))


# ---------------------------------------------------------------------------
# jump scoring: stopping a merged pass short of the panel edge
# ---------------------------------------------------------------------------


def jump_axis(start: tuple, end: tuple):
    """Which of the two distances this line's direction asks for, or None.

    THE DISTANCE BELONGS TO THE LINE. A line that
    runs left to right is a horizontal pass and takes the horizontal distance; a
    line that runs top to bottom is a vertical pass and takes the vertical one.
    A line running down neither axis takes neither: a v-score saw runs along an
    axis, and a diagonal pass is unusual enough to be named rather than assigned
    to whichever axis it leans towards.
    """
    unit = _canonical_unit(start, end)
    if abs(unit[0]) <= BLADE_PARALLEL_TOL:
        return "vertical"
    if abs(unit[1]) <= BLADE_PARALLEL_TOL:
        return "horizontal"
    return None


def _endpoints(item) -> tuple:
    """A drawing's two ends as plain float pairs, for comparing by value."""
    return (
        (float(item.start[0]), float(item.start[1])),
        (float(item.end[0]), float(item.end[1])),
    )


def trim_jumps(board: Board, blades, horizontal_mm: float, vertical_mm: float,
               warnings=None) -> JumpScoring:
    """Stop every merged board-defined saw pass short of the panel edge.

    The industry name for this is jump scoring. The
    saw lifts before the frame, so the panel keeps a continuous rim of material
    and stays rigid on the assembly line. Two distances, separable, each one
    applied at BOTH ends of a pass that runs along its axis.

    **WHAT IT TOUCHES AND WHAT IT DOES NOT.** Only the passes in *blades* - the
    panel-spanning lines :func:`merge_scores` made out of v-score decisions
    living in the BOARDS. Not the panel's own families of pass, which run down
    the gaps and along the rail edges and are a different decision; and nothing
    on the toolpath layer, because a score line is not a cutter pass.

    **THE KEEP-OUT FOLLOWS THE SHORTENED LINE.** The guard belongs to the cut,
    so a pass that stops short is guarded
    over the run the saw makes and no further. Nothing here draws or edits a
    rule area: this runs BEFORE :func:`panel_scores.apply`, which reads the
    `User.38` lines off the panel as they then stand, and one mechanism reaching
    the right answer beats two agreeing with each other.

    **IT REPLACES, IT DOES NOT MUTATE.** The panel this runs on may be a cheap
    copy sharing its items with a panel somebody else is holding
    (`step_and_repeat.cheap_copy`), so a shortened pass is a NEW drawing put in
    the old one's place in the list. The list belongs to the copy; the item did
    not. The uuid rides along unchanged, because a pass that stops short is the
    same pass and not a second one.

    Both distances zero does nothing at all, and the panel is byte-identical to
    a panel with no jump scoring.
    """
    warnings = warnings if warnings is not None else []
    scoring = JumpScoring(horizontal_mm=float(horizontal_mm),
                          vertical_mm=float(vertical_mm))
    if not scoring.asked or not blades:
        return scoring

    wanted = {_endpoints(blade): blade for blade in blades}
    distances = {
        "horizontal": scoring.horizontal_mm,
        "vertical": scoring.vertical_mm,
    }
    trimmed, declined = [], []
    for held in (board.drawings, board.trailing_drawings):
        for index, item in enumerate(held):
            if type(item).__name__ != "PcbLine":
                continue
            if SCORE_LAYER not in item_layers(item):
                continue
            span = _endpoints(item)
            if span not in wanted:
                continue
            axis = jump_axis(*span)
            if axis is None:
                declined.append(
                    f"the saw pass from ({span[0][0]:.4f}, {span[0][1]:.4f}) to "
                    f"({span[1][0]:.4f}, {span[1][1]:.4f}) mm runs along neither "
                    f"axis, so neither jump distance describes it and it was "
                    f"left running edge to edge"
                )
                continue
            distance = distances[axis]
            if distance <= 0.0:
                continue
            shortened = jumped_span(*span, distance)
            if shortened is None:
                declined.append(
                    f"the {axis} saw pass at ({span[0][0]:.4f}, "
                    f"{span[0][1]:.4f}) mm is {math.dist(*span):.2f} mm long and "
                    f"a {distance:g} mm jump at each end would leave under "
                    f"{MIN_JUMPED_PASS_MM:g} mm of cut, so it was left running "
                    f"edge to edge"
                )
                continue
            held[index] = item.model_copy(
                update={"start": shortened[0], "end": shortened[1]}
            )
            trimmed.append(JumpedPass(axis=axis, distance_mm=distance,
                                      was=span, now=shortened))
    for sentence in declined:
        warnings.append(sentence)
    return scoring._replace(trimmed=tuple(trimmed), declined=tuple(declined))


# ---------------------------------------------------------------------------
# the stage
# ---------------------------------------------------------------------------


def channel_guards(board: Board) -> tuple:
    """The gaps in the panel's toolpath that the instances' DESIGNS declare.

    The tabs, read off the panel exactly the way prep reads them off the source
    board: every staple on `User.8`, wherever its footprint ended up. Six
    instances of a four-tab board declare twenty-four tabs, and each one's two
    stop lines are what tells a gap the designer asked for from a sliver the
    arrangement happened to leave.

    Nothing here reads a run, an instance key or a placement: a tab is
    declared geometry, and this stage recognises it as geometry.
    """
    from eedesignpanels.stages.route_toolpath import tabs_of

    return mintab.tab_guards(tabs_of(board))


def pinch_warning(points) -> str:
    """One line naming every place the material was severed and had to be cut.

    Loud rather than silent: a pinch means two cutter passes touched, which is
    usually a feature narrower than the bit somebody chose, and the user
    wants to know that the panel was rescued rather than that it built.
    """
    first = points[0]
    return (
        f"{len(points)} place(s) where two cutter passes touch, leaving "
        f"material of ZERO width - the first at ({first[0]:.4f}, "
        f"{first[1]:.4f}) mm. Each was cut through, because material with no "
        f"width is under any minimum tab; a bit this size cannot make that "
        f"feature any other way"
    )


def apply(board: Board, rails,
          min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM,
          tooling_holes=None, scores=None, rail_copper=None,
          dimensions=None, fiducials=None,
          instance_boxes=(), source_outline=(), placements=(),
          fill_rails: bool = True,
          cli_path=None, work_dir=None,
          eliminate_slivers=None, score_setbacks=None) -> PanelizeResult:
    """Panelize *board* in place: blank, outline, scores, zones. Reports it all.

    The whole stage in one call, and the two halves it is made of are
    :func:`stepped` and :func:`carrier`. Nothing about the panel depends on
    which of the two entries a caller used - this one is the plain answer for a
    caller with one board and one config, and the split is for the live preview,
    which re-runs the carrier half against a panel it already has.

    *rails* is a :class:`channels.Rails` - four widths - or one number meaning
    all four sides. Four equal rails are today's panel, to the byte; four
    different ones move only where the blank's edges sit.

    *score_setbacks* is what the rail scores add to the panel's size. The
    setback GROWS THE PANEL, it does not move the saw line. It belongs to the
    blank, so it is taken here and not with the rest of
    the scores section - see :func:`blank_rails`.

    Order matters once. The instances' outlines are MEASURED first - the blank
    is grown from the shape the boards actually are, break contours included -
    and stripped afterwards, so the blank does not depend on when the strip
    happens to run.

    `min_tab_mm` is the minimum tab, enforced between the channels before the
    boolean: see :func:`channels.close_slivers`.

    THE CARRIER FEATURES run last, after the panel's own outline exists,
    because each of them is measured against it: a tooling hole is judged by
    how close it came to an edge, a score is judged by which boards it crosses,
    and a rail pour is held off the edges by its clearance. Each runs only when
    its settings are given, and each does nothing at all when they are not - so
    a config written before they existed builds exactly the panel it built
    before, to the byte. **The one thing that is not on a switch** is the DRC
    rule area round a score line: a panel carrying passes its BOARDS decided
    gets its keepouts whichever way `panel.scores.enabled` is set
    (`panel_scores.protect`).

    *instance_boxes* are the individual board rectangles, which the caller
    knows and the panel no longer does: the instances' outlines are gone by the
    time the features run. Without them a panel score cannot say which board it
    crosses and the pour's keep-out cannot find the boards.

    *source_outline* and *placements* are the same fact told exactly: the SOURCE
    board's `Edge.Cuts` primitives in its own frame, and where every copy of it
    sits. They are what lets the rail pour be held off each board by the board's
    OWN SHAPE rather than by its rectangle. Both
    are measured by `step_and_repeat.panel_geometry`; omitting them costs the
    shape and nothing else.

    Raises:
        ValueError: when there is no instance outline to grow a blank from,
            when a layer this stage measures holds a shape it cannot read, when
            a footprint holds a zone with a fill record, when
            `min_tab_mm` is under the floor the fab can hold, when a tooling
            hole would be centred off the panel, or when a score position is.
        channels.ChannelError: when the channels do not resolve into a closed
            boundary, which means the panel's edge would have a gap in it.
        refill.RefillError: when a rail pour was asked for and KiCad could not
            fill it, or the fill could not be trusted.
    """
    return carrier(
        board, stepped(board, rails, min_tab_mm,
                       eliminate_slivers=eliminate_slivers,
                       score_setbacks=score_setbacks),
        tooling_holes=tooling_holes, scores=scores, rail_copper=rail_copper,
        dimensions=dimensions, fiducials=fiducials,
        instance_boxes=instance_boxes, source_outline=source_outline,
        placements=placements,
        fill_rails=fill_rails, cli_path=cli_path, work_dir=work_dir,
    )


def stepped(board: Board, rails,
            min_tab_mm: float = mintab.MIN_TAB_DEFAULT_MM,
            eliminate_slivers=None, score_setbacks=None) -> SteppedPanel:
    """THE FIRST HALF: blank, derived outline, merged saw passes, flat copper.

    Everything panelizing does that a carrier section cannot change, done to
    *board* in place. What comes back is a :class:`SteppedPanel` - measurements
    and pure geometry, holding no reference to the board - so a caller may run
    :func:`carrier` against this board or against a copy of it.

    **The order here IS the information flow**: the
    toolpath layer is read, every pass the panel has to add is decided, those
    passes are WRITTEN TO THAT LAYER, and only then is `Edge.Cuts` derived from
    the boundary of the blank minus all of it. The outline comes out of the ink,
    not out of a channel list that existed beside it.

    `eliminate_slivers` false leaves the webs between neighbouring instances
    standing and names each one. None takes the answer from `min_tab_mm`
    (:class:`mintab.Rule`), which is how the config's switch reaches here.

    **`score_setbacks` IS WHY A SCORE SETTING REACHES THIS HALF AT ALL.** The
    rail score setback GROWS THE PANEL rather
    than moving the saw line, so it is a fact about the blank and the blank is
    measured here. The rails themselves stay the widths the user typed -
    `SteppedPanel.rails` carries those, and the score lands on them unchanged -
    and only what the blank is grown by knows about the setback
    (:func:`blank_rails`).

    Raises what :func:`apply` raises, minus the carrier features' own failures.
    """
    warnings: list = []
    min_tab_mm = mintab.checked_min_tab(min_tab_mm, eliminate_slivers)
    rails = channels.Rails.of(rails)
    setbacks = panel_scores_stage.Setbacks.of(score_setbacks)
    instance_bbox = instance_outline_bbox(board)
    blank = channels.grown(instance_bbox, blank_rails(rails, setbacks))

    runs = channels_of(board, warnings)
    derived = channels.derived_boundary(
        blank, list(runs.channels), min_tab_mm, guards=channel_guards(board),
        eliminate=min_tab_mm.eliminate,
    )
    # THE PASSES LAND ON THE TOOLPATH LAYER FIRST. Everything below derives from
    # them, so they are ink before they are an edge.
    from eedesignpanels.rigid_flex import route_layer
    added_items = to_toolpath_items(derived.added, layer=route_layer(board))
    board.drawings.extend(added_items)
    enforced = derived
    for entry in derived.protected:
        warnings.append(mintab.protection_warning(entry))
    note = mintab.join_warning(derived.joins, min_tab_mm)
    if note:
        warnings.append(note)
    for ribbon in derived.standing:
        warnings.append(mintab.sliver_warning(ribbon, min_tab_mm))
    if derived.pinches:
        warnings.append(pinch_warning(derived.pinches))
    boundary = derived.boundary
    if boundary.reaches_outer_edge:
        warnings.append(
            f"a routed channel reaches the panel's outer edge (clearance "
            f"{boundary.clearance_mm:.4f} mm), so the outer edge is broken by "
            f"the route there - widen the rail if the frame is meant to be "
            f"continuous"
        )

    width = outline_width_of(board_items_on(board, OUTLINE_LAYER))
    strip = strip_outline(board, warnings)
    board.drawings.extend(to_board_items(boundary.loops, width))

    flattened = zones.apply(board, warnings)
    merged = merge_scores(board, blank, warnings)

    return SteppedPanel(
        blank=blank,
        instance_bbox=instance_bbox,
        rails=rails,
        strip=strip,
        zones=flattened,
        channel_runs=len(runs.channels),
        channel_items=runs.item_count,
        channel_length_mm=runs.length_mm,
        channel_dust=runs.dropped,
        worst_dust_mm=runs.worst_dropped_mm,
        boundary=boundary,
        outline_width_mm=width,
        scores=merged,
        warnings=tuple(warnings),
        min_tab_mm=min_tab_mm,
        joins=enforced.joins,
        protected_tabs=enforced.protected,
        outline=tuple(piece for loop in boundary.loops for piece in loop),
        cut_channels=derived.channels,
        pinches=derived.pinches,
        added_channels=derived.added,
        added_items=tuple(added_items),
        eliminate_slivers=bool(min_tab_mm.eliminate),
        standing_slivers=derived.standing,
        score_setbacks=setbacks,
    )


def carrier(board: Board, base: SteppedPanel,
            tooling_holes=None, scores=None, rail_copper=None,
            dimensions=None, fiducials=None,
            instance_boxes=(), source_outline=(), placements=(),
            fill_rails: bool = True,
            cli_path=None, work_dir=None,
            score_half_width_mm: float =
            panel_scores_stage.DEFAULT_RULE_HALF_WIDTH_MM,
            text_settings=None, text_content="", text_warning="") -> PanelizeResult:
    """THE SECOND HALF: the carrier features, onto a panel that already exists.

    *base* is what :func:`stepped` measured. *board* is the panel to put the
    features on, which may be the board `stepped` ran on or a copy of it - the
    features only ADD items, and everything they measure against comes from
    *base* rather than from the board, so a copy is as good as the original.
    That is what lets the live preview re-run this half alone
    (`preview.panelbuild`).

    *score_half_width_mm* is the ONE keep-out width this config declares
    (`PanelConfig.score_rule_half_width_mm`, a PREP setting). It overrides
    whatever a scores section carries,
    because the panel has to guard a line at exactly the width the BOARD guarded
    its own - the board's keepout steps in with its instance and is recognised
    by measurement.

    *source_outline* and *placements* are the source board's whole `Edge.Cuts`
    geometry in its own frame and where every copy of it sits
    (`step_and_repeat.PanelGeometry`). The rail pour's keep-out is the board's
    own shape stepped and repeated, so both are needed to draw it; without them
    it falls back to each board's rectangle and says so.

    Each stage runs only when its settings are given. The warnings come back
    with the first half's first, in the order one panel build produced them.
    """
    warnings = list(base.warnings)
    blank, instance_bbox = base.blank, base.instance_bbox
    carrier_footprints_start = len(board.footprints)

    holes = None
    if tooling_holes is not None:
        # THE SETBACKS GO WITH THE HOLES. A `rail_corner_center` hole belongs
        # in the middle of the
        # rail band measured from the FINISHED panel edge, and the blank alone
        # cannot say where that band starts - the setback grew the blank without
        # moving the rail's own width. Handed the two numbers, the hole's
        # distance to the panel edge is the same whatever the setback is; the
        # fiducials follow because each is offset from its hole.
        holes = tooling_holes_stage.apply(
            board, blank, instance_bbox, tooling_holes,
            outline=list(base.outline), warnings=warnings,
            score_setbacks=base.score_setbacks,
        )
    panel_scores = None
    jumps = None
    if scores is not None:
        # THE WIDTH COMES FROM PREP, not from this section.
        # One number for the board and the panel, so the panel recognises a
        # board's own keepout instead of drawing a second one over it.
        stated = panel_scores_stage.Settings.of(scores)._replace(
            rule_half_width_mm=float(score_half_width_mm)
        )
        # A DEPENDANT WITH ITS GOVERNOR OFF CONTRIBUTES NOTHING. The page greys
        # such a setting and the build has to reach
        # the same answer, so the jump distances this half acts on are the
        # governed ones - not the numbers the file happens to carry.
        settings = stated.governed
        # JUMP SCORING FIRST OF THE TWO, and the order is the whole of why the
        # keep-out follows the saw. `apply` below reads the `User.38` lines off the
        # panel as they then stand, so a pass shortened here is guarded over
        # exactly its own run and no rule area has to be edited afterwards.
        jumps = trim_jumps(
            board, base.scores.blades,
            settings.jump_horizontal_mm, settings.jump_vertical_mm,
            warnings=warnings,
        )
        panel_scores = panel_scores_stage.apply(
            board, blank, stated, instance_boxes=instance_boxes,
            warnings=warnings, rails=base.rails,
        )
    elif panel_scores_stage.score_lines_on(board):
        # THE SCORES THE BOARDS DECIDED ARE GUARDED WHETHER OR NOT ANYBODY
        # ASKED. `panel.scores.enabled` switches the panel's own
        # families of saw pass; the rule area round a saw pass is not a family,
        # it is what keeps a track and the rail pour off a line the blade is
        # going to run down. So a panel that carries merged board-edge passes
        # with the group switched off gets its keepouts here, at the declared
        # half width.
        panel_scores = panel_scores_stage.protect(
            board, float(score_half_width_mm), warnings=warnings
        )
    # THE POUR'S OUTLINE HERE, ITS COPPER LAST. `apply` is asked for the zones
    # and nothing else; the fill is taken at the bottom of this function, after
    # every stage that can put copper on the panel has run.
    #
    # ONE POUR OVER THE WHOLE CARRIER, PLUS RULE AREAS. The copper fill is one
    # zone for any combination of settings, because KiCad does not handle
    # overlapping zones of the same net well.
    # The source outline and the placements are handed down because the panel
    # cannot recover them: the instances' own `Edge.Cuts` were stripped in the
    # first half, and which piece of the panel edge was which board is no longer
    # written down anywhere.
    poured = None
    if rail_copper is not None:
        poured = rail_copper_stage.apply(
            board, blank, instance_bbox, rail_copper,
            instance_boxes=instance_boxes, warnings=warnings,
            fill=False, cli_path=cli_path, work_dir=work_dir,
            source_outline=source_outline, placements=placements,
        )
    # AFTER THE HOLES, because the holes are the datum the targets are offset
    # from. A corner the holes stage declined gets no
    # fiducial, by name, which is a thing this order makes true rather than a
    # thing the fiducial stage has to guess.
    optical = None
    if fiducials is not None:
        optical = fiducials_stage.apply(
            board, blank, instance_bbox, fiducials, holes, warnings=warnings,
        )
    # LAST of the drawing stages: the dimensions measure the panel and one of
    # its boards, so the panel has to exist first. `None` here is the config
    # having switched them off, not the section being absent.
    #
    # THE BOARD RECTANGLES, NOT THE TOOLING HOLES. The inner dimensions measure
    # a single board's X and Y extents. The stage needs the
    # instances' own boxes because the panel cannot answer for them - their
    # `Edge.Cuts` were stripped when the panel outline was derived - and it
    # picks which one to measure itself, from where the overall dimensions are.
    drawn = None
    if dimensions is not None:
        drawn = panel_dimensions_stage.apply(
            board, blank, instance_boxes, dimensions, warnings=warnings,
            holes=holes, source_outline=source_outline, placements=placements,
        )
    if text_settings is not None:
        from eedesignpanels.stages import panel_text

        if text_warning:
            warnings.append(text_warning)
        panel_text.apply(
            board, base, text_settings, text_content,
            footprints=board.footprints[carrier_footprints_start:], warnings=warnings,
        )
    # AND NOW THE COPPER. KiCad fills a BOARD, not a zone: the
    # sandbox it is handed is the panel exactly as it stands, so anything added
    # after the fill is copper the fill never saw. A fiducial added after the
    # fill would get no clearance ring round its pads. Nothing below here may
    # add a pad, a track or a
    # graphic on a copper layer.
    if fill_rails:
        poured = rail_copper_stage.fill_placed(
            board, poured, warnings=warnings,
            cli_path=cli_path, work_dir=work_dir,
        )

    return base.result(
        warnings, holes=holes, panel_scores=panel_scores,
        rail_copper=poured, dimensions=drawn, fiducials=optical,
        jumps=jumps,
    )


__all__ = [
    "OUTLINE_LAYER",
    "PANEL_OWNED_LAYERS",
    "SCORE_LAYER",
    "TOOLPATH_LAYER",
    "MIN_JUMPED_PASS_MM",
    "BladeLine",
    "JumpScoring",
    "JumpedPass",
    "OutlineStrip",
    "PanelizeResult",
    "ScoreMerge",
    "SteppedPanel",
    "apply",
    "carrier",
    "stepped",
    "blade_lines",
    "channel_guards",
    "channels_of",
    "edge_key",
    "instance_outline_bbox",
    "jump_axis",
    "jumped_span",
    "merge_scores",
    "trim_jumps",
    "outline_width_of",
    "primitive_of",
    "primitives_of",
    "blank_extent_mm",
    "blank_rails",
    "strip_outline",
    "to_board_items",
    "to_toolpath_items",
]
