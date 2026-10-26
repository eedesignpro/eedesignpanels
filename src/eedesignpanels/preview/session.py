"""One preview session: compute, render, and the decisions taken against it.

The session is the whole of the preview's state. It owns a config path, a
scratch folder, the BOARD and the CONFIG DATA it loaded once, and a GENERATION
counter that goes up every time the picture changes.

**Editing is in memory, and one action writes.** The board is parsed once at
launch and held; accept, decline and a parameter change edit that object and
the held config data, and nothing touches the project until :meth:`save` - the
"Save to Board" button - puts both on disk in one action. So an edit costs a
recomputation and no more: the 468 ms prettified write and the 210 ms re-parse
are paid once, when the designer says so, and not inside every click.

The cycle is one method. `refresh()` runs prep in PREVIEW mode over the held
board - in memory, writing nothing - renders the layers through
:mod:`eedesignpanels.preview.render_seam`, draws the proposal overlay, and bumps
the generation. Every action ends with the same call, so the page a designer
sees is never one edit behind the board being edited.

**Pending changes are counted, not diffed.** The count is how many edits have
been applied since the last save, and an edit that changed nothing - accepting
an already-scored edge, typing the number that is already there - does not
count. It is what the button says and what the unsaved-work prompt asks about.

**One action at a time.** An action gate is taken WITHOUT blocking for the
whole of a write-and-recompute, so a second click arriving inside that window
is refused (:class:`~eedesignpanels.preview.actions.Busy`) instead of queueing
behind it. The page disables its controls for the same window; the gate is what
makes that true rather than merely displayed.

**A CHANGE IS PAINTED IN TWO PHASES.** The content render is kicad-cli - a
process and a write of the whole board, measured at 0.84 s for the corpus board
and 2.1 s for its panel - and paying for it inside the edit would turn a
one-second edit into a three-second one. So `refresh()` renders through
the LIBRARY, publishes, and the page paints it and stays live; the content render
runs on a worker behind that and replaces those layers as a second push on the
same generation. The busy popup covers the fast phase only, the corner of a
viewport says when its content is still coming, and the three orderings - a stale
upgrade, a cancelled one, and two renders at once - are guarded where the
machinery is (see "THE SECOND PHASE" below).

**AND THE POUR IS A THIRD PHASE.** Rail copper is filled by KiCad and by
nothing else - 9.9 s on the corpus panel - which is slow but correct, so
the panel is derived with its pours empty, drawn, upgraded to full content, and
then the fill is taken in a sandbox and transplanted into the panel this session
HOLDS. The picture is drawn once more with the copper in it. Same discipline as
the second phase: a newer generation cancels a pending fill, a stale answer is
dropped rather than applied, and the round trip runs with no session lock held
because the only part that touches the board is the transplant.

A computation that FAILS is a state like any other. A board whose outline does
not close, or a config asking for something impossible, leaves the session
holding the reason and serving it - the alternative is a dead server and a
browser tab showing the last good picture, which is the worst of both.

**LINKED SETTINGS LIVE HERE, NOT ON A PAGE.** A value the two pages share is
the session's: it is set through a method on this object,
and setting it pushes to every connected page over the event stream they
already hold. `/layers` is one CALLER of that method and a browser is one hand
on it - a server-side change reaches every page exactly the same way, with no
page needed to originate it. The layer panel (:meth:`set_layer_visibility`) is
the first of these and the shape the rest will take: one validated setter, one
route that funnels into it, last change wins, and a sequence number so a page
can tell a change it has not seen from one it made itself.

A session may also carry a :class:`~eedesignpanels.preview.journal.UiSession` -
the launch's folder. When it does, every computation, every action and every
interaction the page reports is written to `journal.jsonl`, and
:meth:`PreviewSession.snapshot` saves the whole reviewable state under a shot
number. The journal is instrumentation and never a gate: a session with no
folder behaves exactly as before, which is what keeps the tests that assert
"preview writes nothing" honest.
"""

import contextlib
import hashlib
import json
import threading
import time
import traceback
from pathlib import Path
from typing import NamedTuple

from eedesigncore import Board

from eedesignpanels import (
    __version__,
    config as config_module,
    cuttability,
    prep,
)
from eedesignpanels.channels import ChannelError
from eedesignpanels.config import ConfigError, load_config
from eedesignpanels.preview import (
    actions,
    bugreport,
    checkrun,
    labels as labels_module,
    layer_alarms,
    naming,
    page_panel,
    page_served,
    panelbuild,
    render_seam,
    snapshot as snapshot_module,
)
from eedesignpanels.review import (
    holes as holes_module,
    overlay,
    render,
    theme as theme_module,
)
from eedesignpanels.stages import (
    board_copper,
    board_scores,
    edge_plating,
    refill,
    route_toolpath,
    step_and_repeat,
)
from eedesignpanels.toolpath import OffsetError

# What one `/journal` POST may carry, and how long one note may be. The page
# batches, so a burst of pans is one request - but a request is still a request
# and this is untrusted input, so its size is capped.
MAX_CLIENT_EVENTS = 400
MAX_NOTE_CHARS = 1000
MAX_EVENT_KIND_CHARS = 48

# The names the journal chain takes in a PARAMETER SLOT rather than in its
# `**data`. `Journal.append(self, kind, **data)` is a bound method, so `self`
# is already filled and `kind` is filled by the caller; a posted key of either
# name is not extra data, it is a second value for an argument, and Python
# refuses the whole call. Kept here because the page posts arbitrary keys and
# the journal is one file away, and a posted body is untrusted input.
RESERVED_LOG_KEYS = frozenset({"self", "kind"})
# What such a key is renamed to. Renamed and not dropped: the record still says
# everything the page sent, which is the whole point of keeping the journal.
RESERVED_LOG_PREFIX = "posted_"
# How many layer names one `/layers` POST may carry. KiCad 10 has 32 copper
# layers and 45 technical and user ones; anything past this is not a layer
# panel talking, and the body is untrusted input.
MAX_LAYER_NAMES = 128

# How much bare air the preview renders round the board, as a fraction of the
# outline's larger dimension. The candidate captions live out here, clear of
# the routed channel, and a stroke centred on the outline keeps its outer half
# instead of losing it to the viewport edge. It is a FLOOR: `view_window` grows
# it whenever a caption at the required clearance would not fit.
VIEW_MARGIN_FRACTION = 0.12


# What a view's pictures were drawn by, and therefore what it still owes its
# reader. FAST is the library renderer - graphics exact, no pad, track or zone -
# and FULL is the content render that replaces it.
FAST = "fast"
FULL = "full"

# ...AND THE FAST PHASE IS TWO TIERS. GRAPHICS is the fast render with no
# text, field or dimension in it - half the render and half the markup,
# measured - and LETTERED is the same picture with the lettering swapped in
# behind it. Drawing the lettering in the fast path takes a carrier edit's
# first paint from 150 ms to 260 ms, so the lettering is kept but drawn after
# the first paint, and the edit does not wait for it.
GRAPHICS = "graphics"
LETTERED = "lettered"

# How long the lettering tier waits for the graphics picture to actually be
# handed to a page before it takes the session lock to draw over it. The point
# of the tier is that the FIRST picture goes out first, and the library render
# holds that lock for as long as it draws - so starting it the instant the
# generation is published would make the page's own fragment request queue
# behind exactly the render it was supposed to arrive ahead of. A page reacts to
# the push in a few milliseconds; this is the cap for one that never asks at all
# (a session with no browser attached, a page mid-reload).
FIRST_PAINT_GRACE_SECONDS = 0.3

# Which PAGE a subject's picture is on. The upgrade counter is the session's,
# so a push says whose content landed: the board's content is not news to the
# panel page, and a fragment it has not changed is 2.7 MB nobody asked for.
UPGRADE_PAGES = {
    "board": page_served.PREP_STEP.page,
    "panel": page_served.PANEL_STEP.page,
}

# The corner note, when a viewport is waiting for its content. Deliberately one
# short line: it belongs in the corner of a picture, not across the top of it.
AWAITING_NOTE = (
    "copper, mask and paste are still being drawn - this picture is graphics "
    "only for a moment"
)

# The same corner, for the phase after it: the rail pour is being filled by
# KiCad and the picture's pours are outlines until it answers.
FILLING_NOTE = (
    "KiCad is filling the rail copper - the pours are empty in this picture "
    "for a few seconds"
)

# ...and the same corner on STEP 1. A score
# decision writes a copper keepout into the board, so the board's own zones are
# being filled against it - and until that lands, the copper in this picture is
# the copper the design was saved with, which still runs across the new line.
BOARD_FILLING_NOTE = (
    "KiCad is re-filling this board's copper against the score keepout - the "
    "copper here is the design's own for a few seconds"
)

# ...and when it could not be done. The picture stays valid and is clearly the
# copper the design was saved with, so the fault is not hidden.
BOARD_FILL_FAILED_NOTE = (
    "the board's copper could not be re-filled, so it still pours across the "
    "score: {error}"
)

# What a decision says the panel is out of date FOR, once the board's copper has
# moved under it. Its own sentence because it is not the decision that changed
# the panel this time - it is the copper the panel freezes.
BOARD_FILLED = "the board's copper was re-filled after a score decision"

# ...and the smallest of the three: the graphics are drawn and the text is not,
# because the tier that draws it failed. Said rather than left as a board that
# looks as though it has no text on it.
LETTERING_FAILED_NOTE = (
    "the text and dimensions could not be drawn, so this picture is graphics "
    "only: {error}"
)

# HOW LONG A FRAGMENT WAITED FOR THE SESSION LOCK, carried on the payload and
# taken straight back off it by the route that journals the request
# (`server.PreviewHandler._fragment`). It never reaches the wire.
#
# It needs a channel of its own because the wait and the work are not the same
# thing: a page that asks for a fragment while an edit is computing waits for
# that edit, and a breakdown showing the wait as "assembling the fragment"
# would send somebody optimising the wrong half. Measured: one
# request logged 671 ms, of which 640 was waiting for a layout edit to finish
# and 3 was the fragment itself.
LOCK_WAIT_KEY = "_lock_wait_ms"

# How long a save waits for a fill that is already in flight before it stops
# waiting and does the round trip itself. The round trip is about ten seconds
# on the corpus panel, so this is a guard against a wedged worker and never a
# working limit.
FILL_WAIT_SECONDS = 120.0


def safe_log_keys(data: dict) -> dict:
    """A posted dictionary, made safe to hand a `**data` logging call.

    A KEY OFF THE WIRE IS NEVER AN ARGUMENT NAME. Spreading a posted body into
    a call makes the sender's keys choose which parameters get filled, and the
    page sends a key called `kind` on every single interaction record - so
    without this, `/journal` would answer nothing but `TypeError`. Any name in
    :data:`RESERVED_LOG_KEYS` is prefixed rather than dropped, so the record
    keeps what the page said.

    A body that sends both `kind` and `posted_kind` collapses to one entry, the
    later of the two. That is a hand-built body arguing with itself and it is
    not worth a second field to record.
    """
    return {
        (RESERVED_LOG_PREFIX + key if key in RESERVED_LOG_KEYS else key): value
        for key, value in data.items()
    }


class Phases:
    """Milliseconds per named phase, in the order they happened.

    **THE BREAKDOWN IS A STANDING ARTIFACT.** Interaction latency matters, and
    "an edit takes about a second" is not a number anybody can act on; a named
    chain of phases is, and it is what :mod:`eedesignpanels.preview.timing`
    turns into a table.

    One object per computation, written into the journal record that computation
    ends in. Nothing reads it to decide anything - it is instrumentation, and a
    session with no journal pays two `monotonic` calls per phase for it.
    """

    def __init__(self):
        self.marks = {}
        self._mark = time.monotonic()

    def took(self, name: str) -> dict:
        """Close the phase that just finished, under *name*. The marks so far."""
        now = time.monotonic()
        self.marks[name] = round((now - self._mark) * 1000.0, 1)
        self._mark = now
        return self.marks

    def update(self, pairs) -> dict:
        """Fold in a nested stage's own `(name, ms)` pairs, in its own words."""
        self.marks.update(dict(pairs))
        return self.marks

    def total(self) -> float:
        """Every phase added up, in milliseconds."""
        return round(sum(self.marks.values()), 1)


class RenderPlan(NamedTuple):
    """Everything needed to draw one subject AGAIN, at full content.

    Held by the view the fast phase produced, so the upgrade renders exactly
    what was rendered - same board object, same layers, same window, same
    colours - and differs in one argument: `content=True`.
    """

    subject: str
    board: object
    layers: tuple
    window: tuple
    colors: dict
    theme_name: str
    workdir: object
    svg_dir: object
    origin: tuple


class Rendered(NamedTuple):
    """One computed, rendered view of the board - the thing the page is made of."""

    result: object
    layers: list
    svg_by_layer: dict
    theme: object
    subtitle: str
    bbox: tuple
    # The rendered window: the outline's bounding box with room round it.
    window: tuple = ()
    # `{canonical: display}` for every layer the board declares, so nothing the
    # page says has to fall back to a bare `User.##` (see `preview.naming`).
    layer_names: dict = {}
    # Every candidate numbered `#1`, `#2` in physical order.
    labelled: tuple = ()
    # The run's notes, in the reader's layer names.
    notes: tuple = ()
    # Why this toolpath must not be cut, if it must not (`checks.toolpath_alarms`).
    # Non-empty is what paints the toolpath layer in the alarm colour.
    alarms: tuple = ()
    # EACH LAYER'S MARKUP, KEPT - see :meth:`_write_svgs`.
    # What a fragment sends, held rather than read back off the documents this
    # session has just written: the first read of the set cost 345 ms against
    # 9 ms warm, once per edit, inside the first paint. None means "not kept",
    # and the fragment reads the files instead.
    svg_inner: dict = None
    # WHEN EACH LAYER'S MARKUP LAST CHANGED, as a paint number
    # (`PreviewSession.paint_seq`). What lets a fragment carry the layers that
    # have moved since the page's own last paint and no others.
    svg_stamp: dict = None
    # WHICH PHASE DREW THESE PICTURES: `FAST` while the content render is still
    # to come, `FULL` once it has landed (or when there was only ever one phase).
    content: str = FULL
    # ...AND WHICH TIER OF THE FAST PHASE: `GRAPHICS` while the lettering is
    # still to come, `LETTERED` once it has landed or when it was never left
    # out. A content render draws everything, so it lands LETTERED too - which
    # is what stops a library render being drawn over kicad-cli's own picture.
    lettering: str = LETTERED
    letter_error: str = ""
    # How to draw this subject again at full content, and why that failed if it
    # did. A failure is SAID - the page carries it in the corner note - rather
    # than leaving a picture quietly short of its copper.
    plan: object = None
    content_error: str = ""


class Pending(NamedTuple):
    """One edit made in memory and not yet saved."""

    kind: str
    message: str


def view_window(bbox: tuple, clearance_mm: float = 0.0) -> tuple:
    """The `(x, y, w, h)` the preview renders: the outline's box, with air round it.

    Room is not decoration. The candidate captions sit off the board edge and
    would be clipped by a window cropped to it, and a stroke centred on the
    outline loses its outer half to a viewport edge that lands on the outline.

    *clearance_mm* is how far off the outline a caption sits
    (:func:`overlay.label_clearance_mm`). That distance is MILLIMETRES - it
    comes from the cutter - while the margin is a fraction of the board, so on a
    small board with a wide bit the fraction is not enough and the window grows
    to fit. The caption's own width is counted too: the widest one runs away
    from its anchor, so the margin has to hold both. The font is itself a
    fraction of the WINDOW, which is why the arithmetic below solves for the
    margin rather than adding one.
    """
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    larger = max(width, height)
    room = (
        overlay.LABEL_CAPTION_CHARS
        * overlay.LABEL_CHAR_WIDTH
        * overlay.LABEL_FRACTION
        * overlay.OUTSIDE_LABEL_FACTOR
        * overlay.LABEL_FIT_HEADROOM
    )
    needed = (clearance_mm * overlay.LABEL_FIT_HEADROOM + room * larger) / (
        1.0 - 2.0 * room
    )
    margin = max(larger * VIEW_MARGIN_FRACTION, needed)
    return (bbox[0] - margin, bbox[1] - margin,
            width + 2.0 * margin, height + 2.0 * margin)


def render_notes() -> list:
    """What the page owes its reader about the renderer that drew it.

    One sentence today, and only on a machine with no kicad-cli: the fast
    renderer draws graphics and holes and no pad, track or zone, so the copper,
    mask and paste rows on a page it drew are empty. A shortfall a reader is not
    told about is a picture the reader will believe.
    """
    notice = render_seam.content_notice(render_seam.preview_backend())
    return [notice] if notice else []


def _error_point(exc):
    """The board coordinate an exception names, or None.

    The failed viewport states where, so the place a stage gave up at is read
    off the exception rather than parsed back out of its sentence.
    `OffsetError` and `ChannelError` carry `at`; anything else does not, and a
    failure with no place gets no place line.
    """
    at = getattr(exc, "at", None)
    if at is None:
        return None
    try:
        point = tuple(float(value) for value in at)
    except (TypeError, ValueError):
        return None
    return point if len(point) == 2 else None


class PreviewSession:
    """The live preview's state, and the only thing allowed to change it.

    *render* is the seam. It defaults to
    :func:`render_seam.render_layers` and is injectable so the session can be
    driven with no kicad-cli anywhere - which is what lets the server be tested
    in-process.

    *journal* is the launch's folder, or None. It is optional so that nothing
    about the session's behaviour depends on being recorded.
    """

    def __init__(self, config_path, workdir, render=None, theme=None,
                 journal=None, compare=None, two_phase=None, fill=None,
                 fills=None, tiered=None, board_fill=None, board_fills=None,
                 startup=None):
        self.config_path = Path(config_path).resolve()
        # HOW THE LAUNCH GOT ITS SETTINGS, or None when nobody said. A
        # `config.ConfigStartup` whose `problem` is set is a launch running on
        # STANDARD SETTINGS HELD IN MEMORY, because the file on disk would not
        # load and is not going to be touched until the user saves. The page
        # carries the warning; this session carries the settings the file
        # could not give it.
        self.startup = startup
        self.startup_warning = getattr(startup, "problem", "") or ""
        self._seed_text = (
            config_module.config_text(startup.data)
            if self.startup_warning else ""
        )
        self.workdir = Path(workdir)
        self._render = render or render_seam.render_layers
        # THE POUR SEAM: the slow half of the fill, which is the half that needs
        # kicad-cli (`panelbuild.take_fill`). Injectable for the same reason the
        # renderer is - a test drives the ordering with something it can hold
        # open - and the transplant behind it is always the real one, so a test
        # exercises the real gate.
        self._fill = fill or panelbuild.take_fill
        # TWO-PHASE PAINTING is on when there are two backends to be two phases
        # of. A caller that supplied its OWN renderer supplied both phases as
        # one function, so the default is one phase for it - a test drives the
        # ordering by passing `two_phase=True` with a renderer it can hold open.
        self.two_phase = (
            (render is None and render_seam.two_phase_available())
            if two_phase is None else bool(two_phase)
        )
        # TWO TIERS INSIDE THE FAST PHASE, on the same terms: on when the fast
        # render is the library's, because only the library can be asked to
        # leave the lettering out. A caller that injected a renderer supplied
        # one tier as one function, so the default is one tier for it - a test
        # asks for two with `tiered=True` and a renderer that takes the
        # `lettering` argument.
        self.tiered = (
            (render is None and self.two_phase
             and render_seam.tiered_available())
            if tiered is None else bool(tiered)
        )
        # DOES THE POUR FILL BEHIND THE PICTURE? Only KiCad can fill it, so on a
        # machine with no kicad-cli the answer is no and the page says the pours
        # are empty - which is the honest answer and the one it always gave. A
        # caller that injected a filler has said yes by doing so; a caller that
        # injected only a renderer is a test of something else and gets no
        # KiCad process it did not ask for.
        self.fills_live = (
            (fill is not None
             or (render is None and refill.cli_present()))
            if fills is None else bool(fills)
        )
        # THE BOARD'S OWN POUR SEAM. A score decision writes a copper keepout
        # into the SOURCE board, so the source board's zones have to be filled
        # against it before the panel freezes them into graphics - see
        # `eedesignpanels.stages.board_copper`. Same shape as the pour above:
        # the slow half is injectable, the transplant behind it is always the
        # real one.
        self._board_fill = board_fill or board_copper.take
        # ...and it is live on a NARROWER test than the panel's. A caller that
        # injected any seam at all is testing something else and does not get a
        # KiCad process it did not ask for: the panel's filler being faked says
        # nothing about whether this one should run for real.
        self.board_fills_live = (
            (board_fill is not None
             or (render is None and fill is None and refill.cli_present()))
            if board_fills is None else bool(board_fills)
        )
        self._compare = compare or checkrun.compare_saved_panel
        self._theme_name = theme
        self.journal = journal
        self._lock = threading.RLock()
        # THE BOARD LOCK, and it is deliberately NOT the session lock. The
        # kicad-cli backend switches every `Value` off while it writes a board
        # out, which is a change to an object this session holds - so
        # everything that READS a board has to be kept out of that window. With
        # the session lock doing that job, the cost lands on the wrong party:
        # a page asking for a fragment during a content render waits 655 ms
        # for it on the corpus panel, measured, INSIDE the first paint of the
        # edit the user has just made - and with a knock-on, because the
        # lettering tier's grace then runs out and the first paint becomes the
        # lettered picture, 3.9 MB of it instead of 2.0 MB.
        #
        # A fragment needs no board at all. So a board's readers and writers
        # take THIS lock, and the fragment, the poll and the page take the
        # session lock and are never held up by a render.
        #
        # ONE ORDER, ALWAYS: `_lock` and then `_board_lock`. The computations
        # hold the session lock and take this one inside it; the tier workers
        # take this one ALONE to draw and then the session lock ALONE to apply.
        # No path takes them the other way round, which is the whole of what
        # makes the pair safe.
        self._board_lock = threading.RLock()
        # The action gate. Separate from `_lock` and never held while waiting
        # for it: it is asked for without blocking, so a second click during a
        # recompute is REFUSED rather than queued behind it.
        self._gate = threading.Lock()
        self._running = ""
        self._subscribers = []
        self.generation = 0
        self.config = None
        # The config file's own data, held so a parameter edit is a change to an
        # object and a save is one write of it.
        self.data = None
        # The design, parsed once. Every edit is a change to THIS object.
        self.board = None
        # THE BOARD AS THE FILE WOULD HOLD IT, kept between derivations. See
        # `panelbuild.SourceSnapshot` for what it is and :meth:`_board_touched`
        # for the one rule about it: whoever changes the board drops it.
        self._snapshot = None
        # THE STEPPED PANEL, kept between derivations for the same reason and
        # under the same rule (`panelbuild.SteppedCache`): six copies of the
        # board and the panel round them, which no carrier setting can move. A
        # carrier-only edit reuses it and skips the injection entirely.
        self._panel_cache = None
        self.view = None
        self.error = ""
        # WHERE THE FAILURE IS, in board millimetres, or None when the stage
        # named no place. `toolpath.OffsetError` and `channels.ChannelError`
        # both carry the point they gave up at, and it is held separately from
        # the sentence so the failed viewport can put it on its own line and
        # the problem-report zip can carry it as a number.
        self.error_at = None
        self.message = ""
        # WHICH LAYERS ARE IN AN ALARM STATE, AND WHY:
        # `{canonical layer: (reason, ...)}`, and it is the
        # session's own fact rather than a colour one paint applied. Every
        # render reads it (:meth:`_draw`), the swatch and the page's status line
        # read it, and it clears only when the prep run that set it stops
        # setting it - never because a rebuild finished
        # (:mod:`eedesignpanels.preview.layer_alarms`).
        self.layer_alarms = {}
        self.pending = []
        # WHAT THE LOAD-TIME KEEP-OUT REPAIR FOUND, or None when the board
        # arrived agreeing with itself. Held rather
        # than recomputed because it is a fact about one read of one file, and
        # the prep page says it for as long as the repair is unsaved.
        self.score_repair = None
        # -- THE BOARD'S COPPER, and whether it answers to the board's keepouts
        # HOW MANY DECISIONS HAVE MOVED THE BOARD'S SCORE KEEPOUTS, and which of
        # those the held copper has actually been filled against. Unequal is a
        # board whose zones still pour across a line a blade is going to run
        # down, and that copper must not reach a panel.
        # Counters and not a flag, so an answer taken for decision 3 is never
        # applied to decision 4 (the same staleness discipline as the pour).
        self._board_decisions = 0
        self._board_filled = 0
        # Why the board's copper could not be re-filled, when it could not. Said
        # rather than retried - and cleared by the NEXT decision, because a new
        # decision is a new question and not a retry of the one that failed.
        self.board_fill_error = ""
        # -- STEP 2: the panel, derived from what is held above ------------
        # The panel page's whole state. It is on this session and not on one of
        # its own because there is ONE in-memory design: tab 2 consumes tab 1's
        # board and tab 1's config, and two sessions would be two answers to
        # "what is the board now".
        self.panel = None
        self.panel_error = ""
        # Why the panel picture is not the panel these numbers describe. Once a
        # panel is on screen it is KEPT true, so this holds either
        # nothing, the never-built sentence, or the reason a recalculation could
        # not be made.
        self.panel_stale = [panelbuild.NEVER_BUILT]
        # Has this session ever produced a panel? Before the first one there is
        # nothing on screen to keep fresh, so a change waits for Build Panel;
        # after it, every change the panel is derived from recalculates it. It
        # never goes back to False - a recalculation that FAILS must be retried
        # by the next edit, not turned into a dead page.
        self.panel_live = False
        # What the last Save Panel Files put in the panel folder, or None.
        self.panel_saved = None
        # WHERE THE LAST BOARD COMPUTATION'S TIME WENT, in milliseconds per
        # phase, for the same reason as the panel's below. Prep, the frame, the
        # render and the documents: an edit that got slower says which part of
        # it did.
        self._compute_phases = {}
        # WHERE THE LAST RECALCULATION'S TIME WENT, in milliseconds per phase.
        # Journaled with every `panel.recalc`, because "an edit takes about a
        # second" is not a number anybody can act on and this is.
        # Instrumentation only: nothing reads it to decide anything.
        self._panel_phases = {}
        # The last comparison's report, and what it said.
        self.check_report = None
        self.check_summary = ""
        # The last problem report this session built, so the download route has
        # one file to hand over and the page never names a path.
        self.bug_report_path = None
        # THE LAYER PANEL, which belongs to the session and not to a tab: what
        # is visible, what is active and how the inactive layers draw travel
        # between the two pages. Last change wins; the sequence number is how a
        # page knows a change is newer than the one it already has.
        self.layers = {}
        self.layers_seq = 0
        # -- THE SECOND PHASE ----------------------------------------------
        # How many content upgrades have landed. A page compares it with what it
        # has and repaints when it is behind - and because an upgrade does NOT
        # move the generation, this is the only thing that says a picture of the
        # same generation is now a better picture of it.
        self.upgrade_seq = 0
        # One upgrade render at a time, whoever asked for it: two kicad-cli runs
        # into one scratch folder would be two writers on one file.
        self._upgrade_lock = threading.Lock()
        self._upgrade_worker = None
        self._upgrade_token = None
        self._upgrade_generation = 0
        # HOW MANY BATCHES OF LAYER DOCUMENTS THIS SESSION HAS PRODUCED. Each
        # layer's markup carries the batch it last CHANGED in, so a page can be
        # sent what has moved since the batch it is holding and nothing else
        # (:meth:`_changed_svgs`). Never reused, never reset.
        self.paint_seq = 0
        # THE NEWEST GENERATION EACH PAGE HAS ACTUALLY BEEN HANDED. Written by
        # the two fragment routes, read by the lettering tier, which waits for
        # a subject's own picture to go out before it draws over it
        # (:meth:`_await_first_paint`).
        #
        # PER PAGE, and it has to be: the two tabs fetch about 90 ms apart, so a
        # tier released by the prep page's fetch would land on the panel before
        # the panel page had asked for anything - and the panel's first paint
        # would then be the lettered picture, 3.8 MB of it, which is exactly
        # what the tier exists to keep out of the first paint.
        self._served_generation = dict.fromkeys(UPGRADE_PAGES.values(), 0)
        # Set while no upgrade is outstanding. For a caller that has to know the
        # picture is finished - a test, a snapshot - and never waited on by a
        # request path.
        self._upgrade_idle = threading.Event()
        self._upgrade_idle.set()

    # -- recording ---------------------------------------------------------

    def log(self, kind: str, /, **data) -> None:
        """Write one journal record, when this session has a folder.

        *kind* IS POSITIONAL-ONLY. Every caller in this file
        already passes it positionally, and the marker means a `kind` key in
        `**data` lands in the data rather than in the parameter slot - so the
        name of a record can never be taken away by the record's own contents.
        """
        if self.journal is not None:
            self.journal.append(kind, **data)

    def log_untrusted(self, kind: str, data: dict, /) -> None:
        """Journal a record whose KEYS came off the wire, not out of this code.

        THE BODY IS DATA, NEVER A CALL SIGNATURE. The page posts a dictionary
        the browser built. Spread into `log(kind, **data)`, a posted key called
        `kind` would fill a parameter slot Python had already filled, and every
        one of those requests would die with `got multiple values for argument
        'kind'` - repeating in the user's terminal for as long as the tab
        was open. Both parameters here are positional-only, so nothing the page
        sends can reach a slot, and :func:`safe_log_keys` guards the one splat
        left in the chain: `Journal.append` still names `kind` in a slot of its
        own.
        """
        if self.journal is not None:
            self.journal.append(kind, **safe_log_keys(data))

    # -- one action at a time ---------------------------------------------

    @property
    def next_generation(self) -> int:
        """The generation the work now in hand will be published as.

        **THE JOURNAL'S JOIN KEY.** One interaction produces exactly one new
        generation - every action ends in
        :meth:`_acted` or :meth:`_settled` and both bump it once - so a record
        that names the generation it belongs to can be joined with every other
        record of the same interaction, on both sides of the wire. The panel
        recalculation happens BEFORE the bump, so it states the number it is
        about to be published as rather than the one it was computed under; a
        record written after the bump states `self.generation` as it always did.
        """
        return self.generation + 1

    @property
    def busy(self) -> bool:
        """Is an action in flight? What the page disables its controls on."""
        return bool(self._running)

    @property
    def running(self) -> str:
        """The endpoint of the action in flight, or `""`."""
        return self._running

    @contextlib.contextmanager
    def action(self, endpoint: str):
        """Hold the action gate for *endpoint*, or raise :class:`actions.Busy`.

        An action writes a board and then recomputes, which on a real board is
        most of a second. A second click arriving inside that window must not
        wait and then apply, or three impatient clicks become three writes and
        a page that jumps twice; so the gate is taken without blocking and a
        request that cannot have it is refused, once, cleanly.
        """
        if not self._gate.acquire(blocking=False):
            raise actions.Busy(
                f"{self._running or 'an action'} is still running - the page "
                f"blocks its controls until it finishes, so this click was not "
                f"applied"
            )
        self._running = endpoint
        try:
            yield
        finally:
            self._running = ""
            self._gate.release()

    # -- what is held, and what is not yet saved ---------------------------

    @property
    def pending_count(self) -> int:
        """How many edits have been made since the last save."""
        return len(self.pending)

    @property
    def pending_board(self) -> int:
        """How many of those edits are edits to the BOARD - what Save to Board writes."""
        return sum(1 for entry in self.pending if entry.kind != "param")

    @property
    def pending_config(self) -> int:
        """How many are edits to the CONFIG - what Save Panel Files also writes.

        The two counts are separate because the two pages write different
        things. A panel is derived output: saving the panel folder cannot save a
        score decision, and a button that counted one and wrote the other would
        be lying about what it had done.
        """
        return sum(1 for entry in self.pending if entry.kind == "param")

    def _note_pending(self, kind: str, message: str) -> None:
        self.pending.append(Pending(kind=kind, message=message))

    def _config_pending(self) -> bool:
        """Has a parameter changed? The one thing that makes a save touch the config."""
        return any(entry.kind == "param" for entry in self.pending)

    def _board_touched(self) -> None:
        """The held board has changed, so what was derived from it is worthless.

        **THE ONE RULE ABOUT THE TWO KEPT THINGS** (see
        `panelbuild.SourceSnapshot` and `panelbuild.SteppedCache`). Every place
        in this session that can change the board object calls this: reading the
        files, the prep computation - which owns the toolpath layer and rewrites
        it - the two score decisions, and the two keep-out syncs, the width
        change and the load-time repair. Called rather than reasoned about,
        because a cache that can serve a stale panel is worse than the second it
        saves.

        The stepped panel goes with the snapshot, and BOTH are dropped here even
        though either alone would be enough: the cache is only ever served
        against the snapshot object it was built from, so a dropped snapshot
        already invalidates it. Two fences against a stale panel cost nothing
        and survive a later change to either one.

        Not on the list, deliberately: the content render's Value-hiding write.
        It puts every flag back in a `finally` and holds the session lock while
        it does not, so the board it hands back is the board it was given
        (`render_seam.values_hidden`).
        """
        self._snapshot = None
        self._panel_cache = None

    def _board_snapshot(self):
        """The current snapshot, taken now if there is not one already."""
        lock_on_save = self.data.get("lock_board_on_save", True)
        if (self._snapshot is None
                or getattr(self, "_snapshot_lock_on_save", None) != lock_on_save):
            self._snapshot = panelbuild.snapshot_of(
                actions.board_for_save(self._held_board(), self.data),
            )
            self._snapshot_lock_on_save = lock_on_save
        return self._snapshot

    def _mark_panel_stale(self, reason: str) -> None:
        """Say why the panel picture is out of date, without rebuilding it.

        The banner covers two cases only, because every other change
        recalculates the panel at once: the session that has never built a
        panel, and the recalculation that could not be made. Reasons do not
        repeat: three edits in a row are one sentence, and "no panel has been
        built yet" says everything a list of edits made before the first build
        could add to it.
        """
        if panelbuild.NEVER_BUILT in self.panel_stale:
            return
        if reason not in self.panel_stale:
            self.panel_stale.append(reason)

    def _rebuild_panel(self, reason: str) -> None:
        """Derive the panel again from what is held now. Writes nothing.

        A responsive change recalculates the panel ON ENTRY, behind the busy
        popup. The in-memory
        build measures about 1.1 s on the corpus, which is a wait and not a
        gate; only a slow process keeps a button of its own, and the gerber
        comparison is the only one of those there is.

        A recalculation that fails leaves the reason in the banner and the
        message in the notes - that is the case the banner still exists for -
        and the next edit tries again. **`ChannelError` is one of those**: an
        arrangement whose outline will not close - a zero rail, for one - is a
        panel this build cannot make. Left to raise out of the server, it would
        leave the previous picture on screen looking like an edit that had
        been ignored.
        """
        # STOP THE ROUND TRIP FIRST, so the page is not blocked while a zone
        # refill is in flight. The derivation
        # below takes the BOARD lock, and a fill in flight holds that lock for
        # its whole round trip - so an edit that only cancelled afterwards would
        # sit behind ten seconds of KiCad before it could even start. Cancelling
        # here ends that process within a poll interval and the lock comes free.
        # The board path cancels in the same way, at the top of :meth:`refresh`.
        self._cancel_upgrades("a new edit")
        started = time.monotonic()
        self._panel_phases = {}
        try:
            # Under the board lock: the derivation serializes the held board
            # and renders the panel it makes of it.
            with self._board_lock:
                self.panel = self._compute_panel(fill_rails=False)
            self.panel_error = ""
            self.panel_stale = []
        except (ConfigError, ValueError, OffsetError, ChannelError) as exc:
            self.panel = None
            self.panel_error = str(exc)
            self.panel_stale = [reason]
        self.log(
            "panel.recalc",
            reason=reason,
            # The generation this recalculation will be PUBLISHED as, which is
            # what joins it to the rest of its interaction (see
            # :attr:`next_generation`).
            generation=self.next_generation,
            ok=self.panel is not None,
            error=self.panel_error,
            seconds=round(time.monotonic() - started, 4),
            fill_rails=False,
            # Milliseconds per phase, so a slow edit says WHICH part was slow.
            phases=dict(self._panel_phases),
            **self._panel_counts(),
        )

    def _panel_changed(self, reason: str) -> None:
        """Something the panel is derived from moved. Recalculate, or say so.

        Before the first build there is no picture to keep true, so the reason
        is noted and Build Panel is how a panel starts.
        """
        if self.panel_live and self.view is not None:
            self._rebuild_panel(reason)
        else:
            self._mark_panel_stale(reason)

    def _remeasure_panel(self) -> None:
        """Re-measure the panel that is already there. NO build, NO render.

        The clearance warning moves nothing: it is a
        measurement over the rectangles the last build produced. So a change to
        it recomputes the marking and the report lines that state
        it, and the picture on screen is still the picture of this panel - which
        is what "applies at once, no rebuild" means.
        """
        if self.panel is None:
            return
        outcome = self.panel.outcome
        names = dict(self.panel.layer_names)
        self.panel = self.panel._replace(
            clearance=panelbuild.clearance_view(self.config, outcome),
            report=tuple(naming.humanized(
                panelbuild.report_lines(
                    self.config, outcome, self.prep_result), names
            )),
        )

    @property
    def prep_result(self):
        """Step 1's result, or None before the board has been prepared.

        The panel report reads it for the ROUTING JOBS: which closed loops the
        declared cutter cut and which it could not. One accessor rather than
        three reads of a nested attribute, because a panel built before the
        board prepared has neither.
        """
        return getattr(self.view, "result", None) if self.view else None

    def load(self) -> None:
        """Read the config and the board from disk, discarding what is held.

        Called at launch, and by Reload (:meth:`reload_from_disk`), whose whole
        point is picking up an edit made in KiCad. It throws away whatever is
        held, so the page asks the unsaved-work question before it posts.
        """
        with self._lock:
            # THE BROKEN FILE IS NEVER READ AS SETTINGS, AND NEVER REWRITTEN.
            # A launch that fell back to standard
            # settings holds them here as text, and every read - launch or
            # Reload - tries the file FIRST: the moment it loads, it wins and
            # the standard settings are dropped. So a file fixed in an editor is
            # picked up by Reload, and one still broken never blanks the page.
            if self._seed_text:
                try:
                    self.config = load_config(self.config_path)
                    self.data = json.loads(
                        self.config_path.read_text(encoding="utf-8")
                    )
                    self._seed_text = ""
                    self.startup_warning = ""
                except ConfigError:
                    self.config = config_module.PanelConfig(
                        **json.loads(self._seed_text),
                        config_path=self.config_path,
                    )
                    self.data = json.loads(self._seed_text)
            else:
                self.config = load_config(self.config_path)
                self.data = json.loads(
                    self.config_path.read_text(encoding="utf-8")
                )
            self.board = Board.from_file(self.config.source_board_path)
            self._board_touched()
            # A BOARD READ OFF THE DISK OWES NO FILL. Its copper is whatever the
            # design was saved with, which is the copper KiCad itself last
            # computed - re-filling it unasked would rewrite a designer's file
            # on nothing more than opening the preview.
            self._board_decisions = self._board_filled = 0
            self.board_fill_error = ""
            self._repair_score_keepouts()

    # -- computing ---------------------------------------------------------

    def refresh(self, panel_reason: str = "") -> None:
        """Recompute everything from what is HELD and bump the generation.

        Writes nothing, and reads nothing: the board and the config are the
        objects this session is editing, so the page is a reading of them and
        never of a file that might have moved underneath. Prep mutates the held
        board in place - it owns the toolpath layer and clears it before it
        writes - so recomputing twice leaves exactly one toolpath on it.

        *panel_reason* names what changed for the PANEL, and is why the panel
        recalculation lives here rather than beside each caller: prep rewrites
        the toolpath the panel is derived from, so a panel built before this
        recompute would be a panel of the previous toolpath. One order, in one
        place - board first, panel second, one generation for both.

        Exceptions the stage is entitled to raise become the session's error
        state; anything else propagates, because a stage failing in a way it did
        not declare is not something to paint over, so the fault is not hidden.
        """
        with self._lock:
            started = time.monotonic()
            reused = self.board is not None
            # A NEW EDIT CANCELS A PENDING UPGRADE. What that content render is
            # drawing is about to stop being what anyone is looking at.
            self._cancel_upgrades("a new edit")
            # THE ALARM STATE BELONGS TO THE COMPUTATION IN PROGRESS: cleared
            # here and set again by `_compute`, so a board that now fails to
            # prepare cannot leave a layer red for a reason nothing measured
            # any more. Held under the lock throughout,
            # so no paint sees the gap.
            self.layer_alarms = {}
            loaded = None
            try:
                if self.config is None or self.board is None:
                    # THE ONE READ OF THE FILES, and it is a quarter of a second
                    # of a launch - named, so the launch row of the timing table
                    # says what a launch is made of (`preview.timing`).
                    mark = time.monotonic()
                    self.load()
                    loaded = round((time.monotonic() - mark) * 1000.0, 1)
                # THE BOARD LOCK for the computation: prep rewrites the
                # toolpath on the held board and the render reads it, so it
                # cannot run inside a content render's Value-hiding write
                # (see `__init__`). Taken inside the session lock, always.
                with self._board_lock:
                    self.view = self._compute(self.board)
                if loaded is not None:
                    self._compute_phases["load"] = loaded
                self.error = ""
                self.error_at = None
            except (ConfigError, ValueError, OffsetError, ChannelError) as exc:
                self.view = None
                self.error = str(exc)
                # THE COORDINATE THE FAILURE NAMES, kept as numbers. It is in
                # the sentence already; the failed viewport
                # gives it a line of its own and the zip records it as a place.
                self.error_at = _error_point(exc)
            except FileNotFoundError as exc:
                # kicad-cli missing is the one environment failure a running
                # preview can recover from by itself once it is installed.
                self.view = None
                self.error = f"{exc}"
                self.error_at = None
            if panel_reason:
                self._panel_changed(panel_reason)
            self.generation += 1
            generation = self.generation
            self.log(
                "compute",
                generation=generation,
                ok=self.view is not None,
                error=self.error,
                message=self.message,
                backend=render_seam.preview_backend(),
                seconds=round(time.monotonic() - started, 4),
                reused_board=reused,
                pending=self.pending_count,
                # WHICH PHASE this generation was painted in, so a journal reads
                # as the two timings it actually is: the fast paint here, and the
                # `upgrade` record that follows it.
                phase=(FAST if self.two_phase else FULL),
                # WHERE THE TIME WENT inside that: prep, the frame, the render
                # and the documents. Milliseconds each, in order.
                phases=dict(self._compute_phases),
                **self._computed_counts(),
            )
        self._publish(generation)
        self._schedule_upgrades(generation)

    def _computed_counts(self) -> dict:
        """The handful of numbers that say what the last computation produced."""
        if self.view is None:
            return {}
        result = self.view.result
        return {
            "candidates": len(result.candidates),
            "proposals": len(result.proposals),
            "scores": len(result.scores),
            "tabs": len(result.tabs),
            "toolpath_items": result.written,
            "warnings": len(result.warnings),
        }

    def _shown_layers(self, board, subject: str = "board") -> list:
        """EVERY layer *board* declares, in the board's own order.

        **BOTH TABS LIST ALL OF THEM.** A list cut down to the four layers prep
        is about would hide the tooling holes: their mask apertures are on a
        layer such a filter drops, so a page about a panel could not show the
        panel's own holes. A layer panel that shows the board's whole table is
        the same thing KiCad's Appearance panel shows, and what starts VISIBLE
        is a separate question the pages answer with the physical rule plus
        their own subject (`review.layerpanel`).

        One selection, three callers: the prep compute, the panel compute, and
        the panel page BEFORE anything is built. Two spellings of it would be
        two layer lists, and a page whose list disagrees with its fragment's is
        a page that reloads.
        """
        layers = render.layers_of(board)
        if not layers:
            raise ValueError(
                f"the {subject} declares no layers at all, so there is nothing "
                f"to draw"
            )
        return layers

    def _hole_colors(self, theme) -> dict:
        """What the two kinds of drill mark are painted, in the editor's colours.

        The review pages draw holes in the editor's look, not the plot's
        (`review.holes`), and this is the same overlay on the live ones, so it
        reads the same theme keys. The seam takes them under the library's hole
        pseudo-layer names, which is the one spelling both backends read.
        """
        palette = holes_module.editor_palette(theme)
        return {
            render_seam.PLATED_HOLES: palette[holes_module.KNOCKOUT_COLOR],
            render_seam.NON_PLATED_HOLES: palette[holes_module.MARK_COLOR],
        }

    def _holes_kept(self, layers: list, produced: dict, theme) -> tuple:
        """`(layers, theme)` with the Holes row kept only when it drew marks.

        A board with no drilled holes gets no row - the honest ordinary answer,
        and the same one the frozen review pages give. A row kept gets the
        two-colour swatch no board layer supplies.
        """
        if render_seam.HOLES_LAYER in produced:
            return layers, holes_module.with_swatch(theme)
        return (
            [
                layer for layer in layers
                if layer.canonical_name != render_seam.HOLES_LAYER
            ],
            theme,
        )

    def _draw(self, plan: RenderPlan, content: bool, board_lock=None,
              lettering: bool = True) -> dict:
        """One render of *plan*, at full content or not. The seam's only caller.

        The FAST phase (`content=False`) is the library renderer: every graphics
        layer exactly as it will be, and no pad, track or zone - the copper,
        mask and paste rows come back as empty documents and the page says so
        (:data:`AWAITING_NOTE`). The content render replaces them behind it.

        *board_lock* is passed only when there IS one - the upgrade's - so a
        caller's own renderer is never handed an argument it did not ask for.
        The seam holds it for its board write and for nothing else; without it,
        a panel derived while the second phase was writing the board would carry
        that write's hidden Value fields.

        *lettering* off is the GRAPHICS TIER, and it is passed the same way and
        for the same reason: ONLY when it is not the default, so a renderer
        somebody injected is never handed an argument it does not take.
        """
        locked = {"board_lock": board_lock} if board_lock is not None else {}
        tier = {} if lettering else {"lettering": False}
        # EVERY PAINT HONOURS THE ALARMED SET, and this is the only place it
        # can be done once: kicad-cli takes no per-layer colour, so the content
        # render of an alarmed layer comes back in the theme's colour whatever
        # the fast phase was handed, and the red would go back to yellow a
        # second after it appeared. The document is recoloured on the way out
        # instead.
        #
        # The set is read WITHOUT the session lock, as the rest of this render
        # is: a refresh that has moved it on has already cancelled this upgrade,
        # and the picture is dropped at the swap rather than shown.
        alarmed = self.layer_alarms if plan.subject == "board" else {}
        text_inputs = {}
        if self._render is render_seam.render_layers:
            from eedesigncore import ProjectFile
            from eedesignpanels.project import build_project

            source = self.config.source_project_path
            project = ProjectFile.from_file(source) if source.is_file() else None
            filename = self.config.source_board_path.name
            if plan.subject != "board":
                filename = self.config.output_board_path.name
                if project is not None:
                    project = build_project(
                        project, filename=self.config.output_project_path.name,
                    )
            text_inputs = {"project": project, "filename": filename}
        documents = self._render(
            plan.board, list(plan.layers), plan.window, plan.colors,
            theme=plan.theme_name, workdir=plan.workdir, content=content,
            **tier, **text_inputs,
            # What a kicad-cli export's own origin stands for: it crops to the
            # outline box. The captions are drawn in board coordinates, so a
            # plot carried back by anything else is out of register with them.
            origin=plan.origin,
            **locked,
        )
        if plan.subject == "board" and "User.38" in documents:
            extension = overlay.score_extensions(
                route_toolpath.board_scores(plan.board, []),
                (plan.window[0], plan.window[1]), overlay.ViewBox(*plan.window),
            )
            documents["User.38"] = documents["User.38"].replace(
                "</svg>", extension + "</svg>",
            )
        return layer_alarms.documents_repainted(documents, alarmed)

    def _next_paint(self) -> int:
        """The next paint number. One per batch of documents, never reused.

        WHAT A PAGE COMPARES AGAINST (see :meth:`_changed_svgs`). A layer's
        markup carries the paint it last CHANGED at, so a page that says which
        paint it is holding can be sent the layers that have moved since and no
        others - which on a carrier edit is a handful of layers instead of two
        megabytes.
        """
        self.paint_seq += 1
        return self.paint_seq

    def _write_svgs(self, svg_dir, layers, produced: dict, previous=None,
                    stamps=None) -> tuple:
        """Each layer's markup as a file: `({path}, {inner}, {paint stamp})`.

        Called holding `_lock` by both phases, which is what keeps a fragment
        from reading a document while it is half written.

        **AND THE MARKUP IS KEPT.** Reading all 52 documents back off the disk
        is not free, because the first read of a freshly written file is slow:
        345 ms for the set against 9 ms warm, measured, and paid inside the
        first paint of every single edit. The inner
        markup is what the fragment wants and this function is holding it, so it
        is handed back as well as written. The files stay - they are the
        session's artifact, and a snapshot reads them.

        **AND A LAYER THAT DID NOT CHANGE IS NOT REWRITTEN AND NOT RESTAMPED.**
        *previous* is the markup the view being replaced was holding, and this
        renderer is deterministic: most layers of a panel come out byte for byte
        the same after a carrier edit, so comparing is what lets a fragment send
        only what moved (:meth:`_changed_svgs`) and lets the write skip a
        document already on disk. The stamp of an unchanged layer is CARRIED
        OVER from *stamps*, because that is when it last actually changed.
        """
        svg_dir = Path(svg_dir)
        svg_dir.mkdir(parents=True, exist_ok=True)
        previous, stamps = previous or {}, stamps or {}
        painted = self._next_paint()
        svg_by_layer, inner, stamp = {}, {}, {}
        for layer in layers:
            markup = produced.get(layer.canonical_name)
            if markup is None:
                continue
            name = layer.canonical_name
            path = svg_dir / f"{name.replace('.', '_')}.svg"
            text = page_served.inner_of(markup, path)
            same = previous.get(name) == text and path.is_file()
            if not same:
                path.write_text(markup, encoding="utf-8")
            svg_by_layer[name] = path
            inner[name] = text
            stamp[name] = stamps.get(name, painted) if same else painted
        return svg_by_layer, inner, stamp

    def _changed_svgs(self, view, painted: int) -> dict:
        """The layers whose markup has changed since paint *painted*.

        THE FRAGMENT'S OWN DIFF. Every layer's markup every time is 3.4 MB on
        the corpus panel, and the page would re-parse and re-swap all of it,
        including the forty layers the edit could not have moved. So a page
        says which paint it is holding and gets what has moved since.

        *painted* of zero is a page with nothing, which is every page's first
        fetch and every test: it gets the whole stack, exactly as before.
        """
        held = getattr(view, "svg_stamp", None) or {}
        if not painted or not held:
            return self._markup(view)
        return {
            name: markup for name, markup in self._markup(view).items()
            if held.get(name, painted + 1) > painted
        }

    def _painted_at(self, view) -> int:
        """The newest paint this view's pictures carry. What a page stores."""
        held = getattr(view, "svg_stamp", None) or {}
        return max(held.values(), default=0)

    def _markup(self, view) -> dict:
        """Each visible layer's inner markup, from memory where it is held.

        The fallback is the file, for a view built before this was kept and for
        a layer the render did not produce a document for - a fragment that
        quietly lost a layer would be a picture missing a row.
        """
        held = getattr(view, "svg_inner", None) or {}
        return {
            name: (held[name] if name in held
                   else page_served.inner_svg(path))
            for name, path in view.svg_by_layer.items()
        }

    def _compute(self, held=None) -> Rendered:
        phases = Phases()
        self._compute_phases = phases.marks
        computed = prep.compute(self.config, held)
        phases.took("prep")
        # Prep OWNS the toolpath layer and has just rewritten it, so the board
        # this session holds is not the board the last snapshot was taken of.
        self._board_touched()
        result = computed.result
        board = computed.board

        layers = self._shown_layers(board)
        active = theme_module.load_theme(self._theme_name)
        names = naming.layer_names(render.layers_of(board))

        # THE ALARM. When a check outcome says this toolpath
        # must not be cut, the toolpath layer is painted in the alarm colour
        # rather than the theme's - and because the swatch, the alpha and the
        # render all read the SAME theme object, one substitution repaints all
        # three. Opaque, so the layer is drawn at full strength.
        #
        # AND THE ALARMED SET IS THE SESSION'S, NOT THIS PAINT'S. It is
        # published here, before a single document of this generation is
        # drawn, because every render of it reads the field back
        # (:meth:`_draw`) - which is what stops the background content render
        # from putting the theme's colour back over the red.
        alarmed = layer_alarms.of_prep_result(result)
        self.layer_alarms = alarmed
        # The page's `alarms` list is about the TOOLPATH and is prefixed as such
        # where it is drawn, so the feature alarms do not ride in it: they are a
        # different sentence about a different thing. They reach the reader
        # through the status lines, the red
        # marks and the run notes.
        alarms = tuple(dict.fromkeys(
            reason for layer in (route_toolpath.TOOLPATH_LAYER, "User.37")
            for reason in layer_alarms.reasons(alarmed, layer)))
        active = layer_alarms.themed(active, alarmed)

        bbox = route_toolpath.outline_bbox(board)
        clearance = overlay.label_clearance_mm(self.config.prep.cutter_diameter)
        window = view_window(bbox, clearance)
        # OPAQUE to the renderer, and the layer's alpha applied once by the page
        # on the stacked layer element: an `rgba` colour here is applied per ITEM
        # and composites twice wherever a layer overlaps itself.
        colors = {
            layer.canonical_name: theme_module.opaque(
                active.layer_color(layer.canonical_name)
            )
            for layer in layers
        }
        colors.update(self._hole_colors(active))
        # The Holes row travels with the layers: it is where in the stack the
        # drill marks go, and the seam knows it is not a layer to plot.
        layers = [*layers, holes_module.holes_layer(layers)]
        plan = RenderPlan(
            subject="board", board=board, layers=tuple(layers), window=window,
            colors=colors, theme_name=active.name,
            workdir=self.workdir / "render", svg_dir=self.workdir / "svg",
            origin=(bbox[0], bbox[1]),
        )
        phases.took("frame")
        produced = self._draw(plan, content=not self.two_phase,
                              lettering=not self.tiered)
        phases.took("render")
        layers, active = self._holes_kept(layers, produced, active)

        svg_dir = plan.svg_dir
        # The view being REPLACED is what "changed" is measured against: this
        # renderer is deterministic, so a layer the edit could not move comes
        # out byte for byte the same and is neither rewritten nor re-sent.
        standing = self.view
        svg_by_layer, inner, stamp = self._write_svgs(
            svg_dir, layers, produced,
            previous=getattr(standing, "svg_inner", None),
            stamps=getattr(standing, "svg_stamp", None),
        )
        phases.took("write_svgs")

        marked = labels_module.labelled(result.proposals, bbox)
        # The subtitle names candidates too, and it must name them the way the
        # picture and the rows do.
        numbered = {id(entry.proposal): entry.display_id for entry in marked}
        if result.proposals and svg_by_layer:
            # The overlay is drawn in the SAME frame the seam rendered into, so
            # its origin and its viewBox are the window itself - no shift, and
            # nothing to disagree about. The captions are `#1 SCORED` and they
            # sit one router diameter and a margin off the board edge, where the
            # routed channel cannot reach them.
            #
            # On THIS page they are also controls: `hits` gives every proposable
            # candidate a click target, and a rejected one none. The frozen
            # artifact passes no hits and is unchanged.
            box = overlay.ViewBox(*window)
            path = svg_dir / "vscore-candidates.svg"
            path.write_text(
                overlay.proposal_svg(
                    [entry.proposal for entry in marked],
                    (window[0], window[1]),
                    box,
                    clearance,
                    captions=labels_module.captions(marked),
                    hits=labels_module.hits(marked),
                ),
                encoding="utf-8",
            )
            layers = [*layers, prep.proposal_layer(layers)]
            svg_by_layer[prep.PROPOSAL_LAYER] = path
            # The overlay is a document like any other, so it is kept and
            # stamped like any other: unchanged captions keep the paint they
            # were drawn at and are not sent again.
            overlaid = page_served.inner_of(
                path.read_text(encoding="utf-8"), path
            )
            inner[prep.PROPOSAL_LAYER] = overlaid
            was = getattr(standing, "svg_inner", None) or {}
            stamped = getattr(standing, "svg_stamp", None) or {}
            stamp[prep.PROPOSAL_LAYER] = (
                stamped.get(prep.PROPOSAL_LAYER, self.paint_seq)
                if was.get(prep.PROPOSAL_LAYER) == overlaid
                else self.paint_seq
            )
            active = active._replace(colors={
                **active.colors,
                prep.PROPOSAL_LAYER.lower().replace(".", "_"): overlay.PROPOSED_COLOR,
            })
        # THE FEATURES THE DECLARED CUTTER CANNOT MAKE, IN RED. Its own layer,
        # drawn last so nothing paints over the red, and the session's alarm
        # state already carries the reason - which is what keeps the mark and
        # the sentence on screen through the next background repaint instead
        # of one of them erasing itself.
        # WHAT IS MARKED IS ASKED IN ONE PLACE: a feature
        # the bit cannot make and an inside corner the bit cannot reach are
        # both red, so neither this page nor the frozen review page decides it.
        uncut = cuttability.marked(result.jobs)
        if uncut and svg_by_layer:
            box = overlay.ViewBox(*window)
            path = svg_dir / "uncuttable-features.svg"
            path.write_text(
                overlay.uncuttable_svg(
                    uncut, (window[0], window[1]), box,
                    self.config.prep.cutter_diameter,
                ),
                encoding="utf-8",
            )
            layers = [*layers, prep.uncuttable_layer(layers)]
            svg_by_layer[prep.UNCUTTABLE_LAYER] = path
            marked_up = page_served.inner_of(
                path.read_text(encoding="utf-8"), path
            )
            inner[prep.UNCUTTABLE_LAYER] = marked_up
            was = getattr(standing, "svg_inner", None) or {}
            stamped = getattr(standing, "svg_stamp", None) or {}
            stamp[prep.UNCUTTABLE_LAYER] = (
                stamped.get(prep.UNCUTTABLE_LAYER, self.paint_seq)
                if was.get(prep.UNCUTTABLE_LAYER) == marked_up
                else self.paint_seq
            )
        phases.took("overlay")

        return Rendered(
            result=result,
            layers=layers,
            svg_by_layer=svg_by_layer,
            svg_inner=inner,
            svg_stamp=stamp,
            theme=active,
            subtitle=naming.humanize(
                prep.subtitle_for(
                    result,
                    hosted=prep.hosted_outline_notice(board),
                    name_for=lambda entry: numbered.get(id(entry), ""),
                ),
                names,
            ),
            bbox=bbox,
            window=window,
            layer_names=names,
            labelled=tuple(marked),
            notes=tuple(naming.humanized(
                [*result.warnings, *render_notes(),
                 *self.score_repair_notes()], names
            )),
            alarms=tuple(naming.humanized(alarms, names)),
            content=FAST if self.two_phase else FULL,
            lettering=GRAPHICS if self.tiered else LETTERED,
            plan=plan,
        )

    # -- THE SECOND PHASE: the content arrives behind the fast picture -----
    #
    # AN EDIT PAINTS AT ONCE. The content render is a kicad-cli process and a
    # write of the whole board - 0.84 s for the corpus board and 2.1 s for its
    # panel, measured - which would turn a one-second edit into a three-second
    # one. So a recomputation renders through
    # the library, publishes, and the page paints it and stays live; the content
    # render runs on a worker behind that and replaces the layers when it is
    # ready, as a SECOND push on the SAME generation.
    #
    # Three orderings, and all three are guarded:
    #
    # * an upgrade whose generation has moved is DROPPED - it is a picture of a
    #   board nobody is looking at any more;
    # * a new edit CANCELS whatever is outstanding, at the top of `refresh`;
    # * one upgrade renders at a time (`_upgrade_lock`), because two kicad-cli
    #   runs into one scratch folder would be two writers on one file. A
    #   cancelled render cannot be aborted mid-process - killing kicad-cli would
    #   be a change to `review.render`, which is not this module's - so the next
    #   one waits for it and the wait is journaled.
    #
    # The busy popup covers the fast phase only. An upgrade paints without
    # blocking anything, and the page says in the corner of the viewport that it
    # is waiting for one.

    # How many passes one worker will take before it gives up. A pass is one
    # subject and one tier: two subjects lettered, then drawn at content, then
    # the pour, then the pass that finds nothing left, is six - and a panel
    # derived while the worker is running adds two. This is a guard against a
    # spin, never a working limit.
    MAX_UPGRADE_PASSES = 12

    def _unlettered_views(self) -> list:
        """The plans of every view whose picture has no lettering on it yet.

        THE FIRST TIER OF THE FAST PHASE. These
        are drawn before the content renders, because they are tens of
        milliseconds each and the content renders are seconds - and because the
        content render draws the lettering itself, so a view that has already
        had one is NOT in this list. That last clause is the fence: without it
        a library render would be drawn over kicad-cli's own picture and the
        copper would vanish behind text.
        """
        out = []
        for view in (self.view, self.panel):
            if view is None or view.plan is None:
                continue
            if getattr(view, "letter_error", ""):
                continue
            if (getattr(view, "lettering", LETTERED) == GRAPHICS
                    and view.content == FAST):
                out.append(view.plan)
        return out

    def _fast_views(self) -> list:
        """The plans of every view still showing the fast phase's pictures.

        The BOARD first: step 1 is what a user has open while editing, and
        the panel is a second later either way.
        """
        out = []
        for view in (self.view, self.panel):
            if view is None or view.plan is None:
                continue
            if view.content == FAST and not view.content_error:
                out.append(view.plan)
        return out

    def _fill_owed(self) -> bool:
        """Is there a pour on the held panel that nothing has filled yet?

        Called holding `_lock`. The panel has to be DRAWN first - a view with no
        plan is not on anybody's screen - because the fill ends in one more
        render of that plan.
        """
        return bool(
            self.fills_live
            and self.panel is not None
            and self.panel.plan is not None
            and panelbuild.owes_fill(self.panel)
        )

    def _board_fill_owed(self) -> bool:
        """Is the held board's copper still the copper from before a decision?

        Called holding `_lock`. True from the moment an accept or a decline
        moves the board's score keepouts until KiCad's answer for THAT decision
        has been transplanted - which is exactly the window in which the panel
        would freeze copper that pours across a score line.
        """
        return bool(
            self.board_fills_live
            and self.board is not None
            and not self.board_fill_error
            and self._board_decisions != self._board_filled
        )

    def _cancel_upgrades(self, why: str) -> None:
        """Drop whatever content render is outstanding. Called holding `_lock`."""
        token = self._upgrade_token
        if token is None or token.is_set():
            return
        token.set()
        self.log("upgrade.cancelled", reason=why,
                 generation=self._upgrade_generation)

    def _schedule_upgrades(self, generation: int) -> None:
        """Make sure a worker is drawing the content for *generation*.

        Idempotent: a worker already running for this generation picks up
        whatever became fast while it was working - which is exactly what
        :meth:`start_panel` needs, since it derives a panel without moving the
        generation at all.

        The worker draws content AND fills the pour, so a session with one
        renderer and a pour still starts one: there is no second picture to
        paint, and there is still ten seconds of KiCad to keep off the click.
        """
        if not (self.two_phase or self.fills_live or self.board_fills_live):
            return
        with self._lock:
            if (not self._fast_views() and not self._fill_owed()
                    and not self._board_fill_owed()):
                return
            if (self._upgrade_worker is not None
                    and self._upgrade_generation == generation
                    and self._upgrade_token is not None
                    and not self._upgrade_token.is_set()):
                return
            self._cancel_upgrades("superseded by a newer generation")
            token = threading.Event()
            self._upgrade_token = token
            self._upgrade_generation = generation
            self._upgrade_idle.clear()
            worker = threading.Thread(
                target=self._upgrade_loop, args=(generation, token),
                name="eedesignpanels-upgrade", daemon=True,
            )
            self._upgrade_worker = worker
        worker.start()

    def _retire_worker(self, token) -> None:
        """This worker is done. Called holding `_lock`, and only for itself."""
        if self._upgrade_token is token:
            self._upgrade_worker = None
            self._upgrade_idle.set()

    def _upgrade_loop(self, generation: int, token) -> None:
        """Finish this generation's pictures, cheapest tier first.

        THE ORDER IS THE COST ORDER, AND THEN THE DEPENDENCY ORDER. The
        lettering is tens of milliseconds and goes first, so the text lands
        while the user is still looking at the first paint. THE BOARD'S OWN
        COPPER comes next, ahead of the content renders that are seconds each:
        it changes the board those renders would draw and the panel derived from
        it, so drawing first would draw everything twice. Then the content
        renders. The panel's pour is last and ends
        in one more content render of the panel.
        """
        for _ in range(self.MAX_UPGRADE_PASSES):
            with self._lock:
                stale = token.is_set() or self.generation != generation
                letters = [] if stale else self._unlettered_views()
                board_fill = (not stale and not letters
                              and self._board_fill_owed())
                plans = ([] if stale or letters or board_fill
                         else self._fast_views())
                fill = (not stale and not letters and not board_fill
                        and not plans and self._fill_owed())
                if not letters and not board_fill and not plans and not fill:
                    self._retire_worker(token)
                    if stale:
                        self.log(
                            "upgrade.dropped", generation=generation,
                            reason=("cancelled" if token.is_set()
                                    else "the generation moved on"),
                        )
                    return
                plan = (letters or plans or [None])[0]
            if letters:
                self._letter_one(plan, generation, token)
            elif board_fill:
                # The board's copper moves, so this generation's pictures and
                # the panel derived from them are both a generation behind:
                # `_fill_board` recomputes, publishes the new generation and a
                # new worker owns it, and this one stops. A fill that was
                # dropped or that failed publishes nothing, and this worker
                # carries on with the pictures it still owes.
                if self._fill_board(generation, token):
                    return
            elif plan is not None:
                self._upgrade_one(plan, generation, token)
            else:
                self._fill_one(generation, token)
        with self._lock:
            self._retire_worker(token)
        self.log("upgrade.gave_up", generation=generation,
                 passes=self.MAX_UPGRADE_PASSES)

    def _await_first_paint(self, generation: int, subject: str) -> float:
        """Hold until this generation's picture has actually gone out. Seconds.

        THE TIER IS A SWAP BEHIND THE FIRST PAINT, so it may not be the thing
        the first paint waits for. The library render holds the session lock
        while it draws - it reads the board this session is editing, and a
        kicad-cli content render switches Value fields off under that same lock
        - so starting it the instant the generation is published would put the
        page's own fragment request behind exactly the render it was supposed to
        arrive ahead of.

        A page reacts to the push in a few milliseconds. The cap
        (:data:`FIRST_PAINT_GRACE_SECONDS`) is for a generation nobody asks
        about: a session with no browser on it, a page mid-reload. Polled rather
        than signalled because it is a wait of milliseconds with a hard end,
        and an int read needs no lock to be read once.
        """
        page = UPGRADE_PAGES[subject]
        started = time.monotonic()
        until = started + FIRST_PAINT_GRACE_SECONDS
        while (self._served_generation.get(page, 0) < generation
               and time.monotonic() < until):
            time.sleep(0.005)
        return time.monotonic() - started

    def _letter_one(self, plan, generation: int, token) -> None:
        """One subject, drawn again WITH its lettering, and swapped in.

        The cheap tier: measured on the corpus panel, 233 ms lettered against
        104 ms for the graphics alone, so this is about 130 ms landing behind a
        picture the user already has - and 1.77 MB of text markup that the
        first paint no longer carries.

        THE BOARD LOCK IS HELD FOR THE RENDER, and the session lock only for
        the swap. This is the library renderer READING the board this session
        holds, so it has to be out of the window where a content render has the
        Value fields switched off - which is what `_board_lock` is for. It is
        NOT the session lock, because a fragment does not need a board and must
        not queue behind a render of it (see `__init__`).

        The two locks are taken one after the other and never nested, which is
        what keeps the order rule (`_lock` then `_board_lock`) true from a
        worker that needs both.

        A failure is SAID and NOT retried, so it is never hidden: the view
        keeps its graphics, `letter_error` takes it out of
        :meth:`_unlettered_views`, and the corner of the viewport says the
        lettering is missing.
        """
        waited = self._await_first_paint(generation, plan.subject)
        started = time.monotonic()
        # Read without a lock, exactly as the content tier does: a single
        # attribute read cannot tear, and the answer is re-checked at the swap.
        if token.is_set() or self.generation != generation:
            self.log("letter.dropped", subject=plan.subject,
                     generation=generation,
                     reason=("cancelled" if token.is_set()
                             else "the generation moved on"),
                     waited=round(waited, 4))
            return
        try:
            with self._board_lock:
                produced = self._draw(plan, content=False)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            with self._lock:
                self._letter_failed(plan, generation, exc,
                                    time.monotonic() - started)
            return
        with self._lock:
            view = self._view_of(plan)
            if (token.is_set() or self.generation != generation or view is None
                    or getattr(view, "lettering", LETTERED) != GRAPHICS
                    or view.content != FAST):
                self.log("letter.dropped", subject=plan.subject,
                         generation=generation,
                         reason=("cancelled" if token.is_set()
                                 else "the picture moved on"),
                         waited=round(waited, 4))
                return
            try:
                # The APPLY is guarded too: it writes documents into a scratch
                # folder, and a folder that has gone away underneath a session
                # is a failure to report rather than an exception with nobody to
                # catch it (a worker thread has no caller).
                drawn, changed = self._repaint(view, produced)
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._letter_failed(plan, generation, exc,
                                    time.monotonic() - started)
                return
            self._set_view(plan, view._replace(lettering=LETTERED))
            self.upgrade_seq += 1
            seq = self.upgrade_seq
        self.log("letter", subject=plan.subject, generation=generation,
                 applied=True, layers=drawn, changed=changed,
                 seconds=round(time.monotonic() - started, 4),
                 waited=round(waited, 4), upgrade_seq=seq)
        self._publish(generation, upgrade_page=UPGRADE_PAGES[plan.subject])

    def _letter_failed(self, plan, generation: int, exc, seconds: float) -> None:
        """Record a lettering render that did not happen. Called holding `_lock`."""
        said = describe_failure(exc)
        view = self._view_of(plan)
        if view is not None:
            self._set_view(plan, view._replace(letter_error=said))
            # The counter moves for a failure too: the page reads the reason
            # rather than waiting for text that is not coming.
            self.upgrade_seq += 1
        self.log("letter.failed", subject=plan.subject, generation=generation,
                 error=said, seconds=round(seconds, 4))
        self._publish(generation, upgrade_page=UPGRADE_PAGES[plan.subject])

    def _upgrade_one(self, plan, generation: int, token) -> None:
        """One subject, drawn again at full content and swapped in.

        The render happens WITHOUT the session lock - it is seconds long, and
        holding the lock through it would be the wait this whole mechanism
        exists to remove. What that costs is a small race with an edit arriving
        meanwhile: the picture it produces is then a picture of a board that has
        moved, and it is dropped on the generation check below.

        THE ONE PART THAT IS LOCKED IS THE BOARD WRITE. The kicad-cli backend
        switches every `Value` off while it writes the board out, and
        that is a change to the object this session holds - so the seam is given
        the session lock for exactly that window. A panel derived inside it
        would otherwise carry hidden Value fields into a fabricator's files.

        A failure is SAID. It reaches the page as the viewport's
        corner note, the journal as `upgrade.failed`, and the terminal as the
        traceback - rather than leaving a picture quietly short of its copper
        with a note promising content that will never arrive.
        """
        asked = time.monotonic()
        with self._upgrade_lock:
            waited = time.monotonic() - asked
            if token.is_set():
                self.log("upgrade.dropped", generation=generation,
                         subject=plan.subject, reason="cancelled",
                         waited=round(waited, 4))
                return
            started = time.monotonic()
            try:
                # The session lock, for the seam's board write alone: that write
                # switches every Value off for as long as it takes, and a panel
                # derived inside that window would carry hidden Values into the
                # file a fabricator gets.
                produced = self._draw(plan, content=True,
                                      board_lock=self._board_lock)
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._upgrade_failed(plan, generation, exc,
                                     time.monotonic() - started)
                return
            seconds = time.monotonic() - started
        with self._lock:
            replaced, changed = self._apply_upgrade(plan, produced,
                                                    generation, token)
            seq = self.upgrade_seq
        if replaced < 0:
            self.log("upgrade.dropped", subject=plan.subject,
                     generation=generation, reason="superseded while it drew",
                     seconds=round(seconds, 4), waited=round(waited, 4))
            return
        self.log(
            "upgrade",
            subject=plan.subject,
            generation=generation,
            applied=True,
            layers=replaced,
            changed=changed,
            seconds=round(seconds, 4),
            waited=round(waited, 4),
            upgrade_seq=seq,
        )
        self._publish(generation, upgrade_page=UPGRADE_PAGES[plan.subject])

    def _upgrade_failed(self, plan, generation: int, exc, seconds: float) -> None:
        """Record a content render that did not happen, on the view and in the log."""
        said = describe_failure(exc)
        with self._lock:
            view = self._view_of(plan)
            if view is not None:
                self._set_view(plan, view._replace(content_error=said))
                # The counter moves for a FAILURE too: it means "this picture's
                # content state has changed", and a page that did not read this
                # one would sit under a note promising content that is not
                # coming.
                self.upgrade_seq += 1
        self.log("upgrade.failed", subject=plan.subject, generation=generation,
                 error=said, seconds=round(seconds, 4))
        # Pushed so the corner note becomes the reason rather than the promise.
        self._publish(generation, upgrade_page=UPGRADE_PAGES[plan.subject])

    # -- THE THIRD PHASE: the pour, filled by KiCad, put back in memory ----
    #
    # Only KiCad fills copper, so the pours are added in memory, serialized to
    # a temporary file, filled by kicad-cli, and read back into memory. The
    # panel on screen and the panel a save writes both carry real KiCad copper,
    # and the 9.9 s that costs is paid here rather than inside a click.
    #
    # The discipline is the second phase's, for the same reasons:
    #
    # * the round trip runs with NO session lock - it serialises the panel and
    #   changes nothing, so an edit arriving meanwhile is not blocked by it;
    #   what that costs is an answer about a panel nobody is looking at, which
    #   is dropped;
    # * THE TRANSPLANT IS LOCKED, because it is the one moment the held panel
    #   changes and a save reading that panel mid-transplant would write half a
    #   fill;
    # * a newer generation cancels a pending fill and a stale answer is never
    #   applied;
    # * one KiCad process at a time (`_upgrade_lock`), shared with the content
    #   renders - the fill and a render both write a board into this session's
    #   scratch folder.
    #
    # LATEST WINS, AND IT IS NOT A QUEUE. When another zone-affecting change
    # lands before a fill is done, the first fill stops and the second starts.
    # Cancelling only a PENDING fill is not enough for that - a fill already
    # inside kicad-cli would hold the upgrade lock for the rest of its round
    # trip and the newest edit's fill would queue behind it. So the token
    # reaches the process: `refill.take_fills`
    # watches it and ENDS KiCad, the round trip raises `RefillCancelled`, and
    # the lock is free in milliseconds. A cancellation is NOT a failure and
    # records none (:meth:`_stopped_fill`), because the zones still owe a fill.
    #
    # A failure is SAID and not retried: the reason lands on the view, in the
    # panel's notes and in the corner of the viewport, and the panel stays valid
    # and clearly unfilled.

    def _stopped_fill(self, kind: str, generation: int, stopped,
                      seconds: float) -> None:
        """Record a round trip that was STOPPED by a newer change. Not a failure.

        A zone-affecting change that lands before a fill is done stops that
        fill and starts its own. So this writes nothing on the view, sets no
        error and clears no debt:
        the zones still owe a fill, and the generation that cancelled this one
        is already scheduling a worker to take it.

        The two numbers are journaled because they are the ones that say whether
        LATEST-WINS actually costs anything: how long the stop took, and whether
        the process had to be killed after its grace.
        """
        self.log(kind, generation=generation, reason="cancelled mid round trip",
                 stop_seconds=round(getattr(stopped, "seconds", 0.0), 4),
                 killed=bool(getattr(stopped, "killed", False)),
                 seconds=round(seconds, 4))

    def _fill_dir(self, name: str = "fill"):
        """Where the sandbox board goes: THIS SESSION's scratch folder.

        Never the panel folder. The panel folder is output a fabricator may be
        handed, and a sandbox is not output. Kept rather than
        deleted, because it is the board KiCad was actually asked about and the
        user can open it.
        """
        return self.workdir / name

    # -- THE BOARD'S OWN COPPER: the same phase, one step earlier -------------
    #
    # A V-score line added to the source board brings a rule area with it, and
    # the board's copper is re-filled against it. So the source board's zones
    # are filled, and can be reviewed, before they are frozen into polygons and
    # moved to the panel.
    #
    # The discipline is the pour's, for the pour's reasons: the round trip runs
    # with no session lock, the transplant is locked, a newer decision cancels a
    # pending answer, a stale answer is dropped, and one KiCad process runs at a
    # time. What is different is what happens AFTERWARDS. The board this session
    # is editing has changed, so everything derived from it is worthless: the
    # snapshot, the stepped panel, both pictures. So the fill ends in a plain
    # recomputation - the same one an edit ends in - and the new generation
    # carries the copper.

    def _fill_board(self, generation: int, token) -> bool:
        """Re-fill the held board's copper against its score keepouts.

        True when a new generation was published and this worker is finished.
        False when the answer was dropped or the fill failed, in which case the
        caller still owes this generation its pictures.
        """
        asked = time.monotonic()
        with self._upgrade_lock:
            waited = time.monotonic() - asked
            with self._lock:
                if (token.is_set() or self.generation != generation
                        or not self._board_fill_owed()):
                    self.log("board.fill.dropped", generation=generation,
                             reason=("cancelled" if token.is_set()
                                     else "the board moved on"),
                             waited=round(waited, 4))
                    return False
                board = self.board
                decisions = self._board_decisions
                beside = self.config.source_board_path
            started = time.monotonic()
            try:
                # NO SESSION LOCK HELD: seconds of kicad-cli over a serialised
                # copy, and nothing this session holds is modified by it. The
                # BOARD lock is held because serialising the board is a read of
                # it, and a content render has its Value fields switched off for
                # the length of its own write.
                with self._board_lock:
                    taken = self._board_fill(
                        board, beside=beside,
                        work_dir=self._fill_dir("board_fill"),
                        cancel=token,
                    )
            except refill.RefillCancelled as stopped:
                # NOT A FAILURE. A newer decision set the token and KiCad was
                # ended mid round trip; the board still owes a fill and the
                # newer generation's worker takes it.
                self._stopped_fill("board.fill.dropped", generation, stopped,
                                   time.monotonic() - started)
                return False
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._board_fill_failed(generation, exc,
                                        time.monotonic() - started)
                return False
            try:
                with self._lock:
                    superseded = (
                        "cancelled" if token.is_set()
                        else "the generation moved on"
                        if self.generation != generation
                        else "the board was replaced"
                        if self.board is not board
                        else "a newer decision was taken"
                        if self._board_decisions != decisions else ""
                    )
                    if superseded:
                        self.log("board.fill.dropped", generation=generation,
                                 reason=superseded,
                                 seconds=round(time.monotonic() - started, 4))
                        return False
                    with self._board_lock:
                        fill = board_copper.apply(board, taken)
                    self._board_filled = decisions
                    # The board has changed, so what was derived from it is
                    # worthless - the one rule about the two kept things.
                    self._board_touched()
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._board_fill_failed(generation, exc,
                                        time.monotonic() - started)
                return False
            said = board_copper.note_for(fill)
            self.log(
                "board.fill",
                generation=generation,
                applied=True,
                zones=fill.zones,
                records=fill.records,
                footprint_zones=fill.footprint_zones,
                fill_seconds=round(fill.seconds, 4),
                seconds=round(time.monotonic() - started, 4),
                waited=round(waited, 4),
            )
        # OUTSIDE THE UPGRADE LOCK, because this recomputation schedules the
        # next worker and that worker needs it.
        if said:
            self.message = self._say(said)
        self.refresh(panel_reason=BOARD_FILLED)
        with self._lock:
            # AND THE HANDOVER IS AFTER THE RECOMPUTATION, not before it. The
            # recomputation is what publishes the copper; retiring first would
            # let a caller waiting for the phase - a save, a test, a snapshot -
            # go on reading a picture that had not been redrawn yet. When the
            # recomputation started a worker of its own, this call finds a
            # different token and leaves it alone.
            self._retire_worker(token)
        return True

    def _board_fill_failed(self, generation: int, exc, seconds: float) -> None:
        """Record a board fill that did not happen, and stop asking for it.

        The picture keeps the copper the design was saved with and the corner
        of the viewport says so, which is the honest reading: this is not a
        board waiting for copper, it is a board whose copper does not yet answer
        to the keepout that was just written.
        """
        said = describe_failure(exc)
        with self._lock:
            self.board_fill_error = said
            self.upgrade_seq += 1
        self.log("board.fill.failed", generation=generation, error=said,
                 seconds=round(seconds, 4))
        self._publish(generation, upgrade_page=UPGRADE_PAGES["board"])

    def _fill_one(self, generation: int, token) -> None:
        """Fill the held panel's pour, transplant it, and draw it again."""
        asked = time.monotonic()
        with self._upgrade_lock:
            waited = time.monotonic() - asked
            with self._lock:
                if token.is_set() or self.generation != generation \
                        or not self._fill_owed():
                    self.log("panel.fill.dropped", generation=generation,
                             reason=("cancelled" if token.is_set()
                                     else "the panel moved on"),
                             waited=round(waited, 4))
                    return
                view = self.panel
                outcome = view.outcome
            started = time.monotonic()
            try:
                # NO SESSION LOCK HELD. Ten seconds of kicad-cli over a
                # serialised copy; nothing the session holds is modified by it.
                # The BOARD lock is held, because serialising the panel is a
                # read of it and a content render of that same panel has its
                # Value fields switched off for the length of its own write.
                with self._board_lock:
                    taken = self._fill(outcome, work_dir=self._fill_dir(),
                                       cancel=token)
            except refill.RefillCancelled as stopped:
                # NOT A FAILURE. A newer edit set the token and KiCad was ended
                # mid round trip; the panel still owes a fill and the newer
                # generation's worker takes it.
                self._stopped_fill("panel.fill.dropped", generation, stopped,
                                   time.monotonic() - started)
                return
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._fill_failed(view, generation, exc,
                                  time.monotonic() - started)
                return
            try:
                with self._lock:
                    superseded = (
                        "cancelled" if token.is_set()
                        else "the generation moved on"
                        if self.generation != generation
                        else "the panel was replaced"
                        if self.panel is not view else ""
                    )
                    if superseded:
                        self.log("panel.fill.dropped", generation=generation,
                                 reason=superseded,
                                 seconds=round(time.monotonic() - started, 4))
                        return
                    filled, result = panelbuild.apply_fill(outcome, taken)
                    self.panel = view = self._filled_view(view, filled)
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._fill_failed(view, generation, exc,
                                  time.monotonic() - started)
                return
            self.log(
                "panel.fill",
                generation=generation,
                applied=True,
                records=result.records if result is not None else 0,
                zones_filled=result.zones_filled if result is not None else 0,
                zones_empty=result.zones_empty if result is not None else 0,
                fill_seconds=round(taken.seconds, 4) if taken is not None
                else 0.0,
                seconds=round(time.monotonic() - started, 4),
                waited=round(waited, 4),
            )
            # The pour is on the board now, so the picture is one render behind
            # the panel it is a picture of. Same plan, same layers, same frame -
            # a repaint, not a reload. A render that fails here is the CONTENT
            # render failing, and says so: the fill itself already landed.
            drawing = time.monotonic()
            try:
                produced = self._draw(view.plan, content=True,
                                      board_lock=self._board_lock)
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                self._upgrade_failed(view.plan, generation, exc,
                                     time.monotonic() - drawing)
                return
            seconds = time.monotonic() - drawing
        with self._lock:
            drawn, changed = self._repaint(view, produced)
            self.upgrade_seq += 1
            seq = self.upgrade_seq
        self.log("panel.fill.drawn", generation=generation, layers=drawn,
                 changed=changed, seconds=round(seconds, 4), upgrade_seq=seq)
        self._publish(generation, upgrade_page=UPGRADE_PAGES["panel"])

    def _filled_view(self, view, outcome):
        """*view* re-stated over a filled *outcome*: the words move with it.

        The report under the picture is derived from the build, and the build
        now knows what its pours carry - so the line that said they were empty
        becomes the line that says how much copper landed and what it cost.
        """
        names = view.layer_names or {}
        return view._replace(
            outcome=outcome,
            fill_rails=True,
            fill_error="",
            report=tuple(naming.humanized(
                panelbuild.report_lines(
                    self.config, outcome, self.prep_result), names
            )),
        )

    def _repaint(self, view, produced: dict) -> tuple:
        """Overwrite this view's layer documents in place. `(drawn, changed)`.

        THE LAYER SET DOES NOT MOVE, exactly as in :meth:`_apply_upgrade`: a
        stack that gains or loses a row is a page reload, and this is a repaint.

        The KEPT markup is replaced in the same loop and under the same lock: a
        document on disk that memory disagreed with would serve the previous
        tier's picture for ever (:meth:`_markup`).

        **A LAYER WHOSE MARKUP DID NOT MOVE IS NOT REWRITTEN AND NOT RESTAMPED**,
        so a tier that redraws fifty layers and moves three of them puts three
        on the wire. Both numbers come back, because they answer different
        questions: *drawn* is what this tier was asked to draw and *changed* is
        what it actually altered - and a tier that changed nothing is a fact
        worth having in a journal rather than a silence.
        """
        drawn = changed = 0
        held = getattr(view, "svg_inner", None)
        stamps = getattr(view, "svg_stamp", None)
        painted = None
        for layer in view.layers:
            name = layer.canonical_name
            markup = produced.get(name)
            if markup is None or name not in view.svg_by_layer:
                continue
            drawn += 1
            path = Path(view.svg_by_layer[name])
            text = page_served.inner_of(markup, path)
            if held is not None and held.get(name) == text and path.is_file():
                continue
            path.write_text(markup, encoding="utf-8")
            if held is not None:
                held[name] = text
            if stamps is not None:
                painted = self._next_paint() if painted is None else painted
                stamps[name] = painted
            changed += 1
        return drawn, changed

    def _fill_failed(self, view, generation: int, exc, seconds: float) -> None:
        """Record a fill that did not happen, on the view and in the log.

        The panel keeps its empty pours and says why they are empty. It is NOT
        retried: a missing kicad-cli or a board KiCad will not open does not get
        better by being asked again every second.
        """
        said = describe_failure(exc)
        note = (
            f"the rail copper could not be filled, so the pours in this panel "
            f"are empty: {said}"
        )
        with self._lock:
            if self.panel is view:
                self.panel = view._replace(
                    fill_error=said,
                    # In the NOTES as well as the corner: the notes are what a
                    # reader scrolls, and a panel with bare rails is a panel
                    # nobody should send to a fabricator without knowing.
                    notes=tuple([*view.notes, note]),
                )
            self.upgrade_seq += 1
        self.log("panel.fill.failed", generation=generation, error=said,
                 seconds=round(seconds, 4))
        self._publish(generation, upgrade_page=UPGRADE_PAGES["panel"])

    def _view_of(self, plan):
        """The view *plan* still belongs to, or None once it has been replaced."""
        view = self.view if plan.subject == "board" else self.panel
        return view if view is not None and view.plan is plan else None

    def _set_view(self, plan, view) -> None:
        if plan.subject == "board":
            self.view = view
        else:
            self.panel = view

    def _apply_upgrade(self, plan, produced: dict, generation: int,
                       token) -> int:
        """Swap the fast pictures for the content ones. `(drawn, changed)`.

        `(-1, -1)` is "superseded": the generation moved, the edit was
        cancelled, or the view this was drawn for has been replaced. Nothing is written then, and
        nothing is pushed - the newer picture is already the answer.

        THE LAYER SET DOES NOT MOVE. A stack that gains or loses a row is a page
        RELOAD (`paintSvgs`), and an upgrade is a repaint: only the layers the
        page already has are replaced, and a document the content render
        produced for anything else is not drawn here.
        """
        view = self._view_of(plan)
        if (view is None or token.is_set() or self.generation != generation
                or view.content != FAST):
            return -1, -1
        replaced, changed = self._repaint(view, produced)
        # LETTERED TOO, and it is a fence rather than a nicety: kicad-cli draws
        # the text and the dimensions itself, so a view left saying GRAPHICS
        # here would be lettered again by the tier pass - a library render drawn
        # over kicad-cli's picture, with the copper gone out of it.
        #
        # ...and for the same reason a lettering tier that FAILED is forgiven
        # here: the text it could not draw is in this picture, so the sentence
        # about it would be a picture's corner contradicting the picture.
        self._set_view(plan, view._replace(content=FULL, lettering=LETTERED,
                                           letter_error=""))
        self.upgrade_seq += 1
        return replaced, changed

    def wait_for_upgrades(self, timeout: float = 30.0) -> bool:
        """Block until the worker is done - content renders AND the pour.

        For a caller that needs the finished picture - a test, a snapshot taken
        from a script, a save that will not write an unfilled panel. No browser
        request waits on this.
        """
        return self._upgrade_idle.wait(timeout)

    def content_state(self, view) -> dict:
        """What a page says about the phase ITS picture is in.

        `awaiting_content` is what draws the corner note, and it is deliberately
        not the same question as "is this the fast phase": a session with one
        backend renders once and waits for nothing, and a failed upgrade is
        waiting for nothing either - it has a sentence instead.

        THE POUR IS THE LAST THING THE CORNER SAYS. A panel whose content has
        landed may still be waiting for KiCad's copper, and the difference
        matters to a reader looking at empty pours: one is a picture that is not
        finished, the other is a picture that is finished and wrong about the
        copper. The content phase is reported first because it is the one that
        is about to change everything.
        """
        content = getattr(view, "content", FULL) if view is not None else FULL
        error = getattr(view, "content_error", "") if view is not None else ""
        fill_error = getattr(view, "fill_error", "") if view is not None else ""
        awaiting = bool(
            view is not None and content == FAST and not error and self.two_phase
        )
        filling = bool(
            view is not None and not awaiting and not error
            and not fill_error and self._fill_owed() and self.panel is view
        )
        # THE BOARD'S OWN COPPER, on step 1 and on
        # step 1 only: the panel's copper is the panel's own question and it has
        # the two sentences above for it.
        board_filling = bool(
            view is not None and self.view is view
            and not self.board_fill_error and self._board_fill_owed()
        )
        letter_error = (
            getattr(view, "letter_error", "") if view is not None else ""
        )
        if error:
            note = f"the full-content render did not finish: {error}"
        elif fill_error:
            note = f"the rail copper could not be filled: {fill_error}"
            error = fill_error
        elif self.view is view and self.board_fill_error:
            # A BOARD WHOSE COPPER DOES NOT ANSWER TO ITS KEEPOUT outranks a
            # wait for the rest of the picture: the picture will finish, and
            # the copper in it will still be wrong about the score.
            note = BOARD_FILL_FAILED_NOTE.format(error=self.board_fill_error)
            error = self.board_fill_error
        elif letter_error:
            # A FAILURE OUTRANKS A WAIT. The content render may well draw this
            # text a second later - it draws everything - but until it does, a
            # board whose text is missing looks like a board with no text on
            # it, and that is the reading this sentence exists to prevent.
            # It is cleared the moment the content lands.
            note = LETTERING_FAILED_NOTE.format(error=letter_error)
            error = letter_error
        elif board_filling:
            # AHEAD OF THE CONTENT WAIT on step 1: the rest of this picture is
            # arriving in a second, and the copper in it is about to be replaced
            # wholesale. Saying "copper is still being drawn" would be true and
            # would send the reader to the wrong question.
            note = BOARD_FILLING_NOTE
            awaiting = True
        elif awaiting:
            note = AWAITING_NOTE
        elif filling:
            note = FILLING_NOTE
            awaiting = True
        else:
            note = ""
        return {
            "content": content,
            "content_error": error,
            "awaiting_content": awaiting,
            "content_note": note,
            # WHICH TIER of the fast phase this picture is, for a reader of a
            # fragment and for the snapshot's state file. The corner note says
            # nothing about it: the lettering lands in tens of milliseconds and
            # a note that appeared and vanished again would be a flicker.
            "lettering": (
                getattr(view, "lettering", LETTERED) if view is not None
                else LETTERED
            ),
        }

    # -- what the server serves -------------------------------------------

    @property
    def candidates(self) -> list:
        return list(self.view.result.candidates) if self.view else []

    def state(self) -> dict:
        """The cheap answer: what generation is current, is it good, is it busy?

        **Deliberately not locked.** A recompute holds `_lock` for most of a
        second, and this is the one answer that has to arrive DURING that - it
        carries `busy`, and a busy flag nobody can read until the work finishes
        reports nothing. Every field is one attribute read, so the worst a
        caller sees is a snapshot taken a moment before the generation moved,
        which is what a poll is.
        """
        return {
            "generation": self.generation,
            "ok": self.view is not None,
            "error": self.error,
            # WHERE, as two numbers. The failed viewport puts
            # the place on its own line, so it travels beside the sentence on
            # every poll rather than being parsed out of it on the page.
            "error_at": list(self.error_at) if self.error_at else None,
            "message": self.message,
            "busy": self.busy,
            "running": self.running,
            "backend": render_seam.preview_backend(),
            "config": str(self.config_path),
            "session": str(self.journal.folder) if self.journal else "",
            "shot": self.journal.last_shot if self.journal else 0,
            # What the save button counts and the unsaved-work prompt asks
            # about. Read on every poll, so a page that missed a push still
            # knows whether closing the tab would lose anything.
            "pending": self.pending_count,
            "pending_board": self.pending_board,
            "pending_config": self.pending_config,
            "pending_changes": [entry.message for entry in self.pending],
            # The layer panel, which is the session's and not a tab's. Carried
            # on the cheap poll as well as the push, so a page that missed an
            # event still catches up.
            "layers_seq": self.layers_seq,
            "layers": dict(self.layers),
            # How many content upgrades have landed. A page that is behind this
            # number repaints - without the busy popup, because an upgrade is
            # not a wait anyone is being made to sit through.
            "upgrade_seq": self.upgrade_seq,
            "two_phase": self.two_phase,
        }

    def _served(self, page: str) -> None:
        """*page* has just been handed this generation's picture.

        What the lettering tier waits for (:meth:`_await_first_paint`): the
        second tier is a swap behind the FIRST paint, so it starts once that
        page's first paint has actually been sent. Called holding `_lock` by
        the two fragment routes, and never decreasing.
        """
        self._served_generation[page] = max(
            self._served_generation.get(page, 0), self.generation
        )

    def _prep_frame(self) -> dict:
        """The board's own layer table, for a page with no prepared view.

        A failed board load costs the viewport and nothing else, and the layer
        panel is part of that "nothing else". A board
        that loaded and then failed in prep still has a layer table, so the rows
        come straight off the file - no toolpath, no proposal and no holes row,
        because none of those exist until a computation makes them.

        The panel is a session-wide setting, so what is switched off here is
        switched off on step 2 too. A session holding no board at all states no
        layers, and the page then comes without the panel exactly as step 2 does.
        """
        if self.board is None:
            return {}
        layers = render.layers_of(self.board)
        return {
            "layers": layers,
            "theme": theme_module.load_theme(self._theme_name),
            "layer_names": {
                layer.canonical_name: layer.display_name for layer in layers
            },
        }

    def fragment(self, painted: int = 0) -> dict:
        """Everything the page needs to update itself in place.

        *painted* is the paint number the page is already holding
        (:meth:`_changed_svgs`): it gets the layers that have moved since, and
        zero - which is every first fetch and every test - gets all of them.
        """
        asked = time.monotonic()
        with self._lock:
            waited = round((time.monotonic() - asked) * 1000.0, 1)
            self._served(page_served.PREP_STEP.page)
            state = {**self.state(), **self.content_state(self.view),
                     LOCK_WAIT_KEY: waited}
            if self.view is None:
                # THE SETTINGS STAY LIVE ON A FAILED LOAD, so the numbers
                # travel even with no picture to draw: the page compares a
                # typed value against what the config holds, and an empty
                # answer here would leave it comparing against nothing and
                # offering a change the server then found stale.
                return {**state, "layers": [], "svg": {}, "proposals": [],
                        "params": page_served.params_json(self.config),
                        "subtitle": "", "warnings": [],
                        "alarms": [], "alarm_layers": [],
                        "base": "", "extent": "", "paint_seq": 0}
            view = self.view
            return {
                **state,
                "paint_seq": self._painted_at(view),
                "subtitle": view.subtitle,
                # The frame this render was drawn in, and the extents it
                # measured. Both travel because a page updates ITSELF in place:
                # a picture whose window moved has to be re-framed, and a
                # readout left at the last one's numbers would be a lie.
                "base": page_served.base_box(view),
                "extent": page_served.extent_text(view),
                "layers": [layer.canonical_name for layer in view.layers],
                "svg": self._changed_svgs(view, painted),
                "proposals": page_served.proposals_json(view),
                "route_ends": [{**j._asdict(), "layer": result.toolpath_layer}
                               for result in (view.result, *view.result.rigid_results)
                               for j in result.junctions if j.end_id],
                "params": page_served.params_json(self.config),
                "warnings": list(view.notes),
                "alarms": list(view.alarms),
                # WHICH LAYER IS RED AND WHY, on every fragment. The colour
                # travels in the documents and
                # the reason travels here, so a repaint cannot leave one without
                # the other - and an empty list is what CLEARS the row and the
                # status, which happens when the cause clears and only then.
                "alarm_layers": self._alarm_status(view.layer_names),
            }

    def page(self) -> str:
        """The whole preview page, ready to serve. A page either way, prepared or not.

        A BOARD THAT COULD NOT BE PREPARED COSTS THE VIEWPORT AND NOTHING ELSE.
        A notice page with only a reason and a try-again button would drop the
        report button, the settings and the layer panel in the one state where
        they are needed most. So the page is built the same way either way,
        with the failure drawn in the card the plot would have filled - the
        same way step 2 works.
        """
        with self._lock:
            markup = page_served.build(
                config=self.config,
                view=self.view,
                # THE FAILURE, and the place it names. Empty on a prepared
                # board; the whole of what the failed viewport states otherwise.
                error=self.error,
                error_at=self.error_at,
                # THE ALARM STATE THE PAGE IS SERVED WITH, so a page that
                # opens onto an alarmed board says
                # so in its first bytes rather than waiting for a fragment.
                alarm_layers=self._alarm_status(
                    self.view.layer_names if self.view is not None else {}
                ),
                # ...AND THE COLOUR EACH ROW GOES BACK TO. The swatch is an
                # inline style the server writes,
                # so a page told only the alarm colour could turn a row red and
                # never turn it back.
                theme_colors=self._swatch_colors(),
                **({} if self.view is not None else self._prep_frame()),
                # WHY THE SETTINGS ON SCREEN MAY NOT BE THE FILE'S. Empty on an
                # ordinary launch; a whole plain-words
                # warning when the file would not load.
                startup_warning=self.startup_warning,
                generation=self.generation,
                backend=render_seam.renderer_label(),
                content_note=self.content_state(self.view)["content_note"],
                session_dir=str(self.journal.folder) if self.journal else "",
                shot=self.journal.last_shot if self.journal else 0,
                pending=self.pending_count,
                layers_seq=self.layers_seq,
                # WHICH PAINT the stack baked into this page is, so its first
                # fragment asks for what has moved since rather than for all
                # of it.
                paint_seq=self._painted_at(self.view),
            )
        # No bare `User.##` layer name on the page, checked on the bytes that
        # go out.
        # Recorded rather than raised: a board whose own display name is a bare
        # `User.9` is a board this cannot fix, and refusing to serve a page over
        # it would be worse than showing it.
        bare = naming.violations(markup)
        if bare:
            self.log("naming.bare", names=bare)
        # The retired words, on the same bytes. Recorded rather than
        # raised, for the same reason: a page a reader can read beats a page
        # that refuses to be served over its own vocabulary.
        retired = naming.retired_words(markup)
        if retired:
            self.log("naming.retired", words=retired, page="prep")
        return markup

    # -- STEP 2: the panel ------------------------------------------------

    def _panel_unsaved(self) -> tuple:
        """`(board edits, config edits, board differs)` a panel is derived from.

        The third one is not a count and cannot be one. Prep owns the toolpath
        layer and rewrites it on every computation, so the board in memory can
        differ from the board on disk with nothing edited at all - a file whose
        toolpath an older run wrote is the ordinary case, measured on the
        corpus. The panel is derived from the NEW toolpath, and the
        page says so rather than leaving a reader to assume the file agrees.
        """
        return (self.pending_board, self.pending_config, self.panel_differs)

    @property
    def panel_differs(self) -> bool:
        """Did the last panel come from a board the design file does not carry?"""
        return bool(self.panel is not None and self.panel.board_differs)

    def _compute_panel(self, fill_rails: bool = False) -> panelbuild.PanelView:
        """Derive the panel from what is HELD, and render it. Writes nothing.

        The board is the one the prep page is editing - with its toolpath and
        whatever score decisions are on it, saved or not - and the config is the
        held one. `panelbuild.derive` is `step_and_repeat.build_from`, the same
        call the command line makes after loading the file, so the panel this
        renders is byte for byte the panel a build would write.

        **The PICTURE is derived unfilled**, which is why the
        default here is the opposite of the build's own. Filling the rail pour
        is KiCad's work and costs most of ten seconds; a change that
        recalculates on entry may cost about one. So every live rebuild passes
        `fill_rails=False` and the pour is drawn as an outline with no copper
        in it, and :meth:`save_panel` - which writes the files a fabricator gets
        - is the one caller that asks for the fill.
        """
        from eedesignpanels import fingerprints
        board = self._held_board()
        self._panel_fingerprint_evidence = None
        self._panel_fingerprint_note = ""
        try:
            self._panel_fingerprint_evidence = fingerprints.capture_pending(
                self.config.source_board_path, self.config_path, self.data)
        except (OSError, ValueError, KeyError) as exc:
            self._panel_fingerprint_note = str(exc)
        phases = Phases()
        self._panel_phases = phases.marks
        took = phases.took

        # THE SNAPSHOT IS REUSED ONLY BY THE LIVE PICTURE. A repeated
        # panel-only edit - a tooling hole diameter, a rail width - does not
        # touch the board, and re-serializing 1.77 MB of it to find that out
        # costs 682 ms of every 1166 ms recalculation, measured. The
        # SAVE takes its own, fresh: it costs under a second of a twelve-second
        # write, and what that write produces is the file a fabricator gets.
        fresh = fill_rails or self._snapshot is None
        snapshot = None if fill_rails else self._board_snapshot()
        took("snapshot" if fresh else "snapshot_reused")
        # ...AND SO IS THE STEPPED PANEL. Six copies of the board injected and
        # panelized is 367 ms of the recalculation, measured, and
        # no carrier setting can move any of it, so a tooling hole diameter or
        # a score switch reuses it and pays the carrier stages alone. What it
        # is only true for is the key: see `panelbuild.SteppedCache`. The save
        # path passes no cache and keeps none, exactly as with the snapshot.
        source = actions.board_for_save(board, self.data) if fill_rails else board
        derived = panelbuild.derive(self.config, source, fill_rails=fill_rails,
                                    snapshot=snapshot,
                                    cache=None if fill_rails
                                    else self._panel_cache)
        if self._panel_fingerprint_evidence is not None:
            self._panel_fingerprint_evidence["held_source"] = derived.text
        if not fill_rails:
            self._panel_cache = derived.cache
        # The build's own inner names (`build_full` or `carrier_only`) beside
        # the span that holds them: `build` is the whole derivation.
        phases.update(derived.phases)
        took("build")
        outcome = derived.outcome
        panel = outcome.board
        # Compare what Save to Board writes, including locks and plating settings.
        differs = derived.text != self.config.source_board_path.read_text(
            encoding="utf-8"
        )
        took("differs")

        layers = self._shown_layers(panel, subject="panel")
        active = theme_module.load_theme(self._theme_name)
        names = naming.layer_names(render.layers_of(panel))

        bbox = route_toolpath.outline_bbox(panel)
        board_origin = (bbox[0], bbox[1])
        if outcome.panelize is not None and outcome.panelize.dimensions is not None:
            from eedesignpanels.stages.panel_dimensions import preview_bounds
            annotation = preview_bounds(outcome.panelize.dimensions, outcome.panelize.blank)
            bbox = (min(bbox[0], annotation[0]), min(bbox[1], annotation[1]),
                    max(bbox[2], annotation[2]), max(bbox[3], annotation[3]))
        # No captions out here - the candidate overlay belongs to step 1 - so
        # the window is the board's box plus bare air.
        window = view_window(bbox)
        colors = {
            layer.canonical_name: theme_module.opaque(
                active.layer_color(layer.canonical_name)
            )
            for layer in layers
        }
        colors.update(self._hole_colors(active))
        # The panel's own holes - the tooling holes it is fixtured by among
        # them - drawn on top in the editor's colours, exactly as the review
        # pages draw them.
        layers = [*layers, holes_module.holes_layer(layers)]
        plan = RenderPlan(
            subject="panel", board=panel, layers=tuple(layers), window=window,
            colors=colors, theme_name=active.name,
            workdir=self.workdir / "render_panel",
            svg_dir=self.workdir / "svg_panel", origin=board_origin,
        )
        took("frame")
        produced = self._draw(plan, content=not self.two_phase,
                              lettering=not self.tiered)
        took("render")
        layers, active = self._holes_kept(layers, produced, active)

        standing = self.panel
        svg_by_layer, inner, stamp = self._write_svgs(
            plan.svg_dir, layers, produced,
            previous=getattr(standing, "svg_inner", None),
            stamps=getattr(standing, "svg_stamp", None),
        )
        took("write_svgs")

        board_edits, config_edits = self.pending_board, self.pending_config
        return panelbuild.PanelView(
            outcome=outcome,
            layers=layers,
            svg_by_layer=svg_by_layer,
            svg_inner=inner,
            svg_stamp=stamp,
            theme=active,
            subtitle=naming.humanize(
                panelbuild.subtitle_for(
                    self.config, outcome, board_edits, config_edits, differs
                ),
                names,
            ),
            bbox=bbox,
            window=window,
            layer_names=names,
            report=tuple(naming.humanized(
                panelbuild.report_lines(
                    self.config, outcome, self.prep_result), names
            )),
            notes=tuple(naming.humanized(
                [*outcome.warnings, *render_notes()], names
            )),
            unsaved_board=board_edits,
            unsaved_config=config_edits,
            board_differs=differs,
            generation=self.generation,
            fill_rails=fill_rails,
            content=FAST if self.two_phase else FULL,
            lettering=GRAPHICS if self.tiered else LETTERED,
            plan=plan,
            clearance=panelbuild.clearance_view(self.config, outcome),
        )

    def build_panel(self, payload: dict = None) -> dict:
        """THE panel action: derive the panel in memory and show it.

        Nothing is written. What comes out of this is a board object and a
        picture of it; :meth:`save_panel` is what puts a panel in the folder.

        Like every other rebuild it leaves the rail pour EMPTY, so a manual
        re-derive costs what a responsive one costs. The pour fills on the save.
        """
        with self._lock:
            started = time.monotonic()
            if self.view is None:
                raise actions.ActionError(
                    f"no panel is built while the board cannot be prepared: "
                    f"{self.error or 'the last computation failed'}"
                )
            try:
                with self._board_lock:
                    self.panel = self._compute_panel(fill_rails=False)
                self.panel_error = ""
                self.panel_stale = []
                # From here on the page keeps itself true: every later change
                # the panel is derived from recalculates it.
                self.panel_live = True
            except (ConfigError, ValueError, OffsetError, ChannelError) as exc:
                self.panel = None
                self.panel_error = str(exc)
            seconds = round(time.monotonic() - started, 4)
            self.log(
                "panel.build",
                generation=self.next_generation,
                ok=self.panel is not None,
                error=self.panel_error,
                seconds=seconds,
                fill_rails=False,
                unsaved_board=self.pending_board,
                unsaved_config=self.pending_config,
                board_differs=self.panel_differs,
                **self._panel_counts(),
            )
            if self.panel is None:
                raise actions.ActionError(self.panel_error)
            return self._settled(
                f"panel built in memory in {seconds:.1f} s - "
                f"{self.panel.subtitle}"
            )

    def _panel_counts(self) -> dict:
        """The handful of numbers that say what the last panel build produced."""
        if self.panel is None:
            return {}
        outcome = self.panel.outcome
        made = outcome.panelize
        out = {
            "instances": self.config.grid.columns * self.config.grid.rows,
            "footprints": outcome.panel_content.footprints,
            "tracks": outcome.panel_content.tracks,
            "warnings": len(outcome.warnings),
        }
        if made is not None:
            out.update(
                panel_mm=[round(made.blank.width_mm, 4),
                          round(made.blank.height_mm, 4)],
                loops=made.boundary.loop_count,
                outline_items=made.boundary.item_count,
                saw_passes=made.scores.merged_count,
            )
        return out

    def _settle_fill(self) -> float:
        """Wait for a fill that is already in flight. Seconds waited.

        **A SAVE NEVER WRITES AN UNFILLED PANEL SILENTLY.** The pour fills
        behind the picture, so a user can press Save while KiCad is halfway
        through the round trip - and the two honest answers are "wait for it"
        and "do it again yourself". Waiting is the cheaper one and it is
        the same wait either way, so the save waits, behind the busy popup the
        action gate already raises.

        Called WITHOUT `_lock`, which is not a detail: the worker takes that
        lock to transplant, so a save holding it here would wait for a fill that
        was waiting for the save.
        """
        if not self.fills_live:
            return 0.0
        with self._lock:
            owed = (
                self.panel is not None
                and panelbuild.owes_fill(self.panel)
                and not self._upgrade_idle.is_set()
            )
        if not owed:
            return 0.0
        started = time.monotonic()
        finished = self._upgrade_idle.wait(FILL_WAIT_SECONDS)
        seconds = time.monotonic() - started
        self.log("panel.fill.waited", seconds=round(seconds, 4),
                 finished=finished)
        return seconds

    def _fill_before_saving(self) -> float:
        """Fill the held panel's pour, if nothing has yet. Seconds it took.

        THE SAVE IS THE BACKSTOP, not the mechanism. The pour is
        normally filled behind the picture and what is held is already filled by
        the time anybody presses Save, in which case this costs nothing. It runs
        the round trip itself when the fill never happened - a session with the
        pour phase switched off, a fill that was cancelled by an edit and not
        yet redone - because a panel with bare rails is not the panel this
        config describes.

        MEASURED: the fill transplanted into a finished panel gives
        BYTE-IDENTICAL output to the same panel built with `fill_rails=True`, so
        this saves the same file a CI runner's build writes. That is why the
        save does not derive the panel a second time.

        Zero seconds means there was nothing to fill.
        """
        if not panelbuild.fills_on_save(self.config):
            return 0.0
        if not panelbuild.owes_fill(self.panel):
            return 0.0
        started = time.monotonic()
        try:
            with self._board_lock:
                taken = self._fill(self.panel.outcome,
                                   work_dir=self._fill_dir("fill_save"))
            filled, result = panelbuild.apply_fill(self.panel.outcome, taken)
        except (refill.RefillError, ValueError) as exc:
            # A specific failure with a specific recovery - install KiCad, or
            # take the pour out of the config - so it is answered in the
            # user's own terms and NOTHING is written.
            self.log("panel.fill.failed", error=str(exc), saving=True)
            self.panel = self.panel._replace(fill_error=str(exc))
            raise actions.ActionError(str(exc)) from exc
        self.panel = self._filled_view(self.panel, filled)
        seconds = time.monotonic() - started
        self.log("panel.fill", seconds=round(seconds, 4), saving=True,
                 records=result.records if result is not None else 0,
                 zones_filled=result.zones_filled if result is not None else 0,
                 zones_empty=result.zones_empty if result is not None else 0,
                 **self._panel_counts())
        return seconds

    def save_panel(self) -> dict:
        """Write the panel folder: the board, its project, and the config.

        Exactly what the command line writes, through the same two functions
        (`step_and_repeat.write_panel`), so a panel saved from the page and a
        panel built by a CI runner are the same bytes. The folder is created if
        it is missing and the generated files are overwritten; the config is
        written only when one of its numbers actually moved, because a config
        may be hand-written and rewriting it for nothing would reformat it.

        **A rail pour is never written empty.** The fill normally lands behind
        the picture, so a save usually writes copper that is already there; a
        save that catches a fill in flight WAITS for it (:meth:`_settle_fill`),
        and a save with no fill to wait for does the round trip itself
        (:meth:`_fill_before_saving`). All three write the same bytes, measured.

        The board's own unsaved decisions are NOT written here - those are Save
        to Board's, on step 1 - and the count on this button says so by
        counting only the config edits.
        """
        # BEFORE the lock: the worker needs it to finish the fill this waits for.
        waited = self._settle_fill()
        with self._lock:
            if self.panel is None:
                raise actions.ActionError(
                    self.panel_error
                    or "there is no panel to save yet - press Build Panel first"
                )
            if self.panel_stale:
                self.log("panel.save.stale", reasons=list(self.panel_stale))
            filled = self._fill_before_saving()
            # Same rule, for the panel: this is the file a fabricator gets.
            with self._board_lock:
                written = step_and_repeat.write_panel(self.config,
                                                      self.panel.outcome)
            self.panel_saved = written
            wrote_config = self._config_pending()
            config_edits = self.pending_config
            evidence = getattr(self, "_panel_fingerprint_evidence", None)
            if evidence is not None and self.config_path.read_bytes() != evidence["raw"][self.config_path.resolve()]:
                self._panel_fingerprint_evidence = None
                self._panel_fingerprint_note = "Settings changed on disk during generation. No fresh fingerprint was saved."
            if wrote_config:
                self.config_path.write_text(
                    actions.config_bytes(self.data), encoding="utf-8"
                )
                self.pending = [
                    entry for entry in self.pending if entry.kind != "param"
                ]
            from eedesignpanels import fingerprints
            evidence = getattr(self, "_panel_fingerprint_evidence", None)
            fingerprint_note = getattr(self, "_panel_fingerprint_note", "")
            if evidence is not None:
                try:
                    fingerprints.stamp_saved(written.output_path, evidence)
                    self.panel.outcome.board.properties = Board.from_file(written.output_path).properties
                    written = written._replace(output_sha256=hashlib.sha256(
                        written.output_path.read_bytes()).hexdigest())
                    self.panel_saved = written
                except (OSError, ValueError) as exc:
                    fingerprint_note = str(exc)
            self._panel_fingerprint_evidence = None
            self.log(
                "panel.save",
                board=str(written.output_path),
                board_sha256=written.output_sha256,
                project=(
                    str(written.project_path) if written.project_path else ""
                ),
                project_sha256=written.project_sha256,
                wrote_config=wrote_config,
                config_changes=config_edits,
                unsaved_board=self.pending_board,
                fill_seconds=round(filled, 4),
                fill_waited=round(waited, 4),
                # Whether the FILE has copper in its pours, which is the only
                # form of this question a fabricator cares about - not which of
                # the three paths put it there.
                rail_copper_filled=bool(self.panel.fill_rails),
            )
            said = ", ".join(
                name for name in (
                    written.output_path.name,
                    written.project_path.name if written.project_path else "",
                    self.config_path.name if wrote_config else "",
                ) if name
            )
            standing = (
                f" - {self.pending_board} board change(s) are still unsaved"
                if self.pending_board else ""
            )
            # What the pour cost this save, in the user's own terms: the
            # round trip paid for here, the wait for one that was already
            # running, or nothing at all because the copper was already in.
            if filled:
                poured = f" - the rail copper was filled in {filled:.1f} s"
            elif waited:
                poured = (
                    f" - waited {waited:.1f} s for KiCad to finish filling the "
                    f"rail copper"
                )
            elif self.panel.fill_rails:
                poured = " - the rail copper was already filled"
            else:
                poured = ""
            verification = f" - fingerprint unverified: {fingerprint_note}" if fingerprint_note else ""
            return self._settled(f"wrote {said}{poured}{standing}{verification}")

    def check_fingerprints(self) -> dict:
        """Check saved input and panel content without rendering or refilling."""
        from eedesignpanels.fingerprints import check_panel
        result = check_panel(
            source_board=self.config.source_board_path, settings=self.config_path,
            panel_board=self.config.output_board_path,
            report_path=self.workdir / "panel-fingerprints.json")
        changed = [item for check in result["checks"].values() for item in check["changed"]]
        message = result["summary"]
        if changed:
            message += " Changed: " + ", ".join(changed)
        return {**self._settled(message), "fingerprints": result}

    def check_reason(self) -> str:
        """Why the comparison cannot run, or `""`. What the button's tooltip says."""
        return checkrun.why_not(self.config)

    def check_panel(self, payload: dict = None) -> dict:
        """Compare the SAVED panel against its source, and keep the report.

        The one action on this page that reads files rather than memory, because
        the comparison plots gerbers and kicad-cli reads a board off the disk.
        It therefore judges what was SAVED, which is not always what is on
        screen - and it says which when they differ.
        """
        with self._lock:
            reason = self.check_reason()
            if reason:
                raise actions.ActionError(reason)
            where = self.workdir / "check"
            started = time.monotonic()
            try:
                result = self._compare(self.config, where)
            except ValueError as exc:
                self.log("panel.check.refused", error=str(exc))
                raise actions.ActionError(str(exc)) from exc
            self.check_report = Path(result.report_path)
            self.check_summary = result.summary
            self.log(
                "panel.check",
                report=str(self.check_report),
                seconds=round(time.monotonic() - started, 4),
                summary=result.summary,
                stale=list(self.panel_stale),
            )
            answer = self._settled(result.summary)
            return {**answer, "report": str(self.check_report)}

    def panel_stale_reasons(self) -> list:
        """Why the panel picture is out of date, in the reader's own words."""
        return [self._say(reason) for reason in self.panel_stale]

    def panel_state(self) -> dict:
        """What both the panel page and its fragment say about the panel."""
        return {
            "panel_ok": self.panel is not None,
            "panel_error": self.panel_error,
            "stale": self.panel_stale_reasons(),
            "unsaved": panelbuild.unsaved_sentence(*self._panel_unsaved()),
            "check_reason": self.check_reason(),
            "check_report": (
                str(self.check_report) if self.check_report else ""
            ),
            "panel_board": (
                str(self.panel_saved.output_path) if self.panel_saved else ""
            ),
            # THE POUR'S STATE, said rather than implied: whether this config
            # asks for rail copper at all, and whether the panel on screen
            # carries it. Empty pours are the ordinary case for a picture (see
            # :meth:`_fill_before_saving`), so a reader is told which it is.
            "fills_on_save": (
                panelbuild.fills_on_save(self.config)
                if self.config is not None else False
            ),
            "rail_copper_filled": bool(
                self.panel is not None and self.panel.fill_rails
            ),
            # THE CLEARANCE MARKING, in board millimetres -
            # the frame every layer in the viewport is drawn in, so the page
            # needs no transform. Empty when the setting is off, when nothing
            # is close, and before the first build.
            "clearance": (
                dict(self.panel.clearance) if self.panel is not None else {}
            ),
        }

    def panel_fragment(self, painted: int = 0) -> dict:
        """Everything the panel page needs to update itself in place.

        *painted* is the paint number the page is holding - see
        :meth:`fragment`. It matters most here: the panel's stack is 3.4 MB of
        markup and a carrier edit moves a handful of layers of it.
        """
        asked = time.monotonic()
        with self._lock:
            waited = round((time.monotonic() - asked) * 1000.0, 1)
            self._served(page_served.PANEL_STEP.page)
            state = {**self.state(), **self.panel_state(),
                     **self.content_state(self.panel),
                     LOCK_WAIT_KEY: waited}
            params = page_panel.params_json(self.config) if self.config else {}
            if self.panel is None:
                return {
                    **state, "ok": False, "paint_seq": 0,
                    "error": self.panel_error or panelbuild.NEVER_BUILT,
                    "layers": [], "svg": {}, "params": params,
                    "subtitle": "", "report": [], "breakout": {},
                    # A recalculation that FAILED is the case the banner still
                    # exists for, so the reason it failed goes where the reader
                    # is already looking rather than into the log alone.
                    "notes": [self._say(self.panel_error)]
                             if self.panel_error else [],
                    "base": "", "extent": "",
                }
            view = self.panel
            return {
                **state,
                "ok": True,
                "error": "",
                "subtitle": view.subtitle,
                # What a build hands the viewport instead of a page reload: the
                # frame this panel was rendered in, and what it measures.
                "base": page_served.base_box(view),
                "extent": page_served.extent_text(view),
                "paint_seq": self._painted_at(view),
                "layers": [layer.canonical_name for layer in view.layers],
                "svg": self._changed_svgs(view, painted),
                "params": params,
                "report": list(view.report),
                "notes": list(view.notes),
                # Does this panel come apart? The V-Scores heading's
                # status dot, refreshed with every other number the build made.
                "breakout": panelbuild.breakout_state(view.outcome),
            }

    def _alarm_status(self, layer_names) -> list:
        """The alarm state as the page states it: which layers, and why, in words.

        IN THE READER'S OWN LAYER NAMES: an
        alarm sentence names the layer a cut runs along, and a status reading
        `User.38` is the UI showing a bare canonical name. Humanized here, on
        the way out, exactly as the run's notes and the alarm sentences already
        were.

        Called holding `_lock`.
        """
        names = layer_names or {}
        return layer_alarms.status(
            {
                layer: tuple(naming.humanized(said, names))
                for layer, said in self.layer_alarms.items()
            },
            names,
        )

    def _swatch_colors(self) -> dict:
        """Every layer row's swatch colour as the THEME has it. `{canonical: colour}`.

        THE VIEW'S OWN THEME, LESS THE ALARM. It
        is what a paint needs when an alarm CLEARS, and it has to be the rest of
        the view's theme rather than a plain one: the candidate overlay and the
        Holes row carry colours no board layer supplies, and answering with a
        fresh theme would put those two rows back to a colour they never had.
        Only an ALARMED layer is answered from the plain theme, which is exactly
        the substitution being undone.

        Called holding `_lock`, from :meth:`page`.
        """
        frame = {} if self.view is not None else self._prep_frame()
        layers = list(
            self.view.layers if self.view is not None
            else frame.get("layers", ())
        )
        theme = self.view.theme if self.view is not None else frame.get("theme")
        if not layers or theme is None:
            return {}
        alarmed = set(self.layer_alarms)
        plain = theme_module.load_theme(self._theme_name) if alarmed else theme
        return {
            layer.canonical_name: (
                plain if layer.canonical_name in alarmed else theme
            ).layer_color(layer.canonical_name)
            for layer in layers
        }

    def _panel_frame(self) -> dict:
        """What the panel page draws an EMPTY viewport in, before a build.

        Rare since the panel is derived at startup (:meth:`start_panel`), and
        still the answer for a config whose panel cannot be built at all.

        The stack is the PREP view's own rows less its proposal overlay, which
        belongs to step 1: the panel inherits the source board's layer table, so
        one list serves both, Holes row included when the board has holes. So
        the page is served with the stack a build will fill, and the build is a
        repaint rather than a reload.

        A session whose board could not be prepared has no layers to state, and
        the page comes without a viewport exactly as it always did.
        """
        if self.board is None or self.view is None:
            return {}
        layers = [
            layer for layer in self.view.layers
            if layer.canonical_name != prep.PROPOSAL_LAYER
        ]
        # A FRESH theme, not the prep view's: that one may carry the alarm
        # colour substituted for the toolpath layer, which is a verdict about
        # the board's toolpath and says nothing about a panel. The Holes row
        # still needs the swatch no board layer supplies.
        theme = theme_module.load_theme(self._theme_name)
        if any(
            layer.canonical_name == render_seam.HOLES_LAYER for layer in layers
        ):
            theme = holes_module.with_swatch(theme)
        return {
            "layers": layers,
            "theme": theme,
            "layer_names": self.view.layer_names,
            "window": self.view.window,
        }

    def start_panel(self) -> None:
        """Derive the panel ONCE, at startup, before a browser is pointed here.

        **THE PANEL TAB OPENS SHOWING THE PANEL.** A step 2 served empty until
        somebody pressed Build Panel would make the user's first sight of the
        panel page a blank viewport and a sentence about a button. The
        derivation costs about a second and the
        launch is already paying for a prep computation, so it is paid here -
        prep compute, then panel derive, then the pages open.

        Rails are left UNFILLED, like every other picture on that page: filling
        them is KiCad's work and is paid on Save Panel Files
        (:meth:`_fill_before_saving`).

        A config whose panel cannot be built is a state, not a failure: the
        reason is held and the page opens stating it. Nothing is written here,
        and a session that never calls this behaves exactly as it did.
        """
        with self._lock:
            started = time.monotonic()
            if self.view is None:
                # No board, no panel. The reason is the board's, and saying
                # "press Build Panel" over it would be advice that cannot work.
                self.panel = None
                self.panel_error = (
                    self.error
                    or "the board could not be prepared, so no panel was derived"
                )
                self.panel_stale = []
                self.log("panel.start", generation=self.generation, ok=False,
                         error=self.panel_error, seconds=0.0)
                return
            try:
                with self._board_lock:
                    self.panel = self._compute_panel(fill_rails=False)
                self.panel_error = ""
                self.panel_stale = []
                # From here the page keeps itself true: every later change the
                # panel is derived from recalculates it.
                self.panel_live = True
            except (ConfigError, ValueError, OffsetError, ChannelError) as exc:
                self.panel = None
                self.panel_error = str(exc)
                # Not "never built": it WAS attempted, and the page says why it
                # did not come out rather than pointing at a button.
                self.panel_stale = []
            self.log(
                "panel.start",
                # The launch's own generation: `start_panel` derives without
                # moving it, so this belongs to the compute that came before.
                generation=self.generation,
                ok=self.panel is not None,
                error=self.panel_error,
                seconds=round(time.monotonic() - started, 4),
                fill_rails=False,
                phase=(FAST if self.two_phase else FULL),
                **self._panel_counts(),
            )
        # The generation does not move here, so the running board upgrade - if
        # there is one - picks this panel up on its next pass; if it has already
        # finished, this starts one.
        self._schedule_upgrades(self.generation)

    def panel_page(self) -> str:
        """The whole panel page, ready to serve. A page either way, built or not."""
        with self._lock:
            state = self.panel_state()
            markup = page_panel.build(
                config=self.config,
                view=self.panel,
                **({} if self.panel is not None else self._panel_frame()),
                generation=self.generation,
                backend=render_seam.renderer_label(),
                content_note=self.content_state(self.panel)["content_note"],
                session_dir=str(self.journal.folder) if self.journal else "",
                shot=self.journal.last_shot if self.journal else 0,
                pending=self.pending_config,
                layers_seq=self.layers_seq,
                stale=state["stale"],
                unsaved=state["unsaved"],
                startup_warning=self.startup_warning,
                error=self.panel_error,
                check_reason=state["check_reason"],
                check_report=state["check_report"],
            )
        bare = naming.violations(markup)
        if bare:
            self.log("naming.bare", names=bare, page="panel")
        retired = naming.retired_words(markup)
        if retired:
            self.log("naming.retired", words=retired, page="panel")
        return markup

    # -- LINKED SETTINGS: one value, every page ----------------------------
    #
    # See the module docstring. The layer panel is the first of these and the
    # shape every later one copies: a validated setter HERE, a route that
    # funnels into it, and one push to every connected page.

    def set_layer_visibility(self, visible=None, known=None, active=None,
                             mode=None, page: str = "server",
                             record: bool = True) -> dict:
        """THE setter: what the layer panel shows, from anywhere. Pushes to all.

        The first-class backend for the shared layer panel. `/layers` is one
        caller and a page is one hand on it; this
        is the API, so a value changed SERVER-SIDE - by a later stage, by a
        launcher restoring a session, by a test - reaches every open page
        through the same push and needs no browser to originate it.

        Every argument is OPTIONAL and an omitted one keeps what the session
        holds, so `set_layer_visibility(mode=2)` is a legal, complete change.

        LAST CHANGE WINS. Two pages are two views of one panel, not two panels,
        and the one that spoke most recently is the one whose hand is on it. The
        sequence number is how a page knows a change is newer than the one it
        already applied.

        Changing what is VISIBLE is not changing the board: this bumps the layer
        sequence and never the generation, so nothing recomputes and no picture
        is redrawn from the server.

        *record* writes a journal line. A page journals its own change, and one
        change gets one record, so the route turns this off; a
        direct server-side change has no page to speak for it and is recorded
        here.

        Raises:
            actions.ActionError: on anything that is not a layer panel talking.
        """
        held = self.layers or {}
        visible = held.get("visible", []) if visible is None else visible
        if known is None:
            known = held.get("known") or list(visible)
        active = held.get("active", "") if active is None else active
        mode = held.get("mode", 0) if mode is None else mode

        if not isinstance(visible, list) or not isinstance(known, list):
            raise actions.ActionError(
                "'visible' and 'known' must be lists of canonical layer names"
            )
        if len(known) > MAX_LAYER_NAMES or len(visible) > MAX_LAYER_NAMES:
            raise actions.ActionError(
                f"that is more than {MAX_LAYER_NAMES} layer names, which is "
                f"more layers than any board has"
            )
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 2:
            raise actions.ActionError(
                "'mode' is how the inactive layers draw: 0, 1 or 2"
            )
        state = {
            "page": str(page)[:MAX_EVENT_KIND_CHARS],
            "mode": mode,
            "active": str(active)[:MAX_EVENT_KIND_CHARS],
            "known": [str(name)[:MAX_EVENT_KIND_CHARS] for name in known],
            "visible": [str(name)[:MAX_EVENT_KIND_CHARS] for name in visible],
        }
        with self._lock:
            self.layers = state
            self.layers_seq += 1
            seq = self.layers_seq
            generation = self.generation
        if record:
            self.log("layers.set", layers_seq=seq, **state)
        self._publish(generation)
        return {"ok": True, "layers_seq": seq, "generation": generation,
                "message": f"{len(state['visible'])} layer(s) visible"}

    def set_layers(self, payload: dict) -> dict:
        """The ROUTE's way in: one page's layer panel state, taken as posted.

        A page states its whole panel every time, so nothing is merged here -
        `known` falls back to `visible` for a caller that names only what it can
        see. The change itself is :meth:`set_layer_visibility`'s, and it is not
        journaled twice: the page that moved it has already written its line.
        """
        if not isinstance(payload, dict):
            raise actions.ActionError("the request body must be a JSON object")
        visible = payload.get("visible", [])
        return self.set_layer_visibility(
            visible=visible,
            known=payload.get("known", visible),
            active=payload.get("active", ""),
            mode=payload.get("mode", 0),
            page=payload.get("page", ""),
            record=False,
        )

    # -- the actions a click takes ----------------------------------------

    def _say(self, message: str) -> str:
        """One sentence in the reader's layer names (see `preview.naming`)."""
        names = self.view.layer_names if self.view is not None else {}
        return naming.humanize(message, names)

    def _acted(self, message: str, panel_reason: str = "") -> dict:
        """Announce a change, recompute the board, and keep the panel true.

        *panel_reason* is passed straight through to :meth:`refresh`: what the
        panel is out of date FOR, when it is out of date at all.
        """
        self.message = self._say(message)
        self.refresh(panel_reason)
        # Said again after the recompute, because the layer names come off the
        # board the recompute read and a first action has none yet.
        self.message = self._say(message)
        return {"ok": True, "message": self.message,
                "generation": self.generation, "pending": self.pending_count}

    def _settled(self, message: str) -> dict:
        """Announce a change that did not alter the picture. No recompute.

        A save writes what is already on screen, so recomputing it would cost a
        second render to arrive at the same image. The generation still moves,
        because the page has a pending count to redraw.
        """
        with self._lock:
            self.message = self._say(message)
            self.generation += 1
            generation = self.generation
            self.log("settled", generation=generation, message=self.message,
                     pending=self.pending_count)
        self._publish(generation)
        # A build and a save both derive a picture without going through
        # `refresh`, so this is where those get their content render.
        self._schedule_upgrades(generation)
        return {"ok": True, "message": self.message, "generation": generation,
                "pending": self.pending_count}

    def _held_board(self):
        if self.board is None:
            raise actions.ActionError(
                "this session holds no board yet - the first computation has "
                "not run, so there is nothing to edit"
            )
        return self.board

    def _decided(self, kind: str, written) -> None:
        """One score decision has landed in the held board. Called holding `_lock`.

        The bookkeeping both decisions share: the derived things are dropped,
        the edit is counted as pending, and THE BOARD'S COPPER IS NOW OUT OF
        DATE - a keepout has appeared or gone and the zones were filled against
        the other answer. The refill itself happens off the responsive path
        (:meth:`_fill_board`); this is only the record that it is owed.
        """
        self._board_touched()
        self._note_pending(kind, written.message)
        self._board_decisions += 1
        # A NEW DECISION IS A NEW QUESTION, so a fill that failed for the last
        # one is not held against this one.
        self.board_fill_error = ""

    def _score_half_width_mm(self) -> float:
        """How wide a score keepout this config asks for. One number, two stages."""
        return board_scores.half_width_of(self.config)

    # The parameter name the prep page posts for that width.
    SCORE_WIDTH_PARAM = "score_rule_half_width_mm"

    def _resync_score_keepouts(self, moved: list = None) -> str:
        """The keep-out width moved, so the board's keep-outs move with it.

        The width is on the prep page, which makes it a BOARD setting: the
        strips already on the board were drawn at the old number, and a config
        that says one width while the board says another would disagree with
        itself. So the sync runs here, in memory, exactly as a decision runs
        it - and the copper is owed a re-fill, because a keep-out has just
        changed shape.

        *moved* is the list of parameters a typed edit changed, and it is only
        an optimisation: the sync itself is idempotent and costs one pass over
        the zones. **None means ASK ANYWAY**, which is what an import needs - it
        replaces the whole config, including the width, and named parameters are
        not what it moved.

        Returns the clause the status line adds, or an empty string when
        nothing moved. Called holding `_lock`.
        """
        if self.board is None:
            return ""
        if moved is not None and not any(
            name.startswith(f"{self.SCORE_WIDTH_PARAM}=") for name in moved
        ):
            return ""
        keepouts = actions.resync_keepouts(
            self._held_board(), self._score_half_width_mm()
        )
        if not keepouts.changed:
            return ""
        self._board_touched()
        self._board_decisions += 1
        self.board_fill_error = ""
        self._note_pending("param", actions.keepout_resync_said(keepouts))
        return f" - {actions.keepout_resync_said(keepouts)}"

    def _repair_score_keepouts(self) -> None:
        """Put the freshly-read board's score keep-outs right. Called from :meth:`load`.

        A board can arrive with a `User.38` score line and no keep-out behind
        it, and the other two callers of the sync never open a file. This is
        the third, and it is the same idempotent sync - so an intact board is
        not touched at all and costs one pass over its zones.

        NOTHING IS WRITTEN. The repair is a pending change like any other and
        `Save to Board` is what commits it, which is also why the copper is
        counted as owing a re-fill: a keep-out has just appeared or gone and the
        zones were filled against the other answer.

        Called holding `_lock`, from inside :meth:`load`.
        """
        if self.board is None:
            return
        keepouts = actions.repair_keepouts(
            self.board, self._score_half_width_mm()
        )
        self.score_repair = keepouts if keepouts.changed else None
        if not keepouts.changed:
            return
        self._board_touched()
        self._board_decisions += 1
        self._note_pending("repair", actions.keepout_repair_said(keepouts))

    def score_repair_notes(self) -> list:
        """The one line the PREP PAGE owes its reader about the load-time repair.

        Said only when something was not intact. The note rides with the
        pending change it describes - it is set by :meth:`load` and cleared by
        a save - because a board that has been written is a board that agrees
        with itself again.

        The keep-outs are NOT numbered to carry the pairing: the derived
        identity already pairs each strip with its line exactly, and the name
        is what a design rule error shows, so it stays constant.
        """
        repair = getattr(self, "score_repair", None)
        return [actions.keepout_repair_said(repair)] if repair else []

    def generate_edge_plating(self) -> dict:
        """Generate plating once without changing the synchronization setting."""
        with self._lock:
            # Cancel before taking the board lock held by a copper refill.
            self._cancel_upgrades("generate edge plating")
            with self._board_lock:
                edge_plating.generate(self._held_board(), self.config)
                self._board_touched()
            self._note_pending("edge_plating", "generated edge plating")
            self._show_edge_plating()
            return self._acted(
                "edge plating generated", panel_reason=panelbuild.BOARD_CHANGED,
            )

    def remove_edge_plating(self) -> dict:
        """Remove plating once and stop automatic regeneration."""
        with self._lock:
            if self.config.edge_plating:
                return self.set_param({"edge_plating": False})
            self._clear_edge_plating_once()
            return self._acted(
                "edge plating removed", panel_reason=panelbuild.BOARD_CHANGED,
            )

    def _clear_edge_plating_once(self) -> None:
        """Remove held plating without asserting a persistent empty layer."""
        # Callers hold the session lock. Stop the old refill before editing.
        self._cancel_upgrades("remove edge plating")
        with self._board_lock:
            edge_plating.clear(self._held_board())
            self._board_touched()
        self._note_pending("edge_plating", "removed edge plating")

    def _show_edge_plating(self):
        """Show the layer once when plating is enabled."""
        held = self.layers or {}
        visible = list(held.get("visible", []))
        known = list(held.get("known", visible))
        if edge_plating.LAYER not in visible:
            visible.append(edge_plating.LAYER)
        if edge_plating.LAYER not in known:
            known.append(edge_plating.LAYER)
        for enabled, layer in ((self.config.edge_plating_front_mask, "F.Mask"),
                               (self.config.edge_plating_back_mask, "B.Mask")):
            if enabled and layer not in visible:
                visible.append(layer)
            if enabled and layer not in known:
                known.append(layer)
        self.set_layer_visibility(visible=visible, known=known, page="edge_plating")

    def accept(self, payload: dict) -> dict:
        with self._lock:
            blade = actions.blade_from(payload)
            written = actions.accept(
                self._held_board(), blade, self.candidates,
                half_width_mm=self._score_half_width_mm(),
            )
            if written.changed:
                self._decided("accept", written)
            # A score decision changes the panel: the scored edge is not routed,
            # so the channels and the saw passes both move.
            return self._acted(
                written.message,
                panel_reason=(
                    panelbuild.BOARD_CHANGED if written.changed else ""
                ),
            )

    def decline(self, payload: dict) -> dict:
        with self._lock:
            blade = actions.blade_from(payload)
            written = actions.decline(
                self._held_board(), blade,
                half_width_mm=self._score_half_width_mm(),
            )
            if written.changed:
                self._decided("decline", written)
            return self._acted(
                written.message,
                panel_reason=(
                    panelbuild.BOARD_CHANGED if written.changed else ""
                ),
            )

    def set_param(self, payload: dict) -> dict:
        """Put declared numbers into the held config. Both pages post here.

        A number that only the PANEL uses - the grid, the gaps, the rails - does
        not change the prep computation, so it does not pay for one: the picture
        on step 1 is the same picture and only the panel is derived again. A
        number prep uses recomputes the board first and the panel after it,
        because the panel is derived from the toolpath that number produces.

        Either way the panel is RECALCULATED rather than marked out of date
        for as long as there is a panel on screen to keep true.
        """
        with self._lock:
            if not isinstance(payload, dict) or not payload:
                raise actions.ActionError(
                    "set_param takes an object of parameter names and numbers"
                )
            named, moved, prep_moved = [], [], False
            view_only = True
            for name, value in payload.items():
                changed = actions.set_param(
                    self.data, self.config_path, name, value
                )
                self.data, self.config = changed.data, changed.config
                named.append(f"{name}={value}")
                if changed.changed:
                    moved.append(f"{name}={value}")
                    prep_moved = prep_moved or name in actions.PREP_PARAMS
                    view_only = (
                        view_only and name in actions.VIEW_ONLY_PARAMS
                    )
            reason = ""
            if moved:
                self._note_pending("param", f"config: {', '.join(moved)}")
                reason = (
                    panelbuild.BOARD_CHANGED if prep_moved
                    else panelbuild.CONFIG_CHANGED
                )
            said = f"config updated: {', '.join(named)}"
            said += self._resync_score_keepouts(moved)
            if "edge_plating=True" in moved:
                self._show_edge_plating()
            if "edge_plating=False" in moved:
                self._clear_edge_plating_once()
            if moved and view_only:
                # NOTHING MOVED. The panel on screen is
                # still the panel these numbers describe; only what is measured
                # over it changed, so the marking is recomputed and the picture
                # is left exactly as it is.
                self._remeasure_panel()
                return self._settled(said)
            if moved and not prep_moved:
                # Nothing the PREP picture is drawn from moved, so there is
                # nothing to recompute there and no second render to pay for -
                # but the panel is derived from this number, so it is.
                self._panel_changed(reason)
                return self._settled(said)
            return self._acted(said, panel_reason=reason)

    def import_settings(self, payload: dict) -> dict:
        """Pre-populate every setting from another project's `panel.json`.

        An import fills in every setting and leaves each one editable. So it
        is :meth:`set_param` with the whole file
        as its payload and it takes the identical path: the held data is
        replaced only once the merged config has validated, the change is one
        pending edit, the panel recalculates, and nothing is written until the
        page's own save. Every field stays typeable afterwards, and a second
        import overwrites the first.

        **The held config OBJECT is replaced**, which is what invalidates the
        stepped-panel cache: the cache is keyed on a fingerprint of the config
        (`panelbuild.layout_fingerprint`), so an import that moved the grid
        rebuilds the six copies and one that moved only a carrier setting reuses
        them - the same arithmetic a typed field goes through, for the same
        reason.

        Which numbers moved decides what is recomputed. Prep is drawn from the
        `prep` section and `min_tab_mm` (`actions.PREP_SECTIONS`): when either
        moved, the board is recomputed and the panel after it, because the panel
        is derived from the toolpath prep writes. Otherwise the prep picture is
        the same picture and only the panel is derived again.

        The board is never touched by this. An import is a config edit, so what
        it changes is what "Save Panel Files" writes - and what "Save to Board"
        writes alongside the board, exactly as a typed number is.
        """
        with self._lock:
            if self.data is None or self.config is None:
                raise actions.ActionError(
                    "this session holds no config yet - the first computation "
                    "has not run, so there is nothing to import into"
                )
            imported = actions.import_settings(
                self.data, self.config_path, payload
            )
            if not imported.changed:
                self.log("import", name=imported.name, keys=[], changed=False,
                         prep_moved=False, settings=imported.settings)
                return self._settled(
                    f"{imported.name} says exactly what this panel.json "
                    f"already says - nothing changed"
                )
            plating_was_synchronized = self.config.edge_plating
            self.data, self.config = imported.data, imported.config
            if plating_was_synchronized and not self.config.edge_plating:
                self._clear_edge_plating_once()
            elif not plating_was_synchronized and self.config.edge_plating:
                self._show_edge_plating()
            named = ", ".join(imported.keys)
            self._note_pending("param", f"imported from {imported.name}: {named}")
            reason = (
                panelbuild.BOARD_CHANGED if imported.prep_moved
                else panelbuild.CONFIG_CHANGED
            )
            self.log(
                "import",
                name=imported.name,
                keys=list(imported.keys),
                changed=True,
                prep_moved=imported.prep_moved,
                # The file itself, so a session can be replayed from its
                # journal: a config is about a kilobyte and the import is not
                # reconstructable from the key names alone.
                settings=imported.settings,
            )
            said = (
                f"imported from {imported.name}: {named} - held in memory, "
                f"nothing written yet"
            )
            # AN IMPORT CAN CARRY THE KEEP-OUT WIDTH. It replaces the whole
            # config, so the strips on the
            # board may now be drawn at a width the config no longer states -
            # and a keep-out that moved is copper that has to be filled again.
            # Asked unconditionally, because an import does not name the numbers
            # it moved the way a typed edit does. When it DID move them the
            # board itself has changed, so the prep picture is redrawn too.
            resynced = self._resync_score_keepouts()
            said += resynced
            if resynced:
                reason = panelbuild.BOARD_CHANGED
            if imported.prep_moved or resynced:
                return self._acted(said, panel_reason=reason)
            self._panel_changed(reason)
            return self._settled(said)

    def import_placement(self, payload: dict) -> dict:
        """Put a posted placement CSV into the held config.

        The picker gives the page a File and no path, so the bytes are posted -
        the same shape :meth:`import_settings` has, for the same reason. What is
        kept is the ROWS: the config alone reproduces the panel, so a CSV is an
        input form and never a stored reference.

        While the file is active the pattern controls are not used, and the
        build says which of them it is overriding rather than ignoring them
        silently. Nothing is written; the panel recalculates.
        """
        with self._lock:
            if self.data is None or self.config is None:
                raise actions.ActionError(
                    "this session holds no config yet - the first computation "
                    "has not run, so there is nothing to place into"
                )
            placed = actions.import_placement(
                self.data, self.config_path, payload
            )
            self.data, self.config = placed.data, placed.config
            self._note_pending(
                "param",
                f"placed by {placed.name}: {len(placed.rows)} placements",
            )
            self.log(
                "placement",
                name=placed.name,
                placements=len(placed.rows),
                skipped=placed.skipped,
                header=placed.header,
                # The rows themselves, so a session can be replayed from its
                # journal: a placement is three numbers and the arrangement is
                # not reconstructable from a count.
                rows=[list(row) for row in placed.rows],
            )
            self._panel_changed(panelbuild.CONFIG_CHANGED)
            return self._settled(
                f"placed by {placed.name}: {len(placed.rows)} placements held "
                f"in memory, nothing written yet"
            )

    def _settle_board_fill(self) -> float:
        """Wait for a board fill that is already in flight. Seconds waited.

        **A SAVE NEVER WRITES STALE COPPER SILENTLY**, the same as the panel
        save. The board's zones are re-filled behind the picture, so a user can
        press Save to Board while KiCad is halfway through it - and writing the
        design with copper that still pours across the score, seconds before
        the right answer arrives, is the one outcome nobody wants.

        Called WITHOUT `_lock`: the worker takes that lock to transplant, so a
        save holding it here would wait for a fill that was waiting for the save.
        Polled rather than signalled, because the worker goes on to other work
        after the transplant and this waits for the transplant alone.
        """
        if not self.board_fills_live:
            return 0.0
        started = time.monotonic()
        deadline = started + FILL_WAIT_SECONDS
        waited = False
        while time.monotonic() < deadline:
            with self._lock:
                if not self._board_fill_owed():
                    break
                # Nobody is working on it - a fill cancelled by an edit and not
                # yet redone. Waiting would be waiting for nothing; the save
                # does the round trip itself below.
                if self._upgrade_idle.is_set():
                    break
            waited = True
            time.sleep(0.02)
        seconds = time.monotonic() - started
        if waited:
            self.log("board.fill.waited", seconds=round(seconds, 4))
        return seconds if waited else 0.0

    def _board_fill_before_saving(self) -> float:
        """Re-fill the board's copper if nothing has. Seconds it took.

        THE BACKSTOP, not the mechanism: the fill normally lands behind the
        picture and this costs nothing. It runs the round trip itself when the
        fill never happened - a fill cancelled by a later edit, a session whose
        trailing phase was switched off - because a design saved with copper
        pouring across its own score must never be written.

        Called holding `_lock`.
        """
        if not self._board_fill_owed():
            return 0.0
        started = time.monotonic()
        decisions = self._board_decisions
        try:
            with self._board_lock:
                taken = self._board_fill(
                    self.board, beside=self.config.source_board_path,
                    work_dir=self._fill_dir("board_fill_save"),
                )
                fill = board_copper.apply(self.board, taken)
        except (refill.RefillError, ValueError) as exc:
            # A specific failure with a specific recovery - install KiCad, fix
            # the board - so it is answered in the user's own terms and
            # NOTHING is written.
            self.board_fill_error = str(exc)
            self.log("board.fill.failed", error=str(exc), saving=True)
            raise actions.ActionError(str(exc)) from exc
        self._board_filled = decisions
        self._board_touched()
        seconds = time.monotonic() - started
        self.log("board.fill", saving=True, zones=fill.zones,
                 records=fill.records, footprint_zones=fill.footprint_zones,
                 fill_seconds=round(fill.seconds, 4),
                 seconds=round(seconds, 4))
        return seconds

    def save(self) -> dict:
        """THE write: the board, and the config when a number moved. One action.

        Refused while the computation is in an error state, because the board
        would then carry whatever prep managed before it gave up. A stage that
        cannot complete says so and writes nothing.

        **The score keepouts and the copper filled against them go with it.** A
        decision writes both (`actions.accept`), and a save that caught the
        re-fill in flight waits for it (:meth:`_settle_board_fill`) rather than
        writing the copper from before the decision.
        """
        # BEFORE the lock: the worker needs it to finish the fill this waits for.
        waited = self._settle_board_fill()
        with self._lock:
            if self.view is None:
                raise actions.ActionError(
                    f"nothing is saved while the board cannot be prepared: "
                    f"{self.error or 'the last computation failed'}"
                )
            filled = self._board_fill_before_saving()
            # THE BOARD LOCK: this serializes the held board to the design
            # file, and a content render with its Value fields switched off
            # would otherwise be writing that state into the file a fabricator
            # gets.
            with self._board_lock:
                saved = actions.save(
                    self._held_board(),
                    self.config.source_board_path,
                    self.data,
                    self.config_path,
                    changes=self.pending_count,
                    write_config=self._config_pending(),
                )
            self.log(
                "save",
                board=str(saved.board_path),
                config=str(saved.config_path) if saved.wrote_config else "",
                wrote_config=saved.wrote_config,
                changes=saved.changes,
                changed=[entry.message for entry in self.pending],
                copper_filled=round(filled, 4),
                copper_waited=round(waited, 4),
            )
            self.pending = []
            # THE REPAIR NOTE GOES WITH THE PENDING CHANGE IT DESCRIBES: the
            # file on disk now carries the keep-outs, so a page still saying the
            # board arrived without them would be describing a board nobody has.
            self.score_repair = None
            # What the copper cost this save, in the user's own terms.
            if filled:
                poured = f" - the board's copper was re-filled in {filled:.1f} s"
            elif waited:
                poured = (
                    f" - waited {waited:.1f} s for KiCad to finish re-filling "
                    f"the board's copper"
                )
            else:
                poured = ""
            said = f"{saved.message}{poured}"
            if filled:
                # The copper moved inside this save, so the picture on screen is
                # a picture of the board before it. Recompute rather than settle.
                return self._acted(said)
            return self._settled(said)

    def reload_from_disk(self) -> dict:
        """RELOAD: re-read the board and the config, and recompute from them.

        The Reload button on step 1 always re-reads the source board from
        disk - that is the whole control -
        and the unsaved-work guard is what stands in front of it: the page asks
        the three-way question before posting here, exactly as it does before
        quitting, because a reload with edits outstanding throws them away.

        Held edits are DROPPED here rather than merged. A board read off the
        disk is the file KiCad has, and a pending decision was a decision about
        the file this one replaces; keeping the count would leave a save button
        offering to write changes nothing on screen still shows.
        """
        with self._lock:
            dropped = self.pending_count
            # CLEARED BEFORE THE READ, not after: the read repairs
            # the board's score keep-outs and records that repair as a pending
            # change, and a clear placed after it would throw away the record of
            # an edit the held board is still carrying.
            self.pending = []
            # A RE-READ THAT FAILS IS A STATE, NOT A CRASH.
            # `try again` in the failed viewport posts here, so this is the one
            # control a person with an unreadable board reaches for - and an
            # exception out of the route would answer the page with no JSON at
            # all, which it reads as the server having gone away. The held
            # objects are dropped and the read is left to `refresh`, which owns
            # the error state every other failure lands in. The board alone is
            # dropped - `load` re-reads the config either way - so a re-read
            # that fails
            # leaves the settings that were on screen rather than none at all.
            self.board = None
            self._board_touched()
            said = "re-read the board and the config from disk"
            if dropped:
                said += f" - {dropped} unsaved change(s) discarded"
            # The board this panel was derived from has just been re-read, so
            # whatever KiCad did to it in the meantime belongs in the panel too.
            return self._acted(said, panel_reason=panelbuild.BOARD_CHANGED)

    # -- instrumentation ---------------------------------------------------

    def journal_events(self, payload: dict) -> dict:
        """Record a batch of client interaction events. Changes nothing else.

        The page batches and throttles, so a pan is one settle event rather
        than four hundred pointer moves. This endpoint therefore does NOT bump
        the generation and does not recompute: watching the user must not be
        able to change what the user is looking at.
        """
        if not isinstance(payload, dict):
            raise actions.ActionError("the request body must be a JSON object")
        events = payload.get("events")
        if not isinstance(events, list):
            raise actions.ActionError(
                "'events' must be a list of interaction records"
            )
        if len(events) > MAX_CLIENT_EVENTS:
            raise actions.ActionError(
                f"{len(events)} events in one batch is more than this endpoint "
                f"takes ({MAX_CLIENT_EVENTS}) - the page batches, it does not "
                f"buffer a session"
            )
        logged = 0
        for event in events:
            if not isinstance(event, dict):
                raise actions.ActionError("every event must be a JSON object")
            kind = str(event.get("type") or "event")[:MAX_EVENT_KIND_CHARS]
            data = {key: value for key, value in event.items() if key != "type"}
            # THE POSTED BODY IS CARRIED, NOT SPREAD. See :meth:`log_untrusted`:
            # the page names a key `kind` in its own payload, and
            # `self.log(..., **data)` would hand that key the parameter slot
            # the record's name already holds.
            self.log_untrusted(f"client.{kind}", data)
            logged += 1
        return {"ok": True, "logged": logged, "generation": self.generation,
                "message": f"{logged} interaction event(s) recorded"}

    def note(self, payload: dict) -> dict:
        """Keep one line of the user's own words, tied to the latest shot.

        Minimal typing is the design goal: one input, one Enter, no dialog.
        The note attaches to the newest shot number, so a user can say
        "003 - the left tab is wrong" out loud and the journal already agrees.
        """
        if not isinstance(payload, dict):
            raise actions.ActionError("the request body must be a JSON object")
        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise actions.ActionError("a note needs some words in it")
        text = text.strip()[:MAX_NOTE_CHARS]
        if self.journal is None:
            raise actions.ActionError(
                "this session has no folder, so there is nowhere to keep a note"
            )
        shot = self.journal.last_shot
        self.log("note", text=text, shot=shot, generation=self.generation)
        return {
            "ok": True,
            "shot": shot,
            "generation": self.generation,
            "message": (
                f"note kept against shot {shot:03d}" if shot
                else "note kept - no snapshot taken yet"
            ),
        }

    def backend_state(self) -> dict:
        """Everything the BACKEND knows, for a snapshot's state file.

        The config as currently loaded, the latest computation's results, and
        the identity of the run. A reviewer opening `NNN_state.json` should
        need nothing else to know what produced the picture beside it.
        """
        with self._lock:
            return {
                "session": {
                    "folder": str(self.journal.folder) if self.journal else "",
                    "config_path": str(self.config_path),
                    "board_path": (
                        str(self.config.source_board_path) if self.config else ""
                    ),
                    "stage": "prep",
                    "backend": render_seam.preview_backend(),
                    "generation": self.generation,
                    "eedesignpanels_version": __version__,
                },
                "config": (
                    snapshot_module.jsonable(self.config)
                    if self.config is not None else None
                ),
                "results": self._results_state(),
                # STEP 2, in the same file: what the panel page holds. A
                # reviewer opening one state file should not have to guess
                # whether a panel existed when the shot was taken.
                "panel": self._panel_state_file(),
            }

    def _panel_state_file(self) -> dict:
        """The panel half of a snapshot's state file: numbers, not pictures."""
        base = {
            **self.panel_state(),
            "params": (
                page_panel.params_json(self.config) if self.config else {}
            ),
            "layers": dict(self.layers),
            "layers_seq": self.layers_seq,
        }
        if self.panel is None:
            return {**base, "subtitle": "", "report": [], "notes": [],
                    "counts": {}}
        return {
            **base,
            "subtitle": self.panel.subtitle,
            "bbox_mm": list(self.panel.bbox),
            "report": list(self.panel.report),
            "notes": list(self.panel.notes),
            "unsaved_board": self.panel.unsaved_board,
            "unsaved_config": self.panel.unsaved_config,
            "board_differs_from_file": self.panel.board_differs,
            "built_at_generation": self.panel.generation,
            "counts": self._panel_counts(),
        }

    def _results_state(self) -> dict:
        """The last computation, as data. `prep` is the whole `PrepResult`."""
        base = {
            "ok": self.view is not None,
            "error": self.error,
            # WHERE IT FAILED, in the zip as numbers: the one report a stuck
            # engineer sends is the report of
            # a board that would not prepare, and "where" is the first thing a
            # reader of it asks.
            "error_at": list(self.error_at) if self.error_at else None,
            "message": self.message,
            "generation": self.generation,
            "pending": self.pending_count,
            "pending_changes": [
                {"kind": entry.kind, "message": entry.message}
                for entry in self.pending
            ],
        }
        if self.view is None:
            return {**base, "subtitle": "", "layers": [], "params": {},
                    "proposals": [], "warnings": [], "checks": {}, "prep": None}
        view = self.view
        return {
            **base,
            "subtitle": view.subtitle,
            "bbox_mm": list(view.bbox),
            "layers": [
                {
                    "canonical": layer.canonical_name,
                    "display": layer.display_name,
                    "layer_type": layer.layer_type,
                    "ordinal": layer.ordinal,
                }
                for layer in view.layers
            ],
            "params": page_served.params_json(self.config),
            # A STATE FILE may keep the hash ids beside the display number; the
            # page may not.
            "proposals": page_served.proposals_json(view, include_ids=True),
            "warnings": list(view.result.warnings),
            "checks": check_outcomes(view.result),
            # The full result: tabs, stops, bridges, junctions, candidates,
            # scores and the timings, each with its field names.
            "prep": snapshot_module.jsonable(view.result),
        }

    def snapshot(self, payload: dict) -> dict:
        """Save anything worth reviewing under the next shot number.

        The page posts one serialized SVG per viewport and its own view state;
        this adds the backend state and writes all three artifact kinds.
        """
        if self.journal is None:
            raise actions.ActionError(
                "this session has no folder, so there is nowhere to save a "
                "snapshot - start the preview through "
                "`python -m eedesignpanels.preview <config>`"
            )
        with self._lock:
            state = self.backend_state()
        shot = self.journal.next_shot()
        try:
            saved = snapshot_module.save(
                self.journal, shot, payload if isinstance(payload, dict) else {},
                state,
            )
        except snapshot_module.SnapshotError as exc:
            self.log("snapshot.refused", shot=shot, error=str(exc))
            raise actions.ActionError(str(exc)) from exc
        self.log(
            "snapshot",
            shot=shot,
            svg=saved.svg,
            png=saved.png,
            state=saved.state,
            rasterized=saved.rasterized,
            warnings=saved.warnings,
            generation=self.generation,
        )
        self.message = saved.summary
        return {
            "ok": True,
            "shot": shot,
            "generation": self.generation,
            "message": saved.summary,
            "folder": str(saved.folder),
            "files": {"svg": saved.svg, "png": saved.png, "state": saved.state},
            "warnings": saved.warnings,
        }

    # -- the problem report -------------------------------------------------

    def bug_report(self, payload: dict) -> dict:
        """Collect one zip: the board, the settings, the session and the shot.

        This takes a shot, as a snapshot does, and then puts it in an archive
        beside everything a developer needs to reproduce the run on their own
        machine - the design files, the panel configuration, the journal of
        every press and every number typed, the version facts, and the KiCad
        library tables that decided where the footprints came from.

        The shot is taken FIRST and its files ride along, so the picture in the
        zip is the picture that was on screen when the button was pressed.

        EVERY REPORT STAYS ON THIS COMPUTER. The zip and its summary are
        written into :func:`eedesignpanels.preview.bugreport.report_folder`,
        `bug_reports/` in the panel folder by default, and the reply names the
        file. `Save a copy elsewhere` also hands the same zip to the browser
        through the download route, which then asks where to put it.
        """
        if self.journal is None:
            raise actions.ActionError(
                "this session has no folder, so there is nowhere to build a "
                "report - start the preview through "
                "`python -m eedesignpanels.preview <config>`"
            )
        body = payload if isinstance(payload, dict) else {}
        note = body.get("note")
        if not isinstance(note, str):
            note = body.get("note_box") if isinstance(body.get("note_box"),
                                                      str) else ""
        with self._lock:
            state = self.backend_state()
        shot = self.journal.next_shot()
        try:
            saved = snapshot_module.save(self.journal, shot, body, state)
            shot_warnings = list(saved.warnings)
        except snapshot_module.SnapshotError as exc:
            # A REPORT IS NEVER LOST TO ITS PICTURE. One viewport that
            # serialized oddly would have refused a snapshot; here it costs the
            # picture, the state file is written anyway, and the manifest says
            # why there is no drawing in the archive.
            self.log("bug_report.no_picture", shot=shot, error=str(exc))
            saved = snapshot_module.save(self.journal, shot, {"viewports": []},
                                         state)
            shot_warnings = [f"no picture in this shot: {exc}",
                             *saved.warnings]
        try:
            # THE LIVE SESSION GOES IN THE ZIP, NOT JUST THE FILE. `self.data`
            # is the configuration dictionary a save would write, so it is the
            # honest record of what this panel was built from; the file on disk
            # is collected separately and may be older by every unsaved edit.
            # A report that held only the file would lose every value the user
            # had typed and not yet saved.
            report = bugreport.build(
                self.config, self.journal, state, note=note, shot=shot,
                check_report=self.check_report, shot_warnings=shot_warnings,
                session_settings=self.data,
                pending=[(entry.kind, entry.message) for entry in self.pending],
                stale=self.panel_stale_reasons(),
            )
        except (bugreport.ReportError, OSError) as exc:
            self.log("bug_report.refused", shot=shot, error=str(exc))
            raise actions.ActionError(str(exc)) from exc
        self.bug_report_path = report.path
        self.log(
            "bug_report",
            shot=shot,
            file=report.name,
            folder=str(report.path.parent),
            size_bytes=report.size_bytes,
            collected=len(report.manifest["collected"]),
            absent=len(report.manifest["absent"]),
            skipped=len(report.manifest["skipped"]),
            note=note,
            warnings=shot_warnings,
            summary=str(report.summary_path or ""),
            generation=self.generation,
        )
        self.message = report.summary
        return {
            "ok": True,
            "shot": shot,
            "generation": self.generation,
            "message": report.summary,
            "name": report.name,
            "file": str(report.path),
            "folder": str(report.path.parent),
            "size_bytes": report.size_bytes,
            "summary_file": str(report.summary_path or ""),
            "download": bugreport.DOWNLOAD_PATH,
            "collected": len(report.manifest["collected"]),
            "absent": len(report.manifest["absent"]),
            "skipped": len(report.manifest["skipped"]),
            "warnings": shot_warnings,
        }

    # -- change notification ----------------------------------------------

    def subscribe(self) -> "Subscriber":
        """A queue of change messages, for one SSE connection."""
        subscriber = Subscriber()
        with self._lock:
            self._subscribers.append(subscriber)
            subscriber.push({
                "generation": self.generation,
                "layers_seq": self.layers_seq,
                "layers": dict(self.layers),
                "upgrade": self.upgrade_seq,
            })
        return subscriber

    def unsubscribe(self, subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def announce_closing(self, reason: str) -> None:
        """The last thing every open page hears: this server is going, and why.

        Pushed BEFORE the teardown, so the tab that is still
        open closes itself rather than sitting on a stream that simply stopped
        answering. It carries no generation to act on: the page's own handler
        reads `closing` first and everything after it is moot.
        """
        with self._lock:
            listeners = list(self._subscribers)
            payload = {
                "closing": True,
                "reason": reason,
                "generation": self.generation,
            }
        for subscriber in listeners:
            subscriber.push(payload)

    # How long a closing session waits for the content render it cannot abort.
    # It is holding a scratch folder open, and a caller that closes a session
    # and then deletes that folder must not be racing a worker writing into it.
    CLOSE_WAIT_SECONDS = 10.0

    def close(self) -> None:
        """Wake every listener so its thread can end, and drop any upgrade.

        The upgrade is cancelled and then WAITED FOR, briefly. A kicad-cli run
        cannot be aborted from here, and the alternative to waiting is a worker
        writing into a scratch folder its owner has already deleted - which is
        an error about a missing file, minutes after anything asked for it.
        """
        with self._lock:
            self._cancel_upgrades("the session is closing")
            listeners = list(self._subscribers)
            self._subscribers.clear()
        if not self._upgrade_idle.wait(self.CLOSE_WAIT_SECONDS):
            self.log("upgrade.still_running",
                     seconds=self.CLOSE_WAIT_SECONDS)
        for subscriber in listeners:
            subscriber.stop()

    def _publish(self, generation: int, upgrade_page: str = "") -> None:
        """Tell every listening page what moved: the generation, and the layers.

        One message carries both because a page acts on them differently - a new
        generation means fetch a fragment, a new layer sequence means paint the
        stack it already has - and a second channel would be a second thing that
        could be out of step.

        THE UPGRADE RIDES HERE TOO. A content render that has
        landed is a second push on the SAME generation, and `upgrade` is what
        tells the two apart: a page behind that number repaints, without the
        busy popup, because nobody is being made to wait for it.
        *upgrade_page* is WHOSE content landed, because the counter is the
        session's and the board's content is not news to the panel page.
        """
        with self._lock:
            listeners = list(self._subscribers)
            payload = {
                "generation": generation,
                "layers_seq": self.layers_seq,
                "layers": dict(self.layers),
                "upgrade": self.upgrade_seq,
                "upgrade_page": upgrade_page,
            }
        for subscriber in listeners:
            subscriber.push(payload)


class Subscriber:
    """One listening page. A change message arrives; the connection sends it.

    Messages COALESCE: a listener that has not read yet keeps only the newest.
    That is right for a "something changed" signal - the page fetches the
    current fragment when it wakes, so an intermediate state it never saw would
    tell it nothing - and it means a burst of edits costs one round trip rather
    than one each.
    """

    def __init__(self):
        self._event = threading.Event()
        self._pending = None
        self._closed = False

    def push(self, message) -> None:
        self._pending = message
        self._event.set()

    def stop(self) -> None:
        self._closed = True
        self._event.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def wait(self, timeout: float):
        """The next message, or None when the wait timed out or we are done."""
        if not self._event.wait(timeout):
            return None
        self._event.clear()
        return None if self._closed else self._pending


# The stage's own pass/fail properties, by name. A snapshot records the
# VERDICTS as well as the geometry, so a reviewer reading a state file sees
# what the run thought of itself; `getattr` because a later stage will have a
# different set and a missing check must not cost the snapshot.
CHECK_NAMES = (
    "scores_untouched",
    "min_tab_clear",
    "widths_agree",
    "guard_clear",
    "scores_clear",
    # The junction VERDICT, not the distance quota: a corner whose
    # board edge ends at the score achieves no overshoot and needs none, so
    # `junctions_reached` reported a good board as short.
    "junctions_clear",
)
MEASURE_NAMES = (
    "cutter_diameter_mm",
    "tool_max_mm",
    "min_tab_mm",
    "cut_length_mm",
    "material_mm",
    "narrowest_tab_mm",
    "route_past_mm",
    "dropped",
    "dropped_mm",
    "straightened",
    "straightened_mm",
    "removed",
    "written",
)


def check_outcomes(result) -> dict:
    """The run's own verdicts and headline measurements, for a state file."""
    out = {
        name: bool(getattr(result, name))
        for name in CHECK_NAMES if hasattr(result, name)
    }
    out["stale_scores"] = len(getattr(result, "stale_scores", ()) or ())
    out["rout_through"] = len(getattr(result, "rout_through", ()) or ())
    out["routed_scores"] = len(getattr(result, "routed_scores", ()) or ())
    out["measurements"] = {
        name: snapshot_module.jsonable(getattr(result, name))
        for name in MEASURE_NAMES if hasattr(result, name)
    }
    return out


def describe_failure(exc: BaseException) -> str:
    """A failure a designer can act on, with the traceback kept for the terminal."""
    traceback.print_exc()
    return f"{type(exc).__name__}: {exc}"
