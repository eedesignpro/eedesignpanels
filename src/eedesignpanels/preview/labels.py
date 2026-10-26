"""How a v-score candidate is named on the preview: `#1`, and one of three words.

**A hash is not a name.** A candidate's stable id is a uuid5 of the outline
items it was derived from - right for a key, useless for a person, and it tells
a reviewer nothing about which line on the picture it means.
So the page numbers them in PHYSICAL order and shows the number.

The order is the one a person reads a board in: **clockwise from the top left.**
Board millimetres put +y DOWN, so a clockwise sweep on screen is an increasing
`atan2(dy, dx)` about the outline's centre, and the sweep starts at the
direction of the bounding box's top-left corner. A candidate is placed by its
midpoint. Ties - two candidates whose midpoints share a bearing - are broken by
distance from the centre and then by their own id, so the numbering is a
function of the geometry and never of a dict order.

The three verdict words are exactly `SCORED`, `REJECTED` and `PROPOSED`. They
are derived from `accepted` and `proposable` rather than from the stage's
verdict strings, so the report's wording and the page's wording can differ
without one silently changing the other - and the stage's strings stay what the
frozen review artifacts already say.
"""

import math
from typing import NamedTuple

# The three words, and the whole of the page's vocabulary for a verdict.
SCORED = "SCORED"
REJECTED = "REJECTED"
PROPOSED = "PROPOSED"


def verdict_word(proposal) -> str:
    """One of the three words, for one proposal.

    Reading the two booleans, exactly as `overlay.verdict_color` does: a
    person's decision (`accepted`) first, then whether the tool would offer it
    at all (`proposable`).
    """
    if getattr(proposal, "accepted", False):
        return SCORED
    return PROPOSED if getattr(proposal, "proposable", True) else REJECTED


def _bearing(point: tuple, centre: tuple) -> float:
    """The screen-clockwise angle from *centre* to *point*, in radians."""
    return math.atan2(point[1] - centre[1], point[0] - centre[0])


def order_key(proposal, bbox: tuple) -> tuple:
    """Where one candidate sits in the reading order. Pure, so it is testable."""
    centre = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
    # The top-left corner's direction is where the sweep starts.
    start = _bearing((bbox[0], bbox[1]), centre)
    midpoint = proposal.candidate.midpoint
    turned = (_bearing(midpoint, centre) - start) % (2.0 * math.pi)
    return (
        round(turned, 9),
        round(math.dist(midpoint, centre), 6),
        getattr(proposal, "id", "") or "",
    )


def in_physical_order(proposals: list, bbox: tuple) -> list:
    """The proposals, clockwise from the top left of the outline's bounding box."""
    return sorted(proposals, key=lambda entry: order_key(entry, bbox))


class Labelled(NamedTuple):
    """One proposal with the two things the page calls it by."""

    number: int
    display_id: str
    verdict: str
    proposal: object


def labelled(proposals: list, bbox: tuple) -> list:
    """Every proposal numbered `#1`, `#2`, ... in physical order.

    Returned in that order, so a caller that wants the reading order gets it and
    a caller that wants to look one up has the whole record beside it.
    """
    return [
        Labelled(
            number=index,
            display_id=f"#{index}",
            verdict=verdict_word(proposal),
            proposal=proposal,
        )
        for index, proposal in enumerate(in_physical_order(proposals, bbox), 1)
    ]


def captions(entries: list) -> list:
    """`#1 SCORED` for each labelled entry, in the order given."""
    return [f"{entry.display_id} {entry.verdict}" for entry in entries]


# THE CLICKABLE CAPTION. A caption on the live preview is a
# control: clicking it toggles a candidate between the two states a person may
# put it in. The two actions are the ones the buttons already post, so a click
# and a button press are one path and one journal entry apiece.
ACCEPT_ACTION = "accept"
DECLINE_ACTION = "decline"


def click_action(proposal) -> str:
    """Which action a click on this candidate's caption takes, or `""`.

    SCORED goes back to PROPOSED (decline), PROPOSED becomes SCORED (accept),
    and REJECTED does nothing at all - the tool will not propose that line, so
    there is no decision to take on it and its caption is not a target.
    """
    if not getattr(proposal, "proposable", True):
        return ""
    return DECLINE_ACTION if getattr(proposal, "accepted", False) else ACCEPT_ACTION


def hit(entry) -> dict:
    """What one clickable caption carries, or None when it is not clickable.

    The candidate's NUMBER and the action, because the handler looks the
    candidate up in the state the page was drawn from and posts the same body
    the button does. No geometry travels on the caption.
    """
    action = click_action(entry.proposal)
    if not action:
        return None
    becomes = SCORED if action == ACCEPT_ACTION else PROPOSED
    return {
        "number": entry.number,
        "action": action,
        "verdict": entry.verdict,
        "title": (
            f"{entry.display_id} is {entry.verdict} - click to make it {becomes}"
        ),
    }


def hits(entries: list) -> list:
    """One hit record per labelled entry, in the order given. None is inert."""
    return [hit(entry) for entry in entries]
