"""Passthrough stage: load the source board, write it into ``panel/``.

No geometry is changed, which is the point - it exercises the whole pipeline
and proves the two properties every later stage inherits: the write is
byte-identical to the design that was read, and two runs produce the same
bytes. Anything that later breaks those shows up as a diff
against a stage that does nothing.
"""

import time
from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board

from eedesignpanels.config import PanelConfig
from eedesignpanels.stages import sha256_of


class PassthroughResult(NamedTuple):
    """What one passthrough build produced."""

    output_path: Path
    source_sha256: str
    output_sha256: str
    load_seconds: float
    write_seconds: float

    @property
    def matches_source(self) -> bool:
        return self.source_sha256 == self.output_sha256


def run(config: PanelConfig) -> PassthroughResult:
    """Build the panel board from the config. Writes only inside panel/."""
    source = config.source_board_path
    output = config.output_board_path

    started = time.monotonic()
    board = Board.from_file(source)
    loaded = time.monotonic()

    output.parent.mkdir(parents=True, exist_ok=True)
    board.to_file(output)
    written = time.monotonic()

    return PassthroughResult(
        output_path=output,
        source_sha256=sha256_of(source),
        output_sha256=sha256_of(output),
        load_seconds=loaded - started,
        write_seconds=written - loaded,
    )
