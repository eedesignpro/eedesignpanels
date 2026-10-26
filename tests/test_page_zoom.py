"""The wheel turns the same way on every review page, and cannot stop doing so.

This is a REGRESSION FENCE. A vector viewer whose `zoomAt` takes a
`viewBox`-extent multiplier and a raster viewer whose `zoomAt` takes a
magnification multiplier give one number two opposite meanings. With one
shared wheel handler, correcting it for one viewer makes the other zoom
backwards.

Nothing about that is caught by looking at either page on its own, so the tests
here look at BOTH page families together and at the emitted JavaScript, which is
what the user's browser actually runs:

* one shared expression, from `eedesignpanels.pagezoom`, in both pages;
* one meaning for the factor - it multiplies the view's EXTENT;
* every viewer applying it that way, whatever it stores internally.
"""

import re
import unittest

from eedesignpanels.pagezoom import (
    DIFF_STEP,
    REVIEW_STEP,
    factor_for,
    wheel_factor_js,
)
from eedesignpanels.review.page import JS as REVIEW_JS
from eedesignpanels.verify.report import DIFF_JS

# Every wheel handler this project ships, as `(name, page JS, step)`.
PAGES = (
    ("panel review", REVIEW_JS, REVIEW_STEP),
    ("gerber diff", DIFF_JS, DIFF_STEP),
)

# How each viewer turns the factor into what the reader sees. The value is the
# JavaScript that must appear, and the comment is why it is that way round:
# a viewer that keeps the viewBox multiplies the extent, a viewer that keeps a
# magnification - the extent's reciprocal - divides.
VIEWER_ARITHMETIC = (
    ("panel review, svg viewBox", REVIEW_JS, "view[2] * k, view[3] * k"),
    ("gerber diff, vector viewer", DIFF_JS, "view[2] * k, view[3] * k"),
    ("gerber diff, raster viewer", DIFF_JS, "view.z / k"),
)

_TERNARY = re.compile(r"(\w+(?:\.\w+)*)\s*>\s*0\s*\?\s*([\d.]+)\s*:\s*1\s*/\s*([\d.]+)")


class ZoomRuleTest(unittest.TestCase):
    """The rule itself, in Python, before any page is looked at."""

    def test_wheel_up_narrows_the_view(self):
        # deltaY is NEGATIVE when the wheel turns up, and the factor multiplies
        # the extent, so up must give a factor below 1.
        for step in (REVIEW_STEP, DIFF_STEP):
            self.assertLess(factor_for(-120.0, step), 1.0)
            self.assertGreater(factor_for(120.0, step), 1.0)

    def test_up_and_down_are_exact_inverses(self):
        for step in (REVIEW_STEP, DIFF_STEP):
            self.assertAlmostEqual(
                factor_for(-1.0, step) * factor_for(1.0, step), 1.0, places=12
            )

    def test_a_step_of_one_would_be_no_zoom_at_all(self):
        self.assertEqual(factor_for(-1.0, 1.0), 1.0)

    def test_the_expression_says_what_the_function_computes(self):
        emitted = wheel_factor_js("e.deltaY", DIFF_STEP)
        self.assertEqual(emitted, "e.deltaY > 0 ? 1.25 : 1 / 1.25")


class EmittedPageTest(unittest.TestCase):
    """What the browser gets, read out of the page rather than assumed."""

    # `assertIn` against a page would print the whole script on failure, which
    # buries the one line that is wrong. Every assertion here reports the
    # fragment and the page instead.

    def test_every_page_uses_the_shared_expression(self):
        for name, js, step in PAGES:
            with self.subTest(name):
                delta = "event.deltaY" if "event.deltaY" in js else "e.deltaY"
                wanted = wheel_factor_js(delta, step)
                self.assertTrue(wanted in js, f"{name}: {wanted!r} not emitted")

    def test_no_page_writes_its_own_ternary(self):
        # The whole failure mode: two handlers, drifting apart. Every deltaY
        # test in the emitted script has to be the shared one.
        for name, js, step in PAGES:
            with self.subTest(name):
                found = _TERNARY.findall(js)
                self.assertEqual(len(found), 1, f"{name}: {found}")
                delta, up, down = found[0]
                self.assertTrue(delta.endswith("deltaY"))
                self.assertEqual(float(up), step)
                self.assertEqual(float(down), step)

    def test_wheel_up_zooms_in_on_every_page(self):
        # Read the step back OUT of the emitted JavaScript and put it through
        # the same rule, so a page that emits a different number is judged on
        # the number it actually emitted.
        for name, js, _ in PAGES:
            with self.subTest(name):
                _, up, _ = _TERNARY.findall(js)[0]
                self.assertLess(factor_for(-120.0, float(up)), 1.0)

    def test_no_placeholder_survives_into_a_page(self):
        for name, js, _ in PAGES:
            with self.subTest(name):
                self.assertFalse(
                    "__WHEEL_ZOOM__" in js, f"{name}: placeholder not replaced"
                )

    def test_every_viewer_applies_the_factor_to_the_extent(self):
        # The half of the contract that is not in the handler. A viewer that
        # multiplies a magnification by the factor is backwards even though the
        # handler above it is right - which is exactly what shipped.
        for name, js, arithmetic in VIEWER_ARITHMETIC:
            with self.subTest(name):
                self.assertTrue(
                    arithmetic in js, f"{name}: {arithmetic!r} not found"
                )

    def test_no_viewer_multiplies_a_magnification_by_the_factor(self):
        for name, js, _ in PAGES:
            for wrong in ("view.z * k", "view[2] / k"):
                with self.subTest(f"{name} {wrong}"):
                    self.assertFalse(
                        wrong in js,
                        f"{name}: {wrong!r} inverts the zoom for this viewer",
                    )


if __name__ == "__main__":
    unittest.main()
