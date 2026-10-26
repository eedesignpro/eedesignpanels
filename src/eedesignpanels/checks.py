"""Checks: they warn and they continue, and they never abort a build.

A CHECK is a comparison whose
result is information - it goes into the run output and the review page and
the build finishes regardless. An ERROR is the program unable to do what was
asked, and it still propagates to the caller. Nothing here raises on a
disagreement; it reports one.

Some check outcomes nevertheless say the toolpath that was computed MUST NOT BE
CUT. Those are named by :func:`toolpath_alarms`, in one place, so the preview,
the report and anything later all mean the same thing by "this toolpath is not
valid" - and still nothing is aborted.

The extents check is the cheapest real check the pipeline has. `kicad-cli`
with `--page-size-mode 2` crops the SVG to the board area, so the SVG's own
`width` and `height` in millimetres are an independent measurement of the panel
we just built, made by KiCad rather than by us.
"""

import re
from typing import NamedTuple

# KiCad approximates an arc with segments to a maximum error of ARC_HIGH_DEF,
# 0.005 mm (KiCad 10.0.3, include/base_units.h:119), when it computes the box
# it crops the plot to. An inscribed approximation measures slightly small, and
# more than one arc can sit on the panel edge, so agreement is judged at 0.05 mm
# - an order of magnitude above that noise and far below any stepping error.
DEFAULT_TOLERANCE_MM = 0.05

_SVG_SIZE = re.compile(r'width="([\d.]+)mm"\s+height="([\d.]+)mm"')


class ExtentsCheck(NamedTuple):
    """Predicted panel extents against the ones the renderer measured."""

    predicted_mm: tuple
    rendered_mm: tuple
    tolerance_mm: float

    @property
    def deviation_mm(self) -> tuple:
        """Rendered minus predicted, per axis. Negative means measured small."""
        return (
            self.rendered_mm[0] - self.predicted_mm[0],
            self.rendered_mm[1] - self.predicted_mm[1],
        )

    @property
    def agrees(self) -> bool:
        return all(abs(value) <= self.tolerance_mm for value in self.deviation_mm)

    @property
    def summary(self) -> str:
        """One line for the run output and the review page."""
        dx, dy = self.deviation_mm
        verdict = "agree" if self.agrees else "DISAGREE"
        return (
            f"predicted {self.predicted_mm[0]:.4f} x {self.predicted_mm[1]:.4f} mm, "
            f"rendered {self.rendered_mm[0]:.4f} x {self.rendered_mm[1]:.4f} mm, "
            f"deviation {dx:+.4f} / {dy:+.4f} mm "
            f"(tolerance {self.tolerance_mm:.4f} mm) - {verdict}"
        )


def svg_extent_mm(svg_text: str) -> tuple:
    """The `width`/`height` a kicad-cli SVG reports, in millimetres.

    Raises:
        ValueError: when the text carries no millimetre size, which means it is
            not a kicad-cli export and the caller measured the wrong thing.
    """
    found = _SVG_SIZE.search(svg_text)
    if not found:
        raise ValueError("no width/height in mm found - not a kicad-cli SVG?")
    return (float(found.group(1)), float(found.group(2)))


def compare_extents(
    predicted_mm: tuple,
    rendered_mm: tuple,
    tolerance_mm: float = DEFAULT_TOLERANCE_MM,
) -> ExtentsCheck:
    """Compare predicted panel extents with rendered ones. Never raises."""
    return ExtentsCheck(
        predicted_mm=tuple(predicted_mm),
        rendered_mm=tuple(rendered_mm),
        tolerance_mm=tolerance_mm,
    )


def toolpath_alarms(result) -> tuple:
    """Every outcome of one prep run that INVALIDATES the toolpath it produced.

    Three, and the set is deliberately small. The preview paints the toolpath
    in an alarm colour when this is non-empty, and a colour that fires on every
    quibble stops being read:

    * **the cutter exceeds a tab's ceiling.** A tab declares the widest bit it
      survives in `_PANEL.ROUTE_TOOL_D_MAX`; a wider one cuts the tab away, so
      the path holds no board.
    * **the finished cut runs through a declared tab** (`guard_clear`). Same
      outcome by a different route, and it is asked of the RESULT.
    * **the finished cut runs along a scored edge** (`scores_clear`). The router
      machines away the material the saw was going to separate, so the board
      leaves the panel through neither.

    What is deliberately NOT here, because each is a report about a valid path:
    `min_tab_clear` (a declared tab narrower than the setting - the DESIGN wins
    and the tab stands), `widths_agree` (a computed width disagreeing with its
    measured stop-line spacing), `junctions_clear` (board edge standing at a
    score junction is a warning the designer acts on, and the toolpath it
    reports is still the path that was asked for), and stale or
    undeclared-layer notices, which are about the file rather than the cut.

    Duck-typed on `route_toolpath.PrepResult`. Returns one sentence per alarm,
    in a fixed order, so the same result always reads the same way.
    """
    alarms = []
    ceiling = getattr(result, "tool_max_mm", None)
    cutter = getattr(result, "cutter_diameter_mm", None)
    if ceiling is not None and cutter is not None and cutter > ceiling:
        alarms.append(
            f"the {cutter:.4f} mm cutter exceeds the {ceiling:.4f} mm ceiling "
            f"the tabs on this board declare"
        )
    for hit in getattr(result, "rout_through", ()) or ():
        alarms.append(f"the finished cut runs through tab {hit.label}")
    for hit in getattr(result, "routed_scores", ()) or ():
        alarms.append(f"the finished cut runs along {hit.label}")
    return tuple(alarms)


def feature_alarms(result) -> tuple:
    """Every FEATURE on this board the declared cutter cannot make as drawn.

    A different alarm from :func:`toolpath_alarms`, and deliberately so: it
    reports that not all features could be cut, and its highlight is red. That
    one says the
    path that WAS computed must not be cut; this one says a shape the designer
    drew never became a path at all. The panel still builds - it is a report,
    not a refusal - and the sentences come from
    :func:`eedesignpanels.cuttability.alarms`, which is where the verdict was
    reached.

    TWO KINDS, both red: a feature the bit cannot enter at all, and an inside
    corner tighter than the bit, which comes out rounded to the tool rather
    than sharp. The second is reported even though every square hole has four
    such corners and every notch has one, because what a fab receives is not
    the shape drawn.

    Duck-typed on `route_toolpath.PrepResult`, like its sibling.
    """
    from eedesignpanels import cuttability

    return cuttability.alarms(getattr(result, "jobs", ()) or ())
