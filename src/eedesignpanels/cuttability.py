"""Every closed loop as a routing job: which side the tool rides, and whether it fits.

The panel builder supports internal route-outs with the declared route cutter.
Each closed loop is a toolpath job, the side of the loop that is the board
exterior is resolved, and a feature the cutter cannot make is reported and
highlighted red.

Three things follow from that, and this module is where each is settled.

1. **EVERY CLOSED LOOP IS A JOB, CUT WITH THE DECLARED CUTTER.** The outer
   boundary is the job it has always been; each interior loop is another one.
   There is exactly one cutter - the number declared on step 1 - and no second
   one is invented for a hole. A hole the declared cutter cannot make is
   reported as one it cannot make.

2. **WHICH SIDE IS MATERIAL DECIDES WHERE THE TOOL RIDES**, and it is ASSERTED
   rather than assumed (:func:`straddles`). Getting it backwards puts the
   channel on the wrong side of the line, which is a panel that cuts the board
   away, so the parity test from :mod:`eedesignpanels.material` is asked at two
   points half a cutter width apart on opposite sides of the same edge and the
   two must DISAGREE - material on one side, air on the other, with the air on
   the side the verdict says the tool rides. Two probes that agree mean the
   loop is not the boundary between material and air where it was asked, and
   that job is refused by name and by coordinate rather than guessed at.

3. **A FEATURE THE CUTTER CANNOT MAKE IS REPORTED, NEVER SKIPPED.** Silence
   would leave the board without its holes and nothing would say the board was
   meant to have them. Each verdict here carries the
   loop's size and its centre so the report can name it, and the panel still
   builds - this is a report, not a refusal.

Two ways a loop defeats the declared cutter, and they are different reports:

* **it cannot be made at all** - the cutter does not fit inside it anywhere.
  That is not a rule of thumb, it is the erosion of the loop by the cutter
  radius coming out empty (:func:`eedesignpanels.toolpath.resolve_offset` with
  `inward=True`), which is the exact statement of "narrower than the cutter
  somewhere along its whole length". Four 1.3 mm holes against a 2 mm bit are
  this case.
* **it can be made, but not square** - an inside corner tighter than the
  cutter's radius cannot be reached, so it comes out rounded to the tool
  (:func:`eedesignpanels.toolpath.fillet_corners`). The feature is cut; the
  report says which corners are not the shape that was drawn.

**BOTH ARE RED.** A fab sees a rounded corner where a sharp one was drawn, and
that is a change to the part, so it is the same kind of fact as a hole the bit
cannot enter. Every square hole has four such corners and every notch has one,
so a great many ordinary boards show red - so the reason line says plainly
that the corner comes out rounded to the tool rather than sharp.

**A CUTOUT WIDER THAN THE CUTTER IS POCKETED ONLY WHEN ASKED.** It is a
checkbox, off by default, because most board houses do their own CAM work on
cutouts. Following the wall of a 6 mm cutout with
a 0.88 mm bit leaves a 4.24 mm middle attached to nothing. Off - the default -
that slug is left standing and the derived outline shows it honestly, because
the board house's own CAM decides what to do with it. On, concentric passes
mill it out (:func:`pocket_passes`). Either way the report names which cutouts
were pocketed and which were left.

Millimetres throughout, pure geometry, no I/O and no board.
"""

from typing import NamedTuple

from eedesignpanels import material
from eedesignpanels.toolpath import (
    DoesNotFit,
    OffsetError,
    chain_bbox,
    fillet_corners,
    resolve_offset,
    signed_area,
    weld_chain,
)

# What became of one loop. Two words, because a report a reader has to rank is
# a report they will get wrong: either the declared cutter makes this feature or
# it does not.
CUT = "cut"
NOT_MADE = "not made"

# What a loop IS, for a reader. The boundary is the board's own edge; the other
# two are what `material.loop_is_hole` distinguishes.
BOUNDARY = "the board boundary"
HOLE = "a hole in the board"
PIECE = "a separate piece"

# HOW FAR ONE POCKETING PASS STEPS IN FROM THE ONE BEFORE IT. Half the
# cutter, and that figure is a
# proof rather than a preference: a pass at depth `d` cuts the band from
# `d - radius` to `d + radius`, the walk stops at the first depth the erosion
# refuses, so the deepest material left sits under `d`, and a step of one
# radius puts the previous pass's outer reach at `d - radius + radius = d`.
# Anything wider than a radius can leave a rib standing in the middle of the
# cutout, which is the one thing switching this on is for.
POCKET_STEPOVER_FRACTION = 0.5

# ...AND HOW MANY PASSES ARE EVER MADE, the tool's own limit. A
# 100 mm cutout against a 0.1 mm bit is a thousand concentric loops written into
# the board, which is a file nobody asked for. The cap is stated, the pocket is
# refused whole rather than half-cut, and the report says the slug was left and
# why - a half-milled pocket that nobody was told about is the failure this
# project exists to prevent.
POCKET_MAX_PASSES = 500


class Job(NamedTuple):
    """One closed loop, judged against the declared cutter.

    `path` is the cutter's centreline when this job produced one, and `()` when
    it did not - either because the cutter will not fit, or because the boundary
    job's path is owned by the stage that routes tabs and scores into it.
    """

    index: int
    kind: str
    loop: tuple
    rides_inside: bool
    verdict: str
    reason: str
    bbox: tuple
    # What a reader calls this one: holes are numbered among holes and pieces
    # among pieces, from 1, so a board with four cutouts has a hole 1 and the
    # reader is never left looking for one.
    number: int = 0
    # Something worth saying about a job that is still CUT. The boundary's
    # side assertion lands here rather than in `reason`: the stage routes the
    # boundary whatever this module thinks of it, so a complaint about it is a
    # note and never a refusal.
    note: str = ""
    path: tuple = ()
    rounded: tuple = ()
    unreachable: tuple = ()
    # A CUTOUT WIDER THAN THE WHOLE CUTTER LEAVES ITS MIDDLE BEHIND. The tool
    # follows the wall and the material inside that ring is still attached to
    # nothing - it drops out as a loose slug. How big that middle is, measured,
    # and `()` when the bit covers the cutout end to end.
    slug_mm: tuple = ()
    # THE PASSES THAT TAKE IT OUT, AND ONLY WHEN THE BOX IS TICKED. Empty is
    # the default, because most board houses do their own CAM work on cutouts.
    pocket: tuple = ()
    # ...and why a pocket that was asked for was not made. One sentence, because
    # a cutout the user ticked and did not get is a thing the user has to be told.
    pocket_note: str = ""

    @property
    def is_boundary(self) -> bool:
        return self.kind == BOUNDARY

    @property
    def leaves_slug(self) -> bool:
        """Does a loose piece survive in the middle of this cutout?

        Measured, not assumed. A cutout wider than the whole
        cutter leaves one, and pocketing it is what takes it away - so the
        answer is the measurement AND the switch, read together in one place
        rather than by every caller that asks.
        """
        return bool(self.slug_mm) and not self.pocket

    @property
    def pocketed(self) -> bool:
        """Was this cutout milled out rather than left with its slug in it?"""
        return bool(self.pocket)

    @property
    def made(self) -> bool:
        return self.verdict == CUT

    @property
    def size_mm(self) -> tuple:
        """How big the feature is, across and down."""
        return (self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1])

    @property
    def centre(self) -> tuple:
        """The middle of the feature's own bounding box, in board millimetres."""
        return ((self.bbox[0] + self.bbox[2]) / 2.0,
                (self.bbox[1] + self.bbox[3]) / 2.0)

    @property
    def path_length_mm(self) -> float:
        """How far the cutter travels doing this job. Zero when it does not."""
        return sum(primitive.length for primitive in self.path)

    @property
    def label(self) -> str:
        """What the report calls this job. Numbered from the file order."""
        if self.is_boundary:
            return "the board boundary"
        return f"{'hole' if self.rides_inside else 'piece'} {self.number}"

    @property
    def where(self) -> str:
        """Size and centre in one clause - what a report needs to name it."""
        across, down = self.size_mm
        return (f"{across:.4f} by {down:.4f} mm, centred at "
                f"({self.centre[0]:.4f}, {self.centre[1]:.4f}) mm")


def ride_orientation(loop: list, rides_inside: bool) -> int:
    """The orientation number the offset machinery needs for this ride side.

    :func:`eedesignpanels.toolpath.resolve_offset` places every curve with
    `outward_normal(tangent, orientation)`, so the loop's own orientation puts
    the path outside what the loop encloses and the negative of it puts the path
    inside. One number, stated once, so the offset and the corner test cannot
    disagree about which side the tool is on.
    """
    orientation = 1 if signed_area(loop) > 0.0 else -1
    return -orientation if rides_inside else orientation


def straddles(loops: list, index: int, rides_inside: bool,
              cutter_diameter_mm: float) -> str:
    """Assert the tool rides in air with material on the other side. "" when it does.

    Assert the side, do not assume it. The two probes are
    half a cutter width off the same point of the same edge, on opposite sides
    (:func:`eedesignpanels.material.edge_probes`), and each is answered by the
    parity count that KiCad itself uses. A loop that really is a boundary
    between material and air gives two different answers; anything else - a
    second loop crossing between the probes, a loop drawn over itself, features
    closer together than the cutter - gives the same answer twice, and there is
    then no honest way to say where the channel goes.

    Returns the sentence a report should carry, which is empty when the
    assertion holds.
    """
    inside, outside, edge_point = material.edge_probes(
        loops[index], cutter_diameter_mm)
    inside_is_material = material.is_material(loops, inside)
    outside_is_material = material.is_material(loops, outside)
    if inside_is_material == outside_is_material:
        both = "material" if inside_is_material else "air"
        return (
            f"both sides of this loop read as {both} at "
            f"({edge_point[0]:.4f}, {edge_point[1]:.4f}) mm, probed "
            f"{cutter_diameter_mm / 2.0:.4f} mm out each way, so which side the "
            f"cutter rides on cannot be resolved and no channel is put there"
        )
    rides_in_material = inside_is_material if rides_inside else outside_is_material
    if rides_in_material:
        return (
            f"the cutter would ride in material at "
            f"({edge_point[0]:.4f}, {edge_point[1]:.4f}) mm, which is the wrong "
            f"side of this loop, so no channel is put there"
        )
    return ""


def _kind_of(loops: list, index: int, boundary: int,
             cutter_diameter_mm: float) -> tuple:
    """`(kind, rides_inside)` for one loop, by KiCad's own parity rule."""
    if index == boundary:
        return (BOUNDARY, False)
    is_hole = material.loop_is_hole(loops, index, cutter_diameter_mm)
    return (HOLE if is_hole else PIECE, is_hole)


def job_for(loops: list, index: int, boundary: int,
            cutter_diameter_mm: float, route: bool = True,
            pocket: bool = False) -> Job:
    """Judge one loop against the declared cutter. Never raises on geometry.

    *route* false computes everything except the path, which is what the
    boundary wants: its channel is produced by the stage that also puts the
    tabs, the scores and the minimum tab into it, and computing a second one
    here would be a second answer to the same question.

    *pocket* true mills a cutout wider than the cutter out instead of leaving
    its middle standing. It is OFF by default because most board houses do
    that CAM job themselves.
    """
    loop = weld_chain(list(loops[index]))
    kind, rides_inside = _kind_of(loops, index, boundary, cutter_diameter_mm)
    bbox = chain_bbox(loop)
    radius = cutter_diameter_mm / 2.0
    orientation = ride_orientation(loop, rides_inside)
    rounded = tuple(fillet_corners(loop, radius, orientation))

    try:
        wrong_side = straddles(loops, index, rides_inside, cutter_diameter_mm)
    except ValueError as degenerate:
        # A LOOP THAT ENCLOSES NOTHING IS REPORTED, NOT RAISED. A
        # break contour that runs out and back is a closed chain with no area,
        # and there is no point inside it to probe. It is also not a feature
        # anybody meant to cut, so it is named where it is and the build goes on
        # - a board that prepared before this existed must still prepare.
        wrong_side = str(degenerate)

    # THE BOUNDARY IS NEVER REFUSED BY THIS MODULE. The stage routes it with the
    # tabs, the scores and the minimum tab, and it has always routed it; a
    # failed assertion about it is a note on a job that still happens, because a
    # red verdict on the one loop that IS being cut would say the opposite of
    # what is true.
    if not route:
        return Job(index=index, kind=kind, loop=tuple(loop),
                   rides_inside=rides_inside, verdict=CUT, reason="",
                   bbox=bbox, rounded=rounded, note=wrong_side)

    if wrong_side:
        return Job(index=index, kind=kind, loop=tuple(loop),
                   rides_inside=rides_inside, verdict=NOT_MADE,
                   reason=wrong_side, bbox=bbox, rounded=rounded)

    try:
        offset = resolve_offset(loop, radius, inward=rides_inside)
    except DoesNotFit:
        return Job(
            index=index, kind=kind, loop=tuple(loop),
            rides_inside=rides_inside, verdict=NOT_MADE,
            reason=(
                f"a {cutter_diameter_mm:.4f} mm cutter does not fit inside it - "
                f"no point in this feature is {radius:.4f} mm clear of its own "
                f"edge, so the bit has nowhere to go"
            ),
            bbox=bbox, rounded=rounded,
        )
    except OffsetError as failure:
        return Job(
            index=index, kind=kind, loop=tuple(loop),
            rides_inside=rides_inside, verdict=NOT_MADE,
            reason=(
                f"a {cutter_diameter_mm:.4f} mm cutter cannot be walked round "
                f"it - {failure}"
            ),
            bbox=bbox, rounded=rounded,
        )
    slug = _slug_of(loop, cutter_diameter_mm) if rides_inside else ()
    passes, note = (
        pocket_passes(loop, cutter_diameter_mm) if slug and pocket else ((), "")
    )
    return Job(
        index=index, kind=kind, loop=tuple(loop), rides_inside=rides_inside,
        verdict=CUT, reason="", bbox=bbox, path=tuple(offset.loop),
        rounded=rounded, unreachable=tuple(offset.unreachable),
        slug_mm=slug, pocket=passes, pocket_note=note,
    )


def _slug_of(loop: list, cutter_diameter_mm: float) -> tuple:
    """How big a loose piece this cutout leaves in its middle, or `()`.

    The test is the definition: material survives inside the ring exactly when
    the cutout is wider than the WHOLE cutter somewhere, which is the erosion by
    a full diameter coming out non-empty. A cutout the bit covers end to end
    leaves nothing, and that is the ordinary small hole.
    """
    try:
        left = resolve_offset(loop, cutter_diameter_mm, inward=True)
    except OffsetError:
        return ()
    box = chain_bbox(left.loop)
    return (box[2] - box[0], box[3] - box[1])


def pocket_passes(loop: list, cutter_diameter_mm: float) -> tuple:
    """`(passes, note)` - the concentric cuts that take a cutout's middle out.

    **THE BOX IS OFF BY DEFAULT AND THIS IS WHAT IT DOES WHEN IT IS ON.**
    The wall pass is the cutter's centreline one
    radius in from the edge; each pass here is the same erosion taken one
    stepover deeper, and the walk stops at the first depth the erosion refuses -
    which is the depth at which no material is that far from the wall. With the
    stepover at one radius (:data:`POCKET_STEPOVER_FRACTION`) the bands overlap
    edge to edge, so nothing survives between two passes.

    The *note* is empty when the pocket is whole. It carries a sentence when the
    cap (:data:`POCKET_MAX_PASSES`) is reached, and the passes come back EMPTY
    in that case: half a pocket is a board that looks milled out and is not.

    One limit, stated because it is not obvious: an erosion that falls apart
    into two separate pools - a dumbbell-shaped cutout with a waist narrower
    than the cutter - is walked as one loop by
    :func:`eedesignpanels.toolpath.resolve_offset`, so only one pool is milled.
    Reported through the ordinary slug measurement, never worked around.
    """
    radius = cutter_diameter_mm / 2.0
    step = cutter_diameter_mm * POCKET_STEPOVER_FRACTION
    passes = []
    depth = radius + step
    while True:
        try:
            deeper = resolve_offset(loop, depth, inward=True)
        except OffsetError:
            return (tuple(passes), "")
        passes.append(tuple(deeper.loop))
        if len(passes) >= POCKET_MAX_PASSES:
            return ((), (
                f"a {cutter_diameter_mm:.4f} mm cutter needs more than "
                f"{POCKET_MAX_PASSES} passes to clear this cutout, so no "
                f"pocket is cut and the loose piece is left"
            ))
        depth += step


def plan(loops: list, boundary: int, cutter_diameter_mm: float,
         pocket: bool = False) -> tuple:
    """Every loop as a job, in the order the loops came off the file.

    File order rather than area order, for the reason
    :func:`eedesignpanels.stages.route_toolpath.boundary_loop` states: four
    identical cutouts differ only in float noise by area, and an order that
    repeats without explaining anything is worse than one that does neither.

    *pocket* is the user's checkbox, OFF by default: with it clear, no
    pocketing pass is planned.
    """
    jobs = [
        job_for(loops, index, boundary, cutter_diameter_mm,
                route=index != boundary, pocket=pocket)
        for index in range(len(loops))
    ]
    counted: dict = {}
    for position, job in enumerate(jobs):
        if job.is_boundary:
            continue
        counted[job.kind] = counted.get(job.kind, 0) + 1
        jobs[position] = job._replace(number=counted[job.kind])
    return tuple(jobs)


def report_lines(jobs, cutter_diameter_mm: float) -> list:
    """What was cut and what was not, one plain line at a time.

    A count first, because "did it do them all" is the
    question, then one line per feature that is not the shape it was drawn as.
    """
    jobs = tuple(jobs)
    if not jobs:
        return []
    made = [job for job in jobs if job.made]
    missed = [job for job in jobs if not job.made]
    out = [
        f"{len(jobs)} routing job" + ("s" if len(jobs) != 1 else "")
        + f" on one declared {cutter_diameter_mm:.4f} mm cutter: "
        f"{len(made)} cut, {len(missed)} left uncut."
    ]
    for job in missed:
        out.append(
            f"{job.label.capitalize()} is NOT cut, {job.where}: {job.reason}."
        )
    for job in made:
        # WHICH CUTOUTS WERE POCKETED AND WHICH WERE LEFT, EITHER WAY. Both
        # sentences, because "no line about it" reads
        # as "nothing wide enough to matter" and that is the one thing it does
        # not mean once the box exists.
        if job.pocketed:
            out.append(
                f"{job.label.capitalize()} is wider than the cutter and is "
                f"pocketed out in {len(job.pocket)} pass"
                + ("es" if len(job.pocket) != 1 else "")
                + ", so no loose piece is left."
            )
        if job.pocket_note:
            out.append(f"{job.label.capitalize()}: {job.pocket_note}.")
        if job.leaves_slug:
            out.append(
                f"{job.label.capitalize()} is wider than the cutter, so the "
                f"router leaves a loose piece {job.slug_mm[0]:.2f} x "
                f"{job.slug_mm[1]:.2f} mm in the middle of it."
            )
        if job.rounded:
            count = len(job.rounded)
            out.append(
                f"{job.label.capitalize()} is cut, with {count} corner"
                + ("s" if count != 1 else "")
                + f" rounded to the tool's {cutter_diameter_mm / 2.0:.4f} mm "
                f"radius instead of square."
            )
    return out


def alarms(jobs) -> tuple:
    """Every red sentence this board earns: features not made, corners rounded.

    An alarm is how this project holds a red state across a repaint
    (:mod:`eedesignpanels.preview.layer_alarms`), and TWO THINGS EARN ONE. A
    hole the bit cannot enter is the first. An inside corner tighter than the
    bit is the second, because what a fab receives is a rounded corner where a
    sharp one was drawn.

    Every square hole has four such corners and every notch has one, so a
    great many ordinary boards turn red here.

    Uncut features first, then corners in loop order, so the same board always
    reads the same way.
    """
    jobs = tuple(jobs)
    said = [
        f"{job.label} cannot be cut, {job.where}: {job.reason}"
        for job in jobs if not job.made
    ]
    for job in jobs:
        if not job.made:
            continue
        for corner in job.rounded:
            # ONE PLAIN SENTENCE. "Cannot be cut square" for an inside radius
            # is confusing, so the line says the cutter is too large to cut the
            # feature at a coordinate. The radius stays
            # because it is what the fab receives instead of the drawn corner.
            said.append(
                f"cutter too large to cut feature at ({corner.at[0]:.4f}, "
                f"{corner.at[1]:.4f}) mm - it comes out rounded to "
                f"{corner.radius_mm:.4f} mm"
            )
    return tuple(said)


def marked(jobs) -> tuple:
    """The jobs carrying something red, in loop order. What the overlay draws.

    The corners are red too, so "what is marked" is not the same as "what was
    not made". It is a question with one answer - asked here, so
    the live preview and the frozen review page cannot answer it differently.
    """
    return tuple(job for job in jobs if not job.made or job.rounded)
