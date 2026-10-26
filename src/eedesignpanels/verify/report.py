"""The comparison's review: per-layer blends and statistics.

Per layer it shows the blended overlay, and A, B and the XOR of the two behind
a disclosure, beside the numeric result. Several comparison runs can share the
report, so "the clean run" and "the caught fault" arrive together.

:func:`build_diff_page` writes it as its own self-contained file - the second
artifact of every panel review, opened in its own browser tab. Given a layer
table it takes this shape:

* the **same layer panel component** the panel review page uses
  (:mod:`eedesignpanels.review.layerpanel`), same board order, same look;
* **one layer's comparison on screen at a time** - selecting a layer swaps the
  blend rather than stacking anything;
* a **pass/fail mark per row**, against the run's threshold. That is
  presentation over the statistics and never a replacement for them: the full
  per-layer table stays on the page;
* **one colour scheme for every layer** - grey where the two sides agree, the
  complementary pair where they do not. Theme colours belong to the viewer.

Without a layer table it falls back to listing every layer's section at once,
which is what a comparison-only run wants.

Everything is scoped inside a `.diffroot` element, CSS and JS alike, so the
report cannot restyle or re-handle the panel beside it.

Differences are made unmissable - a red banner, red table rows, red pixels -
because the harness reports rather than blocks,
and a report nobody notices is worse than no report.

This is a **dev harness artifact**, not a board plot, so the review page's rule
that the artifact embeds kicad-cli's unmodified plots does not apply. What does
still apply: no geometry is ever synthesised. Every pixel on this page came out
of the rasterizer that produced the numbers, so the picture and the count
cannot disagree.

The page is one portable file: no `<link>`, no `<script src>`, no `url()`
fetches - images are `data:` URIs - and system fonts only. The one use
of `localStorage` arrives with the shared layer panel and is guarded there.
"""

import base64
import html
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.pagezoom import DIFF_STEP, wheel_factor_js
from eedesignpanels.review import layerpanel
from eedesignpanels.verify import vector
from eedesignpanels.verify.results import CompareResult


class DiffRun(NamedTuple):
    """One comparison on the page: its result and the pictures behind it.

    ``vectors[layer]`` is that layer's picture drawn as SVG, which is the
    normal case: it stays sharp however far it is zoomed into, and the
    user zooms into a difference in order to judge it.

    ``images[layer] = {"blend": uri, "a": uri, "b": uri, "diff": uri}`` carries
    rasterised pictures - for a layer that outgrew the vector cap, and for the
    single-side views behind the disclosure either way.
    """

    name: str
    description: str
    result: CompareResult
    images: dict
    vectors: dict = {}
    label_a: str = "A"
    label_b: str = "B"


def data_uri(png: bytes) -> str:
    """A PNG as a `data:` URI, so the page needs no sibling files."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _slug(*parts: str) -> str:
    raw = "-".join(parts)
    return "".join(char if char.isalnum() or char == "-" else "_" for char in raw)


def _region_entry(region, index: int, over: bool, soft: bool) -> str:
    """One line: how far it moved, how wide it is, how many pixels.

    Deliberately short. No coordinates - the
    numbered button goes there, which is better than reading them; no area,
    which says nothing displacement does not; and no explanation, which
    belongs once above the group rather than on every line of it.
    """
    return (
        f'<li class="{"over" if over else "under"}{" soft" if soft else " hard"}">'
        + ("" if soft else '<span class="hardmark">wider than a pixel</span> ')
        + f'<button type="button" class="jump" '
        f'data-x="{(region.min_x_mm + region.max_x_mm) / 2:.6f}" '
        f'data-y="{(region.min_y_mm + region.max_y_mm) / 2:.6f}" '
        # The region's OWN size travels with the button, so the jump can frame
        # it instead of teleporting to a fixed magnification that lands on
        # flat colour.
        f'data-w="{max(region.width_mm, 1e-4):.6f}" '
        f'data-h="{max(region.height_mm, 1e-4):.6f}">'
        f"{index}</button> "
        f"<b>{region.displacement_mm * 1000:.2f} &micro;m</b> moved"
        f'<span class="ctx"> &middot; widest '
        f"{region.widest_mm * 1000:.1f} &micro;m &middot; "
        f"{region.pixels:,} px</span></li>"
    )


def _region_list(layer) -> str:
    """Regions worst first, in two groups: the real ones, then the rounding.

    Displacement leads each line because it is what decides. The soft
    group's explanation sits on its heading, said once -
    on every line it was noise, and forty copies of a sentence is not
    emphasis. The list scrolls rather than running off the page.
    """
    if not layer.regions:
        return ""
    limit = layer.displacement_threshold_mm
    hard_ids = set(id(region) for region in layer.hard_regions)
    numbered = list(enumerate(layer.regions, 1))

    blocks = ""
    hard = [(i, r) for i, r in numbered if id(r) in hard_ids]
    if hard:
        blocks += (
            f'<div class="regiongroup">{len(hard)} wider than one pixel</div>'
            '<ul class="regions">'
            + "".join(
                _region_entry(
                    region, index,
                    limit is not None and region.displacement_mm > limit,
                    soft=False,
                )
                for index, region in hard
            )
            + "</ul>"
        )

    soft = [(i, r) for i, r in numbered if id(r) not in hard_ids]
    if soft:
        blocks += (
            f'<div class="regiongroup soft">{len(soft)} no wider than one '
            f"pixel &mdash; most likely the drawing's own rounding rather "
            f"than anything that moved</div>"
            '<ul class="regions">'
            + "".join(
                _region_entry(
                    region, index,
                    limit is not None and region.displacement_mm > limit,
                    soft=True,
                )
                for index, region in soft
            )
            + "</ul>"
        )
    return blocks


_COLUMNS = (
    ("layer", ""),
    ("moved &micro;m",
     "How far this layer's artwork moved at the worst single place. This is "
     "what decides the layer."),
    ("widest &micro;m", "The thickest single point of any difference."),
    ("places", "Separate places where the two differ, worst first."),
    ("differing pixels",
     "Given for scale, not as the answer: this grows simply with how long an "
     "edge is."),
    ("differing mm&sup2;",
     "The same, as an area. Also scales with edge length."),
    ("only expected mm&sup2;", "Covered on the expected panel and not on the "
                               "built one."),
    ("only built mm&sup2;", "Covered on the built panel and not on the "
                            "expected one."),
    ("expected mm&sup2;", "Total area this layer covers on the expected "
                          "panel."),
    ("built mm&sup2;", "Total area this layer covers on the built panel."),
    ("carrier mm&sup2;",
     "Of the built panel's artwork, how much belongs to the carrier - the "
     "rails, the tooling holes, the panel's own edge. The board it was built "
     "from cannot account for it, so it is measured and named and never "
     "judged."),
    ("share", "Differing pixels as a share of everything this layer covers."),
    ("result", "Judged on how far the artwork moved at the worst place."),
)


def _summary_table(run: DiffRun) -> str:
    """Every layer, displacement first. Scrolls; never truncates.

    A 49-layer board makes this table longer than a screen, and the picture
    matters more than the table, so the table is capped and scrolls inside its
    own box. Capping what is *shown* is presentation; the rows are all still
    there, because the comparison never collapses its data.
    """
    rows = []
    for layer in run.result.layers:
        if not layer.judged:
            verdict = (
                "panel-owned - information only" if layer.unjudged_reason
                else "information only"
            )
        elif layer.over_threshold:
            verdict = "MOVED TOO FAR"
        else:
            verdict = "matches"
        # Keyed off INK, not off whether a gerber file exists. A layer both
        # sides left blank used to be announced as "only in the built panel"
        # purely because the panel's export wrote an empty file for it, which
        # named a difference that was not there.
        presence = ""
        if layer.ink_pixels_a and not layer.ink_pixels_b:
            presence = (f' <span class="only">content only in '
                        f"{html.escape(run.label_a)}</span>")
        elif layer.ink_pixels_b and not layer.ink_pixels_a:
            presence = (f' <span class="only">content only in '
                        f"{html.escape(run.label_b)}</span>")
        if layer.has_carrier:
            presence += (f' <span class="carrier">'
                         f"{html.escape(layer.carrier_reason)}</span>")
        moved = (
            f"{layer.worst_displacement_mm * 1000:.2f}"
            if layer.regions else "-"
        )
        widest = f"{layer.widest_mm * 1000:.1f}" if layer.regions else "-"
        carrier_area = (
            f"{layer.carrier_ink_area_mm2:.3f}" if layer.has_carrier else "-"
        )
        rows.append(
            f'<tr class="{"bad" if layer.over_threshold else "ok"}'
            f'{"" if layer.judged else " unjudged"}">'
            f'<td><a href="#{_slug(run.name, layer.layer)}" '
            f'data-layer="{html.escape(layer.layer, quote=True)}">'
            f"{html.escape(layer.layer)}</a>{presence}</td>"
            f'<td class="num key">{moved}</td>'
            f'<td class="num">{widest}</td>'
            f'<td class="num">{layer.region_count}</td>'
            f'<td class="num">{layer.pixels_differing}</td>'
            f'<td class="num">{layer.area_differing_mm2:.4f}</td>'
            f'<td class="num">{layer.area_only_in_a_mm2:.4f}</td>'
            f'<td class="num">{layer.area_only_in_b_mm2:.4f}</td>'
            f'<td class="num">{layer.ink_area_a_mm2:.3f}</td>'
            f'<td class="num">{layer.ink_area_b_mm2:.3f}</td>'
            f'<td class="num">{carrier_area}</td>'
            f'<td class="num">{layer.share_of_ink * 100:.4f}%</td>'
            f"<td>{verdict}</td></tr>"
        )
    head = "".join(
        f'<th title="{html.escape(hint)}">{name}</th>' for name, hint in _COLUMNS
    )
    return (
        "<div class='tablebox'>"
        f"<table class='summary'><thead><tr>{head}</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _legend(run: DiffRun) -> str:
    return (
        '<ul class="legend">'
        '<li><span class="sw" style="background:#808080"></span>'
        "grey - the two agree</li>"
        '<li><span class="sw" style="background:#ff8080"></span>'
        f"red - only on the {html.escape(run.label_a)} panel</li>"
        '<li><span class="sw" style="background:#80ffff"></span>'
        f"cyan - only on the {html.escape(run.label_b)}</li>"
        '<li><span class="sw" style="background:#ffffff"></span>'
        "white - bare on both</li>"
        + (
            '<li><span class="sw" style="background:#80ffff"></span>'
            "the cyan round the boards is the carrier - rails, holes and the "
            "panel's own edge. No board can account for it, so it is named "
            "and measured but never judged.</li>"
            if run.result.carrier_layers else ""
        )
        + "</ul>"
    )


def _supporting_panes(run: DiffRun, layer) -> str:
    panes = []
    for label, key in (
        (run.label_a, "a"),
        (run.label_b, "b"),
        ("the differences alone", "diff"),
    ):
        uri = run.images.get(layer.layer, {}).get(key)
        if uri is None:
            continue
        panes.append(
            f'<figure class="pane"><figcaption>{html.escape(label)}</figcaption>'
            f'<div class="viewport"><img src="{uri}" '
            f'alt="{html.escape(label)} {html.escape(layer.layer)}"></div>'
            f"</figure>"
        )
    if not panes:
        return ""
    return (
        "<details><summary>show each panel on its own, and the differences "
        "alone</summary>"
        f'<div class="panes">{"".join(panes)}</div></details>'
    )


def _carrier_note(layer) -> str:
    """What the panel's carrier put on this layer, said in the reader's terms.

    Named, sized and placed on the layer it belongs to. A number withheld from
    the verdict needs a sentence beside it - the reader has to be able to see
    what was set aside and satisfy themselves it is the rails and not their
    board.
    """
    if not layer.has_carrier:
        return ""
    note = (
        f'<p class="carriernote"><b>{html.escape(layer.carrier_reason)}'
        f"</b> &mdash; {layer.carrier_ink_area_mm2:.3f} mm&sup2; of this "
        f"layer belongs to the panel itself and is outside every board. It is "
        f"drawn here, it is measured, and it decides nothing: the board it "
        f"was built from has no such artwork to be held against."
    )
    if layer.carrier_area_differing_mm2 > layer.carrier_ink_area_mm2:
        note += (
            f" {layer.carrier_area_differing_mm2:.3f} mm&sup2; of difference "
            f"was set aside in total, the rest of it where the carrier reaches "
            f"into the millimetre of margin allowed round each board."
        )
    note += "</p>"
    if layer.carrier_leaked:
        note += (
            f'<p class="carrierleak"><b>This should not be possible.</b> '
            f"{layer.carrier.area_only_expected_mm2:.4f} mm&sup2; of the "
            f"board's own artwork is missing from outside every board area. "
            f"The expected panel holds boards and nothing else, so artwork of "
            f"its own out there means a board's artwork landed outside the "
            f"area that stands for it. It is judged, not excused.</p>"
        )
    return note


def _layer_section(run: DiffRun, layer) -> str:
    """One layer: the picture and its list of differences, side by side.

    Both fit in one screen height together, because comparing them is the
    whole job - the image says where, the list says how far, and reading one
    while scrolling for the other is what made the old layout tiring. The
    viewer's box is resizable, so an awkward board shape is the user's to fix
    rather than the page's to guess.
    """
    state = "bad" if layer.over_threshold else "ok"
    limit = layer.displacement_threshold_mm
    if layer.regions:
        against = (
            f", against a {limit * 1000:.0f} &micro;m limit"
            if limit is not None else ""
        )
        verdict = (
            (layer.unjudged_reason
             or "shown for information only - this layer is not part of the "
                "bare board")
            if not layer.judged
            else (
                f"TOO FAR{against}" if layer.over_threshold
                else f"within tolerance{against}"
            )
        )
        headline = (
            f"moved <b>{layer.worst_displacement_mm * 1000:.2f} &micro;m</b> "
            f"at the worst of {layer.region_count} place(s), widest point "
            f"{layer.widest_mm * 1000:.1f} &micro;m &mdash; {verdict} "
            f"&middot; {layer.pixels_differing:,} differing pixels, "
            f"{layer.area_differing_mm2:.4f} mm&sup2; in total"
        )
    else:
        headline = (
            f"identical &middot; {layer.ink_area_a_mm2:.3f} mm&sup2; of this "
            f"layer covered"
        )
    drawn = run.vectors.get(layer.layer)
    blend = run.images.get(layer.layer, {}).get("blend")
    if drawn is not None:
        inner = drawn.markup
        # Either side alone, and the measured differences alone: the evidence
        # behind a disclosure, as three ways of looking at the drawing already
        # on screen rather than three more pictures to embed.
        note = " &middot; " + vector.view_controls()
    elif blend:
        # This layer outgrew the vector cap and is drawn as pixels instead.
        # Said on the layer itself, because it is the one place the difference
        # in sharpness will be noticed.
        inner = (f'<img src="{blend}" '
                 f'alt="overlay of {html.escape(layer.layer)}">')
        note = (' &middot; <span class="asraster">this layer is too dense to '
                "draw as lines, so it is shown as an image and blurs when "
                "magnified</span>")
    else:
        inner = None
        note = ""
    primary = (
        f'<div class="viewbox"><div class="viewport primary'
        f'{" vector" if drawn is not None else ""}">{inner}</div></div>'
        f'<div class="viewhint">scroll over the picture to zoom &middot; '
        f"drag to pan &middot; <b>double-click resets the view</b> "
        f"&middot; drag the bottom edge to resize{note}</div>"
        if inner
        else ""
    )
    # data-layer is how the page shows one layer at a time.
    return (
        f'<section class="layer {state}" id="{_slug(run.name, layer.layer)}" '
        f'data-layer="{html.escape(layer.layer, quote=True)}">'
        f"<h3>{html.escape(layer.layer)} "
        f'<span class="headline">{headline}</span></h3>'
        f"{_carrier_note(layer)}"
        f'<div class="split"><div class="viewcol">{primary}</div>'
        f'<div class="listcol">{_region_list(layer)}</div></div>'
        f"{_supporting_panes(run, layer)}</section>"
    )


def _named_carriers(layers) -> list:
    """One layer per distinct carrier reason, in layer order.

    Six copper layers all carrying the same rail pour is one sentence, not
    six. The full list is on every layer's own section and in the table.
    """
    seen = set()
    named = []
    for layer in layers:
        if layer.carrier_reason not in seen:
            seen.add(layer.carrier_reason)
            named.append(layer)
    return named


def _banner(run: DiffRun) -> str:
    result = run.result
    worst = max(
        (layer.worst_displacement_mm for layer in result.layers), default=0.0
    )
    limit = next(
        (layer.displacement_threshold_mm for layer in result.layers
         if layer.displacement_threshold_mm is not None), None
    )
    allowance = (
        f"nothing moved further than {limit * 1000:.0f} &micro;m"
        if limit is not None else f"allowance {result.threshold} pixels"
    )
    carried = result.carrier_layers
    carrier_line = (
        f' The panel also carries {result.carrier_ink_area_mm2:.1f} '
        f"mm&sup2; of its own artwork &mdash; "
        + ", ".join(
            html.escape(layer.carrier_reason.removeprefix("carrier artwork: "))
            for layer in _named_carriers(carried)
        )
        + " &mdash; on "
        f"{len(carried)} layer(s). That is shown and measured and judges "
        "nothing: it belongs to the panel, not to any board."
        if carried else ""
    )
    if result.clean:
        return (
            f'<div class="banner ok">The panel matches the board &mdash; '
            f"{allowance}."
            f'<span class="sub">{len(result.layers)} layers checked at '
            f"{result.um_per_pixel:g} &micro;m per pixel. The furthest "
            f"anything moved anywhere on the panel: <b>{worst * 1000:.2f} "
            f"&micro;m</b>, across {result.pixels_differing:,} differing "
            "pixels. How far things moved is what decides; the pixel counts "
            "are there for scale. Both the expected panel and the built one "
            "were drawn on the same grid, so identical artwork lands on "
            f"identical pixels.{carrier_line}</span></div>"
        )
    names = ", ".join(
        html.escape(layer.layer) for layer in result.layers_over_threshold
    )
    return (
        '<div class="banner bad">ARTWORK MOVED &mdash; '
        f"{len(result.layers_over_threshold)} layer(s) beyond the allowance: "
        f"{names}"
        f'<span class="sub">The furthest anything moved: <b>{worst * 1000:.2f} '
        f"&micro;m</b> ({allowance}), across {result.pixels_differing:,} "
        f"differing pixels at {result.um_per_pixel:g} &micro;m per pixel. "
        "This page reports; it does not stop anything. The panel files were "
        f"written and are unchanged.{carrier_line}</span></div>"
    )


def _run_section(run: DiffRun) -> str:
    result = run.result
    canvas = result.canvas
    aspect = canvas.height_px / canvas.width_px
    meta = (
        "<div class='meta'>"
        f"checked area {canvas.bounds.width_mm:.3f} &times; "
        f"{canvas.bounds.height_mm:.3f} mm, drawn at "
        f"{canvas.um_per_pixel:g} &micro;m per pixel "
        f"({canvas.width_px} &times; {canvas.height_px} pixels) &middot; "
        f"differences grouped within {result.region_cell_mm:g} mm &middot; "
        f"checked in {result.seconds:.0f} s"
        "</div>"
    )
    sections = "".join(_layer_section(run, layer) for layer in result.layers)
    # The full table lives at the FOOT of the page: it is reference, consulted
    # after the picture rather than read before it.
    return (
        f'<section class="run" id="{_slug(run.name)}" '
        f'data-aspect="{aspect:.9f}" '
        f'data-minx="{canvas.bounds.min_x_mm:.6f}" '
        f'data-maxy="{canvas.bounds.max_y_mm:.6f}" '
        f'data-widthmm="{canvas.bounds.width_mm:.6f}" '
        f'data-heightmm="{canvas.bounds.height_mm:.6f}">'
        f"<h2>{html.escape(run.name)}</h2>"
        f"<p class='desc'>{html.escape(run.description)}</p>"
        f"{meta}{_banner(run)}{_legend(run)}"
        f"{sections}"
        f"<h3 class='tabletitle'>Every layer, in full</h3>"
        f"{_summary_table(run)}</section>"
    )


DIFF_CSS = """
.diffroot { color-scheme: light; padding: 1.2rem 1.4rem 4rem;
            font: 14px/1.45 "Segoe UI", system-ui, sans-serif; color: #0f172a;
            background: #f1f5f9; }
.diffroot * { box-sizing: border-box; }
.diffroot h1 { font-size: 1.4rem; margin: 0 0 .2rem; }
.diffroot h2 { font-size: 1.15rem; margin: 0 0 .2rem; }
.diffroot h3 { font-size: 1rem; margin: 0 0 .5rem; }
.diffroot p.desc { margin: 0 0 .5rem; color: #334155; }
.diffroot .lede { color: #475569; font-size: .85rem; margin: 0 0 1.2rem;
                  max-width: 62rem; }
.diffroot .meta { color: #475569; font-size: .8rem; margin-bottom: .9rem; }
.diffroot .meta code { background: #e2e8f0; padding: 1px 4px;
                       border-radius: 3px; }
.diffroot section.run { background: #f8fafc; border: 1px solid #94a3b8;
                        border-radius: 8px; padding: 1rem 1.1rem;
                        margin-bottom: 1.6rem; }
.diffroot .banner { padding: .85rem 1rem; border-radius: 6px; margin: 0 0 1.1rem;
                    font-weight: 700; letter-spacing: .01em; }
.diffroot .banner.bad { background: #dc2626; color: #fff;
                        box-shadow: 0 0 0 4px #fecaca; }
.diffroot .banner.ok  { background: #dcfce7; color: #14532d;
                        box-shadow: 0 0 0 3px #bbf7d0; }
.diffroot .banner .sub { display: block; font-weight: 400; margin-top: .35rem;
                         font-size: .85rem; opacity: .95; }
/* Capped and scrolling: 49 layers is longer than a screen, and the picture
   above must not be pushed out of view. Every row is still present. */
.diffroot .tabletitle { margin-top: 1.6rem; }
.diffroot .tablebox { max-height: 24rem; overflow: auto; margin-bottom: 1.3rem;
                      border: 1px solid #cbd5e1; border-radius: 5px;
                      background: #fff; }
.diffroot table.summary { border-collapse: collapse; background: #fff;
                          font-size: .85rem; width: 100%; }
.diffroot .tablebox thead th { position: sticky; top: 0; z-index: 1; }
.diffroot table.summary td.key { font-weight: 700; background: #f1f5f9; }
.diffroot tr.bad td.key { background: #fecaca; }
.diffroot table.summary th, .diffroot table.summary td {
    border: 1px solid #cbd5e1; padding: .3rem .6rem; text-align: left; }
.diffroot table.summary th { background: #e2e8f0; }
.diffroot table.summary td.num { text-align: right;
                                 font-variant-numeric: tabular-nums; }
.diffroot table.summary tr.bad td { background: #fee2e2; font-weight: 600; }
/* Measured, reported, not judged: greyed so it reads as information rather
   than as a pass. */
.diffroot table.summary tr.unjudged td { color: #64748b; font-style: italic; }
.diffroot table.summary tr.unjudged td.key { background: #e2e8f0;
                                             font-weight: 400; }
.diffroot .only { color: #b45309; font-weight: 600; }
/* Carrier artwork: the panel's own, named and set aside from the verdict. Its
   own colour so a reader can tell "this belongs to the panel" from "this is a
   difference" at a glance. */
.diffroot .carrier { color: #0369a1; font-weight: 600; }
.diffroot .carriernote { margin: 0 0 .6rem; padding: .5rem .7rem;
                         background: #e0f2fe; border-left: 4px solid #0284c7;
                         border-radius: 4px; color: #075985;
                         font-size: .84rem; }
.diffroot .carrierleak { margin: 0 0 .6rem; padding: .5rem .7rem;
                         background: #fee2e2; border-left: 4px solid #dc2626;
                         border-radius: 4px; color: #7f1d1d;
                         font-size: .84rem; }
.diffroot section.layer { background: #fff; border: 1px solid #cbd5e1;
                          border-radius: 6px; padding: .7rem .9rem .9rem;
                          margin-bottom: .8rem; }
.diffroot section.layer.bad { border-color: #dc2626; border-width: 2px; }
.diffroot section.layer.bad h3 { color: #b91c1c; }
.diffroot .headline { font-weight: 400; font-size: .82rem; color: #475569; }
.diffroot section.layer.bad .headline { color: #b91c1c; font-weight: 600; }
/* Picture and list side by side, both inside one screen height, so neither
   has to be scrolled away to read the other. The list matches the viewer's
   height and scrolls internally. */
.diffroot .split { display: flex; gap: .8rem; align-items: flex-start; }
.diffroot .viewcol { flex: 3 1 0; min-width: 0; }
/* The COLUMN scrolls, not each list: there are two of them now - the real
   differences and the rounding - and a height on each would stack two
   screenfuls. */
.diffroot .listcol { flex: 2 1 0; min-width: 0; height: 52vh;
                     min-height: 220px; overflow: auto;
                     border: 1px solid #e2e8f0; border-radius: 5px;
                     background: #f8fafc; padding: .45rem .6rem; }
/* The user sizes this. `resize` gives a native drag handle on the bottom
   edge; the height is a starting point, not a decision. */
.diffroot .viewbox { height: 52vh; min-height: 220px; resize: vertical;
                     overflow: hidden; border-radius: 4px;
                     display: flex; }
.diffroot .viewbox > .viewport { flex: 1; height: 100%; }
.diffroot .viewhint { font-size: .74rem; color: #64748b; margin-top: .3rem; }
.diffroot .asraster { color: #b45309; }
/* A vector picture fills its box itself, so it needs no white underneath. */
.diffroot .viewport.vector { background: none; }
.diffroot ul.regions { margin: 0; padding: 0 0 0 1.3rem;
                       font-size: .8rem; color: #334155; }
.diffroot ul.regions li { margin-bottom: .15rem; }
.diffroot ul.regions li.over b { color: #b91c1c; }
.diffroot ul.regions li.under b { color: #0f172a; }
.diffroot ul.regions .ctx { color: #64748b; }
/* Two classes, told apart at a glance. A difference no wider than a single
   measuring pixel is the drawing's own rounding, so it recedes; anything
   wider is a real shape and is called out. */
.diffroot ul.regions li.soft { color: #94a3b8; }
.diffroot ul.regions li.soft b { color: #64748b; font-weight: 400; }
.diffroot ul.regions li.hard { color: #0f172a; }
.diffroot .hardmark { background: #e8a13a; color: #422006; font-size: .7rem;
                      border-radius: 3px; padding: 0 .3rem; font-weight: 700; }
/* The soft group's explanation lives on its heading, said once. */
.diffroot .regiongroup { font-size: .74rem; color: #334155; font-weight: 600;
                         margin: .5rem 0 .25rem; }
.diffroot .regiongroup:first-child { margin-top: 0; }
.diffroot .regiongroup.soft { color: #64748b; font-weight: 400; }
.diffroot button.jump { font: inherit; font-size: .78rem; cursor: pointer;
                        border: 1px solid #94a3b8; background: #f1f5f9;
                        border-radius: 3px; padding: 0 .35rem; }
.diffroot button.jump:hover { background: #e2e8f0; }
.diffroot ul.legend { list-style: none; display: flex; flex-wrap: wrap;
                      gap: 1rem; margin: 0 0 1rem; padding: .45rem .7rem;
                      background: #fff; border: 1px solid #cbd5e1;
                      border-radius: 5px; font-size: .8rem; }
.diffroot ul.legend li { display: flex; align-items: center; gap: .4rem; }
.diffroot .sw { width: 1.05rem; height: 1.05rem; border: 1px solid #94a3b8;
                border-radius: 3px; display: inline-block; }
.diffroot details { margin-top: .7rem; }
.diffroot details summary { cursor: pointer; font-size: .8rem; color: #475569; }
.diffroot .panes { display: flex; gap: .6rem; margin-top: .5rem; }
.diffroot .pane { flex: 1 1 0; margin: 0; min-width: 0; }
.diffroot .pane figcaption { font-size: .78rem; color: #475569;
                             margin-bottom: .25rem; }
.diffroot .viewport { position: relative; overflow: hidden; background: #fff;
                      border: 1px solid #cbd5e1; cursor: grab;
                      touch-action: none; user-select: none;
                      -webkit-user-select: none; }
.diffroot .viewport.primary { border-color: #64748b; border-width: 2px;
                              border-radius: 4px; }
.diffroot .viewport.dragging { cursor: grabbing; }
/* pointer-events off: every handler is bound above the image, and letting a
   press land on the image lets the browser start a native image drag, which
   swallows pointermove and freezes panning. */
.diffroot .viewport img { position: absolute; left: 0; top: 0; width: 100%;
                          transform-origin: 0 0; image-rendering: pixelated;
                          pointer-events: none;
                          user-select: none; -webkit-user-drag: none; }
.diffroot .hint { position: fixed; right: .8rem; bottom: .8rem;
                  background: #0f172a; color: #e2e8f0; font-size: .74rem;
                  padding: .4rem .6rem; border-radius: 5px; opacity: .88; }
.diffroot .empty { background: #fff; border: 1px solid #cbd5e1;
                   border-radius: 6px; padding: 1rem 1.1rem; }
.diffroot .empty pre { white-space: pre-wrap; font: 12px ui-monospace,monospace;
                       color: #b91c1c; margin: .6rem 0 0; }
/* One layer at a time: what a host page asks for when it drives the report
   from its own layer panel. The standalone page never sets `single`. */
.diffroot.single section.layer { display: none; }
.diffroot.single section.layer.shown { display: block; }
"""

DIFF_JS = """
(function () {
  var root = document.querySelector('.diffroot');
  if (!root) { return; }

  function runOf(el) { return el.closest('section.run'); }

  // ------------------------------------------------------------------
  // A vector viewer. Pan and zoom drive the SVG's own viewBox rather than a
  // CSS transform, so the picture is re-drawn at every magnification instead
  // of being a scaled-up copy of one. Zooming into a difference to judge it
  // is the whole reason this page exists, so it has to stay sharp there.
  // ------------------------------------------------------------------
  function vectorViewer(vp) {
    var svgs = Array.prototype.slice.call(vp.querySelectorAll('svg'));
    if (!svgs.length || !svgs[0].dataset.base) { return null; }
    var base = svgs[0].dataset.base.split(' ').map(Number);
    var view = base.slice();

    function apply() {
      var box = view.map(function (n) { return n.toFixed(5); }).join(' ');
      svgs.forEach(function (el) { el.setAttribute('viewBox', box); });
    }
    // preserveAspectRatio letterboxes the drawing inside the box, so the
    // offsets have to be taken out before a cursor position means anything.
    function metrics() {
      var r = vp.getBoundingClientRect();
      var scale = Math.min(r.width / view[2], r.height / view[3]);
      return { r: r, scale: scale,
               ox: (r.width - view[2] * scale) / 2,
               oy: (r.height - view[3] * scale) / 2 };
    }
    function toUser(cx, cy) {
      var m = metrics();
      return [view[0] + (cx - m.r.left - m.ox) / m.scale,
              view[1] + (cy - m.r.top - m.oy) / m.scale];
    }
    function zoomAt(k, cx, cy) {
      var ratio = view[2] * k / base[2];
      if (ratio > 1 || ratio < 0.0008) { return; }
      var u = toUser(cx, cy);
      var m = metrics();
      view = [u[0] - (cx - m.r.left - m.ox) / (m.scale / k),
              u[1] - (cy - m.r.top - m.oy) / (m.scale / k),
              view[2] * k, view[3] * k];
      apply();
    }
    return {
      element: vp,
      reset: function () { view = base.slice(); apply(); },
      zoomAt: zoomAt,
      panBy: function (dx, dy) {
        var m = metrics();
        view[0] -= dx / m.scale;
        view[1] -= dy / m.scale;
        apply();
      },
      // Frame a rectangle of the document: the difference fills about a third
      // of the box, so there is context around what is being judged.
      frame: function (x, y, w, h) {
        var want = Math.max(w, h) * 3;
        want = Math.min(Math.max(want, base[2] * 0.0008), base[2]);
        var tall = want * base[3] / base[2];
        view = [x - want / 2, y - tall / 2, want, tall];
        apply();
      },
      refresh: apply
    };
  }

  // ------------------------------------------------------------------
  // A raster viewer, for any layer too dense to draw as lines. Same gestures,
  // a CSS transform underneath because there is only a bitmap to move.
  // ------------------------------------------------------------------
  function rasterViewer(vp) {
    var img = vp.querySelector('img');
    if (!img) { return null; }
    var aspect = parseFloat(runOf(vp).dataset.aspect);
    var view = { z: 1, tx: 0, ty: 0 };

    function apply() {
      img.style.transform = 'translate(' + view.tx + 'px,' + view.ty
                          + 'px) scale(' + view.z + ')';
    }
    function fitZoom() {
      var w = vp.clientWidth, h = vp.clientHeight;
      return (!w || !h) ? 1 : Math.min(1, h / (w * aspect));
    }
    function clamp() {
      var w = vp.clientWidth * view.z, h = vp.clientWidth * aspect * view.z;
      view.tx = (w <= vp.clientWidth) ? (vp.clientWidth - w) / 2
              : Math.min(0, Math.max(vp.clientWidth - w, view.tx));
      view.ty = (h <= vp.clientHeight) ? (vp.clientHeight - h) / 2
              : Math.min(0, Math.max(vp.clientHeight - h, view.ty));
    }
    function reset() {
      view.z = fitZoom(); view.tx = 0; view.ty = 0; clamp(); apply();
    }
    return {
      element: vp,
      reset: reset,
      // k multiplies the view's EXTENT, as it does for the vector viewer
      // beside this one - see eedesignpanels.pagezoom. This viewer keeps a
      // MAGNIFICATION, which is the reciprocal, so it divides. Multiplying
      // here turns the wheel backwards on every raster viewport of this page.
      zoomAt: function (k, cx, cy) {
        var r = vp.getBoundingClientRect();
        var px = cx - r.left, py = cy - r.top;
        var next = Math.min(400, Math.max(fitZoom() * 0.25, view.z / k));
        var f = next / view.z;
        view.tx = px - (px - view.tx) * f;
        view.ty = py - (py - view.ty) * f;
        view.z = next;
        clamp(); apply();
      },
      panBy: function (dx, dy) {
        view.tx += dx; view.ty += dy; clamp(); apply();
      },
      frame: function (x, y, w, h, run) {
        var u = (x - parseFloat(run.dataset.minx))
              / parseFloat(run.dataset.widthmm);
        var v = (parseFloat(run.dataset.maxy) - y)
              / parseFloat(run.dataset.heightmm);
        view.z = Math.min(120, Math.max(fitZoom(),
                 0.33 * parseFloat(run.dataset.widthmm) / Math.max(w, 1e-4)));
        view.tx = vp.clientWidth / 2 - u * vp.clientWidth * view.z;
        view.ty = vp.clientHeight / 2 - v * vp.clientWidth * aspect * view.z;
        clamp(); apply();
      },
      refresh: function () { clamp(); apply(); }
    };
  }

  var viewers = [];
  Array.prototype.forEach.call(root.querySelectorAll('.viewport'),
    function (vp) {
      var viewer = vp.classList.contains('vector')
                 ? vectorViewer(vp) : rasterViewer(vp);
      if (!viewer) { return; }
      viewers.push(viewer);

      // A plain wheel over the picture zooms, the way it does in KiCad. No
      // modifier: that is the convention this audience already has. Only over
      // the picture, and the default is suppressed only when a zoom actually
      // happens, so a wheel anywhere else scrolls the page as usual.
      // One factor, one meaning, both viewers - see eedesignpanels.pagezoom.
      vp.addEventListener('wheel', function (e) {
        if (!vp.clientWidth) { return; }
        e.preventDefault();
        // Anchor inside the picture: a coordinate outside it slings the
        // content away, and at high magnification the view goes blank.
        var r = vp.getBoundingClientRect();
        var ax = Math.min(Math.max(e.clientX, r.left), r.right);
        var ay = Math.min(Math.max(e.clientY, r.top), r.bottom);
        viewer.zoomAt(__WHEEL_ZOOM__, ax, ay);
      }, { passive: false });

      var last = null;
      // Suppress the browser's own drag: a press landing on a selection
      // starts a native drag, which eats pointermove and freezes panning.
      vp.addEventListener('dragstart', function (e) { e.preventDefault(); });
      vp.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) { return; }
        e.preventDefault();
        last = [e.clientX, e.clientY];
        vp.classList.add('dragging');
        vp.setPointerCapture(e.pointerId);
      });
      vp.addEventListener('pointermove', function (e) {
        if (!last) { return; }
        viewer.panBy(e.clientX - last[0], e.clientY - last[1]);
        last = [e.clientX, e.clientY];
      });
      function stop(e) {
        last = null;
        vp.classList.remove('dragging');
        if (vp.hasPointerCapture && vp.hasPointerCapture(e.pointerId)) {
          vp.releasePointerCapture(e.pointerId);
        }
      }
      vp.addEventListener('pointerup', stop);
      vp.addEventListener('pointercancel', stop);
      vp.addEventListener('dblclick', function () { viewer.reset(); });
      viewer.reset();
    });

  function viewerIn(section) {
    var vp = section.querySelector('.viewport.primary')
          || section.querySelector('.viewport');
    for (var i = 0; i < viewers.length; i++) {
      if (viewers[i].element === vp) { return viewers[i]; }
    }
    return null;
  }

  Array.prototype.forEach.call(root.querySelectorAll('button.jump'),
    function (btn) {
      btn.addEventListener('click', function () {
        var run = runOf(btn);
        var section = btn.closest('section.layer');
        var viewer = viewerIn(section);
        if (!viewer) { return; }
        var x = parseFloat(btn.dataset.x);
        var y = parseFloat(btn.dataset.y);
        var w = parseFloat(btn.dataset.w);
        var h = parseFloat(btn.dataset.h);
        // The buttons carry gerber coordinates; the drawing has already been
        // flipped, so y is measured down from the top of the window.
        var minY = parseFloat(run.dataset.maxy)
                 - parseFloat(run.dataset.heightmm);
        viewer.frame(x, minY + (parseFloat(run.dataset.maxy) - y), w, h, run);
        // Scroll only when the picture is not already on screen, and never
        // smoothly. A smooth scroll glides the picture out from under the
        // cursor after the click, so the thing you are about to point at
        // moves while you are pointing at it.
        var seen = viewer.element.getBoundingClientRect();
        var showing = Math.min(seen.bottom, window.innerHeight)
                    - Math.max(seen.top, 0);
        if (showing < seen.height * 0.9) {
          section.scrollIntoView({ block: 'nearest', behavior: 'instant' });
        }
        btn.blur();
      });
    });

  function refreshAll() {
    viewers.forEach(function (viewer) { viewer.refresh(); });
  }
  // A picture inside a closed disclosure, or a layer that was hidden, has no
  // width until it is shown, so it has to be told when that happened.
  Array.prototype.forEach.call(root.querySelectorAll('details'),
    function (d) { d.addEventListener('toggle', refreshAll); });
  window.addEventListener('resize', refreshAll);
  window.__diffResize = refreshAll;
})();
""".replace(
    "__WHEEL_ZOOM__", wheel_factor_js("e.deltaY", DIFF_STEP)
)



# The layer a page opens on, matching the shared panel's own default.
INITIAL_LAYER = "Edge_Cuts"


def _reveal_first(body: str) -> str:
    """Mark the first layer section visible in the MARKUP, not from script.

    A page opened fresh - mailed to somebody, opened from a file, opened with
    nothing remembered - has to show a layer immediately. Leaving that to the
    script means a page that greets a new reader with an empty viewer if
    anything about the script's start-up goes wrong. The script still moves
    the selection; it does not have to create it.
    """
    marker = '<section class="layer '
    # Prefer the layer the script settles on, so a fresh open does not show
    # one layer and then swap to another the instant the script runs.
    at = -1
    outline = body.find(f'data-layer="{INITIAL_LAYER}"')
    if outline >= 0:
        at = body.rfind(marker, 0, outline)
    if at < 0:
        at = body.find(marker)
    if at < 0:
        return body
    end = body.index('"', at + len(marker))
    return body[:end] + " shown" + body[end:]


def diff_body(
    runs, title: str = "", lede: str = "", single_layer: bool = False
) -> str:
    """The report itself, scoped to its own `.diffroot`.

    With ``single_layer`` the per-layer sections start hidden and the page
    shows one at a time by adding ``shown`` to the ``section.layer`` whose
    ``data-layer`` it wants; the summary table and the banner stay visible
    throughout, because presentation may narrow what is *displayed* and never
    what was *measured*. After changing which section is shown,
    ``window.__diffResize()`` has to run: a viewport that was hidden had no
    width to size itself against.

    Raises:
        ValueError: when handed no runs. An empty report would render blank,
            and a blank report reads as "nothing was wrong".
    """
    runs = list(runs)
    if not runs:
        raise ValueError("a comparison report needs at least one run")
    heading = f"<h1>{html.escape(title)}</h1>" if title else ""
    lede_html = f"<p class=lede>{html.escape(lede)}</p>" if lede else ""
    sections = "".join(_run_section(run) for run in runs)
    if single_layer:
        sections = _reveal_first(sections)
    return (
        f"<div class='diffroot{' single' if single_layer else ''}'>"
        f"{heading}{lede_html}"
        + sections
        + "<div class='hint'>scroll over the picture to zoom &middot; "
        "drag = pan &middot; double-click resets the view</div>"
        "</div>"
    )


# Glue between the shared layer panel and the report: the panel owns the
# selection, this decides what a selection shows.
_PANEL_JS = r"""
const sections = [...document.querySelectorAll('.diffroot section.layer')];
const nothing = document.querySelector('.nodiff');
const diff = layerPanel('lp-diff', {
  eyes: false,
  onChange: panel => {
    const key = LAYERS[panel.active].key;
    let found = false;
    sections.forEach(section => {
      const on = section.dataset.layer === key;
      section.classList.toggle('shown', on);
      if (on) found = true;
    });
    if (nothing) {
      nothing.hidden = found;
      nothing.textContent = LAYERS[panel.active].display +
        ' was not part of this comparison, so there is nothing to show for it.';
    }
    // A viewport that was hidden had no width to size itself against.
    if (found && window.__diffResize) window.__diffResize();
  },
});
document.querySelectorAll('.diffroot table.summary a[data-layer]').forEach(a => {
  a.onclick = event => { event.preventDefault(); diff.select(a.dataset.layer); };
});
addEventListener('keydown', event => {
  if (event.target.tagName === 'INPUT') return;
  if (diff.key(event)) event.preventDefault();
});
diff.paint();
"""

_PAGE_CSS = """
body{margin:0;background:#14161a}
body.withpanel{display:flex;gap:1rem;padding:1rem;height:100vh;
               align-items:flex-start}
body.withpanel .diffmain{flex:1;min-width:0;height:100%;overflow:auto;
                         border-radius:8px}
body.withpanel .diffroot{min-height:100%}
.nodiff{padding:1.2rem 1.4rem;background:#f1f5f9;color:#334155;
        font:14px/1.45 "Segoe UI",system-ui,sans-serif}
"""


def compared_specs(result: CompareResult, layers) -> list:
    """The layers the comparison actually covered, in board order.

    The panel lists these and nothing else. A layer with no comparison has
    nothing to select and no number to show, so it is absent rather than
    greyed: a row that cannot be clicked is furniture.
    """
    covered = {layer.layer for layer in result.layers}
    return [spec for spec in layers if spec.svg_suffix in covered]


def status_marks(result: CompareResult, layers) -> dict:
    """Per-layer marks for the panel, keyed by canonical name.

    A layer measured but deliberately not judged is ``info``, not ``ok`` - it
    has numbers, and none of them were allowed to decide anything. A layer the
    comparison never covered would be ``none``, but those are filtered out of
    the panel by :func:`compared_specs` before they get here.

    ``watch`` sits between: inside the allowance, but holding a difference
    wider than a single measuring pixel. That is a real shape rather than the
    instrument's own rounding, so the panel shows its presence without
    opening the layer.
    """
    by_key = {layer.layer: layer for layer in result.layers}
    marks = {}
    for spec in layers:
        measured = by_key.get(spec.svg_suffix)
        if measured is None:
            marks[spec.canonical_name] = layerpanel.STATUS_NONE
        elif measured.over_threshold:
            marks[spec.canonical_name] = layerpanel.STATUS_BAD
        elif measured.has_hard_regions:
            # Ahead of "not judged" deliberately: a difference wider than a
            # measuring pixel is a real shape, and its presence is worth
            # seeing even on a layer whose result decides nothing.
            marks[spec.canonical_name] = layerpanel.STATUS_WATCH
        elif not measured.judged:
            marks[spec.canonical_name] = layerpanel.STATUS_INFO
        else:
            marks[spec.canonical_name] = layerpanel.STATUS_OK
    return marks


def differing_counts(result: CompareResult, layers) -> dict:
    """Per-layer differing-pixel counts for the panel, by canonical name."""
    by_key = {layer.layer: layer for layer in result.layers}
    return {
        spec.canonical_name: by_key[spec.svg_suffix].pixels_differing
        for spec in layers
        if spec.svg_suffix in by_key
    }


def build_diff_page(
    runs,
    out_path: Path,
    title: str = "Gerber comparison",
    lede: str = "",
    layers=None,
    status_by_layer: dict = None,
    counts_by_layer: dict = None,
    extra_css: str = "",
) -> Path:
    """Write one self-contained page covering every run handed to it.

    With ``layers`` the page carries the shared layer panel and shows one
    layer's comparison at a time; ``status_by_layer`` supplies the pass/fail
    marks and is usually :func:`status_marks` of the run's own result. Without
    ``layers`` every section is listed at once, which is what a
    comparison-only run wants.
    """
    if layers is None:
        document = (
            "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            f"<style>{_PAGE_CSS}{DIFF_CSS}{extra_css}</style></head><body>"
            f"{diff_body(runs, title=title, lede=lede)}"
            f"<script>{DIFF_JS}</script><script>{vector.JS}</script>"
            f"</body></html>"
        )
    else:
        panel = layerpanel.panel_html(
            layerpanel.DIFF_PANEL,
            layerpanel.rows_html(
                layers,
                status_by_layer=dict(status_by_layer or {}),
                counts_by_layer=dict(counts_by_layer or {}),
            ),
        )
        document = (
            "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            f"<style>{_PAGE_CSS}{DIFF_CSS}{extra_css}"
            f"{layerpanel.PANEL_CSS}</style>"
            "</head><body class=withpanel>"
            "<div class=diffmain>"
            f"{diff_body(runs, title=title, lede=lede, single_layer=True)}"
            "<div class=nodiff hidden></div></div>"
            f"{panel}"
            f"<script>{DIFF_JS}</script>"
            f"<script>{layerpanel.panel_script(layers)}</script>"
            f"<script>{vector.JS}</script>"
            f"<script>{_PANEL_JS}</script></body></html>"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    return out_path
