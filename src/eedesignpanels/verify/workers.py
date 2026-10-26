"""Size the comparison's process pool from this machine, conservatively.

The per-layer comparison is embarrassingly parallel and the rasterizer holds
the GIL (measured: four threads render exactly as fast as one), so
the pool is made of processes. How many is a property of the machine running
the comparison, never of the output: the same layers produce the same numbers
whether one process computed them or eight, so auto-sizing here does not touch
the rule that the same input gives the same output.

The sizing is deliberately conservative on both axes:

* **CPU** - half the logical cores, so a build on an engineer's workstation
  leaves the machine usable.
* **RAM** - half of what is *available right now*, divided by the measured
  peak working set of one worker. A worker's peak scales with the canvas
  (measured on the 57 Mpx corpus canvas, see :data:`BYTES_PER_CANVAS_PIXEL`),
  so a bigger panel gets fewer workers rather than a swap storm.

When available memory cannot be determined the answer is 1 - serial, the
behaviour the harness always had - because guessing at RAM is how a review
run takes a workstation down with it.

Stdlib only: on Windows the number comes from ``GlobalMemoryStatusEx`` via
ctypes, elsewhere from ``os.sysconf``. No new dependency.
"""

import ctypes
import os
import sys
from typing import Optional

# Peak working set of one layer job, per canvas pixel. Measured on
# the corpus 2x3 panel (7203 x 7904 px = 56.9 Mpx): a worker process peaks at
# 1061-1075 MB (18.6-18.9 bytes per pixel) across the four densest layers,
# dominated by the rasterizer's RGBA canvas and its PNG round trip. 24 gives
# a 27% margin over the worst measurement, and is stated per pixel so a larger
# panel budgets proportionally more per worker.
BYTES_PER_CANVAS_PIXEL = 24

# The shares of the machine one comparison may claim.
RAM_SHARE = 0.5
CPU_SHARE = 0.5


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def available_memory_bytes() -> Optional[int]:
    """Physical memory available right now, or ``None`` when unknowable."""
    if sys.platform == "win32":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
        return None
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_AVPHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    if page <= 0 or pages <= 0:
        return None
    return page * pages


def default_worker_count(
    total_pixels: int,
    job_count: int,
    cpu_count: Optional[int] = None,
    available_bytes: Optional[int] = -1,
) -> int:
    """How many comparison processes this machine can afford, at least 1.

    ``cpu_count`` and ``available_bytes`` exist for tests; callers pass
    neither and get the machine's own numbers. ``available_bytes=-1`` means
    "ask the machine"; an explicit ``None`` means "the machine would not say",
    which sizes the pool at 1.
    """
    if job_count <= 1:
        return 1
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    by_cpu = max(1, int(cpus * CPU_SHARE))
    if available_bytes == -1:
        available_bytes = available_memory_bytes()
    if available_bytes is None:
        return 1
    per_worker = max(1, total_pixels) * BYTES_PER_CANVAS_PIXEL
    by_ram = int((available_bytes * RAM_SHARE) // per_worker)
    return max(1, min(job_count, by_cpu, by_ram))
