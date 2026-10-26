"""Step 2's page: the panel, one viewport, and the numbers that derive it.

Built from the same parts as the prep page (:mod:`page_served`) - the same
viewport, the same layer panel on the right, the same busy popup, the same
interaction grammar - so a designer who can read one can read the other. What
differs is the third column and the one function the shared script calls into:

* IMPORT SETTINGS - one native file picker that pre-populates every group under
  it from another project's `panel.json`, keeping this project's own board.
  It posts the file's CONTENT, because a browser does not hand a page a path,
  and the server refuses a file that will not validate;
* LAYOUT - the grid and its gaps - and then the five carrier groups: RAILS,
  TOOLING HOLES, FIDUCIALS, COPPER FILL and PANEL SCORES, each at the column's
  own level and each a millimetre number held in memory;
* **each carrier group's switch IS its stage**:
  it adds that stage's section to `panel.json` or takes it out, and a stage
  whose section is absent does not run at all;
* **Build Panel**, which derives the panel from what the session holds and
  refreshes the picture;
* **Save Panel Files**, which writes the `panel/` folder - board, project, and
  the config when a number moved;
* the CHECK, which compares the SAVED panel against its source and is refused
  with a reason until there is something saved to compare.

**There is no source-board viewport here.** Step 1 is where the board is
looked at; this page is about the panel, and a second small picture of a board
the user has just come from adds nothing.

**A parameter change RECALCULATES.** The in-memory build measures about 1.1 s,
so a layout or rail edit derives the panel again on entry, behind the busy
popup, and the picture is never a picture of numbers that have moved. Build
Panel remains as the manual re-derive, and the gerber comparison is the one
thing slow enough to keep its own button. The stale banner covers the case a
recalculation could not be made.

**THE POUR FILLS BEHIND THE PICTURE.** A rebuild still paints fast with the
pour's outline empty, because filling is KiCad's own work and costs ~10 s on
the corpus - more than a responsive edit may cost. The trailing upgrade runs
that fill and transplants the copper into the held panel, so the picture
catches up a few seconds later, and Save Panel Files WAITS for a fill in
flight rather than writing an unfilled panel. The page says so under the Copper Fill group, and the save
popup says which wait this is.

**Every group folds, ONE IS OPEN AT A TIME, and the write actions do not
scroll.** Import settings, Layout, Rails, Tooling Holes, Fiducials, Copper
Fill, V-Scores and Checks are collapsible groups at one level - the four with a
switch wear its on/off in their heading, and ALL EIGHT ARE ALWAYS BUILT,
whatever the config carries. The column opens with every one of them shut,
opening one shuts the rest, and what is remembered is which single group was
open. Build Panel, Save Panel Files and Quit Panelizer live in the column's
footer, always on screen. The shared half owns all of it (:mod:`page_served`); this page only
names its groups and turns the accordion on.

**Every entry field is a title, a control and one helper line**, and **no
setting says "sheet"**: a sheet is the KiCad drawing sheet, and the material
standing round the boards is the RAIL.

**A finished build refreshes IN PLACE.** A page reload would trip the
unsaved-work guard and ask the user to confirm leaving the site on every
build. So the page is served with its viewport and its layer panel ALREADY
THERE - an empty stack in the layers the board declares - and a build swaps
each layer's contents and hands the viewport the frame the new panel was
rendered in (`review.page`'s `viewFrames`). The
structure never changes, so there is nothing to reload: a panel two columns
wider is a repaint and a re-fit.
"""

import html
from pathlib import Path

from eedesignpanels import channels, fplib, prep
from eedesignpanels.config import (
    FiducialsConfig,
    PanelDimensionsConfig,
    PanelTextConfig,
    PanelScoresConfig,
    RailCopperConfig,
    ToolingHolesConfig,
)
from eedesignpanels.preview import icons, page_served, panelbuild
from eedesignpanels.review import holes, layerpanel, page
from eedesignpanels.stages import fiducials as fiducials_stage

# The panel page's own half of the script. Spliced between the two shared
# halves by `page_served.page_script`, exactly as `PREP_JS` is.
PANEL_JS = r"""
const staleEl = document.getElementById('pb-stale');
// THE PANEL REPORT AND THE BUILD NOTES ARE NOT ON THE PAGE. Every line the
// server builds still goes into the problem-report zip; these two lookups find
// nothing, which is why `drawReport` writes to the developer console instead.
const reportEl = document.getElementById('pb-report');
const notesEl = document.getElementById('pb-notes');
const checkEl = document.getElementById('pb-check');
const buildEl = document.getElementById('pb-build');
const unlockEl = document.getElementById('pb-rail-unlock');
const importEl = document.getElementById('pb-import');
const placeEl = document.getElementById('pb-placement');
const placeClearEl = document.getElementById('pb-placed-clear');
const persidesEl = document.getElementById('pb-persides');
const linkedEl = document.getElementById('pb-rail-linked');
// EVERY control on this page: element id, parameter name, and KIND. The kind is
// how a value is read and written - a switch is not a number and a named choice
// is neither - so one table fills the page and reads it back.
const LAYOUT_FIELDS = [
  ['pb-columns', 'columns', 'number'],
  ['pb-rows', 'rows', 'number'],
  ['pb-gapx', 'gap_x_mm', 'number'],
  ['pb-gapy', 'gap_y_mm', 'number'],
];
// THE PLACEMENT SUITE. `numchoice` is a dropdown
// whose values are NUMBERS - a turn in degrees - so the page posts the number
// and never the string a select carries.
const PLACEMENT_FIELDS = [
  ['pb-rotation', 'rotation_deg', 'numchoice'],
  ['pb-alternate', 'alternate', 'choice'],
  ['pb-nestx', 'nest_across_mm', 'number'],
  ['pb-nesty', 'nest_down_mm', 'number'],
  ['pb-clearance', 'clearance_warning_mm', 'number'],
];
// Which Layout controls a live placement file takes away.
// Columns and rows are NOT among them: the grid cell is still every copy's
// identity, so the file carries one line per cell and those two say how many.
const PLACEMENT_DISABLES = [
  'pb-gapx', 'pb-gapy', 'pb-rotation', 'pb-alternate', 'pb-nestx', 'pb-nesty',
];
// The rails, in their two spellings. Which one the page reads is which one the
// CONFIG carries - `rails_linked` - so the greyed spelling never posts a number.
const LINKED_RAIL = ['pb-rail', 'rail_mm', 'number'];
const SIDE_RAILS = [
  ['pb-rail-top', 'rail_top_mm', 'number'],
  ['pb-rail-bottom', 'rail_bottom_mm', 'number'],
  ['pb-rail-left', 'rail_left_mm', 'number'],
  ['pb-rail-right', 'rail_right_mm', 'number'],
];
// THE CARRIER STAGES. Each switch is the stage: on adds its section to
// panel.json, off takes it out, and off is a panel byte-identical to the panel
// built before the stage existed.
const CARRIER_GROUPS = [
  ['pb-holes-on', 'tooling_holes_on', 'pb-holes-body'],
  ['pb-fid-on', 'fiducials_on', 'pb-fid-body'],
  ['pb-copper-on', 'rail_copper_on', 'pb-copper-body'],
  ['pb-scores-on', 'scores_on', 'pb-scores-body'],
];
const GROUP_SWITCHES = CARRIER_GROUPS.map(g => [g[0], g[1], 'check']);
const HOLE_FIELDS = [
  ['pb-hole-d', 'hole_diameter_mm', 'number'],
  ['pb-hole-tl', 'hole_top_left', 'check'],
  ['pb-hole-tr', 'hole_top_right', 'check'],
  ['pb-hole-br', 'hole_bottom_right', 'check'],
  ['pb-hole-bl', 'hole_bottom_left', 'check'],
  ['pb-hole-place', 'hole_placement', 'choice'],
];
const HOLE_INSETS = [
  ['pb-hole-x', 'hole_inset_across_mm', 'number'],
  ['pb-hole-y', 'hole_inset_down_mm', 'number'],
];
// THE FIDUCIALS. `name` is a dropdown whose options came
// from the SERVER - the library KiCad's settings resolve - so the page posts
// the footprint name it was offered and never a list of its own.
const FIDUCIAL_FIELDS = [
  ['pb-fid-fp', 'fiducial_footprint', 'name'],
  ['pb-fid-offset', 'fiducial_offset_mm', 'number'],
  ['pb-fid-dir', 'fiducial_direction', 'choice'],
  ['pb-fid-side', 'fiducial_side', 'choice'],
  ['pb-fid-tl', 'fiducial_top_left', 'check'],
  ['pb-fid-tr', 'fiducial_top_right', 'check'],
  ['pb-fid-br', 'fiducial_bottom_right', 'check'],
  ['pb-fid-bl', 'fiducial_bottom_left', 'check'],
];
const COPPER_FIELDS = [
  ['pb-copper-clear', 'rail_copper_clearance_mm', 'number'],
  ['pb-copper-between', 'rail_copper_between', 'check'],
];
// THE V-SCORES: four switches, and not one position. Which families of saw
// pass the panel carries; where each one goes is the layout's answer, computed
// at build time. THE KEEP-OUT WIDTH IS NOT HERE: it is declared on the prep
// page, because accepting a score writes that keep-out into the BOARD, and one
// number serves both.
// FOUR DISTANCE DEPENDANTS ride here, each hanging off one rail score: a
// SETBACK is plain material added between the boards and that rail's score
// line, so the panel grows by twice it and the rail keeps the width that was
// typed; a JUMP DISTANCE stops every score line of its own direction, rail
// scores excepted, short of the panel edge at both ends. Each is a distance
// alone - zero is none, and no switch sits beside it - and the jump belongs to
// the direction its own line runs.
// A THIRD DEPENDANT rides under each rail score, and it is a TICK rather than
// a distance. When that rail score's line falls exactly on a pass the boards
// decided, one saw line carries two meanings - full span as the rail score, or
// stopped short by its direction's jump as a board-edge score - and THE TOOL
// CANNOT TELL WHICH IS WANTED, so the user answers. On by default: full span
// with no jump is what lets a panel come apart.
// LISTED IN THE ORDER THE GROUP SHOWS THEM, which is horizontal before vertical
// except where a dependant has to sit under the rail score that governs it.
const SCORE_FIELDS = [
  ['pb-score-rails-h', 'score_rails_horizontal', 'check'],
  ['pb-score-setback-h', 'score_rail_setback_horizontal_mm', 'number'],
  ['pb-score-jump-v', 'score_jump_vertical_mm', 'number'],
  ['pb-score-shared-h', 'score_shared_line_rail_horizontal', 'check'],
  ['pb-score-rails-v', 'score_rails_vertical', 'check'],
  ['pb-score-setback-v', 'score_rail_setback_vertical_mm', 'number'],
  ['pb-score-jump-h', 'score_jump_horizontal_mm', 'number'],
  ['pb-score-shared-v', 'score_shared_line_rail_vertical', 'check'],
  ['pb-score-inter-row', 'score_inter_row', 'check'],
  ['pb-score-inter-col', 'score_inter_column', 'check'],
];
// THE PANEL'S LETTERING HEIGHT. `optnumber` is
// the one kind whose EMPTY is a value: empty posts `null`, which is the config
// saying "size it from the panel", so the field can always be handed back to
// the automatic size. Every other number field treats empty as nothing typed.
const DIMENSION_FIELDS = [
  ['pb-dim-text', 'dimension_text_height_mm', 'optnumber'],
];
const TEXT_FIELDS = [
  ['pb-text-rails', 'panel_text_rails', 'choice'],
  ['pb-text-height', 'panel_text_height_mm', 'optnumber'],
  ['pb-text-clearance', 'panel_text_clearance_mm', 'number'],
];
const FIELDS = LAYOUT_FIELDS.concat(
  PLACEMENT_FIELDS,
  [LINKED_RAIL], SIDE_RAILS, GROUP_SWITCHES, HOLE_FIELDS, HOLE_INSETS,
  FIDUCIAL_FIELDS, COPPER_FIELDS, SCORE_FIELDS, DIMENSION_FIELDS, TEXT_FIELDS
);

function field(id) { return document.getElementById(id); }

// Which rail fields are live. Linked mode reads one number; unlocked reads
// four; and the fields that are not read are the ones drawn greyed.
function railsLinked() { return PB.params.rails_linked !== false; }

// WHICH PARAMETERS THE PAGE READS RIGHT NOW. A field that is greyed is never
// posted: the locked half of the rails, a carrier stage that is switched off,
// and the fixed insets while the holes are placed by rail corner - in that mode
// the hole sits in the middle of the corner's material and no inset describes
// it. Greyed and not gone, so every one of them is still on screen and still
// readable. Pure, so it can be run and checked on its own.
function livePanelParams(params) {
  const live = ['columns', 'rows'];
  // A LIVE PLACEMENT FILE takes the pattern away: the file
  // says where every copy goes and what angle it takes, so the gaps, the
  // rotation, the alternation and the nest shifts describe nothing and are not
  // read. The clearance warning STAYS live: it measures whatever arrangement is
  // on screen, however the arrangement was made.
  if (!params.placement_on) {
    live.push('gap_x_mm', 'gap_y_mm', 'rotation_deg', 'alternate',
              'nest_across_mm', 'nest_down_mm');
  }
  live.push('clearance_warning_mm');
  if (params.rail_mm == null) return live;
  if (params.rails_linked !== false) live.push('rail_mm');
  else live.push('rail_top_mm', 'rail_bottom_mm', 'rail_left_mm',
                 'rail_right_mm');
  live.push('tooling_holes_on', 'fiducials_on', 'rail_copper_on', 'scores_on');
  if (params.tooling_holes_on) {
    live.push('hole_diameter_mm', 'hole_top_left', 'hole_top_right',
              'hole_bottom_right', 'hole_bottom_left', 'hole_placement');
    if (params.hole_placement === 'fixed_inset')
      live.push('hole_inset_across_mm', 'hole_inset_down_mm');
  }
  if (params.fiducials_on) {
    live.push('fiducial_offset_mm', 'fiducial_direction', 'fiducial_side',
              'fiducial_top_left', 'fiducial_top_right',
              'fiducial_bottom_right', 'fiducial_bottom_left');
    // THE FOOTPRINT IS READ ONLY WHEN THERE IS ONE TO READ. A library that did
    // not resolve leaves the dropdown empty, and an empty dropdown posting an
    // empty name would erase the footprint the file already carries.
    if ((params.fiducial_options || []).length) live.push('fiducial_footprint');
  }
  if (params.rail_copper_on)
    live.push('rail_copper_clearance_mm', 'rail_copper_between');
  // THE LETTERING HEIGHT IS LIVE WHILE THE PANEL HAS DIMENSIONS. There is no
  // switch for it on the page - the panel says how big
  // it is unless a config declines it - so the config's own `enabled` is what
  // decides, and a config that declines them greys the field.
  if (params.dimensions_on) live.push('dimension_text_height_mm');
  if (params.panel_text_available) {
    live.push('panel_text_rails');
    if (params.panel_text_rails !== 'none')
      live.push('panel_text_height_mm', 'panel_text_clearance_mm');
  }
  // A DEPENDANT IS READ ONLY WHILE ITS GOVERNOR IS ON, and the BUILD reaches
  // the same answer - a greyed setting contributes nothing. A setback hangs off
  // the rail score whose passes it moves. A vertical saw line ends at the top
  // and bottom rails, and the score along those two runs horizontally, so the
  // vertical jump is the gap left against the horizontal rail score and means
  // nothing without it; the horizontal jump hangs off the vertical rail score
  // for the mirror reason.
  if (params.scores_on) {
    live.push('score_rails_horizontal', 'score_rails_vertical',
              'score_inter_row', 'score_inter_column');
    // THE SHARED-LINE TICK IS A DEPENDANT TOO: with its rail score off there
    // is no rail line for anything to share, so the question does not arise
    // and the answer is not read.
    if (params.score_rails_horizontal)
      live.push('score_rail_setback_horizontal_mm', 'score_jump_vertical_mm',
                'score_shared_line_rail_horizontal');
    if (params.score_rails_vertical)
      live.push('score_rail_setback_vertical_mm', 'score_jump_horizontal_mm',
                'score_shared_line_rail_vertical');
  }
  return live;
}

// A GREYED SETTING IS NOT A VALUE. The config decides which parameters are
// live; the second test says that a control this page has DISABLED is never
// read, whatever disabled it, so a greyed field on screen never reaches the
// config. It cannot fire while an action is running, because
// `applyPanelParams` refuses to collect while busy.
function liveFields() {
  const live = livePanelParams(PB.params || {});
  return FIELDS.filter(pair => {
    const el = field(pair[0]);
    return !!el && !el.disabled && live.indexOf(pair[1]) >= 0;
  });
}

function readField(pair) {
  const el = field(pair[0]);
  if (!el) return undefined;
  if (pair[2] === 'check') return el.checked;
  if (pair[2] === 'choice') return el.value;
  // A NAME is a string the server offered, and an empty one is not a value: a
  // dropdown with no options would otherwise post "" and clear the config's
  // footprint the first time anything else on the page changed.
  if (pair[2] === 'name') return el.value || undefined;
  const value = parseFloat(el.value);
  // AN EMPTY OPTIONAL NUMBER IS `null`, NOT NOTHING: the
  // lettering height left empty is the config's own way of saying the panel
  // sizes its own text, so it is posted and written like any other value.
  if (pair[2] === 'optnumber') return isFinite(value) ? value : null;
  return isFinite(value) ? value : undefined;
}

// A dropdown holds strings, so a numeric one is written as the string its own
// option carries - `0` for a turn of zero, not `0.0` - or nothing matches and
// the control shows its first option instead of the config's value.
function writeChoiceNumber(el, value) {
  const wanted = String(value);
  for (const option of el.options) {
    if (parseFloat(option.value) === parseFloat(wanted)) {
      el.value = option.value;
      return;
    }
  }
}

// A SWITCH IS WRITTEN EVEN WHEN IT IS FALSE, which a number is not: `false` is
// a value the config states and `null` is a value it does not, and collapsing
// the two would leave a box ticked that the file says is clear.
function writeField(pair, value) {
  const el = field(pair[0]);
  if (!el || document.activeElement === el) return;
  if (pair[2] === 'check') { el.checked = !!value; return; }
  // ...and an optional number is written even when it is `null`, because an
  // empty field is what `null` LOOKS like. Leaving the box
  // alone would show the last number typed over a config that says otherwise.
  if (pair[2] === 'optnumber') { el.value = value == null ? '' : value; return; }
  if (value == null) return;
  if (pair[2] === 'numchoice') { writeChoiceNumber(el, value); return; }
  if (pair[2] === 'name') { writeName(el, value); return; }
  el.value = value;
}

// A NAME DROPDOWN HAS TO BE ABLE TO SHOW WHAT THE FILE SAYS. Its options came
// from the library on this machine, and the config may name a footprint from
// another one - a config written elsewhere, or a library that moved. Setting
// `value` to an option that does not exist silently shows the first option
// instead, so the file's own name is added and marked rather than lost.
function writeName(el, value) {
  const wanted = String(value);
  if (!wanted) return;
  for (const option of el.options) {
    if (option.value === wanted) { el.value = wanted; return; }
  }
  const added = document.createElement('option');
  added.value = wanted;
  added.textContent = wanted + ' (not in this library)';
  el.appendChild(added);
  el.value = wanted;
}

// Whether this config has a carrier at all. `rail_mm` is null exactly when
// `panel.json` carries no panel section, and with no carrier there is nothing
// for any of the five menus to set. They all stay on the page and all read
// greyed, which is how the page shows a config with no carrier.
function hasCarrier() { return (PB.params || {}).rail_mm != null; }

// THE UNLOCK GOVERNS BOTH RAIL BLOCKS, AND NEITHER LEAVES THE PAGE. The block
// that is not live is greyed, keeps its numbers on screen, and has every
// control disabled - so the page still reads exactly one of the two spellings
// into the config. With no carrier the unlock and both
// blocks are greyed together: there are no rails to be linked or unlinked.
function drawRailMode() {
  if (!unlockEl) return;
  const carrier = hasCarrier();
  const linked = railsLinked();
  unlockEl.checked = !linked;
  unlockEl.dataset.off = carrier ? '' : '1';
  unlockEl.disabled = !carrier;
  setDependent(linkedEl, carrier && linked);
  setDependent(persidesEl, carrier && !linked);
}

// A carrier stage's fields are LIVE only while the stage is, and the insets only
// while the holes are placed by a fixed inset. The CONFIG decides both, so a
// switch the user clicked turns its group live when the server has taken it.
//
// A STAGE THAT IS OFF IS GREYED, NOT REMOVED: its fields stay where they are
// with their values readable, disabled so nothing can be typed into them and
// nothing is read out of them.
//
// THE HEADING SAYS WHICH WAY THE SWITCH IS SET, because a
// folded group hides the switch and a stage that is running has to be readable
// either way. The badge is derived from the config, exactly as the switch is.
function drawGroups() {
  const carrier = hasCarrier();
  CARRIER_GROUPS.forEach(group => {
    const on = !!PB.params[group[1]];
    const box = field(group[0]);
    const body = document.getElementById(group[2]);
    if (box) {
      box.checked = on;
      box.dataset.off = carrier ? '' : '1';
      box.disabled = !carrier;
    }
    setDependent(body, carrier && on);
  });
  // THE INSETS ARE GREYED, NOT HIDDEN. Their governor is the Placement
  // dropdown directly above them, and in the other mode they describe nothing:
  // the hole sits in the middle of the material standing in that corner, so a
  // wider rail moves it and no number says where it is. Both numbers stay on
  // screen and stay readable, and disabled is what keeps them out of the
  // config.
  const insets = document.getElementById('pb-hole-insets');
  setDependent(insets, carrier && !!PB.params.tooling_holes_on
                       && PB.params.hole_placement === 'fixed_inset');
  // SIX DEPENDANTS, EACH UNDER ITS OWN GOVERNOR.
  // A setback grows the panel with material between the boards and the rail
  // score directly above it; a jump is the gap left against the rail score the
  // line it shortens runs into; a shared-line tick says which of two meanings
  // one saw line has, because the tool cannot tell. Greyed
  // and locked until that rail score is on, still readable while it is - and
  // the build treats a greyed one as zero, so the picture and the file agree.
  const scores = carrier && !!PB.params.scores_on;
  const railsH = scores && !!PB.params.score_rails_horizontal;
  const railsV = scores && !!PB.params.score_rails_vertical;
  setDependent(document.getElementById('pb-score-setback-h-block'), railsH);
  setDependent(document.getElementById('pb-score-jump-v-block'), railsH);
  setDependent(document.getElementById('pb-score-shared-h-block'), railsH);
  setDependent(document.getElementById('pb-score-setback-v-block'), railsV);
  setDependent(document.getElementById('pb-score-jump-h-block'), railsV);
  setDependent(document.getElementById('pb-score-shared-v-block'), railsV);
  // THE LETTERING FIELD, GREYED WHEN A CONFIG DECLINES THE DIMENSIONS. Its
  // governor is not a control on this page, so the config is read directly -
  // and greyed is what keeps it out of the config.
  setDependent(document.getElementById('pb-dim-block'),
               carrier && !!PB.params.dimensions_on);
  const textAvailable = carrier && !!PB.params.panel_text_available;
  setDependent(document.getElementById('pb-text-block'), textAvailable);
  setDependent(document.getElementById('pb-text-size-block'),
               textAvailable && PB.params.panel_text_rails !== 'none');
  const textWarning = document.getElementById('pb-text-warning');
  if (textWarning) textWarning.textContent = textAvailable ? '' :
    'No rails. Panel text is disabled.';
  drawFiducialLibrary(PB.params);
}

// WHERE THE FOOTPRINT LIST CAME FROM, OR WHY THERE IS NONE.
// The resolver runs on the server, so the page has exactly two things to say:
// how many footprints the library gave, or the sentence naming what was
// missing. Never both, and never silence - an empty dropdown the user cannot
// explain is the failure this line exists to prevent.
//
// THE LIBRARY LINE IS THE COUNT AND THE LIBRARY, AND NOTHING ELSE. The table
// file the library was declared in is a long path the user does not act on.
// The FAILURE line does name the path - when there is no library there IS
// nothing to act on but the path.
//
// THERE IS NO ORIENTATION-PATTERN LINE. The corner tick boxes say which
// corners are asked for, and a line counting them back would only repeat them.
function drawFiducialLibrary(params) {
  const why = document.getElementById('pb-fid-why');
  const from = document.getElementById('pb-fid-from');
  const reason = params.fiducial_reason || '';
  const options = params.fiducial_options || [];
  if (why) {
    why.hidden = !reason;
    why.textContent = reason ? 'No footprints: ' + reason + '.' : '';
  }
  if (from) {
    const library = params.fiducial_library || '';
    from.hidden = !!reason || !library;
    from.textContent = reason || !library ? '' :
      options.length + ' Fiducial footprint(s) in ' + library;
  }
}

// A LIVE PLACEMENT FILE disables every other Layout control except columns and
// rows. Disabled and not hidden: the values are still what
// the config says and still worth reading, they are simply not what puts the
// copies where they are while a file is active. The line above them says so and
// says how to get the pattern back.
// `data-off` is the shared half's own idiom for a control's INTRINSIC disabled
// state: the busy loop re-enables with `busy || dataset.off === '1'`, so a
// control disabled because a file is active is never turned back on by an
// action finishing (`page_served`, "data-off").
function drawPlacementMode() {
  const on = !!PB.params.placement_on;
  PLACEMENT_DISABLES.forEach(id => {
    const el = field(id);
    if (!el) return;
    el.dataset.off = on ? '1' : '';
    el.disabled = on;
  });
  const said = document.getElementById('pb-placed');
  if (said) {
    said.hidden = !on;
    said.textContent = on
      ? 'Placed by ' + (PB.params.placement_source || 'a placement file')
        + ': ' + (PB.params.placement_rows || 0) + ' placements, one per copy. '
        + 'The gaps, rotation, alternation and nest shift are '
        + 'not used while it is active. Clear it to go back to the pattern.'
      : '';
  }
  const clear = document.getElementById('pb-placed-clear');
  if (clear) {
    clear.dataset.off = on ? '' : '1';
    clear.disabled = !on;
  }
}

// THE CLEARANCE MARKING. Drawn by the page, over the plots,
// in BOARD MILLIMETRES - the frame the render seam puts every layer in - so
// there is no transform here and no coordinate of the panel's is touched. The
// distance applies at once: the server re-measures the panel it already has and
// this redraws, with no rebuild and no render (see `session._remeasure_panel`).
function drawClearance(info) {
  const el = document.getElementById('pb-marks');
  if (!el) return;
  const marks = (info && info.marks) || [];
  if (!marks.length) { el.innerHTML = ''; return; }
  const box = String(el.getAttribute('viewBox') || '0 0 1 1')
    .trim().split(/[\s,]+/).map(Number);
  const size = Math.max(box[2], box[3]) || 1;
  const stroke = size * 0.004;
  const font = size * 0.016;
  let body = '';
  marks.forEach(mark => {
    const a = mark.at, b = mark.to;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    body += '<path d="M' + a[0].toFixed(4) + ' ' + a[1].toFixed(4)
          + 'L' + b[0].toFixed(4) + ' ' + b[1].toFixed(4) + '"'
          + ' stroke-width="' + stroke.toFixed(4) + '"/>';
    // A zero-length span is two boards already touching, so the mark is a ring
    // round the place rather than a line nobody can see.
    if (Math.abs(a[0] - b[0]) < 1e-9 && Math.abs(a[1] - b[1]) < 1e-9) {
      body += '<circle cx="' + mx.toFixed(4) + '" cy="' + my.toFixed(4)
            + '" r="' + (font * 0.5).toFixed(4) + '" stroke-width="'
            + stroke.toFixed(4) + '"/>';
    }
    body += '<text x="' + mx.toFixed(4) + '" y="' + (my - stroke).toFixed(4)
          + '" font-size="' + font.toFixed(4) + '" text-anchor="middle"'
          + ' stroke="none" fill="#e8a13a">'
          + mark.gap_mm.toFixed(3) + ' mm</text>';
  });
  el.innerHTML = '<g fill="none" stroke="#e8a13a" stroke-linecap="round"'
    + ' opacity="0.9" font-family="ui-monospace, monospace">' + body + '</g>';
}

// THE STALE BANNER. The page recalculates by itself, so the banner covers two
// cases: the session that has not built a panel yet, and the recalculation
// that could not be made. Said in the reader's own words.
function drawStale(fragment) {
  const reasons = fragment.stale || [];
  const unsaved = fragment.unsaved || '';
  staleEl.textContent = '';
  if (!reasons.length && !unsaved) {
    staleEl.classList.remove('on');
    return;
  }
  if (reasons.length) {
    const head = document.createElement('b');
    head.textContent = 'This picture is not the panel these numbers describe: '
                     + reasons.join('; ') + '. Press Build Panel.';
    staleEl.appendChild(head);
  }
  if (unsaved) {
    const line = document.createElement('div');
    line.className = 'unsaved';
    line.textContent = unsaved + '.';
    staleEl.appendChild(line);
  }
  staleEl.classList.add('on');
}

function drawReport(lines, notes) {
  if (reportEl) {
    reportEl.textContent = '';
    (lines || []).forEach(text => {
      const item = document.createElement('li');
      item.textContent = text;
      reportEl.appendChild(item);
    });
  } else if ((lines || []).length) {
    console.debug('this panel: ' + (lines || []).join(' | '));
  }
  if (notesEl) {
    notesEl.textContent = '';
    (notes || []).forEach(text => {
      const item = document.createElement('li');
      item.textContent = text;
      notesEl.appendChild(item);
    });
  } else if ((notes || []).length) {
    console.debug('build notes: ' + (notes || []).join(' | '));
  }
}

// DOES THE PANEL COME APART. A STATUS and nothing else: green and `comes
// apart`, or red and the count of boards that cannot be freed. It never
// refuses a build, never warns and never blocks a save - a user panelizes
// plenty of things for reasons this page does not know about. Hidden entirely
// until a build has measured one.
function drawBreakout(state) {
  const el = document.getElementById('pb-breakout');
  if (!el) return;
  const said = (state && state.text) || '';
  el.hidden = !said;
  el.querySelector('.txt').textContent = said;
  const dot = el.querySelector('.lp-status');
  dot.className = 'lp-status ' + (state && state.ok ? 'ok' : 'bad');
}

// The comparison needs FILES, so it is dead until there are some - and it says
// which of the two reasons it is dead for rather than merely being grey.
function drawCheck(fragment) {
  const why = fragment.check_reason || '';
  checkEl.dataset.off = why ? '1' : '';
  checkEl.disabled = busy || !!why;
  checkEl.title = why || 'compare the saved panel against its source board';
  const report = document.getElementById('pb-report-link');
  if (report) {
    report.style.display = fragment.check_report ? '' : 'none';
    report.href = '/check/report';
  }
}

function drawPanelParams(params) {
  PB.params = params || {};
  FIELDS.forEach(pair => writeField(pair, PB.params[pair[1]]));
  drawRailMode();
  drawGroups();
  drawPlacementMode();
}

// What the fields say that the config does not. A value the config already
// carries is not a change and costs nothing.
function changedPanelParams() {
  const body = {};
  liveFields().forEach(pair => {
    const value = readField(pair);
    // A value the CONFIG does not state is not one this page may change: the
    // fields it draws come from the config, so a parameter missing from it is
    // a control that has nothing to be different from.
    if (value === undefined || PB.params[pair[1]] === undefined) return;
    if (value !== PB.params[pair[1]]) body[pair[1]] = value;
    // `null` versus `null` is not a change, and JavaScript agrees: the test
    // above is `!==`, not a truth test, so an empty field over an empty config
    // value posts nothing.
  });
  return body;
}

async function applyPanelParams(cause, quiet) {
  if (busy || stopped) return null;
  const body = changedPanelParams();
  if (!Object.keys(body).length) {
    if (!quiet) say('those numbers are already what panel.json says', '');
    return null;
  }
  logEvent('param', Object.assign({cause: cause, page: 'panel'}, body));
  // Apply rail-score dependencies immediately, before the panel rebuild finishes.
  const scoresOn = hasCarrier() && field('pb-scores-on').checked;
  [['pb-score-setback-h-block', 'pb-score-rails-h'],
   ['pb-score-setback-v-block', 'pb-score-rails-v']].forEach(([block, governor]) => {
    setDependent(document.getElementById(block), scoresOn && field(governor).checked);
  });
  return post('/set_param', body, 'recalculating the panel');
}

// THE UNLOCK. One setting, two spellings: unlocking writes
// the four sides the linked value already meant, and re-linking writes the
// widest of the four back over all of them. The shape of `panel.rails` IS the
// mode, so the toggle posts numbers and never a flag.
async function setRailMode(unlocked, cause) {
  if (busy || stopped) { drawRailMode(); return null; }
  const body = {};
  let what;
  if (unlocked) {
    const width = parseFloat(field('pb-rail').value);
    if (!isFinite(width)) { drawRailMode(); return null; }
    SIDE_RAILS.forEach(pair => { body[pair[1]] = width; });
    what = 'unlocking the rails';
  } else {
    let widest = 0;
    SIDE_RAILS.forEach(pair => {
      const value = parseFloat(field(pair[0]).value);
      if (isFinite(value) && value > widest) widest = value;
    });
    body[LINKED_RAIL[1]] = widest;
    what = 'linking the rails';
  }
  logEvent('param', Object.assign({cause: cause, page: 'panel'}, body));
  return post('/set_param', body, what);
}

// A finished build REFRESHES IN PLACE. `post` already fetched the fragment and
// repainted through it - new contents in every layer, and the frame the panel
// was rendered in handed to the viewport - so there is nothing left to do here
// and, above all, nothing to reload: a page reload would raise the browser's
// "leave site?" box on every build.
async function buildPanel(cause) {
  return post('/build_panel', {cause: cause || 'button'}, 'building the panel');
}

// THE POUR FILLS BEHIND THE PICTURE. KiCad fills the rail copper a few
// seconds after each change, and a save with a fill still in flight waits for
// it - so the popup names the wait the user is actually in, read off the
// fragment's own answer rather than guessed from the switch.
async function savePanel(cause) {
  const pouring = !!(PB.params && PB.params.rail_copper_on);
  const waiting = pouring && !PB.railCopperFilled;
  return post('/save_panel', {cause: cause || 'button'},
              waiting
                ? 'waiting for KiCad to finish filling the rail copper, '
                  + 'then writing the panel files'
                : 'writing the panel files');
}

// IMPORT SETTINGS FROM ANOTHER PROJECT. The picker gives a
// File, not a path - browsers do not expose one - so the page reads the bytes
// and posts them. The server validates them through the config model and
// refuses the whole file with a reason, so a bad pick changes nothing.
async function importSettings(file, cause) {
  if (!file) return null;
  let text;
  try {
    text = await file.text();
  } catch (e) {
    // A BROWSER EXCEPTION IS NOT AN ANSWER. The reader failed, and the only
    // thing worth saying is which file and what to try.
    say('that file could not be read - check it is the file you meant and '
        + 'that it is not open in another program', 'bad');
    return null;
  }
  logEvent('import', {cause: cause, page: 'panel', name: file.name,
                      bytes: text.length});
  return post('/import_settings', {name: file.name, content: text},
              'importing the settings from ' + file.name);
}

// THE PLACEMENT FILE. Same shape as the settings import,
// for the same reason: a browser hands the page a File and no path, so the
// bytes are posted and the server refuses a file that will not validate.
async function importPlacement(file, cause) {
  if (!file) return null;
  let text;
  try {
    text = await file.text();
  } catch (e) {
    // A BROWSER EXCEPTION IS NOT AN ANSWER. The reader failed, and the only
    // thing worth saying is which file and what to try.
    say('that file could not be read - check it is the file you meant and '
        + 'that it is not open in another program', 'bad');
    return null;
  }
  logEvent('placement', {cause: cause, page: 'panel', name: file.name,
                         bytes: text.length});
  return post('/import_placement', {name: file.name, content: text},
              'placing every copy from ' + file.name);
}

async function clearPlacement(cause) {
  logEvent('param', {cause: cause, page: 'panel', placement_on: false});
  return post('/set_param', {placement_on: false},
              'clearing the placement file');
}

async function runCheck(cause) {
  const reply = await post('/check', {cause: cause || 'button'},
                           'comparing the saved panel against its source');
  if (reply && reply.ok && reply.report) window.open('/check/report', '_blank');
  return reply;
}

function applyFragment(fragment) {
  // Whether the held panel already carries KiCad's copper: what the save
  // popup reads to say which wait a save is.
  PB.railCopperFilled = !!fragment.rail_copper_filled;
  drawStale(fragment);
  drawCheck(fragment);
  drawPanelParams(fragment.params);
  drawClearance(fragment.clearance);
  if (!fragment.ok) { drawReport([], fragment.notes || []); return; }
  drawReport(fragment.report, fragment.notes);
  drawBreakout(fragment.breakout);
}

buildEl.onclick = () => buildPanel('button');
saveEl.onclick = () => savePanel('button');
checkEl.onclick = () => runCheck('button');
const fingerprintCheck = document.getElementById('pb-fingerprint-check');
if (fingerprintCheck) fingerprintCheck.onclick = () => post('/check_fingerprints', {}, 'checking saved fingerprints');
if (placeClearEl) placeClearEl.onclick = () => clearPlacement('button');
if (placeEl) {
  placeEl.addEventListener('change', () => {
    const file = placeEl.files && placeEl.files[0];
    // CLEARED before the post, exactly as the settings picker is: a refused
    // file leaves the same one selected, and picking it again has to count as a
    // second attempt rather than as no change at all.
    placeEl.value = '';
    importPlacement(file, 'file');
  });
}
if (unlockEl) {
  unlockEl.addEventListener('change',
                            () => setRailMode(unlockEl.checked, 'unlock'));
}
if (importEl) {
  importEl.addEventListener('change', () => {
    const file = importEl.files && importEl.files[0];
    // The picker is CLEARED before the post. A refused import leaves the same
    // file selected, and picking it again has to count as a second attempt
    // rather than as no change at all.
    importEl.value = '';
    importSettings(file, 'file');
  });
}

// EVERY SETTING IS WIRED BY THE SHARED `bindField`, so a dropdown applies when
// it is picked from. The kind in the table says how a value is READ, not WHEN
// it is applied, because that is a property of the control and the shared half
// reads the control. So every kind, the fiducial footprint `name` included,
// applies the same way without a separate test per kind.
FIELDS.forEach(pair => bindField(field(pair[0]), applyPanelParams, pair[1]));

drawStale(PB);
drawCheck(PB);
drawPanelParams(PB.params);
drawClearance(PB.clearance);
drawReport(PB.report, PB.notes);
drawBreakout(PB.breakout);
"""


def fiducial_resolution(config):
    """The fiducial library this config means, resolved - or why it could not be.

    **THE RESOLVER RUNS ON THE SERVER**, because the library tables come from
    the KiCad settings on this machine. The page never reads a
    table and never carries a library path: it is handed the footprint list and,
    when there is none, the sentence naming the table file or the path variable
    that was missing.

    Read on every fragment, which is why it is the cached form: the answer is a
    property of the machine's KiCad settings, and a settings change is a server
    restart (:func:`eedesignpanels.fplib.resolve_cached`).
    """
    section = getattr(getattr(config, "panel", None), "fiducials", None)
    library = (
        section.library if section is not None
        else fplib.DEFAULT_FIDUCIAL_LIBRARY
    )
    pinned = section.library_path if section is not None else None
    return fplib.resolve_cached(library, pinned)


def fiducial_params(panel, resolution) -> dict:
    """The fiducials group, as the held config and the machine now read them.

    Three different questions, and the page needs all three: IS THE STAGE ON,
    what are its numbers, and WHAT MAY THE DROPDOWN OFFER. The third is the one
    that is not in the config at all - it is the library's answer - so it
    travels with the other two rather than through a second endpoint the page
    would have to keep in step.
    """
    shown = panel.fiducial_settings if panel is not None else None
    # A STAGE THAT IS OFF STILL REPORTS ITS NUMBERS, so switching it on fills
    # the fields with what the file already says rather than with blanks. The
    # section is always in the file, so those numbers come from it whichever
    # way the switch is set.
    settings = shown if shown is not None else (
        panel.fiducials if panel is not None else FiducialsConfig()
    )
    return {
        "fiducials_on": shown is not None,
        "fiducial_footprint": settings.footprint,
        "fiducial_library": resolution.library,
        "fiducial_offset_mm": settings.offset_mm,
        "fiducial_direction": settings.direction,
        "fiducial_side": settings.side,
        "fiducial_top_left": settings.corners.top_left,
        "fiducial_top_right": settings.corners.top_right,
        "fiducial_bottom_right": settings.corners.bottom_right,
        "fiducial_bottom_left": settings.corners.bottom_left,
        # THE LIST, AND THE REASON THERE IS NONE. Exactly one of the two is
        # ever filled in, so the group can show the choices or say what stopped
        # it and never both.
        "fiducial_options": list(resolution.fiducials),
        "fiducial_reason": resolution.reason,
        "fiducial_source": (
            "" if resolution.source_table is None
            else str(resolution.source_table)
        ),
        "fiducial_directory": (
            "" if resolution.directory is None else str(resolution.directory)
        ),
        # How a back-side target is made, so the group can say it rather than
        # leaving the user to wonder what "bottom" does to a footprint whose
        # library file is front-side (see `stages.fiducials.place`).
        "fiducial_flip_direction": fiducials_stage.FLIP_DIRECTION,
    }


def carrier_params(panel) -> dict:
    """The three carrier stages, as the held config now reads them.

    Each stage answers two different questions and the page needs both: IS IT ON
    and what would its numbers be. A stage that is off still reports numbers, the
    model's own defaults, so switching it on fills the fields it reveals with the
    values the switch just wrote rather than with blanks.

    "ON" IS THE GROUP'S OWN `enabled`, FOR EVERY STAGE. Each switch reads the
    settings accessor the build acts on, so the page and the panel cannot
    disagree about which stages ran.
    """
    holes = panel.tooling_hole_settings if panel is not None else None
    scores = panel.score_settings if panel is not None else None
    copper = panel.rail_copper_settings if panel is not None else None
    # THE DIMENSIONS, which have no switch on the page: the panel states its
    # own size unless a config declines it, and this accessor is the same one
    # the build acts on - so the page cannot disagree with the panel about
    # whether they were drawn.
    lettering = panel.dimension_settings if panel is not None else None
    # A STAGE THAT IS OFF STILL REPORTS ITS NUMBERS, so switching it on fills
    # the fields with what the file already says rather than with blanks. The
    # section is always in the file, so the numbers come from it whichever way
    # its switch is set.
    shown_holes = panel.tooling_holes if panel is not None else ToolingHolesConfig()
    shown_scores = panel.scores if panel is not None else PanelScoresConfig()
    shown_copper = panel.rail_copper if panel is not None else RailCopperConfig()
    shown_lettering = (panel.dimensions if panel is not None
                       else PanelDimensionsConfig())
    shown_text = panel.text if panel is not None else PanelTextConfig()
    return {
        "tooling_holes_on": holes is not None,
        "hole_diameter_mm": shown_holes.diameter_mm,
        "hole_top_left": shown_holes.corners.top_left,
        "hole_top_right": shown_holes.corners.top_right,
        "hole_bottom_right": shown_holes.corners.bottom_right,
        "hole_bottom_left": shown_holes.corners.bottom_left,
        "hole_placement": shown_holes.placement,
        "hole_inset_across_mm": shown_holes.inset_across_mm,
        "hole_inset_down_mm": shown_holes.inset_down_mm,
        "scores_on": scores is not None,
        "score_rails_horizontal": shown_scores.rails_horizontal,
        "score_rail_setback_horizontal_mm":
            shown_scores.rail_setback_horizontal_mm,
        "score_jump_vertical_mm": shown_scores.jump_vertical_mm,
        "score_shared_line_rail_horizontal":
            shown_scores.shared_line_is_rail_score_horizontal,
        "score_rails_vertical": shown_scores.rails_vertical,
        "score_rail_setback_vertical_mm":
            shown_scores.rail_setback_vertical_mm,
        "score_jump_horizontal_mm": shown_scores.jump_horizontal_mm,
        "score_shared_line_rail_vertical":
            shown_scores.shared_line_is_rail_score_vertical,
        "score_inter_row": shown_scores.inter_row,
        "score_inter_column": shown_scores.inter_column,
        "rail_copper_on": copper is not None,
        "rail_copper_clearance_mm": shown_copper.clearance_mm,
        "rail_copper_between": shown_copper.pour_between_boards,
        # THE PANEL'S OWN DIMENSIONS. `dimensions_on` is NOT a control - there
        # is no switch for it on the page - it is the config's own `enabled`,
        # and the page reads it only to grey the one field. The
        # height travels as the file writes it: a number, or `null` meaning
        # "size it from the panel", which is an empty field on screen.
        "dimensions_on": lettering is not None,
        "dimension_text_height_mm": shown_lettering.text_height_mm,
        "panel_text_rails": shown_text.rails,
        "panel_text_height_mm": shown_text.text_height_mm,
        "panel_text_clearance_mm": shown_text.edge_clearance_mm,
        "panel_text_available": (
            panel is not None and shown_text.enabled
            and any(width > 0.0 for width in panel.rail_widths)
        ),
    }


def _carrier_and_fiducials(config) -> dict:
    """The carrier stages plus the fiducials, which need the machine as well."""
    panel = config.panel
    return {
        **carrier_params(panel),
        **fiducial_params(panel, fiducial_resolution(config)),
    }


def params_json(config) -> dict:
    """The values this page may change, as the held config now reads them.

    The rails arrive BOTH ways - one linked number and four per-side ones - so
    the page can fill whichever fields it is showing without arithmetic of its
    own. `rails_linked` is what the FILE says, and it is the only thing that
    decides which of the two the page reads. The carrier
    stages arrive the same way: a switch saying whether the stage runs, and the
    numbers it would run with.
    """
    grid = config.grid
    panel = config.panel
    rails = channels.Rails.of(panel.rail_widths) if panel is not None else None
    placed = grid.placement
    return {
        "columns": grid.columns,
        "rows": grid.rows,
        "gap_x_mm": grid.gap_across_mm,
        "gap_y_mm": grid.gap_down_mm,
        # THE PLACEMENT SUITE. The placement file
        # arrives as a SWITCH plus a count and a name, never as its rows: the
        # page's job is to say a file is active and how to be rid of it, and
        # three hundred triples in a page's state would be three hundred
        # numbers nobody reads.
        "rotation_deg": grid.rotation_deg,
        "alternate": grid.alternate,
        "nest_across_mm": grid.nest_across_mm,
        "nest_down_mm": grid.nest_down_mm,
        "clearance_warning_mm": grid.clearance_warning_mm,
        "placement_on": placed is not None,
        "placement_rows": len(placed.rows) if placed is not None else 0,
        "placement_source": placed.source if placed is not None else "",
        "rail_mm": rails.linked_mm if rails is not None else None,
        "rail_top_mm": rails.top if rails is not None else None,
        "rail_bottom_mm": rails.bottom if rails is not None else None,
        "rail_left_mm": rails.left if rails is not None else None,
        "rail_right_mm": rails.right if rails is not None else None,
        "rails_linked": panel.rails_linked if panel is not None else True,
        **_carrier_and_fiducials(config),
    }


def _import_html() -> str:
    """GUI: import settings from another project.

    ONE NATIVE PICKER, and no path anywhere. A browser does not hand a page the
    location of a chosen file - it hands over the contents - so the page posts
    the bytes and the server validates them through the config model before any
    of them is kept. The user picks a file and types nothing.

    What it does NOT bring is this project's identity. The board this session is
    preparing stays the board it was preparing; everything else - the layout,
    the rails, every carrier stage, the prep numbers, the minimum tab - is
    pre-populated from the file and stays editable afterwards.
    """
    return (
        page_served.file_field(
            "pb-import", "Settings file",
            "Pick the panel.json of another project. Nothing is saved yet.",
            accept=".json,application/json",
        )
        + '<div class=vd>Every setting is pre-populated and stays editable. '
          'This project keeps its own board file; the imported numbers are '
          'held in memory until Save Panel Files writes them.</div>'
    )


def _placement_html() -> str:
    """How every copy is turned, and where it sits.

    In the LAYOUT group, under the grid and the gaps, because these settings are
    tightly coupled to them and the reference puts them in one group. Every one
    of them is off or zero by default, so a panel that turns nothing is the
    plain grid.

    One of them applies without a rebuild and says so in its helper line: the
    clearance warning is a measurement over the panel already on screen.
    """
    return (
        page_served.choice_field(
            "pb-rotation", "Rotation",
            "Turn every copy by this angle. Upright text is resolved first.",
            (("0", "0 degrees"), ("90", "90 degrees"),
             ("180", "180 degrees"), ("270", "270 degrees")),
        )
        + page_served.choice_field(
            "pb-alternate", "Alternation",
            "Turn every other row or column a half circle so shapes nest.",
            (("none", "none"), ("rows", "every other row"),
             ("columns", "every other column")),
        )
        + page_served.number_field(
            "pb-nestx", "Nest shift across (mm)",
            "Pull the alternated copies sideways. Signed; nothing if none.",
            step="0.1", minimum="-1000",
        )
        + page_served.number_field(
            "pb-nesty", "Nest shift down (mm)",
            "Pull the alternated copies up or down. Signed; nothing if none.",
            step="0.1", minimum="-1000",
        )
        + page_served.number_field(
            "pb-clearance", "Clearance warning (mm)",
            "Mark boards closer than this. Zero is off; nothing rebuilds.",
            step="0.1",
        )
        + page_served.file_field(
            "pb-placement", "Placement file",
            "One CSV: offset across, offset down, angle. One line per copy.",
            accept=".csv,text/csv",
        )
        + '<div class=vd id=pb-placed hidden></div>'
        + '<div class=row>'
          '<button class=btn id=pb-placed-clear data-act=param '
          'title="drop the placement file and go back to the pattern controls">'
          'clear the placement file</button></div>'
    )


def _rails_html(panelizing: bool) -> str:
    """The RAILS group: the unlock, then one rail width, then the four sides.

    ONE design, not a single control plus four. The single
    field IS the linked view of the four values, so exactly one of the two is
    live at a time and the config's own shape decides which.

    BOTH VIEWS STAY ON THE PAGE. A setting that depends on another is greyed,
    never hidden, and its governing setting sits above it. So the unlock sits
    ABOVE the two blocks it governs, each block wears the vertical line that
    marks a dependent setting, and the block that is not live is greyed with its
    numbers still readable. A greyed block's controls are disabled, which is
    what keeps them out of the config.

    First of the five carrier groups, and ALL FIVE ARE ALWAYS BUILT, so every
    setting is on the page to be found. With no carrier the whole set is served
    greyed, and a sentence rides above it saying why.
    """
    absent = "" if panelizing else (
        '<div class=vd>This config has no carrier, so nothing below is in '
        'use yet. Add a rail width to panel.json to panelize.</div>'
    )
    return (
        absent
        + page_served.check_field(
            "pb-rail-unlock", "Set each side",
            "Unlock to give the four rails their own widths.",
        )
        + '<div class="dependent" id=pb-rail-linked>'
        + page_served.number_field(
            "pb-rail", "Rail width (mm)",
            "Bare material round the boards, the same on all four sides.",
            step="0.5",
        )
        + '</div>'
        + '<div class="dependent off" id=pb-persides>'
        + page_served.number_field(
            "pb-rail-top", "Top rail (mm)",
            "Material above the top row. Zero removes it.", step="0.5",
        )
        + page_served.number_field(
            "pb-rail-bottom", "Bottom rail (mm)",
            "Material below the bottom row. Zero removes it.", step="0.5",
        )
        + page_served.number_field(
            "pb-rail-left", "Left rail (mm)",
            "Material left of the first column. Zero removes it.", step="0.5",
        )
        + page_served.number_field(
            "pb-rail-right", "Right rail (mm)",
            "Material right of the last column. Zero removes it.", step="0.5",
        )
        + '</div>'
    )


def _fiducials_html(options) -> str:
    """The optical targets, and where they get their choices.

    THE DROPDOWN IS FILLED BY THE SERVER, which is the whole point of the
    control: the choices are every footprint in the library whose name says
    fiducial, read through KiCad's own settings
    (:mod:`eedesignpanels.fplib`). Nothing here carries a list of footprint
    names.

    A library that did not resolve leaves the dropdown empty and fills
    `pb-fid-why` with the sentence naming what was missing - which table, or
    which path variable. An empty dropdown that says nothing is a control the
    user cannot act on.
    """
    return page_served.group_html(
        "pb-fid", "Fiducials", "pb-fid-on",
        "Panel-level optical targets a placement machine finds the panel by.",
        page_served.choice_field(
            "pb-fid-fp", "Fiducial footprint",
            "Every footprint in the library whose name says fiducial.",
            tuple((name, name) for name in options),
        )
        + '<div class=vd id=pb-fid-why hidden></div>'
        + '<div class=vd id=pb-fid-from hidden></div>'
        + page_served.number_field(
            "pb-fid-offset", "Offset from tooling hole (mm)",
            "How far inward along the rail from that corner's hole.",
            step="0.5", minimum="0.01",
        )
        + page_served.choice_field(
            "pb-fid-dir", "Direction",
            "Which pair of rails the offset runs along.",
            (("along_top_and_bottom_rails", "along top and bottom rails"),
             ("along_left_and_right_rails", "along left and right rails")),
        )
        # KICAD'S OWN LAYER NAMES, AS THE LABELS AND AS THE VALUES, so the
        # page and the board use the same words for the same copper layer.
        # `panel.fiducials.side` carries `F.Cu`, `B.Cu` or `both`, and the
        # stage reads the same three.
        + page_served.choice_field(
            "pb-fid-side", "Copper Layer(s)",
            "Which copper layer carries the targets. B.Cu is turned over.",
            (("F.Cu", "F.Cu"), ("B.Cu", "B.Cu"), ("both", "both")),
        )
        + page_served.check_field(
            "pb-fid-tl", "Top left target",
            "Put a fiducial by the top left tooling hole.",
        )
        + page_served.check_field(
            "pb-fid-tr", "Top right target",
            "Put a fiducial by the top right tooling hole.",
        )
        + page_served.check_field(
            "pb-fid-br", "Bottom right target",
            "Put a fiducial by the bottom right tooling hole.",
        )
        + page_served.check_field(
            "pb-fid-bl", "Bottom left target",
            "Put a fiducial by the bottom left tooling hole.",
        ),
        # NO GROUP NOTE AND NO ORIENTATION-PATTERN LINE. The corner boxes are
        # the setting; nothing narrates them.
        # FIVE TOP-LEVEL GROUPS, ONE OPEN AT A TIME.
        top_level=True,
        collapsed=True,
    )


def _tooling_holes_html() -> str:
    """The holes the panel is fixtured by, and where they go.

    THE TWO INSETS ARE GREYED IN THE OTHER MODE, NOT HIDDEN. Placement, the
    setting that governs them, sits directly above them, they wear the vertical
    line that marks a dependent setting, and in rail-corner mode they are
    greyed with their numbers still readable - in that mode they describe
    nothing, because the hole sits in the middle of the material standing in
    that corner and a wider rail moves it.
    Disabled is what keeps a greyed number out of the config.
    """
    return page_served.group_html(
        "pb-holes", "Tooling Holes", "pb-holes-on",
        "Non-plated holes the fabricator fixtures the panel by.",
        page_served.number_field(
            "pb-hole-d", "Hole diameter (mm)",
            "Finished hole size. 3.175 mm suits this shop fixturing pins.",
            step="0.025", minimum="0.1",
        )
        + page_served.check_field(
            "pb-hole-tl", "Top left hole",
            "Put a hole in the top left corner of the rails.",
        )
        + page_served.check_field(
            "pb-hole-tr", "Top right hole",
            "Put a hole in the top right corner of the rails.",
        )
        + page_served.check_field(
            "pb-hole-br", "Bottom right hole",
            "Put a hole in the bottom right corner of the rails.",
        )
        + page_served.check_field(
            "pb-hole-bl", "Bottom left hole",
            "Put a hole in the bottom left corner of the rails.",
        )
        + page_served.choice_field(
            "pb-hole-place", "Placement",
            "Where in each corner the hole sits.",
            (("rail_corner_center", "rail corner centre"),
             ("fixed_inset", "fixed inset")),
        )
        # Served GREYED, because the config has not been read yet. The script
        # turns it live the moment it knows the placement mode.
        + '<div class="dependent off" id=pb-hole-insets>'
        + page_served.number_field(
            "pb-hole-x", "Inset across (mm)",
            "In from the panel left or right edge. Fixed inset only.",
            step="0.5",
        )
        + page_served.number_field(
            "pb-hole-y", "Inset down (mm)",
            "Down from the panel top or bottom edge. Fixed inset only.",
            step="0.5",
        )
        + '</div>',
        # FIVE TOP-LEVEL GROUPS, ONE OPEN AT A TIME.
        top_level=True,
        collapsed=True,
    )


def _dimensions_html() -> str:
    """The panel's own dimension items: one setting, the lettering height.

    WHY THERE IS A GROUP FOR IT. The other groups are the import, the layout,
    the five carrier stages and the checks - none of them a drawing setting - so
    this is its own group, sitting after the carrier stages because that is the
    order the build runs in: the dimensions are the last thing drawn on the
    panel.

    ONE FIELD, AND EMPTY IS AN ANSWER. Left empty the lettering is sized from
    the panel - the longest extent over 50, floored at 2 mm - which is what the
    field's one line says. A number overrules it, and clearing the field hands
    it back, so the automatic size is always reachable without knowing the
    arithmetic.

    NO SWITCH. The panel states its own size unless a config declines it
    (`panel.dimensions.enabled`). A config that does decline them greys this
    field, because a greyed setting contributes nothing.

    NO UNITS SETTING. A KiCad dimension shows ONE unit - `DIM_UNITS_MODE`, one
    value per item - so a second reading could only be faked through the
    suffix or an override value, which puts a number on the drawing that no
    longer tracks the geometry. Millimetres, like every other number in this
    tool.
    """
    return page_served.section_html(
        "pb-sec-dimensions", "Dimensions",
        '<div id=pb-dim-block>'
        + page_served.number_field(
            "pb-dim-text", "Dimension text height (mm)",
            "Empty sizes the lettering from the panel.",
            step="0.5", minimum="0",
        )
        + '</div>',
        collapsed=True,
    )


def _text_html() -> str:
    """The rail selection and native KiCad text size, with edge clearance."""
    return page_served.section_html(
        "pb-sec-text", "Text",
        '<div id=pb-text-block>'
        + page_served.choice_field(
            "pb-text-rails", "Rails", "Automatic prefers the left vertical rail.",
            (("automatic", "Automatic"), ("left", "Left"), ("right", "Right"),
             ("top", "Top"), ("bottom", "Bottom"),
             ("vertical", "Both vertical"), ("horizontal", "Both horizontal"),
             ("all", "All"), ("none", "None")),
        )
        + '<div id=pb-text-size-block>'
        + page_served.number_field(
            "pb-text-height", "Text height (mm)",
            "Empty fits the part number to the rail.", step="0.1", minimum="0",
        )
        + page_served.number_field(
            "pb-text-clearance", "Edge clearance (mm)",
            "From the panel edge to the text envelope.", step="0.1", minimum="0",
        )
        + '</div></div><div id=pb-text-warning role=status></div>',
        collapsed=True,
    )


def _rail_copper_html() -> str:
    """The pour over the rails, and when it actually fills.

    THE NOTE IS THE POINT. Filling a pour costs KiCad several seconds, which is
    more than a responsive edit may cost, so a live rebuild draws the pour's
    outline empty and KiCad fills it behind the picture a few seconds later. A
    picture that shows no copper yet has to say why.
    """
    return page_served.group_html(
        "pb-copper", "Copper Fill", "pb-copper-on",
        "A no-net copper pour over the rails, on every copper layer.",
        page_served.number_field(
            "pb-copper-clear", "Copper clearance (mm)",
            "How far the pour holds off everything else.", step="0.05",
        )
        + page_served.check_field(
            "pb-copper-between", "Pour between boards",
            "Extend the pour into the gaps, which the router cuts through.",
        ),
        note="KiCad fills the pours behind the picture, a few seconds after "
             "each change, and a save waits for a fill still in flight.",
        # FIVE TOP-LEVEL GROUPS, ONE OPEN AT A TIME.
        top_level=True,
        collapsed=True,
    )


def _panel_scores_html(breakout=None) -> str:
    """The `V-Scores` group: saw passes on no board edge, and their keep-out.

    NAMED `V-Scores`: every line in here is a v-score, which is the word the
    fabricator and the prep page already use. Its switch reads `Add extra
    V-scores`: the panel already carries the scores its BOARDS decided, and
    what this group adds is extra passes on top of them.

    FOUR SWITCHES, AND NO POSITION FIELD. A saw pass goes down the middle of a
    gap or along the inner edge of a rail; both are places the layout already
    knows, so the user says WHICH passes to cut and the build says where. A
    typed position is a number that stops being true the next time the grid
    moves.

    THE NAMES SAY WHAT IS CUT, NOT WHICH WAY THE LINE POINTS. `Inter-row
    scores` is the pass between two rows and `Inter-column scores` the pass
    between two columns, so the label matches what the user is dividing - and
    the config key says the same words. There are two rail switches, `Rail
    scores, horizontal` for the passes along the top and bottom rails and `Rail
    scores, vertical` for the left and right ones, because those are two setups
    on the saw.

    EACH RAIL SCORE GOVERNS TWO SETTINGS UNDER IT. A SETBACK is plain material
    added between the boards and that rail's score line, and the PANEL GROWS by
    twice it: the array edge is where the router channel runs, so a pass left
    there puts the blade and the cutter in one place, and the rail keeps the
    width that was typed, panel edge to score. A JUMP is the gap left against
    the rail score the line it shortens runs into - a vertical saw line ends at
    the top and bottom rails, and the score along those two runs horizontally,
    so the vertical jump hangs off the horizontal rail score, and the
    horizontal jump off the vertical one for the mirror reason. THE SETBACK
    SITS ABOVE THE JUMP. Both wear the vertical line, and both are greyed and
    locked until that rail score is on.

    AND A THIRD SETTING UNDER EACH: `Shared line is the rail score`. With a
    zero setback the rail's line can land exactly on a pass the boards decided -
    one saw line, two meanings. Ticked, it is the RAIL score: edge to edge, no
    jump, which is what lets the panel separate, and that is the default.
    Unticked, it is the board-edge score and its direction's jump stops it short
    at both ends. THE TOOL CANNOT TELL WHICH IS WANTED - both are real cuts and
    the geometry is identical - which is why the user decides. It wears the
    same vertical line and the same greying, and the run's report names every
    shared line and the way it was treated.

    A GREYED SETTING CONTRIBUTES NOTHING TO THE BUILD. Greying is not a hint
    here: with its governor off a dependant's effective value is zero whatever
    the file says, the panel is built that way, and the report names every
    number it ignored.

    HORIZONTAL BEFORE VERTICAL in every label and every list here. The jumps
    read the other way round only because a dependant has to sit under its own
    governor.

    A SETBACK AND A JUMP ARE DISTANCES AND NOTHING ELSE: zero is none, so no
    switch sits beside either number saying the same thing twice.
    """
    def dependant(block_id: str, field_id: str, label: str, helper: str) -> str:
        # Served GREYED, because the config has not been read yet. The script
        # turns it live the moment it knows the rail score above it is on.
        return (
            f'<div class="dependent off" id={block_id}>'
            + page_served.number_field(
                field_id, label, helper, step="0.5", minimum="0",
            )
            + '</div>'
        )

    def dependent_check(block_id: str, field_id: str, label: str,
                        helper: str) -> str:
        # A DEPENDANT THAT IS A TICK. Same wrapper,
        # so it wears the vertical line and the greying the setback and the jump
        # wear - it hangs off the same rail score and has to read as part of it.
        return (
            f'<div class="dependent off" id={block_id}>'
            + page_served.check_field(field_id, label, helper)
            + '</div>'
        )

    return page_served.group_html(
        "pb-scores", "V-Scores", "pb-scores-on",
        "Saw passes across the whole panel, on no board edge.",
        page_served.check_field(
            "pb-score-rails-h", "Rail scores, horizontal",
            "One pass along the inner edge of the top and the bottom rail.",
        )
        + dependant(
            "pb-score-setback-h-block", "pb-score-setback-h",
            "Rail score setback, horizontal (mm)",
            "Material added above and below the boards. Panel "
            "grows by twice it.",
        )
        + dependant(
            "pb-score-jump-v-block", "pb-score-jump-v",
            "Jump-scores, vertical (mm)",
            "How far a top-to-bottom line stops short at each end. 0 is none.",
        )
        + dependent_check(
            "pb-score-shared-h-block", "pb-score-shared-h",
            "Shared line is the rail score",
            "On a shared line, cut edge to edge with no jump.",
        )
        + page_served.check_field(
            "pb-score-rails-v", "Rail scores, vertical",
            "One pass along the inner edge of the left and the right rail.",
        )
        + dependant(
            "pb-score-setback-v-block", "pb-score-setback-v",
            "Rail score setback, vertical (mm)",
            "Material added left and right of the boards. Panel "
            "grows by twice it.",
        )
        + dependant(
            "pb-score-jump-h-block", "pb-score-jump-h",
            "Jump-scores, horizontal (mm)",
            "How far a left-to-right line stops short at each end. 0 is none.",
        )
        + dependent_check(
            "pb-score-shared-v-block", "pb-score-shared-v",
            "Shared line is the rail score",
            "On a shared line, cut edge to edge with no jump.",
        )
        + page_served.check_field(
            "pb-score-inter-row", "Inter-row scores",
            "One pass across the middle of each gap between two board rows.",
        )
        + page_served.check_field(
            "pb-score-inter-col", "Inter-column scores",
            "One pass down the middle of each gap between two board columns.",
        ),
        # NO EXPLANATORY NOTE ON THIS GROUP. A control gets its name and at
        # most one short line saying what the number does.
        switch_label="Add extra V-scores",
        # DOES THE PANEL COME APART. Green says `comes apart`, red says
        # `trapped` and how many boards are stuck. It rides INSIDE THIS MENU,
        # with the settings, not on the heading bar. A status and never a
        # setting: nothing about it can be clicked and it carries no note. The
        # break ORDER is in the panel report, where there is room for a
        # sequence.
        status=page_served.status_badge(
            "pb-breakout", bool((breakout or {}).get("ok")),
            (breakout or {}).get("text", ""),
        ),
        # FIVE TOP-LEVEL GROUPS, ONE OPEN AT A TIME.
        top_level=True,
        collapsed=True,
    )


def _controls_html(generation: int, backend: str, pending: int,
                   panelizing: bool, fiducial_options=(),
                   breakout=None) -> str:
    """The third column: the groups, the write actions and the report.

    **NO CARRIER WRAPPER.** Rails, Tooling Holes, Fiducials, Copper Fill and
    V-Scores are groups of the column itself, in that order, each one folding
    on its own heading. A group inside a group would be one level of nesting
    that tells the reader nothing the five names do not.

    **EVERY GROUP OPENS COLLAPSED AND ONLY ONE OPENS AT A TIME.**
    The accordion itself lives in the shared fold script; this page turns it on
    (`PB.accordion`) and serves every section shut.

    Every control that changes something carries `data-act`, which is what the
    busy state disables - `Quit Panelizer` deliberately does not, because
    quitting has to work while a build is running.
    """
    counted = "change" if pending == 1 else "changes"
    label = (
        f"Save Panel Files ({pending} {counted})" if pending
        else "Save Panel Files"
    )
    layout = (
        page_served.number_field(
            "pb-columns", "Columns",
            "Board count across the panel.", step="1", minimum="1",
        )
        + page_served.number_field(
            "pb-rows", "Rows",
            "Board count down the panel.", step="1", minimum="1",
        )
        + page_served.number_field(
            "pb-gapx", "Gap across (mm)",
            "Edge to edge between neighbour columns; the cut runs in here.",
            step="0.1",
        )
        + page_served.number_field(
            "pb-gapy", "Gap down (mm)",
            "Edge to edge between neighbour rows.", step="0.1",
        )
        + _placement_html()
    )
    checks = (
        '<div class=row>'
        '<button class=btn id=pb-fingerprint-check>Check saved fingerprints</button>'
        '<a class=btn href="/fingerprints/report" download>Download fingerprint report</a>'
        '<button class=btn id=pb-check data-act=check>compare with the '
        'source</button>'
        '<a class=btn id=pb-report-link href="/check/report" target=_blank '
        'style="display:none">open the last report</a>'
        '</div>'
    )
    return (
        '<aside class=controls id=pb-controls aria-busy=false>'
        f'<header><span>Panel</span><span class=gen id=pb-gen>'
        f'gen {generation}</span></header>'
        '<div class=body>'
        # FIRST. An import pre-populates every group under it, so it reads
        # first. Every group here is served COLLAPSED - the accordion opens
        # exactly one, and which one is the user's last choice.
        + page_served.section_html("pb-sec-import", "Import settings",
                                   _import_html(), collapsed=True)
        + page_served.section_html("pb-sec-layout", "Layout", layout,
                                   collapsed=True)
        # THE FIVE CARRIER GROUPS, AND ALWAYS ALL FIVE, so every setting is on
        # the page to be found even when the config carries no panel section.
        # Each menu's own switch is what decides whether its stage runs; the
        # page's job is to have the switch on it. With no carrier the script
        # greys the lot (`hasCarrier`).
        + page_served.section_html("pb-sec-rails", "Rails",
                                   _rails_html(panelizing), collapsed=True)
        + _tooling_holes_html()
        + _fiducials_html(fiducial_options)
        + _rail_copper_html()
        + _panel_scores_html(breakout)
        # THE PANEL'S OWN DRAWING SETTINGS. After the five
        # carrier stages because that is the order the build runs in - the
        # dimensions are the last thing drawn on the panel - and before the
        # checks, which are not a setting at all.
        + _dimensions_html()
        + _text_html()
        + page_served.section_html("pb-sec-checks", "Checks", checks,
                                   collapsed=True)
        # NO "THIS PANEL" AND NO "BUILD NOTES" SECTION. The server still
        # builds both sets of lines and the problem-report zip carries them;
        # the page sends them to the developer console (`drawReport`).
        + '</div>'
        + page_served.actions_html(
            '<div class=row>'
            '<button class="btn go" id=pb-build data-act=build>Build Panel'
            '</button></div><div class=row>'
            f'<button class="btn save" id=pb-save data-act=save>'
            f'{html.escape(label)}</button></div>'
            '<div class=row>'
            '<button class=btn id=pb-stop>Quit Panelizer</button></div>'
            '<div class=vd>A layout or rail change rebuilds by itself. Build '
            'Panel derives the panel in memory; Save Panel Files writes the '
            'panel folder.</div>',
            backend,
        )
        + '<div class=status id=pb-status>every number is held in memory - '
        'Save Panel Files is what writes the panel folder</div>'
        '</aside>'
    )


def _card_html(view, layers, theme, window, content_note: str = "") -> str:
    """The one viewport: the derived panel, every layer stacked in draw order.

    Drawn with a panel and WITHOUT one. Before the first build there is nothing
    to plot, so the stack is one empty element per layer in the frame the board
    is drawn in - and the first build fills them rather than reloading the
    page. The structure is identical either way, which is what
    makes a build a repaint.
    """
    stack = ""
    if view is not None:
        for layer in view.layers:
            svg_path = view.svg_by_layer.get(layer.canonical_name)
            if svg_path is None:
                continue
            stack += page.inline_svg(
                Path(svg_path), f"svg-0-{layer.ordinal}", layer.canonical_name
            ).markup
        if not stack:
            raise ValueError("no SVG rendered for the panel preview")
        marks_box = page.padded_box(*view.window)
    else:
        box = page.padded_box(*window)
        stack = "".join(
            page.empty_svg(f"svg-0-{layer.ordinal}", layer.canonical_name, box)
            for layer in layers
        )
        if not stack:
            raise ValueError("this board declares no layer the preview shows")
        marks_box = box

    # THE CLEARANCE MARKING's element, last in the stack so
    # it draws over every layer. Always there and usually empty: it is not a
    # layer, it carries no board geometry, and the page fills it from the
    # measurement the fragment brings (`drawClearance`). `data-overlay` is what
    # keeps it out of the layer-set check and the layer panel.
    stack += page.overlay_svg("pb-marks", marks_box)

    return (
        f'<div class=card data-viewport="{panelbuild.PANEL_VIEWPORT}">'
        "<div class=head><h2>The panel - edge, cutter path and saw "
        "lines</h2>"
        '<button class="btn fit">fit</button></div>'
        f'<div class=viewport style="background:{theme.background}">'
        f"{stack}{page_served.await_html(content_note)}</div>"
        '<div class=hud><span>zoom <b class=zoom>100%</b></span>'
        "<span>cursor <b class=pos>-</b> mm</span>"
        f"<span><b class=ext>{html.escape(page_served.extent_text(view))}</b>"
        "</span></div>"
        "</div>"
    )


def build(config, view, generation: int, backend: str, session_dir: str = "",
          shot: int = 0, pending: int = 0, layers_seq: int = 0,
          stale=(), unsaved: str = "", error: str = "",
          check_reason: str = "", check_report: str = "",
          layers=None, theme=None, layer_names=None, window=None,
          content_note: str = "", startup_warning: str = "") -> str:
    """The whole panel page. *view* is None when no panel has been built yet.

    THE PAGE HAS THE SAME SHAPE EITHER WAY. Before the first build the viewport
    is there and empty, in the layers the board declares and the frame it is
    drawn in, because a build that has to change the page's STRUCTURE is a build
    that has to reload it - and a reload raises the browser's "leave site?"
    box on every build.

    Everything the viewport needs comes off the *view* when there is one.
    *layers*, *theme*, *layer_names* and *window* are how a caller states them
    when there is not; with no layers at all - a session whose board could not
    be prepared - the page comes without a viewport, as it always did.
    """
    heading = f"Panel - {Path(config.source_board_path).stem}"
    built = view is not None
    # DOES THE PANEL COME APART? Measured here rather than on the
    # page, and SERVED ALREADY FILLED so the bytes carry the verdict - a status
    # that only appears once a script runs is one a render fence cannot read.
    breakout = panelbuild.breakout_state(view.outcome) if built else {}
    layers = list(view.layers) if built else list(layers or ())
    theme = view.theme if built else theme
    names = dict(view.layer_names if built else (layer_names or {}))
    # NO SUBTITLE IN THE HEADER, so the page does not read one. The journal
    # and the problem-report zip still carry `view.subtitle` and the
    # fragment's `subtitle` field.
    # Drawn whenever the board's layers are known, whether or not a panel has
    # been built over them.
    drawn = bool(layers) and theme is not None

    card = _card_html(view, layers, theme, window or (0.0, 0.0, 1.0, 1.0),
                      content_note) if drawn else ""
    panel_html = (
        layerpanel.panel_html(
            layerpanel.ONE_VIEW_PANEL,
            layerpanel.rows_html(layers, theme=theme, title_names=names),
        )
        if drawn else ""
    )
    # The Holes row, when this board has one: drill marks stack above every
    # board layer - a hole belongs to none of them - and they open ON, because
    # the holes a panel is fixtured by are the first thing looked for on it.
    on_top = (
        (holes.HOLES_LAYER,)
        if any(layer.canonical_name == holes.HOLES_LAYER for layer in layers)
        else ()
    )
    # Open the configured dimensions layer with the panel. Later user toggles
    # still use the normal shared layer visibility state.
    dimension_layer = (config.panel.dimensions.layer if config.panel is not None
                       else PanelDimensionsConfig().layer)
    panel_js = layerpanel.panel_script(
        layers, (*prep.REVIEW_LAYERS, dimension_layer, *on_top), theme, (), on_top,
    ) if drawn else ""
    scripts = (
        f"<script>{panel_js}</script>\n<script>{page.JS}</script>"
        if drawn else ""
    )
    state = {
        "page": page_served.PANEL_STEP.page,
        # The name this tab adopts, so the prep page's link finds it rather
        # than opening a second copy of it.
        "stepWindow": page_served.PANEL_STEP.window,
        "steps": page_served.steps_json(),
        # ONE GROUP OPEN AT A TIME, on this page only. The
        # shared fold script reads this and remembers which single group is
        # open instead of a state per group.
        "accordion": True,
        "fragmentUrl": "/panel/fragment",
        "pendingKey": "pending_config",
        "notReady": "no panel has been built in this session yet",
        "saveLabel": "Save Panel Files",
        "saveTitle": "write the panel board, its project and the config",
        "saveIdleTitle": "write the panel folder as it now stands",
        "generation": generation,
        "backend": backend,
        "ok": built,
        "params": params_json(config),
        "report": list(view.report) if built else [],
        # The V-Scores heading's status, before any fragment has arrived.
        "breakout": breakout,
        "notes": list(view.notes) if built else ([error] if error else []),
        "stale": list(stale),
        "unsaved": unsaved,
        "check_reason": check_reason,
        "check_report": check_report,
        # The clearance marking's first paint, before any fragment has arrived.
        "clearance": dict(view.clearance) if built else {},
        "session": session_dir,
        "shot": shot,
        "pending": pending,
        "pendingChanges": [],
        "layersSeq": layers_seq,
        "layerCount": len(layers),
        # The corner note's first paint, before any fragment has arrived.
        "content_note": content_note,
        "upgradeSeq": 0,
    }
    # The panel's own numbers are read on the page, so the fence reads them
    # back off the bytes exactly as the prep page's are.
    styles = f"{page.CSS}{layerpanel.PANEL_CSS}{page_served.CONTROLS_CSS}"
    return f"""<!doctype html><meta charset=utf-8>
<title>{html.escape(heading)}</title>
{icons.PANEL_ICON}
<style>{styles}</style>
<div class=main>
  <div class=titlebar>
    <div class=grow><h1>{html.escape(heading)}</h1>
      <div class=sub>active layer: <b class=lp-active></b></div></div>
    {page_served._steps_html(page_served.PANEL_STEP.path)}
    {page_served._tools_html()}
  </div>
  <div class=stale id=pb-stale></div>
  <div class="views" id=views>{card}{page_served._busy_popup_html()}</div>
  {page_served._footer_html(session_dir, shot)}
</div>
{panel_html}
{_controls_html(generation, backend, pending, config.panel is not None,
                fiducial_resolution(config).fiducials, breakout)}
{page_served._unsaved_prompt_html()}
{page_served._report_prompt_html(page_served.report_folder_text(config))}
{page_served._startup_warning_html(startup_warning)}
{scripts}
<script>{page_served.page_script(PANEL_JS, state)}</script>
"""
