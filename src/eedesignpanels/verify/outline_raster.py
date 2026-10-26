"""The raster oracle: the derived outline against the toolpath, in pixels.

THIS IS THE INVARIANT CHECKER. Every routed section
of `Edge.Cuts` is the outline of the toolpath layer's own ink, with only the
blank's edges exempt - and :func:`invariant` asks exactly that of a board FILE:
it reads `User.36` back out of the file, strokes it at each item's own width, and
subtracts the filled `Edge.Cuts` loops from it. Nothing that built the panel is
consulted, so a board that satisfies it satisfies it standalone.

The analytic checker in :mod:`eedesignpanels.outline_check` reasons about the
outline. This one does not reason about anything - it DRAWS both answers and
subtracts them, so a mistake shared by two pieces of reasoning still has
nowhere to hide.

Three pictures, none of them produced by the code that composed the outline:

* **What the cutter removed.** Every channel the boolean was given, drawn as an
  SVG path stroked at the cutter's width with round caps and round joins -
  which is the Minkowski sum of the path and the bit, and therefore the
  definition. resvg does the stroking.
* **What the outline says is left.** The finished `Edge.Cuts` loops, written out
  as SVG paths - true arcs, no flattening - and FILLED even-odd. The loops are
  the artifact under test; filling them is a generic rasterizer's job, not ours.
* **Where the boards are.** Each instance's own outline, filled, at its own
  placement. THE ONE THING A PANEL MUST NEVER DO IS EAT A BOARD: the cutter runs
  outside an instance by its own radius, so nothing has any business inside one.
  This is what sees a cut that is perfectly well formed and should never have
  been made - the corner a pairwise claim once chamfered off a board is a legal
  cut and an invalid panel, and only the boards' own geometry says so.

The blank minus the second is the region the outline claims was cut; the first
is the region that was cut. Subtract, and every pixel that survives is a
disagreement with a millimetre coordinate attached.

Registration is proved rather than assumed. KiCad plots the `User.36` toolpath
to a gerber at each item's own stroke width, and that file - KiCad's own
plotter, untouched by this project - is compared against the same subset drawn
by resvg. If the two renderers and the two coordinate systems did not agree to a
pixel, every number below would be measuring that instead.

Dev-side only, like the rest of `eedesignpanels.verify`: it needs kicad-cli, a
patched gerbonara and resvg.
"""

import math
import tempfile
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageFilter

from eedesignpanels.review.render import DEFAULT_KICAD_CLI, LayerSpec
from eedesignpanels.toolpath import Arc, Line
from eedesignpanels.verify import gerbers, raster
from eedesignpanels.verify.regions import Bounds, Canvas, cluster_pixels, set_bit_pixels

TOOLPATH_LAYER = "User.36"

# The oracle's own resolution. Ten micrometres a pixel is a fifth of the
# thinnest thing the file can express that anybody cares about and a tenth of
# the smallest wrong wall this project has drawn; the panel comes out around
# thirty megapixels, which renders in a second or two.
DEFAULT_UM_PER_PIXEL = 10.0

# How far outside the blank the canvas reaches. The panel's outer edge sits ON
# the blank, and an edge exactly on the last pixel row disagrees with itself by
# a pixel; a millimetre of margin puts it well inside the picture.
MARGIN_MM = 1.0

# A difference band this thin is the two renderers disagreeing about which side
# of a pixel an edge falls on. Three pixels, stated in millimetres by the
# canvas, and every real defect measured here has been thirty times it.
TOLERANCE_PX = 3.0

# How near a declared minimum-tab join a difference has to be before that join
# explains it. The join's own cut is a stadium of its radius; a tenth of a
# millimetre past the end of that is generous and still local.
JOIN_REACH_MM = 0.1

# A 1-bit mask holds 0 and 255. After a grey-scale erosion anything not black
# is still ink, so the threshold is "not zero" rather than a luminance guess.
_KEEP_INK = [0] + [255] * 255


class Difference(NamedTuple):
    """One region where the outline and the toolpath disagree."""

    region: object
    explained_by_join: bool
    at: tuple
    width_mm: float

    @property
    def summary(self) -> str:
        where = "at a declared minimum-tab join" if self.explained_by_join \
            else "away from any declared join"
        return (f"{self.width_mm:.4f} mm wide, ({self.at[0]:.4f}, "
                f"{self.at[1]:.4f}) mm, {where}")


class RasterReport(NamedTuple):
    """What the two pictures said about each other."""

    canvas: Canvas
    differences: tuple
    forgiven: tuple
    registration_px: int
    cut_px: int
    derived_px: int
    xor_px: int
    tolerance_mm: float
    min_tab_mm: float
    images: dict
    # Where a BOARD lost part of its own outline to the panel. Empty when the
    # instances were not handed in, which is what a turned arrangement does.
    eaten: tuple = ()

    @property
    def ok(self) -> bool:
        return (not self.differences and not self.eaten
                and self.registration_ok)

    @property
    def registration_ok(self) -> bool:
        return self.registration_px <= self.registration_budget_px

    @property
    def registration_budget_px(self) -> int:
        """How many pixels the two renderings of one toolpath may differ by.

        An edge is allowed to land on either side of a pixel, so the budget is
        proportional to how much EDGE there is - taken as a fixed small share of
        the inked area rather than as an absolute count, which would mean one
        thing on a small panel and another on a large one.
        """
        return max(1000, self.cut_px // 200)

    @property
    def summary(self) -> str:
        if not self.registration_ok:
            return (f"raster oracle CANNOT JUDGE: the two renderings of the "
                    f"same toolpath differ by {self.registration_px} px, over "
                    f"the {self.registration_budget_px} px budget")
        if self.eaten:
            first = self.eaten[0]
            return (f"raster oracle FAILED: the panel has taken a bite out of "
                    f"{len(self.eaten)} place(s) INSIDE a board's own outline, "
                    f"the first {first.width_mm:.4f} mm wide at "
                    f"({first.at[0]:.4f}, {first.at[1]:.4f}) mm")
        if not self.differences:
            return (f"raster oracle clean: {self.xor_px} px differ along the "
                    f"edges, none of them wider than {self.tolerance_mm:.4f} mm")
        return (f"raster oracle FAILED: {len(self.differences)} region(s) where "
                f"the outline and the cut disagree by more than "
                f"{self.tolerance_mm:.4f} mm")


# ---------------------------------------------------------------------------
# writing geometry as SVG - the one place a curve becomes a string
# ---------------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{value:.6f}"


def arc_commands(arc: Arc) -> str:
    """One arc as SVG `A` commands, exact, split so no piece exceeds a half turn.

    SVG cannot draw a turn of a full circle in one command and is ambiguous at
    exactly half, so anything over a half turn is drawn in two. The sweep flag
    is 1 when the arc runs in the direction of increasing angle, which is what
    `ccw` means in the stored numbers.
    """
    pieces = 1 if arc.sweep <= math.pi else 2
    step = arc.sweep / pieces
    commands = []
    for index in range(pieces):
        angle = arc.a0 + (step * (index + 1)) * (1.0 if arc.ccw else -1.0)
        point = arc.point_at_angle(angle)
        commands.append(
            f"A {_number(arc.radius)} {_number(arc.radius)} 0 0 "
            f"{1 if arc.ccw else 0} {_number(point[0])} {_number(point[1])}"
        )
    return " ".join(commands)


def path_of(chain, close: bool = True) -> str:
    """A chain of lines and arcs as one SVG path string, in board millimetres."""
    if not chain:
        return ""
    start = chain[0].start
    parts = [f"M {_number(start[0])} {_number(start[1])}"]
    for piece in chain:
        if isinstance(piece, Line):
            parts.append(f"L {_number(piece.p1[0])} {_number(piece.p1[1])}")
        else:
            parts.append(arc_commands(piece))
    if close:
        parts.append("Z")
    return " ".join(parts)


def document(bounds: Bounds, body: str) -> str:
    """An SVG page over *bounds* in board millimetres, y downward.

    The page is sized in millimetres and its viewBox is the same rectangle, so
    the rasterizer's DPI is the only scale and a coordinate in the body is a
    board coordinate.
    """
    width, height = bounds.width_mm, bounds.height_mm
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_number(width)}mm" height="{_number(height)}mm" '
        f'viewBox="{_number(bounds.min_x_mm)} {_number(bounds.min_y_mm)} '
        f'{_number(width)} {_number(height)}">'
        f'<rect x="{_number(bounds.min_x_mm)}" y="{_number(bounds.min_y_mm)}" '
        f'width="{_number(width)}" height="{_number(height)}" fill="white"/>'
        f"{body}</svg>"
    )


def filled_svg(bounds: Bounds, loops) -> str:
    """The panel the loops describe, filled black, even-odd so holes are holes."""
    data = " ".join(path_of(list(loop)) for loop in loops)
    body = (f'<path d="{data}" fill="black" fill-rule="evenodd" '
            f'stroke="none"/>') if data else ""
    return document(bounds, body)


def stroked_svg(bounds: Bounds, sweeps) -> str:
    """The toolpath drawn at cutter width - the ribbon the cutter removed.

    Round caps and round joins, because that is what a round cutter leaves; the
    stroke of a path with both is the Minkowski sum of the path and the cutter,
    which is the definition the whole panel is derived from.
    """
    parts = []
    for item, radius in sweeps:
        parts.append(
            f'<path d="{path_of([item], close=False)}" fill="none" '
            f'stroke="black" stroke-width="{_number(radius * 2.0)}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    return document(bounds, "".join(parts))


def instances_svg(bounds: Bounds, loops, placements) -> str:
    """Every board's OWN outline, filled, stepped to where its copy sits.

    The one thing a panel must never do is eat a board. The cutter runs OUTSIDE
    an instance's outline by its own radius, so no channel and no minimum-tab
    crossing has any business inside one - and an outline that has taken a bite
    out of a board says so here, whatever the boolean thinks of itself.
    """
    data = " ".join(path_of(list(loop)) for loop in loops)
    if not data:
        return document(bounds, "")
    groups = "".join(
        f'<g transform="translate({_number(dx)},{_number(dy)})">'
        f'<path d="{data}" fill="black" fill-rule="evenodd" stroke="none"/>'
        f"</g>"
        for dx, dy in placements
    )
    return document(bounds, groups)


def blank_svg(bounds: Bounds, blank) -> str:
    """The blank rectangle, filled - what the panel was cut out of."""
    body = (
        f'<rect x="{_number(blank.min_x)}" y="{_number(blank.min_y)}" '
        f'width="{_number(blank.max_x - blank.min_x)}" '
        f'height="{_number(blank.max_y - blank.min_y)}" fill="black"/>'
    )
    return document(bounds, body)


# ---------------------------------------------------------------------------
# pixels
# ---------------------------------------------------------------------------


def canvas_over(blank, um_per_pixel: float = DEFAULT_UM_PER_PIXEL,
                margin_mm: float = MARGIN_MM) -> Canvas:
    """The shared grid: the blank with a margin, in BOARD coordinates."""
    return Canvas.over(
        Bounds(
            min_x_mm=blank.min_x - margin_mm,
            min_y_mm=blank.min_y - margin_mm,
            max_x_mm=blank.max_x + margin_mm,
            max_y_mm=blank.max_y + margin_mm,
        ),
        um_per_pixel,
    )


def gerber_bounds(canvas: Canvas) -> tuple:
    """The canvas as gerber coordinates, where Y runs UP and the board's runs down.

    A gerber holds the board mirrored in Y, and gerbonara renders row 0 at the
    largest gerber Y - which is the SMALLEST board Y. So a canvas whose top row
    is the board's minimum Y is rendered by handing gerbonara the negated
    rectangle, and the two pictures land on the same pixels.
    """
    return (
        (canvas.bounds.min_x_mm, -canvas.bounds.max_y_mm),
        (canvas.bounds.max_x_mm, -canvas.bounds.min_y_mm),
    )


def toolpath_layer(board_path: Path) -> LayerSpec:
    """The toolpath layer as the board's own table names it.

    Read from the file rather than typed, because kicad-cli names the exported
    gerber after the layer's DISPLAY name and this project's boards call
    `User.36` something of their own.
    """
    from eedesigncore import Board

    from eedesignpanels.review.render import layers_of

    for layer in layers_of(Board.from_file(Path(board_path))):
        if layer.canonical_name == TOOLPATH_LAYER:
            return layer
    raise ValueError(
        f"{board_path} has no {TOOLPATH_LAYER} in its layer table, so there is "
        f"no toolpath to check the outline against"
    )


# An arc whose three stored points are this close to collinear is the line
# between its ends. The same figure the panel stage uses, and read the same way
# here rather than imported: a guard, a stroke
# width and a stored coordinate are INPUT the file wrote down, and reading them
# is not trusting the code under test - what is not shared is the judgement.
COLLINEAR_ARC_TOL = 1e-12


def ink_of(board_path: Path) -> list:
    """What the FILE says the router removes: `(primitive, radius)` per item.

    Read straight off `User.36` - each item's own geometry at half its own
    stroke width - with nothing from the stage that wrote it. That is what makes
    :func:`invariant` a statement about a board rather than about a build: the
    only inputs are the file and the loops being judged.
    """
    from eedesigncore import Board, items_on_layers

    from eedesignpanels.toolpath import arc_from_three_points

    board = Board.from_file(Path(board_path))
    found = []
    for item in items_on_layers(
        (*board.drawings, *board.trailing_drawings), {TOOLPATH_LAYER}
    ):
        width = item.stroke.width if item.stroke is not None else 0.0
        kind = type(item).__name__
        source = item.id or ""
        if kind == "PcbLine":
            found.append(
                (Line(p0=item.start, p1=item.end, source=source), width / 2.0)
            )
            continue
        if kind != "PcbArc":
            raise ValueError(
                f"{board_path} holds a {kind} on {TOOLPATH_LAYER}, and a "
                f"toolpath is lines and arcs - there is no stroke this oracle "
                f"could draw for it"
            )
        (ax, ay), (bx, by), (cx, cy) = item.start, item.mid, item.end
        twice_area = 2.0 * (
            ax * (by - cy) + bx * (cy - ay) + cx * (ay - by)
        )
        if abs(twice_area) < COLLINEAR_ARC_TOL:
            found.append(
                (Line(p0=item.start, p1=item.end, source=source), width / 2.0)
            )
            continue
        found.append((
            arc_from_three_points(item.start, item.mid, item.end, source=source),
            width / 2.0,
        ))
    return found


def invariant(board_path: Path, blank, loops, instance_loops=(), placements=(),
              min_tab_mm: float = 0.5,
              um_per_pixel: float = DEFAULT_UM_PER_PIXEL,
              out_dir: Path = None,
              cli_path: Path = DEFAULT_KICAD_CLI,
              keep_images: bool = False) -> RasterReport:
    """THE INVARIANT, asked of one board file: is the outline the toolpath's outline?

    The same subtraction :func:`check` makes, with the cut region taken from the
    file's own `User.36` (:func:`ink_of`) rather than handed in. A panel whose
    covering passes never reached the layer fails this even though every number
    inside the build agreed with itself, which is precisely the defect it exists
    to catch.
    """
    return check(
        board_path, blank, loops, ink_of(board_path),
        min_tab_mm=min_tab_mm, instance_loops=instance_loops,
        placements=placements, um_per_pixel=um_per_pixel, out_dir=out_dir,
        cli_path=cli_path, keep_images=keep_images,
    )


def cut_from_kicad(board_path: Path, canvas: Canvas, out_dir: Path,
                   cli_path: Path = DEFAULT_KICAD_CLI) -> Image.Image:
    """KiCad's own plot of the toolpath layer, rasterized onto the canvas."""
    export = gerbers.export_gerbers(
        board_path, [toolpath_layer(board_path)], out_dir, cli_path
    )
    path = export.gerber_by_layer.get(TOOLPATH_LAYER)
    if path is None:
        raise RuntimeError(
            f"kicad-cli plotted no {TOOLPATH_LAYER} gerber for {board_path}; "
            f"it wrote {[item.name for item in export.plotted]}"
        )
    gerber = raster.open_gerber(path)
    svg = str(gerber.to_svg(
        force_bounds=gerber_bounds(canvas),
        arg_unit=raster.MM, svg_unit=raster.MM, fg="black", bg="white",
    ))
    return raster.rasterize_svg(svg, canvas)


def erode(image: Image.Image, radius_px: int) -> Image.Image:
    """Shrink every inked blob by *radius_px*, by repeated three-by-three minima.

    A blob that survives had an inscribed square of that radius in it, which is
    the property this oracle wants: a difference thinner than the tolerance is a
    rendering artifact, and one fatter than it is geometry.
    """
    if radius_px <= 0:
        return image
    working = image.convert("L")
    for _ in range(radius_px):
        working = working.filter(ImageFilter.MinFilter(3))
    return working.point(_KEEP_INK).convert("1", dither=Image.Dither.NONE)


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


def check(board_path: Path, blank, loops, sweeps, board_sweeps=None,
          joins=(), min_tab_mm: float = 0.5,
          instance_loops=(), placements=(),
          um_per_pixel: float = DEFAULT_UM_PER_PIXEL,
          out_dir: Path = None,
          cli_path: Path = DEFAULT_KICAD_CLI,
          keep_images: bool = False) -> RasterReport:
    """Draw both answers and subtract them. *board_path* holds the toolpath.

    *loops* is the derived `Edge.Cuts` and *sweeps* every `(primitive, radius)`
    the boolean was given - the toolpath's own runs, the minimum-tab crossings
    and any pinch cut.

    **All three are on the board now**, so
    *board_sweeps* defaults to the same set and the registration figure becomes
    the whole invariant rather than a witness for part of it: KiCad's own plot of
    `User.36` against the oracle's stroke of what the outline was derived from.
    It stays a separate argument because a caller may hand in a subset
    deliberately - putting a defect back, which is how the fence is shown
    catching one.
    """
    with tempfile.TemporaryDirectory(prefix="panel-oracle-") as temporary:
        work = Path(out_dir) if out_dir is not None else Path(temporary)
        work.mkdir(parents=True, exist_ok=True)
        return _check_in(work, board_path, blank, loops, sweeps,
                         board_sweeps if board_sweeps is not None else sweeps,
                         joins, min_tab_mm, instance_loops, placements,
                         um_per_pixel, cli_path, keep_images)


def _check_in(work, board_path, blank, loops, sweeps, board_sweeps, joins,
              min_tab_mm, instance_loops, placements, um_per_pixel, cli_path,
              keep_images) -> RasterReport:
    canvas = canvas_over(blank, um_per_pixel)
    bounds = canvas.bounds

    cut_kicad = cut_from_kicad(board_path, canvas, work / "gerber", cli_path)
    on_board = raster.rasterize_svg(stroked_svg(bounds, board_sweeps), canvas)
    registration = raster.count_ink(raster.xor(cut_kicad, on_board))

    cut = raster.rasterize_svg(stroked_svg(bounds, sweeps), canvas)
    panel = raster.rasterize_svg(filled_svg(bounds, loops), canvas)
    material = raster.rasterize_svg(blank_svg(bounds, blank), canvas)

    # What the outline says was taken out, and what the cutter took out. Both
    # scoped to the blank: outside it there is nothing to disagree about.
    derived = raster.only_in_first(material, panel)
    truth = raster.intersect(material, cut)
    difference = raster.xor(derived, truth)

    tolerance_px = max(1, int(round(TOLERANCE_PX)))
    surviving = erode(difference, tolerance_px)
    tolerance_mm = tolerance_px * canvas.mm_per_pixel

    differences = _regions(surviving, canvas, tolerance_mm)
    images = {
        "cut": cut, "cut_kicad": cut_kicad, "cut_board": on_board,
        "panel": panel, "derived": derived, "truth": truth,
        "difference": difference,
    }
    eaten: tuple = ()
    if instance_loops and placements:
        instances = raster.rasterize_svg(
            instances_svg(bounds, instance_loops, placements), canvas)
        bitten = raster.only_in_first(instances, panel)
        images["instances"] = instances
        images["bitten"] = bitten
        eaten = tuple(_regions(erode(bitten, tolerance_px), canvas, tolerance_mm))
    return RasterReport(
        canvas=canvas,
        differences=tuple(differences),
        forgiven=(),
        eaten=eaten,
        registration_px=registration,
        cut_px=raster.count_ink(cut_kicad),
        derived_px=raster.count_ink(derived),
        xor_px=raster.count_ink(difference),
        tolerance_mm=tolerance_mm,
        min_tab_mm=min_tab_mm,
        images=images if keep_images else {},
    )


def _regions(surviving, canvas, tolerance_mm) -> list:
    """Every difference that survived erosion, in millimetres."""
    pixels = list(set_bit_pixels(
        surviving.tobytes(), canvas.width_px, canvas.height_px
    ))
    if not pixels:
        return []
    cell = canvas.cell_size_px(max(0.2, 4.0 * canvas.mm_per_pixel))
    found = []
    for box in cluster_pixels(pixels, cell):
        region = canvas.pixel_box_to_mm(box)
        found.append(Difference(
            region=region,
            explained_by_join=False,
            at=((region.min_x_mm + region.max_x_mm) / 2.0,
                (region.min_y_mm + region.max_y_mm) / 2.0),
            # The eroded blob's own size, plus what erosion took off each side.
            width_mm=min(region.width_mm, region.height_mm) + 2.0 * tolerance_mm,
        ))
    return found


__all__ = [
    "DEFAULT_UM_PER_PIXEL",
    "Difference",
    "RasterReport",
    "arc_commands",
    "blank_svg",
    "canvas_over",
    "check",
    "cut_from_kicad",
    "document",
    "erode",
    "filled_svg",
    "ink_of",
    "invariant",
    "path_of",
    "stroked_svg",
]
