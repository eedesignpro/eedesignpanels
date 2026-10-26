"""The panel's PLOT LAYER SELECTION - which layers a fab run inks by default.

The panel builder does not run the fab outputs. A separate CICD does, from the
panel project this builder writes, so **the project is the interface**: whatever
layer selection it carries is what downstream plotting reads when it is told to
use the stored settings.

Where that selection lives, verified against the KiCad source clone (tag
10.0.3) rather than assumed:

* **It is in the BOARD file, not the `.kicad_pro`.** `PCB_PLOT_PARAMS::Format`
  writes `(layerselection 0x…)` and `(plot_on_all_layers_selection 0x…)` inside
  `(setup (pcbplotparams …))` (`pcbnew/pcb_plot_params.cpp:142-153`). The
  project file's complete parameter list (`common/project/project_file.cpp`)
  has exactly six `pcbnew.*` keys - a drawing-sheet path and five last-used
  export paths - and no layer selection of any kind.
* **`kicad-cli pcb export gerbers --board-plot-params` is what reads it.** The
  flag's own help says "the Gerber plot settings already configured in the
  board file" (`kicad/cli/command_pcb_export_gerbers.cpp:36-39`), and the
  handler resolves the layer set as
  `(stored selection & non-copper) | (board's ENABLED layers & copper)`
  (`pcbnew/pcbnew_jobs_handler.cpp:1350-1360`) before intersecting it with the
  enabled layers again at `:1370`. Two consequences, both load-bearing here:
  a non-copper layer is plotted only if the stored bitmask names it, and a
  layer the board's `(layers …)` block does not declare is not enabled
  (`pcb_io_kicad_sexpr_parser.cpp:2331,2358`) and so is dropped whatever the
  bitmask says.
* **A `.kicad_jobset` does not read this.** Its plot jobs store their own
  `"layers"` array of canonical names (`common/jobs/job_export_pcb_plot.cpp:55`
  through `JOB_PARAM_LSEQ`, which serialises `LSET::Name(layer)` -
  `common/jobs/job.h:160-167`). A jobset is a separate file the builder does
  not write; it is named here so nobody looks for the selection in it.

The token format, from `BASE_SET::FmtHex` / `ParseHex`
(`include/base_set.h:302-345`): one hex nibble per four layers over
`PCB_LAYER_ID_COUNT` = 128 bits (`include/layer_ids.h:171`), most significant
nibble first, underscore every eight nibbles, bit *n* = `PCB_LAYER_ID` *n*. So
`User.16` = id 69 (`include/layer_ids.h:139`) = bit 69. `eedesigncore` keeps
the token verbatim as a string (`pcb/models.py`, `PlotParams.layerselection`),
which is why this module parses and re-emits it rather than asking the library
for a layer set.

Ordinals come from `eedesigncore.common.layers.LAYER_NAME_TO_ORDINAL`, which
is that same enum read off KiCad's source - never a table of our own.
"""

from eedesigncore import Board
from eedesigncore.common.layers import LAYER_NAME_TO_ORDINAL, is_copper_layer_name

# `PCB_LAYER_ID_COUNT` (`include/layer_ids.h:171`). The width of the set the
# token spells, so a bit outside it cannot be spelled at all.
SELECTION_BITS = 128
# `FmtHex` writes an underscore before every eighth nibble past the first.
NIBBLE_GROUP = 8


def ordinal_of(layer: str) -> int:
    """The `PCB_LAYER_ID` of a canonical layer name.

    Raises:
        ValueError: for a name KiCad has no ordinal for - a display name like
            `User.16-Dimensions` instead of the canonical `User.16`, say. The
            selection is a bitmask over the enum, so a name that is not in it
            cannot be selected and must not be silently ignored.
    """
    try:
        ordinal = LAYER_NAME_TO_ORDINAL[layer]
    except KeyError:
        raise ValueError(
            f"{layer!r} is not a canonical KiCad layer name, so it has no "
            f"place in a plot layer selection - the selection is a bitmask "
            f"over PCB_LAYER_ID"
        ) from None
    if not 0 <= ordinal < SELECTION_BITS:
        raise ValueError(
            f"{layer!r} is layer {ordinal}, outside the {SELECTION_BITS} bits "
            f"a plot layer selection spells"
        )
    return ordinal


def parse_selection(token: str) -> frozenset[int]:
    """The layer ordinals a `0x…` selection token names.

    Accepts what KiCad writes and what older files carry: an optional `0x`,
    underscores anywhere, any number of nibbles. `ParseHex` reads from the
    right, so a short token is the low layers and nothing else.
    """
    digits = token.strip().lower()
    if digits.startswith("0x"):
        digits = digits[2:]
    digits = digits.replace("_", "")
    if not digits:
        return frozenset()
    value = int(digits, 16)
    return frozenset(
        bit for bit in range(SELECTION_BITS) if value >> bit & 1
    )


def format_selection(ordinals) -> str:
    """A `0x…` selection token for *ordinals*, spelled the way KiCad spells it.

    Always the full 128-bit form KiCad 10 writes: 32 nibbles, most significant
    first, underscore-grouped in fours of eight. A board carrying a shorter
    legacy token is therefore normalised, which is exactly what KiCad itself
    does the first time it saves that board.
    """
    value = 0
    for ordinal in ordinals:
        if not 0 <= ordinal < SELECTION_BITS:
            raise ValueError(
                f"layer {ordinal} is outside the {SELECTION_BITS} bits a plot "
                f"layer selection spells"
            )
        value |= 1 << ordinal
    nibbles = f"{value:0{SELECTION_BITS // 4}x}"
    groups = [
        nibbles[start:start + NIBBLE_GROUP]
        for start in range(0, len(nibbles), NIBBLE_GROUP)
    ]
    return "0x" + "_".join(groups)


def with_layers(token: str, layers) -> str:
    """*token* with *layers* added. Nothing is ever removed.

    The panel INHERITS the design's plot selection, because the design's own
    fab run is the one the panel is plotted for. This only widens it.
    """
    ordinals = set(parse_selection(token))
    ordinals.update(ordinal_of(layer) for layer in layers)
    return format_selection(sorted(ordinals))


def selects(token: str, layer: str) -> bool:
    """Does this selection token name *layer*? The assertion form of the above."""
    return ordinal_of(layer) in parse_selection(token)


def select_layers(board: Board, layers) -> tuple[str, str] | None:
    """Add *layers* to *board*'s stored plot selection. Returns `(before, after)`.

    `None` when the board carries no `(setup …)` block, which means there is no
    `(pcbplotparams …)` to widen - the caller says so rather than inventing a
    whole settings block the source board never had.

    **Nothing is mutated in place.** `setup` and its `pcbplotparams` are
    replaced with copies, because a panel built by
    :func:`eedesignpanels.stages.step_and_repeat.cheap_copy` SHARES its setup
    object with the stepped panel the live preview keeps between edits; writing
    through it would change a panel somebody else is holding.
    """
    setup = board.setup
    if setup is None:
        return None
    wanted = list(layers)
    before = setup.pcbplotparams.layerselection
    after = with_layers(before, wanted)
    board.setup = setup.model_copy(
        update={
            "pcbplotparams": setup.pcbplotparams.model_copy(
                update={"layerselection": after}
            )
        }
    )
    return before, after


def plotted_layers(board: Board) -> tuple:
    """Which layers a fab run on *board*'s OWN plot settings would ink.

    KiCad's rule, not ours (`pcbnew/pcbnew_jobs_handler.cpp:1356-1370`):

        (stored selection & non-copper) | (ENABLED layers & copper)

    intersected afterwards with the enabled layers. Copper is deliberately
    taken from the board and not from the bitmask - KiCad stores the selection
    with all 32 copper layers set so that enabling one in the stackup enables
    it in the plot dialog, and the handler filters them back down here.

    The enabled set is exactly the board's `(layers …)` block
    (`pcb_io_kicad_sexpr_parser.cpp:2331,2358`), which is why a dimension on an
    undeclared layer is not plotted however the bitmask reads.

    Returned in the board's own layer-table order - KiCad's Appearance order.
    KiCad plots in its stackup order instead; the order changes which file
    comes out first and never which files come out.
    """
    if board.setup is None:
        return ()
    selected = parse_selection(board.setup.pcbplotparams.layerselection)
    return tuple(
        layer.canonical_name
        for layer in board.layers
        if is_copper_layer_name(layer.canonical_name)
        or ordinal_of(layer.canonical_name) in selected
    )


__all__ = [
    "NIBBLE_GROUP",
    "SELECTION_BITS",
    "format_selection",
    "ordinal_of",
    "parse_selection",
    "plotted_layers",
    "select_layers",
    "selects",
    "with_layers",
]
