"""Resolve keep-upright text on a copy BEFORE the turn, where KiCad puts the ink.

Text KiCad holds upright is resolved on the copy before the turn, so no
draw-time fold is left to move it - and THE INK LANDS WHERE KICAD'S OWN TURN
WOULD PUT IT, which for a turn past 180 degrees means the justification swaps
with the fold. The source citations below are to KiCad 10.0.3.

**Why the fold is computed for the turn.** KiCad turns the stored angle, then
`KeepUpright` folds a text that ended up at or past 180 degrees back upright AND
SWAPS BOTH JUSTIFICATIONS - so the anchor moves to the other end of the ink and
the glyphs stay in the same physical place while still reading left to right.
Folding the SOURCE text upright and then handing the copy the whole turn would
make the copy a rigid rotation of the source ink instead: the anchor stays at
the same end and the ink turns over, so on a turned board the reference
designator grows the wrong way from its anchor. The fold is therefore computed
FOR THE TURN THE COPY TAKES, not for the source text alone, and it carries
KiCad's justification swap with it.

**Why it is needed at all.** There are two different
keep-upright functions in KiCad and they are not the same transform:

* `PCB_TEXT::GetDrawRotation` (`pcb_text.cpp:204-223`) folds a footprint text's
  stored angle modulo 180 into `(-90, 90]` AT DRAW TIME. That fold is what the
  plot path uses - `attrs.m_Angle = aText->GetDrawRotation()`
  (`plot_brditems_plotter.cpp:766`) - so it is the ink the user reviews;
* `PCB_TEXT::KeepUpright` (`pcb_text.cpp:374-392`) REWRITES the stored angle
  after a turn, folding only the `[180, 360)` half back and swapping both
  justifications. `FOOTPRINT::Rotate` calls it (`footprint.cpp:2899-2919`), so
  it is what the editor does.

The stored angle is therefore NOT pre-resolved in real files: a KiCad-10-written
fixture stores 270 where the ink is at 90 (`qa/data/pcbnew/
api_kitchen_sink.kicad_pcb:883-884`). A copier that turns the STORED angle puts
that reference designator 180 degrees out on every panel copy.

And with the flag left on, the drawn angle can only ever lie in `(-90, 90]`, so
any other target angle is UNREACHABLE - the text would be laid out differently
depending on the copy's orientation.

**The recipe**, applied to every footprint-hosted text and property of a copy
where keep-upright
is in effect, for the turn `A` that copy takes:

1. run KiCad's own turn on the stored angle and read off the ink it leaves:
   `normalize(angle + A)`, then `KeepUpright`'s half circle when that is at or
   past 180, then the draw-time fold into `(-90, 90]`. That is the target drawn
   angle, `T` (:func:`upright_state`);
2. leave the position alone - a footprint-local `at` already rides the
   placement (`pcb_io_kicad_sexpr.cpp:2272-2273`);
3. set `unlocked` - clear keep-upright - on the copy. MANDATORY, not stylistic:
   without it the draw-time fold puts the ink back where the source had it;
4. SWAP `justify` on both axes exactly when KiCad's `KeepUpright` swapped it in
   step 1, and never otherwise (:func:`swap_justification`). Leaving `justify`
   alone would be safe for a rigid rotation, because the glyph pipeline mirrors
   about the anchor and only then rotates (`glyph.cpp:74-111`), so
   `ink(D + A) = R_A(ink(D))` exactly. But a panel copy has to show the ink
   KiCad ITSELF would draw after the same turn, not a rigid rotation of the
   source ink;
5. shed the `render_cache`.

The turn then follows, and `eedesigncore`'s `rotate_footprint` writes
`normalize(angle + A)` and skips its own `keep_upright` because the flag is
already clear (`kfio:common/pcb_family/transform.py:259-260`). The angle stored
by step 1 is therefore `T - A`, PRE-COMPENSATED for the turn that puts it back:
this stage runs on the content before the copy is injected, and `T` is what has
to be in the file afterwards.

Nothing else on a copy needs any of this. A board-level text, a text box and a
dimension's text carry no keep-upright token at all (see the note on
`upright_children`), so the plain turn already leaves them where KiCad would;
and the panel's own footprints - tooling holes and fiducials - are written at
their final positions and never turned.

**This is POLICY and it belongs here.** `eedesigncore` faithfully ports the
editor's behaviour - turn, then rewrite - which is correct as a port and not
what a panel copy needs. It has no `GetDrawRotation` port to borrow. Doing the
fold in this stage is not a workaround for a library defect; it is a decision
about what a PANEL copy should be.

`eedesigncore` exports `keep_upright` - the editor's rewrite - and neither
`draw_angle` nor `resolve_keep_upright`, so :func:`draw_angle` below is the only
port of `GetDrawRotation` either side has. When the library gains that
primitive, this module calls it and keeps its own fold as the fence that checks
the two agree; `tests/test_library_016.py` is what fails the day that becomes
possible.

**What it costs**, both checked against the KiCad source:

* no DRC penalty - the library-parity provider ignores all text items
  ("Currently we punt and ignore all the text items",
  `drc_test_provider_library_parity.cpp:908-917`), so `unlocked yes` on a
  panel's footprints does not produce "footprint differs from library";
* the panel's copies lose upright-on-rotate IN THE EDITOR. If an engineer later
  turns a copy inside the panel, its text turns with it instead of staying
  readable. Acceptable for a generated artifact, and the page says so.

**Only a copy that TURNS is resolved.** A copy at 0 degrees is the copy this
stage built before any of this existed, to the byte, and its ink is already
right - so nothing is folded and no flag is written. That keeps one invariant
worth having: a panel that turns nothing is byte-identical to the panel built
before the placement suite landed.

**ONE RESOLVED VIEW PER TURN ANGLE.** The resolution depends on
the turn, so a panel whose copies take 90 and 180 degrees resolves the content
twice. The caller caches by angle
(:func:`eedesignpanels.stages.step_and_repeat.stepped_build`); the count of items
it touches is the same for every angle, because it is the same set of texts.
"""

from typing import NamedTuple

from eedesigncore import Board, JustifyH, JustifyV, new_board_from
from eedesigncore.common.kimath import normalize_angle
from eedesigncore.common.pcb_family.shapes import PcbText

# `PCB_TEXT::KeepUpright`'s justification swap (pcb_text.cpp:386-391): the two
# axes swap ends, which is what keeps the ink in one physical place while the
# glyphs turn upright. Center is absent from both maps because it has no ends.
_FLIPPED_H = {JustifyH.left: JustifyH.right, JustifyH.right: JustifyH.left}
_FLIPPED_V = {JustifyV.top: JustifyV.bottom, JustifyV.bottom: JustifyV.top}

# Which children carry keep-upright in the FILE. Only
# footprint-hosted text does: a board-level `gr_text` never gets the token - the
# writer gates it on `parentFP` (writer 2295) and the parser THROWS on it
# (parser 3890-3894) - a `PCB_TEXTBOX` is not serialised with one at all, and a
# dimension's text can never be written with one (writer 2264-2265 forces the
# parent null). So a board text and a text box are already rigid and there is
# nothing here to do to them.
#
# A footprint's `properties` are `PCB_FIELD`s, which derive from `PCB_TEXT`
# (`pcb_field.h:33`), so they carry it exactly as an `fp_text` does.


def draw_angle(angle: float) -> float:
    """`PCB_TEXT::GetDrawRotation`'s fold for keep-upright text (pcb_text.cpp:208-215).

    ```cpp
    while( rotation > ANGLE_90 )   rotation -= ANGLE_180;
    while( rotation <= -ANGLE_90 ) rotation += ANGLE_180;
    ```

    The result lies in `(-90, 90]`, which is the whole window a keep-upright
    text can ever be drawn in. It is deliberately NOT normalised to `[0, 360)`
    afterwards: the turn that follows does that (`Rotation.turn`), and
    normalising here would undo the fold for every angle over 90.
    """
    rotation = float(angle)
    while rotation > 90.0:
        rotation -= 180.0
    while rotation <= -90.0:
        rotation += 180.0
    return rotation


def keeps_upright(item) -> bool:
    """Is keep-upright in effect on this text or field?

    `unlocked` is the stored INVERSE: the token absent means keep-upright is on,
    which is why the default here is False.
    """
    return not bool(getattr(item, "unlocked", False))


def upright_state(angle: float, turn: float) -> tuple[float, bool]:
    """KiCad's own ink for a keep-upright text at *angle* turned by *turn* degrees.

    Returns `(drawn_angle, swapped)`: the angle KiCad would DRAW the text at
    after the turn, and whether `KeepUpright` swapped both justifications on the
    way there. Three source steps in order, and the order is the whole point:

    1. `FOOTPRINT::Rotate` turns the stored angle and normalises it to
       `[0, 360)` (`footprint.cpp:2899-2919`, `pcb_text.cpp:445-454`);
    2. `PCB_TEXT::KeepUpright` (`pcb_text.cpp:374-392`) adds a half circle when
       that landed at or past 180 and SWAPS both justifications;
    3. `PCB_TEXT::GetDrawRotation` (`pcb_text.cpp:208-215`) folds what is left
       into `(-90, 90]` at draw time, and does NOT touch justification.

    Step 3 can move the ink and step 2 cannot: the swap keeps the glyphs in the
    same physical place, the bare fold turns them over about the anchor. Both
    are reproduced because both are in the picture the user reviews.
    """
    turned = normalize_angle(float(angle) + float(turn))
    swapped = turned >= 180.0
    if swapped:
        turned = normalize_angle(turned + 180.0)
    return draw_angle(turned), swapped


def swap_justification(item) -> bool:
    """Swap both of one text's justifications, as `KeepUpright` does. In place.

    A text with no `(effects (justify ...))` block is centred on both axes, and
    centre has no ends to swap - so no block is written where the source had
    none, which is also the fence `eedesigncore`'s own port carries
    (`kfio:common/pcb_family/transform.py:265-269`). Returns whether it swapped.
    """
    attributes = getattr(item, "attributes", None)
    if attributes is None:
        return False
    horizontal = _FLIPPED_H.get(attributes.justify_h, attributes.justify_h)
    vertical = _FLIPPED_V.get(attributes.justify_v, attributes.justify_v)
    changed = (horizontal, vertical) != (attributes.justify_h, attributes.justify_v)
    attributes.justify_h, attributes.justify_v = horizontal, vertical
    return changed


def resolve_item(item, turn: float) -> bool:
    """Put one text or field where KiCad's own *turn* would draw it. In place.

    Returns whether anything changed - an already-unlocked text is rigid, KiCad
    holds nothing upright about it, and it is left exactly as it is, so this is
    safe to call on every child of every footprint.

    The angle written is the target MINUS the turn: the caller injects this copy
    with a turn of *turn* degrees still to come, and `rotate_footprint` adds it
    back (module docstring, step 1).
    """
    if not keeps_upright(item):
        return False
    # NO TURN, NOTHING TO RESOLVE. KiCad reaches `KeepUpright`
    # through a ROTATE and through nothing else, so an unturned copy's ink is
    # already the source's ink - and the copy stays byte-identical, which is the
    # invariant the module docstring keeps. The caller skips these copies; this
    # is the fence that says so where it can be read.
    if float(turn) % 360.0 == 0.0:
        return False
    drawn, swapped = upright_state(getattr(item, "angle", 0.0), turn)
    item.angle = normalize_angle(drawn - float(turn))
    item.unlocked = True
    if swapped:
        swap_justification(item)
    # THE TRAP. The cache holds absolute BOARD coordinates
    # even inside a footprint (`formatRenderCache` passes no parent footprint,
    # writer 539) and KiCad's staleness check looks at font, text, angle, offset
    # and mirror - never at POSITION (`eda_text.cpp:683-686`). A copy that kept
    # its cache would draw its glyphs at the SOURCE's position and KiCad would
    # not notice. Dropping the block is safe: the parser only calls
    # `SetupRenderCache` when the token is present (parser 862-909) and
    # otherwise the cache is regenerated from the font at draw time.
    if getattr(item, "render_cache", None) is not None:
        item.render_cache = None
    return True


class Resolved(NamedTuple):
    """One resolved copy of the content, and what resolving it touched.

    `turn_deg` is the turn it was resolved FOR. A view is only
    valid for that one angle, so it is carried with the board rather than left
    for a caller to remember.
    """

    board: Board
    turn_deg: float
    footprints: int
    items: int

    @property
    def changed(self) -> bool:
        return bool(self.items)


def upright_children(footprint) -> list:
    """Every child of *footprint* that carries keep-upright in the file.

    Its properties, and the `PcbText` items among its drawings. A footprint text
    BOX is not one: its flag is never serialised, and its stored angle is
    footprint-relative and takes no change on a turn anyway.
    """
    return [
        *footprint.properties,
        *[item for item in footprint.drawings if isinstance(item, PcbText)],
    ]


def resolve_footprint(footprint, turn: float) -> int:
    """Resolve every keep-upright child of one footprint. Returns how many."""
    return sum(
        1 for child in upright_children(footprint) if resolve_item(child, turn)
    )


def resolved_content(content: Board, turn_deg: float) -> Resolved:
    """*content* with every keep-upright text resolved FOR A TURN OF *turn_deg*.

    On a COPY of *content*, and valid for that turn only: the
    justification swap and the fold both depend on where the text ends up, so a
    view resolved for 180 degrees is the wrong view for 90.

    The source view is not modified: only the footprints that actually carry
    keep-upright text are deep-copied, and every other stream is carried by
    reference - `inject_board` deep-copies everything it injects, so a shared
    item is never mutated by the injection either.

    Handing the whole board back rather than a list keeps the caller's shape:
    :func:`eedesignpanels.stages.step_and_repeat.stepped_build` passes one
    content board per turn to `inject_board`, and a resolved one is just
    another such board.
    """
    view = new_board_from(content)
    view.embedded_files = content.embedded_files
    for name in ("drawings", "trailing_drawings", "points", "tracks", "zones",
                 "targets", "groups", "generators"):
        setattr(view, name, list(getattr(content, name)))

    footprints, touched, items = [], 0, 0
    for footprint in content.footprints:
        if not any(keeps_upright(child)
                   for child in upright_children(footprint)):
            footprints.append(footprint)
            continue
        copy = footprint.model_copy(deep=True)
        items += resolve_footprint(copy, turn_deg)
        touched += 1
        footprints.append(copy)
    view.footprints = footprints
    return Resolved(board=view, turn_deg=float(turn_deg),
                    footprints=touched, items=items)
