"""Read-only content fingerprints shared by the plugin and CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from enum import Enum

from eedesigncore import Board, Property

from eedesignpanels import __version__
from eedesignpanels.config import PanelConfig, revision_problem

API_VERSION = 1
STAMP = "_PANEL.FINGERPRINT_V1"
LIMITATIONS = [
    "Project-file-only variables and external files are not checked.",
    "Fingerprints check recorded content, not fabrication correctness.",
    "Different copper tessellation may report a content change.",
]
IGNORED = {"id", "uuid", "tstamp", "locked", "unlocked", "render_cache",
           "generator", "generator_version", "version", "models", "groups",
           "grid_origin", "pcbplotparams", "paper", "title_block", "path",
           "sheetname", "sheetfile", "placed"}
UNORDERED = {"footprints", "drawings", "trailing_drawings", "tracks", "pads",
             "zones", "properties", "nets", "polygons", "filled_polygons",
             "layers", "targets", "layer_properties"}


class Unverified(ValueError):
    """The saved inputs cannot establish a fingerprint."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normal(value, field=""):
    if isinstance(value, Enum):
        return value.value
    if hasattr(type(value), "model_fields"):
        values = {key: getattr(value, key) for key in type(value).model_fields}
        if type(value).__name__ == "Board":
            values.pop("nets", None)  # Item nets already resolve to names.
        return {"type": type(value).__name__, **{
            key: _normal(item, key) for key, item in values.items()
            if key not in IGNORED and item is not None
        }}
    if isinstance(value, dict):
        return {str(key): _normal(item, str(key)) for key, item in value.items()
                if key not in IGNORED}
    if isinstance(value, (list, tuple)):
        items = [_normal(item) for item in value
                 if not (isinstance(item, Property) and item.name.startswith("_PANEL.FINGERPRINT"))]
        if field in UNORDERED:
            return sorted(items, key=_json)
        if field in {"pts", "vertices"} and items:
            if items[0] == items[-1]:
                items.pop()
            if items:
                # Polygon start point and traversal direction have no meaning.
                candidates = []
                for sequence in (items, list(reversed(items))):
                    first = min(range(len(sequence)), key=lambda i: _json(sequence[i]))
                    candidates.append(sequence[first:] + sequence[:first])
                return min(candidates, key=_json)
        return items
    if isinstance(value, float) and value == 0:
        return 0.0
    return value


def board_hash(board, excluded=None):
    if board.generators:
        raise Unverified("Procedural board generators cannot be fingerprinted by this version.")
    if excluded is not None:
        from eedesignpanels.stages.step_and_repeat import replication_content, resolve_layer_names
        layers, _ = resolve_layer_names(board, excluded)
        # These definitions affect construction even when not copied to output.
        layers = frozenset(layers) - {"Edge.Cuts", "User.8", "User.24", "User.36", "User.37", "User.38"}
        board, _ = replication_content(board, layers)
    return _hash(_normal(board))


def settings_hash(data, path):
    config = PanelConfig(**{key: value for key, value in data.items()
                           if key not in {"revision", "written_by"}}, config_path=Path(path))
    values = config.model_dump(mode="json")
    if config.panel is not None:
        values["panel"]["rails"] = dict(zip(("top", "bottom", "left", "right"), config.panel.rail_widths))
    values["exclude_layers"] = sorted(set(values["exclude_layers"]))
    for key in ("config_path", "source_board", "review", "verify", "stage", "revision", "written_by", "clearance_warning_mm", "lock_board_on_save"):
        values.pop(key, None)
    return _hash(values)


def _read(source, settings):
    source, settings = Path(source).resolve(), Path(settings).resolve()
    raw = {source: source.read_bytes(), settings: settings.read_bytes()}
    data = json.loads(raw[settings].decode("utf-8-sig"))
    problem = revision_problem(data)
    if problem:
        raise Unverified(str(problem))
    if (settings.parent / data["source_board"]).resolve() != source:
        raise Unverified("The source path does not match the panel settings.")
    board = Board.from_string(raw[source].decode("utf-8-sig"))
    config = PanelConfig(**data, config_path=settings)
    hashes = {"source_board": board_hash(board, config.exclude_layers),
              "panel_settings": settings_hash(data, settings)}
    return raw, data, hashes


def capture(source, settings, effective=None, held_board=None):
    """Capture evidence at generation start. Unsaved source cannot be stamped."""
    raw, data, hashes = _read(source, settings)
    if effective is not None:
        hashes["panel_settings"] = settings_hash(effective, settings)
        data = effective
        excluded = PanelConfig(**data, config_path=Path(settings)).exclude_layers
        hashes["source_board"] = board_hash(
            Board.from_string(raw[Path(source).resolve()].decode("utf-8-sig")), excluded)
    if held_board is not None:
        excluded = PanelConfig(**data, config_path=Path(settings)).exclude_layers
        if board_hash(held_board, excluded) != hashes["source_board"]:
            raise Unverified("Save to Board before generating a verified panel.")
    return {"raw": raw, "hashes": hashes, "source": Path(source).resolve(),
            "settings": Path(settings).resolve()}


def _unchanged(raw):
    return all(path.read_bytes() == value for path, value in raw.items())


def capture_pending(source, settings, effective):
    """Freeze file inputs without parsing boards on each preview adjustment."""
    source, settings = Path(source).resolve(), Path(settings).resolve()
    return {"source": source, "settings": settings,
            "raw": {source: source.read_bytes(), settings: settings.read_bytes()},
            "effective": json.loads(_json(effective)), "held_source": None}


def remove_stamp(board):
    board.properties = [p for p in board.properties if not p.name.startswith("_PANEL.FINGERPRINT")]


def stamp_saved(panel_path, evidence):
    """Stamp only a successful generation whose captured inputs still agree."""
    raw, _, hashes = _read(evidence["source"], evidence["settings"])
    if "effective" in evidence:
        effective = evidence["effective"]
        exclusions = PanelConfig(**effective, config_path=evidence["settings"]).exclude_layers
        if evidence["held_source"] is None:
            raise Unverified("No completed generation is available to fingerprint.")
        used = board_hash(Board.from_string(evidence["held_source"]), exclusions)
        if used != hashes["source_board"]:
            raise Unverified("Save to Board before generating a verified panel.")
        evidence = {**evidence, "hashes": {"source_board": used,
                    "panel_settings": settings_hash(effective, evidence["settings"])}}
    if raw[evidence["source"]] != evidence["raw"][evidence["source"]] or hashes != evidence["hashes"]:
        raise Unverified("Inputs changed during generation. No fresh fingerprint was saved.")
    path = Path(panel_path)
    panel_raw = path.read_bytes()
    board = Board.from_string(panel_raw.decode("utf-8-sig"))
    stamp = {"fingerprint_version": API_VERSION, "engine_version": __version__,
             **hashes, "inputs": _hash(hashes), "panel_content": board_hash(board)}
    remove_stamp(board)
    board.properties.append(Property(name=STAMP, value=_json(stamp)))
    if not _unchanged(raw) or path.read_bytes() != panel_raw:
        raise Unverified("Inputs changed during generation. No fresh fingerprint was saved.")
    text = board.to_string().replace("\r\n", "\n")
    if b"\r\n" in panel_raw:
        text = text.replace("\n", "\r\n")
    _atomic(path, text.encode("utf-8"))


def _atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".panel-check-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_panel(*, source_board, settings, panel_board, report_path):
    """Return and atomically write the versioned informational report."""
    source, config, panel, output = map(lambda p: Path(p).resolve(),
                                      (source_board, settings, panel_board, report_path))
    if output in (source, config, panel) or any(
        output.exists() and p.exists() and output.samefile(p) for p in (source, config, panel)
    ):
        raise ValueError("The report path must not overwrite an input file.")
    result = {"report_format_version": 1, "fingerprint_version": API_VERSION,
              "engine_version": __version__, "status": "cannot_verify", "summary": "",
              "checks": {key: {"status": "cannot_verify", "changed": []}
                         for key in ("inputs", "panel")}, "limitations": list(LIMITATIONS)}
    try:
        if not panel.exists():
            result.update(status="no_panel", summary="No saved panel exists.")
            for check in result["checks"].values():
                check["status"] = "no_panel"
        else:
            raw = {panel: panel.read_bytes()}
            board = Board.from_string(raw[panel].decode("utf-8-sig"))
            stamps = [p.value for p in board.properties if p.name == STAMP]
            if len(stamps) != 1:
                raise Unverified("The panel has no unique generation fingerprint. Regenerate it to establish one.")
            stamp = json.loads(stamps[0])
            if stamp.get("fingerprint_version") != API_VERSION:
                raise Unverified("The panel fingerprint version is unsupported.")
            for key in ("source_board", "panel_settings", "inputs", "panel_content"):
                value = stamp.get(key)
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise Unverified("The panel fingerprint is invalid.")
            if stamp["inputs"] != _hash({k: stamp[k] for k in ("source_board", "panel_settings")}):
                raise Unverified("The stored input fingerprints are inconsistent.")
            # Each check can still give evidence when the other cannot run.
            for path in (source, config):
                if path.exists():
                    raw[path] = path.read_bytes()
            try:
                input_raw, _, hashes = _read(source, config)
                if any(raw[path] != value for path, value in input_raw.items()):
                    raise Unverified("An input changed during comparison. Run the check again.")
                changed = [key for key, value in hashes.items() if stamp[key] != value]
                result["checks"]["inputs"] = {"status": "differences_found" if changed else "matches", "changed": changed}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["checks"]["inputs"]["reason"] = str(exc)
            try:
                changed_panel = board_hash(board) != stamp["panel_content"]
                result["checks"]["panel"] = {"status": "differences_found" if changed_panel else "matches",
                                               "changed": ["panel_content"] if changed_panel else []}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["checks"]["panel"]["reason"] = str(exc)
            if not _unchanged(raw):
                raise Unverified("An input changed during comparison. Run the check again.")
            statuses = {check["status"] for check in result["checks"].values()}
            result["status"] = ("differences_found" if "differences_found" in statuses else
                                "cannot_verify" if "cannot_verify" in statuses else "matches")
            result["summary"] = {
                "differences_found": "Panel inputs or panel content differ from the recorded generation.",
                "cannot_verify": "At least one fingerprint check could not finish. See the check reasons.",
                "matches": "Panel inputs and panel content match the recorded generation.",
            }[result["status"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="cannot_verify", summary=str(exc))
        for check in result["checks"].values():
            check.update(status="cannot_verify", changed=[])
    _atomic(output, (_json(result) + "\n").encode("utf-8"))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check"])
    for name in ("source", "settings", "panel", "report"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    result = check_panel(source_board=args.source, settings=args.settings,
                         panel_board=args.panel, report_path=args.report)
    print(result["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
