"""Build the KiCad package file for "Install from File".

KiCad's Plugin and Content Manager installs a plugin from one ZIP archive that
holds `metadata.json`, the `plugins/` folder and the optional `resources/`
folder, and nothing else. This script writes that archive and refuses when the
metadata or the version pins disagree. Standard library only.

    python tools/build_package_file.py
    python tools/build_package_file.py --output some/folder

The archive goes to `dist/<identifier>-<version>.zip` beside this project by
default. Every entry carries a fixed timestamp, so the same files always give
the same bytes and the same SHA-256.

Checks, each one a refusal:

* the metadata fields KiCad's package schema (v2) requires, the identifier
  pattern, a 150-character short description, and exactly ONE version entry,
  which is what "Install from File" demands;
* the version in `metadata.json`, `VERSION`, `plugins/__init__.py`, the
  release tag and the engine version in `plugins/runtime.py`, and the engine
  version in the repository's own `pyproject.toml` are one and the same.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# The archive members KiCad accepts for a plugin package. Anything else in the
# project folder never enters the archive.
MEMBERS = ("metadata.json", "plugins", "resources")
SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", ".gitkeep"}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# Patterns copied from KiCad 10.0.3 `kicad/pcm/schemas/pcm.v2.schema.json`.
IDENTIFIER = re.compile(r"^[a-zA-Z][-a-zA-Z0-9.]{0,98}[a-zA-Z0-9]$")
VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
KICAD_VERSION = re.compile(r"^\d{1,2}(\.\d{1,2}(\.\d{1,2})?)?$")
STATUS = {"stable", "testing", "development", "deprecated"}
REQUIRED = (
    "name", "description", "description_full", "identifier", "type",
    "author", "license", "resources", "versions",
)


class PackageProblem(Exception):
    """One reason the package file was not built."""


def check_metadata(meta: dict) -> dict:
    """Refuse metadata KiCad would refuse. Returns the one version entry."""
    for field in REQUIRED:
        if field not in meta:
            raise PackageProblem(f"metadata.json has no '{field}'")
    if not IDENTIFIER.match(str(meta["identifier"])):
        raise PackageProblem(f"identifier '{meta['identifier']}' is not valid")
    if len(meta["description"]) > 150:
        raise PackageProblem("description is longer than 150 characters")
    if not str(meta["license"]).strip():
        raise PackageProblem("license is empty")
    for who in ("author", "maintainer"):
        person = meta.get(who)
        if person is None and who == "maintainer":
            continue
        if not isinstance(person, dict) or not person.get("name"):
            raise PackageProblem(f"{who} needs a name")
        if not isinstance(person.get("contact"), dict) or not person["contact"]:
            raise PackageProblem(f"{who} needs a contact")
    versions = meta["versions"]
    if not isinstance(versions, list) or len(versions) != 1:
        raise PackageProblem(
            "metadata.json must name exactly one version for Install from File"
        )
    entry = versions[0]
    if not VERSION.match(str(entry.get("version", ""))):
        raise PackageProblem(f"version '{entry.get('version')}' is not valid")
    if entry.get("status") not in STATUS:
        raise PackageProblem(f"status '{entry.get('status')}' is not valid")
    if not KICAD_VERSION.match(str(entry.get("kicad_version", ""))):
        raise PackageProblem("kicad_version is missing or not valid")
    return entry


def load_runtime():
    """`plugins/runtime.py`, loaded by path. It imports neither wx nor pcbnew."""
    source = PROJECT / "plugins" / "runtime.py"
    spec = importlib.util.spec_from_file_location("eedesignpanels_runtime", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def engine_version() -> str:
    """The engine version in `pyproject.toml` one folder above this project.

    The engine and the plugin are released together with one number, so the
    engine's own version is one of the statements that must agree. Absent when
    this folder is copied out alone, which says so instead of guessing.
    """
    pyproject = PROJECT.parent / "pyproject.toml"
    if not pyproject.is_file():
        return "(no pyproject.toml beside kicad_plugin)"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "(none)"


def check_versions(version: str) -> None:
    """Refuse a package whose version statements disagree."""
    found = {"metadata.json": version}
    found["VERSION"] = (PROJECT / "VERSION").read_text(encoding="utf-8").strip()
    init = (PROJECT / "plugins" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', init, re.MULTILINE)
    found["plugins/__init__.py"] = match.group(1) if match else "(none)"
    runtime = load_runtime()
    found["plugins/runtime.py RELEASE_TAG"] = runtime.RELEASE_TAG.lstrip("v")
    found["plugins/runtime.py ENGINE_VERSION"] = runtime.ENGINE_VERSION
    found["pyproject.toml version"] = engine_version()
    if len(set(found.values())) != 1:
        lines = "\n".join(f"    {place}: {value}" for place, value in found.items())
        raise PackageProblem(f"the version statements disagree:\n{lines}")


def archive_files() -> list[tuple[Path, str]]:
    """Every file that goes into the archive, as (path, name), sorted."""
    files = []
    for member in MEMBERS:
        path = PROJECT / member
        if path.is_file():
            files.append((path, member))
            continue
        if not path.is_dir():
            continue
        for item in sorted(path.rglob("*")):
            if not item.is_file() or item.suffix == ".pyc":
                continue
            if SKIP_NAMES.intersection(item.relative_to(PROJECT).parts):
                continue
            files.append((item, item.relative_to(PROJECT).as_posix()))
    return sorted(files, key=lambda pair: pair[1])


def build(output: Path) -> Path:
    """Check, then write the archive. Returns its path."""
    meta = json.loads((PROJECT / "metadata.json").read_text(encoding="utf-8"))
    entry = check_metadata(meta)
    check_versions(entry["version"])
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{meta['identifier']}-{entry['version']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in archive_files():
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=PROJECT / "dist",
        help="folder for the package file (default: dist/ in this project)",
    )
    args = parser.parse_args(argv)
    try:
        target = build(args.output)
    except PackageProblem as problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 1
    data = target.read_bytes()
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        unpacked = sum(info.file_size for info in archive.infolist())
    print(f"package file   {target}")
    print(f"entries        {len(names)}")
    print(f"size           {len(data)} bytes, {unpacked} bytes unpacked")
    print(f"sha256         {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
