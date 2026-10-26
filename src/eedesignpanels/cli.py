"""Command line entry point: one command, one config path, nothing else.

Everything the run needs is in the config, so a run is reproducible by handing
someone the file. Building twice and comparing bytes is the reproducibility
check that the same input gives the same output, and it runs on every
invocation because it costs
one extra write.
"""

import argparse
import sys

from eedesignpanels import __version__, channels
from eedesignpanels.config import ConfigError, PanelConfig, load_config
from eedesignpanels.stages import passthrough, step_and_repeat
from eedesignpanels.toolpath import chain_bbox


def _report(label: str, passed: bool) -> None:
    print(f"  {'PASS' if passed else 'FAIL'}  {label}")


def _run_passthrough(config: PanelConfig) -> int:
    """The stage that changes nothing: output bytes must equal input bytes."""
    first = passthrough.run(config)
    print(f"  build 1      load {first.load_seconds:.2f}s, "
          f"write {first.write_seconds:.2f}s -> {first.output_path.name}")
    second = passthrough.run(config)
    print(f"  build 2      load {second.load_seconds:.2f}s, "
          f"write {second.write_seconds:.2f}s")

    fidelity = first.matches_source
    reproducible = first.output_sha256 == second.output_sha256
    print("checks")
    _report(f"output bytes == source bytes ({first.output_sha256[:16]}...)",
            fidelity)
    _report(f"build 1 == build 2 ({second.output_sha256[:16]}...)",
            reproducible)
    return 0 if fidelity and reproducible else 1


def _content_line(label: str, counts) -> str:
    """One board's content, in the order a panel is usually doubted in."""
    return (f"  {label:<12} {counts.footprints} footprints, {counts.tracks} "
            f"tracks, {counts.zones} zones, {counts.drawings} drawings "
            f"({counts.dimensions} of them dimensions), "
            f"{counts.points} points, {counts.groups} groups")


def _print_panelize(made) -> None:
    """What the panel half of the build did, in the order it is doubted in.

    The blank first, because everything else is measured against it; then the
    outline, as loops and length rather than as a picture, because the picture
    is the review page's job and the arithmetic has to stand on its own.
    """
    blank = made.blank
    boundary = made.boundary
    print(f"  blank        {blank.width_mm:.4f} x {blank.height_mm:.4f} mm, "
          f"{made.rails.summary} round the instance grid")
    print(f"  channels     {made.channel_runs} run(s), {made.channel_items} "
          f"item(s), {made.channel_length_mm:.3f} mm of cutter path, "
          f"{boundary.clearance_mm:.3f} mm clear of the outer edge")
    if made.channel_dust:
        print(f"  dust         {made.channel_dust} toolpath item(s) shorter "
              f"than {channels.MIN_ITEM_MM:g} mm dropped, longest "
              f"{made.worst_dust_mm * 1000:.2f} um - the channel is unchanged "
              f"to within that")
    print(f"  min tab      {made.min_tab_mm:.4f} mm; {len(made.joins)} "
          f"accidental sliver(s) consumed into the cut, "
          f"{len(made.protected_tabs)} declared tab(s) narrower than it")
    for join in made.joins:
        print(f"    consumed   {join.material_mm:.4f} mm of material at "
              f"({join.at[0]:.4f}, {join.at[1]:.4f}) mm; centreline gap "
              f"{join.gap_mm:.4f} mm joined by a {join.length_mm:.4f} mm cut "
              f"at radius {join.radius_mm:.4f} mm")
    print(f"  panel edge   {boundary.loop_count} loop(s), "
          f"{boundary.item_count} item(s), {boundary.length_mm:.3f} mm long, "
          f"{boundary.extent_mm[0]:.4f} x {boundary.extent_mm[1]:.4f} mm, "
          f"drawn at {made.outline_width_mm:g} mm")
    for index, loop in enumerate(boundary.loops, 1):
        box = chain_bbox(list(loop))
        print(f"    loop {index:<3}    {len(loop)} item(s), "
              f"{sum(piece.length for piece in loop):.3f} mm, "
              f"x {box[0]:.3f}..{box[2]:.3f}, y {box[1]:.3f}..{box[3]:.3f} mm")
    scores = made.scores
    print(f"  v-scores     {scores.replaced} instance line(s) -> "
          f"{scores.merged_count} panel pass(es)"
          + (f", {scores.kept} non-line item(s) left alone" if scores.kept
             else ""))
    for blade in scores.blades:
        print(f"    blade       ({blade.start[0]:.4f}, {blade.start[1]:.4f}) -> "
              f"({blade.end[0]:.4f}, {blade.end[1]:.4f}) mm, "
              f"{blade.length_mm:.3f} mm, merges {len(blade.sources)} line(s)")
    strip = made.strip
    kinds = ", ".join(f"{count} {kind}" for kind, count in strip.by_kind)
    print(f"  instances    {strip.total} Edge.Cuts item(s) removed: "
          f"{strip.board_items} board-level, {strip.footprint_items} inside "
          f"{strip.footprints_touched} footprint(s)"
          + (f" ({kinds})" if kinds else ""))
    flat = made.zones
    layers = ", ".join(f"{count} on {layer}" for layer, count in flat.per_layer)
    print(f"  zones        {flat.zones_removed} removed: {flat.filled_zones} "
          f"filled, {flat.empty_zones} empty")
    print(f"  rule areas   {flat.rule_areas_kept} kept: {flat.rule_areas} "
          f"board-level, {flat.footprint_rule_areas} inside footprints")
    print(f"  copper       {flat.polygons} polygon(s) flattened from the "
          f"cached fills, {flat.islands} of them islands with no net"
          + (f" - {layers}" if layers else ""))
    _print_carrier(made)


def _print_carrier(made) -> None:
    """The carrier features, each line absent when its stage did not run.

    Silence means "not asked for". A zero would read as "asked for and got
    none", which is a different panel and a different conversation.
    """
    holes = made.holes
    if holes is not None:
        print(f"  tooling      {holes.count} hole(s), {holes.diameter_mm:g} mm "
              f"non-plated, placed by {holes.placement.replace('_', ' ')}")
        for hole in holes.holes:
            clear = ("edge clearance not measured" if hole.clearance_mm is None
                     else f"{hole.clearance_mm:.4f} mm clear of the nearest edge")
            print(f"    {hole.designator:<10} ({hole.at[0]:.4f}, "
                  f"{hole.at[1]:.4f}) mm, {hole.corner.replace('_', ' ')}, "
                  f"{clear}")
        for corner in holes.skipped:
            print(f"    {'declined':<10} {corner.reason}")
    optical = made.fiducials
    if optical is not None:
        pattern = (" - the orientation pattern" if optical.orientation_pattern
                   else "")
        print(f"  fiducials    {optical.count} target(s), "
              f"{optical.library}:{optical.footprint}, "
              f"{optical.offset_mm:g} mm from the tooling holes "
              f"{optical.direction.replace('_', ' ')}, "
              f"{optical.side} side{pattern}")
        for target in optical.placed:
            print(f"    {target.designator:<10} ({target.at[0]:.4f}, "
                  f"{target.at[1]:.4f}) mm, {target.corner.replace('_', ' ')}, "
                  f"{target.side} side, {optical.offset_mm:g} mm from "
                  f"{target.datum}")
        for refused in optical.declined:
            print(f"    {'declined':<10} {refused.reason}")
    scores = made.panel_scores
    if scores is not None:
        if scores.protection_only:
            print(f"  panel scores none asked for; {scores.inherited_lines} "
                  f"merged instance pass(es) on the panel are guarded anyway")
        else:
            print(f"  panel scores {scores.count} added from the config, "
                  f"measured from the blank's top-left corner; "
                  f"{scores.inherited_lines} merged instance pass(es) already "
                  f"on the panel")
        for score in scores.scores:
            print(f"    {score.axis:<10} at {score.position_mm:g} mm -> "
                  f"({score.start[0]:.4f}, {score.start[1]:.4f}) to "
                  f"({score.end[0]:.4f}, {score.end[1]:.4f}) mm")
        source = (" (the config names no scores section, so this is the "
                  "shipped default)" if scores.protection_only else "")
        # WHAT THE BOARDS BROUGHT WITH THEM. A
        # line their own keepouts guard end to end gets nothing from the panel,
        # so the count above is what the PANEL added and this is the rest of
        # the answer.
        inherited = (
            f"; {scores.inherited_guarded} score line(s) guarded end to end by "
            f"the keepout their boards decided"
            if scores.inherited_guarded else ""
        )
        print(f"  score DRC    {len(scores.rule_areas)} rule area(s), "
              f"{scores.half_width_mm:g} mm either side of every score line, "
              f"keeping out everything on every copper layer{source}{inherited}")
        # A NUMBER THAT DID NOTHING NEVER LOOKS LIKE ONE THAT DID. A setback or
        # a jump whose rail score is off
        # contributes nothing to the build, exactly as the greyed field on the
        # page says, and the run states each one by name rather than leaving
        # the file to suggest otherwise.
        for skipped in scores.ignored:
            print(f"    {'ignored':<10} {skipped.sentence}")
        # A SHARED SAW LINE NEVER CHANGES MEANING SILENTLY. A rail score landing
        # on a pass the boards decided is one
        # line with two meanings, and THE TOOL CANNOT TELL which is wanted, so
        # the run counts them and names the treatment each one got.
        if scores.shared_lines:
            print(f"  shared lines {len(scores.shared_lines)} saw line(s) "
                  f"shared by a rail score and a score the boards decided")
            for line in scores.shared_lines:
                print(f"    {'shared':<10} {line.sentence}")
    # JUMP SCORING. Silent when neither distance was asked
    # for: zero is the full-span pass every earlier panel carried.
    jumps = made.jumps
    if jumps is not None and jumps.asked:
        print(f"  jump scoring {jumps.count} board pass(es) stopped short of "
              f"the panel edge - {jumps.horizontal_mm:g} mm at each end of a "
              f"horizontal pass, {jumps.vertical_mm:g} mm of a vertical one")
        for jumped in jumps.trimmed:
            print(f"    {jumped.axis:<10} ({jumped.now[0][0]:.4f}, "
                  f"{jumped.now[0][1]:.4f}) to ({jumped.now[1][0]:.4f}, "
                  f"{jumped.now[1][1]:.4f}) mm, {jumped.length_mm:.2f} mm of cut")
        for refused in jumps.declined:
            print(f"    {'declined':<10} {refused}")
    poured = made.rail_copper
    if poured is not None:
        between = ("rails and the gaps between boards" if
                   poured.pour_between_boards else "rails only")
        print(f"  rail copper  {poured.count} pour(s) over the {between}, "
              f"{poured.area_mm2:.1f} mm2 of outline on {len(poured.layers)} "
              f"copper layer(s), held {poured.clearance_mm:g} mm off everything")
        filled = poured.refill
        if filled is None:
            print("  rail fill    not filled on this run - the pours carry no "
                  "copper until KiCad fills them")
        else:
            per_layer = ", ".join(
                f"{count} on {layer}" for layer, count in filled.per_layer
            )
            print(f"  rail fill    {filled.records} fill record(s) "
                  f"transplanted into {filled.zones_filled} pour(s) in "
                  f"{filled.seconds:.2f}s"
                  + (f" - {per_layer}" if per_layer else ""))
    drawn = made.dimensions
    if drawn is not None:
        # WHO CHOSE THE LETTERING SIZE. The height is a setting on the panel
        # page, and a number on its own does not say whether
        # anybody picked it, and this run is the record of what was decided.
        chose = ("sized from the panel" if drawn.text_height_derived
                 else "set in the config")
        print(f"  dimensions   {drawn.count} aligned dimension(s) on "
              f"{drawn.layer}, outside the panel top and right, lettered "
              f"{drawn.text_height_mm:g} mm ({chose})")
        for measure in drawn.measures:
            print(f"    {measure.label:<28} {measure.text:>12}  "
                  f"({measure.start[0]:.4f}, {measure.start[1]:.4f}) to "
                  f"({measure.end[0]:.4f}, {measure.end[1]:.4f}) mm, crossbar "
                  f"{measure.offset_mm:g} mm clear of the {measure.side} edge")
        # WHICH BOARD THE INNER PAIR MEASURED, and the corner that chose it:
        # the board closest to the corner the outer dimension items share.
        # Printed because the run picked one instance
        # out of many and a reader cannot see which from the numbers alone.
        if drawn.board_box is not None:
            box = drawn.board_box
            print(f"    board measured               instance "
                  f"{drawn.board_index} at ({box[0]:.4f}, {box[1]:.4f}) to "
                  f"({box[2]:.4f}, {box[3]:.4f}) mm - the one nearest the "
                  f"corner the overall dimensions share, "
                  f"({drawn.board_corner[0]:.4f}, "
                  f"{drawn.board_corner[1]:.4f}) mm")


def _transplant_gate(made):
    """Did the rail fill transplant touch anything but the pours? None if unrun."""
    if made is None or made.rail_copper is None:
        return None
    filled = made.rail_copper.refill
    return None if filled is None else filled.gate_passed


def _run_build_panel(config: PanelConfig) -> int:
    """Step and repeat the whole board. Extents are predicted here, checked in
    the review render - kicad-cli is the independent measurement and it does not
    belong on the headless path."""
    first = step_and_repeat.run(config)
    print(f"  build 1      load {first.load_seconds:.2f}s, "
          f"build {first.build_seconds:.2f}s, "
          f"write {first.write_seconds:.2f}s -> {first.output_path.name}")
    second = step_and_repeat.run(config)
    print(f"  build 2      load {second.load_seconds:.2f}s, "
          f"build {second.build_seconds:.2f}s, "
          f"write {second.write_seconds:.2f}s")

    print(f"  instance     {first.source_size_mm[0]:.4f} x "
          f"{first.source_size_mm[1]:.4f} mm outline, "
          f"{first.items_per_instance} Edge.Cuts items, "
          f"{first.nets_per_instance} nets")
    print(_content_line("source", first.source_content))
    print(_content_line("replicated", first.instance_content))
    print(_content_line("panel", first.panel_content))
    if first.excluded_layers:
        print(f"  excluded     {len(first.excluded_layers)} layer(s) stay in the "
              f"table, content not replicated: "
              f"{', '.join(sorted(first.excluded_layers))}")
    print(f"  step         {first.step_mm[0]:.4f} x {first.step_mm[1]:.4f} mm")
    print(f"  panel        {first.outline_extent_mm[0]:.4f} x "
          f"{first.outline_extent_mm[1]:.4f} mm outline, "
          f"{first.inked_extent_mm[0]:.4f} x "
          f"{first.inked_extent_mm[1]:.4f} mm stroked, "
          f"{first.instances} instances")
    print(f"  replicates   {len(first.replicated_layers)} layers")
    if first.panelize is not None:
        _print_panelize(first.panelize)
    if first.project_path is not None:
        print(f"  project      {first.project_path.name}")
    if first.plot_layers:
        # What the downstream CICD inks when it plots this panel with the
        # board's own stored plot settings. The panel builder does not run the
        # fab outputs; the files it writes are the interface, so the selection
        # they carry is reported here rather than left to be discovered.
        print(f"  plot layers  {len(first.plot_layers)} layer(s) selected in "
              f"the panel's own plot settings: "
              f"{', '.join(first.plot_layers)}")
    print(f"  panel folder {config.panel_dir}")

    for warning in first.warnings:
        print(f"  WARNING      {warning}")

    reproducible = first.output_sha256 == second.output_sha256
    project_reproducible = first.project_sha256 == second.project_sha256
    print("checks")
    _report(f"build 1 == build 2 ({second.output_sha256[:16]}...)",
            reproducible)
    _report(f"panel.kicad_pro 1 == 2 ({second.project_sha256[:16] or 'none'}...)",
            project_reproducible)
    _report(f"panel content == replicated x {first.instances} "
            f"({first.expected_content.footprints} footprints, "
            f"{first.expected_content.tracks} tracks, "
            f"{first.expected_content.zones} zones)",
            first.content_matches)
    # A dimension item is never replicated, whatever layer it is on, so this
    # is worth its own line rather than being
    # one term inside the content comparison.
    _report(f"no dimension replicated (source {first.source_content.dimensions}, "
            f"panel {first.panel_content.dimensions})",
            first.panel_content.dimensions == 0)
    transplant = _transplant_gate(first.panelize)
    if transplant is not None:
        _report("rail copper transplanted, everything else byte-unchanged",
                transplant)
    # Every one of these is a check, and a check reports rather than blocks:
    # the design files are written either way, so the user can
    # look at what went wrong instead of at nothing.
    passed = (
        reproducible
        and project_reproducible
        and first.content_matches
        and first.panel_content.dimensions == 0
        and transplant is not False
    )
    return 0 if passed else 1


_STAGES = {
    "passthrough": _run_passthrough,
    "build_panel": _run_build_panel,
}


def use_utf8_output(streams=None) -> None:
    """Make the run's text output UTF-8, whatever the console code page is.

    The report prints characters such as the diameter sign in "4x ⌀3.175".
    On Windows, output sent to a pipe or a file uses the code page, often
    cp1252 or cp437, which has no such character: the print stopped the run
    with `UnicodeEncodeError` after the panel files were written. A real
    console window already takes UTF-8 text, so this changes only the
    redirected case. A stream that cannot be changed is left as it is.
    """
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run a panel build. Returns a process exit code."""
    use_utf8_output()
    parser = argparse.ArgumentParser(
        prog="eedesignpanels",
        description="Build a fabrication panel from a KiCad 10 board.",
    )
    parser.add_argument("config", help="path to panel/panel.json")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # The one recovery worth having: a config error is the user's, and a
        # traceback would bury the reason. Nothing has been written yet.
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print(f"eedesignpanels {__version__}")
    print(f"  config       {config.config_path}")
    print(f"  source board {config.source_board_path}")
    print(f"  panel folder {config.panel_dir}")
    print(f"  grid         {config.grid.summary}")
    print(f"  stage        {config.stage}")

    return _STAGES[config.stage](config)


if __name__ == "__main__":
    sys.exit(main())
