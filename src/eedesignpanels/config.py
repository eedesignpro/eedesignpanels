"""Load and validate ``panel/panel.json`` - revision 1.

The config is the second half of "same input, same output": a run is
reproducible by handing someone this file. Validation is hard and early, so a
bad config is a clear message and no output rather than a broken panel.

```json
{
  "revision": 1,
  "written_by": "eedesignpanels 0.1.0",
  "source_board": "../board.kicad_pcb",
  "stage": "build_panel",
  "grid": { "columns": 2, "rows": 3, "gap_mm": 3.0,
            "gap_x_mm": null, "gap_y_mm": null,
            "rotation_deg": 0, "alternate": "none",
            "nest_across_mm": 0.0, "nest_down_mm": 0.0,
            "clearance_warning_mm": 0.0, "placement": null },
  "min_tab_mm": 0.5,
  "eliminate_slivers": true,
  "outline_heal_mm": 0.01,
  "pocket_cutouts": false,
  "panel": {
    "rails": 5.0,
    "tooling_holes": { "enabled": true, "diameter_mm": 3.175 },
    "scores": { "enabled": true,
                "rails_horizontal": true, "rail_setback_horizontal_mm": 0.0,
                "jump_vertical_mm": 0.0,
                "rails_vertical": false, "rail_setback_vertical_mm": 0.0,
                "jump_horizontal_mm": 0.0,
                "inter_row": false, "inter_column": true },
    "rail_copper": { "enabled": true, "clearance_mm": 0.635 },
    "fiducials": { "enabled": true,
                   "footprint": "Fiducial_1mm_Mask2mm", "offset_mm": 5.0,
                   "direction": "along_top_and_bottom_rails",
                   "side": "F.Cu" },
    "dimensions": { "enabled": true, "layer": "User.16" }
  },
  "exclude_layers": ["User.Drawings", "User.41"],
  "review": { "layers": "all" },
  "verify": { "enabled": true, "um_per_pixel": 5.0 }
}
```

**THE CONFIG IS EXPLICIT.** Every setting is always written and each group
carries its own `enabled`. Absence is not how anything is said: a missing
section would change the panel in a way no reader of the file could see. A
group switched off adds nothing to the panel, to the byte.

**THE FILE SAYS WHICH REVISION IT IS.** The shape above is revision 1. A file
from any other revision is named as older or as newer
and the launch goes on with standard settings, untouched
(:func:`revision_problem`, :func:`load_or_create`).

``stage`` defaults to ``passthrough`` so a config written before the geometry
stage existed still means what it meant.

``min_tab_mm`` defaults to :data:`mintab.MIN_TAB_DEFAULT_MM` and is refused
below :data:`mintab.MIN_TAB_FLOOR_MM` - a value the fab cannot hold is a
config error rather than a number quietly raised to something else.
``eliminate_slivers`` sits beside it and says whether the router takes what
falls under it; the two travel together as one rule (:class:`mintab.Rule`).

``outline_heal_mm`` is the largest gap between two board-outline ends the tool
will close. It defaults to KiCad's own figure,
:data:`toolpath.CHAINING_EPSILON_MM`, so the tool keeps accepting every board
KiCad accepts; zero refuses any gap at all, and every gap it does close is
announced by coordinate and distance.

``pocket_cutouts`` says whether a cutout wider than the cutter is milled out or
only walked round. False is the default, because most board houses do their
own CAM work on cutouts: the wall is routed, the middle drops out as a loose
slug, and the report names it.
True adds concentric passes that clear the middle. Top level beside the minimum
tab and the heal, because it is one fabrication rule about the SOURCE BOARD's
own cutouts and a rule stated twice can disagree with itself.
"""

import json
from pathlib import Path
from typing import Literal, NamedTuple, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from eedesignpanels import __version__, fplib
from eedesignpanels.mintab import MIN_TAB_DEFAULT_MM, Rule, checked_min_tab
from eedesignpanels.placement import ROTATIONS
from eedesignpanels.toolpath import CHAINING_EPSILON_MM

StageName = Literal["passthrough", "build_panel"]
Alternation = Literal["none", "rows", "columns"]

# Layers whose content describes the DESIGN at drawing scale - the fabrication
# drawing frame, its dimensions, its DXF underlays, its notes. Repeating them
# once per instance gives six overlapping copies of one drawing, so they are not
# replicated. The layers themselves stay in the panel's layer table.
#
# The default set, and a starting point rather than a law: which layers a project
# uses for drawing-scale work varies by project, which is why this is a config
# key. An entry may be a canonical name (`User.41`) or the display name the
# KiCad Appearance panel shows (`User.Drawings`) - the latter is what an
# engineer reads off the screen, so both resolve.
DEFAULT_EXCLUDED_LAYERS = (
    "User.Drawings",
    "User.Comments",
    "User.14",
    "User.15",
    "User.16",
    "User.41",
    "User.42",
)

# HOW FAR EITHER SIDE OF A SCORE LINE THE COPPER KEEP-OUT REACHES, when nobody
# said. Stated here because this is where the setting lives
# (`PrepConfig.vscore.rule_half_width_mm`), and said
# a second time in `stages.panel_scores.DEFAULT_RULE_HALF_WIDTH_MM` because that
# stage runs for a config that names no prep at all. The two are fenced against
# each other in the tests.
SCORE_RULE_HALF_WIDTH_DEFAULT_MM = 0.3

# THE LARGEST OUTLINE GAP THE TOOL WILL CLOSE, when nobody said. KiCad's own
# figure (`pcbnew/board.h:86-88`, 10.0.3, quoted in full at
# `toolpath.CHAINING_EPSILON_MM`), because departing from it would refuse boards
# KiCad accepts.
OUTLINE_HEAL_DEFAULT_MM = CHAINING_EPSILON_MM

# AND THE MOST A HAND-EDITED CONFIG MAY ASK FOR. A config file may be typed by
# hand, so its content is untrusted input, and a millimetre of missing outline
# is not a gap - it
# is an edge nobody drew. Refused by name beats healing a hole in the board.
MAX_OUTLINE_HEAL_MM = 1.0

# HOW FAR A JUMP-SCORED SAW PASS MAY STOP SHORT OF THE PANEL EDGE, at most.
# A bound rather than a judgement: a config file may be hand-edited, so its
# content is untrusted input, and no fabrication panel is a metre across, so a
# number bigger than
# this is a typo and is refused by name instead of silently deleting a saw pass.
MAX_JUMP_MM = 1000.0

# HOW MUCH PLAIN MATERIAL MAY STAND BETWEEN A RAIL SCORE AND THE BOARD ARRAY,
# at most. The same bound and the same reason as
# the jump: the setback GROWS THE PANEL by twice this on one axis, and a metre
# of it is a typo rather than a fabrication number. The rail's own width is
# never eaten by it, so there is nothing else for a bound to guard.
MAX_SETBACK_MM = 1000.0

# HOW TALL THE PANEL'S DIMENSION LETTERING MAY BE ASKED TO BE. The lettering
# height is a setting on the panel page. A bound rather than a judgement, and
# the same reason as the
# two above it: the derived size is the panel's longest extent over 50, which is
# 8 mm on the 400 mm envelope maximum, so a number this far past it is a typed
# mistake. Nothing stops a stated height anywhere below it.
MAX_TEXT_HEIGHT_MM = 50.0
MAX_EDGE_PLATING_MASK_WIDTH_MM = 10.0


class ConfigError(Exception):
    """The config is missing, unreadable, or invalid. Nothing was written."""


# ---------------------------------------------------------------------------
# WHICH SETTINGS THESE ARE, WRITTEN IN THE FILE. Without a revision in the
# file, a config written by an older tool fails by complaining about a FIELD -
# which reads as a
# broken file rather than an old one. Once engineers hold their own configs
# that difference is the whole diagnosis.
#
# An integer, because it is COMPARED and nothing else: older, newer, or this
# one. No migration exists, so the number only has to answer which side of
# this tool a file came from.
# ---------------------------------------------------------------------------
CONFIG_REVISION = 1

# The first two keys of every `panel.json`, in this order. First so the first
# thing a person opening the file reads is which tool's settings these are.
REVISION_KEY = "revision"
WRITTEN_BY_KEY = "written_by"

# ...and what the second one says. INFORMATIONAL AND NEVER COMPARED: the
# revision decides whether a file loads, and this line
# exists purely so a problem report says where the file came from. Judging it
# would tie a settings file to a release, which the revision deliberately does
# not do.
WRITTEN_BY = f"eedesignpanels {__version__}"


class ConfigRevisionError(ConfigError):
    """The file's revision is not this tool's. It is old, or it is new.

    Told apart from every other `ConfigError` because the ANSWER is different:
    a file this tool cannot read is a problem
    with the file, and a file from another revision is a problem with neither -
    it is a fact about which tool wrote it, and the sentence has to say that.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        # The plain sentence, ready for the page. The message carries the path
        # as well, which a person reading a warning does not need twice.
        self.reason = reason


class PlacementRowConfig(BaseModel):
    """One copy of a PLACEMENT FILE: two offsets from the design, and a turn.

    Millimetres from the source board's own outline rectangle, so `0, 0, 0` is
    the design exactly where it sits. The angle is a quarter turn, because only
    a quarter turn is exact: any other angle puts every coordinate through a
    sine and a cosine, and the copies stop landing on the same grid.
    """

    model_config = ConfigDict(extra="forbid")

    across_mm: float
    down_mm: float
    angle_deg: float = 0.0

    @field_validator("angle_deg")
    @classmethod
    def _quarter_turns_only(cls, value: float) -> float:
        if value % 90.0 != 0.0:
            raise ValueError(
                f"a placement turns by a quarter turn - 0, 90, 180 or 270 "
                f"degrees - not {value:g}. Only those are exact: KiCad rotates "
                f"a cardinal angle with integer arithmetic and tessellates a "
                f"rectangle into a polygon for any other."
            )
        return value % 360.0

    @property
    def triple(self) -> tuple:
        return (self.across_mm, self.down_mm, self.angle_deg)


class PlacementFileConfig(BaseModel):
    """The placement file's ROWS, held in the config.

    **The rows live here, not a path.** "Same config, same panel" means the
    config alone reproduces the panel, and a config pointing at a CSV elsewhere
    on a machine builds a different panel the day that file moves. The CSV is an
    input FORM: the page posts its bytes, `eedesignpanels.placementfile` turns
    them into rows, and the rows are written here. `source` is the file's name,
    kept for the record and never opened.

    While this section is present the pattern controls are not used: the file
    says where every copy goes and how it is turned. `columns` and `rows` stay
    live, because the grid CELL is still every copy's identity - the uuids are
    derived from it - so the file carries exactly `columns x rows`
    rows, in row-major order.
    """

    model_config = ConfigDict(extra="forbid")

    rows: list[PlacementRowConfig] = Field(min_length=1)
    source: str = ""

    @property
    def triples(self) -> tuple:
        return tuple(row.triple for row in self.rows)


class GridConfig(BaseModel):
    """Array of board instances the panel is built from, and how each is turned.

    `gap_mm` is the clear space between neighbouring instances and is the
    ONE number a config needs. `gap_x_mm` and `gap_y_mm` override it per
    axis, because the two directions are not the same thing on a panel: the
    across gap carries the routed channel between columns and the down gap
    carries the one between rows, and a fab may hold different figures for
    them. Omitted, each takes `gap_mm` - so every config written before they
    existed builds exactly the panel it built before.

    **THE PLACEMENT SUITE** is the rest of this
    section, and every one of its settings defaults to off or zero: a config
    written before it existed builds the panel it always built, to the byte
    (`tests/test_placement_suite.py`).

    * `rotation_deg` - 0, 90, 180 or 270, the base angle every copy takes. Text
      KiCad holds upright is resolved on the copy BEFORE the turn, so every copy
      is a true translation and rotation of the source
      (:mod:`eedesignpanels.stages.upright`);
    * `alternate` - `none`, `rows` or `columns`: a further 180 degrees on every
      other row or column, so a shaped board interlocks with its neighbour;
    * `nest_across_mm` / `nest_down_mm` - signed millimetres, applied to the
      ALTERNATED copies alone. Nothing is alternated when alternation is off, so
      these then move nothing and the build says so;
    * `clearance_warning_mm` - zero disables. The viewport MARKS any two board
      rectangles closer than this. It is a measurement over a panel that already
      exists: it moves no coordinate, it is not board data, and changing it
      rebuilds nothing (fenced byte for byte);
    * `placement` - the rows of a placement FILE. Present means the file says
      where every copy goes and the pattern settings above are not used; the
      build names them rather than silently ignoring them. See
      :class:`PlacementFileConfig`.

    `mirror` is still refused: mirroring needs a layer-flipping transform
    `eedesigncore` does not expose, and a config that asked for one and got an
    unmirrored panel would be a wrong panel nobody was told about.
    """

    model_config = ConfigDict(extra="forbid")

    columns: int = Field(ge=1)
    rows: int = Field(ge=1)
    gap_mm: float = Field(ge=0.0)
    gap_x_mm: Union[float, None] = Field(default=None, ge=0.0)
    gap_y_mm: Union[float, None] = Field(default=None, ge=0.0)
    rotation_deg: float = 0.0
    alternate: Alternation = "none"
    # SIGNED, and deliberately unbounded in sign: which way a shape nests is
    # the shape's business, and a user who needs to pull a row up types a
    # negative number rather than looking for a second control.
    nest_across_mm: float = 0.0
    nest_down_mm: float = 0.0
    clearance_warning_mm: float = Field(default=0.0, ge=0.0)
    placement: Union[PlacementFileConfig, None] = None
    # Modelled so it can be REJECTED by name. Left unmodelled it would be
    # rejected too - extra="forbid" - but with a message that says nothing
    # about why, and silence about why is how wrong panels get built.
    mirror: bool = False

    @field_validator("rotation_deg")
    @classmethod
    def _quarter_turns_only(cls, value: float) -> float:
        if value not in ROTATIONS:
            raise ValueError(
                f"grid.rotation_deg is one of "
                f"{', '.join(f'{turn:g}' for turn in ROTATIONS)} - not "
                f"{value:g}. Only a quarter turn is exact: KiCad rotates a "
                f"cardinal angle with integer swaps and no trigonometry "
                f"(trigo.cpp:229-264), and it tessellates a rectangle into a "
                f"polygon for any other angle (eda_shape.cpp:957-972), which "
                f"this builder does not reproduce."
            )
        return float(value)

    @property
    def turns(self) -> bool:
        """Does anything in this arrangement take a turn?

        Every copy of a placement file included, because a file may turn a copy
        the pattern settings never would.
        """
        if self.placement is not None:
            return any(row.angle_deg % 360.0 for row in self.placement.rows)
        return bool(self.rotation_deg % 360.0) or self.alternate != "none"

    @property
    def nest_shift_mm(self) -> tuple:
        """`(across, down)` - what a nest shift moves the alternated copies by."""
        return (self.nest_across_mm, self.nest_down_mm)

    @property
    def unused_by_the_placement_file(self) -> tuple:
        """Which pattern settings a live placement file is overriding.

        Named rather than ignored: a config that states two arrangements has to
        say which one won, or the file and the picture disagree with nobody
        told. It warns and does not block the build.
        """
        if self.placement is None:
            return ()
        stated = []
        if self.rotation_deg % 360.0:
            stated.append(f"grid.rotation_deg = {self.rotation_deg:g}")
        if self.alternate != "none":
            stated.append(f"grid.alternate = {self.alternate}")
        for name, value in (("nest_across_mm", self.nest_across_mm),
                            ("nest_down_mm", self.nest_down_mm)):
            if value:
                stated.append(f"grid.{name} = {value:g}")
        return tuple(stated)

    @property
    def gap_across_mm(self) -> float:
        """The clear space between two columns."""
        return self.gap_mm if self.gap_x_mm is None else self.gap_x_mm

    @property
    def gap_down_mm(self) -> float:
        """The clear space between two rows."""
        return self.gap_mm if self.gap_y_mm is None else self.gap_y_mm

    @property
    def summary(self) -> str:
        """How this grid reads in a report, in millimetres and plain words.

        One place, because three consumers say it: the run report, the review
        page's subtitle and the panel page. Two gaps are named separately only
        when they differ, so the ordinary panel reads the way it always did -
        and so does an arrangement that turns nothing, which says nothing about
        turns.
        """
        if self.placement is not None:
            return (
                f"{self.columns} across x {self.rows} down, placed by file "
                f"({len(self.placement.rows)} placements)"
            )
        across, down = self.gap_across_mm, self.gap_down_mm
        gaps = (
            f"{across:g} mm between boards" if across == down
            else f"{across:g} mm across and {down:g} mm down between boards"
        )
        turned = ""
        if self.rotation_deg % 360.0:
            turned += f", turned {self.rotation_deg:g} degrees"
        if self.alternate != "none":
            turned += f", every other {self.alternate[:-1]} turned a half circle"
        return f"{self.columns} across x {self.rows} down, {gaps}{turned}"

    @field_validator("mirror")
    @classmethod
    def _reject_mirror(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "grid.mirror is not supported yet, so this config is rejected "
                "rather than silently built unmirrored. Mirroring needs a "
                "layer-flipping transform that eedesigncore does not expose. "
                "Remove the key or set it to false."
            )
        return value


class ReviewConfig(BaseModel):
    """What the review render covers. ``"all"`` means every board layer."""

    model_config = ConfigDict(extra="forbid")

    layers: Union[Literal["all"], list[str]] = "all"


class VScoreConfig(BaseModel):
    """V-scores: which straight board edges the saw cuts instead of the router.

    **`detect` accepts nothing.** Which candidates become scores is the
    DESIGNER's decision, and
    it lives in the board as `User.38` lines - not in this config, and not in a
    policy. This setting only says whether the v-score stage runs at all:

    * ``"auto"`` - propose every blade line the geometry permits, show the
      proposals on the review page, and validate the board's own `User.38` lines
      against them. The default, because a board whose edge a saw could cut is a
      board that has to be looked at.
    * ``"none"`` - skip the stage. No proposals are computed, no `User.38` line
      is honoured, and every edge is routed. Any score lines on the board are
      reported as ignored rather than silently overrun.

    `route_past_score_mm` is DECLARED, never inferred, for the same reason the
    cutter diameter is: it comes from the fab's blade geometry - how far the
    routed channel must overshoot the score plane so the score exits into air
    and the corner between them is cut clean - and the artwork cannot carry it.
    A board with a score the route runs into and no declared value is an error
    naming the junction, not a guess.

    **It measures the cut's TANGENT POINT**: the last point
    of the board edge taken to full depth sits this far past the score plane, so
    the centreline ends there too and the bit's round end sweeps one radius
    further into air. Measuring the outer extreme instead would leave a
    strip of uncut board edge one radius wide between the route and the score.

    **`rule_half_width_mm` IS A PREP SETTING.** It is set on the prep page. It
    is how far
    either side of a score line the copper keep-out reaches, and it sits beside
    `route_past_score_mm` because it is the same kind of number: a fact about
    the blade, declared once, and changing it is a BOARD change - accepting a
    score writes that keep-out into the source board
    (:mod:`eedesignpanels.stages.board_scores`).

    ONE NUMBER FOR THE BOARD AND THE PANEL, read everywhere through
    :attr:`PanelConfig.score_rule_half_width_mm`. The board's keep-out steps into
    the panel with its instance and the panel recognises it by MEASURING it
    (`panel_scores.uncovered_runs`), so a second width would make that
    recognition fail and the panel would guard one line twice.

    0.3 mm by default: a v-score groove and its flanking damage on a 1.6 mm
    panel is a few tenths, and DRC has to be able to say that a track laid over
    it will be cut.
    """

    model_config = ConfigDict(extra="forbid")

    detect: Literal["auto", "none"] = "auto"
    route_past_score_mm: Union[float, None] = Field(default=None, ge=0.0)
    route_along_score_mm: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    route_tangent_angle_deg: float = Field(default=10.0, ge=0.0, le=90.0, allow_inf_nan=False)
    route_end_overrides: dict[str, float] = Field(default_factory=dict)
    custom_route_ends: bool = False

    @field_validator("route_end_overrides")
    @classmethod
    def valid_route_overrides(cls, values):
        import math
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("Route end distances must be finite and nonnegative.")
        return values
    rule_half_width_mm: float = Field(
        default=SCORE_RULE_HALF_WIDTH_DEFAULT_MM, gt=0.0
    )


class PrepConfig(BaseModel):
    """Source prep: what `python -m eedesignpanels.prep` needs to know.

    `cutter_diameter` is DECLARED, never inferred from geometry. A router bit
    is a thing on a shelf, and a stage that guessed its size from the artwork
    would be guessing the one number the artwork cannot carry.

    Each tab footprint declares the widest bit it can survive in its own
    `_PANEL.ROUTE_TOOL_D_MAX` field; prep compares this value against the
    smallest of those and warns rather than refuses.
    """

    model_config = ConfigDict(extra="forbid")

    cutter_diameter: float = Field(gt=0.0)
    # Omitted means the stage still runs: a board whose edge a saw could cut is
    # a board that has to be looked at, and its own `User.38` decides.
    vscore: VScoreConfig = VScoreConfig()


class RailWidths(BaseModel):
    """The four rails, one number each, in millimetres.

    The per-side form of `panel.rails`. All four are required: a side left
    unwritten would have to mean either "as wide as the others" or "none", and a
    fabrication number nobody typed is the wrong kind of default. Zero is how a
    side is removed, said out loud.
    """

    model_config = ConfigDict(extra="forbid")

    top: float = Field(ge=0.0)
    bottom: float = Field(ge=0.0)
    left: float = Field(ge=0.0)
    right: float = Field(ge=0.0)


class ToolingCorners(BaseModel):
    """Which of the four rail corners carries a tooling hole.

    Four booleans, named the way a reader looking at the panel names them, so a
    config that fixtures on three corners says which three. All four by default:
    a fabricator who is handed a panel expects to be able to pin it down.
    """

    model_config = ConfigDict(extra="forbid")

    top_left: bool = True
    top_right: bool = True
    bottom_right: bool = True
    bottom_left: bool = True


class ToolingHolesConfig(BaseModel):
    """Non-plated holes the fabricator and the assembler fixture the panel with.

    **THIS ONE IS ON WHEN IT IS ABSENT**:
    a panel nobody can pin down is a panel the shop cannot hold, so every config
    with a `panel` section gets the four `PNLTH` holes. Like
    :class:`PanelDimensionsConfig`, the section is here to TUNE the holes or to
    decline them with `"enabled": false`, never to switch them on. Panel scores
    and rail copper are unchanged: those two still run only when named.

    `diameter_mm` defaults to 3.175 - one eighth of an inch, the size the
    fixturing pins on this shop's machines are.

    `placement` is how the four positions are derived, and it is a CHOICE
    between two rules rather than four coordinates:

    * ``"rail_corner_center"`` - the middle of the block of material standing in
      that corner of the panel, so a wider rail moves the hole with it. The
      default, because it is the position that stays right when the rails
      change.
    * ``"fixed_inset"`` - `inset_across_mm` in from the blank's left or right
      edge and `inset_down_mm` down from its top or bottom edge, which is what a
      fixture with a fixed pin pattern needs.

    The insets are read only in fixed mode; they are still validated in either,
    because a negative inset is a typo whichever mode the file is in.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    diameter_mm: float = Field(default=3.175, gt=0.0)
    corners: ToolingCorners = ToolingCorners()
    placement: Literal["rail_corner_center", "fixed_inset"] = "rail_corner_center"
    inset_across_mm: float = Field(default=5.0, ge=0.0)
    inset_down_mm: float = Field(default=5.0, ge=0.0)


class PanelScoresConfig(BaseModel):
    """V-score passes across the whole panel that sit on no board edge.

    **Switches, and no positions anywhere.** The page calls the group
    `V-Scores` and names each switch after the pass it cuts, not after the
    axis a reader has to work out:

    * `rails_horizontal` - `Rail scores, horizontal`: one pass along the inner
      edge of the TOP and the BOTTOM rail, each running across the panel;
    * `rails_vertical` - `Rail scores, vertical`: one pass along the inner edge
      of the LEFT and the RIGHT rail, each running down the panel;
    * `inter_row` - `Inter-row scores`: one pass across the middle of every gap
      between two board rows;
    * `inter_column` - `Inter-column scores`: one pass down the middle of every
      gap between two board columns.

    **THE KEY IS THE LABEL**. The two passes
    between boards were `horizontal` and `vertical` in the file while the page
    called them `Inter-row scores` and `Inter-column scores`, so the file and
    the screen named one setting two ways. The file now says what the menu
    says. No old config is converted: this project is unreleased and none is
    kept.

    **THE SECTION IS ALWAYS WRITTEN AND `enabled` IS THE SWITCH.** Absence as
    "off" would be a silent way to change the panel that no reader of the file
    can see. Every setting is stated, and `enabled: false` adds no panel score,
    to the byte.

    **ONE RAIL SWITCH IS TWO**. A single `rails`
    said "every rail that exists", which is one answer for two different cuts:
    the top and bottom passes run the long way and the left and right passes
    run the short way, and a fabricator takes them on different setups.

    HORIZONTAL BEFORE VERTICAL, everywhere, which
    is why the fields read in that order and the page lists them in that order.
    The two setbacks and the two jump distances are the exception: each one sits
    under the rail switch that governs it, which puts them the other way round.

    Where each pass goes is COMPUTED from the panel's own layout at build time
    (:func:`eedesignpanels.stages.panel_scores.plan`) - the board rectangles and
    the rail widths - so a gap that moves takes its score with it. A position
    somebody types is a position that stops being true the next time the grid
    changes, and that is exactly what happened: typed positions put the corpus
    panel's scores on the left edge of each column instead of down the gap.

    **THE DATUM IS STILL THE BLANK'S TOP-LEFT CORNER**: every score is
    REPORTED as a distance across or down from that corner, because that is the
    number a fabricator measures. It is an output now, not an input.

    `enabled: false` IS NOT "NO KEEPOUTS". The DRC rule area round
    a saw pass was never one of this section's families: every `User.38` line
    on the panel gets one, including the merged instance-edge passes that come
    from decisions living in the BOARDS, because those lines are on the panel
    whatever this section says and the rail pour must not run across one
    (`eedesignpanels.stages.panel_scores.protect`).

    The keep-out width is NOT here. It is set on the prep page. It is one
    number for the board and
    the panel, and the board is where a score is decided, so it lives at
    `prep.vscore.rule_half_width_mm` and is read through
    :attr:`PanelConfig.score_rule_half_width_mm`.

    **THE RAIL SCORE SETBACK GROWS THE PANEL. IT DOES NOT MOVE THE LINE.** A
    rail score would otherwise land on the outer edge of the board
    array, which is where the router channel already runs - the blade and the
    cutter in one place. The fix is a BUFFER ADDED TO THE BOARD: each side gains
    that much plain material, so the panel grows by twice the setback on that
    axis and the rail keeps the dimension the user typed. That dimension is
    the distance from the PANEL EDGE to the RAIL SCORE.

    Across one axis the panel reads, from the edge inwards: the panel edge, the
    rail's own width, the rail score line, the setback of plain material, the
    board array - then the setback, the score, the rail and the far edge.

    WHICH SETBACK GROWS WHICH WAY IS EASY TO SWAP, so it is said plainly. `Rail
    scores, horizontal` are the passes along the TOP AND BOTTOM rails, so
    `rail_setback_horizontal_mm` grows the panel VERTICALLY. The vertical
    setback belongs to the left and right rails and grows the panel ACROSS. A
    setback whose own rail score is off grows nothing - a greyed setting
    contributes nothing to the build. Zero, the default, adds no material, to
    the byte.

    **JUMP SCORING**. `jump_horizontal_mm` and
    `jump_vertical_mm` are two DISTANCES, and nothing else - there is no switch
    beside either, and zero means no jump. Each one stops the saw short of the
    panel edge at both ends of a pass, and the distance belongs to the LINE'S
    OWN DIRECTION: a horizontal pass is shortened by `jump_horizontal_mm` at its
    left end and its right end, a vertical pass by `jump_vertical_mm` at its top
    and its bottom. They are separable because the rails they run out into are
    separable - a wide top rail and a narrow left rail want different answers.

    **A GREYED SETTING CONTRIBUTES NOTHING TO THE BUILD.** Four settings here
    are DEPENDANTS: each setback and each jump
    hangs off one rail score. A vertical saw line ends at the top rail and at
    the bottom rail, and the score along those two rails is the HORIZONTAL rail
    score, so `jump_vertical_mm` is governed by `rails_horizontal`;
    `jump_horizontal_mm` hangs off `rails_vertical` for the mirror reason. Each
    setback is governed by the rail score it moves. With its governor off a
    dependant's effective value is ZERO whatever the file says, the page greys
    it, and the build reaches the same answer -
    :attr:`eedesignpanels.stages.panel_scores.Settings.governed` is the one place
    that decides it. The model still stores the number the user typed: a
    file is refused rather than quietly rewritten, and the run's report names
    every dependant it ignored.

    **A JUMP SHORTENS EVERY SCORE EXCEPT A RAIL SCORE.** A jump distance
    shortens every score line that runs in its
    direction, at both ends: the inter-row passes, the inter-column passes, and
    the lines the boards decided. A RAIL score is never shortened by a jump - it
    runs along the rim the jump exists to keep whole.

    THE KEEP-OUT FOLLOWS THE SHORTENED LINE: the guard belongs to the cut, so
    the trim
    runs before the rule areas are drawn and the copper is guarded over exactly
    the saw's run - and then a keep-out's width past each end of it.

    **A SHARED LINE HAS TO BE TOLD WHICH IT IS.** With a rail score on and its
    setback zero, the rail's line can land exactly on a line the BOARDS decided
    - one saw pass, two meanings. As a RAIL score it runs the full span and no
    jump shortens it; as a BOARD-EDGE score its direction's jump stops it short
    at both ends, and a panel cut that way may not come apart.
    THE TOOL CANNOT TELL WHICH MEANING IS WANTED: both are legitimate cuts, and
    nothing in the board or the config distinguishes them - so the user owns
    the answer. `shared_line_is_rail_score_horizontal` and
    `shared_line_is_rail_score_vertical` are one per rail score, each a
    dependant of it, TRUE by default because full span with no jump is what lets
    a panel come apart. Either means nothing unless the two lines are actually
    collinear, which the build detects at the tolerance it already uses for "on
    this line"; the run's report says how many lines were shared and which way
    each was treated, so a saw line never changes meaning silently.
    """

    model_config = ConfigDict(extra="forbid")

    # THE SECTION'S OWN SWITCH. Off adds no panel score, to the byte - and it
    # is still written, because an absent section would say nothing visible.
    enabled: bool = False
    # IN THE ORDER THE PAGE LISTS THEM, so a saved
    # file reads down the screen: each rail score, then the setback and the jump
    # it governs, then the two passes between boards. Horizontal before vertical
    # throughout, except where a dependant has to sit under its own governor.
    rails_horizontal: bool = False
    # THE SETBACK GROWS THE PANEL: it is a buffer added to the board on each
    # side, so the
    # rail keeps its typed dimension - panel edge to rail score - and plain
    # material stands between the score and the board array, keeping the blade
    # and the router bit apart. The HORIZONTAL setback belongs to the top and
    # bottom rail passes and grows the panel VERTICALLY. Bounded above for the
    # reason the jump is.
    rail_setback_horizontal_mm: float = Field(
        default=0.0, ge=0.0, le=MAX_SETBACK_MM
    )
    # THE TWO JUMP DISTANCES. Bounded above because a config may be hand-edited
    # and its content is untrusted input, and a panel a metre across is not one
    # anybody scores; the
    # stage refuses a distance that would leave no saw pass at all and says so.
    # A DISTANCE IS THE WHOLE SETTING: zero is no
    # jump, and there is no switch beside it saying the same thing twice.
    jump_vertical_mm: float = Field(default=0.0, ge=0.0, le=MAX_JUMP_MM)
    # WHAT A SHARED SAW LINE IS. When this rail
    # score lands on a line the boards decided, one cut carries two meanings and
    # THE TOOL CANNOT TELL WHICH IS WANTED - both are real cuts and nothing in
    # the board says which - so the user decides. True is the rail score:
    # full span, no jump, which is what lets a panel come apart. False is the
    # board-edge score, shortened by its direction's jump. Means nothing unless
    # the two lines are collinear.
    shared_line_is_rail_score_horizontal: bool = True
    rails_vertical: bool = False
    rail_setback_vertical_mm: float = Field(
        default=0.0, ge=0.0, le=MAX_SETBACK_MM
    )
    jump_horizontal_mm: float = Field(default=0.0, ge=0.0, le=MAX_JUMP_MM)
    # The same choice for the left and right rail passes, and for the same
    # reason: the tool cannot know which meaning a
    # shared line has, so the user owns it per rail score.
    shared_line_is_rail_score_vertical: bool = True
    inter_row: bool = False
    inter_column: bool = False


class FiducialCorners(BaseModel):
    """Which of the four rail corners carries a panel fiducial.

    THREE BY DEFAULT, and the fourth off: three checked gives an orientation
    pattern. Four targets in a rectangle look the same
    with the panel turned 180 degrees; three do not, so the shipped answer is
    the one that tells a placement machine which way round the panel went in.
    """

    model_config = ConfigDict(extra="forbid")

    top_left: bool = True
    top_right: bool = True
    bottom_right: bool = True
    bottom_left: bool = False


class FiducialsConfig(BaseModel):
    """Panel-level optical targets, placed as real footprints on the rails.

    **Opt-in**: absent means no panel fiducials and a panel
    byte-identical to the one built before this stage existed, exactly like
    `scores` and `rail_copper`. The switch IS the section.

    `footprint` is a footprint NAME, and `library` is the library nickname it
    lives in - the two together are what KiCad calls an FPID. The name is not
    validated against a list here, because the list is a property of the machine
    the build runs on: :mod:`eedesignpanels.fplib` resolves the library through
    KiCad's own `fp-lib-table` and the stage refuses by name when the footprint
    is not in it. What IS validated is the SHAPE of the name, because a config
    is hand-editable, so its content is untrusted input, and a footprint name
    reaches the file system.

    `library_path` pins the library to one directory and skips KiCad's tables
    altogether. That is the headless escape hatch - a CI runner has no KiCad and
    no settings - and it is read INSTEAD of the tables, never as a fallback,
    because a silent fallback would let two machines resolve one config
    differently.

    `offset_mm` is measured FROM THE TOOLING HOLE at that corner and always runs
    inward; `direction` says which pair of rails it runs along. The holes are
    the datum because they are what the fixture locates the panel by, so a rail
    that changes width moves the holes and the fiducials together.

    `side` names the COPPER LAYER, in KiCad's own words: `F.Cu`, `B.Cu` or
    `both`, so the config, the page and the board use one name for one layer.
    A `B.Cu` target is the
    library's front-side footprint turned over by KiCad's own `FOOTPRINT::Flip`,
    at the same panel coordinate - so `both` puts one target on each side of the
    same spot (see :mod:`eedesignpanels.stages.fiducials`). No old config is
    converted: this project is unreleased and none is kept.
    """

    model_config = ConfigDict(extra="forbid")

    # THE GROUP'S OWN SWITCH. Off places no fiducial - and the section is
    # written either way, because an absent section would say nothing visible.
    enabled: bool = False
    footprint: str = ""
    library: str = fplib.DEFAULT_FIDUCIAL_LIBRARY
    library_path: Union[str, None] = None
    offset_mm: float = Field(default=5.0, gt=0.0)
    direction: Literal[
        "along_top_and_bottom_rails", "along_left_and_right_rails"
    ] = "along_top_and_bottom_rails"
    side: Literal["F.Cu", "B.Cu", "both"] = "F.Cu"
    corners: FiducialCorners = FiducialCorners()

    @field_validator("footprint")
    @classmethod
    def _a_footprint_name_is_a_file_name(cls, value: str) -> str:
        """Refuse anything that is not a footprint name, before it is a path.

        Empty is allowed and means "not chosen yet": the group's switch writes
        the section before the user has picked from the dropdown, and the
        stage is what refuses a build with no footprint named.
        """
        if not value:
            return value
        return fplib.checked_name(value)

    @field_validator("library")
    @classmethod
    def _a_nickname_is_a_name(cls, value: str) -> str:
        """A library nickname is a name too, and reaches no path on its own."""
        if not value:
            raise ValueError(
                "panel.fiducials.library is a footprint library nickname - the "
                "name KiCad's footprint library table gives the library - and "
                "an empty one names nothing"
            )
        return fplib.checked_name(value)


class RailCopperConfig(BaseModel):
    """A no-net copper pour over the rails, on every copper layer.

    `enabled` is the switch;
    off means bare laminate round the boards. The section is written either
    way, because an absent section would say nothing visible.

    `clearance_mm` is what the pour holds off everything else - 0.635 mm by
    default, twenty-five thousandths of an inch, which is the figure this shop
    quotes for copper to a routed edge.

    `pour_between_boards` extends the pour into the gaps between neighbouring
    boards. Off by default: the gap carries the routed channel and the tabs, and
    copper there is copper the router cuts through.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    clearance_mm: float = Field(default=0.635, ge=0.0)
    pour_between_boards: bool = False


class PanelDimensionsConfig(BaseModel):
    """The panel's own dimension items: overall size and tooling-hole spacing.

    **This one is ON when it is absent**, as the tooling holes are: the panel
    builder adds the panel's dimensions automatically. A fabrication panel that
    does not say how big it is is a
    panel somebody has to measure, so this section exists to TUNE the dimensions
    or to switch them off with `"enabled": false`, never to switch them on.

    `layer` is the CANONICAL layer name - `User.16`, which
    a board may name `User.16-Dimensions` in the Appearance panel. A layer
    the panel does not declare is a warning naming it: KiCad reads the item, then
    keeps the layer disabled and shows nobody.

    `first_offset_mm` is how far outside the panel's edge the innermost
    crossbar sits, and `row_pitch_mm` is the step out to the next one. Which
    dimension takes which row is decided by SIZE - smallest innermost, the way
    a drafter orders them - so the single board's size can never overprint the
    panel's overall extents whatever the rails are. The inner pair measures one
    board, not the tooling holes. Left out, both are 2.5 times the
    text height: they scale with the lettering, because a fixed 5 mm pitch under
    8 mm lettering prints one row through the next.

    **The lettering SCALES WITH THE PANEL**. Left out,
    `text_height_mm` is the panel's longest extent over 50, rounded to the
    nearest 0.5 mm and floored at 2 mm - so a 47.5 mm panel letters at 2 mm and
    a 400 mm one at 8 mm, and both read the same at panel scale
    (:func:`eedesignpanels.stages.panel_dimensions.text_height_for`). A height
    stated here WINS and turns the scaling off. `text_thickness_mm` follows the
    height at KiCad's own 15% stroke ratio unless it is stated too.
    `precision` is a number of decimal places, 0 to 5.

    **`text_height_mm` IS THE ONE THE PANEL PAGE SHOWS.** Empty on the page
    means empty here - `null`, written out like
    every other value - and that is the scaling rule above. Anything the page
    can set, the page can set back.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    layer: str = "User.16"
    # None on all four is the scaling rule; a number is the user
    # overruling it.
    first_offset_mm: Union[float, None] = Field(default=None, ge=0.0)
    row_pitch_mm: Union[float, None] = Field(default=None, ge=0.0)
    # THE ONE OF THE FOUR THAT IS ON THE PAGE. Empty is still the scaling rule,
    # which is what the field's own line says, and a number is the user
    # overruling it; the bound refuses a typed mistake by
    # name (:data:`MAX_TEXT_HEIGHT_MM`).
    text_height_mm: Union[float, None] = Field(
        default=None, gt=0.0, le=MAX_TEXT_HEIGHT_MM
    )
    text_thickness_mm: Union[float, None] = Field(default=None, gt=0.0)
    line_thickness_mm: float = Field(default=0.1, gt=0.0)
    arrow_length_mm: float = Field(default=1.27, gt=0.0)
    extension_offset_mm: float = Field(default=0.5, ge=0.0)
    precision: int = Field(default=3, ge=0, le=5)
    suppress_zeroes: bool = True


class PanelTextConfig(BaseModel):
    """Native part-number text on the panel rails."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = True
    layer: Literal["F.SilkS", "B.SilkS"] = "F.SilkS"
    rails: Literal[
        "automatic", "left", "right", "top", "bottom",
        "vertical", "horizontal", "all", "none",
    ] = "automatic"
    text_height_mm: Union[float, None] = Field(default=None, gt=0.0, le=MAX_TEXT_HEIGHT_MM)
    edge_clearance_mm: float = Field(default=1.0, ge=0.0)


class PanelizeConfig(BaseModel):
    """The panel half of the build: what turns instances into a panel.

    Present means panelize (wipe the instances' board outlines, derive the
    panel outline from the material minus the routed channels, merge the
    scores); absent means the build stops at step and repeat, which is what
    every config written before this stage existed meant.

    **`rails` is ONE setting with two spellings**. A number
    is the linked view - the same rail on all four sides - and an object of
    `top`, `bottom`, `left`, `right` is the unlocked one. There is no separate
    "linked" flag, because a flag and four numbers can disagree with each other
    and the shape of the value cannot:

    ```json
    "panel": { "rails": 5.0 }
    "panel": { "rails": {"top": 8, "bottom": 8, "left": 3, "right": 3} }
    ```
    """

    model_config = ConfigDict(extra="forbid")

    rails: Union[float, RailWidths, None] = None
    # THE CARRIER FEATURES, AND EVERY ONE OF THEM IS ALWAYS WRITTEN. A section
    # switched off by being absent would change the panel in a way no reader of
    # the file could see. Each group carries its own `enabled`.
    scores: PanelScoresConfig = PanelScoresConfig()
    rail_copper: RailCopperConfig = RailCopperConfig()
    fiducials: FiducialsConfig = FiducialsConfig()
    # ...and the two that run unless they are declined. See
    # :class:`ToolingHolesConfig` and :class:`PanelDimensionsConfig`: a panel is
    # pinned down and says how big it is whether or not anybody asked.
    tooling_holes: ToolingHolesConfig = ToolingHolesConfig()
    dimensions: PanelDimensionsConfig = PanelDimensionsConfig()
    text: PanelTextConfig = PanelTextConfig()

    @property
    def dimension_settings(self) -> Union[PanelDimensionsConfig, None]:
        """The dimension settings this config means, or None when switched off.

        ONE QUESTION FOR EVERY CARRIER STAGE: the
        section is always there, so the switch inside it is the whole answer and
        no reader has to know which kind of switch a stage has.
        """
        return self.dimensions if self.dimensions.enabled else None

    @property
    def tooling_hole_settings(self) -> Union[ToolingHolesConfig, None]:
        """The tooling holes this config means, or None when switched off."""
        return self.tooling_holes if self.tooling_holes.enabled else None

    @property
    def score_settings(self) -> Union[PanelScoresConfig, None]:
        """The panel scores this config asks for, or None when it asks for none.

        None here switches off the panel's own FAMILIES of saw pass and nothing
        else: every `User.38` line on the panel still gets its keep-out, because
        the lines the boards decided are on the panel whatever this says
        (:func:`eedesignpanels.stages.panel_scores.protect`).
        """
        return self.scores if self.scores.enabled else None

    @property
    def rail_copper_settings(self) -> Union[RailCopperConfig, None]:
        """The rail pour this config asks for, or None when it asks for none."""
        return self.rail_copper if self.rail_copper.enabled else None

    @property
    def fiducial_settings(self) -> Union[FiducialsConfig, None]:
        """The fiducials this config asks for, or None when it asks for none."""
        return self.fiducials if self.fiducials.enabled else None

    @model_validator(mode="after")
    def _a_panel_needs_a_rail_width(self) -> "PanelizeConfig":
        if self.rails is None:
            raise ValueError(
                "a panel needs a rail width: set panel.rails to a number for "
                "one rail on all four sides, or to an object of top, bottom, "
                "left and right"
            )
        if isinstance(self.rails, float) and self.rails < 0.0:
            raise ValueError(f"panel.rails must not be negative: {self.rails}")
        return self

    @property
    def rail_widths(self) -> tuple:
        """`(top, bottom, left, right)` in millimetres, however it was written."""
        if isinstance(self.rails, RailWidths):
            return (self.rails.top, self.rails.bottom,
                    self.rails.left, self.rails.right)
        return (self.rails, self.rails, self.rails, self.rails)

    @property
    def rails_linked(self) -> bool:
        """Does the FILE say one rail width, rather than four?

        The page's unlock control reads this and nothing else. Four numbers that
        happen to be equal are still unlocked - the file said "per side" and
        the page is not going to argue with the file.
        """
        return not isinstance(self.rails, RailWidths)


class VerifyConfig(BaseModel):
    """The gerber comparison the review run performs, and what bounds it.

    Every value is a knob the user can turn from the config alone, because
    a comparison that changes its own settings reports a difference it could
    not have seen. Defaults are the harness's own: 5 um per pixel, report any
    difference at all, 1 mm region cells.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    um_per_pixel: float = Field(default=5.0, gt=0.0)
    threshold: int = Field(default=0, ge=0)
    region_cell_mm: float = Field(default=1.0, gt=0.0)
    # Roughly 1.6 GB of peak working set at a measured 16 MB per
    # megapixel. Over it the comparison warns and stops rather than coarsening.
    max_canvas_mpixels: float = Field(default=100.0, gt=0.0)
    # How large the pictures on the page may be, for any layer that falls back
    # to a rasterised one. Only the DISPLAY is reduced, never the measurement,
    # and the reduction keeps every isolated differing pixel.
    display_max_px: int = Field(default=8000, ge=64)
    # THE VECTOR GUARDRAIL. The picture is vector by default: sharp at any
    # magnification, which is what a user zooming into a difference
    # needs. A layer whose artwork exceeds either cap falls back to a
    # rasterised picture for that layer alone, decided when the page is built
    # and stated on the page. Measured on this corpus the worst layer is
    # 946 KB and 5,033 elements at a locked 60 fps, so these sit far enough
    # above it to be insurance for a denser panel rather than a limit anyone
    # meets today.
    vector_max_kb: float = Field(default=4096.0, gt=0.0)
    vector_max_elements: int = Field(default=40000, gt=0)
    # How far outside the board outline the comparison still looks. The window
    # is clipped to the outline because that is what ships; fabrication text
    # parked far from the board is not part of the panel.
    clip_margin_mm: float = Field(default=1.0, ge=0.0)
    # THE VERDICT THRESHOLD, in the unit the verdict is judged in: how far the
    # geometry moved at the worst place on a layer. 0.01 mm
    # sits a decade above the rasterizer's own sub-micrometre floor and a
    # decade below any fault an engineer would care about.
    displacement_threshold_mm: float = Field(default=0.01, ge=0.0)
    # WHICH LAYERS MAY SET A VERDICT. "physical"
    # is the default: the layers that drive physical construction of
    # the bare board - copper, mask, silkscreen, paste, outline and the
    # route/score layers - decided by the same rule that drives the review
    # page's default visibility, so the two answers cannot drift apart.
    # Fabrication and documentation layers are still replicated and still
    # measured; they are reported and not judged. "all" judges everything that
    # was compared; a list of canonical names judges exactly those.
    compared_layers: Union[Literal["physical", "all"], list[str]] = "physical"
    # HOW MANY PROCESSES the per-layer comparison may use. 0 (the default)
    # sizes the pool at run time from this machine's CPU count and available
    # RAM (eedesignpanels.verify.workers); 1 runs serially in-process. The pool
    # decides only when a layer is computed, never what, so auto-sizing does
    # not break "same input, same output" - which is why this knob, unlike
    # every other one here, may vary between machines without varying the
    # panel.
    workers: int = Field(default=0, ge=0)


class PanelConfig(BaseModel):
    """A validated panel.json, plus the paths it resolves to."""

    model_config = ConfigDict(extra="forbid")

    # WHICH SETTINGS THESE ARE, AND WHO WROTE THEM. Declared first because
    # pydantic dumps fields in the order
    # they are declared, and the file is written from that dump: the revision
    # is the first line of `panel.json` for the same reason a part number is
    # the first line of a drawing.
    #
    # NEITHER IS JUDGED HERE. The revision is compared by `load_config` and by
    # the settings import, before the model ever sees the data, because a
    # field-level refusal is exactly the message the revision exists to replace:
    # "revision must be 1" reads as a broken file, and "these settings came
    # from an older version" reads as what happened.
    revision: int = CONFIG_REVISION
    written_by: str = WRITTEN_BY
    source_board: str
    stage: StageName = "passthrough"
    grid: GridConfig
    # THE MINIMUM TAB, and it is top level on purpose: both halves of the build
    # enforce it - prep between the runs of one board's toolpath, panelize
    # between the channels of the whole panel - and one fabrication rule stated
    # twice is one that can disagree with itself. Below the hard floor is a
    # config ERROR (:mod:`eedesignpanels.mintab`): the run cannot do what it was
    # asked, so it says so and writes nothing.
    min_tab_mm: float = MIN_TAB_DEFAULT_MM
    # ...AND WHETHER THE ROUTER TAKES WHAT IS UNDER IT. Beside
    # the width and for the same reason the width is here: it is one fabrication
    # rule, both halves of the build enforce it, and stated twice it could
    # disagree with itself. True is what this project has always done. False
    # leaves every accidental sliver standing, names each one with its size and
    # its position, and derives an outline that honestly follows it - which is a
    # fab's decision to make, because consuming a sliver is more cutting than the
    # design asked for. It travels to the two stages ON the width
    # (:class:`mintab.Rule`), so nothing between here and them has to carry it.
    eliminate_slivers: bool = True
    edge_plating: bool = False
    edge_plating_front_mask: bool = True
    edge_plating_back_mask: bool = True
    edge_plating_mask_width_mm: float = Field(
        default=0.2, gt=0.0, le=MAX_EDGE_PLATING_MASK_WIDTH_MM, allow_inf_nan=False,
    )
    lock_board_on_save: bool = True
    # THE LARGEST OUTLINE GAP THE TOOL WILL CLOSE. It is set on the page, and
    # every use is logged. Top level beside the minimum tab, and for the same
    # reason: it is
    # one reading rule that the prep half applies to the source board, and one
    # rule stated twice can disagree with itself. The default is KiCad's own
    # figure, so the tool accepts every board KiCad accepts; ZERO refuses any
    # gap at all, which is the setting for someone who wants the tool stricter
    # than KiCad. Every gap it closes is announced with its coordinate and its
    # distance, because the tool helps detect why a bad outline is bad rather
    # than working around it.
    outline_heal_mm: float = Field(
        default=OUTLINE_HEAL_DEFAULT_MM, ge=0.0, le=MAX_OUTLINE_HEAL_MM
    )
    # WHETHER A CUTOUT WIDER THAN THE CUTTER IS MILLED OUT. A checkbox, FALSE
    # BY DEFAULT, because most board houses do their own CAM work on cutouts:
    # the wall is routed, the middle is left attached to
    # nothing, and the derived outline shows that slug honestly. True adds
    # concentric passes inside the wall and no loose piece remains. Top level
    # beside the minimum tab and the heal, for the same reason they are: one
    # fabrication rule about the source board, stated once.
    pocket_cutouts: bool = False
    # An explicit empty list replicates everything; omitting the key takes the
    # default set.
    exclude_layers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_LAYERS)
    )
    review: ReviewConfig = ReviewConfig()
    verify: VerifyConfig = VerifyConfig()
    # Absent unless the project prepares its source board. `eedesignpanels.prep`
    # says so plainly rather than inventing a cutter diameter.
    prep: Union[PrepConfig, None] = None
    # Absent means the build stops at step and repeat: six copies of a board in
    # a grid, each still carrying its own outline. Present is what makes them a
    # panel.
    panel: Union[PanelizeConfig, None] = None

    # Set by load_config; relative paths in the config resolve against it.
    config_path: Path = Field(exclude=True, default=Path())

    @field_validator("min_tab_mm")
    @classmethod
    def _honour_the_floor(cls, value: float) -> float:
        return checked_min_tab(value)

    @model_validator(mode="after")
    def _one_minimum_tab_rule(self) -> "PanelConfig":
        """Bind the switch to the width, so the rule travels as one thing.

        `min_tab_mm` reads as the number it has always been - it IS a float - and
        carries `eliminate` for the two stages that ask. Doing it here rather
        than at every call site is what keeps the setting from having to be
        threaded through code that has no opinion about it.
        """
        self.__dict__["min_tab_mm"] = Rule(
            float(self.min_tab_mm), self.eliminate_slivers
        )
        return self

    @property
    def score_rule_half_width_mm(self) -> float:
        """How far either side of a score line the copper keep-out reaches.

        THE ONE PLACE ANY STAGE ASKS. The board's
        keep-out and the panel's are the same statement, because the board's one
        steps into the panel with its instance and the panel recognises it by
        measuring it. Two numbers would break that recognition and the panel
        would guard one line twice.

        `prep.vscore.rule_half_width_mm` when this config prepares a board, and
        the shipped default when it does not - a config with no prep section
        decides no score on a board, so there is nothing for the number to
        agree with.
        """
        if self.prep is not None:
            return float(self.prep.vscore.rule_half_width_mm)
        return SCORE_RULE_HALF_WIDTH_DEFAULT_MM

    def model_copy(self, *, update=None, deep: bool = False) -> "PanelConfig":
        """A copy whose minimum-tab rule still agrees with its own switch.

        `model_copy` does not re-validate, so a copy made with `eliminate_slivers`
        updated would otherwise keep the rule the original was built with - a
        stale flag riding on a number, which is exactly the failure a float
        subclass invites. Rebound here, once, where it cannot be forgotten.
        """
        copied = super().model_copy(update=update, deep=deep)
        copied.__dict__["min_tab_mm"] = Rule(
            float(copied.min_tab_mm), copied.eliminate_slivers
        )
        return copied

    @property
    def rail_widths(self) -> tuple:
        """`(top, bottom, left, right)`, or None when this config makes no panel."""
        return self.panel.rail_widths if self.panel is not None else None

    @property
    def carrier_features(self) -> tuple:
        """`(tooling_holes, scores, rail_copper)`, each None when it will not run.

        One accessor rather than three, because the three travel together into
        the panel half and a caller that forgot one would silently skip a stage.

        EVERY ONE COMES FROM ITS OWN `enabled`.
        The sections are always in the file, so None here means DECLINED and
        never unnamed, and the three stages answer the same question the same
        way.
        """
        if self.panel is None:
            return (None, None, None)
        return (self.panel.tooling_hole_settings, self.panel.score_settings,
                self.panel.rail_copper_settings)

    @property
    def cutter_diameter_mm(self) -> float:
        """The declared router bit, or 0.0 when this config declares none.

        One accessor for a number that lives two levels down and is read by
        callers that hold the config rather than the prep section. Zero means
        UNDECLARED and not a diameter: a bit is a thing on a shelf, the artwork
        cannot carry it, and a caller that needs one refuses by name rather than
        inventing a figure.
        """
        return self.prep.cutter_diameter if self.prep is not None else 0.0

    @property
    def panel_dir(self) -> Path:
        """The folder the core owns. A panel build writes nothing outside it.

        The one write outside it is `Save` on the step-1 page, which replaces
        the design file and keeps its previous bytes beside it
        (:func:`eedesignpanels.preview.actions.save`).
        """
        return self.config_path.parent

    @property
    def source_board_path(self) -> Path:
        return (self.panel_dir / self.source_board).resolve()

    @property
    def output_board_path(self) -> Path:
        return self.panel_dir / "panel.kicad_pcb"

    @property
    def output_project_path(self) -> Path:
        """The panel's own `.kicad_pro`. A board file alone is not a project."""
        return self.panel_dir / "panel.kicad_pro"

    @property
    def source_project_path(self) -> Path:
        """The design's project file, beside its board. May not exist."""
        return self.source_board_path.with_suffix(".kicad_pro")


# WHAT A PERSON READS WHEN A FILE CAME FROM ANOTHER REVISION. One sentence,
# naming the file's revision and this tool's, and it
# never says "invalid" - the file is not wrong, it is from another version of
# this tool. The newer case is the one an engineer hits when a colleague
# upgrades first, and it must not look like a corrupt file.
REVISION_HEADLINE = "These settings came from a different version of eedesignpanels"


def revision_sentence(found) -> str:
    """Why *found* is not this tool's revision, in one plain sentence, or "".

    *found* is whatever the file's `revision` key held, including None for a
    file that has no such key at all.
    """
    mine = CONFIG_REVISION
    if found is None:
        return (f"they carry no revision number, so they are older than the "
                f"revision this tool writes (revision {mine})")
    if isinstance(found, bool) or not isinstance(found, int):
        return (f"their revision is not a whole number, so this tool cannot "
                f"tell which version wrote them - it writes revision {mine}")
    if found < mine:
        return (f"they are revision {found} and this tool writes revision "
                f"{mine}, so they were written by an older version")
    if found > mine:
        return (f"they are revision {found} and this tool writes revision "
                f"{mine}, so they came from a newer version")
    return ""


def revision_problem(data: dict) -> str:
    """The sentence for this config DATA's revision, or "" when it is ours."""
    return revision_sentence(data.get(REVISION_KEY))


def stamped(data: dict) -> dict:
    """*data* with the revision and the writing tool first, exactly as written.

    ONE PLACE, so every write agrees. A held
    config passes through here on its way to disk whatever it was built from -
    the model's own dump, a hand-edited file, another project's imported
    settings - and comes out saying this tool's revision, this tool's version,
    and saying them in the first two lines.
    """
    rest = {
        key: value for key, value in data.items()
        if key not in (REVISION_KEY, WRITTEN_BY_KEY)
    }
    return {REVISION_KEY: CONFIG_REVISION, WRITTEN_BY_KEY: WRITTEN_BY, **rest}


def load_config(path: Union[Path, str]) -> PanelConfig:
    """Read and validate a panel.json. Raises ConfigError with the reason.

    A file from another revision raises :class:`ConfigRevisionError` BEFORE the
    model is asked anything, so it is told it is old rather than told a field
    is wrong.
    """
    config_path = Path(path).resolve()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: top level must be a JSON object")
    if "config_path" in data:
        raise ConfigError(f"{config_path}: 'config_path' is not a config key")

    problem = revision_problem(data)
    if problem:
        raise ConfigRevisionError(f"{config_path}: {problem}", reason=problem)

    try:
        config = PanelConfig(**data, config_path=config_path)
    except ValidationError as exc:
        raise ConfigError(f"{config_path} is invalid:\n{exc}") from exc

    board = config.source_board_path
    if not board.is_file():
        raise ConfigError(f"source_board does not exist: {board}")
    if board.suffix != ".kicad_pcb":
        raise ConfigError(f"source_board is not a .kicad_pcb file: {board}")
    if board.parent == config.panel_dir:
        raise ConfigError(
            f"source_board lives in the panel folder, which the builder "
            f"overwrites: {board}"
        )
    return config


# ---------------------------------------------------------------------------
# A PROJECT THAT HAS NEVER BEEN PANELIZED STILL STARTS. A missing `panel.json`
# and an empty one both lead to a starting file, because a refusal is the one
# answer a starting point may not give.
# ---------------------------------------------------------------------------

# WHAT A BRAND-NEW panel.json SAYS, where the model has no answer of its own.
# The three fabrication numbers are typical fabricator values; the arrangement
# numbers are a starting point the page exists to change, and they are stated
# here so a reader of the file can see every one of them.
NEW_CUTTER_DIAMETER_MM = 0.88
NEW_ROUTE_PAST_SCORE_MM = 0.2
NEW_GRID_COLUMNS = 2
NEW_GRID_ROWS = 2
NEW_GAP_MM = 3.0
NEW_RAILS_MM = 5.0

# WHAT THE PERSON READS WHEN THE FILE WOULD NOT LOAD. Three plain sentences,
# in this order: what happened, that the
# tool is running anyway, and that their file is untouched. No file format is
# named and no parser is quoted - the reason a JSON parser gives is written for
# somebody debugging a parser.
UNREADABLE_HEADLINE = "The settings file could not be read"
UNREADABLE_LINES = (
    ("eedesignpanels has started with its standard settings, so you can carry "
     "on working."),
    ("Your settings file has NOT been changed. It stays exactly as it is "
     "until you press Save."),
)

# ...and the short reason, in the same register. One phrase per way a file
# fails, because "empty" and "not settings this tool knows" are different
# things to a person and the same thing to a parser.
UNREADABLE_EMPTY = "the file is empty"
UNREADABLE_NOT_JSON = "the text in it is not in the form this tool writes"
UNREADABLE_NOT_SETTINGS = "it does not hold the settings this tool expects"
UNREADABLE_NO_ACCESS = "the file could not be opened"


def find_source_board(project_dir: Union[Path, str]) -> Path:
    """The one `.kicad_pcb` beside the panel folder. Exactly one, or an error.

    A GUESS ABOUT WHICH BOARD IS WORSE THAN A QUESTION. None and several are
    both refusals, and each names the folder
    that was looked in and what was found there, so the answer is in the
    sentence rather than in a second command.
    """
    project_dir = Path(project_dir).resolve()
    boards = sorted(
        path for path in project_dir.glob("*.kicad_pcb") if path.is_file()
    )
    if len(boards) == 1:
        return boards[0]
    if not boards:
        raise ConfigError(
            f"there is no board to panelize in {project_dir}: that folder "
            f"holds no .kicad_pcb file. Start this tool on a KiCad project "
            f"folder, or write panel/panel.json yourself and name the board "
            f"in it."
        )
    found = ", ".join(path.name for path in boards)
    raise ConfigError(
        f"there is more than one board in {project_dir}, so this tool cannot "
        f"tell which one you meant: {found}. Write panel/panel.json and name "
        f"the board in its 'source_board' setting."
    )


def default_config_data(source_board: str) -> dict:
    """A starting `panel.json`, fully explicit, as a plain dict ready to write.

    THE MODEL WRITES ITS OWN DEFAULTS. Everything except the six numbers above
    comes from the config classes themselves, so a default that moves moves
    here too and this file can never drift from the schema it has to load
    cleanly under.
    """
    config = PanelConfig(
        source_board=source_board,
        stage="build_panel",
        grid=GridConfig(
            columns=NEW_GRID_COLUMNS, rows=NEW_GRID_ROWS, gap_mm=NEW_GAP_MM
        ),
        prep=PrepConfig(
            cutter_diameter=NEW_CUTTER_DIAMETER_MM,
            vscore=VScoreConfig(route_past_score_mm=NEW_ROUTE_PAST_SCORE_MM),
        ),
        panel=PanelizeConfig(rails=NEW_RAILS_MM),
    )
    return config.model_dump(mode="json")


def config_text(data: dict) -> str:
    """The bytes a `panel.json` is written as. One place, so every write agrees."""
    return json.dumps(stamped(data), indent=2) + "\n"


def create_config(config_path: Union[Path, str]) -> dict:
    """Make the `panel/` folder and write a starting `panel.json` into it.

    ON DISK, NOT IN MEMORY. The file is the record of what was built,
    so a first run leaves one behind and the second run reads it.
    """
    config_path = Path(config_path).resolve()
    board = find_source_board(config_path.parent.parent)
    data = default_config_data(f"../{board.name}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_text(data), encoding="utf-8")
    return data


def _plain_reason(raw: str, cause) -> str:
    """Why the file would not load, in words for somebody who does not program."""
    if not raw.strip():
        return UNREADABLE_EMPTY
    if isinstance(cause, json.JSONDecodeError):
        return UNREADABLE_NOT_JSON
    if isinstance(cause, OSError):
        return UNREADABLE_NO_ACCESS
    return UNREADABLE_NOT_SETTINGS


class ConfigStartup(NamedTuple):
    """How a launch got its config, and what the person has to be told.

    *problem* is empty on an ordinary launch. When it is not, it is the whole
    warning the page shows, already in plain words, and *config* and *data* are
    the standard settings held IN MEMORY - the file on disk is untouched.
    """

    config: PanelConfig
    data: dict
    created: bool
    problem: str
    reason: str
    banner: str


def load_or_create(path: Union[Path, str]) -> ConfigStartup:
    """Read `panel.json`, write one if there is none, and NEVER refuse to start.

    Three ways in:

    * no file - the folder and a starting file are created, and the launch goes
      on normally.
    * a file that will not load - empty, not in the form this tool writes, or
      holding settings it does not know. All one case: the launch goes on with
      standard settings, the person is told in plain words, and **their file is
      not touched.**
    * a file that loads - nothing to say.

    The one thing that still stops a launch is not knowing which board was
    meant (:func:`find_source_board`).
    """
    config_path = Path(path).resolve()

    if not config_path.exists():
        data = create_config(config_path)
        return ConfigStartup(
            config=load_config(config_path), data=data, created=True,
            problem="", reason="",
            banner="written now - this project had no settings file",
        )

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # A FILE FROM ANOTHER REVISION TAKES THIS SAME PATH and only the
        # sentence differs: standard settings, the warning on the page, and
        # the user's file untouched until they save. No migration exists.
        if isinstance(exc, ConfigRevisionError):
            headline, reason = REVISION_HEADLINE, exc.reason
            lead = "came from a different version of this tool"
        else:
            try:
                raw = config_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            headline = UNREADABLE_HEADLINE
            reason = _plain_reason(raw, exc.__cause__)
            lead = "could not be read"
        board = find_source_board(config_path.parent.parent)
        data = default_config_data(f"../{board.name}")
        return ConfigStartup(
            config=PanelConfig(**data, config_path=config_path), data=data,
            created=False,
            problem=f"{headline} - {reason}. " + " ".join(UNREADABLE_LINES),
            reason=reason,
            banner=f"{lead} ({reason}) - started on standard "
                   f"settings, your file is untouched",
        )

    # Stamped on the way in as well as on the way out, so the settings this
    # session holds say which tool they belong to even when the file on disk
    # was hand-written and said only the revision.
    return ConfigStartup(
        config=config,
        data=stamped(json.loads(config_path.read_text(encoding="utf-8"))),
        created=False, problem="", reason="", banner="",
    )
