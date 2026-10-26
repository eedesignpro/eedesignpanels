"""The placement CSV: one line per copy, validated as untrusted input.

The escape hatch for an arrangement the pattern controls
cannot express. One line per copy, three numbers: offset across, offset down,
angle.

**The file is an INPUT FORM, never a stored reference.** What the config keeps
is the ROWS, not a path: "same config, same panel" means the config alone
reproduces the panel, and a config pointing at a CSV somewhere else on a
machine is a config that builds a different panel the day that file moves. So
the picker posts the bytes, this module turns them into rows, and the rows go
into `panel.json` (`config.PlacementFileConfig`). The file's NAME is kept for
the record and is never opened.

A CSV is hand-made and comes off a user's disk, so its content is untrusted
input, and every refusal names the line it happened on and changes nothing:

* not text, or empty of rows;
* more rows than :data:`MAX_ROWS`;
* a row that is not three fields;
* a field that is not a finite number;
* an angle that is not a quarter turn - only those are exact, because KiCad
  rotates by an integer branch at quarter turns and by sine and cosine
  everywhere else;
* an offset further than :data:`MAX_OFFSET_MM` from the design, which is not a
  panel any fabricator has a machine for.

Blank lines and `#` comments are skipped. A first row whose fields are not all
numbers is taken as a HEADER and skipped once - a spreadsheet writes one and
refusing the file over it would be refusing the commonest way of making one.
"""

import csv
import io
import math
from typing import NamedTuple

from eedesignpanels.placement import is_cardinal, normalized_angle

# How many copies one placement file may name. A panel of more boards than this
# is not a panel anybody is fixturing, and the number bounds the work a posted
# file can ask for.
MAX_ROWS = 512

# How far from the design one copy may be placed. Two metres: far past the
# largest panel any of this shop's machines takes, and a bound rather than a
# judgement.
MAX_OFFSET_MM = 2000.0

# How long a posted file name may be. Only ever displayed.
MAX_NAME_CHARS = 120

# What the three columns are, in order, for a message that has to name one.
COLUMNS = ("offset across", "offset down", "angle")


class PlacementFileError(Exception):
    """The placement file cannot be used. Nothing was changed."""


class PlacementRow(NamedTuple):
    """One copy: two millimetre offsets from the design, and a quarter turn."""

    across_mm: float
    down_mm: float
    angle_deg: float

    @property
    def triple(self) -> tuple:
        return (self.across_mm, self.down_mm, self.angle_deg)


class ParsedPlacements(NamedTuple):
    """One placement file, read. `rows` in the file's own order."""

    rows: tuple
    header: str = ""
    skipped: int = 0

    @property
    def triples(self) -> tuple:
        return tuple(row.triple for row in self.rows)


def _number(text: str, line: int, column: int) -> float:
    """One field as a finite float, or a refusal naming where it was."""
    try:
        value = float(text.strip())
    except (TypeError, ValueError) as exc:
        raise PlacementFileError(
            f"line {line}: the {COLUMNS[column]} is {text.strip()!r}, which is "
            f"not a number. Nothing was changed."
        ) from exc
    if not math.isfinite(value):
        raise PlacementFileError(
            f"line {line}: the {COLUMNS[column]} is {text.strip()!r}, which is "
            f"not a finite number. Nothing was changed."
        )
    return value


def _all_numbers(fields) -> bool:
    for field in fields:
        try:
            float(field.strip())
        except (TypeError, ValueError):
            return False
    return True


def parse(content: str) -> ParsedPlacements:
    """Read a placement CSV. Raises :class:`PlacementFileError` with the reason.

    Nothing is placed here and nothing is validated against a grid - that is
    :func:`placement.placed_arrangement`, which knows how many cells there are.
    This function's whole job is turning bytes into numbers, or saying why it
    cannot.
    """
    if not isinstance(content, str):
        raise PlacementFileError(
            "a placement file is text: three numbers a line, one line per copy"
        )
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise PlacementFileError(
            "that placement file is empty. One line per copy: offset across, "
            "offset down, angle."
        )

    rows, header, skipped, line_no = [], "", 0, 0
    for raw in csv.reader(io.StringIO(text)):
        line_no += 1
        fields = [field for field in raw]
        if not fields or all(not field.strip() for field in fields):
            skipped += 1
            continue
        if fields[0].lstrip().startswith("#"):
            skipped += 1
            continue
        if len(fields) != len(COLUMNS):
            raise PlacementFileError(
                f"line {line_no}: {len(fields)} field(s) where a placement has "
                f"{len(COLUMNS)} - {', '.join(COLUMNS)}. Nothing was changed."
            )
        if not rows and not header and not _all_numbers(fields):
            # A spreadsheet's own column titles, once, at the top.
            header = ",".join(field.strip() for field in fields)
            skipped += 1
            continue
        if len(rows) >= MAX_ROWS:
            raise PlacementFileError(
                f"that placement file names more than {MAX_ROWS} copies, which "
                f"is more than this page takes. Nothing was changed."
            )

        across = _number(fields[0], line_no, 0)
        down = _number(fields[1], line_no, 1)
        angle = _number(fields[2], line_no, 2)
        for value, column in ((across, 0), (down, 1)):
            if abs(value) > MAX_OFFSET_MM:
                raise PlacementFileError(
                    f"line {line_no}: the {COLUMNS[column]} is {value:g} mm, "
                    f"further than {MAX_OFFSET_MM:g} mm from the design - no "
                    f"panel is that big. Nothing was changed."
                )
        if not is_cardinal(angle):
            raise PlacementFileError(
                f"line {line_no}: the angle is {angle:g} degrees. A panel copy "
                f"takes a quarter turn - 0, 90, 180 or 270 - because only "
                f"those are exact. Nothing was changed."
            )
        rows.append(
            PlacementRow(across_mm=across, down_mm=down,
                         angle_deg=normalized_angle(angle))
        )

    if not rows:
        raise PlacementFileError(
            "that placement file carries no placements - every line was blank "
            "or a comment. One line per copy: offset across, offset down, angle."
        )
    return ParsedPlacements(rows=tuple(rows), header=header, skipped=skipped)


def file_name(value) -> str:
    """The posted file's name, as something safe to print. Never a path.

    The same rule `actions.import_name` keeps, for the same reason: a browser
    sends `C:\\fakepath\\placements.csv` on some platforms, and a name is not a
    location.
    """
    if not isinstance(value, str) or not value.strip():
        return "the placement file"
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable())
    return name[:MAX_NAME_CHARS] or "the placement file"
