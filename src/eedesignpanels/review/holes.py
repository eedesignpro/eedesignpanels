"""The holes, lifted out of kicad-cli's own exports and re-stacked on top.

A hole is the one thing on a board that belongs to no layer, and the PCB
editor draws it accordingly: last, over everything. The review viewer stacks
whole per-layer SVGs at equal z in board order, so any later layer's fill
paints over the marks an earlier layer carried and the holes disappear. This
module gives them their own entry, stacked above every board layer.

**Nothing here draws a hole.** kicad-cli already plotted them - with
`--drill-shape-opt` defaulting to 2, "actual shape" - and what this module does
is find its drawing in the export and re-emit it verbatim in a document of its
own. Only the SVG wrapper is written by us, exactly as in
:mod:`eedesignpanels.review.overlay`; not one drawn coordinate is touched.

What the two colours mean, from KiCad's own source (10.0.3 clone,
`pcbnew/plot_board_layers.cpp:138`): "Drill marks are plotted in white to
knockout the pad if any layers of the pad are being plotted, and in black if
the pad is not being plotted." `BRDITEMS_PLOTTER::PlotDrillMarks`
(`pcbnew/plot_brditems_plotter.cpp:1271`) is where that happens: every via on a
plotted layer is drawn WHITE, and each drilled pad is drawn BLACK unless some
plotted layer carries copper wider than the drill, in which case it switches to
WHITE. So white is a knockout of copper that is there, black is a mark with
nothing to knock out - it is NOT plated against non-plated. On the corpus panel
the 72 NPTH mousebite drills are black on every copper layer, because those
pads are exactly their own drill and there is no annulus anywhere.

That per-layer rule is also why the marks are NOT the same on every copper
layer, measured on the corpus panel: F.Cu carries 732 white marks,
B.Cu 552 and In1.Cu 1464, because the board's vias are BLIND and each is only
plotted on the layers it spans. The black marks are identical on all three. So
the marks are merged across every exported copper layer, and where one hole is
white on one layer and black on another the white wins - which is precisely
what KiCad itself would have drawn had it been given all those layers in one
plot, since its pad loop switches to white as soon as ANY plotted layer has an
annulus.

The extraction keys on the export's structure, never on a colour alone: the
drill marks are the TRAILING run of top-level groups that carry no transform,
are painted only in the two drill-mark colours, and contain nothing but flashes.
When an export is not shaped that way the marks from it are dropped and the
page says so - a wrong overlay is worse than none.

The colours, though, are NOT the plotter's. The overlay matches the EDITOR's
presentation, not the plot's: the plot draws a non-plated hole as a black void,
and the PCB editor draws it light blue. So the geometry stays kicad-cli's byte
for byte and the group's fill/stroke is re-painted from the active theme -
editor-look over plot-look, derived from the theme at page build, never
transcribed.

Which theme key paints what, read out of the 10.0.3 clone. The first line is a
trap worth stating plainly:

* `common/settings/color_settings.cpp:136` - ``CLR( "board.plated_hole",
  LAYER_NON_PLATEDHOLES )``. The json key reads "plated_hole" and paints the
  NON-plated hole. `common/layer_id.cpp:138` gives that id the editor label
  "Non-plated holes", `pcbnew/pad.cpp:2334` is where an NPTH pad asks for it,
  and `pcbnew/pcb_painter.cpp:1643` draws the drill through it. Its built-in
  default is `CSS_COLOR( 26, 196, 210 )`
  (`common/settings/builtin_color_themes.h:168`) - the light blue the editor
  shows. There is no key with "non_plated" in it anywhere; that is why one was
  never found.
* `pcbnew/pcb_painter.cpp:160` - ``m_layerColors[LAYER_PAD_PLATEDHOLES] =
  aSettings->GetColor( LAYER_PCB_BACKGROUND )``, under the comment "Colors for
  layers that aren't theme-able". A PLATED pad hole is painted the board
  BACKGROUND, so `board.pad_plated_hole` exists in the theme file and the PCB
  editor overrides it. It is not read here.
* `pcbnew/pcb_painter.cpp:1238` - `LAYER_VIA_HOLES` fills a via's drill, from
  `board.via_hole`, default `CSS_COLOR( 227, 183, 46 )` (golden). And
  `pcb_painter.cpp:295` remaps `LAYER_PAD_HOLEWALLS` to that same id - "Use via
  'golden copper' hole color for pad hole walls for contrast" - so golden is
  what the editor puts at a PLATED hole whether it is a via or a pad.
* `board.via_hole_walls` (`LAYER_VIA_HOLEWALLS`, `pcb_painter.cpp:1187`) paints
  a RING outside the drill radius. kicad-cli plots no such geometry and this
  module draws none, so the key is not used.

So the two mark kinds take two derived colours:

* a BLACK mark - a drilled pad with no copper wider than its drill on ANY
  plotted layer, which after merging the whole copper stack is a non-plated
  hole - is painted `board.plated_hole`, i.e. `LAYER_NON_PLATEDHOLES`.
* a WHITE mark is painted `board.via_hole`, i.e. `LAYER_VIA_HOLES`.

The white run holds two editor kinds at once - every via (always white, see
`PlotDrillMarks` above) and every plated pad - and they are DELIBERATELY not
split. The plot gives both the one colour, and the only thing that could tell
them apart is emission order: `SVG_PLOTTER::emitSetRGBColor` only marks the
graphics context dirty on a real change, and `PlotDrillMarks` calls
`SetColor( BLACK )` for every pad before deciding, so the first trailing group
is always the via run and no pad can join it. That rule is exact when the board
has vias and silently wrong when it has none, and there is nothing in an export
that says which. Guessing is refused on the same grounds the structure check is
made at all. Golden is the honest single answer anyway: it is the colour the
editor paints a plated hole with in both cases - the via's fill and the pad
wall's ring - and it is one derived key instead of an inference.

A key the theme does not carry falls back to the plot colour it replaces, which
makes the overlay degrade to exactly what it looked like before rather than to
something invented.

**THE NATIVE HOLE DOCUMENT DOES NOT REPLACE THIS** (checked against
eedesigncore 0.16.0). The library grew hole rendering in 0.15.0 and
the LIVE preview already takes it: `preview.render_seam.render_with_library`
asks for the `Holes.Plated` and `Holes.NonPlated` pseudo-layers and stacks the
two documents into the same `Holes.Drill` row this module names. Nothing here
became deletable by it, because this module serves the other backend:

* the FROZEN review artifact is kicad-cli's output and stays that way, because
  a view assembled by our own code can look right while the output is wrong,
  so its holes must be lifted out of its exports;
* the preview's CONTENT phase is kicad-cli for the same reason its copper is,
  so its holes come through here too.

The two roads also answer two different questions, which is why neither is a
workaround for the other. The library knows what the BOARD holds - every hole,
including a blind via no single layer shows - while this module reports what
KiCad PLOTTED, per layer, knockout rule and all. A frozen page that showed a
hole kicad-cli did not plot would be the page inventing geometry, which is the
one thing it may never do.
"""

import re
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.review.render import LayerSpec

# The two colours KiCad hardcodes for drill marks. Lowercased for comparison;
# the export's own spelling is what gets re-emitted.
KNOCKOUT_COLOR = "#ffffff"
MARK_COLOR = "#000000"
DRILL_MARK_COLORS = frozenset({KNOCKOUT_COLOR, MARK_COLOR})

# The theme keys the editor paints holes with. `plated_hole` really is the
# NON-plated one - see the module docstring and color_settings.cpp:136.
NON_PLATED_HOLE_KEY = "plated_hole"
PLATED_HOLE_KEY = "via_hole"

# The entry's name in the layer panel. Not a board layer - it takes a name no
# board can collide with, the same way the v-score proposal overlay does.
HOLES_LAYER = "Holes.Drill"
HOLES_LAYER_DISPLAY = "Holes"

_GROUP_TOKEN = re.compile(r"<g\b[^>]*>|</g>")
_STYLE = re.compile(r'style="([^"]*)"', re.S)
_PAINT = re.compile(r"\b(fill|stroke)\s*:\s*([^;\"]+)")
# What one drill mark is drawn as: a circle for a round hole, and for a slot a
# stroked path (`PSLIKE_PLOTTER::FlashPadOval` walks a thick segment).
_FLASH = re.compile(r"<(?:circle|path|rect|ellipse|polygon|polyline)\b[^>]*/>", re.S)
_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.S)
_VIEWBOX = re.compile(r'viewBox="([^"]*)"')
_DIMENSION = re.compile(r'\b(width|height)="([^"]*)"')
_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def normalize_color(value: str) -> str:
    """A colour as `#rrggbb`, whatever notation it arrived in.

    The theme writes `rgb(200, 52, 52)` and the export writes `#C83434`; the
    guard against a layer plotted in a drill-mark colour has to compare the two.
    Anything unrecognised comes back as itself, which simply will not match.
    """
    text = (value or "").strip().lower()
    match = _RGB.match(text)
    if match:
        return "#" + "".join(f"{int(part):02x}" for part in match.groups())
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        return "#" + "".join(character * 2 for character in text[1:])
    return text


def editor_palette(theme) -> dict:
    """What the PCB editor paints each drill mark with, from the active theme.

    Maps the plotter's colour to the editor's, so the overlay reads the way
    KiCad does: a black mark is a non-plated hole, a white one is a plated hole.
    A key the theme does not carry maps to itself - the mark keeps the colour
    kicad-cli gave it rather than one this module made up.
    """
    colors = getattr(theme, "colors", None) or {}

    def keyed(key: str, plotted: str) -> str:
        value = colors.get(key)
        return value if isinstance(value, str) and value.strip() else plotted

    return {
        MARK_COLOR: keyed(NON_PLATED_HOLE_KEY, MARK_COLOR),
        KNOCKOUT_COLOR: keyed(PLATED_HOLE_KEY, KNOCKOUT_COLOR),
    }


def restyle(open_tag: str, palette) -> str:
    """One group's open tag with its paint re-coloured through *palette*.

    Only `fill` and `stroke` values that ARE a drill-mark colour are touched;
    `stroke:none`, the opacities and everything else in the style survive
    untouched, and so does every coordinate inside the group.
    """
    if not palette:
        return open_tag

    def paint(match):
        replacement = palette.get(normalize_color(match.group(2)))
        if replacement is None:
            return match.group(0)
        return f"{match.group(1)}:{replacement}"

    return _PAINT.sub(paint, open_tag)


def swatch(palette) -> str:
    """The Holes row's two-colour label: plated hole over non-plated hole."""
    return (
        f"linear-gradient(135deg, {palette[KNOCKOUT_COLOR]} 50%, "
        f"{palette[MARK_COLOR]} 50%)"
    )


class Group(NamedTuple):
    """One top-level `<g>` of an export: its open tag, style and inner markup."""

    open_tag: str
    style: str
    body: str

    @property
    def paints(self) -> set:
        """The colours this group paints in, lowercased. `none` is not one."""
        return {
            normalize_color(value)
            for _, value in _PAINT.findall(self.style)
            if value.strip().lower() != "none"
        }

    @property
    def elements(self) -> list:
        """The flashes inside, verbatim and in order."""
        return [match.group(0) for match in _FLASH.finditer(self.body)]


class Frame(NamedTuple):
    """The coordinate system an export drew in: its viewBox and page size."""

    view_box: str
    width: str
    height: str


class Marks(NamedTuple):
    """The merged drill marks, ready to stack, and anything the page must say.

    ``markup`` is "" when there is nothing to show - which is the ordinary
    answer for a board with no drilled holes, and is only worth a ``notice``
    when an export could not be read the way this module expects.
    """

    markup: str
    elements: int
    sources: tuple
    notice: str


def top_level_groups(text: str):
    """Every top-level `<g>` of an SVG document, in order.

    Returns ``None`` when the groups do not nest cleanly, which means the
    document is not the flat sequence of style groups kicad-cli writes and
    nothing here should try to read it.
    """
    groups = []
    depth = 0
    start = 0
    open_tag = ""
    for token in _GROUP_TOKEN.finditer(text):
        if token.group(0).startswith("</"):
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                body = text[start + len(open_tag):token.start()]
                style_match = _STYLE.search(open_tag)
                groups.append(Group(
                    open_tag=open_tag,
                    style=style_match.group(1) if style_match else "",
                    body=body,
                ))
        else:
            if depth == 0:
                start = token.start()
                open_tag = token.group(0)
            depth += 1
    if depth != 0:
        return None
    return groups


def is_drill_group(group: Group) -> bool:
    """Is this group one of kicad-cli's drill-mark groups?

    Three things have to hold at once, and each rules out something real: a
    transform means the plotter's root state group, which is black and would
    otherwise pass; a colour outside the two hardcoded drill colours means it
    is the layer's own ink; anything other than a flash inside means it is
    board geometry rather than a run of holes.
    """
    if "transform" in group.open_tag:
        return False
    paints = group.paints
    if not paints or not paints <= DRILL_MARK_COLORS:
        return False
    return not _FLASH.sub("", group.body).strip()


def frame_of(text: str):
    """The export's viewBox and page size, or ``None`` when it has no viewBox."""
    opening = _SVG_OPEN.search(text)
    if not opening:
        return None
    box = _VIEWBOX.search(opening.group(0))
    if not box:
        return None
    sizes = dict(_DIMENSION.findall(opening.group(0)))
    return Frame(box.group(1), sizes.get("width", ""), sizes.get("height", ""))


def drill_groups(text: str, layer_color: str = ""):
    """The trailing drill-mark groups of one export, and why there are none.

    Returns ``(groups, reason)``. An empty list with an empty reason is the
    honest ordinary answer: that layer plots no holes. A reason is only given
    when the export is not shaped the way this rule was measured against, and
    it is written to be read on the page.
    """
    groups = top_level_groups(text)
    if groups is None:
        return [], "its groups do not nest the way a kicad-cli export does"
    if not groups:
        return [], "it carries no groups at all"
    if layer_color and normalize_color(layer_color) in DRILL_MARK_COLORS:
        return [], (
            f"the layer is plotted in {layer_color}, which is also a drill-mark "
            f"colour, so its own ink cannot be told from a hole"
        )

    trailing = []
    for group in reversed(groups):
        if not is_drill_group(group):
            break
        trailing.append(group)
    trailing.reverse()

    if len(trailing) == len(groups):
        # Even an empty layer keeps the plotter's transformed root group, so a
        # document that is nothing but drill marks is not one of ours.
        return [], "every group in it looks like a drill mark"
    return trailing, ""


def merge(sources, layer_colors=None, palette=None) -> Marks:
    """Merge the drill marks of several exports into one document to stack.

    *sources* is a sequence of ``(canonical layer name, path)`` in board order -
    the copper exports of ONE board. *layer_colors* maps a layer name to the
    colour it was plotted in, so a layer painted in a drill-mark colour can be
    refused rather than mistaken for holes.

    *palette* re-paints the emitted groups - :func:`editor_palette` builds one
    from the active theme. Without it the marks keep the plot's own white and
    black.
    """
    colors = layer_colors or {}
    frame = None
    refused = []
    read = []

    for name, path in sources:
        text = Path(path).read_text(encoding="utf-8")
        here = frame_of(text)
        if here is None:
            refused.append((name, "it carries no viewBox"))
            continue
        if frame is None:
            frame = here
        elif here.view_box != frame.view_box:
            refused.append((name, "it was cropped to a different area"))
            continue
        groups, reason = drill_groups(text, colors.get(name, ""))
        if reason:
            refused.append((name, reason))
            continue
        if groups:
            read.append((name, groups))

    # Which holes are knockouts anywhere: KiCad's own pad rule reads every
    # plotted layer and takes white as soon as one has an annulus. Merging
    # across the copper stack is the same question, so it gets the same answer.
    knockouts = {
        element
        for _, groups in read
        for group in groups if KNOCKOUT_COLOR in group.paints
        for element in group.elements
    }

    order = []
    by_style = {}
    seen = set()
    used = []
    for name, groups in read:
        kept_any = False
        for group in groups:
            marks_only = KNOCKOUT_COLOR not in group.paints
            # Grouped by the tag it will be WRITTEN with: the plotter emits a
            # fresh group per pad, and once they are all one editor colour they
            # are one group.
            open_tag = restyle(group.open_tag, palette)
            for element in group.elements:
                if element in seen or (marks_only and element in knockouts):
                    continue
                seen.add(element)
                if open_tag not in by_style:
                    order.append(open_tag)
                    by_style[open_tag] = []
                by_style[open_tag].append(element)
                kept_any = True
        if kept_any:
            used.append(name)

    notice = _notice(refused, bool(seen))
    if not seen or frame is None:
        return Marks("", 0, tuple(used), notice)

    body = "".join(
        f"{open_tag}\n" + "\n".join(by_style[open_tag]) + "\n</g>\n"
        for open_tag in order
        if by_style[open_tag]
    )
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{frame.width}" height="{frame.height}" '
        f'viewBox="{frame.view_box}">\n{body}</svg>\n'
    )
    return Marks(markup, len(seen), tuple(used), notice)


def _notice(refused, any_marks: bool) -> str:
    """What the PAGE says when an export's holes could not be lifted."""
    if not refused:
        return ""
    listed = "; ".join(f"{name} because {reason}" for name, reason in refused)
    if any_marks:
        return (
            f"Some holes could not be shown: they were left out of "
            f"{len(refused)} export(s) - {listed}."
        )
    return (
        f"The holes could not be shown: no export could be read for them - "
        f"{listed}."
    )


def write_beside(sources, layer_colors, out_path: Path, palette=None):
    """Merge the marks and write them beside the render. Returns (path, marks).

    The path is ``None`` when there is nothing to stack, which covers both a
    board with no holes and an export that could not be read - the two are told
    apart by ``marks.notice``, which only the second one carries.
    """
    marks = merge(sources, layer_colors, palette)
    if not marks.markup:
        return None, marks
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(marks.markup, encoding="utf-8")
    return out_path, marks


def copper_sources(layers, svg_by_layer):
    """The exported copper layers of one board, in board order.

    Copper is where KiCad plots drill marks: a paste export carries none at all
    and a silkscreen export carries none either (both measured), so the
    marks are taken from the copper stack and nowhere else.
    """
    return [
        (layer.canonical_name, svg_by_layer[layer.canonical_name])
        for layer in layers
        if layer.canonical_name.endswith(".Cu")
        and layer.canonical_name in svg_by_layer
    ]


def holes_layer(layers) -> LayerSpec:
    """The Holes row: last in the list, and stacked above every board layer."""
    return LayerSpec(
        ordinal=max((layer.ordinal for layer in layers), default=0) + 1,
        canonical_name=HOLES_LAYER,
        layer_type="holes",
        display_name=HOLES_LAYER_DISPLAY,
    )


def with_swatch(theme):
    """The theme with a swatch for the Holes row - no board layer supplies one.

    Derived from the same theme keys the marks are painted with, so the row's
    label cannot drift from what the overlay shows.
    """
    key = HOLES_LAYER.lower().replace(".", "_")
    return theme._replace(
        colors={**theme.colors, key: swatch(editor_palette(theme))}
    )
