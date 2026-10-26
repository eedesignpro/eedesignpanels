"""Render a board to per-layer SVG with `kicad-cli`, for review only.

Three rules this module exists to keep:

* **One call, all layers.** Cost is per invocation - 49 layers in a single
  `--mode-multi` call take 0.4 s, the same layers one call each take 8-9 s.
* **Absolute binary path.** KiCad's subprocess environment does not inherit
  the user's PATH. The path is found once, at import, by
  :func:`eedesignpanels.kicad_install.default_kicad_cli`.
* **Consume at `Done.`.** The files are complete and settled when that line
  prints; the process then spends ~3.4 s in teardown, which nothing waits for.

The layer list is derived from the board's own layer table - never hardcoded -
and output files are matched by kicad-cli's naming rule: the layer's *display*
name with dots replaced by underscores, not its canonical name.
"""

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board

from eedesignpanels.kicad_install import default_kicad_cli

DEFAULT_KICAD_CLI = default_kicad_cli()
CREATE_NO_WINDOW = 0x08000000

_PLOTTED = re.compile(r"Plotted to '(.+)'\.")


class LayerSpec(NamedTuple):
    """One entry of the board's layer table, in board-file order."""

    ordinal: int
    canonical_name: str
    layer_type: str
    display_name: str

    @property
    def svg_suffix(self) -> str:
        """The part of the exported filename kicad-cli derives from a layer."""
        return self.display_name.replace(".", "_")


class ExportResult(NamedTuple):
    """What one kicad-cli invocation produced."""

    svg_by_layer: dict
    plotted: list
    unmapped: list
    seconds_to_done: float


def layers_of(board: Board) -> list[LayerSpec]:
    """Layer table in board-file order - the order KiCad's Appearance shows.

    The display name is the entry's user name when present, else the canonical
    name: `(9 "F.Adhes" user "F.Adhesive")` displays as `F.Adhesive`.
    """
    return [
        LayerSpec(
            ordinal=layer.ordinal,
            canonical_name=layer.canonical_name,
            layer_type=layer.layer_type,
            display_name=layer.user_name or layer.canonical_name,
        )
        for layer in board.layers
    ]


def select_layers(layers: list[LayerSpec], wanted) -> list[LayerSpec]:
    """Filter a layer table by the config's `review.layers` value."""
    if wanted == "all":
        return list(layers)
    by_name = {layer.canonical_name: layer for layer in layers}
    unknown = [name for name in wanted if name not in by_name]
    if unknown:
        raise ValueError(f"board has no such layer(s): {', '.join(unknown)}")
    return [by_name[name] for name in wanted]


def prep_for_render(board_path: Path, out_dir: Path) -> Path:
    """Write a plot-only copy with footprint Value fields hidden.

    `kicad-cli` has no equivalent of the PCB editor's Objects panel, so every
    Value the user keeps switched off comes back in the export and buries
    the geometry. Hiding is what the Objects panel does, and it happens on a
    copy - the input design is never modified. The copy keeps the original
    filename so exported SVG names still match the board stem.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    copy_path = out_dir / board_path.name
    if copy_path.resolve() == board_path.resolve():
        raise ValueError(f"render prep would overwrite the input: {board_path}")

    board = Board.from_file(board_path)
    for footprint in board.footprints:
        for prop in footprint.properties:
            if prop.name == "Value":
                prop.visible = False
    board.to_file(copy_path)
    return copy_path


def export_svgs(
    board_path: Path,
    layers: list[LayerSpec],
    out_dir: Path,
    theme: str,
    cli_path: Path = DEFAULT_KICAD_CLI,
) -> ExportResult:
    """One kicad-cli call for every layer. Returns {canonical name: svg path}."""
    if not Path(cli_path).is_file():
        raise FileNotFoundError(f"kicad-cli not found at {cli_path}")
    if not layers:
        raise ValueError("no layers selected for render")

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.svg"):
        stale.unlink()

    command = [
        str(cli_path), "pcb", "export", "svg",
        "--layers", ",".join(layer.canonical_name for layer in layers),
        "--output", str(out_dir),
        "--mode-multi",
        "--page-size-mode", "2",
        "--exclude-drawing-sheet",
        "--theme", theme,
        str(board_path),
    ]
    started = time.monotonic()
    # Fixed argv, absolute binary, no shell: nothing here is interpolated by a
    # shell, and the only caller-supplied values are paths and layer names.
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    plotted: list[Path] = []
    transcript: list[str] = []
    done = False
    for raw in process.stdout:
        line = raw.strip()
        transcript.append(line)
        match = _PLOTTED.match(line)
        if match:
            plotted.append(Path(match.group(1)))
        elif line == "Done.":
            done = True
            break
    seconds_to_done = time.monotonic() - started

    if not done:
        process.wait()
        raise RuntimeError(
            f"kicad-cli did not finish the export (rc={process.returncode}):\n"
            + "\n".join(transcript[-20:])
        )
    # The files are settled at "Done."; the ~3.4 s teardown is reaped off the
    # caller's path so nothing - GUI or CI - waits on it.
    threading.Thread(target=process.communicate, daemon=True).start()

    by_name = {path.name: path for path in plotted}
    svg_by_layer: dict[str, Path] = {}
    unmapped: list[str] = []
    for layer in layers:
        expected = f"{Path(board_path).stem}-{layer.svg_suffix}.svg"
        if expected in by_name:
            svg_by_layer[layer.canonical_name] = by_name[expected]
        else:
            unmapped.append(layer.canonical_name)

    return ExportResult(
        svg_by_layer=svg_by_layer,
        plotted=plotted,
        unmapped=unmapped,
        seconds_to_done=seconds_to_done,
    )
