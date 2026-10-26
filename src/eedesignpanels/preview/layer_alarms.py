"""Which layers are in an alarm state, why, and the colour every paint owes them.

**AN ALARM IS A FACT THE SESSION HOLDS, NOT A COLOUR A PAINT APPLIES ONCE.**
An invalid cutter diameter turns the cutter path red, and the content render
lands about a second later. If the alarm were only a colour handed to one
computation, that later render would put the layer back to the theme's yellow
and the warning would erase itself while the user was still reading it. A
warning that disappears on its own is worse than none, because by then the
reader has looked away.

The kicad-cli backend takes no per-layer colour (only `--theme`, see
:mod:`eedesignpanels.preview.render_seam`), so a colour substituted into one
theme object cannot survive a second render of the same board. The state has
to live somewhere every render consults.

So the alarmed set lives on the session as `{canonical layer: (reason, ...)}`,
and this module is the one place that:

* names WHICH layer an alarm belongs to (:func:`of_prep_result`) - the alarm
  sentences themselves stay `eedesignpanels.checks`' to write;
* paints a theme with it (:func:`themed`), which is the swatch and the alpha;
* **forces the alarm colour onto a document whichever backend drew it**
  (:func:`repainted`), which is what makes the state survive a repaint - it is
  applied to the markup rather than asked of the renderer, because one of the
  two backends cannot be asked;
* states the reason in one status line per alarmed layer (:func:`status`), so
  the picture is never red with the reason nowhere on the page.

**THE STATE CLEARS WHEN ITS CAUSE CLEARS, AND ONLY THEN.** Nothing here clears
anything: the set is derived from the prep
result on every recomputation, so a diameter that works again empties it and a
rebuild that changes nothing leaves it exactly as it was. No timer, no
"finished" event, no clearing on paint.
"""

import re

from eedesignpanels import checks, prep
from eedesignpanels.review import theme as theme_module
from eedesignpanels.stages import route_toolpath

# THE COLOUR IS THE THEME-INDEPENDENT ONE: a layer the run says must not be cut
# cannot read as somebody's colour preference.
ALARM_COLOR = theme_module.ALARM_COLOR

# What a status line says before the reason. Short, because it is a status
# rather than an explanation.
ALARM_HEADLINE = "MUST NOT BE CUT"

# ...and the one the uncuttable features wear. Two
# headlines, because they are two different facts: a toolpath that exists and
# must not be run, against a feature that never became a toolpath. A reader who
# cannot tell them apart cannot act on either.
FEATURE_HEADLINE = "CANNOT BE CUT"
HEADLINES = {prep.UNCUTTABLE_LAYER: FEATURE_HEADLINE}

# A paint inside a `style="..."`: `fill:#c2c2c2`, `stroke: rgb(1,2,3)`. The
# hyphenated properties (`stroke-width`, `fill-opacity`) cannot match, because
# the colon has to follow the word itself.
_STYLE_PAINT = re.compile(r"\b(fill|stroke)(\s*):(\s*)([^;\"'}]+)")
# ...and a paint as its own attribute, which is what the library renderer writes.
_ATTRIBUTE_PAINT = re.compile(r"\b(fill|stroke)=\"([^\"]*)\"")
# A paint that draws NOTHING is left alone: turning `fill:none` red would fill
# every stroked outline solid.
_NO_PAINT = frozenset({"none", "transparent", "inherit", ""})


def of_prep_result(result) -> dict:
    """The alarmed layers of one prep run: `{canonical name: (reason, ...)}`.

    One entry today - the cutter path, alarmed by every outcome
    :func:`eedesignpanels.checks.toolpath_alarms` names - and a mapping rather
    than a flag because the next item in the run puts an uncuttable FEATURE into
    the same state, on a layer of its own.

    An ordinary board answers `{}`.
    """
    if result is None:
        return {}
    alarmed = {}
    alarms = tuple(checks.toolpath_alarms(result))
    if alarms:
        alarmed[getattr(result, "toolpath_layer", route_toolpath.TOOLPATH_LAYER)] = alarms
    rigid_alarms = tuple(reason for rigid in getattr(result, "rigid_results", ())
                         for reason in checks.toolpath_alarms(rigid))
    if rigid_alarms:
        alarmed[route_toolpath.TOOLPATH_LAYER] = rigid_alarms
    # THE UNCUTTABLE FEATURES, ON A LAYER OF THEIR OWN. Their own layer rather
    # than the outline's, because a hole the cutter cannot make is one shape
    # among many on `Edge.Cuts` and painting that layer red would say the whole
    # board edge is wrong. The layer holds
    # the marks and nothing else, so the red lands on the features themselves.
    features = tuple(checks.feature_alarms(result))
    if features:
        alarmed[prep.UNCUTTABLE_LAYER] = features
    return alarmed


def reasons(alarmed, layer: str) -> tuple:
    """Why *layer* is alarmed, or `()`. Reads a mapping that may be None."""
    return tuple((alarmed or {}).get(layer, ()))


def themed(theme, alarmed):
    """*theme* with the alarm colour substituted for every alarmed layer.

    The swatch, the alpha and the fast render all read the same theme object, so
    one substitution answers all three. An empty set returns the theme itself,
    unchanged, so an ordinary board is untouched by any of this.
    """
    if not alarmed or theme is None:
        return theme
    return theme._replace(colors={
        **theme.colors,
        **{
            layer.lower().replace(".", "_"): ALARM_COLOR
            for layer in alarmed
        },
    })


def repainted(markup: str, color: str = ALARM_COLOR) -> str:
    """*markup* with every paint in it forced to *color*.

    **THE DOCUMENT IS RECOLOURED, NOT THE RENDERER ASKED.** kicad-cli has no
    per-layer colour argument, so the content render of an alarmed layer comes
    back in the theme's colour however the alarm is expressed upstream of it.
    Forcing the colour onto the bytes is the one place that holds for BOTH
    backends, so every paint shows the alarm.

    `fill:none` and its kind are left exactly as they are: a stroked outline
    whose fill was turned red would come back a solid red shape.
    """
    if not markup:
        return markup

    def style_paint(match):
        if match.group(4).strip().lower() in _NO_PAINT:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}:{match.group(3)}{color}"

    def attribute_paint(match):
        if match.group(2).strip().lower() in _NO_PAINT:
            return match.group(0)
        return f'{match.group(1)}="{color}"'

    return _ATTRIBUTE_PAINT.sub(
        attribute_paint, _STYLE_PAINT.sub(style_paint, markup)
    )


def documents_repainted(produced: dict, alarmed) -> dict:
    """*produced* with every alarmed layer's document forced to the alarm colour.

    The choke point: one call on the way out of a render, so the fast phase, the
    lettering tier, the content render and a refill all come back obeying the
    session's state rather than each honouring it separately. One choke point
    cannot be missed by a render path added later; a fix in each path can.
    """
    if not alarmed or not produced:
        return produced
    return {
        name: (repainted(markup) if name in alarmed else markup)
        for name, markup in produced.items()
    }


def status(alarmed, layer_names=None) -> list:
    """One status line per alarmed layer, in layer order, for the page.

    `{"layer": canonical, "display": what a reader calls it, "says": one line}`.
    The reason is stated in the display name because a status reading
    `User.36 MUST NOT BE CUT` is the UI showing a bare canonical name, which
    :mod:`eedesignpanels.preview.naming` keeps off the page.
    """
    named = layer_names or {}
    out = []
    for layer in sorted(alarmed or {}):
        said = reasons(alarmed, layer)
        display = named.get(layer, layer)
        headline = HEADLINES.get(layer, ALARM_HEADLINE)
        out.append({
            "layer": layer,
            "display": display,
            "says": f"{display} {headline} - {said[0]}" if said
                    else f"{display} {headline}",
            "reasons": list(said),
        })
    return out
