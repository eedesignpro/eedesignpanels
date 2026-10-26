"""What makes six copies of one board six things and not one - pure policy.

Duplicating a board duplicates every identifier in it: uuids, net names and
reference designators. Which of those identifiers get rewritten is
policy rather than geometry, so it lives here: no board data, no
`eedesigncore`, no I/O, and every function is a function of its arguments
alone.

* **The instance index** numbers the copies 1..N in row-major order, which is
  the order `placement.grid_placements` emits and the order an engineer reads a
  panel in.
* **A net name** takes the index as a suffix, because a net is a NAME in KiCad
  10 - two copies both naming `AGND` are ONE electrical net, and a panel of six
  boards wired together is not what anybody asked for.
* **A reference designator does NOT.** Every copy
  keeps the source's, verbatim. The designator is printed on the silkscreen, so
  a copy reading `R1_4` where the design reads `R1` is a board an assembler
  cannot use. The suffix here is the net's alone.

The uuid key is here too, so the one string every replicated uuid is derived
from - the grid cell, never a float offset - is written once. A float offset
can round differently between two runs, and the uuids would change.

Suffix collisions are possible in principle (a source that already contains
both `AGND` and `AGND_1`) and would silently merge two nets, so
:func:`collisions` names them and the caller warns. It never rewrites its own
scheme to dodge one: a naming rule that changes shape depending on the board is
a rule nobody can predict.
"""

from collections.abc import Iterable
from uuid import UUID

# THE NAMESPACE EVERY REPLICATED ITEM'S UUID IS DERIVED IN. The
# engine names its own value here and passes it to the board library, so a
# change of the library's default never changes a panel. The value is
# uuid5(NAMESPACE_URL,
#       "https://github.com/eedesignpro/eedesignpanels/panel-instance"),
# the same scheme as the engine's other namespace strings. A new value gives
# every replicated item a new uuid, so it changes only together with those.
INSTANCE_UUID_NAMESPACE = UUID("752f6543-72c3-5aa4-b08d-0e37aa95379a")

# Between the original name and the instance index. One character, and legal
# anywhere in a KiCad net name.
SEPARATOR = "_"


def instance_index(column: int, row: int, columns: int) -> int:
    """The 1-based row-major number of the copy in grid cell (column, row).

    Row-major because `grid_placements` is row-major: instance 1 is the top
    left, and the index counts along a row before stepping down.
    """
    if columns < 1:
        raise ValueError(f"a grid needs at least one column, got {columns}")
    if not 0 <= column < columns:
        raise ValueError(f"column {column} is outside a {columns}-column grid")
    if row < 0:
        raise ValueError(f"row must not be negative, got {row}")
    return row * columns + column + 1


def instance_key(column: int, row: int) -> str:
    """The uuid derivation key for one grid cell.

    The cell, not the millimetre offset: an offset is a float and item identity
    must not depend on float formatting. `eedesigncore.derive_uuids` turns this
    into `uuid5(namespace, f"{source_uuid}|{key}")`.
    """
    return f"{column}|{row}"


def suffixed(name: str, index: int) -> str:
    """*name* with the instance index appended. An empty name stays empty.

    An empty name is not a name: it means "none", and `_3` reads like an
    identifier and is not one.
    """
    return f"{name}{SEPARATOR}{index}" if name else name


def net_map(net_names: Iterable[str], index: int) -> dict[str, str]:
    """The old-name to new-name map for one instance's nets.

    Every net is renamed, including instance 1's: a scheme where one copy is
    special is a scheme with an off-by-one in it. The empty name is skipped
    because it means "no net", and mapping it would connect every unconnected
    pad in the copy to one another.
    """
    return {name: suffixed(name, index) for name in net_names if name}


def collisions(names: Iterable[str], instances: int) -> list[str]:
    """Names whose suffixed form is already a name in *names*.

    A collision means two distinct source nets land on one name in the panel,
    which silently merges them electrically. The caller warns; it never blocks
    and never changes the scheme.
    """
    existing = {name for name in names if name}
    return sorted(
        {
            name
            for name in existing
            for index in range(1, instances + 1)
            if suffixed(name, index) in existing
        }
    )
