"""Make the synthetic test board and its frozen outline. Run by hand, twice.

The board stands in for a real design in the tests. Every number in it is made
up here, and it carries no part number, no net name, no cost, and no link.

What the board holds, and why each piece is there:

* An outer outline of lines and arcs: four rounded corners, three notches of
  different widths, a semicircular bite, and a shallow pocket whose ends are
  shoulders of two tangent arcs. The
  notches are narrower than, wider than, and between the cutter sizes the
  tests use, so the cutter offset meets sealed, open, and borderline recesses.
* Open joints: 50 nm at a line-to-line, a line-to-arc and an arc-to-line
  joint, and 3 nm at one more line-to-arc and arc-to-line pair. A designer's
  outline closes to within a rounding step, not exactly, and the chaining code
  has to weld each kind of joint. The real outline these tests used before had
  gaps from 1 nm to 56 nm, at line-to-arc and arc-to-line joints only.
* One rounded slot cut out inside the board, as a second outline loop.
* Four mounting holes, each a footprint with one unplated hole.
* Three generic parts. R1 and C1 sit on the front. C2 is turned over to the
  back by KiCad's own `FOOTPRINT::Flip`, so the file holds the bytes KiCad
  writes for a back-side footprint.

Step 1 runs in KiCad's own Python, because only KiCad can flip a footprint the
way KiCad does and save the board the way KiCad does::

    "<KiCad bin>/python.exe" tests/data/make_synthetic_board.py board

Step 2 runs in the engine's Python. It reads the saved board with eedesigncore
and writes the outer outline, in loop order, as the frozen chain the cutter
offset tests read::

    "<engine venv>/python.exe" tests/data/make_synthetic_board.py outline
"""

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOARD_PATH = HERE / "synthetic_board.kicad_pcb"
OUTLINE_PATH = HERE / "synthetic_outline.json"

# The board, in millimetres. It sits at (100, 100) on the page, as a real
# board does, so no coordinate is a small round number by accident.
LEFT, TOP, RIGHT, BOTTOM = 100.0, 100.0, 150.0, 140.0
CORNER_RADIUS = 2.0
# How far apart the two ends are at each deliberately open joint.
GAP_MM = 0.00005
SMALL_GAP_MM = 0.000003
EDGE_WIDTH_MM = 0.05


def _arc(center, radius, start_deg, end_deg):
    """An arc as (start, mid, end) points, angles in degrees, y down."""
    def at(angle):
        rad = math.radians(angle)
        return (center[0] + radius * math.cos(rad),
                center[1] + radius * math.sin(rad))
    return ("arc", at(start_deg), at((start_deg + end_deg) / 2.0), at(end_deg))


def _line(start, end):
    return ("line", start, end)


def outer_outline() -> list:
    """The outer outline, clockwise on screen, as line and arc tuples."""
    r = CORNER_RADIUS
    items = []
    # Top edge, left to right.
    x = LEFT + r
    # Notch A: 0.7 mm wide, 1.2 mm deep, entry corners filleted at 0.2 mm,
    # sharp bottom corners.
    fillet, a_left, a_right, a_depth = 0.2, 114.65, 115.35, 1.2
    items.append(_line((x, TOP), (a_left - fillet, TOP)))
    items.append(_arc((a_left - fillet, TOP + fillet), fillet, 270.0, 360.0))
    items.append(_line((a_left, TOP + fillet), (a_left, TOP + a_depth)))
    # Joint 1, line to line: this line starts GAP_MM right of where the last
    # one ended.
    items.append(_line((a_left + GAP_MM, TOP + a_depth),
                       (a_right, TOP + a_depth)))
    items.append(_line((a_right, TOP + a_depth), (a_right, TOP + fillet)))
    items.append(_arc((a_right + fillet, TOP + fillet), fillet, 180.0, 270.0))
    # Notch B: 3.0 mm wide, 1.5 mm deep, sharp entry, bottom corners rounded
    # at 0.5 mm.
    round_r, b_left, b_right, b_depth = 0.5, 133.5, 136.5, 1.5
    items.append(_line((a_right + fillet, TOP), (b_left, TOP)))
    items.append(_line((b_left, TOP), (b_left, TOP + b_depth - round_r)))
    items.append(_arc((b_left + round_r, TOP + b_depth - round_r), round_r,
                      180.0, 90.0))
    items.append(_line((b_left + round_r, TOP + b_depth),
                       (b_right - round_r, TOP + b_depth)))
    items.append(_arc((b_right - round_r, TOP + b_depth - round_r), round_r,
                      90.0, 0.0))
    items.append(_line((b_right, TOP + b_depth - round_r), (b_right, TOP)))
    # A round bite of 0.775 mm radius close to the top right corner, its centre
    # 0.6 mm inside the board, so it takes more than half a circle. The offset
    # of this bite and the offset of the corner arc lie one inside the other.
    bite_r, bite_x, bite_y = 0.775, 147.0, TOP + 0.6
    half_chord = math.sqrt(bite_r ** 2 - (bite_y - TOP) ** 2)
    items.append(_line((b_right, TOP), (bite_x - half_chord, TOP)))
    items.append(("arc", (bite_x - half_chord, TOP), (bite_x, bite_y + bite_r),
                  (bite_x + half_chord, TOP)))
    items.append(_line((bite_x + half_chord, TOP), (RIGHT - r, TOP)))
    # Top right corner.
    items.append(_arc((RIGHT - r, TOP + r), r, 270.0, 360.0))
    # Right edge, top to bottom, with a 0.6 mm semicircular bite at y = 120.
    bite = 0.6
    # Joint 2, line to arc: this line stops GAP_MM short of the bite.
    items.append(_line((RIGHT, TOP + r), (RIGHT, 120.0 - bite - GAP_MM)))
    items.append(("arc", (RIGHT, 120.0 - bite), (RIGHT - bite, 120.0),
                  (RIGHT, 120.0 + bite)))
    # Joint 3, arc to line: this line starts GAP_MM below the bite's end.
    items.append(_line((RIGHT, 120.0 + bite + GAP_MM), (RIGHT, BOTTOM - r)))
    # Bottom right corner.
    items.append(_arc((RIGHT - r, BOTTOM - r), r, 0.0, 90.0))
    # Bottom edge, right to left, with notch C: 1.8 mm wide, 1.0 mm deep,
    # sharp entry corners, and inside corners rounded at 0.25 mm. A sharp
    # inside corner is where GEOS itself is about 1.5 um out (see
    # test_toolpath_oracle), and that would leave the agreement check no margin.
    c_right, c_left, c_depth, c_round = 125.9, 124.1, 1.0, 0.25
    items.append(_line((RIGHT - r, BOTTOM), (c_right, BOTTOM)))
    items.append(_line((c_right, BOTTOM),
                       (c_right, BOTTOM - c_depth + c_round)))
    items.append(_arc((c_right - c_round, BOTTOM - c_depth + c_round), c_round,
                      0.0, -90.0))
    items.append(_line((c_right - c_round, BOTTOM - c_depth),
                       (c_left + c_round, BOTTOM - c_depth)))
    items.append(_arc((c_left + c_round, BOTTOM - c_depth + c_round), c_round,
                      270.0, 180.0))
    items.append(_line((c_left, BOTTOM - c_depth + c_round), (c_left, BOTTOM)))
    items.append(_line((c_left, BOTTOM), (LEFT + r, BOTTOM)))
    # Bottom left corner.
    items.append(_arc((LEFT + r, BOTTOM - r), r, 90.0, 180.0))
    # Left edge, bottom to top, with a shallow pocket 0.6 mm deep. Each end of
    # the pocket is a shoulder of two TANGENT 0.3 mm arcs, one concave and one
    # convex, so two of the joints are arc to arc. Those two close exactly.
    s, depth = 0.3, 0.6
    items.append(_line((LEFT, BOTTOM - r), (LEFT, 122.0)))
    items.append(_arc((LEFT + s, 122.0), s, 180.0, 270.0))
    items.append(_arc((LEFT + s, 122.0 - 2 * s), s, 90.0, 0.0))
    # Joints 4 and 5, line to arc and arc to line, open by SMALL_GAP_MM: the
    # few-nanometre gaps a real outline also carries.
    items.append(_line((LEFT + depth, 122.0 - 2 * s),
                       (LEFT + depth, 119.6 + SMALL_GAP_MM)))
    items.append(_arc((LEFT + s, 119.6), s, 360.0, 270.0))
    items.append(_arc((LEFT + s, 119.6 - 2 * s), s, 90.0, 180.0))
    items.append(_line((LEFT, 119.6 - 2 * s - SMALL_GAP_MM), (LEFT, TOP + r)))
    # Top left corner, back to the start. It is listed FIRST, so the loop the
    # tests chain starts on an arc, as the real outline's did.
    return [_arc((LEFT + r, TOP + r), r, 180.0, 270.0)] + items


def inner_slot() -> list:
    """A rounded slot 10 mm long and 3 mm wide, cut out of the board."""
    cx, cy, half, r = 125.0, 125.0, 3.5, 1.5
    return [
        _line((cx - half, cy - r), (cx + half, cy - r)),
        ("arc", (cx + half, cy - r), (cx + half + r, cy), (cx + half, cy + r)),
        _line((cx + half, cy + r), (cx - half, cy + r)),
        ("arc", (cx - half, cy + r), (cx - half - r, cy), (cx - half, cy - r)),
    ]


MOUNTING_HOLES = ((104.0, 104.0), (146.0, 104.0), (104.0, 136.0),
                  (146.0, 136.0))
MOUNTING_HOLE_MM = 3.2

# Generic parts: (reference, value, footprint name, x, y, back side).
PARTS = (
    ("R1", "10k", "R_0603", 112.0, 112.0, False),
    ("C1", "100nF", "C_0603", 118.0, 112.0, False),
    ("C2", "100nF", "C_0603", 118.0, 130.0, True),
)


def make_board() -> None:
    """Step 1. Build the board with KiCad's own API and save it."""
    import pcbnew

    def mm(value):
        return pcbnew.FromMM(value)

    def point(xy):
        return pcbnew.VECTOR2I(mm(xy[0]), mm(xy[1]))

    board = pcbnew.BOARD()

    def edge(item):
        if item[0] == "line":
            shape = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
            shape.SetStart(point(item[1]))
            shape.SetEnd(point(item[2]))
        else:
            shape = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_ARC)
            shape.SetArcGeometry(point(item[1]), point(item[2]), point(item[3]))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(mm(EDGE_WIDTH_MM))
        board.Add(shape)

    for item in outer_outline() + inner_slot():
        edge(item)

    for number, at in enumerate(MOUNTING_HOLES, 1):
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetFPID(pcbnew.LIB_ID("Synthetic", "MountingHole_3.2mm"))
        footprint.SetReference(f"H{number}")
        footprint.SetValue("MountingHole")
        footprint.Reference().SetVisible(False)
        pad = pcbnew.PAD(footprint)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(mm(MOUNTING_HOLE_MM), mm(MOUNTING_HOLE_MM)))
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(MOUNTING_HOLE_MM),
                                         mm(MOUNTING_HOLE_MM)))
        pad.SetLayerSet(pad.UnplatedHoleMask())
        footprint.Add(pad)
        board.Add(footprint)
        footprint.SetPosition(point(at))

    for reference, value, name, x, y, back in PARTS:
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetFPID(pcbnew.LIB_ID("Synthetic", name))
        footprint.SetReference(reference)
        footprint.SetValue(value)
        footprint.SetAttributes(pcbnew.FP_SMD)
        footprint.Reference().SetPosition(pcbnew.VECTOR2I(0, mm(-1.4)))
        footprint.Value().SetPosition(pcbnew.VECTOR2I(0, mm(1.4)))
        footprint.Value().SetLayer(pcbnew.F_Fab)
        for number, dx in (("1", -0.8), ("2", 0.8)):
            pad = pcbnew.PAD(footprint)
            pad.SetNumber(number)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
            pad.SetSize(pcbnew.VECTOR2I(mm(0.8), mm(0.95)))
            pad.SetLayerSet(pad.SMDMask())
            pad.SetPosition(pcbnew.VECTOR2I(mm(dx), 0))
            footprint.Add(pad)
        for layer, half_x, half_y in ((pcbnew.F_SilkS, 0.15, 0.6),
                                      (pcbnew.F_Fab, 0.8, 0.4)):
            line = pcbnew.PCB_SHAPE(footprint, pcbnew.SHAPE_T_SEGMENT)
            line.SetStart(pcbnew.VECTOR2I(mm(-half_x), mm(-half_y)))
            line.SetEnd(pcbnew.VECTOR2I(mm(half_x), mm(-half_y)))
            line.SetLayer(layer)
            line.SetWidth(mm(0.12 if layer == pcbnew.F_SilkS else 0.1))
            footprint.Add(line)
        board.Add(footprint)
        footprint.SetPosition(point((x, y)))
        if back:
            footprint.Flip(footprint.GetPosition(),
                           pcbnew.FLIP_DIRECTION_LEFT_RIGHT)

    pcbnew.SaveBoard(str(BOARD_PATH), board)
    # SaveBoard also writes a project file and a local settings file beside
    # the board. The tests need the board alone.
    for suffix in (".kicad_pro", ".kicad_prl"):
        BOARD_PATH.with_suffix(suffix).unlink(missing_ok=True)
    print(f"wrote {BOARD_PATH}")


def write_outline() -> None:
    """Step 2. Read the saved board and freeze its outer outline, in order."""
    from eedesigncore import Board

    board = Board.from_file(BOARD_PATH)
    wanted = outer_outline()
    found = []
    for drawing in board.drawings:
        if getattr(drawing, "layer", None) != "Edge.Cuts":
            continue
        kind = type(drawing).__name__
        if kind == "PcbLine":
            found.append(("line", tuple(drawing.start), tuple(drawing.end)))
        elif kind == "PcbArc":
            found.append(("arc", tuple(drawing.start), tuple(drawing.mid),
                          tuple(drawing.end)))

    def near(a, b):
        return math.dist(a, b) < 1e-5

    items = []
    for index, item in enumerate(wanted):
        # KiCad may save an arc from its other end, so either way round is
        # the same item. The file's own direction is kept: the chaining code
        # under test orients each item itself.
        match = [one for one in found if one[0] == item[0] and (
            (near(one[1], item[1]) and near(one[-1], item[-1]))
            or (near(one[1], item[-1]) and near(one[-1], item[1])))]
        if len(match) != 1:
            raise SystemExit(f"outline item {index} is not in the board once")
        saved = match[0]
        entry = {"kind": saved[0], "source": f"o{index}",
                 "start": list(saved[1]), "end": list(saved[-1])}
        if saved[0] == "arc":
            entry["mid"] = list(saved[2])
        items.append(entry)
    OUTLINE_PATH.write_text(json.dumps({
        "note": ("the synthetic board's outer Edge.Cuts outline, read back "
                 "from synthetic_board.kicad_pcb and listed in loop order, in "
                 "board millimetres. Made by make_synthetic_board.py."),
        "items": items,
    }, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUTLINE_PATH} with {len(items)} items")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "board":
        make_board()
    elif step == "outline":
        write_outline()
    else:
        raise SystemExit("say which step: board or outline")
