"""An independent verdict on a derived panel outline: is it a boundary at all?

This module answers one question and it answers it WITHOUT ASKING THE CODE THAT
BUILT THE ANSWER. :mod:`eedesignpanels.channels` composes the panel edge by
arranging offset curves, splitting them at their crossings and keeping the
pieces that survive a test; everything here is written the other way round -
from the DEFINITION of the region - so that a mistake in the composition cannot
also be a mistake in the check.

**The definition.** The panel is the blank rectangle minus the material a cutter
removed, and the cutter removed every point within its own radius of some
toolpath item. So a point of the finished `Edge.Cuts` is a boundary point only
if all three of these hold:

* it is inside the blank, edges included;
* it is not STRICTLY inside any swept region - a boundary may touch a cut, it
  may not run through one;
* it is ON something: at exactly one cutter radius from some toolpath item, or
  on the blank's own rectangle. A piece of outline that is neither is floating
  in solid material, which is the shape of every "slot no cutter could make"
  this project has produced.

and locally, the two sides have to disagree: step a hair off the boundary either
way, and exactly one of the two points is material - the same one the outline's
own parity says is inside the panel.

**Nothing here trusts a signed area, a winding rule or a loop walk from the
module under test.** Whether a point is in the panel is decided by casting a ray
across the finished loops and counting crossings, arcs measured exactly, with
the sample nudged whenever the ray would pass through a vertex or a tangency.
Whether a point is material is decided by distance to the toolpath. The two
answers are compared.

Four families of defect come out, and each carries a millimetre coordinate so a
review page can crop to it:

``open``       a loop that does not close, or a node where a number of pieces
               other than two meet.
``crossing``   two pieces of outline that cross or touch away from a shared end.
``off_wall``   a boundary point that is not at a cutter radius from anything and
               not on the blank - or one that is inside a cut, or outside the
               blank.
``region``     the outline and the material disagree about which side is the
               panel, either along an edge or at a sampled point of the blank.

Pure geometry: millimetre floats, no board, no I/O. It imports
:mod:`eedesignpanels.toolpath` for its primitive types and its index, and `math`.
"""

import math
from typing import NamedTuple

from eedesignpanels.toolpath import (
    Arc,
    Grid,
    Line,
    bounded_intersections,
    chain_bbox,
    distance_to_primitive,
    index_cell_mm,
    padded_bbox,
)

# How far a boundary point may sit off the true offset and still be that offset.
# The arrangement welds pieces onto their neighbours by up to
# `channels.BOUNDARY_NODE_TOL_MM` (5 um), and the file itself holds nanometres,
# so ten micrometres is twice the largest move the construction makes and four
# hundred times the nanometre the file rounds to. Every defect this project has
# seen was hundreds of micrometres wide; nothing real hides under this.
WALL_TOL_MM = 1e-2

# How far INSIDE a swept region a boundary point may be before it is wrong. The
# same allowance, for the same reason - a wall welded 5 um along its neighbour
# can land 5 um inside the cut it borders.
CUT_TOL_MM = 1e-2

# How far off the boundary the two side probes are taken. FOUR TIMES the
# welding slack, so a wall that was welded five micrometres onto its neighbour
# still has the probe land clearly on either side of it; and a fifth of the
# narrowest wall these panels have, which is the 0.1 mm radius a 0.2 mm cutter
# leaves.
PROBE_MM = 0.02

# ...and the fractions of it the probe is retried at where the material is
# narrower than the probe itself. A quarter and a sixteenth: the smallest is
# 1.25 um, which is still a thousand times the nanometre the file holds.
PROBE_SCALES = (1.0, 0.25, 0.0625)

# Where the samples go along one piece of outline: every fiftieth of a
# millimetre, and never fewer than three, so a piece shorter than the step is
# still looked at in its middle rather than skipped.
SAMPLE_MM = 0.02
MIN_SAMPLES = 3

# HOW MANY SAMPLES IN A ROW HAVE TO FAIL BEFORE IT IS A DEFECT, and this is the
# one number in the module that is a judgement rather than a definition. The
# side probes are taken along the normal, and at a sharp CONCAVE corner - where
# two walls of the same cut converge - a normal twenty micrometres long reaches
# across into the neighbouring cut, so the sample either side of such a corner
# can read "no material on either side" while the outline is perfectly correct.
# That artifact is one sample wide, at the end of a piece. Every defect this
# project has produced was a whole wall in the wrong place - hundreds of
# micrometres, dozens of samples. Three in a row is a tenth of a millimetre of
# continuous disagreement, which is fifty times the file's own precision and
# well under the smallest wrong wall anybody has drawn.
MIN_RUN = 3

# The lattice the whole blank is sampled on for the region comparison, and how
# far from the outline a sample has to be to count. A quarter of a millimetre
# over a panel is a few hundred thousand points, which is seconds; the raster
# oracle is what looks closer than this.
REGION_STEP_MM = 0.25
REGION_BAND_MM = 0.03

# Two pieces of outline that meet share a node, and a node is a point both of
# them end at. Anything they have in common further from both ends than this is
# a crossing. Five micrometres is what the loop walk itself welds at.
NODE_TOL_MM = 5e-3


class Defect(NamedTuple):
    """One thing wrong with an outline, and where in millimetres it is."""

    kind: str
    at: tuple
    detail: str
    span_mm: float = 0.0

    def __str__(self) -> str:
        return (f"{self.kind} at ({self.at[0]:.4f}, {self.at[1]:.4f}) mm: "
                f"{self.detail}")


class OutlineReport(NamedTuple):
    """What the checker found, and how hard it looked.

    `defects` are the verdict and `advisories` are not, and the line between
    them is whether the reading can be wrong. A loop that does not close, a wall
    that is nowhere near a cutter radius from any toolpath, an outline with the
    panel on the empty side, a loose chip of material - each of those is true or
    false and nothing about the measurement can soften it. How THICK a piece of
    standing material is cannot be decided by one ray: every peninsula tapers to
    a point at its end, so a reading under the minimum tab is sometimes a
    whisker and sometimes the tip of something solid. Those are measured,
    located and reported, and they do not fail a build.
    """

    defects: tuple
    edge_samples: int
    region_samples: int
    loops: int
    pieces: int
    advisories: tuple = ()
    # The thinnest standing material a one-ray probe found, and where.
    thinnest_web_mm: float = math.inf
    thinnest_web_at: tuple = (0.0, 0.0)

    @property
    def ok(self) -> bool:
        return not self.defects

    @property
    def kinds(self) -> tuple:
        """Which families failed, in a fixed order, for a one-line report."""
        return tuple(sorted({defect.kind for defect in self.defects}))

    def of_kind(self, kind: str) -> tuple:
        return tuple(defect for defect in self.defects if defect.kind == kind)

    @property
    def summary(self) -> str:
        noted = (f"; {len(self.advisories)} thin place(s) noted, the thinnest "
                 f"{self.thinnest_web_mm:.4f} mm" if self.advisories else "")
        if self.ok:
            return (f"outline valid: {self.loops} loop(s), {self.pieces} "
                    f"piece(s), {self.edge_samples} edge and "
                    f"{self.region_samples} area samples agree" + noted)
        counts: dict = {}
        for defect in self.defects:
            counts[defect.kind] = counts.get(defect.kind, 0) + 1
        listed = ", ".join(
            f"{count} {kind}" for kind, count in sorted(counts.items())
        )
        return (f"outline INVALID: {len(self.defects)} defect(s) - {listed}"
                + noted)


# ---------------------------------------------------------------------------
# the two truths, each computed from a definition
# ---------------------------------------------------------------------------


def sweeps_of(channels) -> list:
    """`(primitive, radius)` for every toolpath item in every channel."""
    return [
        (item, channel.radius)
        for channel in channels for item in channel.path
    ]


class Material:
    """Is a point solid? The blank, minus everything a cutter reached.

    The definition and nothing else: a point is cut when it is nearer to some
    toolpath item than that item's own radius. The index only decides which
    items are worth measuring against.
    """

    def __init__(self, blank, sweeps: list):
        self.blank = blank
        self.sweeps = list(sweeps)
        boxes = [padded_bbox(item, radius) for item, radius in self.sweeps]
        self.grid = Grid(boxes, index_cell_mm(boxes) if boxes else 1.0)

    def clearance_mm(self, point: tuple) -> float:
        """Distance out of the nearest cut. Negative inside one, `inf` if clear."""
        best = math.inf
        box = (point[0], point[1], point[0], point[1])
        for index in self.grid.near(box):
            item, radius = self.sweeps[index]
            best = min(best, distance_to_primitive(point, item) - radius)
        return best

    def in_blank(self, point: tuple, tol: float = 0.0) -> bool:
        return (
            self.blank.min_x - tol <= point[0] <= self.blank.max_x + tol
            and self.blank.min_y - tol <= point[1] <= self.blank.max_y + tol
        )

    def blank_edge_distance_mm(self, point: tuple) -> float:
        """How far the point is from the blank's rectangle, unsigned."""
        inside = self.in_blank(point)
        gaps = (
            point[0] - self.blank.min_x, self.blank.max_x - point[0],
            point[1] - self.blank.min_y, self.blank.max_y - point[1],
        )
        if inside:
            return min(abs(gap) for gap in gaps)
        dx = max(self.blank.min_x - point[0], 0.0, point[0] - self.blank.max_x)
        dy = max(self.blank.min_y - point[1], 0.0, point[1] - self.blank.max_y)
        return math.hypot(dx, dy)

    def is_material(self, point: tuple, tol: float = 0.0) -> bool:
        """Solid: inside the blank and out of every cut."""
        return self.in_blank(point, tol) and self.clearance_mm(point) > tol


class OutlineRegion:
    """Which side of the finished loops a point is on, by counting crossings.

    A ray is cast in +x from the point and every piece it crosses is counted;
    an odd count is inside. Arcs are met exactly - a horizontal line and a
    circle - so nothing here approximates a curve by segments.

    **Degeneracy is avoided rather than reasoned about.** A ray through a vertex
    or through the top of an arc has no honest parity, so the sample is nudged
    in y until it passes through neither. The caller is asking about a point it
    chose a hair off an edge, and a hair off that is the same question.
    """

    # A ray this close to a vertex or an arc's extreme in y is degenerate.
    DEGENERATE_MM = 1e-7
    NUDGES = (0.0, 3.7e-6, -5.1e-6, 8.3e-6, -1.19e-5)

    def __init__(self, loops):
        self.pieces = [piece for loop in loops for piece in loop]
        self.boxes = [chain_bbox([piece]) for piece in self.pieces]
        self.critical = sorted(self._critical_heights())
        cell = index_cell_mm(self.boxes) if self.boxes else 1.0
        self.cell = max(cell, 1e-3)
        self.rows: dict = {}
        for index, box in enumerate(self.boxes):
            low = math.floor(box[1] / self.cell)
            high = math.floor(box[3] / self.cell)
            for row in range(low, high + 1):
                self.rows.setdefault(row, []).append(index)

    def _critical_heights(self) -> set:
        """Every y a ray must not be at: the vertices, and each arc's extremes."""
        heights = set()
        for piece in self.pieces:
            heights.add(piece.start[1])
            heights.add(piece.end[1])
            if isinstance(piece, Arc):
                for angle in (0.5 * math.pi, 1.5 * math.pi):
                    if piece.contains_angle(angle):
                        heights.add(piece.point_at_angle(angle)[1])
        return heights

    def _degenerate(self, y: float) -> bool:
        for height in self.critical:
            if abs(height - y) <= self.DEGENERATE_MM:
                return True
        return False

    def contains(self, point: tuple) -> bool:
        """True when *point* is inside the panel the loops describe."""
        for nudge in self.NUDGES:
            y = point[1] + nudge
            if self._degenerate(y):
                continue
            return self._parity(point[0], y)
        raise ValueError(
            f"every ray from ({point[0]:.6f}, {point[1]:.6f}) mm passes "
            f"through a vertex - this outline has geometry stacked in y"
        )

    def _parity(self, x: float, y: float) -> bool:
        count = 0
        for index in self.rows.get(math.floor(y / self.cell), ()):
            box = self.boxes[index]
            if not (box[1] <= y <= box[3]) or box[2] < x:
                continue
            for crossing in self._crossings(self.pieces[index], y):
                if crossing > x:
                    count += 1
        return count % 2 == 1

    @staticmethod
    def _crossings(piece, y: float) -> list:
        """Where the horizontal line at *y* meets this piece. Exact."""
        if isinstance(piece, Line):
            (x0, y0), (x1, y1) = piece.p0, piece.p1
            if (y0 <= y) == (y1 <= y) or y0 == y1:
                return []
            return [x0 + (y - y0) * (x1 - x0) / (y1 - y0)]
        offset = y - piece.center[1]
        if abs(offset) >= piece.radius:
            return []
        half = math.sqrt(piece.radius * piece.radius - offset * offset)
        found = []
        for x in (piece.center[0] - half, piece.center[0] + half):
            angle = math.atan2(offset, x - piece.center[0])
            if piece.contains_angle(angle):
                found.append(x)
        return found


# ---------------------------------------------------------------------------
# sampling one piece of outline
# ---------------------------------------------------------------------------


def point_on(piece, position: float) -> tuple:
    """A point a fraction *position* of the way along *piece*."""
    if isinstance(piece, Line):
        return (
            piece.p0[0] + position * (piece.p1[0] - piece.p0[0]),
            piece.p0[1] + position * (piece.p1[1] - piece.p0[1]),
        )
    sweep = piece.sweep * (position if piece.ccw else -position)
    return piece.point_at_angle(piece.a0 + sweep)


def normal_on(piece, position: float) -> tuple:
    """The unit normal at that point, to the LEFT of the direction of travel."""
    if isinstance(piece, Line):
        dx = piece.p1[0] - piece.p0[0]
        dy = piece.p1[1] - piece.p0[1]
        length = math.hypot(dx, dy) or 1.0
        return (-dy / length, dx / length)
    sweep = piece.sweep * (position if piece.ccw else -position)
    tangent = piece.tangent_at_angle(piece.a0 + sweep)
    return (-tangent[1], tangent[0])


def samples_on(piece, step_mm: float = SAMPLE_MM) -> list:
    """Interior sample positions along a piece, ends excluded.

    Ends are excluded because they are nodes: two pieces share them, the probe
    either side of one lands in a wedge rather than across an edge, and whether
    a node is right is what the topology check answers.
    """
    count = max(MIN_SAMPLES, int(math.ceil(piece.length / step_mm)))
    return [(index + 0.5) / count for index in range(count)]


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def check_topology(loops, tol: float = NODE_TOL_MM) -> list:
    """Every loop closes, piece to piece, and comes back to where it started."""
    defects = []
    for number, loop in enumerate(loops, 1):
        if not loop:
            defects.append(Defect("open", (0.0, 0.0), f"loop {number} is empty"))
            continue
        for index, piece in enumerate(loop):
            following = loop[(index + 1) % len(loop)]
            gap = math.dist(piece.end, following.start)
            if gap > tol:
                defects.append(Defect(
                    "open", piece.end,
                    f"loop {number} has a {gap * 1000:.1f} um gap between "
                    f"piece {index + 1} and the next",
                    span_mm=gap,
                ))
    return defects


def check_crossings(loops, tol: float = NODE_TOL_MM) -> list:
    """No two pieces meet anywhere but at an end they share."""
    pieces = [piece for loop in loops for piece in loop]
    boxes = [padded_bbox(piece, tol) for piece in pieces]
    if not boxes:
        return []
    grid = Grid(boxes, index_cell_mm(boxes))
    defects = []
    for index, box in enumerate(boxes):
        for other in grid.near(box):
            if other <= index:
                continue
            ends = (
                pieces[index].start, pieces[index].end,
                pieces[other].start, pieces[other].end,
            )
            for point in bounded_intersections(
                pieces[index], pieces[other], tol=1e-7
            ):
                if any(math.dist(point, end) <= tol for end in ends):
                    continue
                defects.append(Defect(
                    "crossing", point,
                    "two pieces of the outline meet away from a shared end",
                ))
    return defects


def check_edges(loops, material: Material, region: OutlineRegion,
                step_mm: float = SAMPLE_MM, probe_mm: float = PROBE_MM,
                wall_tol: float = WALL_TOL_MM,
                cut_tol: float = CUT_TOL_MM,
                min_run: int = MIN_RUN, web: bool = False) -> tuple:
    """Every sampled point of outline is on the real boundary, right way round.

    A sample that fails on its own is not reported: see :data:`MIN_RUN`. What is
    reported is the MIDDLE of each run of failures, with the run's length in
    millimetres, so a review page crops to the wall rather than to its corner.

    Returns `(defects, samples, thinnest_web_mm, thinnest_web_at)`.
    """
    defects, samples = [], 0
    thinnest, thinnest_at = math.inf, (0.0, 0.0)
    for loop in loops:
        for piece in loop:
            positions = samples_on(piece, step_mm)
            failures: dict = {}
            for order, position in enumerate(positions):
                samples += 1
                point = point_on(piece, position)
                normal = normal_on(piece, position)
                found = _check_one_edge_sample(
                    point, normal, material, region, probe_mm, wall_tol, cut_tol,
                )
                for defect in found:
                    failures.setdefault(defect.kind, []).append((order, defect))
                if web:
                    ahead = _step(point, normal, probe_mm)
                    inward = normal if material.is_material(ahead) else (
                        -normal[0], -normal[1])
                    depth = _web_thickness(point, inward, material)
                    if depth < thinnest:
                        thinnest, thinnest_at = depth, point
            step_length = piece.length / max(len(positions), 1)
            defects.extend(_runs_of(failures, min_run, step_length))
    return tuple(defects), samples, thinnest, thinnest_at


def _runs_of(failures: dict, min_run: int, step_length: float) -> list:
    """Consecutive failing samples, collapsed into one defect each."""
    reported = []
    for entries in failures.values():
        run: list = []
        for order, defect in entries:
            if run and order != run[-1][0] + 1:
                reported.extend(_report_run(run, min_run, step_length))
                run = []
            run.append((order, defect))
        reported.extend(_report_run(run, min_run, step_length))
    return reported


def _report_run(run: list, min_run: int, step_length: float) -> list:
    if len(run) < min_run:
        return []
    worst = max(run, key=lambda entry: entry[1].span_mm)[1]
    middle = run[len(run) // 2][1]
    return [middle._replace(
        span_mm=max(worst.span_mm, len(run) * step_length),
        detail=f"{middle.detail} ({len(run) * step_length:.3f} mm of it)",
    )]


def _step(point: tuple, direction: tuple, distance: float) -> tuple:
    return (point[0] + direction[0] * distance,
            point[1] + direction[1] * distance)


def _check_one_edge_sample(point, normal, material, region, probe_mm,
                           wall_tol, cut_tol) -> list:
    """One sample of outline against the definition."""
    defects = []
    clearance = material.clearance_mm(point)
    to_blank = material.blank_edge_distance_mm(point)

    if not material.in_blank(point, tol=wall_tol):
        defects.append(Defect(
            "off_wall", point,
            f"the outline runs {to_blank:.4f} mm outside the blank",
            span_mm=to_blank,
        ))
    elif clearance < -cut_tol:
        defects.append(Defect(
            "off_wall", point,
            f"the outline runs {-clearance:.4f} mm inside a cut - a boundary "
            f"may touch the material a cutter removed, never cross it",
            span_mm=-clearance,
        ))
    elif abs(clearance) > wall_tol and to_blank > wall_tol:
        defects.append(Defect(
            "off_wall", point,
            f"the outline is {clearance:.4f} mm clear of every cut and "
            f"{to_blank:.4f} mm from the blank's edge, so it is a cut no "
            f"cutter made",
            span_mm=min(abs(clearance), to_blank),
        ))

    # THE PROBE HAS TO BE SMALLER THAN THE FEATURE. Material that tapers to a
    # point - the tip of every peninsula, and every place two cuts meet - is
    # thinner than the probe within a few tens of micrometres of its end, and a
    # probe that reaches across it reads "no material on either side" of a wall
    # that is perfectly correct. So the probe is stepped down until the two
    # sides disagree, and only a sample that fails at EVERY scale is reported.
    for scale in PROBE_SCALES:
        ahead = _step(point, normal, probe_mm * scale)
        behind = _step(point, normal, -probe_mm * scale)
        solid_ahead = material.is_material(ahead)
        if solid_ahead == material.is_material(behind):
            continue
        inside_ahead = region.contains(ahead)
        if inside_ahead == region.contains(behind):
            defects.append(Defect(
                "region", point,
                "the outline does not separate here - both sides of it read "
                "as the same side of the panel",
            ))
        elif inside_ahead != solid_ahead:
            defects.append(Defect(
                "region", point,
                "the outline has the panel on the side the cutter emptied",
            ))
        return defects
    solid = material.is_material(_step(point, normal, probe_mm * PROBE_SCALES[-1]))
    defects.append(Defect(
        "region", point,
        "the material is the same on both sides of this outline, so it is not "
        "an edge of anything"
        if solid else
        "there is no material on either side of this outline",
    ))
    return defects


# How deep the web probe looks, and in what steps. A millimetre is twice the
# widest minimum tab anybody sets; a fiftieth of that is finer than the file.
WEB_PROBE_MM = 1.0
WEB_STEP_MM = 0.02


def _web_thickness(point, inward, material) -> float:
    """How far into the material a ray from this point gets. Advisory."""
    depth = WEB_STEP_MM
    while depth <= WEB_PROBE_MM:
        if not material.is_material(_step(point, inward, depth)):
            return depth
        depth += WEB_STEP_MM
    return math.inf


def check_region(material: Material, region: OutlineRegion, loops,
                 step_mm: float = REGION_STEP_MM,
                 band_mm: float = REGION_BAND_MM) -> tuple:
    """The whole blank, on a lattice: does the outline enclose the material?

    This is what catches a loop that is missing altogether, or one that encloses
    the wrong side. Points within *band_mm* of the outline are skipped - the two
    answers are allowed to disagree at the edge itself, and the raster oracle is
    what looks at the edge closely.

    Returns `(defects, samples)`.
    """
    pieces = [piece for loop in loops for piece in loop]
    boxes = [padded_bbox(piece, band_mm) for piece in pieces]
    grid = Grid(boxes, index_cell_mm(boxes)) if boxes else None
    blank = material.blank
    defects, samples = [], 0
    seen: set = set()
    steps_x = int(math.floor(blank.width_mm / step_mm))
    steps_y = int(math.floor(blank.height_mm / step_mm))
    for row in range(steps_y + 1):
        y = blank.min_y + (row + 0.5) * step_mm
        if y >= blank.max_y:
            break
        for column in range(steps_x + 1):
            x = blank.min_x + (column + 0.5) * step_mm
            if x >= blank.max_x:
                break
            point = (x, y)
            if grid is not None and _near_outline(point, pieces, boxes, grid,
                                                  band_mm):
                continue
            samples += 1
            solid = material.is_material(point)
            if region.contains(point) == solid:
                continue
            key = (round(x, 0), round(y, 0))
            if key in seen:
                continue
            seen.add(key)
            defects.append(Defect(
                "region", point,
                "this point is material the outline leaves out of the panel"
                if solid else
                "this point was cut away and the outline keeps it in the panel",
            ))
    return tuple(defects), samples


def _near_outline(point, pieces, boxes, grid, band_mm) -> bool:
    box = (point[0], point[1], point[0], point[1])
    for index in grid.near(box):
        if distance_to_primitive(point, pieces[index]) <= band_mm:
            return True
    return False


# ---------------------------------------------------------------------------
# the minimum tab, checked on the FINISHED outline rather than on the rule
# ---------------------------------------------------------------------------

# How finely the material is probed for its thickness, and how far. Nothing
# thicker than the minimum tab is of interest, so the probe stops there.
WEB_STEP_DIVISIONS = 20

# HOW A SLIVER IS TOLD FROM THE TIP OF A PENINSULA, and this is the second
# judgement in the module.
#
# Every peninsula of material tapers to nothing at its end, and every hairpin in
# a toolpath leaves one - the material between the two legs of the turn comes to
# a point where the two cuts meet. Measured across, that tip is thinner than any
# minimum tab there is, and it is not a sliver: it is the end of something
# thick. A SLIVER has two nearly parallel cuts either side of it and is the same
# thickness along its length.
#
# So a run of thin readings is a sliver when the thickest reading in it is no
# more than three times the thinnest - the walls did not converge - OR when the
# run is longer than four minimum tabs, which no tip is. Measured on the corpus
# panel: the hairpin tips at a 0.889 mm cutter read 0.025 mm rising to 0.5 mm
# over 0.16 mm of outline, a ratio of twenty; the slivers a point-shaped join
# left behind at 0.6 mm read a flat 0.375 mm, a ratio of one.
TAPER_RATIO = 3.0
LONG_RUN_TABS = 4.0


def _crosses_all_blades(first: tuple, second: tuple, guard) -> bool:
    """Does the chord `first`-`second` cross every blade of *guard*?

    A declared tab is recognised by its own two stop lines: the chord across the
    tab crosses both, and nothing else on the board does. Duck-typed on
    `mintab.Guard` so this module keeps to `toolpath` and `math` - a guard is
    input the design wrote down, not a result of the code under test.
    """
    blades = getattr(guard, "blades", ())
    if not blades:
        return False
    chord = Line(p0=first, p1=second, source="probe", role="chord")
    return all(
        bounded_intersections(chord, blade, tol=1e-7) for blade in blades
    )


def guarded_here(first: tuple, second: tuple, guards) -> bool:
    """Is this chord across a gap the DESIGN declared - a tab or a score?"""
    for guard in guards:
        ends = getattr(guard, "ends", ())
        if ends and len(ends) == 2:
            near = min(
                math.dist(ends[0], first) + math.dist(ends[1], second),
                math.dist(ends[0], second) + math.dist(ends[1], first),
            )
            if near <= 1.0:
                return True
        if _crosses_all_blades(first, second, guard):
            return True
    return False


def check_slivers(loops, material: Material, min_tab_mm: float, guards=(),
                  step_mm: float = SAMPLE_MM, probe_mm: float = PROBE_MM,
                  min_run: int = MIN_RUN,
                  chips_are_defects: bool = True) -> tuple:
    """No standing material thinner than the minimum tab, and no loose chips.

    Two rules, and the second is the one that a point-shaped sliver rule breaks:

    ``chip``  a piece of material that is nowhere as wide as the minimum tab is
              swarf, not structure. It is recognised by being a loop of its own
              whose INSIDE is material and whose box is under the minimum tab -
              nothing legitimate on a panel is that small.

    **THE CHIP RULE IS CONDITIONAL**. A chip
    is a defect when the build was asked to eliminate slivers: the rule said the
    swarf would be cut away and here it is, standing. With the setting OFF it is
    the CONSEQUENCE the user chose and was warned about, so it is reported as
    an advisory instead. Nothing about the measurement changes - only whether it
    is a verdict, which is the one thing the setting can honestly move.
    ``web``   material with a cut on both sides of it, thinner than the minimum
              tab, over a run of the outline. A declared tab is exempt: that is
              the one place a design asks for material this thin, and the guard
              is how it says so.

    A peninsula's own TIP reads thin from every direction and is not a web, so
    the probe is taken along the inward normal only, and a single sample never
    reports: see :data:`MIN_RUN`.

    Returns `(chips, thin_places, thinnest_mm, thinnest_at)` - the first the
    verdict, the second a measurement.
    """
    defects, advisories = [], []
    thinnest, thinnest_at = math.inf, (0.0, 0.0)
    depth_step = min_tab_mm / WEB_STEP_DIVISIONS
    for loop in loops:
        chip = _chip_defect(loop, material, min_tab_mm, probe_mm, guards)
        if chip is not None:
            (defects if chips_are_defects else advisories).append(chip)
            continue
        failures: list = []
        order = -1
        for piece in loop:
            for position in samples_on(piece, step_mm):
                order += 1
                point = point_on(piece, position)
                normal = normal_on(piece, position)
                inward = normal if material.is_material(
                    _step(point, normal, probe_mm)) else (
                    -normal[0], -normal[1])
                depth = _web_depth(point, inward, material, min_tab_mm,
                                   depth_step)
                if depth < thinnest:
                    thinnest, thinnest_at = depth, point
                if depth >= min_tab_mm:
                    continue
                far = _step(point, inward, depth + probe_mm)
                if guarded_here(_step(point, inward, -probe_mm), far, guards):
                    continue
                failures.append((order, point, depth))
        advisories.extend(_web_runs(failures, min_run, step_mm, min_tab_mm))
    return tuple(defects), tuple(advisories), thinnest, thinnest_at


def _web_runs(failures: list, min_run: int, step_mm: float,
              min_tab_mm: float) -> list:
    """Consecutive thin readings, judged as slivers or as peninsula tips."""
    reported, run = [], []
    for entry in failures:
        if run and entry[0] != run[-1][0] + 1:
            reported.extend(_web_defect(run, min_run, step_mm, min_tab_mm))
            run = []
        run.append(entry)
    reported.extend(_web_defect(run, min_run, step_mm, min_tab_mm))
    return reported


def _web_defect(run: list, min_run: int, step_mm: float,
                min_tab_mm: float) -> list:
    if len(run) < min_run:
        return []
    depths = [entry[2] for entry in run]
    thinnest, thickest = min(depths), max(depths)
    length = len(run) * step_mm
    parallel = thickest <= TAPER_RATIO * max(thinnest, 1e-6)
    if not parallel and length <= LONG_RUN_TABS * min_tab_mm:
        return []
    point = run[len(run) // 2][1]
    return [Defect(
        "web", point,
        f"{thinnest:.4f} mm of material stands here over {length:.3f} mm of "
        f"outline, under the {min_tab_mm:.4f} mm minimum tab, and no tab is "
        f"declared across it",
        span_mm=thinnest,
    )]


def _web_depth(point, inward, material, limit, step) -> float:
    """How far into the material a ray gets, giving up at *limit*."""
    if material.is_material(_step(point, inward, limit)):
        return limit
    depth = step
    while depth < limit:
        if not material.is_material(_step(point, inward, depth)):
            return depth
        depth += step
    return limit


def _chip_defect(loop, material: Material, min_tab_mm: float, probe_mm: float,
                 guards):
    """Is this whole loop a loose chip of material? A defect if so."""
    if not loop:
        return None
    box = chain_bbox(list(loop))
    if min(box[2] - box[0], box[3] - box[1]) >= min_tab_mm:
        return None
    piece = loop[0]
    point = point_on(piece, 0.5)
    normal = normal_on(piece, 0.5)
    ahead = _step(point, normal, probe_mm)
    inward = normal if material.is_material(ahead) else (-normal[0], -normal[1])
    inside = OutlineRegion([loop])
    if not inside.contains(_step(point, inward, probe_mm)):
        return None
    width = min(box[2] - box[0], box[3] - box[1])
    if guarded_here(_step(point, inward, -probe_mm),
                    _step(point, inward, width + probe_mm), guards):
        return None
    return Defect(
        "chip", ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0),
        f"a loose piece of material {box[2] - box[0]:.4f} x "
        f"{box[3] - box[1]:.4f} mm stands here, under the {min_tab_mm:.4f} mm "
        f"minimum tab in every direction",
        span_mm=width,
    )


def check(blank, loops, channels, region_step_mm: float = REGION_STEP_MM,
          sample_mm: float = SAMPLE_MM, web: bool = False,
          min_tab_mm: float = 0.0, guards=(),
          eliminate_slivers: bool = True) -> OutlineReport:
    """The whole verdict on one derived outline.

    *blank* is the panel rectangle (`channels.Rect`), *loops* the finished
    `Edge.Cuts` and *channels* the swept runs the boolean was given - each one
    anything with a `path` of primitives and a `radius`.

    A region sample step of zero skips the area comparison, which is the
    expensive half; the edge checks alone are what a unit test usually wants.
    A `min_tab_mm` of zero skips the minimum-tab rules, which are the other
    expensive half and which need the design's own tab declarations to be
    honest - hand them in as *guards*.

    *eliminate_slivers* is the build's own switch. False demotes the loose-chip
    rule to an advisory: a chip is a defect when the build promised to cut the
    swarf away, and the warned-about consequence of the choice when it did not.
    """
    material = Material(blank, sweeps_of(channels))
    region = OutlineRegion(loops)
    defects = list(check_topology(loops))
    defects.extend(check_crossings(loops))
    edge_defects, edge_samples, thinnest, web_at = check_edges(
        loops, material, region, step_mm=sample_mm, web=web
    )
    defects.extend(edge_defects)
    region_samples = 0
    if region_step_mm > 0.0:
        region_defects, region_samples = check_region(
            material, region, loops, step_mm=region_step_mm
        )
        defects.extend(region_defects)
    advisories: tuple = ()
    if min_tab_mm > 0.0:
        sliver_defects, advisories, thin, thin_at = check_slivers(
            loops, material, min_tab_mm, guards=guards, step_mm=sample_mm,
            chips_are_defects=eliminate_slivers,
        )
        defects.extend(sliver_defects)
        if thin < thinnest:
            thinnest, web_at = thin, thin_at
    return OutlineReport(
        defects=tuple(defects),
        edge_samples=edge_samples,
        region_samples=region_samples,
        loops=len(loops),
        pieces=sum(len(loop) for loop in loops),
        advisories=advisories,
        thinnest_web_mm=thinnest,
        thinnest_web_at=web_at,
    )


__all__ = [
    "CUT_TOL_MM",
    "Defect",
    "Material",
    "OutlineRegion",
    "OutlineReport",
    "PROBE_MM",
    "SAMPLE_MM",
    "WALL_TOL_MM",
    "check",
    "check_crossings",
    "check_edges",
    "check_region",
    "check_slivers",
    "check_topology",
    "guarded_here",
    "normal_on",
    "point_on",
    "samples_on",
    "sweeps_of",
]
