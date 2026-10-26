"""Does this panel come apart, and in what order does a person break it.

A fabricator holds the finished panel and has to get six
boards out of it undamaged. This module answers whether that is possible and
hands back the order to do it in - not a yes or a no, because an order is what
somebody can act on and a verdict is not.

**THE TEST.** A piece of panel splits in two when a saw pass runs CLEAR ACROSS
it. Repeat on both halves until every board is loose. The pieces are rectangles
and there are a handful of them, so the search is a plain recursion and nothing
more.

**THE SEARCH NEEDS NO BACKTRACKING**, which is why the recursion is three
lines. A cut never re-attaches anything, and splitting a
piece cannot stop another pass from spanning one of its halves: a pass that ran
the full width still runs the full width of a narrower piece. So taking every
available split, greedily, reaches exactly the same leaves as any other order,
and there is no move to undo.

**TABS COUNT, NOT ONLY SCORES.** This is the difference between a useful check
and a useless one. A board held in a routed channel by
a few tabs comes out when somebody snaps the tabs, with no saw involved. A
check that understood only saw passes would call every ordinary tab-routed
panel trapped, which is worse than no check at all.

**IT NEVER REFUSES ANYTHING.** No warning banner, no blocked
build, no refused save. A user panelizes plenty of things for reasons
this tool does not know about; the check reports and the build carries on.

**HOW A BOARD IS JUDGED LOOSE.** Its outline is walked at
:data:`SAMPLE_MM`, and every sample point is one of four things: cut away by
the router, standing on a declared tab, standing on a saw line, or plain
material joining it to the panel. A board is loose inside a piece when every
one of its points is cut, is a tab, or lies on that piece's own edge - because
a point on the piece edge is a point the saw has already been through.
"""

import math
from typing import NamedTuple

from eedesignpanels.stages import panel_scores as panel_scores_stage
from eedesignpanels.stages import route_toolpath
from eedesignpanels.toolpath import (
    Arc,
    Grid,
    Line,
    distance_to_primitive,
    index_cell_mm,
    padded_bbox,
)

# HOW FINELY A BOARD EDGE IS WALKED. Half a millimetre is well under the
# narrowest tab this project will make (`mintab.MIN_TAB_DEFAULT_MM` is 0.5 mm,
# and a tab that narrow is already reported) and coarse enough that six boards
# cost a few thousand point tests rather than a few hundred thousand
# (MEASURED on the corpus panel).
SAMPLE_MM = 0.5

# HOW CLOSE A POINT SITS TO A THING TO BE ON IT. A tenth of a millimetre is a
# hundred times the nanometre coincident geometry differs by and a fifth of the
# walk above, so no sample falls between two classifications.
ON_TOL_MM = 0.1

# HOW FAR A SAW PASS MAY FALL SHORT AND STILL RUN CLEAR ACROSS A PIECE. Same
# tenth of a millimetre: a pass drawn to the blank edge lands on it exactly, and
# a pass that stops a jump distance short is stopping millimetres short.
SPAN_TOL_MM = 0.1


class Board(NamedTuple):
    """One board instance, and what is holding it into the panel."""

    label: str
    box: tuple
    points: tuple
    # One of "cut", "tab", "saw" or "joined" per point in `points`, in order.
    kinds: tuple

    @property
    def joined_mm(self) -> float:
        """How much of this board's edge is plain material joining it in."""
        return SAMPLE_MM * sum(1 for kind in self.kinds if kind == "joined")

    @property
    def sawn_mm(self) -> float:
        """How much of this board's edge stands on a saw line."""
        return SAMPLE_MM * sum(1 for kind in self.kinds if kind == "saw")


class Verdict(NamedTuple):
    """Whether the panel comes apart, the order to break it in, and what does not.

    `steps` is written for a person at the bench: one sentence per saw pass or
    per handful of tabs, in the order they happen. `trapped` is one sentence per
    board that never came free, saying what is still holding it.
    """

    comes_apart: bool
    steps: tuple = ()
    trapped: tuple = ()

    @property
    def trapped_count(self) -> int:
        return len(self.trapped)

    @property
    def status(self) -> str:
        """The two words the V-Scores menu shows beside its status dot."""
        if self.comes_apart:
            return "comes apart"
        count = self.trapped_count
        return f"trapped: {count} board" + ("s" if count != 1 else "")


# ---------------------------------------------------------------------------
# walking the geometry
# ---------------------------------------------------------------------------


def walk(primitive, step_mm: float = SAMPLE_MM) -> list:
    """Points along one line or arc, ends included, no wider apart than *step*."""
    length = primitive.length
    count = max(1, math.ceil(length / step_mm))
    if isinstance(primitive, Arc):
        sweep = primitive.sweep * (1.0 if primitive.ccw else -1.0)
        return [
            primitive.point_at_angle(primitive.a0 + sweep * index / count)
            for index in range(count + 1)
        ]
    p0, p1 = primitive.p0, primitive.p1
    return [
        (p0[0] + (p1[0] - p0[0]) * index / count,
         p0[1] + (p1[1] - p0[1]) * index / count)
        for index in range(count + 1)
    ]


def placed(point: tuple, place) -> tuple:
    """One source-frame point turned and moved onto its instance.

    The transform `Placement` documents and `inject_board` applies: a turn about
    the origin, then the offset. Written out here rather than imported so this
    module needs nothing from the pour.
    """
    turned = route_toolpath.rotate_kicad(point, place.rotation_deg)
    return (turned[0] + place.dx_mm, turned[1] + place.dy_mm)


def rect_points(box: tuple, step_mm: float = SAMPLE_MM) -> list:
    """The four sides of a rectangle, walked - the fallback board edge."""
    corners = (
        (box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]),
    )
    points = []
    for index in range(4):
        side = Line(p0=corners[index], p1=corners[(index + 1) % 4])
        points.extend(walk(side, step_mm)[:-1])
    return points


def board_points(geometry, place, box: tuple) -> list:
    """Where one board instance's own edge runs, as points on the panel.

    The SOURCE board's whole outline, turned and stepped - because the panel no
    longer holds it: by the time this runs the instances' outlines have been
    replaced by the panel's one derived edge. A panel built without that outline
    falls back to the instance RECTANGLE, which is pessimistic on a shaped board
    and never optimistic.
    """
    outline = getattr(geometry, "source_outline", ()) or ()
    if not outline:
        return rect_points(box)
    points = []
    for primitive in outline:
        points.extend(placed(point, place) for point in walk(primitive)[:-1])
    return points


# ---------------------------------------------------------------------------
# what is holding each board in
# ---------------------------------------------------------------------------


def saw_lines(panel_board) -> list:
    """Every saw pass on the finished panel, as geometry.

    Read off the PANEL'S OWN `User.38`, not off the settings that asked for the
    passes: the board is what the fabricator gets, and a pass the merge moved or
    a jump shortened has to be judged where it ended up.
    """
    return [
        Line(p0=tuple(line.start), p1=tuple(line.end), role="saw")
        for line in panel_scores_stage.score_lines_on(panel_board)
        if math.dist(tuple(line.start), tuple(line.end)) > ON_TOL_MM
    ]


def cut_index(cut_channels) -> tuple:
    """Every piece of cutter path with its radius, plus a lattice over them."""
    items = [
        (primitive, channel.radius)
        for channel in cut_channels
        for primitive in channel.path
    ]
    boxes = [padded_bbox(item, radius) for item, radius in items]
    return items, Grid(boxes, index_cell_mm(boxes) if boxes else 1.0)


def classify(point: tuple, items, index, tabs, lines) -> str:
    """What is at one point on a board's edge: "cut", "tab", "saw" or "joined".

    The order is the order a fabricator would look: gone already, held by a tab
    somebody put there on purpose, standing on a line the saw will take, or
    plain material nobody planned to remove.
    """
    probe = (point[0], point[1], point[0], point[1])
    for near in index.near(probe):
        item, radius = items[near]
        if distance_to_primitive(point, item) <= radius + ON_TOL_MM:
            return "cut"
    for base in tabs:
        # THE TAB'S OWN WIDTH IS THE ALLOWANCE. A staple's
        # base is drawn on the IDEAL board edge, while the outline at that tab
        # follows the break contour, which stands off it - measured up to
        # 0.45 mm off a 0.82 mm base on the corpus board. A contour standing
        # further off than the tab is wide would be a hole in the board rather
        # than a tab, so the base length is the reach and no number is invented.
        if distance_to_primitive(point, base) <= base.length + ON_TOL_MM:
            return "tab"
    for line in lines:
        if distance_to_primitive(point, line) <= ON_TOL_MM:
            return "saw"
    return "joined"


def boards_of(geometry, made, panel_board) -> list:
    """Every board instance, walked and classified. One pass over the geometry."""
    items, index = cut_index(getattr(made, "cut_channels", ()) or ())
    tabs = [tab.base for tab in route_toolpath.tabs_of(panel_board, [])]
    lines = saw_lines(panel_board)
    boxes = geometry.instance_boxes
    out = []
    for place, box in zip(geometry.placements, boxes):
        points = board_points(geometry, place, box)
        out.append(Board(
            label=f"column {place.column + 1}, row {place.row + 1}",
            box=tuple(box),
            points=tuple(points),
            kinds=tuple(
                classify(point, items, index, tabs, lines) for point in points
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# breaking the panel up
# ---------------------------------------------------------------------------


def splits_of(lines, boxes) -> list:
    """Every saw pass that could ever split something, as `(axis, position)`.

    A pass that runs through the INSIDE of a board is not a split - it saws the
    board in half, which is not what anybody is holding the panel to do. A pass
    along a board's edge is exactly what is wanted and is kept.
    """
    found = set()
    for line in lines:
        dx = abs(line.p1[0] - line.p0[0])
        dy = abs(line.p1[1] - line.p0[1])
        if dx <= ON_TOL_MM and dy > ON_TOL_MM:
            axis, at = "vertical", (line.p0[0] + line.p1[0]) / 2.0
        elif dy <= ON_TOL_MM and dx > ON_TOL_MM:
            axis, at = "horizontal", (line.p0[1] + line.p1[1]) / 2.0
        else:
            continue
        low = 0 if axis == "vertical" else 1
        if any(box[low] + ON_TOL_MM < at < box[low + 2] - ON_TOL_MM
               for box in boxes):
            continue
        found.add((axis, round(at, 6)))
    return sorted(found)


def spans(line, piece: tuple) -> bool:
    """Does this pass run clear across *piece*, edge to edge?"""
    if abs(line.p1[0] - line.p0[0]) <= ON_TOL_MM:
        low, high = sorted((line.p0[1], line.p1[1]))
        return low <= piece[1] + SPAN_TOL_MM and high >= piece[3] - SPAN_TOL_MM
    low, high = sorted((line.p0[0], line.p1[0]))
    return low <= piece[0] + SPAN_TOL_MM and high >= piece[2] - SPAN_TOL_MM


def cuts_in(piece: tuple, splits, lines) -> list:
    """The passes that split *piece* in two: inside it, and clear across it."""
    out = []
    for axis, at in splits:
        low = 0 if axis == "vertical" else 1
        if not piece[low] + SPAN_TOL_MM < at < piece[low + 2] - SPAN_TOL_MM:
            continue
        if any(spans(line, piece) and _on_axis(line, axis, at) for line in lines):
            out.append((axis, at))
    return out


def _on_axis(line, axis: str, at: float) -> bool:
    if axis == "vertical":
        return abs(line.p0[0] - at) <= ON_TOL_MM and abs(line.p1[0] - at) <= ON_TOL_MM
    return abs(line.p0[1] - at) <= ON_TOL_MM and abs(line.p1[1] - at) <= ON_TOL_MM


def halves(piece: tuple, axis: str, at: float) -> tuple:
    """*piece* sawn in two along one pass."""
    if axis == "vertical":
        return ((piece[0], piece[1], at, piece[3]),
                (at, piece[1], piece[2], piece[3]))
    return ((piece[0], piece[1], piece[2], at),
            (piece[0], at, piece[2], piece[3]))


def inside(box: tuple, piece: tuple) -> bool:
    """Is this board's middle in this piece? A board belongs to one piece only."""
    middle = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    return (piece[0] <= middle[0] <= piece[2]
            and piece[1] <= middle[1] <= piece[3])


def on_edge(point: tuple, piece: tuple) -> bool:
    """Is this point on the piece's own edge - material the saw already passed?"""
    return (
        abs(point[0] - piece[0]) <= ON_TOL_MM
        or abs(point[0] - piece[2]) <= ON_TOL_MM
        or abs(point[1] - piece[1]) <= ON_TOL_MM
        or abs(point[1] - piece[3]) <= ON_TOL_MM
    )


def loose_in(board: Board, piece: tuple) -> bool:
    """Does this board come out of *piece* by snapping tabs and nothing else?"""
    return all(
        kind in ("cut", "tab") or on_edge(point, piece)
        for point, kind in zip(board.points, board.kinds)
    )


def datum(axis: str, at: float, blank) -> float:
    """A coordinate as the report reads it: across or down from the top-left."""
    return at - (blank.min_x if axis == "vertical" else blank.min_y)


def _saw_sentence(axis: str, at: float, blank) -> str:
    where = datum(axis, at, blank)
    if axis == "vertical":
        return f"Saw down the line {where:.2f} mm across from the top-left corner."
    return f"Saw across the line {where:.2f} mm down from the top-left corner."


def _snap_sentence(labels) -> str:
    if len(labels) == 1:
        return f"Snap out the board at {labels[0]}."
    listed = "; ".join(labels)
    return f"Snap out the {len(labels)} boards at {listed}."


def _stuck_sentence(board: Board) -> str:
    joined, sawn = board.joined_mm, board.sawn_mm
    if joined > 0.0:
        return (
            f"The board at {board.label} is joined to the panel along "
            f"{joined:.1f} mm of plain material - no cut, no tab and no saw "
            f"line there."
        )
    return (
        f"The board at {board.label} stands on {sawn:.1f} mm of saw line that "
        f"no pass reaches clear across the panel."
    )


def break_up(piece: tuple, boards, splits, lines, blank, steps: list,
             stuck: list) -> None:
    """Break one piece down as far as its passes allow, writing the order out.

    Greedy on purpose (see the module docstring): every split is taken, none is
    undone, and the leaves are the same leaves any other order would reach.
    """
    held = [board for board in boards if inside(board.box, piece)]
    free = [board for board in held if loose_in(board, piece)]
    if free:
        steps.append(_snap_sentence([board.label for board in free]))
    rest = [board for board in held if board not in free]
    if not rest:
        return
    cuts = cuts_in(piece, splits, lines)
    if not cuts:
        stuck.extend(rest)
        return
    axis, at = cuts[0]
    steps.append(_saw_sentence(axis, at, blank))
    for half in halves(piece, axis, at):
        break_up(half, rest, splits, lines, blank, steps, stuck)


def verdict(panel_board, made, geometry) -> Verdict:
    """Does this panel come apart? The order if it does, the boards if it does not.

    *made* is a `panelize.PanelizeResult`; *geometry* the build's
    `PanelGeometry`. Never raises on a panel it dislikes - a panel that cannot
    be taken apart is a report, not an error.
    """
    blank = made.blank
    boards = boards_of(geometry, made, panel_board)
    lines = saw_lines(panel_board)
    splits = splits_of(lines, [board.box for board in boards])
    steps: list = []
    stuck: list = []
    break_up(blank.bbox, boards, splits, lines, blank, steps, stuck)
    return Verdict(
        comes_apart=not stuck,
        steps=tuple(steps),
        trapped=tuple(_stuck_sentence(board) for board in stuck),
    )


def report_lines(check: Verdict) -> list:
    """What the panel report says about coming apart, in the order it happens."""
    if check.comes_apart:
        out = [
            f"The panel comes apart in {len(check.steps)} step"
            + ("s" if len(check.steps) != 1 else "") + ":"
        ]
    else:
        out = [
            f"{check.trapped_count} board"
            + ("s" if check.trapped_count != 1 else "")
            + " cannot be freed from this panel."
        ]
        out.extend(check.trapped)
        if not check.steps:
            return out
        out.append("As far as it does come apart:")
    out.extend(f"{number}. {text}" for number, text in enumerate(check.steps, 1))
    return out
