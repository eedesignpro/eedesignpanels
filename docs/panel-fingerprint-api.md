# Panel fingerprint check

A fingerprint check tells whether a saved panel still matches the board and
the settings it was built from. Use it in an automated check before a
fabrication export, or from the **Check saved fingerprints** button on the
Step 2 page.

The check reports. It never changes an input file, never builds a panel, and
never fills copper. A match says that the recorded content is unchanged. It
does not say that the panel is correct for fabrication.

Available from engine 0.1.0. The API version is 1.

## How a fingerprint is recorded

After a successful build, the engine saves the panel and then records a
fingerprint in it. The fingerprint is a board property named
`_PANEL.FINGERPRINT_V1`. Its value is JSON that holds the fingerprint
version, the engine version, a hash of the source board, a hash of the
settings, a combined input hash, and a hash of the panel content.

The engine records a fingerprint only when the inputs did not change during
the build. A board with unsaved changes on the Step 1 page gets no
fingerprint: press **Save to Board** first. Opening or saving the panel in
KiCad does not change the fingerprint.

## Run the check

From Python:

```python
from eedesignpanels.fingerprints import API_VERSION, check_panel

report = check_panel(
    source_board="board.kicad_pcb",
    settings="panel/panel.json",
    panel_board="panel/panel.kicad_pcb",
    report_path="build/panel-fingerprints.json",
)
```

From a command line:

```
python -m eedesignpanels.fingerprints check --source board.kicad_pcb --settings panel/panel.json --panel panel/panel.kicad_pcb --report build/panel-fingerprints.json
```

Each path can be text or a `pathlib.Path`. A relative path is relative to
the current folder. The source board must be the board that `source_board`
in the settings names. The report path must not be one of the three input
files.

`check_panel` writes the report and also returns it as a dictionary. The
command line prints the summary line. It exits with 0 for every finished
report, whatever the status. A wrong argument or a report that cannot be
written gives a nonzero exit code.

To check that the installed engine supports this API, import
`API_VERSION` and require that it is `1`.

## The report

```json
{
  "report_format_version": 1,
  "fingerprint_version": 1,
  "engine_version": "0.1.0",
  "status": "matches",
  "summary": "Panel inputs and panel content match the recorded generation.",
  "checks": {
    "inputs": {"status": "matches", "changed": []},
    "panel": {"status": "matches", "changed": []}
  },
  "limitations": [
    "Project-file-only variables and external files are not checked.",
    "Fingerprints check recorded content, not fabrication correctness.",
    "Different copper tessellation may report a content change."
  ]
}
```

| Status | Meaning |
|---|---|
| `matches` | The inputs and the panel content match the recorded fingerprint. |
| `differences_found` | An input or the panel content changed. `changed` names which. |
| `cannot_verify` | The check could not finish. A check that could not finish has a `reason`. |
| `no_panel` | There is no panel file. |

The `changed` list of the `inputs` check can hold `source_board` and
`panel_settings`. The `changed` list of the `panel` check can hold
`panel_content`.

These rules decide the overall status:

- A difference found by one check gives `differences_found`, even when the
  other check could not finish.
- An input file that changes while the check runs gives `cannot_verify`.
  Run the check again.
- A panel with no fingerprint, or with an unsupported or damaged one, gives
  `cannot_verify`. Build the panel again to record one.

A later report can add fields. A program that reads the report must ignore
fields it does not know.

## What the hashes include

**The panel content hash** covers what the fabricator gets: geometry,
layers, net names, footprint references, pads, drills, tracks, zones and
their saved fills, text, drawings, the stackup, and the board properties. It
ignores the order of items, item identifiers, file formatting, lock state,
3D models, groups, the title block, the plot settings, and the grid origin.
Two identical items still count as two. The start point and direction of a
polygon do not matter.

**The source board hash** uses the same rules. It leaves out the layers in
`exclude_layers`, because the build does not copy them. It always includes
`Edge.Cuts`, `User.8`, `User.24`, `User.36`, `User.37`, and `User.38`,
because they change how the panel is cut.

**The settings hash** covers the settings that change the panel. It leaves
out `source_board`, `stage`, `review`, `verify`, `revision`, `written_by`,
and `lock_board_on_save`. A rail width written as one number and the same
width written for four sides give the same hash.

The check does not read the project file or other external files. A project
text variable is checked only through the text the panel board holds.

A board that uses KiCad's generator items, for example tuning patterns,
cannot be fingerprinted by this version. The check then gives
`cannot_verify`.

## Use in an automated check

1. Pass the saved paths of the source board, the settings, and the panel to
   `check_panel` or to the command line.
2. Show the summary, the status of both checks, the limitations, and the
   report file.
3. Treat a missing engine, an old engine, a time-out, or a nonzero exit code
   as `cannot_verify`.
4. Continue the export. The check is for information. It does not stop an
   export.

Do not read the board property or compute hashes yourself. Only the engine
records a fingerprint.
