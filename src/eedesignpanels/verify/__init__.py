"""Gerber comparison harness - the engine of the future verification gate.

Two gerber sets in, a per-layer count of differing pixels and the bounding
regions of those differences out, plus a self-contained review page.

It is a **tool, not a gate**: it never blocks a build, never fails one, and
never turns a difference into an exception. It warns, loudly, and the run
completes.

The rasterization backend (`gerbonara` + `resvg-py` + `Pillow`) is a dev-side
dependency of this harness alone. Nothing here reaches the plugin engineers
install, so it adds no runtime dependency to KiCad's own Python interpreter.
"""

from eedesignpanels.verify.compare import (
    DEFAULT_UM_PER_PIXEL,
    DEFAULT_REGION_CELL_MM,
    compare,
    format_summary,
)
from eedesignpanels.verify.gerbers import export_gerbers
from eedesignpanels.verify.regions import Bounds, Canvas, Region
from eedesignpanels.verify.results import (
    DEFAULT_THRESHOLD,
    CompareResult,
    LayerDiff,
)

__all__ = [
    "DEFAULT_UM_PER_PIXEL",
    "DEFAULT_REGION_CELL_MM",
    "DEFAULT_THRESHOLD",
    "Bounds",
    "Canvas",
    "CompareResult",
    "LayerDiff",
    "Region",
    "compare",
    "export_gerbers",
    "format_summary",
]
