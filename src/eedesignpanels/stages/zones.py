"""Turn the instances' copper zones into the copper they were already filled with.

A copper zone is a *live* object: it carries an outline, a set of rules and a
CACHED fill that KiCad computed the last time somebody pressed B. On a single
design that is exactly right - the fill follows the design. On a panel it is a
hazard, because the panel is a fabrication artifact and a filled zone in it is an
invitation for something downstream to refill it against a shape (six boards, a
frame, routed channels) that the designer never approved.

So the panel carries no copper zone. Each one's cached fill becomes plain copper
polygons - the same records, re-expressed as `gr_poly` graphics that no refill
can move - and the zone itself is dropped. `eedesigncore.flatten_fills` does the
re-expression and cites the KiCad 10.0.3 plot path for every rule it follows;
this module owns only what a PANEL needs on top of that: which zones go, what
identity the graphics get, and what the run has to say about it.

**Every RULE AREA survives, unconditionally.** A keepout pours no copper, so
there is nothing to flatten and
nothing to be refilled wrongly - the hazard that empties the panel of copper
zones does not exist for it. What it does carry is a statement the designer made
about where nothing may go, and a panel that drops it throws that away for
nothing. Board-level and footprint-hosted alike are kept, with no predicate
between them: `eedesigncore` moves a footprint-hosted zone with its footprint,
so every replica's keepout lands at its own
instance's position.

Filled zones inside footprints flatten like any other, for the same reason: the
records are board coordinates and the library now moves them.
"""

import uuid
from typing import NamedTuple

from eedesigncore import Board, flatten_fills

from eedesignpanels.stages import prune_members

# uuid5(NAMESPACE_URL, ...). Fixed for all time, so a panel built twice on two
# machines gives its flattened copper the same identities.
FILL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/zone-fill"
)


class ZoneFlatten(NamedTuple):
    """What flattening did to one panel, counted rather than described."""

    # Zones that carried a cached fill and became copper graphics, wherever they
    # were hosted.
    filled_zones: int
    # Zones with a copper pour and NO fill record: nothing to flatten, and the
    # copper the design shows is not in the file to begin with.
    empty_zones: int
    # Rule areas (keepouts) at board level, KEPT.
    rule_areas: int
    # Rule areas inside footprints, KEPT.
    footprint_rule_areas: int
    # The graphics written, in total and per layer, and how many carry no net
    # because their record is an island.
    polygons: int
    per_layer: tuple
    islands: int
    groups_pruned: int
    groups_dropped: int

    @property
    def zones_removed(self) -> int:
        """Zones the panel no longer holds. A rule area is never one of them."""
        return self.filled_zones + self.empty_zones

    @property
    def rule_areas_kept(self) -> int:
        """Every keepout that survived into the panel, hosted either way."""
        return self.rule_areas + self.footprint_rule_areas


def is_rule_area(zone) -> bool:
    """True for a keepout zone: a rule, not copper.

    The `(keepout …)` block is what makes a zone a rule area in KiCad 10, and a
    zone that has one never pours copper - which is why it has no fill record
    to flatten, and why nothing downstream can refill it wrongly.
    """
    return zone.keepout is not None


def fill_records(zone) -> int:
    """How many `(filled_polygon …)` records *zone* stores."""
    return len(zone.filled_polygons)


def zone_key(zone, position: int) -> str:
    """The identity flattened copper is derived from.

    The zone's own uuid, which `inject_board` already derived from the grid
    cell, so the graphic inherits a per-instance identity without this module
    knowing anything about instances. A zone with no uuid - which a board KiCad
    wrote never has - falls back to its position in the run over every zone on
    the panel, which is stable for as long as the build is.
    """
    return zone.id or f"index:{position}"


def graphics_of(zone, position: int) -> list:
    """One zone's cached fill as identified copper graphics.

    `flatten_fills` builds them and mints nothing; the uuid is derived here so
    that the same panel built twice names the same copper.
    """
    graphics = flatten_fills(zone)
    key = zone_key(zone, position)
    for index, graphic in enumerate(graphics):
        graphic.id = str(uuid.uuid5(FILL_NAMESPACE, f"{key}|{index}"))
    return graphics


def apply(board: Board, warnings: list) -> ZoneFlatten:
    """Flatten every copper zone on *board* into graphics, in place. Keeps rule areas.

    Board-level zones first, in file order, then the footprints' - so the
    graphics come out in one order however the panel was assembled. Afterwards
    the only zones the panel holds are rule areas.

    `sift` is written once and run over every zone list the panel has, because
    where a zone is hosted changes nothing about what happens to it.
    """
    graphics: list = []
    per_layer: dict = {}
    removed: set = set()
    filled = empty = islands = position = 0

    def sift(zones: list) -> tuple:
        """Return `(kept, rule_areas)`: rule areas stay, copper zones flatten."""
        nonlocal filled, empty, islands, position
        kept: list = []
        rule_areas = 0
        for zone in zones:
            here, position = position, position + 1
            if is_rule_area(zone):
                rule_areas += 1
                kept.append(zone)
                continue
            if zone.id is not None:
                removed.add(zone.id)
            if not fill_records(zone):
                empty += 1
                continue
            filled += 1
            for graphic in graphics_of(zone, here):
                per_layer[graphic.layer] = per_layer.get(graphic.layer, 0) + 1
                if graphic.net_name is None:
                    islands += 1
                graphics.append(graphic)
        return kept, rule_areas

    board.zones, rule_areas = sift(board.zones)

    footprint_rule_areas = 0
    for footprint in board.footprints:
        footprint.zones, kept = sift(footprint.zones)
        footprint_rule_areas += kept

    board.drawings.extend(graphics)
    pruned, dropped = _prune_owners(board, removed)

    if empty:
        warnings.append(
            f"{empty} copper zone(s) on the panel carry no fill record, so "
            f"they flattened to nothing - the design was saved with those "
            f"zones unfilled and the copper they show on screen is not in the "
            f"file"
        )

    return ZoneFlatten(
        filled_zones=filled,
        empty_zones=empty,
        rule_areas=rule_areas,
        footprint_rule_areas=footprint_rule_areas,
        polygons=len(graphics),
        per_layer=tuple(sorted(per_layer.items())),
        islands=islands,
        groups_pruned=pruned,
        groups_dropped=dropped,
    )


def _prune_owners(board: Board, removed: set) -> tuple:
    """Drop every reference to a removed zone, at board and footprint level."""
    pruned = dropped = 0
    board.groups, group_pruned, group_dropped = prune_members(board.groups, removed)
    pruned += group_pruned
    dropped += group_dropped
    board.generators, gen_pruned, gen_dropped = prune_members(
        board.generators, removed
    )
    pruned += gen_pruned
    dropped += gen_dropped
    for footprint in board.footprints:
        footprint.groups, fp_pruned, fp_dropped = prune_members(
            footprint.groups, removed
        )
        pruned += fp_pruned
        dropped += fp_dropped
    return pruned, dropped


__all__ = [
    "FILL_NAMESPACE",
    "ZoneFlatten",
    "apply",
    "fill_records",
    "graphics_of",
    "is_rule_area",
    "zone_key",
]
