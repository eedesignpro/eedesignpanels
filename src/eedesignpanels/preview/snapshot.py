"""One snapshot: save anything worth reviewing, under one shot number.

The user presses one key and gets everything a reviewer needs to know what was
on screen. Three artifacts, one number::

    shots/NNN_<viewport>.svg    the viewport's CURRENT view, serialized by the
                                page: visible layers, their opacities, the live
                                viewBox, overlays. Vector, and exact.
    shots/NNN_<viewport>.png    the same document rasterized, long edge about
                                1600 px on a solid background, so it reads
                                alone and costs a vision model very little.
    shots/NNN_state.json        THE BACKEND STATE: the config as loaded, the
                                latest computation's results, and the client
                                view state as posted.

**The SVG comes from the client, deliberately.** What the user is looking at
is the browser's composition of a stack of layers with a pan, a zoom, three dim
modes and an annotation overlay - the server does not know any of that. Asking
the page for it keeps the shot faithful: the document holds kicad-cli's own
output, wrapped and switched exactly as the viewport had it, and nothing here
synthesises geometry. A view assembled by this code could look right while the
real output is wrong, which is the failure a review exists to catch.

The state file is PRETTY-PRINTED with sorted keys, because it is read by a
person in a diff as often as by a program, and because two snapshots of the
same state should differ in no byte that does not mean something.
"""

import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import NamedTuple

from eedesignpanels.preview import journal as journal_module, raster as raster_module

SCHEMA = "eedesignpanels.ui-snapshot/1"
STATE_SUFFIX = "state.json"

# A page posts one document per viewport. Two today (board and panel); the cap
# is a sanity bound on a request body, not a design limit.
MAX_VIEWPORTS = 12
MAX_SVG_BYTES = 8 * 1024 * 1024
MAX_NAME_CHARS = 48

_UNSAFE_NAME = re.compile(r"[^a-z0-9_-]+")
# Depth is bounded because a result holds board items, and a board item holds
# a board. Nothing here needs to go deeper than the tables the page shows.
MAX_STATE_DEPTH = 8
MAX_STATE_ITEMS = 2000
MAX_REPR_CHARS = 300


class SnapshotError(Exception):
    """The snapshot request cannot be honoured. Nothing was written."""


class Saved(NamedTuple):
    """What one shot put on disk."""

    shot: int
    folder: Path
    svg: list
    png: list
    state: str
    rasterized: bool
    warnings: list

    @property
    def summary(self) -> str:
        """The sentence the page shows: `saved shot 003` and what it holds."""
        parts = [f"saved shot {self.shot:03d}"]
        if self.svg:
            parts.append(f"{len(self.svg)} svg")
        if self.png:
            parts.append(f"{len(self.png)} png")
        parts.append("state")
        line = parts[0] + " - " + ", ".join(parts[1:])
        return line + ("; " + "; ".join(self.warnings) if self.warnings else "")


def jsonable(value, depth: int = 0):
    """*value* as JSON-safe data, expanding the records the stages return.

    NamedTuples, dataclasses and pydantic models are the three shapes the core
    uses, and each one becomes an object with its field names. Anything else
    becomes a short `repr` - a state file that omits a field a reviewer wanted
    is a nuisance; one that fails to write is a lost snapshot.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else repr(value)
    if isinstance(value, Path):
        return str(value)
    if depth >= MAX_STATE_DEPTH:
        return f"[depth limit: {type(value).__name__}]"

    dump = getattr(value, "model_dump", None)  # pydantic
    if callable(dump):
        return jsonable(dump(mode="json"), depth + 1)
    fields = getattr(value, "_asdict", None)  # NamedTuple
    if callable(fields):
        return {
            "_type": type(value).__name__,
            **{name: jsonable(item, depth + 1) for name, item in fields().items()},
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "_type": type(value).__name__,
            **{name: jsonable(item, depth + 1)
               for name, item in asdict(value).items()},
        }
    if isinstance(value, dict):
        return {
            str(key): jsonable(item, depth + 1)
            for key, item in list(value.items())[:MAX_STATE_ITEMS]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:MAX_STATE_ITEMS]
        return [jsonable(item, depth + 1) for item in items]
    text = repr(value)
    return text if len(text) <= MAX_REPR_CHARS else text[:MAX_REPR_CHARS] + "...[cut]"


def clean_name(name, fallback: str = "viewport") -> str:
    """A viewport name reduced to a filename component. Never a path.

    Everything a page sends is untrusted, and this one ends
    up in a filename, so it is rebuilt from a whitelist rather than checked
    for the ways it could be wrong.
    """
    text = _UNSAFE_NAME.sub("_", str(name or "").strip().lower()).strip("_")
    return (text or fallback)[:MAX_NAME_CHARS]


def _viewports_from(payload: dict) -> list:
    """The `(name, svg, view)` triples in a request body, validated."""
    if not isinstance(payload, dict):
        raise SnapshotError("a snapshot body must be a JSON object")
    posted = payload.get("viewports", [])
    if not isinstance(posted, list):
        raise SnapshotError("'viewports' must be a list")
    if len(posted) > MAX_VIEWPORTS:
        raise SnapshotError(
            f"{len(posted)} viewports is more than this page can have "
            f"({MAX_VIEWPORTS})"
        )
    out, used = [], set()
    for index, entry in enumerate(posted):
        if not isinstance(entry, dict):
            raise SnapshotError("every viewport must be a JSON object")
        markup = entry.get("svg", "")
        if not isinstance(markup, str) or not markup.lstrip().startswith("<svg"):
            raise SnapshotError(
                f"viewport {index} posted no SVG document - a snapshot is the "
                f"page's own serialization of what it is showing"
            )
        if len(markup.encode("utf-8")) > MAX_SVG_BYTES:
            raise SnapshotError(f"viewport {index}'s SVG is far too large")
        if "<script" in markup.lower():
            # A shot is opened later in a browser. It carries drawing and
            # nothing that runs, exactly like the frozen review artifact.
            raise SnapshotError("a snapshot SVG may not carry a script")
        name = clean_name(entry.get("name"), f"view{index}")
        while name in used:
            name = f"{name}_{index}"
        used.add(name)
        view = entry.get("view") if isinstance(entry.get("view"), dict) else {}
        out.append((name, markup, view))
    return out


def _client_state(payload: dict, viewports: list, shot: int) -> dict:
    """The posted view state, with the SVG documents replaced by their size.

    The documents are already on disk as files; repeating a megabyte of markup
    inside the state file would make the one artifact a reviewer opens first
    the one they cannot read.
    """
    state = {key: value for key, value in payload.items() if key != "viewports"}
    state["viewports"] = [
        {
            "name": name,
            "svg": f"{shot:03d}_{name}.svg",
            "svg_bytes": len(markup.encode("utf-8")),
            "view": view,
        }
        for name, markup, view in viewports
    ]
    return jsonable(state)


def save(session: journal_module.UiSession, shot: int, payload: dict,
         backend_state: dict,
         long_edge_px: int = raster_module.LONG_EDGE_PX) -> Saved:
    """Write shot *shot*: one SVG and one PNG per viewport, and the state file.

    The vector artifacts and the state file are written FIRST and the rasters
    after, so a missing dev extra - or a rasterizer that refuses one document -
    costs the PNG and never the snapshot.
    """
    viewports = _viewports_from(payload)
    warnings, svg_names, png_names = [], [], []

    for name, markup, _view in viewports:
        svg_path = session.shot_path(shot, f"{name}.svg")
        svg_path.write_text(markup, encoding="utf-8")
        svg_names.append(svg_path.name)

    state = {
        "schema": SCHEMA,
        "shot": shot,
        "recorded_at": journal_module.utc_now(),
        **backend_state,
        "client": _client_state(payload, viewports, shot),
        "raster": {
            "available": raster_module.AVAILABLE,
            "long_edge_px": long_edge_px,
            "backend": "resvg_py" if raster_module.AVAILABLE else "",
            "reason": raster_module.UNAVAILABLE_REASON,
            "images": [],
        },
    }

    if not viewports:
        warnings.append(
            "no viewport was posted, so this shot is the backend state only"
        )
    if not raster_module.AVAILABLE:
        warnings.append("no rasterizer here, so no PNG was written")

    for name, markup, _view in viewports:
        if not raster_module.AVAILABLE:
            break
        png_path = session.shot_path(shot, f"{name}.png")
        try:
            written = raster_module.rasterize(
                markup, png_path, long_edge_px=long_edge_px
            )
        except (raster_module.RasterUnavailable, ValueError, OSError) as exc:
            # A raster is a convenience over an exact vector file that is
            # already saved. Say what happened; keep the shot.
            warnings.append(f"{name}: no PNG ({type(exc).__name__}: {exc})")
            continue
        png_names.append(png_path.name)
        state["raster"]["images"].append({
            "file": png_path.name,
            "width_px": written.width_px,
            "height_px": written.height_px,
            "dpi": written.dpi,
        })

    state["warnings"] = list(warnings)
    state["artifacts"] = {
        "svg": list(svg_names),
        "png": list(png_names),
        "state": f"{shot:03d}_{STATE_SUFFIX}",
    }
    state_path = session.shot_path(shot, STATE_SUFFIX)
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return Saved(
        shot=shot,
        folder=session.shots_dir,
        svg=svg_names,
        png=png_names,
        state=state_path.name,
        rasterized=bool(png_names),
        warnings=warnings,
    )
