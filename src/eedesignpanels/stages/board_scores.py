"""The keepout that rides with a v-score decision on the SOURCE board.

When a v-score line is added to the source board, a rule area is added with it
and the board's copper is re-filled. The source board's zones are then filled
and reviewed before they are frozen into polygons and moved to the panel.

Both halves of that are one fact about where a panel's copper comes from. The
panel does not pour the instances' copper - it carries the fill the DESIGN was
saved with, re-expressed as graphics no refill can move
(:mod:`eedesignpanels.stages.zones`). So a score decided on the board has to pull
the board's own copper back BEFORE the freeze, or the panel carries copper
sitting where a blade is going to run and nothing downstream can notice.

The mechanism is the panel's, moved one step earlier:

* accepting a score writes the `User.38` line AND the thin all-out keepout
  centred on it, on every copper layer the board declares - the same rectangle
  :func:`eedesignpanels.stages.panel_scores.rule_area_for` draws, because a
  keepout that differed from the panel's would be a second answer to one
  question;
* declining takes both away;
* the board's zones are then re-filled by KiCad, off to the side, and the fill
  is transplanted into the board being held (:mod:`eedesignpanels.stages.refill`).

**WHERE THE WIDTH COMES FROM.** The prep page sets it, at
`prep.vscore.rule_half_width_mm`,
read through `PanelConfig.score_rule_half_width_mm`, and 0.3 mm when no config
states one. It is a PREP parameter because changing it is a board change - this
stage is what writes the keep-out into the source board.

ONE number for the board and the panel, deliberately: the board's keepout steps
into the panel with its instance, and the panel recognises it as the guard
already in place by MEASURING it (`panel_scores.uncovered_runs`). Two numbers
would make that recognition fail and the panel would guard the same line twice.
That is why the panel's own score families read the same accessor rather than a
width of their own (`stages.panelize.carrier`).

**THE SYNC IS THE WHOLE OF IT.** :func:`sync` makes the board's score keepouts
say exactly what its score lines say - it adds what is missing, removes what no
line asks for any more, and does nothing at all when the two already agree. An
accept and a decline therefore differ only in the line they move, idempotence is
a property of the function rather than a rule the callers keep, and accepting a
score and declining it again returns the board to the bytes it had.

**AND IT RUNS AT LOAD.** Idempotence is only worth
anything if something calls it: with a decision and a width change as its only
two callers, a board that arrived carrying a `User.38` line and no keep-out
would keep that state until the user switched the score off and on again. The live
session runs this once on the board it has just read
(`preview.actions.repair_keepouts`), in memory, as a pending change.

What may be removed is bounded on purpose: a keepout this stage did not make is
never touched. The test is the name it writes plus the geometry it writes -
a thin rectangle, everything kept out - so a designer's own keepout of any other
shape, name or intent is left exactly where it is.
"""

import uuid
from typing import NamedTuple

from eedesigncore import Board, item_layers

from eedesignpanels.stages import panel_scores, route_toolpath

SCORE_LAYER = route_toolpath.SCORE_LAYER

# uuid5(NAMESPACE_URL, ...). Fixed for all time, and its own namespace rather
# than the panel's: a keepout the BOARD decided and one the PANEL added are
# different items with different lifetimes, and a board's keepout has to keep
# its identity when it is stepped into a panel that also draws its own.
RULE_AREA_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/board-score-rule-area"
)


class ScoreKeepouts(NamedTuple):
    """What one sync did to the board's score keepouts. Counts, not prose."""

    added: int
    removed: int
    # How many keepouts the board carries afterwards, which is one per score
    # line on a board this stage has finished with.
    total: int
    half_width_mm: float

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def half_width_of(config) -> float:
    """How far either side of a score line the board's keepout reaches.

    `prep.vscore.rule_half_width_mm`, read through
    the one accessor every stage asks - `PanelConfig.score_rule_half_width_mm`.
    See the module docstring: it is one number for the board and the panel.

    The duck-typed fallback below is for a stand-in config that is not the
    model: it reads the same key off whatever object it was handed, and lands on
    the shipped default when nothing states one.
    """
    width = getattr(config, "score_rule_half_width_mm", None)
    if width is not None:
        return float(width)
    prep = getattr(config, "prep", None)
    vscore = getattr(prep, "vscore", None) if prep is not None else None
    width = getattr(vscore, "rule_half_width_mm", None)
    if width is None:
        return panel_scores.DEFAULT_RULE_HALF_WIDTH_MM
    return float(width)


def score_lines_on(board: Board) -> list:
    """Every board-level `User.38` straight line, in the board's own order.

    File order and not sorted order: this is the board a designer edits, the
    lines are appended one decision at a time, and the keepouts are made to
    match them one for one. The panel sorts because two stages write its score
    layer; here there is one.
    """
    return [
        item for item in (*board.drawings, *board.trailing_drawings)
        if type(item).__name__ == "PcbLine"
        and SCORE_LAYER in item_layers(item)
    ]


def rule_area_key(line, half_width_mm: float) -> str:
    """What this keepout's uuid is derived FROM: the score line's own identity.

    A score line's uuid is itself
    derived from the outline items the candidate was measured on
    (`route_toolpath.score_line_for`), so a keepout named this way survives a
    save, a reload and a second run of prep without being renamed - and a
    decision taken twice writes one uuid both times.

    A line with no uuid - which a board KiCad wrote never has - falls back to
    where it is, which is the only other thing that identifies a cut.
    """
    if getattr(line, "id", None):
        return f"{line.id}|{half_width_mm:.6f}"
    return "|".join(
        [f"{value:.6f}" for point in (line.start, line.end) for value in point]
        + [f"{half_width_mm:.6f}"]
    )


def rule_area_for(line, half_width_mm: float, layers) -> object:
    """The keepout accepting *line* writes: the panel's rectangle, this uuid.

    Everything about the shape comes from the panel stage - one rectangle, one
    set of flags, one name - so the board's keepout and the panel's are the same
    statement and the panel can recognise this one by measuring it.
    """
    zone = panel_scores.rule_area_for(
        line.start, line.end, half_width_mm, layers
    )
    zone.id = str(uuid.uuid5(RULE_AREA_NAMESPACE,
                             rule_area_key(line, half_width_mm)))
    return zone


def ours(zone) -> bool:
    """Is this keepout one this stage made, and therefore one it may remove?

    Name AND shape. The name is what a DRC message says and what a user
    sees in the zone properties dialog; the shape is what makes it a strip
    either side of a line. A keepout that is not both is a designer's own and is
    never touched.
    """
    if getattr(zone, "name", "") != panel_scores.RULE_AREA_NAME:
        return False
    return panel_scores.keepout_span(zone) is not None


def keepouts_on(board: Board) -> list:
    """The board-level keepouts this stage owns, in the board's own order."""
    return [zone for zone in board.zones if ours(zone)]


def guards(zone, line, half_width_mm: float) -> bool:
    """Is *zone* exactly the keepout *line* asks for - same strip, same width?"""
    span = panel_scores.keepout_span(zone)
    if span is None:
        return False
    start, end, half = span
    if abs(half - half_width_mm) > panel_scores.ON_LINE_TOL_MM:
        return False
    wanted = (tuple(line.start), tuple(line.end))
    for ends in ((start, end), (end, start)):
        if all(
            abs(ends[index][axis] - wanted[index][axis])
            <= panel_scores.ON_LINE_TOL_MM
            for index in (0, 1) for axis in (0, 1)
        ):
            return True
    return False


def sync(board: Board, half_width_mm: float) -> ScoreKeepouts:
    """Make the board's score keepouts say exactly what its score lines say.

    In place, and idempotent: a board whose keepouts already match is not
    touched at all, so running this after every decision costs one pass over the
    zones and changes no bytes when nothing changed.

    Added keepouts go on in score-line order, after whatever zones the board
    already holds, so two runs of one decision produce one file.
    """
    lines = score_lines_on(board)
    mine = keepouts_on(board)
    layers = panel_scores.copper_layers(board)

    kept, doomed = [], set()
    for zone in mine:
        if any(guards(zone, line, half_width_mm) for line in lines):
            kept.append(zone)
        else:
            # BY IDENTITY, never by value: two keepouts on one line are equal
            # objects, and removing "the equal one" would take out whichever
            # the comparison reached first.
            doomed.add(id(zone))
    removed = len(doomed)
    if removed:
        board.zones = [zone for zone in board.zones if id(zone) not in doomed]

    added = 0
    for line in lines:
        if any(guards(zone, line, half_width_mm) for zone in kept):
            continue
        zone = rule_area_for(line, half_width_mm, layers)
        board.zones.append(zone)
        kept.append(zone)
        added += 1
    return ScoreKeepouts(added=added, removed=removed, total=len(kept),
                         half_width_mm=half_width_mm)


__all__ = [
    "RULE_AREA_NAMESPACE",
    "SCORE_LAYER",
    "ScoreKeepouts",
    "guards",
    "half_width_of",
    "keepouts_on",
    "ours",
    "rule_area_for",
    "rule_area_key",
    "score_lines_on",
    "sync",
]
