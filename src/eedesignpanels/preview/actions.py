"""The things a click in the preview may change, and the one that writes.

**Everything here edits MEMORY. One function writes.** The session loads the
board and the config once and holds them; accept, decline and set_param change
those held objects and nothing else, and :func:`save` puts the result on disk in
a single action. That is what makes an edit cost a
recomputation instead of a write plus a re-parse, and it is what lets a designer
try a decision and take it back without the design ever having carried it.

The list of edits is short and explicit, which is what keeps it inside the
source-design-untouched rule:

* :func:`accept` adds ONE `User.38` line to the held board, at the outline
  bounding box's reach, exactly the line :func:`route_toolpath.score_line_for`
  defines - the accept surface built for this, so the corpus, the tests and the
  click agree - AND the copper keepout centred on that line
  (`eedesignpanels.stages.board_scores`);
* :func:`decline` removes the board-level `User.38` lines lying on a blade
  line, and their keepouts with them, which is what "no decision" looks like
  under a model where rejection is absence;
* :func:`set_param` puts one value into the held config data - a number, a
  switch or a named choice - and, for a carrier stage's group switch, writes
  that stage's whole section out with `enabled` set, so the config is
  explicit and a missing section never switches a stage off;
* :func:`import_settings` puts ANOTHER PROJECT'S settings into the held config
  data, all of them at once, keeping this project's identity - see "THE IMPORT"
  below;
* :func:`repair_keepouts` runs that same keep-out sync ONCE ON THE BOARD THE
  SESSION HAS JUST READ, so a board that arrives carrying a score line and
  no keep-out is put right without the user having to switch the score off
  and on again;
* :func:`save` writes the board and, when a parameter changed, the config.

A blade line arrives as GEOMETRY - `{"start": [x, y], "end": [x, y]}` - because
that is how the decision model identifies a cut. Ids are not used and must not
be: operations clobber uuids and a uuid says nothing about where a cut is.
"""

import copy
import json
import math
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from eedesigncore import (
    Board,
    GuardedDocument,
    GuardedSaveError,
    SourceSnapshot,
    item_layers,
    save_guarded_documents,
)
from pydantic import ValidationError

from eedesignpanels import placementfile
from eedesignpanels.config import (
    MAX_EDGE_PLATING_MASK_WIDTH_MM,
    MAX_JUMP_MM,
    MAX_SETBACK_MM,
    MAX_TEXT_HEIGHT_MM,
    REVISION_KEY,
    WRITTEN_BY_KEY,
    FiducialsConfig,
    PanelConfig,
    PanelDimensionsConfig,
    PanelTextConfig,
    PanelScoresConfig,
    RailCopperConfig,
    ToolingHolesConfig,
    config_text,
    revision_problem,
    stamped,
)
from eedesignpanels.fplib import (
    DEFAULT_FIDUCIAL_LIBRARY,
    LibraryError,
    checked_name,
    resolve_cached,
)
from eedesignpanels.placement import ALTERNATIONS, ROTATIONS
from eedesignpanels.stages import board_scores, route_toolpath
from eedesignpanels.stages.fiducials import DIRECTIONS as FIDUCIAL_DIRECTIONS
from eedesignpanels.stages.fiducials import SIDES as FIDUCIAL_SIDES
from eedesignpanels.stages.panel_scores import DEFAULT_RULE_HALF_WIDTH_MM
from eedesignpanels.toolpath import COLLINEAR_TOL_MM

# The config keys a preview click may set, and where each one lives in the
# file. Only these: a preview that could write anything into the config would be
# a second, worse config editor. `min_tab_mm` is here because the preview
# already reports what it did - the slivers it consumed and the declared tabs
# narrower than it - so the user can tune it and see the effect.
SETTABLE = {
    "cutter_diameter": ("prep", "cutter_diameter"),
    "route_past_score_mm": ("prep", "vscore", "route_past_score_mm"),
    "route_along_score_mm": ("prep", "vscore", "route_along_score_mm"),
    "route_tangent_angle_deg": ("prep", "vscore", "route_tangent_angle_deg"),
    "route_end_overrides": ("prep", "vscore", "route_end_overrides"),
    "custom_route_ends": ("prep", "vscore", "custom_route_ends"),
    # THE SCORE KEEP-OUT WIDTH, on the PREP page beside the other two declared
    # blade numbers. It is a BOARD setting: accepting a score
    # writes the keep-out into the source board at this width, and the panel
    # guards at the same number so it recognises what the board already did.
    "score_rule_half_width_mm": ("prep", "vscore", "rule_half_width_mm"),
    "min_tab_mm": ("min_tab_mm",),
    # THE LARGEST OUTLINE GAP THE TOOL WILL CLOSE. Top level in the
    # file, on step 1 beside the cutter and the keep-out in the GUI, because it
    # is a fact about reading the SOURCE BOARD's outline.
    "outline_heal_mm": ("outline_heal_mm",),
    # ...and whether the router takes what falls under it. The same setting
    # family as the width, so the same page and the same endpoint, and
    # a change to it is a BOARD change: the covering passes are computed at prep
    # time and land in the board's own toolpath layer.
    "eliminate_slivers": ("eliminate_slivers",),
    "edge_plating": ("edge_plating",),
    "edge_plating_front_mask": ("edge_plating_front_mask",),
    "edge_plating_back_mask": ("edge_plating_back_mask",),
    "edge_plating_mask_width_mm": ("edge_plating_mask_width_mm",),
    "lock_board_on_save": ("lock_board_on_save",),
    # POCKETING, AND IT IS OFF BY DEFAULT: the default is to route the outline
    # only, because most board houses do their own pocketing in their CAM
    # work. A BOARD change like the two above it: the passes that clear a
    # cutout are computed at prep time and land in the board's toolpath layer.
    "pocket_cutouts": ("pocket_cutouts",),
    # The PANEL page's own numbers. Same endpoint, same
    # validation, same pending count: a config edit is a config edit whichever
    # page it was typed on.
    "columns": ("grid", "columns"),
    "rows": ("grid", "rows"),
    "gap_x_mm": ("grid", "gap_x_mm"),
    "gap_y_mm": ("grid", "gap_y_mm"),
    # THE PLACEMENT SUITE. Every one of these lives
    # under `grid`, because where a copy sits and how it is turned are one
    # setting group and the GUI says so too.
    "rotation_deg": ("grid", "rotation_deg"),
    "alternate": ("grid", "alternate"),
    "nest_across_mm": ("grid", "nest_across_mm"),
    "nest_down_mm": ("grid", "nest_down_mm"),
    # The one that moves NOTHING by itself: a measurement the viewport draws
    # over a panel that already exists. It still lives in the config, because
    # every value on the page does.
    "clearance_warning_mm": ("grid", "clearance_warning_mm"),
    # THE CARRIER'S RAILS. One setting
    # with two spellings: `rail_mm` writes the LINKED form - one number for all
    # four sides - and the four per-side keys write the unlocked form. Which
    # spelling the file carries IS the unlock state, so there is no flag beside
    # them that could disagree with them.
    "rail_mm": ("panel", "rails"),
    "rail_top_mm": ("panel", "rails", "top"),
    "rail_bottom_mm": ("panel", "rails", "bottom"),
    "rail_left_mm": ("panel", "rails", "left"),
    "rail_right_mm": ("panel", "rails", "right"),
    # THE CARRIER STAGES. Each one lives
    # under its own section, the section is ALWAYS WRITTEN, and `enabled` inside
    # it is the switch, so a reader of the file can always see whether a stage
    # is on. Off builds the same panel as a project with no such stage, to the
    # byte - the file just says so.
    "hole_diameter_mm": ("panel", "tooling_holes", "diameter_mm"),
    "hole_top_left": ("panel", "tooling_holes", "corners", "top_left"),
    "hole_top_right": ("panel", "tooling_holes", "corners", "top_right"),
    "hole_bottom_right": ("panel", "tooling_holes", "corners", "bottom_right"),
    "hole_bottom_left": ("panel", "tooling_holes", "corners", "bottom_left"),
    "hole_placement": ("panel", "tooling_holes", "placement"),
    "hole_inset_across_mm": ("panel", "tooling_holes", "inset_across_mm"),
    "hole_inset_down_mm": ("panel", "tooling_holes", "inset_down_mm"),
    # THE FOUR SCORE SWITCHES. Not
    # positions: which families of pass the panel carries, each computed from
    # the layout. In the order the `V-Scores` group lists them - each rail
    # score, then the setback and the jump that hang off it, then the passes
    # between boards - and horizontal before vertical wherever a dependant does
    # not force the other way.
    #
    # THE KEY IS THE LABEL: the two passes between boards are
    # `inter_row` and `inter_column` in the file, exactly what the menu calls
    # them, so the file and the screen do not name one setting two ways.
    #
    # THE TWO SETBACKS. Each moves its rail pass off the board array and into
    # the rail, so the blade and the router bit are not in one place. Zero
    # puts the pass on the edge of the board array.
    #
    # THE TWO JUMP DISTANCES. A tab-2 setting because it
    # depends on the rail widths and tab 2 is the only place it can be seen.
    # Each distance stops a board-defined saw pass short of the panel edge at
    # both ends, the distance belongs to the LINE'S own direction, and zero is
    # no jump - there is no switch beside the number.
    "score_rails_horizontal": ("panel", "scores", "rails_horizontal"),
    "score_rail_setback_horizontal_mm": (
        "panel", "scores", "rail_setback_horizontal_mm"
    ),
    "score_jump_vertical_mm": ("panel", "scores", "jump_vertical_mm"),
    # WHICH SCORE A SHARED LINE IS. A rail score
    # with a zero setback can land exactly on a pass the boards decided, and THE
    # TOOL CANNOT TELL which of the two that one saw line is - both cuts are
    # real and the geometry is identical. On is the rail score: full span, no
    # jump, the reading that lets a panel come apart. One per rail score, each a
    # dependant of it.
    "score_shared_line_rail_horizontal": (
        "panel", "scores", "shared_line_is_rail_score_horizontal"
    ),
    "score_rails_vertical": ("panel", "scores", "rails_vertical"),
    "score_rail_setback_vertical_mm": (
        "panel", "scores", "rail_setback_vertical_mm"
    ),
    "score_jump_horizontal_mm": ("panel", "scores", "jump_horizontal_mm"),
    "score_shared_line_rail_vertical": (
        "panel", "scores", "shared_line_is_rail_score_vertical"
    ),
    "score_inter_row": ("panel", "scores", "inter_row"),
    "score_inter_column": ("panel", "scores", "inter_column"),
    "rail_copper_clearance_mm": ("panel", "rail_copper", "clearance_mm"),
    "rail_copper_between": ("panel", "rail_copper", "pour_between_boards"),
    # THE PANEL FIDUCIALS. The footprint is a NAME, which
    # makes it the one string field on this page whose allowed values are a
    # property of the machine: they come from the library KiCad's settings
    # resolve, so the value is checked for SHAPE here (:data:`NAME_PARAMS`) and
    # for membership by the stage, which is the only place holding the library.
    "fiducial_footprint": ("panel", "fiducials", "footprint"),
    "fiducial_offset_mm": ("panel", "fiducials", "offset_mm"),
    "fiducial_direction": ("panel", "fiducials", "direction"),
    "fiducial_side": ("panel", "fiducials", "side"),
    "fiducial_top_left": ("panel", "fiducials", "corners", "top_left"),
    "fiducial_top_right": ("panel", "fiducials", "corners", "top_right"),
    "fiducial_bottom_right": ("panel", "fiducials", "corners", "bottom_right"),
    "fiducial_bottom_left": ("panel", "fiducials", "corners", "bottom_left"),
    # THE LETTERING HEIGHT ON THE PANEL'S DIMENSIONS. The one field of the four
    # derived sizes that is on the page, and one of the parameters here that may
    # be EMPTY: empty is the config's `null`, which is the scaling rule
    # (`NULLABLE_PARAMS`), so the user can always get the automatic size back
    # without knowing it.
    "dimension_text_height_mm": ("panel", "dimensions", "text_height_mm"),
    "panel_text_rails": ("panel", "text", "rails"),
    "panel_text_height_mm": ("panel", "text", "text_height_mm"),
    "panel_text_clearance_mm": ("panel", "text", "edge_clearance_mm"),
}

# THE GROUP SWITCHES. Each one names a whole config section, and it writes
# `enabled` INSIDE that section, so whether a stage is on is always visible in
# the file. The section is written out in full the first time either the switch
# or a field in it is touched, because `panel.json` alone fully determines the
# panel: what the user saw on screen is what the file says, and a later change
# to a default cannot move a panel that was already approved.
CARRIER_SECTIONS = {
    "tooling_holes_on": ("panel", "tooling_holes"),
    "scores_on": ("panel", "scores"),
    "rail_copper_on": ("panel", "rail_copper"),
    "fiducials_on": ("panel", "fiducials"),
}

# What each switch writes when it is turned on.
SECTION_DEFAULTS = {
    "tooling_holes_on": ToolingHolesConfig,
    "scores_on": PanelScoresConfig,
    "rail_copper_on": RailCopperConfig,
    "fiducials_on": FiducialsConfig,
}

# EVERY CARRIER SECTION IS WRITTEN AND `enabled` IS ITS SWITCH, so the name
# below is simply the whole set. What differs between groups is only which way
# `enabled` defaults, and that is the config model's answer
# (`SECTION_DEFAULTS`).
SECTION_SWITCH_INSIDE = frozenset(CARRIER_SECTIONS)

# A SECTION WITH NO SWITCH ON THE PAGE IS STILL STATED IN FULL. The dimensions
# group is one of these: the panel says how big
# it is unless a config declines it, so the page carries no switch for it - and
# the section still has numbers whether or not the file names them. Tuning the
# one field on the page therefore writes the whole section out, exactly as a
# carrier field does, because `panel.json` alone fully determines the panel
# and a later change to a default must not move a panel that was already
# approved.
STATED_SECTIONS = {
    "panel_text_rails": (("panel", "text"), PanelTextConfig),
    "panel_text_height_mm": (("panel", "text"), PanelTextConfig),
    "panel_text_clearance_mm": (("panel", "text"), PanelTextConfig),
    "dimension_text_height_mm": (
        ("panel", "dimensions"), PanelDimensionsConfig,
    ),
}

# THE PARAMETERS THAT MAY BE EMPTY: the two lettering heights. Empty is not
# "no answer": it is the config's `null`, and for a lettering height `null` IS
# an answer - size it from the panel. So it is posted,
# it is written, and it counts as a change like any other value. Every other
# number on both pages is a size the build needs, where an empty field is a
# field nobody filled in and is not read at all.
NULLABLE_PARAMS = frozenset({"dimension_text_height_mm", "panel_text_height_mm"})

# ...and how tall the lettering may be asked to be. The config model's own bound
# (`config.MAX_TEXT_HEIGHT_MM`), refused here by a sentence rather than as a
# validation traceback, and fenced against it in the tests.
TEXT_HEIGHT_PARAMS = frozenset({"dimension_text_height_mm", "panel_text_height_mm"})

# The parameters that are not numbers. A page control has a type and so does the
# config key under it, and a checkbox posting 1.0 or a dropdown posting a
# number nobody can read are both ways of losing that.
BOOLEAN_PARAMS = frozenset({
    "custom_route_ends",
    "edge_plating",
    "edge_plating_front_mask", "edge_plating_back_mask", "lock_board_on_save",
    "hole_top_left", "hole_top_right", "hole_bottom_right", "hole_bottom_left",
    "rail_copper_between",
    "score_rails_horizontal", "score_rails_vertical",
    "score_inter_row", "score_inter_column",
    # THE TWO SHARED-LINE SWITCHES: a tick, not a
    # distance, because the question is which of two meanings one saw line has.
    "score_shared_line_rail_horizontal", "score_shared_line_rail_vertical",
    "fiducial_top_left", "fiducial_top_right",
    "fiducial_bottom_right", "fiducial_bottom_left",
    "eliminate_slivers",
    # A tick, not a distance: the question is whether the middle comes out.
    "pocket_cutouts",
    *CARRIER_SECTIONS,
})

# THE ONE FIELD WHOSE ALLOWED VALUES THIS FILE CANNOT LIST.
# A fiducial footprint name comes from the library KiCad's own settings resolve,
# so the set is a property of the machine and not of this code. It is checked
# for SHAPE - a file name, no path of its own - by the same function the config
# model uses, so the page and the file agree; whether the library HOLDS it is
# the stage's answer, because the stage is what has the library.
NAME_PARAMS = frozenset({"fiducial_footprint"})

# THE PLACEMENT FILE'S SWITCH, and it only ever goes OFF.
# Turning it ON means picking a file, which is `import_placement` - a switch
# cannot conjure the rows, and a config with an empty placement section would
# be a panel of no boards. So `false` clears the section and `true` says so.
PLACEMENT_SWITCH = "placement_on"
PLACEMENT_SECTION = ("grid", "placement")

# The dropdowns, and every value each one may take. Read off the config model's
# own `Literal`, so a value the schema would refuse is refused HERE, by name,
# rather than as a validation traceback.
CHOICE_PARAMS = {
    "panel_text_rails": (
        "automatic", "left", "right", "top", "bottom",
        "vertical", "horizontal", "all", "none",
    ),
    "hole_placement": ("rail_corner_center", "fixed_inset"),
    "alternate": ALTERNATIONS,
    "fiducial_direction": FIDUCIAL_DIRECTIONS,
    "fiducial_side": FIDUCIAL_SIDES,
}

# The dropdowns whose values are NUMBERS. A dropdown posts what the user
# picked and the config key under it holds a millimetre or a degree, so the
# value is checked against the offered set by NAME rather than left to the
# schema - and a value the page could never have offered is refused here.
NUMBER_CHOICE_PARAMS = {
    "rotation_deg": ROTATIONS,
}

# The signed ones. Every other number field on the page is a size or a
# clearance and cannot be negative; a nest shift is a DIRECTION - which way a
# shape interlocks is the shape's business - so it is named here rather than
# left to the schema's silence.
SIGNED_PARAMS = frozenset({"nest_across_mm", "nest_down_mm"})

# NO PARAMETER ON THIS PAGE IS A LIST. The panel score positions were the last
# ones, and nobody types a score position: a pass goes down the middle of a
# gap or along a rail edge, both of which the layout already knows. Kept as an
# empty set so the coercion below still has one place to ask, and so a list
# parameter added later cannot slip past it.
LIST_PARAMS = frozenset()

# How many entries a list parameter may carry, if one is ever added back: a
# config file may be hand-edited and a page may not be asked to take an
# unbounded one.
MAX_SCORE_POSITIONS = 64

# How far a nest shift may pull an alternated copy. A nest shift closes the
# space between two interlocking boards; a number bigger than a board is not
# one, and this bounds what a posted value can ask the geometry to do.
MAX_NEST_SHIFT_MM = 1000.0

# THE TWO JUMP DISTANCES, bounded at the page as well as in
# the schema so a typed metre is refused by a sentence rather than by a
# validation traceback. The bound is the config model's own (`config.MAX_JUMP_MM`
# ), stated once and fenced against it in the tests.
JUMP_PARAMS = frozenset({"score_jump_horizontal_mm", "score_jump_vertical_mm"})

# THE TWO RAIL SCORE SETBACKS, bounded the same way and for
# the same reason. Whether the rail is actually wide enough for the number is
# the LAYOUT's answer and the build says so by name - this bound only refuses a
# figure no fabrication panel could carry.
SETBACK_PARAMS = frozenset({
    "score_rail_setback_horizontal_mm", "score_rail_setback_vertical_mm",
})

# The four per-side keys, in the order the page shows them.
RAIL_SIDES = ("top", "bottom", "left", "right")
RAIL_PARAMS = tuple(f"rail_{side}_mm" for side in RAIL_SIDES)
# The linked one, which is the same setting said in one number.
RAIL_LINKED_PARAM = "rail_mm"

# THE ONE THAT MOVES NOTHING. A clearance warning is a
# measurement the viewport draws over a panel that already exists: it moves no
# coordinate, so it may not cost a rebuild. The session recomputes the marking
# and repaints (`session.set_param`), and it is excluded from the stepped-panel
# fingerprint so a build cannot happen behind it
# (`panelbuild.VIEW_ONLY_KEYS`). Fenced byte for byte.
VIEW_ONLY_PARAMS = frozenset({"clearance_warning_mm"})

# The ones the PREP computation reads. Everything else in `SETTABLE` changes
# only the panel, so changing it costs no prep recompute and no second render -
# it marks the panel out of date instead (see `session.set_param`).
PREP_PARAMS = frozenset({
    "edge_plating",
    "edge_plating_front_mask", "edge_plating_back_mask", "edge_plating_mask_width_mm",
    "cutter_diameter", "route_past_score_mm", "min_tab_mm",
    "route_along_score_mm", "route_tangent_angle_deg", "route_end_overrides",
    "custom_route_ends",
    "eliminate_slivers",
    # POCKETING CHANGES WHAT PREP WRITES: the
    # concentric passes are toolpath ink in the board, so moving the box runs
    # prep again and the picture shows the cutout milled out or left standing.
    "pocket_cutouts",
    # A BOARD CHANGE: the score keep-outs already on the
    # board were written at the old width, so a new one re-syncs them
    # (`session.set_param`) and the copper is filled against the new answer.
    "score_rule_half_width_mm",
    # A CHANGE TO THE HEAL RE-READS THE OUTLINE: it decides whether the board's
    # outline is a loop at all, so the prep runs again and says what it closed
    # or why it refused.
    "outline_heal_mm",
})

# A count of boards is a whole number. Written as one, too: `"columns": 2.0` in
# a config file is a number nobody typed.
INTEGER_PARAMS = frozenset({"columns", "rows"})

# Sections a parameter needs the config to HAVE ALREADY, and `panel` is now the
# only one: creating it turns panelizing on, and a build changing shape is not a
# number moving. A CARRIER SECTION IS NOT ON THIS LIST: the section is always
# written and `enabled` inside it is the switch, so typing a number into a
# stage that is switched off tunes a setting rather than turning a stage on by
# the back door.
REQUIRES_SECTION = {
    name: (("panel",),) for name in (RAIL_LINKED_PARAM, *RAIL_PARAMS)
}
REQUIRES_SECTION.update(
    {name: (("panel",),) for name in CARRIER_SECTIONS}
)
REQUIRES_SECTION.update({
    name: (("panel",),)
    for name, path in SETTABLE.items()
    if len(path) > 2 and path[0] == "panel"
})
# WHICH GROUP EACH CARRIER FIELD BELONGS TO. Tuning one writes its whole section
# out first (`_materialised`), so a file that never named the group now states
# every number the panel was built from.
SECTION_FIELDS = {
    name: switch
    for name, path in SETTABLE.items()
    for switch in SECTION_SWITCH_INSIDE
    if len(path) > 2 and path[:2] == CARRIER_SECTIONS[switch]
}


class ActionError(Exception):
    """The action cannot be carried out. Nothing was changed."""


class Busy(ActionError):
    """Another action is already running. This one was NOT applied.

    Its own type because it is not the designer asking for something impossible
    - it is the same request arriving twice while the first one is still
    running. The server answers it with 409 rather than 400 and the journal
    records it as a rejection, so a run's history shows the click that happened
    and does not show a change that did not.
    """


class Blade(NamedTuple):
    """A blade line as the page names it: two points, in board millimetres."""

    start: tuple
    end: tuple

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def unit(self) -> tuple:
        length = self.length_mm
        if length <= 0.0:
            raise ActionError("a blade line of zero length names no cut")
        return (
            (self.end[0] - self.start[0]) / length,
            (self.end[1] - self.start[1]) / length,
        )

    def carries(self, point: tuple, tol: float = COLLINEAR_TOL_MM) -> bool:
        """Does *point* lie on this line, however far along it?

        The LINE, not the segment - the same extent-tolerant test the stage
        matches decisions with, so a score drawn short still names its cut.
        """
        unit = self.unit
        offset = (point[0] - self.start[0], point[1] - self.start[1])
        return abs(offset[0] * unit[1] - offset[1] * unit[0]) <= tol


def blade_from(payload: dict) -> Blade:
    """A blade line out of a request body, validated. Raises ActionError."""
    if not isinstance(payload, dict):
        raise ActionError("the request body must be a JSON object")
    points = []
    for key in ("start", "end"):
        value = payload.get(key)
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or not all(isinstance(n, (int, float))
                           and not isinstance(n, bool) for n in value)
                or not all(math.isfinite(float(n)) for n in value)):
            raise ActionError(
                f"'{key}' must be a pair of finite numbers in board millimetres"
            )
        points.append((float(value[0]), float(value[1])))
    blade = Blade(start=points[0], end=points[1])
    if blade.length_mm <= COLLINEAR_TOL_MM:
        raise ActionError("a blade line of zero length names no cut")
    return blade


def matching_candidate(blade: Blade, candidates: list):
    """The candidate whose blade line this is. Raises when none matches.

    A line may lie on more than one candidate - two stretches of edge that one
    saw pass takes - and the stage already treats that as accepting both. So the
    FIRST match writes the line, once: the accepted thing is the cut, and a
    second identical line on the same cut would be one item KiCad shows twice.
    """
    for candidate in candidates:
        if blade.carries(candidate.start) and blade.carries(candidate.end):
            return candidate
    raise ActionError(
        f"no v-score candidate lies on the line from "
        f"({blade.start[0]:.4f}, {blade.start[1]:.4f}) to "
        f"({blade.end[0]:.4f}, {blade.end[1]:.4f}) mm - the outline may have "
        f"changed since this page was drawn. Rerun and decide again."
    )


def board_score_lines(board: Board) -> list:
    """Every board-level `User.38` item, in file order. Footprints are not decisions."""
    return [
        item for item in (*board.drawings, *board.trailing_drawings)
        if route_toolpath.SCORE_LAYER in item_layers(item)
    ]


def _accepts(item, blade: Blade) -> bool:
    """Is this board item a score line lying on *blade*?"""
    if type(item).__name__ != "PcbLine":
        return False
    return blade.carries(item.start) and blade.carries(item.end)


class Written(NamedTuple):
    """What one in-memory edit changed. Counts of board items, and a sentence.

    Nothing reached the disk. `changed` is the one thing the pending count is
    made of: an accept that finds the edge already scored has changed nothing,
    and a save that followed it would write the same bytes either way.
    """

    added: int
    removed: int
    message: str

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _guarded(board: Board, half_width_mm: float) -> board_scores.ScoreKeepouts:
    """Make the board's score keepouts match its score lines. See below.

    **A DECISION WRITES BOTH**: the `User.38` line
    is the acceptance and the keepout is what stops copper, a track or a pad
    sitting where the blade will run. They are written and taken away together,
    through one idempotent sync, so an accept, a second accept of the same edge
    and a decline all leave the board saying one consistent thing.
    """
    return board_scores.sync(board, half_width_mm)


def resync_keepouts(board: Board,
                    half_width_mm: float) -> board_scores.ScoreKeepouts:
    """Redraw the board's score keep-outs at a width that has just changed.

    The same idempotent sync a decision runs (:func:`_guarded`), reached by the
    other route: the user typed a new keep-out width on the prep page. A
    board whose strips already match is not
    touched, so this costs one pass over the zones and no bytes when the number
    landed back where it was.
    """
    return _guarded(board, half_width_mm)


def repair_keepouts(board: Board,
                    half_width_mm: float) -> board_scores.ScoreKeepouts:
    """Make the board's score keep-outs match its score lines AT LOAD.

    **A BOARD IS REPAIRED WHEN IT ARRIVES.** The same sync that a score
    decision and a width change run also runs once on the board the session
    has just read, so a board carrying a `User.38` line and no keep-out is put
    right without the user switching the score off and on again. This is the
    third route into the sync and the only one that fires without a click.

    Bounded exactly as the other two are, and deliberately not widened: it adds
    the keep-out a score line asks for, and removes only a strip this stage
    made - `board_scores.ours`, by name AND by shape - that no line still asks
    for. A designer's own keep-out of another name or another shape is not
    touched.

    In memory. Nothing is written: the repair is a pending change like any
    other, and `Save to Board` is what puts it on the file.
    """
    return _guarded(board, half_width_mm)


def keepout_repair_said(keepouts: board_scores.ScoreKeepouts) -> str:
    """What the load-time repair found, as the one line the prep page shows.

    Said ONLY when something was not intact (the caller checks `changed`): a
    board that arrived agreeing with itself is the ordinary case and owes the
    reader nothing. It names the pairs that loaded intact as well as the ones
    that moved, because "1 repaired" alone does not say how much of the board
    was already right.
    """
    intact = max(keepouts.total - keepouts.added, 0)
    parts = []
    if keepouts.added:
        parts.append(f"{keepouts.added} repaired")
    if keepouts.removed:
        parts.append(f"{keepouts.removed} orphan(s) cleared")
    return (
        f"score keep-outs on load: {intact} of {keepouts.total} pair(s) intact, "
        f"{' and '.join(parts)} at {keepouts.half_width_mm:g} mm either side - "
        f"held as an unsaved change until Save to Board"
    )


def keepout_resync_said(keepouts: board_scores.ScoreKeepouts) -> str:
    """What a width change did to the board, as one sentence."""
    return (
        f"{keepouts.total} score keep-out(s) redrawn at "
        f"{keepouts.half_width_mm:g} mm either side"
    )


def _keepout_said(keepouts: board_scores.ScoreKeepouts) -> str:
    """What the sync did, in the one clause a status line has room for."""
    if not keepouts.changed:
        return ""
    parts = []
    if keepouts.added:
        parts.append(f"{keepouts.added} copper keepout(s) added")
    if keepouts.removed:
        parts.append(f"{keepouts.removed} removed")
    return (
        f", {' and '.join(parts)} at "
        f"{keepouts.half_width_mm:g} mm either side"
    )


def accept(board: Board, blade: Blade, candidates: list,
           half_width_mm: float = DEFAULT_RULE_HALF_WIDTH_MM) -> Written:
    """Put the `User.38` line accepting this candidate, AND its keepout, on the board.

    In memory. Nothing is written: the session holds the board and :func:`save`
    is what puts it on disk. Accepting an already-accepted line is a no-op,
    which is what makes a double click harmless - and the keepout sync is
    idempotent for the same reason, so a board that already agrees is not
    touched at all.

    *half_width_mm* is how far either side of the line the keepout reaches, and
    it comes from the config (`board_scores.half_width_of`): one number for the
    board and for the panel, so the panel recognises this keepout as the guard
    already in place instead of drawing a second one over it.
    """
    candidate = matching_candidate(blade, candidates)
    if any(_accepts(item, blade) for item in board_score_lines(board)):
        keepouts = _guarded(board, half_width_mm)
        if not keepouts.changed:
            return Written(
                0, 0, "that edge is already scored - nothing to change"
            )
        # The line was there and its keepout was not - a board scored before
        # this rule existed, or one scored by hand in KiCad. Saying so is the
        # point: the copper is about to move.
        return Written(
            keepouts.added, keepouts.removed,
            f"that edge was already scored{_keepout_said(keepouts)}",
        )

    line = route_toolpath.score_line_for(
        candidate, route_toolpath.outline_bbox(board)
    )
    board.drawings.append(line)
    keepouts = _guarded(board, half_width_mm)
    return Written(
        1 + keepouts.added, keepouts.removed,
        f"scored: one {route_toolpath.SCORE_LAYER} line from "
        f"({line.start[0]:.4f}, {line.start[1]:.4f}) to "
        f"({line.end[0]:.4f}, {line.end[1]:.4f}) mm"
        f"{_keepout_said(keepouts)}",
    )


def decline(board: Board, blade: Blade,
            half_width_mm: float = DEFAULT_RULE_HALF_WIDTH_MM) -> Written:
    """Take the held board's `User.38` lines - and their keepouts - off this line.

    Rejection is ABSENCE under the decision model, so declining is a deletion
    and never a mark. The keepout goes with the line: a guard round a cut nobody
    is making would keep copper out of good material for ever. Declining
    something that was never accepted changes nothing - and says so.
    """
    doomed = [item for item in board_score_lines(board) if _accepts(item, blade)]
    if not doomed:
        keepouts = _guarded(board, half_width_mm)
        if not keepouts.changed:
            return Written(
                0, 0, "that edge was not scored - nothing to change"
            )
        return Written(
            keepouts.added, keepouts.removed,
            f"that edge was not scored{_keepout_said(keepouts)}",
        )

    condemned = {id(item) for item in doomed}
    board.drawings = [
        item for item in board.drawings if id(item) not in condemned
    ]
    board.trailing_drawings = [
        item for item in board.trailing_drawings if id(item) not in condemned
    ]
    keepouts = _guarded(board, half_width_mm)
    plural = "line" if len(doomed) == 1 else "lines"
    return Written(
        keepouts.added, len(doomed) + keepouts.removed,
        f"declined: {len(doomed)} {route_toolpath.SCORE_LAYER} {plural} "
        f"removed{_keepout_said(keepouts)}",
    )


def _place(data: dict, path: tuple, value) -> None:
    """Set a nested key, creating the objects on the way. Order is preserved."""
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _read(data: dict, path: tuple):
    """What the config data says at *path* now, or None when it says nothing."""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def rails_now(data: dict) -> dict:
    """The four rail widths this config data says, whichever way it says them.

    Reading is where the two spellings become one: a number is the linked view
    of four equal rails and an object is the four themselves. Anything else - a
    rail-less config - is None.
    """
    panel = _read(data, ("panel",))
    if not isinstance(panel, dict):
        return None
    rails = panel.get("rails")
    if isinstance(rails, dict):
        return {side: float(rails.get(side, 0.0)) for side in RAIL_SIDES}
    if isinstance(rails, (int, float)) and not isinstance(rails, bool):
        return {side: float(rails) for side in RAIL_SIDES}
    return None


def _reshaped_for_rails(data: dict, name: str) -> dict:
    """*data* with `panel.rails` in the shape *name* is about to write into.

    Two things happen here and each one of them is a rule:

    * setting a PER-SIDE rail on a config that states one number EXPANDS it into
      four equal ones first, so unlocking a rail keeps the other three where the
      user left them rather than defaulting them to nothing;
    * setting the LINKED rail collapses the object back to one number, which is
      what re-linking means.
    """
    panel = data.get("panel")
    if not isinstance(panel, dict):
        return data
    known = rails_now(data)
    if name == RAIL_LINKED_PARAM:
        panel["rails"] = 0.0 if known is None else max(known.values())
    else:
        panel["rails"] = (
            {side: 0.0 for side in RAIL_SIDES} if known is None
            else {side: known[side] for side in RAIL_SIDES}
        )
    return data


class Changed(NamedTuple):
    """One parameter edit: the new config data, the config it parses as, and
    whether the file's number actually moved."""

    data: dict
    config: PanelConfig
    changed: bool


def _coerced(name: str, value):
    """*value* as the config key behind *name* holds it. Raises ActionError.

    One place where a control's type meets a config key's type, so a checkbox
    that posts a number and a dropdown that posts a word the schema has never
    heard of are both refused by NAME rather than as a validation traceback.
    """
    if name == "route_end_overrides":
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or len(k) > 128 or isinstance(v, bool)
            or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
            for k, v in value.items()
        ):
            raise ActionError("Route end overrides need an end identity and a nonnegative distance.")
        return value
    if name in BOOLEAN_PARAMS:
        if not isinstance(value, bool):
            raise ActionError(
                f"'{name}' is a switch, so it takes true or false, not "
                f"{type(value).__name__}"
            )
        return value
    # EMPTY IS A VALUE HERE. The nullable parameters are the lettering
    # heights, where `null` means "size it from the panel" - so it is taken as
    # posted and written to the file, and it is NOT treated as a field nobody
    # filled in.
    if value is None:
        if name in NULLABLE_PARAMS:
            return None
        raise ActionError(
            f"'{name}' takes a number, and an empty one is not a value it can "
            f"be set to"
        )
    if name in CHOICE_PARAMS:
        allowed = CHOICE_PARAMS[name]
        if value not in allowed:
            raise ActionError(
                f"'{name}' takes one of {', '.join(allowed)} - not {value!r}"
            )
        return value
    if name in NAME_PARAMS:
        # A name field's allowed values live on the MACHINE, not here: the
        # dropdown was filled from the library KiCad's settings resolve. So the
        # shape is checked with the config model's own checker and membership is
        # left to the stage, which is the only place that has the library.
        if not isinstance(value, str):
            raise ActionError(
                f"'{name}' takes a footprint name, not {type(value).__name__}"
            )
        if not value:
            return value
        try:
            return checked_name(value)
        except LibraryError as exc:
            raise ActionError(f"'{name}': {exc}") from exc
    if name in LIST_PARAMS:
        if not isinstance(value, list):
            raise ActionError(
                f"'{name}' is a list of millimetre positions, one per line"
            )
        if len(value) > MAX_SCORE_POSITIONS:
            raise ActionError(
                f"{len(value)} score positions is more than this page takes "
                f"({MAX_SCORE_POSITIONS})"
            )
        out = []
        for entry in value:
            if (isinstance(entry, bool) or not isinstance(entry, (int, float))
                    or not math.isfinite(float(entry))):
                raise ActionError(
                    f"'{name}' takes finite millimetre numbers, one per line - "
                    f"{entry!r} is not one"
                )
            out.append(float(entry))
        return out
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionError(f"'{name}' takes a number, not {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ActionError(f"'{name}' takes a finite number")
    if name in NUMBER_CHOICE_PARAMS:
        allowed = NUMBER_CHOICE_PARAMS[name]
        if number not in allowed:
            raise ActionError(
                f"'{name}' takes one of "
                f"{', '.join(f'{value:g}' for value in allowed)} - not "
                f"{number:g}"
            )
        return number
    if name in SIGNED_PARAMS:
        if abs(number) > MAX_NEST_SHIFT_MM:
            raise ActionError(
                f"'{name}' is {number:g} mm, further than the "
                f"{MAX_NEST_SHIFT_MM:g} mm this page takes - a nest shift pulls "
                f"a copy towards its neighbour, not across the panel"
            )
        return number
    if name in JUMP_PARAMS:
        if number < 0.0 or number > MAX_JUMP_MM:
            raise ActionError(
                f"'{name}' is {number:g} mm, and a jump distance runs from 0 to "
                f"{MAX_JUMP_MM:g} mm - it is how far the saw stops short of the "
                f"panel edge, not a coordinate"
            )
        return number
    if name in SETBACK_PARAMS:
        if number < 0.0 or number > MAX_SETBACK_MM:
            raise ActionError(
                f"'{name}' is {number:g} mm, and a rail score setback runs from "
                f"0 to {MAX_SETBACK_MM:g} mm - it is how much plain material "
                f"the panel grows by on each side, not a coordinate"
            )
        return number
    if name in TEXT_HEIGHT_PARAMS:
        if number <= 0.0 or number > MAX_TEXT_HEIGHT_MM:
            raise ActionError(
                f"'{name}' is {number:g} mm, and lettering runs from just above "
                f"0 to {MAX_TEXT_HEIGHT_MM:g} mm - it is how tall the numbers "
                f"on the drawing are. Clear the field to have the panel size "
                f"them itself"
            )
        return number
    if name == "edge_plating_mask_width_mm":
        if number <= 0.0 or number > MAX_EDGE_PLATING_MASK_WIDTH_MM:
            raise ActionError(
                f"Mask width must be greater than zero and at most "
                f"{MAX_EDGE_PLATING_MASK_WIDTH_MM:g} mm."
            )
        return number
    if name in INTEGER_PARAMS:
        if number != int(number):
            raise ActionError(
                f"'{name}' counts boards, so it takes a whole number, not "
                f"{value}"
            )
        number = int(number)
    return number


def _needs_sections(data: dict, name: str) -> None:
    """Refuse a parameter whose config section is not there. Says which switch."""
    for section in REQUIRES_SECTION.get(name, ()):
        if isinstance(_read(data, section), dict):
            continue
        if section == ("panel",):
            raise ActionError(
                "this config has no 'panel' section, so it does not panelize "
                "at all - and typing a number into a field is not how a build "
                "changes shape. Add the section to panel.json."
            )
        switch = next(
            (key for key, path in CARRIER_SECTIONS.items() if path == section),
            "",
        )
        raise ActionError(
            f"this config has no '{'.'.join(section)}' section, so that stage "
            f"does not run - switch the group on first"
            + (f" ({switch})" if switch else "") + "."
        )


def _switched(data: dict, name: str, on: bool) -> tuple:
    """*data* with a carrier section added or removed. `(data, did it move)`.

    THE STAGE SWITCH, AND IT IS A FIELD IN THE SECTION, so whether a stage is
    on is always visible in the file. The section is written out with the
    model's own defaults the first time it is needed, so the file states every
    number the panel was built from, and `enabled` carries the answer.
    """
    path = CARRIER_SECTIONS[name]
    parent = _read(data, path[:-1])
    key = path[-1]
    section = parent.get(key) if isinstance(parent, dict) else None
    # A section written as `null` says nothing, so it is replaced rather than
    # reached into. What it would have meant is the model's own default.
    shipped = bool(SECTION_DEFAULTS[name]().enabled)
    was = (bool(section.get("enabled", shipped))
           if isinstance(section, dict) else shipped)
    if not isinstance(section, dict):
        section = _section_defaults(name)
        parent[key] = section
        section["enabled"] = on
        # Writing the section IS a change to the file even when the switch
        # landed where it already was: the panel's numbers are stated now.
        return data, True
    if was == on:
        return data, False
    section["enabled"] = on
    return data, True


def _section_defaults(name: str) -> dict:
    """The section a group writes out the first time anything touches it.

    The model's own defaults, except for the fiducials: their footprint has no
    default any code here could know, because the choices come from the library
    KiCad's settings resolve. So the switch reaches for the FIRST fiducial
    footprint that library offers, which is what makes turning the group on give
    the user a panel with targets on it rather than a banner. A machine
    where the library does not resolve writes the empty name, and the stage says
    which library it could not read.
    """
    section = SECTION_DEFAULTS[name]().model_dump(mode="json")
    if name != "fiducials_on" or section.get("footprint"):
        return section
    offered = resolve_cached(
        section.get("library") or DEFAULT_FIDUCIAL_LIBRARY,
        section.get("library_path"),
    ).fiducials
    if offered:
        section["footprint"] = offered[0]
    return section


def _materialised(data: dict, switch: str) -> dict:
    """*data* with a carrier section written out, defaults and all.

    A stage has numbers whether or not the file names them, so the first time
    one is tuned the whole section is stated: `panel.json` alone fully
    determines the panel, and a later change to a default must not move a
    panel that was already approved.
    """
    path = CARRIER_SECTIONS[switch]
    parent = _read(data, path[:-1])
    if not isinstance(parent, dict):
        return data
    if not isinstance(parent.get(path[-1]), dict):
        parent[path[-1]] = _section_defaults(switch)
    return data


def _stated(data: dict, path: tuple, model) -> dict:
    """*data* with a switchless section written out, defaults and all.

    The same rule :func:`_materialised` applies to a carrier group, for a group
    that has no switch on the page, such as the dimensions. The
    defaults come from the config model, which is the only place that knows
    them.
    """
    parent = _read(data, path[:-1])
    if not isinstance(parent, dict):
        return data
    if not isinstance(parent.get(path[-1]), dict):
        parent[path[-1]] = model().model_dump(mode="json")
    return data


def _clear_placement(data: dict, config_path: Path, value) -> Changed:
    """Take the placement file out of the held config. Only `false` does this.

    Clearing it hands the arrangement back to the pattern controls exactly as
    the config already states them: rotation, alternation, the nest shifts and
    the gaps are all still there and were never overwritten while the file was
    active (the file DISABLES them, it does not replace
    them).
    """
    if not isinstance(value, bool):
        raise ActionError(
            f"'{PLACEMENT_SWITCH}' is a switch, so it takes true or false, not "
            f"{type(value).__name__}"
        )
    if value:
        raise ActionError(
            "a placement file cannot be switched on: it has to be picked, "
            "because the rows come from the file and nothing else has them. "
            "Use the placement file picker."
        )
    grid = _read(data, ("grid",))
    if not isinstance(grid, dict) or grid.get("placement") is None:
        return Changed(data=data, config=validate(data, config_path),
                       changed=False)
    edited = copy.deepcopy(data)
    edited["grid"].pop("placement", None)
    return Changed(data=edited, config=validate(edited, config_path),
                   changed=True)


def set_param(data: dict, config_path: Path, name: str, value) -> Changed:
    """Put one value into the held config DATA and return what it now says.

    Validated through the config model before anything is kept, so a value the
    schema refuses leaves the held data as it was: the config is half of "same
    input, same output" and a half-edited one builds a wrong panel silently.
    Nothing is written here - :func:`save` does that.
    """
    if name == PLACEMENT_SWITCH:
        return _clear_placement(data, config_path, value)
    if name not in SETTABLE and name not in CARRIER_SECTIONS:
        raise ActionError(
            f"'{name}' is not a preview parameter - "
            f"try {', '.join(sorted((*SETTABLE, *CARRIER_SECTIONS, PLACEMENT_SWITCH)))}"
        )
    wanted = _coerced(name, value)
    _needs_sections(data, name)

    if name in CARRIER_SECTIONS:
        edited, moved = _switched(copy.deepcopy(data), name, wanted)
        return Changed(data=edited, config=validate(edited, config_path),
                       changed=moved)

    path = SETTABLE[name]
    edited = copy.deepcopy(data)
    if name in (RAIL_LINKED_PARAM, *RAIL_PARAMS):
        # A rail edit may change the SHAPE of `panel.rails` as well as a number
        # in it - unlocking or re-linking - and the shape is what says whether
        # the sides are linked. So the whole
        # section decides whether anything moved: an unlock that keeps every
        # width is still a change the file has to carry.
        edited = _reshaped_for_rails(edited, name)
        _place(edited, path, wanted)
        moved = _read(data, ("panel",)) != _read(edited, ("panel",))
    elif name in SECTION_FIELDS:
        # Tuning a stage that is already running. The section is written out in
        # full first, so a file that never named it now states every number the
        # panel is built from - and the whole section decides whether anything
        # moved, because writing the defaults IS a change to the file.
        section = CARRIER_SECTIONS[SECTION_FIELDS[name]]
        edited = _materialised(edited, SECTION_FIELDS[name])
        _place(edited, path, wanted)
        moved = _read(data, section) != _read(edited, section)
    elif name in STATED_SECTIONS:
        # A group with no switch on the page, stated in full the first time its
        # field is tuned. Same rule, same reason, and the
        # whole section decides whether anything moved: writing the defaults IS
        # a change to the file.
        section, model = STATED_SECTIONS[name]
        edited = _stated(edited, section, model)
        _place(edited, path, wanted)
        moved = _read(data, section) != _read(edited, section)
    else:
        before = _read(data, path)
        _place(edited, path, wanted)
        moved = before != wanted
    config = validate(edited, config_path)
    return Changed(data=edited, config=config, changed=moved)


def validate(data: dict, config_path: Path) -> PanelConfig:
    """Validate a config DICT against the schema. Raises :class:`ActionError`.

    Separate from :func:`set_param` so a candidate value is refused BEFORE
    anything is kept. The path checks `load_config` also makes are not repeated:
    this is an edit of a config that already loaded, and the keys it can reach
    do not name files.
    """
    try:
        return PanelConfig(**data, config_path=Path(config_path))
    except ValidationError as exc:
        raise ActionError(f"the config would be invalid:\n{exc}") from exc


# -- THE IMPORT: another project's settings, this project's identity ---------
#
# An import fills in every setting from another project's config and leaves
# every field editable afterwards.
#
# So an import is a SET_PARAM OF EVERY KEY AT ONCE, and it obeys every rule one
# of those obeys: the value lands in the held data, it is validated through the
# config model before anything is kept, the pending count moves, and nothing
# reaches the disk until the page's own save. A second import overwrites the
# first, and every field stays typeable afterwards.
#
# WHAT DOES NOT IMPORT IS IDENTITY. `source_board` names THIS project's design;
# taking another project's path would point the session at another project's
# board, which is not a setting changing but a session becoming a different one.
# It is the only key in `PanelConfig` that names a file: every output path is
# derived from where `panel.json` itself lives (`config.panel_dir`), and
# `config_path` is not a config key at all - `load_config` refuses a file that
# carries one, and so does this.
#
# EVERY OTHER TOP-LEVEL KEY IS REPLACED WHOLESALE, present or absent. `stage`,
# `grid`, `min_tab_mm`, `exclude_layers`, `review`, `verify`, `prep` and `panel`
# come from the imported file, and a key the imported file does NOT carry is
# taken out rather than kept from before. That is the only merge that makes the
# result predictable: after an import the panel is exactly the panel the
# imported file describes, built over this project's board, which is a sentence
# a user can check and a test can assert byte for byte.
IDENTITY_KEYS = frozenset({"source_board"})

# ...and what is not a setting at all: the revision and the version line this
# tool stamps on every write. They are replaced
# rather than imported, and they are never counted as a change.
STAMP_KEYS = frozenset({REVISION_KEY, WRITTEN_BY_KEY})

# The keys the PREP computation is drawn from, as a config file spells them.
# `PREP_PARAMS` is the same answer for one typed field; this is the answer for a
# whole file arriving at once, so it names the SECTION rather than the three
# fields inside it - an imported `vscore.detect` moves prep as surely as an
# imported cutter diameter does.
PREP_SECTIONS = ("prep", "min_tab_mm", "eliminate_slivers",
                 "outline_heal_mm", "pocket_cutouts", "edge_plating",
                 "edge_plating_front_mask", "edge_plating_back_mask",
                 "edge_plating_mask_width_mm")

# How long a posted file name may be, and it is only ever DISPLAYED: the journal
# records it, the status line says it, and nothing opens it. The path is dropped
# to its last component all the same - a browser sends `C:\fakepath\panel.json`
# on some platforms, and a name is not a location.
MAX_IMPORT_NAME_CHARS = 120

# Told apart from a key written as `null`, which is a value the file states.
_ABSENT = object()


class Imported(NamedTuple):
    """One settings file, read and merged into the held config. Nothing written.

    `keys` is which top-level settings the import moved, in the order a report
    reads them: it is what the journal records and what the status line says, so
    an import that changed two things does not claim to have changed eight.
    """

    name: str
    data: dict
    config: PanelConfig
    keys: tuple
    settings: dict
    prep_moved: bool

    @property
    def changed(self) -> bool:
        return bool(self.keys)


def import_name(value) -> str:
    """The name of the posted file, as something safe to print. Never a path."""
    if not isinstance(value, str) or not value.strip():
        return "the imported file"
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable())
    return name[:MAX_IMPORT_NAME_CHARS] or "the imported file"


def parse_settings(content: str, name: str) -> dict:
    """The posted bytes as a config object, or an ActionError saying why not.

    Exactly the checks :func:`~eedesignpanels.config.load_config` makes before it
    reaches the model, because this IS a config being loaded - it just arrived
    over a socket instead of off a disk. A config is untrusted, hand-editable
    input, and a file that does not parse changes nothing.
    """
    try:
        settings = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ActionError(
            f"{name} is not valid JSON, so nothing was changed: {exc}"
        ) from exc
    if not isinstance(settings, dict):
        raise ActionError(
            f"{name} is not a panel config: its top level must be a JSON "
            f"object, not {type(settings).__name__}. Nothing was changed."
        )
    if "config_path" in settings:
        raise ActionError(
            f"{name} carries a 'config_path' key, which is not a config "
            f"setting - it is where a config file lives, and this session "
            f"already knows where its own is. Nothing was changed."
        )
    # SAID BY REVISION, NOT BY FIELD. An imported
    # file from another revision is refused the way a launch refuses one: by
    # naming the file's revision and this tool's, so a colleague's newer
    # settings do not read as a corrupt file.
    problem = revision_problem(settings)
    if problem:
        raise ActionError(
            f"{name} was not written by this version of eedesignpanels - "
            f"{problem}. Nothing was changed."
        )
    return settings


def merged_settings(data: dict, settings: dict) -> dict:
    """*data*'s identity, plus everything *settings* says. See "THE IMPORT".

    The revision and the writing version are THIS tool's stamp rather than
    settings, so they are re-stamped rather than
    imported: an import from a colleague's file must not report that the
    version line changed, because nothing about the panel did.
    """
    merged = {
        key: copy.deepcopy(value)
        for key, value in data.items() if key in IDENTITY_KEYS
    }
    for key, value in settings.items():
        if key in IDENTITY_KEYS:
            continue
        merged[key] = copy.deepcopy(value)
    return stamped(merged)


def moved_keys(before: dict, after: dict) -> tuple:
    """Which top-level settings differ, in the order a config file states them.

    The imported file's own order first, so a report reads the way the file
    does, and anything only the old data had after it - those are the settings
    the import took away, and they are changes like any other.

    THE REVISION AND THE VERSION LINE ARE NOT SETTINGS: they say which tool
    wrote the file, they are re-stamped on
    every write, and an import that reported them would claim a change the
    panel never saw.
    """
    order = list(after) + [key for key in before if key not in after]
    return tuple(
        key for key in order
        if key not in STAMP_KEYS
        and before.get(key, _ABSENT) != after.get(key, _ABSENT)
    )


def import_settings(data: dict, config_path: Path, payload: dict) -> Imported:
    """Read a posted settings file into the held config DATA. Writes nothing.

    The browser posts the file's CONTENT and its name, because a browser does
    not expose a path, and typing a path by hand is slow and easy to get
    wrong.

    Refuses, with the reason and nothing changed:

    * a body with no content in it;
    * content that is not valid JSON, or not a JSON object;
    * settings the config model will not validate - the merged config is put
      through :func:`validate`, so a file that would build a wrong panel is
      refused before any of it is kept;
    * settings with no `prep` section, which is what a launch refuses too
      (`eedesignpanels.preview.__main__`): with no declared cutter diameter this
      session could not prepare the board, and step 1 would go dark.
    """
    if not isinstance(payload, dict):
        raise ActionError("the request body must be a JSON object")
    name = import_name(payload.get("name"))
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ActionError(
            f"no settings arrived with {name} - the page posts the file's own "
            f"contents, and this request carried none"
        )

    settings = parse_settings(content, name)
    merged = merged_settings(data, settings)
    try:
        config = validate(merged, config_path)
    except ActionError as exc:
        raise ActionError(f"{name} was refused - {exc}") from exc
    if config.prep is None:
        raise ActionError(
            f"{name} has no 'prep' section, so it declares no cutter diameter "
            f"and this session could not prepare the board with it. Nothing "
            f"was changed."
        )
    return Imported(
        name=name,
        data=merged,
        config=config,
        keys=moved_keys(data, merged),
        settings=settings,
        prep_moved=any(
            _read(data, (key,)) != _read(merged, (key,))
            for key in PREP_SECTIONS
        ),
    )


# -- THE PLACEMENT FILE: an input form, not a stored reference ---------------
#
# The picker gives the page a File and no path - browsers do
# not expose one - so the bytes are posted, exactly as an imported settings file
# is. What is KEPT is the ROWS: the config alone has to reproduce the panel, and
# a config pointing at a CSV somewhere on a machine builds a different panel the
# day that file moves. The file's NAME rides along for the record and nothing
# ever opens it.


class PlacedByFile(NamedTuple):
    """One placement file, read and put into the held config. Nothing written."""

    name: str
    data: dict
    config: PanelConfig
    rows: tuple
    skipped: int
    header: str

    @property
    def changed(self) -> bool:
        return True


def import_placement(data: dict, config_path: Path, payload: dict) -> PlacedByFile:
    """Read a posted placement CSV into the held config DATA. Writes nothing.

    Refuses, with the reason and NOTHING CHANGED:

    * a body with no content in it;
    * a file `eedesignpanels.placementfile` will not parse - a non-number, a row
      of the wrong width, an angle that is not a quarter turn, an offset off the
      end of any machine, more rows than the page takes;
    * a row count that is not this grid's cell count. One line per copy, and the
      cell is what every copy's uuids are derived from, so the two have to agree.
    """
    if not isinstance(payload, dict):
        raise ActionError("the request body must be a JSON object")
    name = placementfile.file_name(payload.get("name"))
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ActionError(
            f"no placements arrived with {name} - the page posts the file's own "
            f"contents, and this request carried none"
        )
    try:
        parsed = placementfile.parse(content)
    except placementfile.PlacementFileError as exc:
        raise ActionError(f"{name} was refused - {exc}") from exc

    grid = _read(data, ("grid",))
    columns = grid.get("columns") if isinstance(grid, dict) else None
    rows = grid.get("rows") if isinstance(grid, dict) else None
    if isinstance(columns, int) and isinstance(rows, int):
        wanted = columns * rows
        if len(parsed.rows) != wanted:
            raise ActionError(
                f"{name} carries {len(parsed.rows)} placement(s) and this grid "
                f"asks for {columns} x {rows} = {wanted}. One line per copy - "
                f"change the columns and rows, or the file. Nothing was changed."
            )

    edited = copy.deepcopy(data)
    edited.setdefault("grid", {})["placement"] = {
        "source": name,
        "rows": [
            {"across_mm": row.across_mm, "down_mm": row.down_mm,
             "angle_deg": row.angle_deg}
            for row in parsed.rows
        ],
    }
    try:
        config = validate(edited, config_path)
    except ActionError as exc:
        raise ActionError(f"{name} was refused - {exc}") from exc
    return PlacedByFile(
        name=name, data=edited, config=config,
        rows=parsed.triples, skipped=parsed.skipped, header=parsed.header,
    )


def config_bytes(data: dict) -> str:
    """The config file's text for this data. One place, so a save and a test agree.

    The config module's own writer, so a saved file carries the revision and
    the version that wrote it in its first two lines, whichever door the save
    came through.
    """
    return config_text(data)


class Saved(NamedTuple):
    """What one save put on disk."""

    board_path: Path
    config_path: Path
    wrote_config: bool
    changes: int
    message: str
    backup_path: Path = None


# THE PREVIOUS DESIGN IS KEPT BESIDE IT. A save replaces the person's own board
# file, so the bytes it replaces are copied first to `<board file>-bak`. One
# copy, the newest save's previous state. The suffix is the one older KiCad
# versions gave their own backup copies, so a reader knows what it is, and no
# `*.kicad_pcb` search finds it.
BACKUP_SUFFIX = "-bak"


def backup_path_for(board_path: Path) -> Path:
    """Where the save keeps the board's previous bytes."""
    board_path = Path(board_path)
    return board_path.with_name(board_path.name + BACKUP_SUFFIX)


def _replace_file(target: Path, data: bytes) -> None:
    """Write *data* to *target* through a temporary file in the same folder.

    The temporary file is complete and flushed before one `os.replace` puts it
    in place, so a crash or a full disk leaves either the old file or the new
    one, never half of one.
    """
    target = Path(target)
    handle, name = tempfile.mkstemp(dir=target.parent,
                                    prefix=f".{target.name}.", suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _write_board_safely(board: Board, board_path: Path) -> Path:
    """Replace the design file with *board*, keeping its previous bytes.

    1. Read the file as it is now.
    2. Keep those bytes as the backup beside it (:func:`backup_path_for`).
    3. eedesigncore's guarded save serializes the board, parses the new text
       to check that it loads, writes it to a temporary file in the same
       folder, and replaces the design file with it in one step.

    Any failure leaves the design file as it was, and says why.
    """
    try:
        source = SourceSnapshot.capture(board_path)
    except (OSError, ValueError) as exc:
        raise ActionError(
            f"the design file {Path(board_path).name} cannot be read before "
            f"the save, so nothing was written: {exc}"
        ) from exc
    backup = backup_path_for(source.path)
    try:
        _replace_file(backup, source.content)
    except OSError as exc:
        raise ActionError(
            f"the backup copy {backup.name} cannot be written, so the design "
            f"was not saved: {exc}"
        ) from exc
    try:
        save_guarded_documents([GuardedDocument(source, board)])
    except GuardedSaveError as exc:
        raise ActionError(
            f"the design was not saved and {source.path.name} is unchanged: "
            f"{exc}. The previous copy is also at {backup.name}"
        ) from exc
    return backup


def _locked_save_copy(board: Board) -> Board:
    """Lock supported board items without changing the live preview."""
    from pydantic import BaseModel

    saved = board.model_copy(deep=True)
    seen = set()

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, BaseModel):
            fields = type(value).model_fields
            if "locked" in fields:
                value.locked = True
            for name in fields:
                visit(getattr(value, name))
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(saved)
    return saved


def board_for_save(board: Board, data: dict) -> Board:
    """Apply save settings to a private copy for writing or comparison."""
    from eedesignpanels.stages.edge_plating import sync_board_setting

    saved = (_locked_save_copy(board) if data.get("lock_board_on_save", True)
             else board.model_copy(deep=True))
    sync_board_setting(saved)
    return saved


def save(board: Board, board_path: Path, data: dict, config_path: Path,
         changes: int = 0, write_config: bool = False) -> Saved:
    """THE write. The held board, and the held config when a number moved.

    One action, so a design and the config that describes it never land half a
    session apart. The config is written only when a parameter actually changed:
    rewriting a file nobody edited would reformat a hand-written config for
    nothing, and a config may well be hand-written.

    THE DESIGN FILE IS REPLACED SAFELY (:func:`_write_board_safely`). Its
    previous bytes are kept beside it, the new text is checked to load before
    it is written, and the write goes through a temporary file. The config goes
    through a temporary file too.
    """
    board_path = Path(board_path)
    config_path = Path(config_path)
    board = board_for_save(board, data)
    backup = _write_board_safely(board, board_path)
    if write_config:
        # The same bytes `write_text` wrote before: text mode turns each
        # newline into the platform's line ending.
        text = config_bytes(data).replace("\n", os.linesep)
        _replace_file(config_path, text.encode("utf-8"))

    scores = len(board_score_lines(board))
    keepouts = len(board_scores.keepouts_on(board))
    toolpath = len(list(
        item for item in (*board.drawings, *board.trailing_drawings)
        if route_toolpath.TOOLPATH_LAYER in item_layers(item)
    ))
    tail = " and panel.json" if write_config else ""
    counted = "change" if changes == 1 else "changes"
    return Saved(
        board_path=board_path,
        config_path=config_path,
        wrote_config=write_config,
        changes=changes,
        message=(
            f"saved to {board_path.name}{tail}: {changes} {counted}, "
            f"{scores} {route_toolpath.SCORE_LAYER} line(s), {keepouts} score "
            f"keepout(s) and {toolpath} "
            f"{route_toolpath.TOOLPATH_LAYER} item(s) on the board. The "
            f"previous design is kept as {backup.name}"
        ),
        backup_path=backup,
    )
