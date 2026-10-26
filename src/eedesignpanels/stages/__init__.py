"""Panel build stages. Each stage takes board data in and returns board data.

A stage that cannot complete says so and writes nothing.
"""

import hashlib
from pathlib import Path


def sha256_of(path: Path) -> str:
    """Digest of a file's bytes - the reproducibility check's unit."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prune_members(owners: list, removed: set) -> tuple:
    """Drop references to removed items; drop an owner left with none.

    Returns `(kept, pruned, dropped)`. A `(group ...)` naming a uuid that is not
    in the file has that member silently dropped by KiCad's parser, and an
    emptied group is deleted on the next save. Silence is the problem, so the
    pruning is done here and counted.

    Shared because two stages remove items: replication filters what a panel may
    not repeat, and panelizing removes outlines and zones.
    """
    kept, pruned, dropped = [], 0, 0
    for owner in owners:
        if any(member in removed for member in owner.members):
            owner = owner.model_copy(deep=True)
            owner.members = [m for m in owner.members if m not in removed]
            pruned += 1
        if owner.members:
            kept.append(owner)
        else:
            dropped += 1
    return kept, pruned, dropped
