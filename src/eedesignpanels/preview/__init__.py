"""The live prep preview: a loopback HTTP host for one board, one session.

`python -m eedesignpanels.preview <config.json>` starts it. The page is the
review page's own components with a third column of controls; a click posts a
decision, the server applies it to the board it holds IN MEMORY, recomputes,
and pushes the new picture back. One button - "Save to Board" - puts the result
on disk.

The pieces:

* :mod:`eedesignpanels.preview.session` - the held board and config, compute,
  render, the pending-change count, the generation counter;
* :mod:`eedesignpanels.preview.actions` - the edits a click may make, and the
  one function that writes;
* :mod:`eedesignpanels.preview.server` - the routes, on 127.0.0.1 only;
* :mod:`eedesignpanels.preview.page_served` - the page and its handlers;
* :mod:`eedesignpanels.preview.labels` - `#1`, `#2` in physical order, and the
  three verdict words;
* :mod:`eedesignpanels.preview.naming` - the board's display names, and the
  fence that keeps a bare `User.##` off the page;
* :mod:`eedesignpanels.preview.render_seam` - kicad-cli today, the library's own
  renderer the moment it lands.

KiCad's lock file is not consulted anywhere: the session holds the board in
memory, nothing on disk is touched until "Save to Board", and saving under an
open editor is the user's call.

And the session instrumentation, because a reviewer reads a picture faster than
anyone can type a description of one:

* :mod:`eedesignpanels.preview.journal` - the launch's folder and its
  append-only JSONL journal of everything that happened;
* :mod:`eedesignpanels.preview.snapshot` - one keypress saves the current view
  as vector and raster plus the whole backend state, under one shot number;
* :mod:`eedesignpanels.preview.raster` - the PNG, sized for a reader that pays
  per pixel; optional, and the snapshot survives its absence.
"""
