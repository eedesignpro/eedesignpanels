"""The one check the panel page can run: compare the SAVED panel with its source.

It is the review run's own comparison (:mod:`eedesignpanels.verify`), pointed at
the files in the panel folder, and it is here rather than in the session for two
reasons. It NEEDS FILES - the comparison plots gerbers with kicad-cli, which
reads a board off the disk - so it is the one action on the panel page that
cannot run from memory, and the page says so instead of pretending. And it pulls
in the whole rasterizing backend, which is a development-time extra: a
preview must start and run without it, so the import lives inside the call.

It reports and never blocks. A comparison that cannot run at all -
no backend, no panel, switched off in the config - gives a reason and no page.
"""

import time
from pathlib import Path
from typing import NamedTuple


class CheckResult(NamedTuple):
    """One comparison: the report it wrote, what it found, how long it took."""

    report_path: Path
    summary: str
    seconds: float
    log: tuple = ()


def why_not(config) -> str:
    """Why the comparison cannot run, or `""` when it can.

    Read by the page to keep the button dead WITH A REASON on it, which is the
    difference between a control that is disabled and one that looks broken.
    """
    if config is None:
        return "no config is loaded yet"
    if config.stage != "build_panel":
        return (
            f"this config's stage is {config.stage!r}, so it builds no panel "
            f"to compare"
        )
    if not config.verify.enabled:
        return "the config switches the comparison off (verify.enabled = false)"
    if not config.output_board_path.is_file():
        return (
            "there is no saved panel to compare yet - press Save Panel Files "
            "first, because the comparison plots files"
        )
    return ""


def compare_saved_panel(config, out_dir: Path) -> CheckResult:
    """Compare the panel folder's board against its source. Writes a report.

    Raises:
        ValueError: when :func:`why_not` has an answer. The page keeps its
            button dead for the same reason, so this is the fence rather than
            the first line of defence.
    """
    reason = why_not(config)
    if reason:
        raise ValueError(reason)

    from eedesigncore import Board

    from eedesignpanels.review import render
    from eedesignpanels.review.__main__ import geometry_of, run_comparison

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    said = []
    started = time.monotonic()

    source = Board.from_file(config.source_board_path)
    layers = render.select_layers(render.layers_of(source), config.review.layers)
    geometry = geometry_of(config, source)
    project = config.source_board_path.stem
    page_path = run_comparison(
        config, geometry, layers, out_dir, project, log=said.append
    )
    seconds = time.monotonic() - started
    if page_path is None:
        raise ValueError(
            "the comparison did not produce a report: "
            + (said[-1] if said else "no reason given")
        )
    return CheckResult(
        report_path=Path(page_path),
        summary=(
            f"compared the saved panel against its source in "
            f"{seconds:.1f} s - the report is open in a second tab"
        ),
        seconds=seconds,
        log=tuple(said),
    )
