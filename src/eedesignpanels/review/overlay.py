"""Annotation the PAGE draws: the v-score proposal, over kicad-cli's own plots.

The review page never synthesises board geometry - a view assembled
by our own code can look right while the output is wrong, and catching that is
what the review loop is for. **A proposal is not board geometry.** A candidate
blade line the stage turned down exists nowhere in the file, on no layer, and
there is nothing for kicad-cli to plot; but a designer cannot review a proposal
they cannot see, and "which edges could have been scored, and why was this one
not?" is exactly the question this page has to answer. So it is drawn here, in
the same class as the comparison page's delta markers: annotation, kept
visually subordinate to the real layers, and carried on its own toggle so it can
be switched off in one click.

What is drawn is exactly what the board cannot show. Every candidate gets its
blade line - the line, produced across the view, because a blade does not stop
where the outline does - and a caption naming it and its verdict.

**One line in two dash states, on every page.** One line, one width, one
colour: DASHED where the blade line runs off the board and SOLID along the
stretch it contacts the board edge, for all three verdicts alike. The live
preview and the frozen review page draw it the same way, so one drawing has one
meaning. Nothing new is drawn: the produced line was already there, over the
same stretch, so an accepted candidate is still not having its board geometry
synthesised. This is ANNOTATION styling only.

Three verdicts and three colours, because the reader has to tell a line the
DESIGNER declined from one the TOOL will not propose.
Green is scored, neutral grey is proposed and not accepted, amber is rejected
because the blade line passes through a tab.

**A caption never sits over the board or over the toolpath**, and that
outranks any preference about sides. It steps OUT of the material, square off
its own blade line, by a fixed clearance - one router diameter and a margin,
which is the whole reach of the channel the cutter takes out of that edge.
"Top of a horizontal line, right of a vertical one" only settles the case where
neither side is material: a blade line the board does not touch, or a tie.
Applied to every line, that preference put two of the corpus board's captions
1.389 mm INSIDE the outline, over board content.

The overlay is one more SVG in the same stack, in the same millimetre
coordinates, so the existing viewport, pan/zoom and layer panel drive it with no
special case anywhere. Alignment is the only thing that needs care: kicad-cli's
`--page-size-mode 2` crops to the board's own bounding box, so the exported
coordinate system is board millimetres shifted to that box's corner. The caller
supplies that corner and the viewBox it must match; a mismatch is reported
rather than drawn crooked. A caption that sits outside the board needs air
around it to be seen at all, which is why the frozen page pads its viewport by
:func:`label_reach_mm` and the live preview renders into
:func:`preview.session.view_window`.
"""

import html
import math
import re
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.review.theme import ALARM_COLOR

# Three verdicts, three colours. Green and amber are the two the layer panel
# already uses for "passes" and "worth looking at", so a colour means one thing
# across the artifacts. The third is deliberately NEUTRAL: a candidate the
# designer did not accept is a decision, not a defect, and colouring a person's
# choice amber would tell them the tool disagreed with it.
ACCEPTED_COLOR = "#2ea043"
REJECTED_COLOR = "#e8a13a"
PROPOSED_COLOR = "#9aa4ae"
# Everything here is annotation, so nothing is drawn at full strength: the plots
# underneath are the thing being reviewed.
LINE_OPACITY = 0.85
BLADE_OPACITY = 0.5
# Fractions of the board's larger dimension, so one style reads the same on a
# 15 mm coupon and a 200 mm panel.
BLADE_WIDTH_FRACTION = 0.003
LABEL_FRACTION = 0.022
# Every caption is now placed clear of the board, so every caption has to fit in
# the air the view leaves round it - and it is set against its anchor, so its own
# width counts against that margin as well as the clearance. Three quarters of
# the fraction above is what that buys, and the picture is vector, so smaller
# costs nothing a reader cannot zoom back.
OUTSIDE_LABEL_FACTOR = 0.75
# How wide one character is, as a fraction of the font size, for a monospace
# face. Used to state the fit above as a test, and by the preview to size the
# window it renders.
LABEL_CHAR_WIDTH = 0.6
# The widest caption this can produce for a board with fewer than a hundred
# candidates - `#12 PROPOSED`. The window arithmetic is stated against it.
LABEL_CAPTION_CHARS = 12
# How much more room than that arithmetic asks for the window leaves. Two
# reasons, both small and both real: coordinates go into the SVG rounded to four
# decimals, and 0.6 em is a nominal monospace advance rather than a measured one.
LABEL_FIT_HEADROOM = 1.05
# THE LABEL CLEARANCE: a caption sits one router diameter
# plus this margin off the board edge, measured square off the outline. The
# routed channel reaches exactly one diameter out from the edge it follows - a
# radius to the centreline and a radius past it - so a caption this far off is
# clear of the channel by the margin, whatever the cutter and whatever the
# junction overshoot. Half a millimetre is a comfortable gap at the scale a
# reviewer zooms to and is small enough to keep the caption beside the line it
# names rather than adrift of it.
LABEL_CLEAR_MARGIN_MM = 0.5
# Where a caption's glyphs sit against its own baseline, as fractions of the
# font size: how far the capitals rise above it, and how far the middle of that
# body lies below it. Nominal figures for a monospace face, used to place the
# BODY of a caption rather than its baseline - a caption set below the line it
# names would otherwise grow back over the board.
LABEL_CAP_FRACTION = 0.8
LABEL_MIDDLE_FRACTION = 0.35
# Two directions count as square to one another below this. Every dot product it
# guards is exactly 0 or exactly +-1 for an axis-aligned line, so it is a guard
# against float dust and never a judgement call.
SQUARE_TOL = 1e-6
# Enough of the id to tell four candidates apart at a glance; the run report
# prints the same prefix, which is what ties a line on the page to a line in the
# terminal.
SHORT_ID_CHARS = 8
# THE CLICK TARGET. A caption is a control on the live
# preview: clicking it toggles a proposable candidate between SCORED and
# PROPOSED. Glyphs are a mean thing to aim at, so the target is a rectangle
# round the caption, grown by this fraction of the font size on every side -
# roughly half a character, which makes the whole word plus its margin
# clickable without reaching over the next caption.
HIT_PAD_FRACTION = 0.55
# The class names the page's CSS and its click handler key on. Emitted only
# when a caller asks for hit targets, so the frozen review artifact - which
# has nothing to talk to - carries neither.
HIT_CLASS = "pb-hit"
HIT_BOX_CLASS = "pb-hitbox"


_VIEWBOX = re.compile(
    r'viewBox="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"'
)


class ViewBox(NamedTuple):
    """The SVG viewBox the export used, in millimetres."""

    x: float
    y: float
    width: float
    height: float

    @property
    def larger(self) -> float:
        return max(self.width, self.height)


def viewbox_of(svg_path: Path) -> ViewBox:
    """The viewBox of an exported SVG - the coordinate system to draw in.

    Raises:
        ValueError: when the file carries none. Guessing one would put the
            annotation somewhere it does not belong.
    """
    found = _VIEWBOX.search(Path(svg_path).read_text(encoding="utf-8"))
    if not found:
        raise ValueError(f"{svg_path} has no viewBox - not a kicad-cli export?")
    return ViewBox(*(float(value) for value in found.groups()))


def _mm(value: float) -> str:
    """One coordinate, as the overlay writes it: four decimals, and no -0.

    A negative zero is the same coordinate as a zero and has to print the same,
    or the identical drawing of a board at the origin and of the same board
    195 mm away comes out as different bytes.
    """
    text = f"{value:.4f}"
    return text[1:] if text.startswith("-") and float(text) == 0.0 else text


def _to_svg(point: tuple, origin: tuple, box: ViewBox) -> tuple:
    """A board millimetre point in the export's own coordinates."""
    return (point[0] - origin[0] + box.x, point[1] - origin[1] + box.y)


def _blade_ends(candidate, origin: tuple, box: ViewBox) -> tuple:
    """The candidate's line produced to the edges of the viewport.

    The blade does not stop where the outline does - that is the whole reason a
    line either carries a tab or does not - so the overlay draws the line across
    the view and the spans on top of it.
    """
    start = _to_svg(candidate.start, origin, box)
    end = _to_svg(candidate.end, origin, box)
    length = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
    if length <= 0.0:
        return start, end
    unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    reach = box.larger
    return (
        (start[0] - unit[0] * reach, start[1] - unit[1] * reach),
        (end[0] + unit[0] * reach, end[1] + unit[1] * reach),
    )


def label_side(unit: tuple, normal: tuple = None) -> tuple:
    """Which way a caption steps off its own blade line: OUT of the material.

    Two rules decide it, in order. The first governs - a caption never
    overlaps the board or the toolpath - so a caption goes on the side of the
    line the material is NOT on.
    *normal* is the candidate's own, pointing from the score plane INTO the
    board, so the answer is simply the other way.

    The second rule - top of a horizontal line, right of a vertical one -
    settles the case where the material says nothing: a blade line the board
    does not touch, or a tie. Board millimetres put +y down, so "up" is the
    normal with the negative y; the two normals of a vertical line have none,
    and the tie-break takes the one pointing right.

    Either way it reads a DIRECTION, and both of a line's directions give the
    same answer - which is the point of it. The stored line's start/end order is
    an accident of how it was drawn, and it used to decide which end of the
    board a caption flew off to.
    """
    length = math.hypot(unit[0], unit[1])
    along = (unit[0] / length, unit[1] / length) if length > 0.0 else (1.0, 0.0)
    side = (-along[1], along[0])
    if normal is not None:
        # Projected rather than negated, so a normal that is not exactly square
        # to the line still names a side instead of tilting the caption off it.
        into = side[0] * normal[0] + side[1] * normal[1]
        if abs(into) > SQUARE_TOL:
            return side if into < 0.0 else (-side[0], -side[1])
    if abs(side[1]) < SQUARE_TOL:
        return side if side[0] > 0.0 else (-side[0], -side[1])
    return side if side[1] < 0.0 else (-side[0], -side[1])


def label_clearance_mm(cutter_diameter_mm: float) -> float:
    """How far off the outline a caption sits: one router diameter, plus margin.

    The channel the router cuts reaches one whole diameter out from the edge it
    follows, so this is the smallest offset that cannot be overlapped by it.
    """
    return cutter_diameter_mm + LABEL_CLEAR_MARGIN_MM


def label_reach_mm(larger_mm: float, clearance_mm: float) -> float:
    """How far off the board a caption reaches, square to the line it names.

    What a page has to leave in air round the board for the caption to be seen:
    the clearance it is held off the edge by, plus the height of the caption
    itself, since the clearance places its BASELINE. *larger_mm* is the larger
    dimension of the frame the overlay is drawn in, because the type is sized as
    a fraction of that.

    This is the perpendicular reach only. A caption set ALONG its line runs the
    other way and stays beside the edge it names; one set upright runs away from
    the line, and the caller that does that (the live preview) counts its width
    as well - see :func:`eedesignpanels.preview.session.view_window`.
    """
    font = larger_mm * LABEL_FRACTION * OUTSIDE_LABEL_FACTOR
    return (clearance_mm + font * LABEL_CAP_FRACTION) * LABEL_FIT_HEADROOM


def _label_angle(candidate) -> float:
    """Degrees to turn the caption so it runs ALONG the line, reading upright."""
    unit = candidate.unit
    angle = math.degrees(math.atan2(unit[1], unit[0]))
    if angle > 90.0:
        angle -= 180.0
    elif angle <= -90.0:
        angle += 180.0
    return angle


def _caption_set(side: tuple, angle: float, size: float) -> tuple:
    """How a caption turned by *angle* has to be set to lie on the *side* given.

    Returns `(shift, place)`: how far to carry the anchor off the point the
    clearance put it at, in millimetres, and the `text-anchor` to set it with.
    Both come from where the caption's own two directions point once it is
    turned - it reads along its +x and its glyph body rises above its baseline -
    against the outward side it must lie on.
    """
    turn = math.radians(angle)
    run = (math.cos(turn), math.sin(turn))
    rises = (math.sin(turn), -math.cos(turn))
    out_run = run[0] * side[0] + run[1] * side[1]
    out_rise = rises[0] * side[0] + rises[1] * side[1]

    if out_rise > SQUARE_TOL:
        shift = (0.0, 0.0)
    elif out_rise < -SQUARE_TOL:
        # The body would rise back over the board: carry the baseline one cap
        # height further out, so what clears the edge is the caption and not the
        # line it is written on.
        shift = (side[0] * size * LABEL_CAP_FRACTION,
                 side[1] * size * LABEL_CAP_FRACTION)
    else:
        # The caption runs straight out from the line, so none of it is over the
        # board either way: centre its body on the line instead.
        shift = (-rises[0] * size * LABEL_MIDDLE_FRACTION,
                 -rises[1] * size * LABEL_MIDDLE_FRACTION)

    if out_run > SQUARE_TOL:
        place = "start"
    elif out_run < -SQUARE_TOL:
        place = "end"
    else:
        place = "middle"
    return shift, place


def caption_placement(candidate, origin: tuple, box: ViewBox, clearance: float,
                      size: float, angle: float) -> tuple:
    """Where one caption sits and how it is set. Returns `(anchor, place)`.

    Square off the candidate's own line at its midpoint, on the side
    :func:`label_side` names, by *clearance* millimetres - then off by whatever
    setting the caption at *angle* needs to keep its body out there too.
    """
    side = label_side(candidate.unit, getattr(candidate, "normal", None))
    shift, place = _caption_set(side, angle, size)
    middle = candidate.midpoint
    point = (middle[0] + side[0] * clearance + shift[0],
             middle[1] + side[1] * clearance + shift[1])
    return _to_svg(point, origin, box), place


def caption_box(anchor: tuple, place: str, size: float, chars: int,
                pad_fraction: float = HIT_PAD_FRACTION) -> tuple:
    """The rectangle one caption can be clicked in: `(x, y, width, height)`.

    Pure arithmetic on the same figures that set the caption - the anchor the
    clearance put it at, how it is anchored across that point, the font size and
    how many characters it has - grown by *pad_fraction* of the font size on
    every side. In the caption's OWN frame, so a turned caption gets the same
    box under the same rotation.

    The glyph body runs one cap height UP from the baseline, which is why the
    box's top is above the anchor rather than below it.
    """
    width = chars * LABEL_CHAR_WIDTH * size
    height = size * LABEL_CAP_FRACTION
    left = {"start": 0.0, "end": -width, "middle": -width / 2.0}[place]
    pad = size * pad_fraction
    return (anchor[0] + left - pad, anchor[1] - height - pad,
            width + 2.0 * pad, height + 2.0 * pad)


def _hit_markup(hit: dict, anchor: tuple, place: str, size: float, chars: int,
                angle: float, text: str) -> str:
    """One caption wrapped as a click target: a box, a tooltip, and the text.

    *hit* carries what the page's handler needs - the candidate's number and the
    action a click takes - as data attributes, plus the sentence the tooltip
    says. Nothing about the geometry travels: the handler looks the candidate up
    by number in the state it was drawn from, so a click and the accept button
    post the identical body.
    """
    box = caption_box(anchor, place, size, chars)
    turn = (
        f' transform="rotate({angle:.3f} {_mm(anchor[0])} {_mm(anchor[1])})"'
        if angle else ""
    )
    data = "".join(
        f' data-{name}="{html.escape(str(value), quote=True)}"'
        for name, value in sorted(hit.items())
        if name != "title"
    )
    title = hit.get("title", "")
    tip = f"<title>{html.escape(title)}</title>" if title else ""
    return (
        f'<g class="{HIT_CLASS}"{data}>{tip}'
        f'<rect class="{HIT_BOX_CLASS}" x="{_mm(box[0])}" y="{_mm(box[1])}" '
        f'width="{box[2]:.4f}" height="{box[3]:.4f}"{turn}/>'
        f"{text}</g>"
    )


def verdict_color(proposal) -> str:
    """The colour one verdict is drawn in: scored, proposed, or rejected.

    Reading `proposable` rather than the verdict string, so a wording change on
    the page cannot silently repaint the overlay.
    """
    if proposal.accepted:
        return ACCEPTED_COLOR
    if getattr(proposal, "proposable", True):
        return PROPOSED_COLOR
    return REJECTED_COLOR


def _along(point: tuple, first: tuple, last: tuple) -> float:
    """Where *point* falls along the produced line, as a distance from *first*."""
    span = (last[0] - first[0], last[1] - first[1])
    length = math.hypot(*span)
    if length <= 0.0:
        return 0.0
    unit = (span[0] / length, span[1] / length)
    return (point[0] - first[0]) * unit[0] + (point[1] - first[1]) * unit[1]


def _at(first: tuple, last: tuple, distance: float) -> tuple:
    span = (last[0] - first[0], last[1] - first[1])
    length = math.hypot(*span)
    if length <= 0.0:
        return first
    return (first[0] + span[0] * distance / length,
            first[1] + span[1] * distance / length)


def contact_stretches(spans, first: tuple, last: tuple) -> list:
    """A produced line cut into `(from, to, contacts)` stretches.

    Distances along the line from *first*, in order and without gaps, each
    saying whether the blade is over board edge there. Overlapping or touching
    spans merge, so one stretch is one dash state and the line reads as one
    line.

    *spans* are `(start, end)` point pairs IN THE FRAME *first* and *last* are
    in. Stated, and taken as points rather than as span records, because it is
    exactly what went wrong: measured against a produced line already carried
    into the export's coordinates, a span still in board millimetres projects
    off the end of it and every contact stretch disappears.
    """
    total = math.dist(first, last)
    spans = sorted(
        (
            min(_along(start, first, last), _along(end, first, last)),
            max(_along(start, first, last), _along(end, first, last)),
        )
        for start, end in spans
    )
    merged: list = []
    for low, high in spans:
        low, high = max(0.0, low), min(total, high)
        if high <= low:
            continue
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))

    out, at = [], 0.0
    for low, high in merged:
        if low > at:
            out.append((at, low, False))
        out.append((low, high, True))
        at = high
    if at < total:
        out.append((at, total, False))
    return out


def _blade_paths(candidate, origin: tuple, box: ViewBox, first: tuple,
                 last: tuple, color: str, width: float) -> str:
    """The blade line as one stroke in two dash states."""
    spans = [
        (_to_svg(span.start, origin, box), _to_svg(span.end, origin, box))
        for span in candidate.spans
    ]
    body = ""
    for start, end, contacts in contact_stretches(spans, first, last):
        from_point, to_point = _at(first, last, start), _at(first, last, end)
        dashes = (
            "" if contacts
            else f'stroke-dasharray="{width * 6:.4f} {width * 6:.4f}" '
        )
        body += (
            f'<path class="pb-blade{" on" if contacts else ""}" '
            f'd="M{_mm(from_point[0])} {_mm(from_point[1])} '
            f'L{_mm(to_point[0])} {_mm(to_point[1])}" stroke="{color}" '
            f'stroke-width="{width:.4f}" {dashes}'
            f'opacity="{BLADE_OPACITY if not contacts else LINE_OPACITY}"/>'
        )
    return body


# HOW AN UNCUTTABLE FEATURE IS MARKED: in red, because a feature the cutter
# cannot make is a fault the reader has to see at once.
# A box round the feature says WHICH shape, and a circle of the declared cutter
# drawn on the same picture says WHY - a 2 mm disc sitting over a 1.3 mm hole is
# the whole argument, and it needs no sentence.
UNCUTTABLE_PAD_FRACTION = 0.6
UNCUTTABLE_WIDTH_FRACTION = 0.0025
UNCUTTABLE_DASH_FRACTION = 0.01

# AND A CORNER TIGHTER THAN THE BIT IS RED TOO. The mark is the tool itself,
# dashed, sitting on the
# corner it cannot reach into - the same argument the disc over a small hole
# makes, and it needs no caption: the circle IS the radius. No caption is also
# what keeps a square hole's four corners legible, and the alarm state names
# each corner's place and radius in words.


def uncuttable_svg(jobs: list, origin: tuple, box: ViewBox,
                   cutter_diameter_mm: float) -> str:
    """The red marks over every feature the declared cutter cannot make.

    One standalone SVG in the same millimetre frame as every other layer, so the
    existing stack, pan and zoom drive it with no special case. *jobs* are the
    :class:`eedesignpanels.cuttability.Job` records carrying something red -
    :func:`eedesignpanels.cuttability.marked` - and a call with none of them
    returns an empty document rather than nothing, so a caller never has to
    decide whether the layer exists.

    TWO MARKS, BECAUSE THERE ARE TWO FACTS. A
    feature the bit cannot make gets a box, the tool as a dashed disc and its
    size; a corner the bit cannot reach into gets the tool as a dashed disc on
    the corner itself, because that feature IS cut and only its corner is not
    the shape that was drawn.

    Nothing here is board geometry: the box and the caption are
    annotation, and the circle is the declared TOOL, which appears on no layer
    of any board.
    """
    width = box.larger * UNCUTTABLE_WIDTH_FRACTION
    dash = box.larger * UNCUTTABLE_DASH_FRACTION
    font = box.larger * LABEL_FRACTION * OUTSIDE_LABEL_FACTOR
    body = ""
    for job in jobs:
        for corner in job.rounded:
            at = _to_svg(corner.at, origin, box)
            body += (
                f'<circle cx="{_mm(at[0])}" cy="{_mm(at[1])}" '
                f'r="{corner.radius_mm:.4f}" stroke="{ALARM_COLOR}" '
                f'stroke-width="{width:.4f}" stroke-dasharray="{dash:.4f}"/>'
            )
        if job.made:
            continue
        across, down = job.size_mm
        pad = max(across, down) * UNCUTTABLE_PAD_FRACTION
        left, top = _to_svg((job.bbox[0] - pad, job.bbox[1] - pad), origin, box)
        centre = _to_svg(job.centre, origin, box)
        body += (
            f'<rect x="{_mm(left)}" y="{_mm(top)}" '
            f'width="{_mm(across + 2.0 * pad)}" '
            f'height="{_mm(down + 2.0 * pad)}" '
            f'stroke="{ALARM_COLOR}" stroke-width="{width:.4f}"/>'
            f'<circle cx="{_mm(centre[0])}" cy="{_mm(centre[1])}" '
            f'r="{cutter_diameter_mm / 2.0:.4f}" stroke="{ALARM_COLOR}" '
            f'stroke-width="{width:.4f}" stroke-dasharray="{dash:.4f}" '
            f'opacity="{BLADE_OPACITY}"/>'
        )
        # THE SIZE, AND NOTHING ELSE. The reason is a whole sentence and it is
        # already in the status line, the run notes and the report; set over a
        # 1.3 mm hole it would run across the next hole's mark. The size is what
        # matches this mark to its line in the list.
        caption = f"{across:.2f} x {down:.2f} mm"
        body += (
            f'<text class="pb-label" x="{_mm(centre[0])}" '
            f'y="{_mm(top - font * 0.4)}" fill="{ALARM_COLOR}" '
            f'font-size="{font:.4f}" text-anchor="middle" '
            f'opacity="{LINE_OPACITY}">{html.escape(caption)}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{box.width:.4f}mm" height="{box.height:.4f}mm" '
        f'viewBox="{box.x:.4f} {box.y:.4f} {box.width:.4f} {box.height:.4f}">'
        '<g fill="none" stroke-linecap="round" '
        'font-family="ui-monospace, monospace">'
        f"{body}</g></svg>"
    )


def proposal_svg(proposals: list, origin: tuple, box: ViewBox,
                 clearance_mm: float, captions: list = None,
                 along_line: bool = False, hits: list = None) -> str:
    """The whole proposal overlay as one standalone SVG document.

    *proposals* are :class:`route_toolpath.Proposal` records, *origin* is the
    board millimetre coordinate that the export put at the viewBox corner, and
    *clearance_mm* is how far off the outline a caption must sit
    (:func:`label_clearance_mm`). The clearance is not optional: only a caller
    that knows the cutter can say it, and a caption placed without it is over
    the channel or over the board, which is the one place a caption must not
    sit.

    Two arguments are presentation, and they are the only difference between the
    live preview and the frozen review page:

    * *captions* replaces the default `<short id> <verdict>` with one string per
      proposal, in the order given - the preview numbers its candidates `#1`,
      `#2` and shows three words (:mod:`eedesignpanels.preview.labels`), while
      the frozen page keeps the stage's own wording;
    * *along_line* turns each caption to read ALONG the line it names, which is
      how the frozen page has always set them and what keeps a long caption
      beside its edge instead of running out into the margin. The preview sets
      them upright.

    *hits* makes the captions CLICKABLE - one entry per
    proposal, in the order given: a mapping of data attributes for a caption a
    click may act on, and None for one it may not. A REJECTED candidate gets
    None, so it carries no target and no affordance. Omitted entirely - the
    frozen review artifact, which has no server to talk to - and not one byte
    changes.

    Everything else is one drawing with one meaning: the blade line as ONE
    stroke, dashed where it runs off the board and solid along the board edge,
    and every caption a clearance OUT of the material.
    """
    blade_width = box.larger * BLADE_WIDTH_FRACTION
    font = box.larger * LABEL_FRACTION * OUTSIDE_LABEL_FACTOR

    body = ""
    for index, proposal in enumerate(proposals):
        candidate = proposal.candidate
        color = verdict_color(proposal)
        first, last = _blade_ends(candidate, origin, box)
        body += _blade_paths(
            candidate, origin, box, first, last, color, blade_width
        )
        caption = (
            captions[index] if captions is not None
            else f"{proposal.id[:SHORT_ID_CHARS]} {proposal.verdict}"
        )
        angle = _label_angle(candidate) if along_line else 0.0
        anchor, place = caption_placement(
            candidate, origin, box, clearance_mm, font, angle
        )
        turn = (
            f'transform="rotate({angle:.3f} '
            f'{_mm(anchor[0])} {_mm(anchor[1])})" ' if along_line else ""
        )
        text = (
            f'<text class="pb-label" x="{_mm(anchor[0])}" y="{_mm(anchor[1])}" '
            f'{turn}'
            f'fill="{color}" font-size="{font:.4f}" text-anchor="{place}" '
            f'opacity="{LINE_OPACITY}">{html.escape(caption)}</text>'
        )
        hit = hits[index] if hits is not None and index < len(hits) else None
        body += (
            _hit_markup(hit, anchor, place, font, len(caption), angle, text)
            if hit else text
        )


    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{box.width:.4f}mm" height="{box.height:.4f}mm" '
        f'viewBox="{box.x:.4f} {box.y:.4f} {box.width:.4f} {box.height:.4f}">'
        '<g fill="none" stroke-linecap="round" '
        'font-family="ui-monospace, monospace">'
        f"{body}</g></svg>"
    )


def score_extensions(lines, origin: tuple, box: ViewBox) -> str:
    """Noninteractive continuations of saved scores, for the preview only."""
    blade_width = box.larger * BLADE_WIDTH_FRACTION
    body = ""
    # Extend existing scores as annotation only. These paths have no controls.
    for line in lines:
        length = math.dist(line.start, line.end)
        if length == 0:
            continue
        unit = ((line.end[0] - line.start[0]) / length,
                (line.end[1] - line.start[1]) / length)
        start = _to_svg(line.start, origin, box)
        end = _to_svg(line.end, origin, box)
        reach = box.width + box.height
        for anchor, direction in ((start, -1), (end, 1)):
            far = (anchor[0] + direction * unit[0] * reach,
                   anchor[1] + direction * unit[1] * reach)
            body += (
                f'<path class="pb-manual-score-extension" pointer-events="none" '
                f'd="M {_mm(anchor[0])} {_mm(anchor[1])} '
                f'L {_mm(far[0])} {_mm(far[1])}" '
                f'stroke="{ACCEPTED_COLOR}" stroke-width="{blade_width:.4f}" '
                f'stroke-dasharray="1 1" opacity="{BLADE_OPACITY}">'
                '<title>User-added v-score extension</title></path>'
            )

    return f'<g fill="none">{body}</g>'
