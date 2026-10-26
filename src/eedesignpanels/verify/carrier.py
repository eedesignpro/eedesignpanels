"""Which part of a panel is a BOARD and which part is CARRIER - pure geometry.

The comparison holds the built panel against the source board stamped at every
placement. That reference contains the boards and nothing else, so every piece
of carrier the panel legitimately carries - copper poured over the rails, the
mask openings round the tooling holes, the panel's own outline - is artwork the
reference can never have. Judged literally, a panel with a carrier can only
fail, and the failure says nothing.

So judgment is **scoped by region** rather than by layer. Blinding a whole
layer would give up exactly what the check is for: instance copper and instance
mask are where it earns its keep.

Three regions, all derived from the build's own placement data - the same
offsets the reference is composed from - and never from a stage name or a
config key:

* the **board** of an instance: the source board's outline extent, moved to
  that placement. Everything here is judged, exactly as before.
* its **margin**: the fabrication margin already established round the
  comparison window (1 mm by default), which is where a board's own toolpath,
  silkscreen and courtyards legitimately spill past its outline.
* the **carrier**: everything else on the panel.

The rule those regions drive, per layer:

1. **Ink the panel has that the reference does not, outside every instance
   window, is carrier artwork** - measured, named in the reader's own terms
   (:func:`describe`), and never judged.
2. A layer carrying no such ink is measured exactly as it always was. The
   scoping is then arithmetically a no-op, which is what makes a panel without
   a carrier report identically to before.
3. On a layer that does carry carrier artwork, judgment keeps: every piece of
   ink the reference has and the panel does not, wherever it is, and every
   difference at all inside a board's own outline extent. What is given up is
   *extra* ink in the margin ring - because a rail pour stands off a scored
   board edge by its copper clearance, which measured 0.635 mm on the corpus
   panel and is therefore inside the 1 mm margin. Extra ink confined to that
   ring, on a layer the carrier already writes to, is not evidence of a fault.
4. **Ink the reference has that the panel lacks, outside every instance
   window, is impossible**: the reference is boards only. If it is ever
   measured it is reported loudly and judged all the same, because it would
   mean a board's artwork landed outside the window that stands for it.

One limit, stated rather than hidden: a board is scoped by its outline
*extent*, a rectangle. A strongly concave outline - an L, a C - has carrier
inside its own bounding box, and that carrier is judged rather than named.
That reports a difference that is not a fault; it is loud, visible and
conservative, which is the right direction to be wrong in.

Pure values only: this module imports nothing but the region types, so the
rule can be read and tested without a rasterizer.
"""

from typing import NamedTuple

from eedesignpanels.verify.regions import Bounds

NM_PER_MM = 1_000_000

# Below this, extra ink outside the instance windows is the drawing's own
# rounding rather than artwork: a tenth of a millimetre square, four hundred
# pixels at the default 5 um. It keeps a stray sub-pixel sliver from turning a
# whole layer into a carrier-bearing one and quietly changing how it is
# measured.
MIN_CARRIER_AREA_MM2 = 0.01

# A carrier feature no larger than this in both directions, and roughly as wide
# as it is tall, reads as a hole rather than as a pour. An eighth-inch tooling
# hole is 3.175 mm and a fiducial's mask opening a few millimetres, so this
# leaves room above both without reaching pour size.
HOLE_MAX_MM = 8.0
# How square "roughly square" is. A circle's bounding box is exactly square;
# this allows the coarse feature boxes their own slack.
HOLE_ASPECT = 1.6
# More separate features than this is not a set of holes.
HOLE_MAX_COUNT = 12


def is_artwork(ink_pixels: int, pixel_area_mm2: float) -> bool:
    """Is that much ink outside the boards artwork, or the drawing's rounding?

    The one policy question the scoping asks of a measurement, kept here with
    the constant it uses so a caller cannot answer it a second way.
    """
    return ink_pixels * pixel_area_mm2 > MIN_CARRIER_AREA_MM2


def _nm(value_mm: float) -> int:
    """Millimetres to whole nanometres - the resolution KiCad's gerbers use."""
    return round(value_mm * NM_PER_MM)


def _grow(box: Bounds, margin_mm: float) -> Bounds:
    """A box grown by a margin on every side, in whole nanometres."""
    margin = _nm(margin_mm)
    return Bounds(
        min_x_mm=(_nm(box.min_x_mm) - margin) / NM_PER_MM,
        min_y_mm=(_nm(box.min_y_mm) - margin) / NM_PER_MM,
        max_x_mm=(_nm(box.max_x_mm) + margin) / NM_PER_MM,
        max_y_mm=(_nm(box.max_y_mm) + margin) / NM_PER_MM,
    )


class InstanceWindow(NamedTuple):
    """One placed board: the material it is, and the margin round it.

    ``board`` is the source outline extent moved to this placement - the part
    of the panel that IS this instance. ``window`` is that plus the fabrication
    margin, the region a board's own artwork may legitimately reach into.
    """

    column: int
    row: int
    board: Bounds
    window: Bounds


def instance_windows(outline: Bounds, placements, margin_mm: float) -> tuple:
    """One window per placement, from the source outline and the build's grid.

    ``placements`` are :class:`eedesignpanels.placement.Placement` values, the
    same ones the reference is composed from, so the windows cannot drift from
    where the reference actually put the boards. Gerber Y is the board's Y
    negated (see :mod:`eedesignpanels.verify.reference`), which is the one
    conversion done here.

    Raises:
        ValueError: for a negative margin, which would shrink a window and drop
            instance artwork out of judgment without saying so.
    """
    if margin_mm < 0:
        raise ValueError(f"margin must not be negative, got {margin_mm}")
    windows = []
    for placement in placements:
        dx, dy = placement.dx_mm, -placement.dy_mm
        board = Bounds(
            min_x_mm=(_nm(outline.min_x_mm) + _nm(dx)) / NM_PER_MM,
            min_y_mm=(_nm(outline.min_y_mm) + _nm(dy)) / NM_PER_MM,
            max_x_mm=(_nm(outline.max_x_mm) + _nm(dx)) / NM_PER_MM,
            max_y_mm=(_nm(outline.max_y_mm) + _nm(dy)) / NM_PER_MM,
        )
        windows.append(InstanceWindow(
            column=placement.column,
            row=placement.row,
            board=board,
            window=_grow(board, margin_mm),
        ))
    return tuple(windows)


def boards_of(windows) -> tuple:
    """Just the board extents - what is judged in full."""
    return tuple(window.board for window in windows)


def windows_of(windows) -> tuple:
    """Just the windows - board plus fabrication margin."""
    return tuple(window.window for window in windows)


def block_of(windows) -> Bounds:
    """The rectangle every instance window fits inside: the boards' own block.

    What lies outside it is the rail; what lies inside it but outside a window
    is the space between neighbouring boards. That is the whole of the position
    evidence :func:`describe` needs, and it comes from the placements rather
    than from any stage.

    Raises:
        ValueError: when handed no windows, which would mean a panel with no
            instances - a caller error, not an empty answer.
    """
    windows = list(windows)
    if not windows:
        raise ValueError("a panel with no instances has no board block")
    return Bounds(
        min_x_mm=min(w.window.min_x_mm for w in windows),
        min_y_mm=min(w.window.min_y_mm for w in windows),
        max_x_mm=max(w.window.max_x_mm for w in windows),
        max_y_mm=max(w.window.max_y_mm for w in windows),
    )


class CarrierInk(NamedTuple):
    """What one layer's carrier holds, measured and named. Never a verdict.

    ``ink_pixels`` is the extra ink the panel carries outside every instance
    window - the rail pour, the mask openings, the panel's own outline.
    ``differing_pixels`` is everything this layer's difference contains that
    was set aside from judgment, which is that ink plus whatever the carrier
    reaches into the fabrication margin with.

    ``only_expected_pixels`` is the case that must be impossible: ink the
    reference has, outside every window, that the panel does not. It is judged
    all the same and reported loudly - see :attr:`leaked`.
    """

    ink_pixels: int
    differing_pixels: int
    only_expected_pixels: int
    reason: str
    features: tuple = ()
    pixel_area_mm2: float = 0.0

    @property
    def ink_area_mm2(self) -> float:
        return self.ink_pixels * self.pixel_area_mm2

    @property
    def area_differing_mm2(self) -> float:
        return self.differing_pixels * self.pixel_area_mm2

    @property
    def leaked(self) -> bool:
        """Did instance artwork go missing outside the window that holds it?"""
        return self.only_expected_pixels > 0

    @property
    def area_only_expected_mm2(self) -> float:
        return self.only_expected_pixels * self.pixel_area_mm2

    def to_dict(self) -> dict:
        return {
            "ink_pixels": self.ink_pixels,
            "ink_area_mm2": self.ink_area_mm2,
            "differing_pixels": self.differing_pixels,
            "area_differing_mm2": self.area_differing_mm2,
            "only_expected_pixels": self.only_expected_pixels,
            "area_only_expected_mm2": self.area_only_expected_mm2,
            "leaked": self.leaked,
            "reason": self.reason,
            "features": len(self.features),
        }


def leak_warning(layer: str, carrier: CarrierInk) -> str:
    """The loud line for the case that cannot happen. Reported, never hidden."""
    return (
        f"  IMPOSSIBLE: {layer} - the board's own artwork is missing from "
        f"{carrier.area_only_expected_mm2:.4f} mm2 OUTSIDE every instance "
        f"window ({carrier.only_expected_pixels} px). The reference holds "
        f"boards and nothing else, so ink of its own out there means an "
        f"instance's artwork landed outside the window that stands for it. "
        f"It is judged, not excused - but the window rule is what needs "
        f"looking at."
    )


def layer_kind(layer_key: str) -> str:
    """What a layer IS, in a designer's words, from its own name.

    The key is the one kicad-cli writes: the layer's display name with dots as
    underscores. Read here rather than mapped from a stage, so a panel names
    its carrier from what is actually on it.
    """
    name = layer_key.lower()
    if name.startswith("edge_cuts"):
        return "outline"
    if "score" in name:
        return "score lines"
    if name.endswith("_cu") or "_cu-" in name:
        return "copper"
    if "mask" in name:
        return "solder mask"
    if "paste" in name:
        return "solder paste"
    if "silkscreen" in name or "silks" in name:
        return "silkscreen"
    if "courtyard" in name or "crtyd" in name:
        return "courtyards"
    if "fab" in name:
        return "fabrication notes"
    if "adhesive" in name or "adhes" in name:
        return "adhesive"
    return "artwork"


def _hole_like(features) -> bool:
    """Do these features read as a handful of holes rather than as a pour?"""
    features = list(features)
    if not features or len(features) > HOLE_MAX_COUNT:
        return False
    for box in features:
        width, height = box.width_mm, box.height_mm
        if width > HOLE_MAX_MM or height > HOLE_MAX_MM:
            return False
        if width <= 0 or height <= 0:
            return False
        if max(width / height, height / width) > HOLE_ASPECT:
            return False
    return True


def _reaches_the_rails(features, block: Bounds) -> bool:
    """Does any of it lie outside the block the boards occupy?"""
    return any(
        box.min_x_mm < block.min_x_mm or box.max_x_mm > block.max_x_mm
        or box.min_y_mm < block.min_y_mm or box.max_y_mm > block.max_y_mm
        for box in features
    )


def describe(layer_key: str, features, block: Bounds) -> str:
    """Name a layer's carrier artwork in the reader's own terms.

    Derived from two pieces of evidence and nothing else: **what layer** the
    ink is on, and **where it sits** relative to the block of boards - plus,
    for a set of small square features, that they read as holes. No stage name
    is consulted, so a carrier feature nobody has written yet still gets an
    honest sentence rather than a wrong one.

    ``features`` are coarse boxes, good enough to count and to size and not
    presented as measurements.
    """
    kind = layer_kind(layer_key)
    where = "on the rails" if _reaches_the_rails(features, block) \
        else "between the boards"
    if kind == "outline":
        return "carrier artwork: the panel's own outline"
    if kind == "score lines":
        return "carrier artwork: score lines run across the whole panel"
    holes = _hole_like(features)
    if kind == "copper":
        if holes:
            return f"carrier artwork: {len(features)} copper pads {where}"
        return (
            "carrier artwork: rail copper" if where == "on the rails"
            else "carrier artwork: copper poured between the boards"
        )
    if kind in ("solder mask", "solder paste"):
        if holes:
            return (
                f"carrier artwork: {kind} openings at {len(features)} tooling "
                f"holes {where}"
            )
        return f"carrier artwork: {kind} {where}"
    return f"carrier artwork: {kind} {where}"
