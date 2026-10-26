"""Export a board to gerbers with `kicad-cli`, and find them again.

Rules this module keeps:

* **One invocation, all layers.** kicad-cli's cost is per call, not per layer.
* **Absolute binary path.** KiCad's subprocess environment does not inherit
  the user's PATH.
* **Never `--check-zones`.** Cached fills are the contract - the panel ships
  the fills the board already carries, so a comparison must plot those and not
  a fresh recomputation.
* **`--no-protel-ext`**, which makes every file `.gbr` and names it after the
  layer's *display* name with dots replaced by underscores - the same rule the
  SVG export follows, verified 7/7 on the reference board.

Unlike the SVG export, gerber export prints no `Done.` line and exits promptly,
so this waits for the process rather than consuming a progress marker.
"""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.review.render import CREATE_NO_WINDOW, DEFAULT_KICAD_CLI, LayerSpec

_PLOTTED = re.compile(r"Plotted to '(.+)'\.")


class GerberExport(NamedTuple):
    """What one kicad-cli gerber invocation produced."""

    gerber_by_layer: dict
    plotted: list
    unmapped: list
    seconds: float


def gerber_name(board_stem: str, layer: LayerSpec) -> str:
    """The filename kicad-cli gives a layer under ``--no-protel-ext``."""
    return f"{board_stem}-{layer.svg_suffix}.gbr"


def export_gerbers(
    board_path: Path,
    layers: list[LayerSpec],
    out_dir: Path,
    cli_path: Path = DEFAULT_KICAD_CLI,
) -> GerberExport:
    """Plot ``layers`` of ``board_path`` into ``out_dir`` in one call."""
    if not Path(cli_path).is_file():
        raise FileNotFoundError(f"kicad-cli not found at {cli_path}")
    if not layers:
        raise ValueError("no layers selected for gerber export")

    board_path = Path(board_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in list(out_dir.glob("*.gbr")) + list(out_dir.glob("*.gbrjob")):
        stale.unlink()

    command = [
        str(cli_path), "pcb", "export", "gerbers",
        "--layers", ",".join(layer.canonical_name for layer in layers),
        "--output", str(out_dir),
        "--no-protel-ext",
        str(board_path),
    ]
    started = time.monotonic()
    # Fixed argv, absolute binary, no shell: the only caller-supplied values
    # are paths and layer names, and nothing is interpolated by a shell.
    finished = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    seconds = time.monotonic() - started
    if finished.returncode != 0:
        raise RuntimeError(
            f"kicad-cli gerber export failed (rc={finished.returncode}):\n"
            f"{finished.stdout}"
        )

    matches = (_PLOTTED.match(line.strip()) for line in finished.stdout.splitlines())
    plotted = [Path(match.group(1)) for match in matches if match]
    by_name = {path.name: path for path in plotted}
    gerber_by_layer: dict[str, Path] = {}
    unmapped: list[str] = []
    for layer in layers:
        expected = gerber_name(board_path.stem, layer)
        if expected in by_name:
            gerber_by_layer[layer.canonical_name] = by_name[expected]
        else:
            unmapped.append(layer.canonical_name)

    return GerberExport(
        gerber_by_layer=gerber_by_layer,
        plotted=plotted,
        unmapped=unmapped,
        seconds=seconds,
    )


def board_stem_of(directory: Path) -> str:
    """The board name kicad-cli prefixed every file in ``directory`` with.

    Taken from the job file, ``<stem>-job.gbrjob``, because the stem itself
    contains hyphens on real part numbers and so cannot be split back out of a
    gerber filename: ``100-200-3000_sensor-board-v2-F_Cu.gbr``. Layer
    display names contain hyphens too (``User_4-F_FpRef``), so neither end of
    the name is safe to guess at.
    """
    jobs = sorted(Path(directory).glob("*-job.gbrjob"))
    if len(jobs) != 1:
        raise ValueError(
            f"{directory} holds {len(jobs)} .gbrjob files; exactly one is "
            f"needed to identify the board name"
        )
    return jobs[0].name[: -len("-job.gbrjob")]


def layer_key(path: Path, board_stem: str) -> str:
    """The layer part of a gerber filename: display name with underscores."""
    prefix = f"{board_stem}-"
    if not path.stem.startswith(prefix):
        raise ValueError(f"{path.name} is not a gerber of board {board_stem}")
    return path.stem[len(prefix):]


def collect_gerbers(directory: Path, board_stem: str = "") -> dict:
    """Map ``{layer key: path}`` for every ``.gbr`` in a directory.

    The key is the layer's display name with underscores, e.g. ``F_Cu``, so two
    directories compare layer to layer even when they came from boards with
    different filenames.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"not a gerber directory: {directory}")
    stem = board_stem or board_stem_of(directory)

    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.gbr")):
        key = layer_key(path, stem)
        if key in found:
            raise ValueError(
                f"{directory} has two files for layer {key}: "
                f"{found[key].name} and {path.name}"
            )
        found[key] = path
    return found
