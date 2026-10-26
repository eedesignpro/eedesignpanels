"""The panel's companion ``.kicad_pro`` - because a board file is not a project.

A `.kicad_pcb` alone is not a valid KiCad project. The net classes live in the
project file, and
so do the text variables that `${…}` in board text resolves against - which is
why a panel without one plots the literal `${PCB_DWG_SCALE}` where the design
plots `10`.

So the panel gets its own, built from the source project's. `eedesigncore`
models a project file as a **preserved JSON tree**: it emits exactly what it
read plus whatever the caller changed, so every key this module does not
understand survives untouched. That is the whole reason to copy and edit rather
than to compose a project from scratch.

Four edits, and nothing else:

* `meta.filename` names the panel, not the design.
* `sheets` is emptied and `schematic.top_level_sheets` with it. They name the
  source design's schematic sheets by uuid, and the panel has no schematic - the
  same reason a panel footprint's `path` is cleared.
* `erc` and `cvpcb` are dropped. Both configure schematic-side tools.
* `_CICD_PANEL` is added to `text_variables`. It is the panel's identity marker:
  the one unambiguous way for anything downstream to tell a panel from the
  design it repeats, which is what makes carrying the design's own variables
  safe.
"""

import copy

from eedesigncore import ProjectFile

# The panel's identity marker, in the project's text variables and in the
# board's property snapshot. A string because a KiCad text variable is one.
PANEL_MARKER = "_CICD_PANEL"
PANEL_MARKER_VALUE = "true"

# Sections that configure schematic-side tools only. The panel has no
# schematic, so they configure nothing.
SCHEMATIC_ONLY_SECTIONS = ("erc", "cvpcb")


def build_project(source: ProjectFile, *, filename: str) -> ProjectFile:
    """The panel's project file, from the design's. *source* is not modified.

    *filename* is what `meta.filename` becomes - `panel.kicad_pro`, matching the
    board beside it.
    """
    panel = copy.deepcopy(source)

    meta = panel.root.get_object("meta")
    if meta is not None:
        meta.set("filename", filename)

    # The panel has no schematic, so nothing may name one.
    panel.root.set("sheets", [])
    schematic = panel.root.get_object("schematic")
    if schematic is not None:
        schematic.set("top_level_sheets", [])
    for section in SCHEMATIC_ONLY_SECTIONS:
        panel.root.remove(section)

    variables = panel.root.get_object("text_variables")
    if variables is None:
        panel.root.set("text_variables", {})
        variables = panel.root.get_object("text_variables")
    variables.set(PANEL_MARKER, PANEL_MARKER_VALUE)

    return panel
