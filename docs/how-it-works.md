# How eedesignpanels works

This page describes what a panel is made of, the order in which the engine
builds it, the KiCad layers it reads and writes, and the main design choices.
For the settings file, see [settings.md](settings.md).

## The two steps

A panel is made in two steps. The browser page shows each step on its own tab.

1. **Prepare the board.** The engine draws the router tool path on the source
   board, and you decide which board edges a v-score saw cuts. The button
   **Save to Board** writes the board file and `panel/panel.json`. The command
   line equivalent is `python -m eedesignpanels.prep <project>/panel/panel.json`.
2. **Panelize.** The engine steps and repeats the prepared board into a grid,
   adds the rails and the other panel items, and writes the panel. The button
   **Save Panel Files** writes the `panel/` folder. The command line equivalent
   is `eedesignpanels <project>/panel/panel.json`.

Step 1 changes your design. The router tool path belongs to the board, so you
review it on one board, and it then steps into the panel like any other
drawing. Step 2 writes only inside the `panel/` folder.

## Files

| Path | Written by | Content |
|---|---|---|
| `<project>/<board>.kicad_pcb` | Step 1 | your board, with the router tool path on `User.36` |
| `<project>/<board>.kicad_pcb-bak` | Step 1 | the board as it was before the last **Save to Board** |
| `<project>/panel/panel.json` | Step 1, or the first start | every panel setting |
| `<project>/panel/panel.kicad_pcb` | Step 2 | the panel board |
| `<project>/panel/panel.kicad_pro` | Step 2 | the panel project, made from your project file |
| `<project>/panel/logs/` | the browser page | one folder for each session: a journal, saved views, and a copy of the settings |
| `<project>/panel/bug_reports/` | **Report a Problem** | one ZIP archive and one summary file for each report |
| `<project>/panel/review/` | the command-line prep and review | review pages |

The settings file names the board with a path relative to the `panel/`
folder, for example `../board.kicad_pcb`. The board must not be inside the
`panel/` folder, because Step 2 writes over that folder.

On the first start, the browser page writes a new `panel.json` when there is
none. It needs exactly one `.kicad_pcb` file in the project folder. If the
settings file cannot be read, the page starts with standard settings and
leaves your file as it is until you save. The command line does not do this.
It stops with a message when the settings file is missing or invalid.

## The build, in order

`eedesignpanels <panel.json>` runs these stages in this order. Each stage
works on the panel in memory. Nothing is written until the end.

1. **Read the board.** The engine reads the source board and its settings.
   It also reads the project file beside the board, if there is one.
2. **Step and repeat.** The engine puts one copy of the board in each grid
   cell. Each copy is the source board turned by a quarter turn and then
   moved. The copies keep their tracks, footprints, zones with their saved
   fills, and drawings. Layers in `exclude_layers` stay in the layer table,
   but their content is not copied. No dimension item is ever copied.
3. **Rails.** The engine grows the rectangle around the copies by the rail
   width on each side. This is the blank, the piece of material the panel is
   cut from.
4. **Router tool path.** The router path on `User.36` arrives with each copy.
   The engine adds the cuts that the arrangement needs, for example where the
   paths of two neighboring copies leave a strip of material narrower than
   `min_tab_mm`. These cuts go on `User.36` too.
5. **Panel outline.** The engine removes every `Edge.Cuts` item that the
   copies brought, including tab break contours. It then derives one outline:
   the blank minus the material the router removes. The outline is drawn on
   `Edge.Cuts`.
6. **Copper zones.** Each copy's copper zone becomes plain copper polygons,
   made from the fill the zone had when you saved the board. Rule areas stay
   as rule areas.
7. **V-scores.** The `User.38` lines of all copies that sit on one straight
   line merge into one saw pass across the panel.
8. **Tooling holes.** Non-plated holes in the rail corners, as footprints
   `PNLTH1`, `PNLTH2`, and so on. They are on by default.
9. **Panel v-scores.** Optional saw passes along the rails and between rows
   or columns. Jump scoring shortens passes at the panel edge. Each score line
   on the panel gets a copper rule area, whether or not this group is on.
10. **Rail copper.** Optional. One copper zone with no net on every copper
    layer, over the whole rail area. Rule areas keep it off the boards.
11. **Fiducials.** Optional. Footprints from a KiCad library, as `PNLFD1`,
    `PNLFD2`, and so on. Each one is measured from the tooling hole in its
    corner.
12. **Panel dimensions.** Native KiCad dimensions outside the panel: overall
    size, one board's size, the rails, the gaps between boards, and the
    tooling holes. They are on by default.
13. **Panel text.** A part number on a rail, in KiCad's own font.
14. **Fill the rail copper.** KiCad fills the rail copper zone. This happens
    last, so the fill clears everything that the earlier stages added.
15. **Write and check.** The engine writes `panel.kicad_pcb` and
    `panel.kicad_pro`, records a fingerprint in the panel board, and runs the
    checks in the next section.

The engine also sets the drill and place origin and the grid origin to the
bottom-left corner of the panel. When the panel has dimensions, it adds their
layer to the panel's plot layer selection.

## The checks after a build

The command line builds the panel twice and compares the results. It prints
`PASS` or `FAIL` for each check:

| Check | Passes when |
|---|---|
| build 1 == build 2 | the two `panel.kicad_pcb` files are identical, byte for byte |
| `panel.kicad_pro` 1 == 2 | the two project files are identical |
| panel content == replicated | the panel holds exactly N copies of the board's footprints, tracks, and zones |
| no dimension replicated | the copies brought no dimension items |
| rail copper transplanted | the copper fill changed nothing on the panel except the rail copper zone |

The exit code is 0 when every check passes, 1 when a check fails, and 2 when
the settings file is invalid. A failed check does not stop the build. The
panel files are written either way, so you can look at what went wrong.

The `passthrough` stage reads the board and writes it back. Its checks are
that the output equals the input byte for byte, and that two builds are
identical. The command-line prep also runs twice and checks that the second
run gives the same board file.

To compare a saved panel with its inputs later, use the fingerprint check in
[panel-fingerprint-api.md](panel-fingerprint-api.md).

## Layers

The engine reads and writes these layers by their KiCad canonical names. The
display name a board gives a layer, for example `User.36-RouteToolPath`, does
not matter.

| Layer | Content | Who draws it |
|---|---|---|
| `Edge.Cuts` | the board outline. In the panel, the derived panel outline. | you, and the engine for the panel |
| `User.8` | tab stop lines, as line segments, and tab mouth points, as points | the tab footprint |
| `User.36` | the router tool path. The line width is the cutter diameter. | Step 1 |
| `User.38` | v-score lines. A line on a proposed score line accepts that score. | you, or the accept control on Step 1 |
| `User.39` | edge plating marks, when `edge_plating` is on | Step 1 |
| `User.16` | panel dimensions. This is the one layer you can change, with `panel.dimensions.layer`. | Step 2 |
| `User.24` | rigid regions of a rigid-flex board | you |
| `User.37` | the flex router path of a rigid-flex board | Step 1 |
| `F.SilkS` or `B.SilkS` | panel text | Step 2 |

Do not keep other drawings on these layers. The engine reads them as panel
instructions.

Step 1 owns the board-level items on `User.36`. It deletes them and draws
them again on each run. Items on `User.36` inside a footprint are left alone.
Step 1 never writes `User.38` by itself. A score line appears there only when
you accept a score.

### Tabs

A tab is a footprint. It draws its break contour on `Edge.Cuts` and two stop
lines on `User.8`. The router runs along the board edge and stops where its
cutting edge reaches a stop line. Two points on `User.8` mark where the break
contour meets the ideal board edge. The router follows the ideal edge between
them.

A tab footprint can state the largest cutter it can survive, in its field
`_PANEL.ROUTE_TOOL_D_MAX`. Step 1 warns when the cutter diameter in the
settings is larger.

### V-scores

Step 1 finds every straight board edge that a saw can cut: a line that runs
to the board extents through outline and air only. These are proposals. You
accept a proposal by drawing a `User.38` line on it, or with the accept
control on the page. Accepting also adds a copper rule area along the line
and fills the board's copper again. An edge with an accepted score is not
routed.

Where a routed edge meets a score, the route continues past the score line by
`prep.vscore.route_past_score_mm`. When the route meets the score line at a
shallow angle, below `route_tangent_angle_deg`, it continues along its own
direction by `route_along_score_mm` instead. On the Step 1 page, you can
change this distance for each route end.

A `User.38` line that matches no proposal is reported as stale and has no
effect.

### Rigid-flex boards

The engine treats a board as rigid-flex when the project text variable
`PCB_CONSTRUCTION_TYPE` or `CONSTRUCTION_TYPE` is `RIGID FLEX`. The rigid
regions are on `User.24`. Only the flex router path, on `User.37`, cuts
through the panel.

### Panel text

Panel text reads three text variables from the board's project file:
`PART_ID`, `ARRAY_TABULATION`, and `PCB_REVISION`. It joins them with `-`.
When one is missing, the engine writes no panel text and gives a warning.
With no `text_height_mm`, the text is as tall as the rail allows.

### Panel dimensions

The dimensions go on the top and right edges for the overall and one-board
sizes, on the bottom-right corner for the rails, on the top and left edges
for the gaps between boards, and on the bottom-left corner for the tooling
holes. Equal values show once with `TYP`. The engine keeps the dimensions in
one group and replaces that group on each build. Your own drawings stay.

With no `text_height_mm`, the text height is 1.2 mm to 3 mm, from the panel
size and the smallest board size. If labels overlap, the engine makes the
automatic text smaller, down to 0.9 mm, and then moves the labels out. If
labels still overlap, it gives a warning and keeps them.

## Design choices

**Copies are quarter turns only.** KiCad turns an item by 90, 180, or 270
degrees with exact integer arithmetic. Other angles change shapes into
polygons, so the engine refuses them.

**Mirror is refused.** A mirrored copy needs a transform that flips layers,
and `eedesigncore` does not have one.

**Every copy has its own nets.** KiCad 10 joins nets by name, so the engine
adds the copy number to each net name: `GND` becomes `GND_1`, `GND_2`, and so
on. If a new name is already in use, the engine warns, because those nets
join. Reference designators do not change, because the assembler reads them
on the silkscreen. Each copy loses its link to the schematic.

**Same inputs give the same file.** Every identifier in the panel comes from
the grid cell and the source item, not from a random number. So two builds
from the same inputs give the same bytes. The build-twice check proves it on
each run.

**The cutter diameter and the route past a score are stated, never guessed.**
The board drawing cannot carry the size of a router bit or the depth of a
saw blade. A board that needs one of these values and has none stops with a
message that names the place.

**Narrow strips are cut away.** Two router paths closer than `min_tab_mm`
leave a strip of material that the fabricator cannot hold. With
`eliminate_slivers` on, the engine cuts the strip away and reports where. A
tab that your footprint declares is never cut away.

**Small outline gaps are closed and reported.** KiCad joins outline ends
closer than 0.01 mm. The engine does the same up to `outline_heal_mm`, and
reports each gap it closes with its position and size.

**Copper zones become plain copper in the panel.** A zone in a panel could be
filled again against a shape that you never checked. The engine keeps the
fill you saved and drops the zone. Rule areas stay, because they pour
nothing.

**KiCad fills the copper.** The engine computes no copper fill. It writes a
scratch copy of the panel, runs `kicad-cli` to fill it, and copies back only
the fill of the rail copper zone. A byte comparison proves that nothing else
changed.

**The settings file is explicit.** Every group is always written, with its
own `enabled` setting. A group that is off is still in the file, so the file
shows everything that made the panel.

**Checks warn, errors stop.** A check that finds a problem prints a warning,
and the build continues. A stage that cannot do what the settings ask stops
with a message and writes nothing.

**Save to Board locks the board.** With `lock_board_on_save` on, the default,
**Save to Board** locks every item on the board that KiCad can lock.

**Step 1 does not use KiCad's lock file.** The browser page holds the board
in memory and writes only when you press **Save to Board**. If the board is
open in KiCad at the same time, decide yourself whether to save.
