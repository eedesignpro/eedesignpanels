# Settings reference

All panel settings are in one file, `<project>/panel/panel.json`. The browser
page writes it when you press **Save to Board** or **Save Panel Files**. You
can also edit it by hand. The engine checks every value when it reads the file
and refuses a file with an unknown key or a value out of range. The message
names the setting.

Lengths are in millimeters. Angles are in degrees. `null` means "not set",
and the table says what the engine then does.

The models that define these settings are in `src/eedesignpanels/config.py`.

## A typical file

The browser page writes a file like this for a new project. The values that
are not defaults are the stage, the grid, the cutter, the route past a score,
and the rails. Most settings that keep their default are left out here.

```json
{
  "revision": 1,
  "written_by": "eedesignpanels 0.1.0",
  "source_board": "../board.kicad_pcb",
  "stage": "build_panel",
  "grid": { "columns": 2, "rows": 2, "gap_mm": 3.0 },
  "min_tab_mm": 0.5,
  "eliminate_slivers": true,
  "outline_heal_mm": 0.01,
  "pocket_cutouts": false,
  "prep": {
    "cutter_diameter": 0.88,
    "vscore": { "detect": "auto", "route_past_score_mm": 0.2 }
  },
  "panel": {
    "rails": 5.0,
    "tooling_holes": { "enabled": true, "diameter_mm": 3.175 },
    "scores": { "enabled": false },
    "rail_copper": { "enabled": false, "clearance_mm": 0.635 },
    "fiducials": { "enabled": false },
    "dimensions": { "enabled": true, "layer": "User.16" },
    "text": { "enabled": true, "layer": "F.SilkS" }
  }
}
```

The file the page writes holds every setting, with its default. A file you
write by hand can leave out any setting that has a default.

## Top level

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `revision` | integer | `1` | The settings file format. A file with another revision is from another version of the tool. The browser page then starts with standard settings and leaves the file as it is. The command line refuses it. |
| `written_by` | text | `eedesignpanels <version>` | The tool version that wrote the file. For information only. |
| `source_board` | text | required | The board file, relative to the `panel/` folder. It must end in `.kicad_pcb` and must not be inside the `panel/` folder. |
| `stage` | `passthrough` or `build_panel` | `passthrough` | `passthrough` writes the board back unchanged, as a test of the read and write path. `build_panel` builds the panel. A new file from the browser page has `build_panel`. |
| `grid` | section | required | The copies of the board. See below. |
| `min_tab_mm` | number | `0.5` | The narrowest strip of material between two router paths. The smallest permitted value is `0.1`. |
| `eliminate_slivers` | true or false | `true` | `true` cuts away a strip narrower than `min_tab_mm` and reports it. `false` keeps it and reports it. |
| `outline_heal_mm` | number, 0 to 1 | `0.01` | The largest gap between two board outline ends that the engine closes. Each closed gap is reported. `0` closes no gap. |
| `pocket_cutouts` | true or false | `false` | `false` routes only around a cutout, so the middle falls out as a loose piece. `true` adds passes that mill the middle away. |
| `edge_plating` | true or false | `false` | Marks the routed board edges on `User.39` for edge plating. |
| `edge_plating_front_mask` | true or false | `true` | With edge plating, also opens the solder mask along the edge on `F.Mask`. |
| `edge_plating_back_mask` | true or false | `true` | The same on `B.Mask`. |
| `edge_plating_mask_width_mm` | number, above 0, up to 10 | `0.2` | The width of the mask opening. |
| `lock_board_on_save` | true or false | `true` | **Save to Board** locks every board item that KiCad can lock. |
| `exclude_layers` | list of layer names | see below | Layers whose content is not copied into the panel. The layers stay in the layer table. An empty list copies every layer. |
| `prep` | section or `null` | `null` | The router and v-score settings for Step 1. Step 1 needs this section. See below. |
| `panel` | section or `null` | `null` | The rails and panel items. Without this section, the build stops after step and repeat: a grid of copies, each with its own outline. |
| `review` | section | `{"layers": "all"}` | Which layers the command-line review page shows: `"all"`, or a list of layer names. |
| `verify` | section | see below | The gerber comparison. A development tool. |

The default `exclude_layers` list is:

```json
["User.Drawings", "User.Comments", "User.14", "User.15", "User.16", "User.41", "User.42"]
```

A layer name can be the canonical name, for example `User.41`, or the name
KiCad shows in the Appearance panel, for example `User.Drawings`. Dimension
items are never copied, on any layer.

## `grid`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `columns` | integer, 1 or more | required | Copies across. |
| `rows` | integer, 1 or more | required | Copies down. |
| `gap_mm` | number | required | The space between neighboring copies. |
| `gap_x_mm` | number or `null` | `null` | The space between columns. `null` uses `gap_mm`. |
| `gap_y_mm` | number or `null` | `null` | The space between rows. `null` uses `gap_mm`. |
| `rotation_deg` | 0, 90, 180, or 270 | `0` | The turn of every copy. |
| `alternate` | `none`, `rows`, or `columns` | `none` | Turns every other row or column a further 180 degrees, so shaped boards fit together. |
| `nest_across_mm` | number, can be negative | `0` | Moves the alternated copies across. Has no effect when `alternate` is `none`. |
| `nest_down_mm` | number, can be negative | `0` | Moves the alternated copies down. |
| `clearance_warning_mm` | number | `0` | The page marks two board rectangles closer than this. `0` turns the marks off. It does not change the panel. |
| `placement` | section or `null` | `null` | A placement list. See below. |
| `mirror` | true or false | `false` | `true` is refused. |

**Placement list.** `placement` holds one row for each copy, in row order,
and exactly `columns` times `rows` rows. Each row is
`{"across_mm": ..., "down_mm": ..., "angle_deg": ...}`. The offsets are from
where the source board sits, so `0, 0, 0` is the source position. The angle is 0, 90, 180, or 270. When a
placement list is present, the engine ignores `rotation_deg`, `alternate`,
and the nest settings, and says so. The Step 2 page fills this section from a
CSV file with three numbers on each line. `source` keeps the CSV file name
for information.

## `prep`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `cutter_diameter` | number, above 0 | required | The router bit diameter. A new file gets `0.88`. |
| `vscore` | section | see below | V-score settings. |

### `prep.vscore`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `detect` | `auto` or `none` | `auto` | `auto` proposes v-scores and uses the `User.38` lines that accept them. `none` routes every edge and ignores `User.38` lines, with a report. |
| `route_past_score_mm` | number or `null` | `null` | How far a route continues past a score line, so the score ends in routed air. Required when a route meets a score. A new file gets `0.2`. |
| `route_along_score_mm` | number | `0` | For a route that meets a score line at a shallow angle: how far it continues along its own direction. |
| `route_tangent_angle_deg` | number, 0 to 90 | `10` | Below this angle between the route and the score line, `route_along_score_mm` applies. At or above it, `route_past_score_mm` applies. |
| `custom_route_ends` | true or false | `false` | Uses the distances set for single route ends. |
| `route_end_overrides` | map of end to distance | `{}` | Distances for single route ends. Set them on the Step 1 page. The keys come from the board geometry, so a change to the outline can make a key stale. The engine warns about a stale key. |
| `rule_half_width_mm` | number, above 0 | `0.3` | How far the copper rule area reaches on each side of a score line, on the board and on the panel. |

## `panel`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `rails` | number, or `{"top", "bottom", "left", "right"}` | required | The rail width. One number sets all four sides. The object sets each side, and all four are required. `0` removes a rail. A new file gets `5.0`. |
| `tooling_holes` | section | on | See below. |
| `scores` | section | off | See below. |
| `rail_copper` | section | off | See below. |
| `fiducials` | section | off | See below. |
| `dimensions` | section | on | See below. |
| `text` | section | on | See below. |

### `panel.tooling_holes`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `true` | Adds non-plated tooling holes. |
| `diameter_mm` | number | `3.175` | The hole diameter. |
| `corners` | `top_left`, `top_right`, `bottom_right`, `bottom_left` | all `true` | Which corners get a hole. |
| `placement` | `rail_corner_center` or `fixed_inset` | `rail_corner_center` | `rail_corner_center` puts the hole in the middle of the rail corner, so it moves with the rail width. `fixed_inset` uses the two insets below. |
| `inset_across_mm` | number | `5.0` | With `fixed_inset`: the distance from the left or right panel edge. |
| `inset_down_mm` | number | `5.0` | With `fixed_inset`: the distance from the top or bottom panel edge. |

### `panel.scores`

Panel v-scores are saw passes that the panel adds. The engine computes their
positions from the layout. The v-scores that the boards bring are always on
the panel, whatever this section says.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `false` | Turns this group on. |
| `rails_horizontal` | true or false | `false` | One pass along the inner edge of the top rail and one along the bottom rail. |
| `rail_setback_horizontal_mm` | number, 0 to 1000 | `0` | Plain material between the top and bottom rail scores and the boards. The panel grows by twice this value, top to bottom. Needs `rails_horizontal`. |
| `jump_vertical_mm` | number, 0 to 1000 | `0` | Stops each vertical pass short of the top and bottom panel edges by this distance. Needs `rails_horizontal`. |
| `shared_line_is_rail_score_horizontal` | true or false | `true` | When a horizontal rail score falls on a score that the boards brought: `true` treats the line as a rail score, full length. `false` treats it as a board score, shortened by the jump. Needs `rails_horizontal`. |
| `rails_vertical` | true or false | `false` | One pass along the inner edge of the left rail and one along the right rail. |
| `rail_setback_vertical_mm` | number, 0 to 1000 | `0` | The same as the horizontal setback, for the left and right rails. The panel grows across. Needs `rails_vertical`. |
| `jump_horizontal_mm` | number, 0 to 1000 | `0` | Stops each horizontal pass short of the left and right panel edges. Needs `rails_vertical`. |
| `shared_line_is_rail_score_vertical` | true or false | `true` | The same as the horizontal setting, for vertical rail scores. Needs `rails_vertical`. |
| `inter_row` | true or false | `false` | One pass along the middle of each gap between rows. |
| `inter_column` | true or false | `false` | One pass along the middle of each gap between columns. |

A setting that needs a rail score has no effect while that rail score is
off. The page shows it gray, and the build report names it. A jump never
shortens a rail score.

### `panel.rail_copper`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `false` | Adds one copper zone with no net on every copper layer over the rails. |
| `clearance_mm` | number | `0.635` | The clearance from the zone to everything else. |
| `pour_between_boards` | true or false | `false` | Also pours copper in the gaps between boards. |

### `panel.fiducials`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `false` | Adds fiducial footprints on the rails. |
| `footprint` | text | `""` | The footprint name. Required when `enabled` is `true`. |
| `library` | text | `Fiducial` | The library nickname in the KiCad footprint library table. |
| `library_path` | text or `null` | `null` | A `.pretty` folder to use instead of the library table. Use it on a computer with no KiCad settings. |
| `offset_mm` | number, above 0 | `5.0` | The distance from the tooling hole in the same corner, inward. |
| `direction` | `along_top_and_bottom_rails` or `along_left_and_right_rails` | `along_top_and_bottom_rails` | The rails along which the offset runs. |
| `side` | `F.Cu`, `B.Cu`, or `both` | `F.Cu` | The copper side. `B.Cu` flips the footprint. `both` puts one on each side at the same position. |
| `corners` | `top_left`, `top_right`, `bottom_right`, `bottom_left` | `bottom_left` is `false`, the others `true` | Which corners get a fiducial. Three corners show which way round the panel is. A corner with no tooling hole gets no fiducial. |

### `panel.dimensions`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `true` | Adds the panel dimensions. |
| `layer` | layer name | `User.16` | The layer for the dimensions. The engine warns when the board does not declare it. |
| `text_height_mm` | number or `null`, up to 50 | `null` | The text height. `null` sizes it from the panel, 1.2 mm to 3 mm. |
| `text_thickness_mm` | number or `null` | `null` | The text line width. `null` uses 0.15 times the text height. |
| `first_offset_mm` | number or `null` | `null` | The distance from the panel edge to the first row of dimensions. `null` derives it from the text height. |
| `row_pitch_mm` | number or `null` | `null` | The smallest distance between rows of dimensions. `null` derives it from the text height. |
| `line_thickness_mm` | number | `0.1` | The dimension line width. |
| `arrow_length_mm` | number | `1.27` | The arrow length. |
| `extension_offset_mm` | number | `0.5` | The gap between the measured point and the extension line. |
| `precision` | integer, 0 to 5 | `3` | The number of decimal places. |
| `suppress_zeroes` | true or false | `true` | Removes trailing zeros. |

### `panel.text`

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | true or false | `true` | Adds the part number text. It needs three project text variables. See [how-it-works.md](how-it-works.md#panel-text). |
| `layer` | `F.SilkS` or `B.SilkS` | `F.SilkS` | The silkscreen side. |
| `rails` | `automatic`, `left`, `right`, `top`, `bottom`, `vertical`, `horizontal`, `all`, or `none` | `automatic` | The rails that get text. `automatic` uses the first rail with a width above zero, in the order left, right, top, bottom. |
| `text_height_mm` | number or `null`, up to 50 | `null` | The text height. `null` uses the largest height that fits the rail. |
| `edge_clearance_mm` | number | `1.0` | The space between the text and the panel edge. |

## `verify`

The gerber comparison checks a saved panel against its source board. It
needs the `verify` extra and `kicad-cli`. Most users do not change these
settings.

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Allows the comparison. |
| `um_per_pixel` | `5.0` | The image resolution, in micrometers for each pixel. |
| `threshold` | `0` | The number of differing pixels that is still accepted. |
| `region_cell_mm` | `1.0` | The size of the cells in which differences are grouped. |
| `max_canvas_mpixels` | `100.0` | The largest image, in megapixels. Above it, the comparison warns and stops. |
| `display_max_px` | `8000` | The largest picture shown on the page. It does not change the measurement. |
| `vector_max_kb` | `4096.0` | Above this size, a layer is shown as an image, not as vector graphics. |
| `vector_max_elements` | `40000` | Above this number of elements, a layer is shown as an image. |
| `clip_margin_mm` | `1.0` | How far outside the board outline the comparison looks. |
| `displacement_threshold_mm` | `0.01` | The largest movement of geometry that still passes. |
| `compared_layers` | `physical` | `physical` judges the layers that make the bare board. `all` judges every compared layer. A list judges the layers it names. |
| `workers` | `0` | The number of processes. `0` chooses from the computer's processors and memory. `1` uses one process. |
