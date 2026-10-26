"""The active KiCad colour theme, so review swatches match the PCB editor.

Read from the user's settings, never transcribed: the theme name comes from
`pcbnew.json`, the colours from `colors/<theme>.json`. A missing or unreadable
theme degrades to neutral defaults rather than failing a render - the colours
are decoration, and losing them must not cost the user a review cycle.

Colour keys are the canonical layer name lowercased with dots as underscores
(`F.SilkS` -> `f_silks`), except copper, which lives in a `copper` sub-object
keyed `f`, `b`, `in1`..`in30`.
"""

import json
import re
from pathlib import Path
from typing import NamedTuple

from eedesigncore import default_user_settings_dir

FALLBACK_LAYER_COLOR = "rgb(160, 160, 160)"
FALLBACK_BACKGROUND = "rgb(0, 16, 35)"
DEFAULT_THEME_NAME = "user"

# THE ALARM COLOUR, and it is deliberately not from the theme: a layer painted
# this is one the run says must not be cut, and
# that must not read as somebody's colour preference. Opaque, and near enough to
# pure red that no KiCad default theme colour is mistaken for it.
ALARM_COLOR = "rgb(255, 32, 32)"

_RGBA = re.compile(
    r"rgba\(\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*[0-9.]+\s*\)", re.I
)


def opaque(color: str) -> str:
    """*color* with its alpha channel dropped: `rgba(r,g,b,a)` -> `rgb(r,g,b)`.

    **Alpha is applied ONCE, by the page, to the whole layer.** A renderer
    given `rgba` paints every ITEM at that alpha, so two subsegments of one
    toolpath that overlap composite twice and the overlap reads darker than the
    rest of the same layer - visible on the cutter path. Handing the renderer
    an opaque colour and letting the viewer
    set the layer group's opacity flattens the layer first and blends it once,
    which is how the frozen review pages have always drawn it.

    Anything that is not an `rgba(...)` string comes back unchanged.
    """
    found = _RGBA.match(color.strip()) if isinstance(color, str) else None
    if not found:
        return color
    red, green, blue = (part.strip() for part in found.groups())
    return f"rgb({red}, {green}, {blue})"


class Theme(NamedTuple):
    """A resolved colour theme: its name, its layer colours, its background."""

    name: str
    colors: dict
    background: str

    def layer_color(self, canonical_name: str) -> str:
        """Swatch colour for a canonical layer name."""
        if canonical_name.endswith(".Cu"):
            key = canonical_name[:-3].lower()
            copper = self.colors.get("copper")
            if isinstance(copper, dict):
                return copper.get(key, FALLBACK_LAYER_COLOR)
            return FALLBACK_LAYER_COLOR
        key = canonical_name.lower().replace(".", "_")
        value = self.colors.get(key, FALLBACK_LAYER_COLOR)
        return value if isinstance(value, str) else FALLBACK_LAYER_COLOR

    def layer_alpha(self, canonical_name: str) -> float:
        """Alpha of the layer's theme colour, 1.0 when opaque.

        kicad-cli drops the theme's alpha channel when it exports SVG
        (measured: board `user_36` rgba(...,0.702) rendered
        opacity 1), so the viewer applies it per layer instead.
        """
        match = re.match(
            r"rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\s*\)",
            self.layer_color(canonical_name),
        )
        return float(match.group(1)) if match else 1.0


def settings_dir() -> Path:
    """KiCad's per-user settings folder on this machine.

    eedesigncore's `default_user_settings_dir` follows KiCad's own rule,
    `KICAD_CONFIG_HOME` first, and is what `eedesignpanels.fplib` uses too, so
    the theme and the footprint tables are read from one folder.
    """
    return default_user_settings_dir()


def active_theme_name(settings: Path | None = None) -> str:
    """Theme selected in the PCB editor, else the first theme file, else user.

    `pcbnew.json` is only written when KiCad exits, so this reads whatever the
    last session saved.
    """
    root = settings or settings_dir()
    pcbnew_json = root / "pcbnew.json"
    if pcbnew_json.is_file():
        try:
            data = json.loads(pcbnew_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        name = data.get("appearance", {}).get("color_theme")
        if isinstance(name, str) and (root / "colors" / f"{name}.json").is_file():
            return name

    available = sorted((root / "colors").glob("*.json"))
    return available[0].stem if available else DEFAULT_THEME_NAME


def load_theme(name: str | None = None, settings: Path | None = None) -> Theme:
    """Load a colour theme by name, defaulting to the active one."""
    root = settings or settings_dir()
    theme_name = name or active_theme_name(root)
    path = root / "colors" / f"{theme_name}.json"

    board: dict = {}
    if path.is_file():
        try:
            board = json.loads(path.read_text(encoding="utf-8")).get("board", {})
        except (OSError, json.JSONDecodeError):
            board = {}
    if not isinstance(board, dict):
        board = {}

    background = board.get("background", FALLBACK_BACKGROUND)
    if not isinstance(background, str):
        background = FALLBACK_BACKGROUND
    return Theme(name=theme_name, colors=board, background=background)
