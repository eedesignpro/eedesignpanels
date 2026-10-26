"""Panel scores: saw passes across the whole panel, and the DRC that guards them.

A v-score on a board edge is the boards' own
decision and arrives merged by :mod:`eedesignpanels.stages.panelize`; a PANEL
score sits on no board edge at all. It is what breaks a finished panel into
column or row strips before anybody starts snapping tabs.

**NOBODY TYPES A SCORE POSITION**. The config carries four
switches, not two lists of numbers. The page calls the group `V-Scores` and
names them after the cut, horizontal before
vertical:

* `rails_horizontal` - one pass along the INNER EDGE of the top rail and one
  along the bottom, each running across the panel;
* `rails_vertical` - one pass along the inner edge of the left rail and one
  along the right, each running down the panel;
* `inter_row` - one pass across the middle of every gap between board rows;
* `inter_column` - one pass down the MIDDLE of every gap between board columns.

The rail switch is two switches, because the long passes and the short ones are
two setups on the saw and a user who wants one rarely wants both.

**THE RAIL SCORE SETBACK GROWS THE PANEL. IT DOES NOT MOVE THE LINE.** The
setback is a BUFFER ADDED TO THE BOARD: it makes the board that much bigger on
each side, so the rails keep the dimension the user typed. Across one axis the panel
reads, from the edge inwards: the panel edge, the rail's own width, the rail
score line, the setback of plain material, the board array - then the setback,
the score, the rail and the far edge. The blank GROWS by twice the setback on
that axis, and the rail dimension, panel edge to rail score, is exactly the
number typed.

WHICH SETBACK GROWS WHICH WAY IS EASY TO SWAP, so it is said plainly. `Rail
scores, horizontal` are the passes along the TOP AND BOTTOM rails, so
`rail_setback_horizontal_mm` grows the panel VERTICALLY. The vertical setback
belongs to the left and right rails and grows the panel ACROSS. The
growth happens where the blank is measured
(:func:`eedesignpanels.stages.panelize.stepped`). This module only places the
line, at the panel edge plus the rail's own width, which is the arithmetic it
used before any setback existed. Zero is the panel with no setback, to the byte.

**A DEPENDANT WITH ITS GOVERNOR OFF CONTRIBUTES NOTHING.** Each
setback and each jump distance hangs off one rail switch, and
:attr:`Settings.governed` is the single place that zeroes it - not a test
scattered through the geometry. The run's report names every one it ignored,
so a number in the file that did nothing never looks like one that did.

Every position is then COMPUTED from the panel's own layout - where the boards
actually landed, and how wide the rails actually are - so a gap that moves takes
its score with it and nothing in the file can disagree with the panel. A typed
position goes stale as soon as the layout changes, and a score that sits on the
edge of a column instead of mid-gap is the result.

**THE DATUM IS STILL THE BLANK'S TOP-LEFT CORNER**: every score is
REPORTED as a distance across or down from that corner, in millimetres, because
that is the number a fabricator can measure and check. It is a consequence of
the layout now rather than an input to it.

**Every score on the panel gets a rule area, by one mechanism.** The tool does
not reimplement DRC: the panel generates a thin rule
area per score line with keepouts on it, and KiCad's own engine then tells the
designer that a track, a via, a pad, a pour or a footprint is sitting where a
saw is going to run. So this stage does not distinguish its own scores from the
merged instance-edge ones - it reads `User.38` off the panel after both are on
it and gives every line the same treatment. One mechanism, one place it can be
wrong.

**AND THE KEEP-OUT RUNS PAST THE END OF ITS LINE.** The rule area is the line's
length PLUS the keep-out width at each end, so the guard wraps the end of the
cut the same way it guards the sides. A line a jump shortened is guarded
the same way: the keep-out follows the shortened line and then extends past
each of its ends.

**AND EVERY SCORE EXCEPT A RAIL SCORE IS SHORTENED BY ITS DIRECTION'S JUMP.**
A jump shortens the inter-row passes, the
inter-column passes and the lines the boards decided, at both ends. A RAIL
score is never shortened - it runs along the rim the jump exists to keep whole.

**AND A SHARED LINE IS WHICHEVER THE USER SAYS.** A rail score whose
setback is zero lands exactly on a pass the boards decided: one saw line, two
meanings. As a RAIL score it runs the full span and no jump shortens it; as a
BOARD-EDGE score its direction's jump has already stopped it short at both ends.
THE TOOL CANNOT TELL WHICH IS WANTED - both are legitimate cuts and the geometry
is identical - so a checkbox under each rail score answers it, defaulting to the
rail score because that is the reading that lets a panel separate. It means
nothing unless the two lines are actually collinear, which is detected here
(:func:`lines_at`, at the tolerance this project already uses for "on this
line"), and every shared line the run found is reported with the treatment it
got: a saw line must never change meaning silently.

**AND THE PROTECTION IS NOT ON THE SWITCH.** `panel.scores` says which
families of pass the panel ADDS; it does not say whether the passes the BOARDS
decided are guarded. A config whose `scores` group is switched off still gets
one rule area per `User.38` line on the panel - :func:`protect` - because those
lines are on the panel either way, the rail pour must not cross one, and a
keepout that appears only when somebody happened to ask for a panel-level score
is a keepout nobody can rely on. The half width comes from the prep section
whatever this group says, and the run's report names where it came from.

The rule areas go on EVERY COPPER LAYER: tracks, vias, pads and
pours only exist there, inner layers are the ones a designer forgets, and a
rule area drawn on `Edge.Cuts` would put a hatched region on the layer a
fabricator reads as "cut here". Rule areas are never plotted on any layer -
verified in KiCad 10.0.3, `pcbnew/plot_board_layers.cpp:850` and `:1104`, both
of which skip a zone with `GetIsRuleArea()` before looking at its layer - so
this choice changes what the engineer sees and never what is fabricated.

A score position outside the blank is an ERROR naming the position: the panel
would be asked for a cut that is not on it, and there is no sane thing to build.
A score crossing a board is a WARNING naming the position: computed positions
run down gaps and along rail edges and cross nothing, so this now guards against
a layout nobody has built yet rather than against a typo. The saw-cut proof at
panel level - what the blade does to the material either side over its whole
run - is a later stage; this is the basic guard.
"""

import uuid
from typing import NamedTuple

from eedesigncore import (
    Board,
    PcbLine,
    Stroke,
    Zone,
    ZoneKeepout,
    ZoneOutlinePolygon,
    items_on_layers,
)

from eedesignpanels import channels

SCORE_LAYER = "User.38"

# The stroke a panel score line is drawn at. It means nothing physical - the
# blade's width is the fab's, not the file's - and it matches what the merged
# instance scores are drawn at so the layer reads as one kind of thing.
DEFAULT_SCORE_WIDTH_MM = 0.1

# uuid5(NAMESPACE_URL, ...). Fixed for all time.
SCORE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/panel-score-line"
)
RULE_AREA_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/eedesignpro/eedesignpanels/score-rule-area"
)

# The name every generated rule area carries. KiCad puts a rule area's name in
# its DRC violation messages, so this is the sentence the designer reads when
# something is in the way.
RULE_AREA_NAME = "Panel v-score"

# How far either side of a score line the rule area reaches when NOBODY SAID.
# The same figure `config.PanelScoresConfig.rule_half_width_mm` defaults to, and
# it is stated here as well because :func:`protect` runs for a config that has
# no scores section to read it from - the keepout is not on the switch. Two
# spellings of one number, fenced against each other in the tests.
DEFAULT_RULE_HALF_WIDTH_MM = 0.3


# Two score lines this far apart are ONE line: the file's own six decimal
# places. A computed rail edge that lands on a merged board-edge pass is the
# ordinary case, not a coincidence - a board whose outer edge is scored IS the
# rail's inner edge - so it is recognised and not drawn twice.
SAME_LINE_TOL_MM = 1e-6

# How far off a line a keepout that came from somewhere else may sit and still
# count as guarding THAT line. Wider than :data:`SAME_LINE_TOL_MM` because an
# inherited keepout has been through a step-and-repeat offset and a file round
# trip since it was drawn; a micrometre is still far below anything a saw or a
# designer means by "on the line".
ON_LINE_TOL_MM = 1e-6

# The shortest stretch of a score line this stage will draw a rule area over.
# Subtracting what the boards already guard leaves the rails and the gaps, and
# it can also leave a sliver a micrometre long where two coverages meet - which
# is a zone in the file that guards nothing and that a designer would have to
# ask about.
MIN_RULE_AREA_MM = 0.001

# The shortest saw pass jump scoring will leave standing. A pass trimmed to
# nothing is not a jump-scored pass, it is a pass the panel lost, and losing a
# cut quietly is the failure this floor exists to prevent: the trim is declined
# and the distance and the length are both named. Stated HERE, and imported by
# :mod:`eedesignpanels.stages.panelize`, because one rule covers both cases - a
# jump shortens the passes the boards decided AND the passes between rows and
# columns - and one rule needs one floor.
MIN_JUMPED_PASS_MM = 1.0


def _width_or_default(value) -> float:
    """A stated keep-out half width, or the shipped one.

    THE WIDTH IS NOT A `panel.scores` SETTING. It is declared on the prep page,
    at `prep.vscore.rule_half_width_mm`, so a
    config section handed to this stage carries no width and None means "nobody
    said here". The width the build actually uses is handed in by
    :func:`eedesignpanels.stages.panelize.carrier` from the one config accessor;
    the shipped figure is what a caller holding no config at all gets.
    """
    return DEFAULT_RULE_HALF_WIDTH_MM if value is None else float(value)


def _distance_or_zero(value) -> float:
    """A stated jump distance or setback, or none at all.

    None is a section that does not carry the key, and none is zero: the saw
    runs edge to edge along the rail's inner edge.
    """
    return 0.0 if value is None else float(value)


class Setbacks(NamedTuple):
    """How much the rail scores GROW THE PANEL, per axis, in millimetres.

    `horizontal_mm` belongs to the top and bottom
    rail passes and grows the panel VERTICALLY; `vertical_mm` belongs to the
    left and right passes and grows it ACROSS. Each side gains its own axis's
    number, so the panel gains twice it.

    Always the GOVERNED figures: a setback whose rail score is off is zero, so a
    panel with the rail scores switched off does not grow at all.
    """

    horizontal_mm: float = 0.0
    vertical_mm: float = 0.0

    @property
    def asked(self) -> bool:
        """Does this ask the blank to grow at all?"""
        return self.horizontal_mm > 0.0 or self.vertical_mm > 0.0

    @classmethod
    def of(cls, value) -> "Setbacks":
        """Setbacks from another pair, a settings object, a mapping, or nothing.

        None is "nobody said", which is zero.
        """
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Settings):
            return value.governed.setbacks
        if isinstance(value, dict):
            return cls(horizontal_mm=float(value.get("horizontal_mm", 0.0)),
                       vertical_mm=float(value.get("vertical_mm", 0.0)))
        return cls(*(float(number) for number in value))


class Ignored(NamedTuple):
    """One dependent setting the build did not use, and why.

    The report reads this straight out: a number in
    the file that did nothing must never look like one that did.
    """

    # The label the menu shows, so the sentence names what the user sees.
    setting: str
    governor: str
    stated_mm: float

    @property
    def sentence(self) -> str:
        return (
            f"{self.setting} says {self.stated_mm:g} mm and was NOT used: "
            f"{self.governor} is off, so the setting it depends on contributes "
            f"nothing to this panel"
        )


# EVERY DEPENDANT IN THIS SETTINGS GROUP, AND THE SWITCH THAT GOVERNS IT. A
# greyed setting contributes nothing to the build. One table, read by one
# function, rather than a test scattered through the geometry, so a jump
# distance cannot shorten saw lines on a panel whose rail scores are off.
#
# `(field, governor, label, governor label)`, in the order the page lists them.
# Each setback is governed by the rail score it moves. Each jump is governed by
# the rail score the line it shortens RUNS INTO: a vertical pass ends at the top
# and bottom rails, and the score along those two is the horizontal rail score.
DEPENDANTS = (
    ("rail_setback_horizontal_mm", "rails_horizontal",
     "Rail score setback, horizontal", "Rail scores, horizontal"),
    ("jump_vertical_mm", "rails_horizontal",
     "Jump-scores, vertical", "Rail scores, horizontal"),
    ("rail_setback_vertical_mm", "rails_vertical",
     "Rail score setback, vertical", "Rail scores, vertical"),
    ("jump_horizontal_mm", "rails_vertical",
     "Jump-scores, horizontal", "Rail scores, vertical"),
)

# THE SWITCH DEPENDANTS, same table shape, same rule. Kept apart from the
# distances because "off" for a switch is
# False and not 0.0, and because the report has to read it as a tick rather
# than as a measurement. Each answers one question: WHEN THIS RAIL SCORE'S LINE
# FALLS ON A LINE THE BOARDS DECIDED, WHICH OF THE TWO IS IT? The tool cannot
# tell - one saw pass, two legitimate meanings, and nothing in the board or the
# config separates them - so the user owns the choice, per rail score.
SHARED_LINE_DEPENDANTS = (
    ("shared_line_is_rail_score_horizontal", "rails_horizontal",
     "Shared line is the rail score, horizontal", "Rail scores, horizontal"),
    ("shared_line_is_rail_score_vertical", "rails_vertical",
     "Shared line is the rail score, vertical", "Rail scores, vertical"),
)


class Settings(NamedTuple):
    """WHICH of the panel's saw passes to cut, and the four numbers that shape them.

    The positions are the layout's business (:func:`plan`). What a config
    chooses is which of the four families of pass the panel carries, which is
    what the four checkboxes on the page write.

    **THE RAIL SWITCH IS TWO SWITCHES.**
    `rails_horizontal` cuts along the inner edge of the top and the bottom rail,
    each of those passes running across the panel; `rails_vertical` cuts along
    the left and the right rail, each running down it. They are separate
    because the two cuts are two setups on the saw.

    **THE PASSES BETWEEN BOARDS ARE NAMED FOR WHAT THEY DIVIDE.**
    `inter_row` is the pass between two rows and `inter_column` the pass between
    two columns - the words the menu uses, so the file and the screen say one
    thing.

    **THE RAIL SCORE SETBACK GROWS THE PANEL.** It is a buffer added to the
    board on each side, so the blank grows by twice it on that axis and the rail
    keeps the dimension the user typed - panel edge to rail score - with the
    setback standing as
    plain material between the score and the board array. NOTHING IN THIS MODULE
    APPLIES IT: the blank arrives already grown
    (:func:`eedesignpanels.stages.panelize.blank_rails`), and the rail pass lands
    at the panel edge plus the rail width, which is what it always did.

    **THE TWO JUMP DISTANCES SHORTEN EVERY PASS EXCEPT A RAIL PASS.** This
    module shortens its own inter-row and inter-column passes in
    :func:`apply`; :func:`eedesignpanels.stages.panelize.trim_jumps` shortens the
    merged board-defined ones. A rail pass is never shortened.

    **A DEPENDANT WITH ITS GOVERNOR OFF IS ZERO.** Four of these
    fields hang off a rail switch; :attr:`governed` is the one place that
    applies it, and every read site in this project goes through it.
    """

    # IN THE ORDER THE PAGE LISTS THEM: each rail score, then
    # the setback and the jump that hang off it, then the two passes between
    # boards.
    rails_horizontal: bool = False
    # HOW MUCH THE TOP AND BOTTOM RAIL SCORES GROW THE PANEL, on each of those
    # two sides. The pass does not move; the panel gets bigger.
    rail_setback_horizontal_mm: float = 0.0
    # THE DISTANCE BELONGS TO THE LINE'S OWN DIRECTION: a
    # horizontal pass stops `jump_horizontal_mm` short at each end, a vertical
    # pass `jump_vertical_mm`. Zero is the full-span pass, to the byte, and zero
    # is the whole of "no jump" - there is no switch beside the number.
    jump_vertical_mm: float = 0.0
    # WHICH SCORE A SHARED LINE IS. True is the
    # rail score - full span, no jump - and it is the default because that is
    # the treatment that lets a panel come apart. THE TOOL CANNOT DECIDE THIS:
    # the rail's line and the board's line are the same cut and both readings
    # are real, so the user answers per rail score.
    shared_line_is_rail_score_horizontal: bool = True
    rails_vertical: bool = False
    rail_setback_vertical_mm: float = 0.0
    jump_horizontal_mm: float = 0.0
    shared_line_is_rail_score_vertical: bool = True
    inter_row: bool = False
    inter_column: bool = False
    rule_half_width_mm: float = DEFAULT_RULE_HALF_WIDTH_MM

    @property
    def jumping(self) -> bool:
        """Does this config ask for a pass to stop short of the panel edge?"""
        return self.jump_horizontal_mm > 0.0 or self.jump_vertical_mm > 0.0

    @property
    def setbacks(self) -> Setbacks:
        """The two numbers the BLANK grows by, as these settings state them.

        Take this off :attr:`governed` and not off a raw read: a setback whose
        rail score is off contributes nothing, so a panel with the rail scores
        switched off does not grow.
        """
        return Setbacks(horizontal_mm=self.rail_setback_horizontal_mm,
                        vertical_mm=self.rail_setback_vertical_mm)

    def jump_for(self, axis: str) -> float:
        """The jump distance a pass running along *axis* is shortened by.

        THE DISTANCE BELONGS TO THE LINE'S OWN DIRECTION: a
        vertical pass takes `jump_vertical_mm`, a horizontal pass
        `jump_horizontal_mm`. Read off :attr:`governed` for the same reason the
        setbacks are.
        """
        return (self.jump_vertical_mm if axis == "vertical"
                else self.jump_horizontal_mm)

    def shared_line_is_rail_score(self, axis: str) -> bool:
        """Is a shared line on *axis* the RAIL score rather than the board's?

        *axis* is the direction the rail PASS runs,
        which is also the direction its rail score is named for: the top and
        bottom rail passes run horizontally and belong to `rails_horizontal`,
        exactly as that switch's setback and its own name do. Read off
        :attr:`governed`, because with the rail score off there is no rail line
        for anything to share.
        """
        return (self.shared_line_is_rail_score_horizontal
                if axis == "horizontal"
                else self.shared_line_is_rail_score_vertical)

    @property
    def ignored(self) -> tuple:
        """Every dependant whose governor is off, with the number it states.

        Empty on settings that have already been through :attr:`governed`, which
        is what makes that property safe to apply twice.
        """
        return tuple(
            Ignored(setting=label, governor=governor_label,
                    stated_mm=float(getattr(self, field)))
            for field, governor, label, governor_label in DEPENDANTS
            if not getattr(self, governor) and float(getattr(self, field)) > 0.0
        )
        # THE SHARED-LINE TICKS ARE NOT REPORTED HERE. They default ON, so an
        # ungoverned one would put a line in every report on every panel -
        # noise, and the opposite of what naming an ignored setting is for. What
        # the user has to be told is which
        # way a line that WAS shared got treated, and
        # :attr:`PanelScores.shared_lines` is where that is said.

    @property
    def governed(self) -> "Settings":
        """These settings with every ungoverned dependant taken to zero.

        **THE ONE PLACE THE RULE LIVES.** Without it, a jump distance could
        shorten saw lines while the rail scores that govern it are switched
        off. The page greys a dependant
        when its governor is off; the build has to reach the same answer, so
        every read of a dependant in this project comes through here.

        Idempotent, which is why :func:`plan`, :func:`apply` and
        :func:`eedesignpanels.stages.panelize.carrier` may each ask without any
        of them having to know whether another already did.
        """
        zeroed = {
            field: 0.0
            for field, governor, _label, _governor_label in DEPENDANTS
            if not getattr(self, governor)
        }
        # A SWITCH DEPENDANT'S "zero" IS False:
        # with the rail score off there is no rail line, so nothing can be
        # shared with it and the answer to "which is a shared line" is no.
        zeroed.update({
            field: False
            for field, governor, _label, _governor_label in SHARED_LINE_DEPENDANTS
            if not getattr(self, governor)
        })
        return self._replace(**zeroed) if zeroed else self

    @classmethod
    def of(cls, value) -> "Settings":
        """Settings from a config section, a mapping, or another Settings.

        RAW, exactly as the file states it: the gate is :attr:`governed`, and
        reading and gating in one step would leave nothing able to report the
        number the user typed.
        """
        if isinstance(value, cls):
            return value
        read = (value.get if isinstance(value, dict)
                else lambda key, default=None: getattr(value, key, default))
        return cls(
            rails_horizontal=bool(read("rails_horizontal", False)),
            rail_setback_horizontal_mm=_distance_or_zero(
                read("rail_setback_horizontal_mm")
            ),
            jump_vertical_mm=_distance_or_zero(read("jump_vertical_mm")),
            # DEFAULTS TRUE: a section that does
            # not carry the key gets the rail score - full span, no jump - which
            # is the treatment that lets a panel come apart.
            shared_line_is_rail_score_horizontal=bool(
                read("shared_line_is_rail_score_horizontal", True)
            ),
            rails_vertical=bool(read("rails_vertical", False)),
            rail_setback_vertical_mm=_distance_or_zero(
                read("rail_setback_vertical_mm")
            ),
            jump_horizontal_mm=_distance_or_zero(read("jump_horizontal_mm")),
            shared_line_is_rail_score_vertical=bool(
                read("shared_line_is_rail_score_vertical", True)
            ),
            inter_row=bool(read("inter_row", False)),
            inter_column=bool(read("inter_column", False)),
            rule_half_width_mm=_width_or_default(read("rule_half_width_mm")),
        )

    @property
    def any_on(self) -> bool:
        """Does this config ask for a pass of the panel's own at all?

        False is a real setting and not an idle one: the stage still gives every
        merged board-edge pass its rule area, which is how a panel asks for the
        DRC round the scores its boards already decided.
        """
        return (self.rails_horizontal or self.rails_vertical
                or self.inter_row or self.inter_column)

    @property
    def switches_on(self) -> tuple:
        """The switches that are set, in the order the page shows them."""
        return tuple(
            name for name, on in (("rails_horizontal", self.rails_horizontal),
                                  ("rails_vertical", self.rails_vertical),
                                  ("inter_row", self.inter_row),
                                  ("inter_column", self.inter_column))
            if on
        )


class PanelScore(NamedTuple):
    """One saw pass this stage put on the panel, and what asked for it."""

    axis: str
    # Where the panel says it is: across or down from the blank's top-left
    # corner, in millimetres. Derived from the layout, reported for measuring.
    position_mm: float
    start: tuple
    end: tuple
    # Which of the layout's features put it here - "column gap 1", "left rail".
    source: str = ""

    @property
    def length_mm(self) -> float:
        return abs(self.end[0] - self.start[0]) + abs(self.end[1] - self.start[1])


class SharedLine(NamedTuple):
    """One saw line a rail score and the boards both landed on, and its meaning.

    THE REPORT SAYS WHAT HAPPENED. A shared line
    carries two meanings and the tool cannot tell which is wanted, so the run
    states every one it found and which way the user's setting took it, so a
    saw line never changes meaning without a report.
    """

    axis: str
    # Across or down from the blank's top-left corner, the datum every score is
    # reported against.
    position_mm: float
    # Which rail asked for it - "left rail", "top rail".
    source: str
    # True: taken as the RAIL score, full span, no jump. False: left as the
    # board-edge score the merge drew, shortened by its direction's jump.
    as_rail_score: bool
    # Did taking it as the rail score actually lengthen the line? False when the
    # merged line already ran the full span, which is every panel with no jump.
    lengthened: bool = False

    @property
    def sentence(self) -> str:
        which = "the rail score" if self.as_rail_score else "a board-edge score"
        how = (
            "put back across the whole panel"
            if self.lengthened else
            ("left running edge to edge" if self.as_rail_score
             else "left as the boards' own pass")
        )
        return (
            f"the {self.axis} saw line at {self.position_mm:g} mm is shared by "
            f"the {self.source} and a pass the boards decided, and was treated "
            f"as {which} - {how}"
        )


class ScoreRuleArea(NamedTuple):
    """One thin keepout, centred on one score line."""

    start: tuple
    end: tuple
    half_width_mm: float
    layers: tuple


class PanelScores(NamedTuple):
    """What the stage put on the panel, counted and measured."""

    scores: tuple
    rule_areas: tuple
    half_width_mm: float
    # The score lines already on the panel when the stage ran - the merged
    # instance-edge passes. They get rule areas too, and are counted apart so
    # the report can say which came from where.
    inherited_lines: int
    # The switches this run was made with, by name.
    switches: tuple = ()
    # Passes the layout asked for that the BOARDS had already decided - a rail
    # whose inner edge is a scored board edge is the ordinary case. Named, so
    # the report can say the pass is there and this stage did not draw it.
    already_scored: tuple = ()
    # True when the config named NO scores section and this run exists only to
    # rule-area the passes the boards decided (:func:`protect`). The reports say
    # so, because "0 panel scores" and "no panel scores were asked for" are
    # different sentences and the second one is the true one.
    protection_only: bool = False
    # THE DEPENDANTS THIS RUN DID NOT USE. A jump
    # or a setback whose rail score is off contributes nothing, and the report
    # has to say so in its own line: a number in the file that did nothing must
    # never look like one that did.
    ignored: tuple = ()
    # EVERY LINE A RAIL SCORE AND THE BOARDS BOTH LANDED ON, with the meaning
    # the user's setting gave it. One saw
    # line, two possible cuts, and the report has to say which was built.
    shared_lines: tuple = ()
    # HOW MANY SCORE LINES THE BOARDS THEMSELVES GUARD END TO END.
    # A panel of scored boards inherits their keepouts and adds none over them,
    # so "0 rule areas" can mean "every line is already guarded" - which is a
    # different sentence from "nothing is guarded" and has to be said.
    inherited_guarded: int = 0

    @property
    def count(self) -> int:
        return len(self.scores)


class ScorePlan(NamedTuple):
    """One saw pass the layout calls for, before the blank is measured."""

    axis: str
    # Where the line runs, in BOARD millimetres: an x for a vertical pass, a y
    # for a horizontal one.
    at_mm: float
    source: str
    # IS THIS A RAIL PASS? A rail score is never shortened by a jump. Carried as
    # a fact rather than read back out of the
    # source sentence, because a jump that shortened a rail pass would eat the
    # rim the jump exists to keep.
    rail: bool = False


def copper_layers(board: Board) -> tuple:
    """Every copper layer the board declares, in the board file's own order."""
    return tuple(
        layer.canonical_name
        for layer in board.layers
        if layer.canonical_name.endswith(".Cu")
    )


def _coordinate(value: float) -> str:
    """One millimetre coordinate at the file's own precision, sign-normalised."""
    rounded = round(value, 6)
    return f"{rounded + 0.0:.6f}" if rounded else "0.000000"


def score_span(axis: str, position_mm: float, blank) -> tuple:
    """The two ends of one panel score, across the whole blank.

    A saw enters at one edge of the panel and leaves at the other, so a panel
    score is always the full width or the full height. Where it starts is the
    datum: the blank's top-left corner.

    Raises:
        ValueError: when the position is not on the blank.
    """
    if axis == "vertical":
        limit = blank.width_mm
        if not 0.0 <= position_mm <= limit:
            raise ValueError(
                f"vertical panel score at x = {position_mm:g} mm is off the "
                f"blank, which is {limit:.4f} mm wide measured from its "
                f"top-left corner"
            )
        x = blank.min_x + position_mm
        return ((x, blank.min_y), (x, blank.max_y))
    if axis == "horizontal":
        limit = blank.height_mm
        if not 0.0 <= position_mm <= limit:
            raise ValueError(
                f"horizontal panel score at y = {position_mm:g} mm is off the "
                f"blank, which is {limit:.4f} mm tall measured from its "
                f"top-left corner"
            )
        y = blank.min_y + position_mm
        return ((blank.min_x, y), (blank.max_x, y))
    raise ValueError(
        f"a panel score runs 'vertical' or 'horizontal', not {axis!r}"
    )


def span_at(axis: str, at_mm: float, blank) -> tuple:
    """The two ends of one panel score at a BOARD coordinate, across the blank.

    The primitive :func:`score_span` is written in terms of: a computed position
    is a coordinate on the panel, so the line is built from it exactly rather
    than from the datum plus a difference, and the number the file carries is the
    number the layout produced.

    Raises:
        ValueError: when the coordinate is not on the blank.
    """
    # Validated through the datum spelling, so an impossible coordinate is
    # refused in the words the report and the user both use.
    score_span(axis, position_of(axis, at_mm, blank), blank)
    if axis == "vertical":
        return ((at_mm, blank.min_y), (at_mm, blank.max_y))
    return ((blank.min_x, at_mm), (blank.max_x, at_mm))


def position_of(axis: str, at_mm: float, blank) -> float:
    """A board coordinate as the datum reads it: across or down from the corner."""
    return at_mm - (blank.min_x if axis == "vertical" else blank.min_y)


def spans_of(instance_boxes, axis: str) -> tuple:
    """Each column's or row's own extent, in order, deduplicated.

    `(low, high)` pairs measured off the board rectangles themselves rather than
    off the grid settings, exactly as the rail pour measures its gaps: the score
    goes down the space between the boards that are actually there.
    """
    index = 0 if axis == "vertical" else 1
    return tuple(sorted({(box[index], box[index + 2]) for box in instance_boxes}))


def gap_midlines(instance_boxes, axis: str, warnings=None) -> tuple:
    """The MIDDLE of every clear gap between neighbouring columns or rows.

    One number per gap, ascending. Two boards that touch leave no gap and get no
    score - there is nothing between them for a blade to run down - and that is
    said out loud rather than silently skipped.

    **A LINE THAT WOULD CROSS A BOARD IS DROPPED.** With a regular
    grid the boards fall into as many distinct bands as there are columns and
    rows, and the space between two consecutive bands is a real gap. A NEST
    SHIFT or a PLACEMENT FILE can break that: a row pulled sideways gives its
    column a band of its own, and the "gap" between two overlapping bands is a
    stretch of somebody's board. So every candidate is checked against every
    board rectangle before it is kept, and a saw pass that would run through a
    board is refused with the reason rather than cut. The check never fires on
    an aligned grid, which is fenced byte for byte.
    """
    spans = spans_of(instance_boxes, axis)
    index_on_axis = 0 if axis == "vertical" else 1
    out = []
    for index in range(len(spans) - 1):
        high, low = spans[index][1], spans[index + 1][0]
        if low - high <= SAME_LINE_TOL_MM:
            if warnings is not None:
                warnings.append(
                    f"the {axis} gap between "
                    f"{'column' if axis == 'vertical' else 'row'} "
                    f"{index + 1} and {index + 2} is {low - high:.4f} mm, so "
                    f"there is nothing between those boards for a saw to run "
                    f"down and no panel score was put there"
                )
            continue
        at = (high + low) / 2.0
        crossed = [
            box for box in instance_boxes
            if box[index_on_axis] + SAME_LINE_TOL_MM < at
            < box[index_on_axis + 2] - SAME_LINE_TOL_MM
        ]
        if crossed:
            if warnings is not None:
                warnings.append(
                    f"a {axis} panel score at {at:.4f} mm would run through "
                    f"{len(crossed)} board(s), because this arrangement does "
                    f"not put the boards in clear bands - a nest shift or a "
                    f"placement file offsets some of them. That pass was not "
                    f"cut."
                )
            continue
        out.append(at)
    return tuple(out)


def rail_inner_edges(blank, rails) -> tuple:
    """Where each rail that EXISTS ends: the panel edge plus that rail's width.

    `(axis, coordinate, side)` per rail, derived from the blank and the rail
    widths - the two numbers the blank was grown from - so the line lands exactly
    on the edge of the material the carrier is made of. A rail of zero width is
    not a rail: there is no carrier there to separate from anything.

    **THE SETBACK IS NOT APPLIED HERE, AND THAT IS THE WHOLE POINT.** A setback
    GROWS THE BLANK by that much on each side
    (:func:`eedesignpanels.stages.panelize.stepped`); it never moves the score.
    The rail dimension the user typed is the distance from the panel edge to
    the rail score, which is exactly what this returns, setback or no setback.
    With the panel already grown, the plain material between this line and the
    board array IS the setback, and no arithmetic here has to know about it.
    """
    sides = channels.Rails.of(rails)
    candidates = (
        ("vertical", blank.min_x + sides.left, "left", sides.left),
        ("vertical", blank.max_x - sides.right, "right", sides.right),
        ("horizontal", blank.min_y + sides.top, "top", sides.top),
        ("horizontal", blank.max_y - sides.bottom, "bottom", sides.bottom),
    )
    return tuple(
        (axis, at, side)
        for axis, at, side, width in candidates
        if width > 0.0
    )


def plan(blank, settings, instance_boxes=(), rails=None, warnings=None) -> tuple:
    """Every saw pass this panel's layout calls for, in a fixed order.

    Pure: the blank, the board rectangles, the rail widths and four switches in;
    a list of :class:`ScorePlan` out. Verticals first and then horizontals, each
    ascending, so the same panel always writes the same lines in the same order
    however the layout was measured.

    Two passes on one line are ONE pass, whichever feature asked for them.

    THE TWO RAIL SWITCHES SELECT BY THE PASS'S OWN DIRECTION: a pass along the
    top or the bottom rail runs across the panel
    and is a HORIZONTAL line, so `rails_horizontal` takes exactly the edges
    `rail_inner_edges` reports as horizontal, and `rails_vertical` the left and
    right ones.

    A SETBACK MOVES NOTHING THIS FUNCTION PLACES. It grew the blank before this ran,
    so the rail pass sits at the panel edge plus the rail width and the plain
    material out to the board array is the setback itself.

    A JUMP SHORTENS EVERY PASS EXCEPT A RAIL PASS, which is why
    each plan entry says which it is. The shortening happens in :func:`apply`,
    where the line has ends to shorten.
    """
    settings = Settings.of(settings).governed
    wanted = []
    if settings.inter_column:
        wanted += [
            ScorePlan("vertical", at, f"column gap {index + 1}")
            for index, at in enumerate(
                gap_midlines(instance_boxes, "vertical", warnings)
            )
        ]
    if settings.inter_row:
        wanted += [
            ScorePlan("horizontal", at, f"row gap {index + 1}")
            for index, at in enumerate(
                gap_midlines(instance_boxes, "horizontal", warnings)
            )
        ]
    if settings.rails_horizontal or settings.rails_vertical:
        if rails is None:
            if warnings is not None:
                warnings.append(
                    "the rail scores were asked for and this build did not say "
                    "how wide the rails are, so no rail score could be placed"
                )
        else:
            asked = set()
            if settings.rails_horizontal:
                asked.add("horizontal")
            if settings.rails_vertical:
                asked.add("vertical")
            wanted += [
                ScorePlan(axis, at, f"{side} rail", rail=True)
                for axis, at, side in rail_inner_edges(blank, rails)
                if axis in asked
            ]

    wanted.sort(key=lambda item: (0 if item.axis == "vertical" else 1,
                                  round(item.at_mm, 6), item.source))
    out = []
    for item in wanted:
        if any(other.axis == item.axis
               and abs(other.at_mm - item.at_mm) <= SAME_LINE_TOL_MM
               for other in out):
            continue
        out.append(item)
    return tuple(out)


def jumped_span(start: tuple, end: tuple, distance_mm: float):
    """The line, shortened by *distance_mm* at BOTH ends. None when it cannot be.

    JUMP SCORING, and THE ONE PLACE THE ARITHMETIC LIVES: a jump distance
    shortens every score line running in its
    direction - the inter-row passes, the inter-column passes and the lines the
    boards decided - at both ends. A RAIL score is never shortened, so a rail
    pass never reaches this function.

    None is a pass with no room for the jump: what would be left is under
    :data:`MIN_JUMPED_PASS_MM`, so there is no saw pass to make and the caller
    leaves the line exactly as it was rather than losing a cut quietly.
    """
    length = _distance(start, end)
    if length <= 0.0 or length - 2.0 * distance_mm < MIN_JUMPED_PASS_MM:
        return None
    unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    return (
        (start[0] + unit[0] * distance_mm, start[1] + unit[1] * distance_mm),
        (end[0] - unit[0] * distance_mm, end[1] - unit[1] * distance_mm),
    )


def crossed_boards(axis: str, start: tuple, instance_boxes) -> int:
    """How many board instances this score runs THROUGH.

    Touching a board's edge is not crossing it: an instance-edge score is
    exactly that, and it is the merge's business rather than a warning. So the
    comparison is strict on both sides.
    """
    crossed = 0
    for box in instance_boxes:
        if axis == "vertical" and box[0] < start[0] < box[2]:
            crossed += 1
        elif axis == "horizontal" and box[1] < start[1] < box[3]:
            crossed += 1
    return crossed


def rule_area_corners(start: tuple, end: tuple, half_width_mm: float) -> list:
    """The four corners of a thin rectangle centred on a segment, OVERHANGING it.

    Written for any direction rather than for the two axes, because the merged
    instance-edge scores come from the boards and a board is not obliged to be
    drawn square to the panel.

    **THE GUARD RUNS PAST THE END OF THE LINE**: it
    is the line's length PLUS the keep-out width at each end, so the guard wraps
    the end of the cut the same way it guards the sides. A blade does not stop
    being a blade at the last millimetre of its run, and copper sitting just off
    the end of a pass is as exposed as copper beside it. A line a jump shortened
    is guarded the same way - the keep-out follows the shortened line and then
    extends past each of its ends.

    The overhang is exactly *half_width_mm*, which is what lets
    :func:`keepout_span` read the LINE back out of the rectangle: the rectangle
    is the line plus its own half width at each end, and nothing else.

    Raises:
        ValueError: for a segment of no length, which has no direction and
            therefore no rectangle.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0.0:
        raise ValueError(
            f"a score line of no length at ({start[0]:.4f}, {start[1]:.4f}) mm "
            f"has no direction, so no rule area can be centred on it"
        )
    # Sideways by the half width, and lengthways by the same, so the rectangle
    # is (length + 2 * half) by (2 * half).
    nx, ny = -dy / length * half_width_mm, dx / length * half_width_mm
    ex, ey = dx / length * half_width_mm, dy / length * half_width_mm
    first = (start[0] - ex, start[1] - ey)
    last = (end[0] + ex, end[1] + ey)
    return [
        (first[0] + nx, first[1] + ny),
        (last[0] + nx, last[1] + ny),
        (last[0] - nx, last[1] - ny),
        (first[0] - nx, first[1] - ny),
    ]


def rule_area_for(start: tuple, end: tuple, half_width_mm: float,
                  layers) -> Zone:
    """One score line's keepout: thin, centred on it, on every copper layer.

    Everything is kept out - tracks, vias, pads, copper pour and footprints -
    because everything is cut by a saw. Keeping out the pour is what makes a
    fill avoid the region on its own, so a rail pour holds off the score
    without anybody computing where the score is twice.

    **`ZoneKeepout`'s fields read "is this ALLOWED", not "is this kept out".**
    `True` writes `(tracks allowed)` and keeps nothing out, which is what the
    model's own defaults do, so every field here is False. Caught by measuring
    the panel rather than by reading the code: with the flags set the other way
    round the rail pour ran straight across every score line and DRC said
    nothing.
    """
    key = "|".join(
        [_coordinate(value) for point in (start, end) for value in point]
        + [_coordinate(half_width_mm)]
    )
    return Zone(
        layers=list(layers),
        id=str(uuid.uuid5(RULE_AREA_NAMESPACE, key)),
        name=RULE_AREA_NAME,
        hatch_style="full",
        hatch_pitch=0.508,
        keepout=ZoneKeepout(
            tracks=False, vias=False, pads=False, copper_pour=False,
            footprints=False,
        ),
        polygons=[ZoneOutlinePolygon(
            pts=rule_area_corners(start, end, half_width_mm)
        )],
    )


def keeps_everything_out(zone) -> bool:
    """Is this zone a keepout that stops a saw's worth of everything?

    The five flags this stage writes, all of them. A zone keeping out only vias
    is a statement about vias and says nothing about a blade, so it is not read
    as a score's guard however it is shaped.
    """
    keepout = getattr(zone, "keepout", None)
    if keepout is None:
        return False
    return not any((
        keepout.tracks, keepout.vias, keepout.pads, keepout.copper_pour,
        keepout.footprints,
    ))


def keepout_span(zone) -> tuple:
    """One thin keepout read BACK off its geometry: `(start, end, half_width)`.

    None for anything that is not one - a zone with no keepout, a keepout that
    allows something through, an outline that is not a single rectangle, or a
    rectangle too square to say which way it runs.

    **Geometry, not names and not uuids**. A score
    keepout that arrives on a board instance has been through a step-and-repeat
    and carries a uuid derived for that instance, and the merged panel line it
    guards was drawn by another stage entirely. What makes it the same statement
    as the one this stage would draw is where it is, so where it is, is what is
    read.
    """
    if not keeps_everything_out(zone):
        return None
    polygons = list(getattr(zone, "polygons", ()) or ())
    if len(polygons) != 1:
        return None
    pts = [tuple(point) for point in polygons[0].pts]
    if len(pts) != 4:
        return None
    # A rectangle, and not merely a parallelogram: equal diagonals is the whole
    # of that test, and a leaning keepout is not a strip either side of a line.
    if abs(_distance(pts[0], pts[2]) - _distance(pts[1], pts[3])) > ON_LINE_TOL_MM:
        return None
    # The corners as :func:`rule_area_corners` writes them: along one side, back
    # along the other. Either pairing gives a rectangle; the centreline runs
    # along the LONGER pair, because a score keepout is thin by construction.
    sides = [
        (pts[0], pts[1], pts[3], pts[2]),  # 0->1 and 3->2 are the long sides
        (pts[0], pts[3], pts[1], pts[2]),  # ...or 0->3 and 1->2 are
    ]
    best = None
    for first_a, first_b, second_a, second_b in sides:
        length = (_distance(first_a, first_b) + _distance(second_a, second_b)) / 2.0
        width = (_distance(first_a, second_a) + _distance(first_b, second_b)) / 2.0
        if abs(_distance(first_a, first_b) - _distance(second_a, second_b)) > ON_LINE_TOL_MM:
            return None
        if length <= width + ON_LINE_TOL_MM:
            continue
        # THE OVERHANG COMES BACK OFF. The
        # rectangle is the line plus the half width at each end
        # (:func:`rule_area_corners`), so the LINE is the rectangle less that
        # much - and every reader of this - coverage, and the board stage's
        # "does this keepout match its line" - goes on comparing against the
        # cut rather than against the guard.
        half = width / 2.0
        line_start = _midpoint(first_a, second_a)
        line_end = _midpoint(first_b, second_b)
        if length - 2.0 * half <= ON_LINE_TOL_MM:
            continue
        unit = (
            (line_end[0] - line_start[0]) / length,
            (line_end[1] - line_start[1]) / length,
        )
        best = (
            (line_start[0] + unit[0] * half, line_start[1] + unit[1] * half),
            (line_end[0] - unit[0] * half, line_end[1] - unit[1] * half),
            half,
        )
    return best


def _distance(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _midpoint(a: tuple, b: tuple) -> tuple:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def score_keepouts_on(board: Board) -> list:
    """Every thin all-out keepout the board carries, board level and footprints.

    `(zone, start, end, half_width)` per keepout, in the order the board holds
    them. The footprints' are included because a score keepout arrives with the
    instance that decided it, and `eedesigncore` moves a footprint-hosted zone
    with its footprint.
    """
    out = []
    hosts = [board.zones]
    hosts += [footprint.zones for footprint in board.footprints]
    for zones in hosts:
        for zone in zones:
            span = keepout_span(zone)
            if span is not None:
                out.append((zone, *span))
    return out


def covered_runs(start: tuple, end: tuple, half_width_mm: float,
                 keepouts: list, warnings=None) -> tuple:
    """How much of this line is guarded already, as `(from, to)` along it.

    Distances are measured from *start*, in millimetres, clamped to the line and
    merged - so two instance keepouts that meet come back as one run.

    A keepout ON the line at a DIFFERENT half width is not coverage: it guards a
    strip of a different size, and quietly accepting it would leave the panel
    guarding less than its config asks for. It is named instead, both numbers
    said, and the panel draws its own.
    """
    length = _distance(start, end)
    if length <= 0.0:
        return ()
    unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    runs = []
    mismatched = []
    for _zone, other_start, other_end, half in keepouts:
        if not _on_line(start, unit, other_start) or not _on_line(start, unit,
                                                                 other_end):
            continue
        if abs(half - half_width_mm) > ON_LINE_TOL_MM:
            mismatched.append(half)
            continue
        along = sorted(
            (_along(start, unit, other_start), _along(start, unit, other_end))
        )
        low, high = max(0.0, along[0]), min(length, along[1])
        if high - low > MIN_RULE_AREA_MM:
            runs.append((low, high))
    if mismatched and warnings is not None:
        widths = ", ".join(f"{value:g}" for value in sorted(set(mismatched)))
        warnings.append(
            f"a score line at ({start[0]:.4f}, {start[1]:.4f}) mm already "
            f"carries {len(mismatched)} keepout(s) half {widths} mm wide and "
            f"this panel asks for {half_width_mm:g} mm, so the two are not the "
            f"same guard - the panel drew its own over the whole line"
        )
    runs.sort()
    merged = []
    for low, high in runs:
        if merged and low <= merged[-1][1] + MIN_RULE_AREA_MM:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return tuple(merged)


def _on_line(start: tuple, unit: tuple, point: tuple) -> bool:
    """Is *point* on the line through *start* in direction *unit*?"""
    offset = (point[0] - start[0], point[1] - start[1])
    return abs(offset[0] * unit[1] - offset[1] * unit[0]) <= ON_LINE_TOL_MM


def _along(start: tuple, unit: tuple, point: tuple) -> float:
    """How far along the line *point* sits, in millimetres from *start*."""
    return (point[0] - start[0]) * unit[0] + (point[1] - start[1]) * unit[1]


def uncovered_runs(start: tuple, end: tuple, half_width_mm: float,
                   keepouts: list, warnings=None) -> tuple:
    """The stretches of this line that NOTHING guards yet, as point pairs.

    The whole line when nothing guards it, which is every panel built before the
    boards carried their own keepouts. Nothing at all when the boards guard all
    of it.
    """
    length = _distance(start, end)
    if length <= 0.0:
        return ()
    unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    at = 0.0
    spans = []
    for low, high in covered_runs(start, end, half_width_mm, keepouts,
                                  warnings):
        if low - at > MIN_RULE_AREA_MM:
            spans.append((at, low))
        at = max(at, high)
    if length - at > MIN_RULE_AREA_MM:
        spans.append((at, length))

    def at_mm(distance: float) -> tuple:
        """The point *distance* along the line - and THE LINE'S OWN ENDS exactly.

        `start + unit * length` is within an ulp of `end` and is not `end`, and
        an ulp is a different uuid and a different byte. A line nothing guards
        must produce the rule area it produced before any of this existed, so
        the two ends are handed back rather than recomputed.
        """
        if distance <= 0.0:
            return start
        if distance >= length:
            return end
        return (start[0] + unit[0] * distance, start[1] + unit[1] * distance)

    return tuple(
        (at_mm(low), at_mm(high)) for low, high in spans
    )


def score_lines_on(board: Board) -> list:
    """Every board-level `User.38` straight line, in a fixed order.

    Sorted by geometry rather than taken in file order: the merged passes are
    appended by one stage and this stage's own by another, and a rule area set
    that depended on which ran first would change when nothing about the panel
    had.
    """
    lines = [
        item
        for item in items_on_layers(
            (*board.drawings, *board.trailing_drawings), {SCORE_LAYER}
        )
        if type(item).__name__ == "PcbLine"
    ]
    return sorted(lines, key=lambda item: (item.start, item.end))


def already_on(lines, axis: str, at_mm: float) -> bool:
    """Is this exact pass already on the panel, drawn by somebody else?

    A merged board-edge score lies where the boards decided, and a rail's inner
    edge IS a board edge, so the two coincide on any panel whose outer board
    edges are scored. One line is the right answer; two identical lines is a
    thing KiCad shows twice and a fabricator has to ask about.
    """
    index = 0 if axis == "vertical" else 1
    for line in lines:
        if abs(line.start[index] - line.end[index]) > SAME_LINE_TOL_MM:
            continue  # not a pass on this axis at all
        if abs(line.start[index] - at_mm) <= SAME_LINE_TOL_MM:
            return True
    return False


def lines_at(board: Board, axis: str, at_mm: float) -> list:
    """Every `User.38` pass running along *axis* at this coordinate.

    `(held, index, line)` per hit, so a caller can put a replacement in the
    line's own place. Collinearity is the only test - same direction, same
    position within :data:`SAME_LINE_TOL_MM`, the tolerance this project already
    uses for "on this line" - because a shared line is a line two features
    landed on and not two features that know about each other.
    """
    index_on_axis = 0 if axis == "vertical" else 1
    found = []
    for held in (board.drawings, board.trailing_drawings):
        for index, item in enumerate(held):
            if type(item).__name__ != "PcbLine":
                continue
            if getattr(item, "layer", None) != SCORE_LAYER:
                continue
            start, end = tuple(item.start), tuple(item.end)
            if abs(start[index_on_axis] - end[index_on_axis]) > SAME_LINE_TOL_MM:
                continue  # not a pass on this axis at all
            if abs(start[index_on_axis] - at_mm) > SAME_LINE_TOL_MM:
                continue
            found.append((held, index, item))
    return found


def take_as_rail_score(board: Board, blank, axis: str, at_mm: float) -> bool:
    """Put a shared saw line back across the WHOLE panel. True when it moved it.

    **THE USER DECIDES THIS AND THE TOOL CANNOT.** A rail score with a zero
    setback lands exactly on a pass the boards decided. As a BOARD-EDGE score
    that pass has already been stopped short at both ends by its direction's
    jump (:func:`eedesignpanels.stages.panelize.trim_jumps`, which runs before
    this stage); as a RAIL score it runs the full span and no jump touches it.
    Both are real cuts, nothing in the board or the config distinguishes them,
    and the full-span reading is the one that lets a panel separate - so the
    answer is a setting and this function is what carries it out.

    IT REPLACES, IT DOES NOT MUTATE, for the reason `trim_jumps` does: the panel
    may be a cheap copy sharing its items with a panel somebody else holds
    (`step_and_repeat.cheap_copy`). The uuid rides along unchanged, because a
    pass put back to full length is the same pass and not a second one.
    """
    start, end = span_at(axis, at_mm, blank)
    moved = False
    for held, index, item in lines_at(board, axis, at_mm):
        was = (tuple(item.start), tuple(item.end))
        # EITHER WAY ROUND IS THE SAME PASS. A line already running edge to edge
        # is left exactly as it is, whichever end it was drawn from, so a panel
        # with no jump is byte-identical to the panel built before this setting.
        if any(_distance(was[0], first) <= SAME_LINE_TOL_MM
               and _distance(was[1], second) <= SAME_LINE_TOL_MM
               for first, second in ((start, end), (end, start))):
            continue
        held[index] = item.model_copy(update={"start": start, "end": end})
        moved = True
    return moved


def apply(board: Board, blank, settings, instance_boxes=(),
          warnings=None, rails=None) -> PanelScores:
    """Draw the panel's own scores and rule-area every score on the panel.

    In place, and in this order: the layout's own lines go on first, then every
    `User.38` line the panel now holds - this stage's and the merged
    instance-edge ones alike - gets one rule area. That is what makes the
    keepout ONE mechanism instead of two that can disagree.

    Every position is COMPUTED (:func:`plan`) from *instance_boxes* - where the
    boards landed - and from *rails*, the four widths the blank was grown by.
    Nothing here reads a position out of the config.

    A DEPENDANT WHOSE GOVERNOR IS OFF IS ZERO HERE TOO. The gate is
    idempotent, so it costs nothing when the caller
    already applied it and it cannot be forgotten when the caller did not.

    Raises:
        ValueError: when a computed position is off the blank, or when a
            `User.38` line has no length.
    """
    stated = Settings.of(settings)
    ignored = stated.ignored
    settings = stated.governed
    warnings = warnings if warnings is not None else []
    inherited_lines = score_lines_on(board)
    inherited = len(inherited_lines)

    scores, skipped, shared = [], [], []
    for item in plan(blank, settings, instance_boxes, rails, warnings):
        if already_on(inherited_lines, item.axis, item.at_mm):
            skipped.append(item.source)
            # ONE SAW LINE, TWO MEANINGS, AND THE USER PICKS. The line is
            # already on the panel, so no second
            # one is drawn either way; what is left to decide is whether it is
            # the RAIL score, which runs the full span and takes no jump, or the
            # BOARD-EDGE score the jump has already stopped short. THE TOOL
            # CANNOT TELL - both cuts are legitimate and the geometry is
            # identical - and the full-span reading is the one that lets a
            # panel come apart, so the setting answers and the report says which.
            if item.rail:
                as_rail = settings.shared_line_is_rail_score(item.axis)
                lengthened = (
                    take_as_rail_score(board, blank, item.axis, item.at_mm)
                    if as_rail else False
                )
                shared.append(SharedLine(
                    axis=item.axis,
                    position_mm=position_of(item.axis, item.at_mm, blank),
                    source=item.source, as_rail_score=as_rail,
                    lengthened=lengthened,
                ))
            continue
        start, end = span_at(item.axis, item.at_mm, blank)
        position = position_of(item.axis, item.at_mm, blank)
        # EVERY SCORE EXCEPT A RAIL SCORE IS SHORTENED BY ITS DIRECTION'S JUMP.
        # The rail pass is the
        # exception because it runs along the rim the jump exists to keep whole.
        # The keep-out is drawn from the line afterwards, so it follows the
        # shortened cut with nothing here having to say so twice.
        distance = 0.0 if item.rail else settings.jump_for(item.axis)
        if distance > 0.0:
            shortened = jumped_span(start, end, distance)
            if shortened is None:
                warnings.append(
                    f"the {item.axis} panel score at {position:g} mm "
                    f"({item.source}) is {_distance(start, end):.2f} mm long "
                    f"and a {distance:g} mm jump at each end would leave under "
                    f"{MIN_JUMPED_PASS_MM:g} mm of cut, so it was left running "
                    f"edge to edge"
                )
            else:
                start, end = shortened
        scores.append(PanelScore(axis=item.axis, position_mm=position,
                                 start=start, end=end, source=item.source))
        crossed = crossed_boards(item.axis, start, instance_boxes)
        if crossed:
            warnings.append(
                f"the {item.axis} panel score at {position:g} mm ({item.source}) "
                f"runs through {crossed} board instance(s) - a panel score is "
                f"for breaking the panel between boards, and this one saws "
                f"across one"
            )
    if skipped:
        warnings.append(
            f"{len(skipped)} panel score(s) the layout asked for are already on "
            f"the panel as merged board-edge passes ({', '.join(skipped)}), so "
            f"no second line was drawn there"
        )

    for score in scores:
        key = f"{score.axis}|{_coordinate(score.position_mm)}"
        board.drawings.append(PcbLine(
            start=score.start,
            end=score.end,
            layer=SCORE_LAYER,
            stroke=Stroke(width=DEFAULT_SCORE_WIDTH_MM),
            id=str(uuid.uuid5(SCORE_NAMESPACE, key)),
        ))

    # COUNTED BEFORE ANYTHING IS ADDED, or every line would look guarded.
    guarded = guarded_end_to_end(board, settings.rule_half_width_mm)
    return PanelScores(
        scores=tuple(scores),
        rule_areas=rule_area_every_score(
            board, settings.rule_half_width_mm, warnings
        ),
        half_width_mm=settings.rule_half_width_mm,
        inherited_lines=inherited,
        switches=settings.switches_on,
        already_scored=tuple(skipped),
        ignored=ignored,
        inherited_guarded=guarded,
        shared_lines=tuple(shared),
    )


def guarded_end_to_end(board: Board, half_width_mm: float) -> int:
    """How many of this panel's score lines are guarded already, end to end.

    The boards' own keepouts, stepped in with them: a line this counts needs
    nothing from the panel, and a panel that reports no rule areas of its own
    may be fully guarded rather than unguarded.
    """
    keepouts = score_keepouts_on(board)
    return sum(
        1 for line in score_lines_on(board)
        if not uncovered_runs(line.start, line.end, half_width_mm, keepouts)
    )


def rule_area_every_score(board: Board, half_width_mm: float,
                          warnings=None) -> tuple:
    """Guard every `User.38` line the panel now holds, ONCE. In place.

    The one mechanism, and the only place a keepout is made: whether a line came
    from a board's own decision or from this stage's own plan, it is a saw pass
    and it is guarded the same way. Returns what it made, in the order it made
    it.

    **IT INHERITS AND IT NEVER DOUBLES**. A board
    that decided a v-score now carries the keepout for it
    (:mod:`eedesignpanels.stages.board_scores`), and that keepout STEPS with the
    board into the panel - so the stretch of the merged blade line that runs
    over a board instance is already guarded before this stage looks at it.
    Drawing a second rule area over it would put two zones in the file saying
    one thing, which is a thing a designer has to ask about and a DRC report
    says twice.

    So each line is guarded over the stretches NOTHING guards yet - the rails
    and the gaps between instances, on a panel of scored boards; the whole line,
    on a panel whose boards brought no keepout with them. Coverage is read off
    the geometry (:func:`keepout_span`), the same way merged scores recognise
    one blade pass drawn twice.
    """
    layers = copper_layers(board)
    # Read ONCE, before anything is added: the answer a line gets must not
    # depend on which line was guarded before it.
    inherited = score_keepouts_on(board)
    areas = []
    guarded = 0
    for line in score_lines_on(board):
        runs = uncovered_runs(line.start, line.end, half_width_mm, inherited,
                              warnings)
        if not runs:
            guarded += 1
            continue
        for start, end in runs:
            board.zones.append(
                rule_area_for(start, end, half_width_mm, layers)
            )
            areas.append(ScoreRuleArea(
                start=start, end=end,
                half_width_mm=half_width_mm, layers=layers,
            ))
    if guarded and warnings is not None:
        warnings.append(
            f"{guarded} score line(s) on this panel are guarded over their "
            f"whole length by the keepout their boards decided, so the panel "
            f"added none of its own there"
        )
    if areas and not layers and warnings is not None:
        warnings.append(
            "the panel declares no copper layer, so the score rule areas have "
            "nothing to keep anything out of and DRC will not police the scores"
        )
    return tuple(areas)


def protect(board: Board, half_width_mm: float = DEFAULT_RULE_HALF_WIDTH_MM,
            warnings=None) -> PanelScores:
    """Rule-area the scores THE BOARDS decided, and add no pass of any kind.

    What a config with `panel.scores.enabled` false gets. That
    switch governs the panel's own families of saw pass - down the gaps, along
    the rail edges - and it was never the switch for the DRC round a score line:
    a merged board-edge pass is on the panel whatever the config says, the rail
    pour has to hold off it, and a designer laying a track across it has to be
    told. The half width is the one the config declares on the prep page, and
    the shipped figure when there is no config at all.

    Nothing happens on a panel with no score line on it, which is the panel
    every config without a scored board builds - to the byte.
    """
    warnings = warnings if warnings is not None else []
    inherited = len(score_lines_on(board))
    guarded = guarded_end_to_end(board, half_width_mm)
    return PanelScores(
        scores=(),
        rule_areas=rule_area_every_score(board, half_width_mm, warnings),
        half_width_mm=half_width_mm,
        inherited_lines=inherited,
        switches=(),
        protection_only=True,
        inherited_guarded=guarded,
    )


__all__ = [
    "DEFAULT_RULE_HALF_WIDTH_MM",
    "DEPENDANTS",
    "SHARED_LINE_DEPENDANTS",
    "DEFAULT_SCORE_WIDTH_MM",
    "MIN_JUMPED_PASS_MM",
    "MIN_RULE_AREA_MM",
    "ON_LINE_TOL_MM",
    "RULE_AREA_NAME",
    "RULE_AREA_NAMESPACE",
    "SAME_LINE_TOL_MM",
    "SCORE_LAYER",
    "SCORE_NAMESPACE",
    "PanelScore",
    "PanelScores",
    "ScorePlan",
    "ScoreRuleArea",
    "SharedLine",
    "Ignored",
    "Setbacks",
    "Settings",
    "already_on",
    "apply",
    "copper_layers",
    "covered_runs",
    "crossed_boards",
    "gap_midlines",
    "guarded_end_to_end",
    "jumped_span",
    "keepout_span",
    "keeps_everything_out",
    "lines_at",
    "plan",
    "position_of",
    "protect",
    "rail_inner_edges",
    "rule_area_every_score",
    "rule_area_corners",
    "rule_area_for",
    "score_keepouts_on",
    "score_lines_on",
    "score_span",
    "span_at",
    "spans_of",
    "take_as_rail_score",
    "uncovered_runs",
]
