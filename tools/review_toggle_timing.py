"""Drive the corpus through the caption toggle and time every recalculation.

A review page for two defects: the caption that would not go back to
PROPOSED, and the edit that cost about a second.
Writes one self-contained page into `scratch/<stamp>_toggle_and_timings/` -
three pictures of the same board with one decision changing under them, and the
per-phase timing of every kind of edit there is.

Run it with the engine's interpreter, from anywhere. Name the project with
`--config <project>/panel/panel.json`, or set EEDESIGNPANELS_CORPUS to the
project folder, the same variable the corpus tests read:

    <python> tools/review_toggle_timing.py --config <panel.json>

Nothing is written outside the scratch folder, and the design is not touched.
"""

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from eedesignpanels import prep
from eedesignpanels.preview import naming
from eedesignpanels.preview.session import PreviewSession

_CORPUS_SETTING = os.environ.get("EEDESIGNPANELS_CORPUS", "").strip()
CORPUS = (Path(_CORPUS_SETTING) / "panel" / "panel.json"
          if _CORPUS_SETTING else None)
SCRATCH = Path(__file__).resolve().parents[1] / "kicad_plugin" / "scratch"

# The layers this review is about, bottom to top. Everything else on the board
# is noise for a question about one score decision.
SHOWN = ("Edge.Cuts", "User.36", "User.38", prep.PROPOSAL_LAYER)


def stacked(fragment: dict, title: str) -> str:
    """One viewport: the shown layers of *fragment*, stacked in its own frame.

    eedesigncore's documents, unmodified and unmerged. The page draws no
    geometry of its own, so each layer is its own element and the stack is the
    picture.
    """
    box = fragment.get("base") or ""
    parts = []
    for name in SHOWN:
        markup = fragment["svg"].get(name)
        if markup is None:
            continue
        parts.append(
            f'<svg class="lay" data-layer="{html.escape(name)}" '
            f'viewBox="{html.escape(box)}" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">{markup}</svg>'
        )
    rows = "".join(
        f"<li><b>{html.escape(row['displayId'])}</b> "
        f"{html.escape(row['verdict'])}</li>"
        for row in fragment["proposals"]
    )
    return (
        f'<figure><figcaption>{html.escape(title)}</figcaption>'
        f'<div class="viewport">{"".join(parts)}</div>'
        f'<ul class="verdicts">{rows}</ul></figure>'
    )


def timing_table(rows: list) -> str:
    """The measured table, phase columns and all."""
    names = []
    for row in rows:
        for phase in row["phases"]:
            if phase not in names:
                names.append(phase)
    head = "".join(f"<th>{html.escape(name)}</th>" for name in names)
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{row['phases'].get(name, '')}</td>" for name in names
        )
        body.append(
            f"<tr><th>{html.escape(row['what'])}</th>"
            f"<td class=\"tot\">{row['ms']:.0f}</td>{cells}</tr>"
        )
    return (
        '<table><thead><tr><th>edit</th><th class="tot">total ms</th>'
        f'{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


CSS = """
body{margin:0;padding:24px;background:#0d1117;color:#c9d1d9;
     font:14px/1.5 "Segoe UI",system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}
p.sub{color:#8b949e;margin:0 0 20px}
.row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
figure{margin:0;flex:1 1 300px;min-width:300px}
figcaption{font-weight:600;margin-bottom:6px}
.viewport{position:relative;height:320px;background:#010409;
          border:1px solid #30363d;border-radius:6px;overflow:hidden}
.viewport svg{position:absolute;inset:0;width:100%;height:100%}
ul.verdicts{list-style:none;margin:8px 0 0;padding:0;color:#8b949e}
ul.verdicts li{display:inline-block;margin-right:12px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{border:1px solid #30363d;padding:4px 10px;text-align:right}
thead th{background:#161b22;text-align:center}
tbody th{text-align:left;font-weight:600}
td.tot{font-weight:700;color:#e6edf3}
pre{background:#161b22;border:1px solid #30363d;border-radius:6px;
    padding:10px;overflow:auto;color:#8b949e}
"""


def build_page(states: list, rows: list, notes: list) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>panel builder - caption toggle and edit cost</title>"
        f"<style>{CSS}</style></head><body>"
        "<h1>The caption toggle, and what an edit costs</h1>"
        "<p class=\"sub\">One board, one decision, three states - and the "
        "measured cost of every kind of edit. Millimetres throughout.</p>"
        f"<div class=\"row\">{''.join(states)}</div>"
        "<h1>Where the time goes</h1>"
        "<p class=\"sub\">Milliseconds, server side, to the moment the page "
        "can paint. The phases are the panel recalculation's own.</p>"
        f"{timing_table(rows)}"
        f"<h1>Notes</h1><pre>{html.escape(chr(10).join(notes))}</pre>"
        "</body></html>"
    )


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CORPUS) if CORPUS else None,
                        required=CORPUS is None)
    parser.add_argument("--scratch", default=str(SCRATCH))
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.scratch) / f"{stamp}_toggle_and_timings"
    review = out / "review"
    review.mkdir(parents=True, exist_ok=True)

    session = PreviewSession(Path(args.config), out / "work")
    states, rows, notes = [], [], []
    try:
        started = time.perf_counter()
        session.refresh()
        rows.append({"what": "startup: prep compute + render", "phases": {},
                     "ms": (time.perf_counter() - started) * 1000.0})
        if session.error:
            print(f"the board could not be prepared: {session.error}")
            return 1
        session.wait_for_upgrades(60)

        started = time.perf_counter()
        session.start_panel()
        rows.append({"what": "first panel derive",
                     "phases": dict(session._panel_phases),
                     "ms": (time.perf_counter() - started) * 1000.0})
        session.wait_for_upgrades(60)

        target = None
        for row in session.fragment()["proposals"]:
            if row["verdict"] == "PROPOSED":
                target = row
                break
        if target is None:
            print("this board proposes nothing, so there is no toggle to show")
            return 1

        states.append(stacked(session.fragment(),
                              f"as the board says it: {target['displayId']} "
                              f"is PROPOSED"))

        started = time.perf_counter()
        session.accept(target["blade"])
        rows.append({"what": f"accept {target['displayId']} (board edit)",
                     "phases": dict(session._panel_phases),
                     "ms": (time.perf_counter() - started) * 1000.0})
        session.wait_for_upgrades(60)
        states.append(stacked(session.fragment(),
                              f"after one click: {target['displayId']} is "
                              f"SCORED"))

        started = time.perf_counter()
        session.decline(target["blade"])
        rows.append({"what": f"decline {target['displayId']} (board edit)",
                     "phases": dict(session._panel_phases),
                     "ms": (time.perf_counter() - started) * 1000.0})
        session.wait_for_upgrades(60)
        states.append(stacked(session.fragment(),
                              f"after the second click: "
                              f"{target['displayId']} is PROPOSED again"))

        for name, value, what in (
            ("tooling_holes_on", True, "carrier: tooling holes on"),
            ("hole_diameter_mm", 3.2, "carrier: hole diameter 3.2 mm"),
            ("hole_diameter_mm", 3.4, "carrier: hole diameter 3.4 mm"),
            ("rail_left_mm", 6.0, "panel: left rail 6 mm"),
            ("gap_x_mm", 3.5, "panel: gap across 3.5 mm"),
            ("cutter_diameter", 0.9, "prep: cutter 0.9 mm (board changes)"),
        ):
            started = time.perf_counter()
            session.set_param({name: value})
            rows.append({"what": what,
                         "phases": dict(session._panel_phases),
                         "ms": (time.perf_counter() - started) * 1000.0})
            session.wait_for_upgrades(60)

        notes.append(f"config: {args.config}")
        notes.append(f"board:  {session.config.source_board_path}")
        notes.append(f"session journal: {out / 'work'}")
        notes.append("")
        notes.append(
            "snapshot = serialising the held board to text and parsing it "
            "back, which is what a derive runs on. snapshot_reused = the "
            "board had not changed, so the previous one still stood."
        )
        notes.append(
            "The design on disk is never written: every edit above is in "
            "memory until Save to Board."
        )
        # NO BARE `User.##` ANYWHERE, on this page as on the live ones: every
        # layer is named the way the board names it, and the check is on the
        # bytes that get written.
        page = naming.humanize(build_page(states, rows, notes),
                               session.view.layer_names)
        bare = naming.violations(page)
        if bare:
            raise AssertionError(f"bare layer names on the page: {bare}")
        index = review / "index.html"
        index.write_text(page, encoding="utf-8")
        (out / "timings.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
        print(f"review page: {index}")
        print(f"timings:     {out / 'timings.json'}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
