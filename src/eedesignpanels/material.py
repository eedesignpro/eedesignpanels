"""Where a board has material and where it has air: loops, holes, and the cutter.

`Edge.Cuts` holds a SET of closed loops, and which side of any one of them is
material is not a property of that loop on its own. A loop inside another is a
hole; a loop inside a hole is an island of material again. So "is there material
here" is a question about the whole set at once, and this module is where it is
asked.

Two rules decide it.

1. PARITY, NOT A WINDING SUM. Count the loops a point lies inside and read the
   count's parity. KiCad decides the same way: a contour with an even number of
   parents is a top-level outline and an odd number makes it a hole
   (`parentIndexes.size() % 2 == 0` at
   `pcbnew/convert_shape_list_to_polygon.cpp:411`, the odd case at `:446`,
   KiCad 10.0.3), where a contour's parents are the other contours whose
   interior holds a point of it (`:392`). Mind the one-term difference between
   the two counts: a contour's parent count EXCLUDES the contour itself, so even
   means material there, while a free probe point is counted against every loop
   including the one it was taken off, so ODD means material here. Same rule,
   one term apart, and :func:`loop_is_hole` is the form that matches KiCad's
   literally.

   Why parity and not a signed turn total: the turn total was measured wrong on
   a real board. See
   :func:`eedesignpanels.toolpath.point_inside_loop` for that measurement and for
   what it replaced.

2. PROBE HALF A CUTTER WIDTH IN FROM THE EDGE, along the inward normal: the
   probe starts at the centre point of the cutter. Two reasons, and the second
   is the better one. It keeps the probe
   clear of the boundary, where every inside test is undefined. And it asks the
   question the router actually has - is there material where the tool would
   sit - rather than a question about a point on a line.

Millimetres throughout, and no I/O: this is the set-of-loops layer above
`eedesignpanels.toolpath`'s plane geometry, and nothing here reads a board.
"""

from eedesignpanels.toolpath import (
    CONTACT_TOL_MM,
    Arc,
    Line,
    midpoint_of,
    outward_normal,
    point_inside_loop,
    signed_area,
)


def loops_containing(loops: list, point: tuple, ignore: int = -1) -> int:
    """How many of *loops* hold *point* inside them, *ignore* excluded by index.

    `ignore` is what makes this KiCad's parent count rather than a plain
    containment count: a contour is never its own parent
    (`pcbnew/convert_shape_list_to_polygon.cpp:387-388`, KiCad 10.0.3).
    """
    return sum(
        1 for index, loop in enumerate(loops)
        if index != ignore and point_inside_loop(loop, point)
    )


def is_material(loops: list, point: tuple) -> bool:
    """True when *point* sits in board material rather than in air.

    Parity. Inside no loop at all is off the board; inside
    one is material; inside two is a cutout in that material; inside three is an
    island standing in that cutout. So an odd count is material, and an even
    count - nought included - is air.

    Undefined ON a boundary, which is what :func:`is_material_under_cutter`
    exists to keep a caller clear of.
    """
    return loops_containing(loops, point) % 2 == 1


def cutter_centre_at_edge(edge_point: tuple, inward_normal: tuple,
                          cutter_diameter_mm: float) -> tuple:
    """Where the middle of the bit sits with its cutting edge on *edge_point*.

    Half a cutter width in from the edge, along the inward
    normal. `inward_normal` must be a unit vector pointing out of the air and
    into whatever the loop encloses.
    """
    if cutter_diameter_mm <= 0.0:
        raise ValueError(
            f"a cutter diameter of {cutter_diameter_mm} mm cuts nothing, so "
            f"there is no point half a cutter width in from an edge"
        )
    radius = cutter_diameter_mm / 2.0
    return (
        edge_point[0] + inward_normal[0] * radius,
        edge_point[1] + inward_normal[1] * radius,
    )


def is_material_under_cutter(loops: list, edge_point: tuple,
                             inward_normal: tuple,
                             cutter_diameter_mm: float) -> bool:
    """Is there material where the tool would sit, one radius in from the edge?

    The router's own question, and both rules of this module in one call: the
    probe is the cutter's centre and the answer is a parity count.
    """
    return is_material(
        loops,
        cutter_centre_at_edge(edge_point, inward_normal, cutter_diameter_mm),
    )


def _inward_normal_at_middle(loop: list, index: int) -> tuple:
    """The unit normal at the middle of `loop[index]`, pointing into the loop.

    "Into the loop" is the region the loop encloses, whether that region is
    material or the air of a cutout - which is the whole point: the parity count
    is what decides which of the two it is.
    """
    primitive = loop[index]
    if isinstance(primitive, Line):
        tangent = primitive.tangent_at_start()
    elif isinstance(primitive, Arc):
        sign = 1.0 if primitive.ccw else -1.0
        tangent = primitive.tangent_at_angle(
            primitive.a0 + sign * primitive.sweep / 2.0
        )
    else:
        raise TypeError(f"{type(primitive).__name__} is not a line or an arc")
    orientation = 1 if signed_area(loop) > 0.0 else -1
    outward = outward_normal(tangent, orientation)
    return (-outward[0], -outward[1])


def edge_probes(loop: list, cutter_diameter_mm: float) -> tuple:
    """Two points straddling one edge of *loop*: `(inside, outside, edge_point)`.

    Both are the SAME distance off the SAME point of the edge, on opposite
    sides, which is what makes them a pair worth comparing: any difference
    between them is a fact about the loop and not about where they were taken.

    The edge point is the middle of the loop's LONGEST primitive. That is a
    choice rather than a measurement, so it is made the one way that gives the
    same answer on every run and every machine: longest first, earliest in the
    chain to break a tie.

    The step is half a cutter width, and a feature
    smaller than the cutter would throw that probe out the far side - 1.3 mm
    holes in a thin flexible board are exactly that case, and a 2 mm cutter clears
    them entirely. So the step is halved until the inside probe really is
    inside. It costs nothing on a feature the cutter fits, and it keeps the
    answer true on one it does not.

    Raises:
        ValueError: when no step above the contact tolerance lands inside, which
            means the loop encloses less than a micrometre of anything.
    """
    if not loop:
        raise ValueError("an empty loop encloses nothing to probe")
    if cutter_diameter_mm <= 0.0:
        raise ValueError(
            f"a cutter diameter of {cutter_diameter_mm} mm gives no probe step"
        )
    longest = min(range(len(loop)), key=lambda index: (-loop[index].length, index))
    edge_point = midpoint_of(loop[longest])
    inward = _inward_normal_at_middle(loop, longest)
    step = cutter_diameter_mm / 2.0
    while step > CONTACT_TOL_MM:
        probe = (edge_point[0] + inward[0] * step,
                 edge_point[1] + inward[1] * step)
        if point_inside_loop(loop, probe):
            return (
                probe,
                (edge_point[0] - inward[0] * step,
                 edge_point[1] - inward[1] * step),
                edge_point,
            )
        step /= 2.0
    raise ValueError(
        f"no step down from half a cutter width lands inside the loop at "
        f"({edge_point[0]:.4f}, {edge_point[1]:.4f}) mm, so that loop encloses "
        f"less than {CONTACT_TOL_MM} mm of anything"
    )


def probe_inside_loop(loop: list, cutter_diameter_mm: float) -> tuple:
    """A point just inside *loop*, half a cutter width in from its own edge.

    The inside half of :func:`edge_probes`.
    """
    return edge_probes(loop, cutter_diameter_mm)[0]


def loop_is_hole(loops: list, index: int, cutter_diameter_mm: float) -> bool:
    """Is `loops[index]` a hole in the board, or a top-level outline of its own?

    KiCad's rule exactly - an even number of parents is a top-level outline, an
    odd number is a hole (`pcbnew/convert_shape_list_to_polygon.cpp:411` and
    `:446`, KiCad 10.0.3) - with a different probe in place of KiCad's own.
    KiCad tests the contour's first POINT, which sits on the boundary where an
    inside test is at its most fragile; this tests a point half a cutter width
    inside the loop, which answers the same question a measured distance clear
    of it.
    """
    probe = probe_inside_loop(loops[index], cutter_diameter_mm)
    return loops_containing(loops, probe, ignore=index) % 2 == 1
