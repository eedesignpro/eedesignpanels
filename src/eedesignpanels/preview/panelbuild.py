"""The panel page's own half: derive a panel in memory, and say what it is.

**Tab 2 consumes tab 1's IN-MEMORY state.** The panel is
derived from the board the prep page is editing - unsaved score decisions and
the toolpath included - and from the config as it now reads, so a designer can
see the panel a decision produces before the decision is on disk. Nothing here
writes: :func:`derive` builds a board object, and the panel folder is written
only by the page's own explicit save.

The build itself is `stages.step_and_repeat.build_from`, which is the same call
`python -m eedesignpanels <config>` makes after loading the file. One build, two
callers: a board whose file bytes match therefore gives a panel whose file bytes
match, and that is asserted rather than assumed.

**The pour is the one thing here that needs KiCad**, and it is a second seam:
`take_fill` asks kicad-cli what the rail copper is, `apply_fill` puts that answer
on the panel this session holds. See "THE POUR" below - the split is about which
half may run without a lock.

What is in this module besides the seam is WORDS AND NUMBERS - the report a
designer reads under the picture, the sentence over it, and why the picture is
out of date. They are pure functions over a build outcome, so they are testable
without a renderer, and they are written in millimetres and plain language,
with no unit selector.
"""

import json
import time
from typing import NamedTuple

from eedesigncore import Board

from eedesignpanels import comes_apart, cuttability
from eedesignpanels import placement as placement_math
from eedesignpanels.stages import rail_copper, refill, step_and_repeat

# What the panel viewport is called, and therefore what a snapshot of it is
# called. The prep page's is `board`.
PANEL_VIEWPORT = "panel"

# Why a panel picture is out of date. A panel build is heavier than a prep
# compute, so nothing here rebuilds by itself: the page says which of these
# happened and waits for one click.
NEVER_BUILT = "no panel has been built in this session yet"
BOARD_CHANGED = "the board has changed since this panel was built"
CONFIG_CHANGED = "the panel numbers have changed since this panel was built"


class PanelView(NamedTuple):
    """One derived panel, rendered - the thing the panel page is made of."""

    outcome: object
    layers: list
    svg_by_layer: dict
    theme: object
    subtitle: str
    bbox: tuple
    window: tuple
    layer_names: dict
    # EACH LAYER'S MARKUP, KEPT. What a fragment sends, held here rather than
    # read back off the 51 documents the render has just written - which was
    # measured at 345 ms the first time and 9 ms warm,
    # once per edit, inside the first paint (`session._markup`,
    # `session._repaint`). None means "not kept": the fragment reads the files,
    # exactly as it did before, so a view built without it still serves.
    svg_inner: dict = None
    # WHEN EACH LAYER'S MARKUP LAST CHANGED, as a paint number
    # (`session.PreviewSession.paint_seq`). A carrier edit redraws every layer
    # of the panel and moves three of them; this is what lets the fragment
    # carry those three.
    svg_stamp: dict = None
    # The report under the picture: one line each, in the reader's layer names.
    report: tuple = ()
    # What the build had to say for itself, in the reader's layer names.
    notes: tuple = ()
    # How many unsaved board edits and config edits this panel was derived
    # from. Not a warning: it is the point of deriving from memory, and the
    # page says it out loud so a picture is never mistaken for the files.
    unsaved_board: int = 0
    unsaved_config: int = 0
    # Was the board this was derived from different from the design FILE?
    # True with nothing edited is the ordinary case for a board whose toolpath
    # an older prep run wrote - see :func:`unsaved_sentence`.
    board_differs: bool = False
    # The generation the build ran at, so a reader can tie a picture to a log.
    generation: int = 0
    # Did this build FILL the rail pour? False is the live picture: the pour's
    # outline is there and carries no copper, because filling it is KiCad's work
    # and costs most of ten seconds (see :func:`derive`). A save fills.
    fill_rails: bool = True
    # WHICH PHASE DREW THIS PICTURE, and how to draw it again
    # at full content. See `session.FAST` / `session.FULL`: a panel edit paints
    # the library's answer in about a second and the kicad-cli content render -
    # 2.1 s on the corpus - replaces it behind that.
    content: str = "full"
    plan: object = None
    content_error: str = ""
    # WHICH TIER OF THE FAST PHASE drew it: `session.GRAPHICS` while the text
    # and the dimensions are still to come, `session.LETTERED` once they have.
    # The panel is where this matters most - its dimensions and its fab text are
    # 128 ms of a 233 ms render and 1.77 MB of a 3.40 MB fragment, measured.
    lettering: str = "lettered"
    letter_error: str = ""
    # WHY THIS PANEL'S POURS ARE STILL EMPTY, when the reason is a failure
    # rather than a wait. Non-empty stops the fill being asked for again and is
    # what the page says instead of promising copper that is not coming. The
    # panel itself stays valid, and clearly unfilled.
    fill_error: str = ""
    # THE CLEARANCE WARNING's own answer for this panel:
    # `clearance_view`'s mapping. Held on the view because a change to the
    # distance recomputes only this - no build, no render - and the page
    # redraws the marks from it.
    clearance: dict = {}


class Derived(NamedTuple):
    """One in-memory build, and the board text it was derived from.

    The TEXT is kept because it is the answer to "is this panel a picture of
    the design file?": it is the board exactly as a save would write it, so
    comparing it with the file on disk is a comparison of bytes rather than of
    intentions. It costs nothing extra - the round trip below has to produce it
    anyway.
    """

    outcome: object
    text: str
    # THE STEPPED PANEL TO KEEP for the next derivation, or None when this one
    # kept nothing. Handed back rather than stored here, because a cache with
    # no owner is a cache nobody drops: the session holds exactly one and drops
    # it wherever it touches the board (`preview.session`).
    cache: object = None
    # Where this derivation's time went: `(name, milliseconds)` pairs in the
    # order they happened, for the session's journal to say WHICH half an edit
    # paid for. Pairs and not a dict, so nothing shares a mutable default.
    phases: tuple = ()


def as_the_file_holds_it(board: Board) -> Board:
    """*board* with the precision the FILE will keep, and nothing more.

    MEASURED, and it is the whole reason this function exists. A
    board file carries about ten significant digits per coordinate, so a value
    prep has just computed at full double precision is not the value the file
    will hold. Derive the panel from the computed one and the panel's channel
    geometry lands picometres away from the panel a build produces from the
    saved file - different bytes, on a project whose defining requirement is
    that the same input gives the same output.

    Observed on the fixture: an arc midpoint of `-0.6286179285` against
    `-0.6286179284`, and `0` against `-0`, in the boolean-derived panel edge.
    Physically nothing; byte identity is byte identity.

    So the round trip is taken deliberately, through the library that owns the
    format - it decides the precision, not us - and the panel is derived from
    the board AS A SAVE WOULD WRITE IT. Verified stable: a second round trip
    changes nothing.
    """
    return Board.from_string(board.to_string())


class SourceSnapshot(NamedTuple):
    """The held board as the FILE will hold it: its text, and it parsed back.

    **MEASURED, and this type is the whole of the fix.** The round
    trip above is 682 ms of the corpus board's 1166 ms panel recalculation -
    451 ms to write 1.77 MB of text and 231 ms to read it back - which is more
    than half of what every edit cost, whether or not the board had moved. A
    tooling-hole diameter does not move a board, so paying for a re-reading of
    it was work with no answer in it.

    Both halves are kept because both are answers about the board and neither is
    an answer about the panel: `text` is what a save would write, which is how
    the page knows whether the panel is a picture of the design FILE, and
    `board` is the private copy at file precision that a build runs on.
    :func:`derive` never modifies either, which is fenced by a test - a build
    that mutated its source would make a second derive from one snapshot wrong.

    A snapshot is valid for exactly as long as the board it was taken from is
    unchanged. Whoever holds one is the party that knows: the session drops its
    snapshot wherever it touches the board (`preview.session`).
    """

    text: str
    board: Board


def snapshot_of(board: Board) -> SourceSnapshot:
    """Take one snapshot of *board*. This is where the round trip is paid."""
    text = board.to_string()
    return SourceSnapshot(text=text, board=Board.from_string(text))


# -- THE STEPPED PANEL, KEPT ------------------------------------------------
#
# MEASURED on the corpus panel: a live recalculation's build is
# 367 ms, of which 301 ms is `inject_board` deep-copying six instances and
# 96 ms is panelizing them. NONE of that depends on a tooling hole's diameter,
# a score switch or a dimension setting - so a carrier-only edit pays 66 ms of
# the 367 and skips the injection entirely (`step_and_repeat.SteppedBuild`).
#
# WHAT MAKES THAT SAFE IS THE KEY, and it is deliberately blunt. A cache that
# can serve a stale panel is worse than the third of a second it saves, and
# staleness bugs are easy to make. So:
#
# * the BOARD is fenced by the snapshot's own identity - the session drops its
#   snapshot wherever it touches the board, and a new snapshot object is a
#   different key even when it holds identical text;
# * the CONFIG is fenced by everything in it EXCEPT the four carrier sections.
#   Not by a list of layout keys: an allowlist of what may change is the only
#   direction that is safe to be wrong in, so a key nobody has written yet
#   invalidates the cache by default.
#
# The sections here are the stages `panelize.carrier` runs, and they are the
# whole of what a kept stepped panel does not know about. A stage joins this
# list only when it can change NOTHING the stepped panel holds - `fiducials`
# qualifies on the same grounds the tooling holes do: it appends footprints to
# the carrier and reads only the blank, the board field and the holes, none of
# which a stepped panel's six copies depend on. Fenced byte for byte in
# `tests/test_panel_fiducials.py`, as this list's own rule demands.

# `("panel", "scores")` IS NOT ON THIS LIST, because the rail score setback
# GROWS THE PANEL instead of moving the saw line. A setback is therefore a fact
# about the BLANK, the blank is measured in the stepped half, and a section that
# can resize the panel is layout - not carrier. Listing it here would let the
# cache serve a panel of the previous size. The cost is that any score edit
# re-steps; the direction that is safe to be wrong in is the one this list's
# own rule names.
CARRIER_SECTIONS = (
    ("panel", "tooling_holes"),
    ("panel", "rail_copper"),
    ("panel", "dimensions"),
    ("panel", "fiducials"),
    ("panel", "text"),
)

# ...AND THE ONE SETTING THAT MOVES NOTHING AT ALL (from the placement
# suite). It is read by something OTHER than the build, so a change to it has to
# leave the panel alone - which is the opposite of what the fingerprint's default
# says, and is therefore declared here by name and fenced byte for byte
# (`tests/test_placement_suite.py`):
#
# * `clearance_warning_mm` is a MEASUREMENT over a panel that already exists.
#   The viewport marks the pairs of boards it names; not one coordinate moves,
#   and the setting applies at once with no rebuild.
#
# Everything else in the config counts as layout whether or not anybody thought
# of it, which is the direction that is safe to be wrong in.
VIEW_ONLY_KEYS = (
    ("grid", "clearance_warning_mm"),
)


def layout_fingerprint(config) -> str:
    """Everything about *config* that moves the stepped panel, as one string.

    The config's own dump minus the carrier sections and the two settings that
    move nothing, so a value that is neither counts as layout whether or not
    anybody thought of it. Two configs with the same fingerprint step and
    panelize identically; two with different ones are not asked to.

    Every PLACEMENT key - rotation, alternation, the nest shifts, the placement
    rows - is therefore layout by default and invalidates a kept stepped panel,
    which is exactly right: each of them moves a copy.
    """
    data = config.model_dump(mode="json")
    for *path, name in (*CARRIER_SECTIONS, *VIEW_ONLY_KEYS):
        section = data
        for key in path:
            section = section.get(key) if isinstance(section, dict) else None
            if section is None:
                break
        if isinstance(section, dict):
            section.pop(name, None)
    return json.dumps(data, sort_keys=True, default=str)


class SteppedCache(NamedTuple):
    """One kept stepped panel, and the two things it is only true for."""

    snapshot: SourceSnapshot
    fingerprint: str
    built: object

    def serves(self, snapshot: SourceSnapshot, fingerprint: str) -> bool:
        """Is this cache the answer for this board and this config?

        The snapshot is compared by IDENTITY. A snapshot object is taken once
        per change to the held board and thrown away by whoever changes it, so
        identity is the strongest statement available here and the cheapest -
        and it fails in the safe direction, rebuilding when a new snapshot
        happens to carry the same text.
        """
        return self.snapshot is snapshot and self.fingerprint == fingerprint


def derive(config, source, fill_rails: bool = True,
           snapshot: SourceSnapshot = None,
           cache: SteppedCache = None) -> Derived:
    """Build the panel from a board ALREADY IN MEMORY. Writes nothing.

    *source* is not modified - the round trip works on a copy, `inject_board`
    deep-copies everything it injects, and the panel half works on the panel
    board - so the prep page keeps editing the object this was derived from.

    *snapshot* is a :class:`SourceSnapshot` a caller has already taken of
    *source* and knows to still be true. Handing one in skips the round trip,
    which is what makes a repeated panel-only edit cost the build and nothing
    else. Absent one, this takes its own - so a caller with no opinion about
    staleness gets the safe answer.

    *cache* is the :class:`SteppedCache` this function handed back last time.
    When it is the answer for this board and this config, the six copies and
    the panel round them are REUSED and only the carrier features run again -
    onto a copy, so the kept panel stays the pre-carrier panel it is
    (`step_and_repeat.cheap_copy`). Otherwise it is rebuilt, and the rebuilt one
    comes back on `Derived.cache` for the next call. A caller that hands nothing
    in pays the full build and may ignore what comes back; only a caller that
    means to reuse one has to hold it, and holding it is what makes it its job
    to drop it.

    A FILLED derive keeps nothing and reuses nothing - the save path is
    untouched by all of this, which is the same rule it already had about the
    snapshot.

    **`fill_rails=False` is what keeps the page responsive.** Filling the rail
    pour is KiCad's own work and costs most of ten seconds - measured: 9.9 s of
    a 10.5 s all-stages build - which is more than a change may cost when a
    change recalculates on entry. So a live rebuild derives the pour's OUTLINE
    and leaves it empty, and the fill is taken afterwards, off the responsive
    path, and transplanted into this very panel (:func:`take_fill`,
    :func:`apply_fill`). The picture the user ends up looking at carries real
    KiCad copper; the picture one second after an edit does not, and says so.
    """
    if config.stage != "build_panel":
        raise ValueError(
            f"this config's stage is {config.stage!r}, so it does not build a "
            f"panel. Set \"stage\": \"build_panel\" in panel.json and the "
            f"panel page can derive one."
        )
    taken = snapshot if snapshot is not None else snapshot_of(source)
    # THE SAVE PATH IS UNTOUCHED. A filled derive is the file a fabricator
    # gets: it steps the board itself, keeps nothing and reuses nothing, and
    # the cache it was handed comes back exactly as it arrived.
    if fill_rails:
        return Derived(
            outcome=step_and_repeat.build_from(
                config, taken.board, fill_rails=fill_rails
            ),
            text=taken.text,
            cache=cache,
        )

    phases: list = []
    mark = time.monotonic()

    def took(name: str) -> None:
        nonlocal mark
        now = time.monotonic()
        phases.append((name, round((now - mark) * 1000.0, 1)))
        mark = now

    fingerprint = layout_fingerprint(config)
    if cache is not None and cache.serves(taken, fingerprint):
        # THE CARRIER HALF ALONE, onto a copy of the panel that was kept. The
        # copy is `finish`'s own doing, so what the cache holds stays the panel
        # before any carrier feature however many edits are made against it.
        outcome = step_and_repeat.finish_from(config, cache.built,
                                             fill_rails=fill_rails)
        took("carrier_only")
    else:
        # The whole build, through the entry the command line uses, so that a
        # live panel and a built one cannot come from two different functions.
        outcome = step_and_repeat.build_from(config, taken.board,
                                             fill_rails=fill_rails)
        took("build_full")
        # A build that kept no stepped panel is not cached, and the next edit
        # builds again. Nothing in this package does that today; a caller that
        # replaced the build seam might, and the safe direction is to rebuild.
        cache = (
            SteppedCache(snapshot=taken, fingerprint=fingerprint,
                         built=outcome.stepped)
            if outcome.stepped is not None else None
        )
    return Derived(
        outcome=outcome,
        text=taken.text,
        cache=cache,
        phases=tuple(phases),
    )


# -- THE POUR: a KiCad round trip, paid behind the picture --------------------
#
# Copper fills need a KiCad round trip: the pours are added in memory, written
# to a temporary file, filled by kicad-cli, and read back into memory. So the
# panel the user SEES carries real KiCad copper, not an outline standing in for
# it - and the ten seconds that costs is paid off the responsive path
# (`preview.session`, the trailing phase), never inside an edit.
#
# The two calls are `refill`'s two halves and they are kept apart for one
# reason: `take_fill` is ten seconds and changes nothing, `apply_fill` is
# milliseconds and changes the board. The session runs the first with no lock
# held and the second under the board-write lock.
#
# MEASURED on the corpus panel: a panel built unfilled and filled
# afterwards is BYTE-IDENTICAL to the same panel built with `fill_rails=True`,
# and two fills of one panel agree byte for byte. That is what makes this a
# saving mechanism rather than a second kind of panel: the files a save writes
# after a late fill are the files a CI runner's build writes.


def pour_of(outcome):
    """The rail pour this build made, or None when it made none."""
    made = getattr(outcome, "panelize", None)
    return None if made is None else made.rail_copper


def owes_fill(view) -> bool:
    """Does this panel carry a pour that nothing has filled yet?

    False for a panel with no pour, for one whose pour came out empty of
    rectangles - there is nothing for KiCad to do - and for one whose fill has
    already landed or already failed.
    """
    if view is None or getattr(view, "fill_error", ""):
        return False
    if getattr(view, "fill_rails", False):
        return False
    poured = pour_of(getattr(view, "outcome", None))
    return poured is not None and poured.count > 0 and poured.refill is None


def take_fill(outcome, cli_path=None, work_dir=None, cancel=None):
    """Ask KiCad for the pours' copper. THE SLOW HALF, and it changes nothing.

    Returns a :class:`~eedesignpanels.stages.refill.FillTaken`, or None when this
    panel has no pour to fill. Nothing on *outcome* is modified, so a caller may
    throw the answer away when the panel it was taken for has moved on.

    *cancel* is passed straight through: a newer zone-affecting edit sets it and
    the running KiCad is STOPPED rather than waited out (`refill.take_fills`).

    Raises:
        refill.RefillError: when kicad-cli is missing, fails or loses a zone.
        refill.RefillCancelled: when *cancel* is set. The panel still owes a
            fill, and the edit that cancelled this one is about to ask.
    """
    if pour_of(outcome) is None:
        return None
    kwargs = {} if cli_path is None else {"cli_path": cli_path}
    return refill.take_fills(
        outcome.board, rail_copper.pour_zones(outcome.board),
        work_dir=work_dir, cancel=cancel, **kwargs
    )


def apply_fill(outcome, taken):
    """Put *taken*'s copper on the panel. `(outcome, result)`. THE FAST HALF.

    The outcome comes back carrying the fill's own numbers, so the report under
    the picture stops saying the pours are empty and starts saying what is in
    them. The board is modified in place - it is the panel this session holds,
    and after this call a save writes filled copper.

    Raises:
        refill.RefillError: when the answer does not match the panel's zones, or
            when the transplant changed anything but those zones' fills.
    """
    poured = pour_of(outcome)
    if poured is None or taken is None:
        return outcome, None
    result = refill.transplant(
        outcome.board, rail_copper.pour_zones(outcome.board), taken
    )
    return (
        outcome._replace(
            panelize=outcome.panelize._replace(
                rail_copper=poured._replace(refill=result)
            )
        ),
        result,
    )


def fill_pours(outcome, cli_path=None, work_dir=None):
    """The round trip in one call: `(outcome, result)`. For a caller with no page.

    A test, a script, a save that has nobody to stay responsive for. A live
    preview uses the two halves.
    """
    taken = take_fill(outcome, cli_path=cli_path, work_dir=work_dir)
    return apply_fill(outcome, taken)


def fills_on_save(config) -> bool:
    """Does this config ask for a pour at all?

    The one question the save has to ask before deciding whether to wait for a
    fill: with no rail copper there is nothing to fill, and waiting for nothing
    would put ten seconds in front of every save.
    """
    panel = getattr(config, "panel", None)
    # The group's own `enabled`, through the accessor the build reads: the
    # section is always in the file, so its presence answers nothing.
    return panel is not None and panel.rail_copper_settings is not None


# -- THE CLEARANCE WARNING ------------------------------
#
# PAGE-SIDE, ALWAYS. The distance is a measurement over the rectangles a build
# already produced: it moves no coordinate, it writes no file, and it is
# excluded from the layout fingerprint above so a change to it rebuilds
# nothing. What the page gets is a list of spans in BOARD MILLIMETRES - the
# same coordinates every layer in the viewport is drawn in, because the render
# seam normalises every export into the caller's own window - so the overlay
# needs no transform of its own.


def clearance_of(config) -> float:
    """The clearance warning distance this config states. Zero is off."""
    grid = getattr(config, "grid", None)
    return float(getattr(grid, "clearance_warning_mm", 0.0) or 0.0)


def clearance_view(config, outcome) -> dict:
    """What the viewport draws for the clearance warning, in board millimetres.

    `{"distance_mm": …, "found": N, "marks": [{"gap_mm", "at", "to"}, …]}`, and
    an empty `marks` when the setting is off or nothing is close. `found` may
    exceed the marks drawn: the worst are drawn and the rest are counted, so a
    nested arrangement does not put four hundred labels on one picture.
    """
    distance = clearance_of(config)
    boxes = list(outcome.geometry.instance_boxes) if outcome is not None else []
    marks, found = placement_math.clearance_marks(boxes, distance)
    return {
        "distance_mm": distance,
        "found": found,
        "marks": [
            {
                "gap_mm": round(mark.gap_mm, 4),
                "boards": [mark.first + 1, mark.second + 1],
                "at": [round(mark.at[0], 4), round(mark.at[1], 4)],
                "to": [round(mark.to[0], 4), round(mark.to[1], 4)],
            }
            for mark in marks
        ],
    }


def clearance_lines(config, outcome) -> list:
    """What the report says about the clearance warning. Silence when it is off.

    A count and the worst figure, in millimetres, and the fact that it is
    measured over the boards' RECTANGLES - which for a shaped board is the
    pessimistic answer and the safe direction for a warning.
    """
    view = clearance_view(config, outcome)
    distance = view["distance_mm"]
    if not distance:
        return []
    found = view["found"]
    if not found:
        return [
            f"No two boards are closer than {distance:g} mm, measured between "
            f"their rectangles."
        ]
    worst = min(mark["gap_mm"] for mark in view["marks"])
    drawn = len(view["marks"])
    tail = f" The {drawn} closest are marked." if drawn < found else ""
    return [
        f"{found} pair" + ("s" if found != 1 else "")
        + f" of boards are closer than {distance:g} mm - the tightest is "
        f"{worst:.3f} mm, measured between their rectangles.{tail}"
    ]


def unsaved_sentence(board_edits: int, config_edits: int,
                     board_differs: bool = False) -> str:
    """What the page says when the panel reflects work the files do not carry.

    The whole reason the panel is derived from memory, stated rather than
    implied: a panel built from an unsaved decision is a true picture of that
    decision and a false picture of the design on disk.

    *board_differs* is the case counting edits cannot see, and it is real -
    measured on the corpus. Prep OWNS the toolpath layer and
    regenerates it on every compute, so a board file whose toolpath was written
    by an older run differs from the board in memory with nobody having edited
    anything. The panel is derived from the NEW toolpath, which is the right
    answer and a surprising one, so it is said out loud.
    """
    parts = []
    if board_edits:
        parts.append(
            f"{board_edits} unsaved board change"
            + ("s" if board_edits != 1 else "")
        )
    if config_edits:
        parts.append(
            f"{config_edits} unsaved config change"
            + ("s" if config_edits != 1 else "")
        )
    if board_differs and not board_edits:
        parts.append(
            "a prepared board the design file does not carry yet - press Save "
            "to Board on step 1 to put it there"
        )
    if not parts:
        return ""
    return f"this panel reflects {' and '.join(parts)}"


def subtitle_for(config, outcome, board_edits: int = 0,
                 config_edits: int = 0, board_differs: bool = False) -> str:
    """The one line over the picture: what this panel is, and how big it came out.

    A designer's sentence: the arrangement, the size, and - when there is one -
    the warning that the files do not say this yet.
    """
    geometry = outcome.geometry
    extent = (
        outcome.panelize.blank if outcome.panelize is not None else None
    )
    size = (
        f"{extent.width_mm:.2f} x {extent.height_mm:.2f} mm panel"
        if extent is not None else
        f"{geometry.outline_extent_mm[0]:.2f} x "
        f"{geometry.outline_extent_mm[1]:.2f} mm of boards"
    )
    said = unsaved_sentence(board_edits, config_edits, board_differs)
    tail = f" - {said}" if said else ""
    return f"{config.grid.summary} - {size}{tail}"


def report_lines(config, outcome, prepared=None) -> list:
    """What the build did, one plain line at a time, for the page to list.

    Numbers a designer can check by hand, in the order a panel is doubted in:
    how many boards and how they sit, then the material they sit in, then what
    is cut out of it, then what the saw takes, then what became of the copper.

    *prepared* is step 1's `route_toolpath.PrepResult` when the caller has one.
    It is what carries the ROUTING JOBS - every closed loop on the board, cut or
    not, so the report says what was cut and what was not - and the panel
    cannot work them out for itself, because by the time it
    exists the holes are channels on the cutter-path layer like any other. A
    caller with no prep result leaves those lines out rather than guessing.
    """
    geometry = outcome.geometry
    instances = config.grid.columns * config.grid.rows
    source = geometry.outline_box
    across, down = geometry.instance_extent_mm
    sits = (
        "" if (across, down) == (source.width, source.height)
        else f" It sits {across:.2f} x {down:.2f} mm as turned."
    )
    placed = config.grid.placement is not None
    out = [
        f"{instances} board" + ("s" if instances != 1 else "")
        + f", {config.grid.summary}.",
        f"One board measures {source.width:.2f} x {source.height:.2f} mm."
        + sits
        + (
            " A placement file says where each copy goes, so there is no step."
            if placed else
            f" The step is {geometry.extent.step_x_mm:.2f} x "
            f"{geometry.extent.step_y_mm:.2f} mm."
        ),
    ]
    turned = sum(1 for place in geometry.placements if place.turns)
    if turned:
        out.append(
            f"{turned} of the {instances} copies are turned, and each carries "
            f"its upright text where KiCad's own turn would draw it - upright, "
            f"in the same place beside the part."
        )
    out += clearance_lines(config, outcome)
    # WHAT THE ROUTER CUT ON ONE BOARD, AND WHAT IT DID NOT. It sits with the
    # board's own numbers, above everything about
    # the carrier: a hole the cutter cannot make is a fact about the design, and
    # it is the same fact however the panel is arranged.
    if prepared is not None and getattr(prepared, "jobs", ()):
        out += cuttability.report_lines(
            prepared.jobs, prepared.cutter_diameter_mm)

    made = outcome.panelize
    if made is None:
        out.append(
            "This config has no carrier, so the boards are stepped and repeated "
            "and keep their own outlines. Add a rail width to panelize them."
        )
        return out

    blank, boundary = made.blank, made.boundary
    out.append(
        f"The panel is {blank.width_mm:.2f} x {blank.height_mm:.2f} mm, "
        f"with {made.rails.summary} round the boards."
    )
    out.append(
        f"The panel edge is {boundary.loop_count} loop"
        + ("s" if boundary.loop_count != 1 else "")
        + f", {boundary.item_count} item"
        + ("s" if boundary.item_count != 1 else "")
        + f", {boundary.length_mm:.1f} mm long, measuring "
        f"{boundary.extent_mm[0]:.2f} x {boundary.extent_mm[1]:.2f} mm."
    )
    out.append(
        f"The router cuts {made.channel_runs} run"
        + ("s" if made.channel_runs != 1 else "")
        + f" and {made.channel_length_mm:.1f} mm of path, staying "
        f"{boundary.clearance_mm:.2f} mm clear of the outer edge."
    )
    scores = made.scores
    if scores.replaced or scores.merged_count:
        out.append(
            f"{scores.merged_count} saw pass"
            + ("es" if scores.merged_count != 1 else "")
            + f" replace {scores.replaced} score line"
            + ("s" if scores.replaced != 1 else "")
            + "."
        )
    if made.joins:
        many = len(made.joins) != 1
        out.append(
            f"{len(made.joins)} sliver{'s' if many else ''} of material "
            f"narrower than {made.min_tab_mm:.2f} mm "
            f"{'were' if many else 'was'} cut away."
        )
    if made.protected_tabs:
        out.append(
            f"{len(made.protected_tabs)} declared tab"
            + ("s are" if len(made.protected_tabs) != 1 else " is")
            + f" narrower than {made.min_tab_mm:.2f} mm and were kept as drawn."
        )
    flat = made.zones
    out.append(
        f"{flat.polygons} copper fill"
        + ("s" if flat.polygons != 1 else "")
        + f" became plain copper and {flat.zones_removed} zone"
        + ("s" if flat.zones_removed != 1 else "")
        + f" went with them. {flat.rule_areas_kept} keepout"
        + ("s" if flat.rule_areas_kept != 1 else "")
        + " survived into the panel."
    )
    out.append(
        f"{made.strip.total} board-outline item"
        + ("s" if made.strip.total != 1 else "")
        + " the copies brought were removed; the panel edge is derived."
    )
    out.extend(carrier_lines(made))
    # DOES IT COME APART, AND IN WHAT ORDER. Last in the
    # report because it is the question a fabricator asks after every other
    # number checks out, and it is the only one whose answer is a sequence.
    out.extend(comes_apart.report_lines(breakout_of(outcome)))
    return out


def breakout_of(outcome):
    """Does this panel come apart? None when the config asked for no carrier.

    MEASURED 11 ms on the corpus panel (`comes_apart.verdict` timed around a
    5.3 s build), so it is computed where it is wanted rather
    than cached: the report and the V-Scores status each ask for their own.
    """
    made = outcome.panelize
    if made is None:
        return None
    return comes_apart.verdict(outcome.board, made, outcome.geometry)


def breakout_state(outcome) -> dict:
    """The V-Scores heading's status, as the page reads it. Empty says nothing."""
    check = breakout_of(outcome)
    if check is None:
        return {}
    return {"ok": check.comes_apart, "text": check.status}


def carrier_lines(made) -> list:
    """What each carrier stage did, or nothing at all when it did not run.

    SILENCE MEANS "NOT ASKED FOR". A zero would read as "asked for and got
    none", which is a different panel and a different conversation - the same
    rule the command line's own report keeps.
    """
    out = []
    holes = made.holes
    if holes is not None:
        placed = "the middle of each rail corner" \
            if holes.placement == "rail_corner_center" else "a fixed inset"
        out.append(
            f"{holes.count} tooling hole"
            + ("s" if holes.count != 1 else "")
            + f" of {holes.diameter_mm:g} mm, non-plated, at {placed}."
        )
        measured = [
            hole for hole in holes.holes if hole.clearance_mm is not None
        ]
        if measured:
            tightest = min(hole.clearance_mm for hole in measured)
            out.append(
                f"The tightest hole sits {tightest:.2f} mm clear of the "
                f"nearest edge."
            )
        for corner in holes.skipped:
            out.append(f"{corner.reason[0].upper()}{corner.reason[1:]}.")
    scores = made.panel_scores
    if scores is not None:
        if scores.protection_only:
            out.append(
                f"No panel saw pass was asked for. The "
                f"{scores.inherited_lines} pass"
                + ("es" if scores.inherited_lines != 1 else "")
                + " merged from the board edges are guarded all the same."
            )
        else:
            out.append(
                f"{scores.count} saw pass"
                + ("es" if scores.count != 1 else "")
                + " across the whole panel, measured from the blank's top-left "
                f"corner, and {scores.inherited_lines} merged from the board "
                f"edges."
            )
        out.append(
            f"{len(scores.rule_areas)} keep-out area"
            + ("s keep" if len(scores.rule_areas) != 1 else " keeps")
            + f" copper {scores.half_width_mm:g} mm clear of every saw line."
        )
        # A NUMBER THAT DID NOTHING NEVER LOOKS LIKE ONE THAT DID. Its own line
        # per ignored dependant, because a setback or a jump whose rail score is
        # off contributed nothing and the file still carries the number the
        # user typed.
        for skipped in scores.ignored:
            out.append(f"{skipped.sentence[0].upper()}{skipped.sentence[1:]}.")
        # A SHARED SAW LINE NEVER CHANGES MEANING SILENTLY. One line, two
        # possible cuts, and THE TOOL CANNOT TELL which is wanted - so the run
        # says how many it found and which way the user's setting took each one.
        if scores.shared_lines:
            out.append(
                f"{len(scores.shared_lines)} saw line"
                + ("s are" if len(scores.shared_lines) != 1 else " is")
                + " shared by a rail score and a score the boards decided."
            )
            for line in scores.shared_lines:
                out.append(f"{line.sentence[0].upper()}{line.sentence[1:]}.")
        if scores.inherited_guarded:
            # The panel adds nothing over a line the boards already guard, so
            # the count above is the panel's own and this is the rest of it.
            out.append(
                f"{scores.inherited_guarded} saw line"
                + ("s are" if scores.inherited_guarded != 1 else " is")
                + " guarded end to end by the keep-out the boards decided."
            )
    out.extend(jump_lines(made.jumps))
    poured = made.rail_copper
    if poured is not None:
        where = ("the rails and the gaps between boards"
                 if poured.pour_between_boards else "the rails")
        out.append(
            f"{poured.count} copper pour"
            + ("s" if poured.count != 1 else "")
            + f" over {where}, on {len(poured.layers)} copper layer"
            + ("s" if len(poured.layers) != 1 else "")
            + f", held {poured.clearance_mm:g} mm off everything."
        )
        filled = poured.refill
        if filled is None and poured.count:
            out.append(
                "The pours are empty in this picture: only KiCad can fill "
                "them, which takes several seconds - the picture is drawn "
                "again when that copper lands, and a save waits for it."
            )
        elif filled is not None:
            out.append(
                f"{filled.zones_filled} pour"
                + ("s" if filled.zones_filled != 1 else "")
                + f" carry copper, {filled.records} fill record"
                + ("s" if filled.records != 1 else "")
                + f" transplanted in {filled.seconds:.1f} s."
            )
    return out


def jump_lines(jumps) -> list:
    """What jump scoring did, or nothing when no distance was asked for.

    SILENCE MEANS ZERO HERE TOO. A panel whose distances are both zero is the
    full-span panel, and a line saying "0 mm short of the edge" would be a
    setting reported at every build for a setting nobody set.
    """
    if jumps is None or not jumps.asked:
        return []
    out = [
        f"{jumps.count} board saw pass"
        + ("es stop" if jumps.count != 1 else " stops")
        + f" short of the panel edge: {jumps.horizontal_mm:g} mm at each end "
        f"of a horizontal pass, {jumps.vertical_mm:g} mm of a vertical one."
    ]
    for sentence in jumps.declined:
        out.append(f"{sentence[0].upper()}{sentence[1:]}.")
    return out
