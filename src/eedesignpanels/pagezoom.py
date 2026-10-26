"""The one rule every review page follows when the wheel turns over a picture.

Wheel UP zooms IN. That is KiCad's convention and the one this audience already
has in its hands, and a page that disagrees with the editor beside it is read as
broken - which is exactly what happened: the gerber diff page's raster viewports
were backwards from the moment the vector viewer landed beside them (af6ef5c),
because one wheel handler was feeding two viewers that meant opposite things by
the same number.

So the number has ONE meaning here, stated once and shared:

    **the zoom factor multiplies the view's EXTENT.**

A factor below 1 shows less of the board, which is zooming in. Every viewer on
every page takes it that way - the SVG viewers multiply their `viewBox` width by
it directly, and a viewer that keeps a magnification instead DIVIDES by it. The
factor is never a magnification multiplier, whatever the viewer stores.

:func:`factor_for` is that rule in Python and :func:`wheel_factor_js` emits it as
the JavaScript expression the page uses. The pages must call this rather than
writing the ternary themselves: that is what makes the two families provably
agree, and what `tests/test_page_zoom.py` fences.
"""

# How far one wheel notch moves the view. Per page family, because the diff
# page's job is to dive into a 5 um region and the panel review's is to look
# around - the DIRECTION is shared, the step is not.
REVIEW_STEP = 1.15
DIFF_STEP = 1.25


def factor_for(delta_y: float, step: float) -> float:
    """The zoom factor for one wheel event: a multiplier for the view's extent.

    `delta_y` is the browser's `WheelEvent.deltaY`, which is POSITIVE when the
    wheel turns down. Down widens the view (zoom out), up narrows it (zoom in).
    """
    return step if delta_y > 0.0 else 1.0 / step


def wheel_factor_js(delta: str, step: float) -> str:
    """:func:`factor_for` as a JavaScript expression over *delta*.

    *delta* is the expression naming the event's `deltaY` - `e.deltaY` on one
    page, `event.deltaY` on the other - so a page keeps its own variable names
    and still gets the shared rule.
    """
    return f"{delta} > 0 ? {step:g} : 1 / {step:g}"


__all__ = ["DIFF_STEP", "REVIEW_STEP", "factor_for", "wheel_factor_js"]
