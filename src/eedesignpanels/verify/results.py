"""The comparison result types - measurements, not verdicts.

Kept apart from :mod:`eedesignpanels.verify.compare` on purpose: the data model
depends on nothing but arithmetic, so it stays importable - and testable -
without the rasterization backend installed.

**The result is data, not a verdict.** Each layer
reports differing pixels *and* differing area in mm2, the inked area of each
side, the count and size of every difference region, and how the difference
splits between "only in A" and "only in B". ``threshold`` and
``over_threshold`` are a presentation layer sitting on top of those numbers,
not a replacement for them: the threshold is recorded so a report can say what
it was judged against, and the boolean is derived, never stored.

Room to grow was designed in, because the fuller deviation statistic is
deferred until comparisons run against real panels. Both types are frozen,
keyword-only dataclasses with defaults, and both carry a free-form ``metrics``
mapping. A new field appends without breaking a caller, and an experimental
statistic rides in ``metrics`` until it earns a field of its own.
"""

import math
from dataclasses import dataclass, field
from pathlib import Path

from eedesignpanels.verify.regions import Canvas

DEFAULT_THRESHOLD = 0


@dataclass(frozen=True, kw_only=True)
class LayerDiff:
    """Everything measured about one layer. Raw counts and areas, no verdict.

    Pixel counts are the primitive - the rasterizer's own unit - and every area
    is that count times the canvas pixel area, so the two can never disagree.
    """

    layer: str
    present_in_a: bool
    present_in_b: bool
    total_pixels: int
    pixel_area_mm2: float
    ink_pixels_a: int
    ink_pixels_b: int
    pixels_differing: int
    pixels_only_in_a: int
    pixels_only_in_b: int
    threshold: int = DEFAULT_THRESHOLD
    displacement_threshold_mm: float = None
    # Measured and reported, but not allowed to set a verdict. Documentation
    # layers are replicated onto the panel and are not what the bare board is
    # built from, so a difference there is information rather than a fault.
    # The numbers are still here in full - what is withheld is the judgment,
    # never the data.
    judged: bool = True
    # Why, in the reader's own terms, when the answer is not the usual "this is
    # not part of the bare board". A layer the PANEL derives - its outline, its
    # score lines - has no per-instance reference to be judged against at all,
    # and saying so is the difference between a report and a shrug.
    unjudged_reason: str = ""
    # What the panel's CARRIER put on this layer, when it put anything there:
    # a :class:`eedesignpanels.verify.carrier.CarrierInk`, measured and named,
    # and taken out of the difference above rather than added to it. The
    # counts on this dataclass are therefore what was JUDGED - the boards and
    # their fabrication margin - and this is the rest, reported beside them.
    # ``None`` means the layer carries no carrier artwork at all,
    # which is the case for every layer of a panel with no carrier, and in
    # that case every number above is exactly what it was before scoping.
    carrier: object = None
    regions: tuple = ()
    seconds: float = 0.0
    metrics: dict = field(default_factory=dict)

    @property
    def pixel_width_mm(self) -> float:
        """One measuring pixel, as a length. Pixels are square, so exact."""
        return math.sqrt(self.pixel_area_mm2)

    @property
    def hard_regions(self) -> list:
        """Differences wider than a single measuring pixel.

        A difference no wider than one pixel cannot be told apart from where
        the rasterizer happened to put an edge: it is the instrument's own
        rounding rather than something that moved. Wider than that is a real
        shape, and is flagged hard. The
        margin is float slack, not tolerance - a difference exactly one pixel
        wide is the soft case.
        """
        limit = self.pixel_width_mm * 1.0001
        return [region for region in self.regions if region.widest_mm > limit]

    @property
    def has_hard_regions(self) -> bool:
        """Does this layer hold any difference wider than one pixel?"""
        return bool(self.hard_regions)

    @property
    def worst_region(self):
        """The region that moved furthest, or ``None`` on a clean layer.

        Regions arrive worst-first, so this is normally the first; it is
        computed rather than indexed so the property holds whatever order a
        caller supplies.
        """
        return max(
            self.regions, key=lambda region: region.displacement_mm, default=None
        )

    @property
    def worst_displacement_mm(self) -> float:
        """How far the geometry moved at the worst place on this layer."""
        worst = self.worst_region
        return worst.displacement_mm if worst is not None else 0.0

    @property
    def widest_mm(self) -> float:
        """The thickest single point of any difference on this layer."""
        return max((region.widest_mm for region in self.regions), default=0.0)

    @property
    def over_threshold(self) -> bool:
        """Derived, never stored: the presentation layer over the numbers.

        **The verdict is displacement, judged on the worst single region.**
        Total differing area is context: it grows with how
        long an edge is, so a sub-micrometre rendering sliver traced right
        around an outline can carry more of it than a real fault, and no
        threshold on it can tell the two apart. Displacement can - the sliver
        is nanometres wide, a moved track is tenths of a millimetre.

        With no displacement threshold set this falls back to the pixel count
        it used to be, which is what a bare export-against-export comparison
        wants: nothing has been translated there, so any pixel at all is news.

        A layer that is not :attr:`judged` never returns True, whatever it
        measured.
        """
        if not self.judged:
            return False
        if self.displacement_threshold_mm is None:
            return self.pixels_differing > self.threshold
        return self.worst_displacement_mm > self.displacement_threshold_mm

    @property
    def has_carrier(self) -> bool:
        """Did the panel's carrier put anything on this layer?"""
        return self.carrier is not None and self.carrier.ink_pixels > 0

    @property
    def carrier_reason(self) -> str:
        """What the carrier artwork on this layer is, in the reader's terms."""
        return self.carrier.reason if self.has_carrier else ""

    @property
    def carrier_ink_area_mm2(self) -> float:
        """How much of this layer is carrier artwork, never judged."""
        return self.carrier.ink_area_mm2 if self.carrier is not None else 0.0

    @property
    def carrier_area_differing_mm2(self) -> float:
        """Difference set aside as carrier - measured, reported, not judged."""
        return self.carrier.area_differing_mm2 if self.carrier is not None else 0.0

    @property
    def carrier_leaked(self) -> bool:
        """Did instance artwork go missing outside its own window?

        The case the scoping says is impossible: the reference holds boards
        only, so it can have no ink of its own out in the carrier. True here
        means the window rule is wrong somewhere, and it is reported loudly -
        the difference is judged either way.
        """
        return self.carrier is not None and self.carrier.leaked

    @property
    def canvas_area_mm2(self) -> float:
        return self.total_pixels * self.pixel_area_mm2

    @property
    def ink_area_a_mm2(self) -> float:
        return self.ink_pixels_a * self.pixel_area_mm2

    @property
    def ink_area_b_mm2(self) -> float:
        return self.ink_pixels_b * self.pixel_area_mm2

    @property
    def area_differing_mm2(self) -> float:
        return self.pixels_differing * self.pixel_area_mm2

    @property
    def area_only_in_a_mm2(self) -> float:
        return self.pixels_only_in_a * self.pixel_area_mm2

    @property
    def area_only_in_b_mm2(self) -> float:
        return self.pixels_only_in_b * self.pixel_area_mm2

    @property
    def share_of_canvas(self) -> float:
        """Differing pixels as a fraction of the whole canvas."""
        return self.pixels_differing / self.total_pixels if self.total_pixels else 0.0

    @property
    def share_of_ink(self) -> float:
        """Differing pixels against the ink both sides have between them.

        A better sense of scale than share of canvas: a 1% difference on a
        layer that is 2% copper is enormous, and the canvas figure hides that.
        """
        union_ink = self.ink_pixels_a + self.ink_pixels_b - self._both_pixels
        return self.pixels_differing / union_ink if union_ink else 0.0

    @property
    def _both_pixels(self) -> int:
        return self.ink_pixels_a - self.pixels_only_in_a

    @property
    def region_count(self) -> int:
        return len(self.regions)

    @property
    def largest_region(self):
        """The region with the most differing pixels, or ``None`` if clean."""
        return max(self.regions, key=lambda r: r.pixels, default=None)

    def to_dict(self) -> dict:
        """A plain mapping, for logging or a future machine-read report."""
        return {
            "layer": self.layer,
            "present_in_a": self.present_in_a,
            "present_in_b": self.present_in_b,
            "total_pixels": self.total_pixels,
            "pixel_area_mm2": self.pixel_area_mm2,
            "ink_pixels_a": self.ink_pixels_a,
            "ink_pixels_b": self.ink_pixels_b,
            "ink_area_a_mm2": self.ink_area_a_mm2,
            "ink_area_b_mm2": self.ink_area_b_mm2,
            "pixels_differing": self.pixels_differing,
            "area_differing_mm2": self.area_differing_mm2,
            "pixels_only_in_a": self.pixels_only_in_a,
            "pixels_only_in_b": self.pixels_only_in_b,
            "area_only_in_a_mm2": self.area_only_in_a_mm2,
            "area_only_in_b_mm2": self.area_only_in_b_mm2,
            "share_of_canvas": self.share_of_canvas,
            "share_of_ink": self.share_of_ink,
            "region_count": self.region_count,
            "regions": [region.to_dict() for region in self.regions],
            "worst_displacement_mm": self.worst_displacement_mm,
            "widest_mm": self.widest_mm,
            "threshold": self.threshold,
            "displacement_threshold_mm": self.displacement_threshold_mm,
            "judged": self.judged,
            "unjudged_reason": self.unjudged_reason,
            "carrier": self.carrier.to_dict() if self.carrier is not None else None,
            "over_threshold": self.over_threshold,
            "seconds": self.seconds,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, kw_only=True)
class CompareResult:
    """Every layer's measurements, and the canvas they were all taken on."""

    canvas: Canvas
    um_per_pixel: float
    threshold: int
    region_cell_mm: float
    layers: tuple
    dir_a: Path
    dir_b: Path
    seconds: float = 0.0
    metrics: dict = field(default_factory=dict)

    @property
    def pixels_differing(self) -> int:
        return sum(layer.pixels_differing for layer in self.layers)

    @property
    def area_differing_mm2(self) -> float:
        return sum(layer.area_differing_mm2 for layer in self.layers)

    @property
    def layers_over_threshold(self) -> list:
        return [layer for layer in self.layers if layer.over_threshold]

    @property
    def carrier_layers(self) -> list:
        """Layers the panel's carrier put artwork on, in layer order."""
        return [layer for layer in self.layers if layer.has_carrier]

    @property
    def carrier_ink_area_mm2(self) -> float:
        return sum(layer.carrier_ink_area_mm2 for layer in self.layers)

    @property
    def leaked_layers(self) -> list:
        """Layers where instance artwork was missing outside its own window.

        Empty on every panel this is right about. Not empty means the window
        rule needs looking at, and every page and log says so loudly.
        """
        return [layer for layer in self.layers if layer.carrier_leaked]

    @property
    def clean(self) -> bool:
        """True when nothing exceeded the threshold. Not a pass/fail gate."""
        return not self.layers_over_threshold

    def to_dict(self) -> dict:
        return {
            "dir_a": str(self.dir_a),
            "dir_b": str(self.dir_b),
            "um_per_pixel": self.um_per_pixel,
            "threshold": self.threshold,
            "region_cell_mm": self.region_cell_mm,
            "canvas": {
                "min_x_mm": self.canvas.bounds.min_x_mm,
                "min_y_mm": self.canvas.bounds.min_y_mm,
                "max_x_mm": self.canvas.bounds.max_x_mm,
                "max_y_mm": self.canvas.bounds.max_y_mm,
                "width_px": self.canvas.width_px,
                "height_px": self.canvas.height_px,
                "mm_per_pixel": self.canvas.mm_per_pixel,
            },
            "pixels_differing": self.pixels_differing,
            "area_differing_mm2": self.area_differing_mm2,
            "carrier_ink_area_mm2": self.carrier_ink_area_mm2,
            "carrier_layers": [layer.layer for layer in self.carrier_layers],
            "leaked_layers": [layer.layer for layer in self.leaked_layers],
            "seconds": self.seconds,
            "layers": [layer.to_dict() for layer in self.layers],
            "metrics": dict(self.metrics),
        }
