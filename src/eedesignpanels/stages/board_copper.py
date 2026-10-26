"""Re-fill the SOURCE board's own copper, so a decision is visible before it freezes.

A v-score accepted on the source board writes its keepout, and the board's
copper is re-filled. The source board's zones are then filled and reviewed
before they are frozen into polygons and moved to the panel.

The freeze is literal. A panel carries no copper zone at all - each instance's
CACHED fill becomes graphics (:mod:`eedesignpanels.stages.zones`) - so whatever
copper the board is holding when the panel is derived is the copper a fabricator
gets. A keepout that the board's zones have not been filled against is a keepout
nothing obeys.

This is the panel's own pour mechanism (:mod:`eedesignpanels.stages.refill`)
pointed at the board, with two differences, both of them consequences of the
board being somebody's DESIGN rather than an artifact this tool made:

* **the island setting is left alone.** A panel rail pour is netted to nothing
  and must be told to keep its islands or it comes back empty. A designer's zone
  carries the island rule the designer chose, and changing it here would be this tool
  editing a design decision it was not asked about;
* **the project goes into the sandbox.** Net classes and custom design rules
  live beside the board, not in it, so a board filled alone is filled against
  KiCad's defaults. Measured on a test board: two zones came back
  split into three islands and two islands without the project, and one island
  each with it.

Nothing here writes to the design. The board is serialised into a sandbox, KiCad
fills the copy, and the fill RECORDS are transplanted into the board this process
is holding - the same two-call shape the panel uses, so the ten seconds are
payable off a responsive path and the answer can be dropped when the board has
moved on.
"""

from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board

from eedesignpanels.stages import refill

# What the journal and the corner note call this phase.
PHASE = "board copper"


class BoardFill(NamedTuple):
    """One board refill, measured. Every number here is reported."""

    zones: int
    records: int
    seconds: float
    # Copper zones hosted INSIDE footprints, which this does not fill - see
    # :func:`footprint_copper_zones`. Counted so a run can say so rather than
    # leave a designer to notice.
    footprint_zones: int


def copper_zones(board: Board) -> list:
    """The board-level zones KiCad is asked about: copper, never a rule area.

    A keepout pours nothing, so it has no fill to take and including it would
    only widen what the transplant has to prove it left alone.
    """
    return [zone for zone in board.zones if zone.keepout is None]


def footprint_copper_zones(board: Board) -> list:
    """Copper zones hosted inside footprints, which this cannot fill.

    `kicad-cli` fills them like any other, but the answer comes back inside the
    footprint and the transplant matches zones by uuid at BOARD level, so they
    are named rather than silently half-handled. A board that has
    none - which is the common case - gets an empty list and no
    sentence.
    """
    return [
        zone
        for footprint in board.footprints
        for zone in footprint.zones
        if zone.keepout is None
    ]


def take(board: Board, beside=None, cli_path=None, work_dir=None, cancel=None):
    """Ask KiCad what this board's copper is. THE SLOW HALF; changes nothing.

    Measured on a test board: 1.7 s of `kicad-cli`, plus about a
    second of writing and re-reading 1.8 MB of board. Returns a
    :class:`~eedesignpanels.stages.refill.FillTaken`, or None when the board has
    no copper zone to fill.

    *cancel* is passed straight through: a newer score decision or keep-out
    width sets it and the running KiCad is STOPPED rather than waited out
    (`refill.take_fills`).

    Raises:
        refill.RefillError: when kicad-cli is missing, fails or loses a zone.
        refill.RefillCancelled: when *cancel* is set. The board still owes a
            fill, and the change that cancelled this one is about to ask.
    """
    zones = copper_zones(board)
    if not zones:
        return None
    kwargs = {} if cli_path is None else {"cli_path": cli_path}
    return refill.take_fills(
        board, zones, work_dir=work_dir, require_island_keep=False,
        beside=None if beside is None else Path(beside), cancel=cancel,
        **kwargs
    )


def apply(board: Board, taken) -> BoardFill:
    """Put *taken*'s copper on the board. THE FAST HALF, and the only mutation.

    The transplant proves it changed nothing but those zones' fills - the board
    is serialised with them detached before and after, and the two byte strings
    must agree - which is what makes it safe to do this to a board a designer is
    editing in the preview.

    Raises:
        refill.RefillError: when the answer is not this board's, or when the
            transplant moved anything else.
    """
    zones = copper_zones(board)
    if taken is None or not zones:
        return BoardFill(zones=0, records=0, seconds=0.0,
                         footprint_zones=len(footprint_copper_zones(board)))
    result = refill.transplant(board, zones, taken)
    return BoardFill(
        zones=len(zones),
        records=result.records,
        seconds=taken.seconds,
        footprint_zones=len(footprint_copper_zones(board)),
    )


def refill_board(board: Board, beside=None, cli_path=None,
                 work_dir=None) -> BoardFill:
    """The round trip in one call, for a caller with nobody to stay responsive for.

    A test, a script, a headless run. The live preview uses the two halves.
    """
    return apply(board, take(board, beside=beside, cli_path=cli_path,
                             work_dir=work_dir))


def note_for(fill: BoardFill) -> str:
    """What a run says about a board fill, or `""` when there is nothing to say."""
    if fill is None or not fill.zones:
        return ""
    said = (
        f"the board's {fill.zones} copper zone(s) were re-filled by KiCad in "
        f"{fill.seconds:.1f} s, giving {fill.records} filled area(s)"
    )
    if fill.footprint_zones:
        said += (
            f" - {fill.footprint_zones} copper zone(s) inside footprints were "
            f"not re-filled and still carry the fill the design was saved with"
        )
    return said


__all__ = [
    "PHASE",
    "BoardFill",
    "apply",
    "copper_zones",
    "footprint_copper_zones",
    "note_for",
    "refill_board",
    "take",
]
