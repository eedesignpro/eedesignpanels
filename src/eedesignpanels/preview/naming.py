"""What the UI is allowed to call a layer, and the fence that keeps it honest.

**No bare `User.##` reaches the page.** A designer reads
layer names off KiCad's Appearance panel, and KiCad shows the board's own
display name there - `User.38-VScore`, not `User.38`. The stage's messages,
subtitles and warnings are written in canonical names because that is what the
file and the code use, so the served page translates them on the way out.

Two halves, and both are needed:

* :func:`humanize` rewrites a sentence, using the mapping the BOARD declares.
  Nothing is hardcoded - a board that calls `User.36` something else gets its
  own word - and a canonical name the board gives no display name to is left
  exactly as it is, because inventing one would be worse than showing it.
* :func:`violations` reads finished markup back and reports every bare name
  still in it. That is the fence: the rule is checked on the bytes that are
  served, not on the intention of the code that built them.

A display name that CONTAINS the canonical one - `User.38-VScore` - is not a
violation, and the pattern says so: a bare name is `User.` plus digits with no
name character behind it.

**The same fence carries the RETIRED WORDS.** "Sheet" names the KiCad drawing
sheet and nothing else; the material standing round the boards is the RAIL, and
the boards plus their rails are the PANEL. One word for one thing, checked on the bytes that are served rather than on the
intention of the code that built them - which is the only version of that rule
that survives a refactor.
"""

import re

# `User.38` is bare. `User.38-VScore` is the board's own display name and is
# exactly what the rule asks for, so the lookahead lets it through.
BARE_LAYER = re.compile(r"User\.\d+(?![-\w])")

# Words no page may show, and why. Whole words, case-insensitive: `stylesheet`
# is a CSS word in a style block and is never text on the page, but the fence
# reads text nodes only, so it never sees one anyway.
RETIRED_WORDS = {
    "sheet": (
        'a "sheet" is the KiCad drawing sheet. The material round the boards '
        "is the RAIL, and the boards plus their rails are the PANEL"
    ),
}
_RETIRED = re.compile(
    r"\b(" + "|".join(sorted(RETIRED_WORDS)) + r")\b", re.I
)

# Markup this module reads back: tags, and the two attributes a browser shows
# to a reader as text.
_TAG = re.compile(r"<[^>]*>", re.S)
_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_SHOWN_ATTRS = re.compile(r"\b(title|placeholder|aria-label)=(\"[^\"]*\"|'[^']*')")

# The state fields whose strings become text on the page. Everything else the
# state carries is a machine key - a canonical layer name the SVG stack is
# keyed by - and naming those in display terms would break the stack.
SHOWN_STATE_KEYS = (
    "subtitle", "warnings", "message", "status", "scoreLayerName",
    "label", "summary", "reason", "verdict", "displayId",
)


def layer_names(layers) -> dict:
    """`{canonical name: display name}` for every layer a board declares.

    Built from the board's own `(layers ...)` block, never from a hardcoded
    layer table, because a board may rename or add layers: the fourth field
    when it is there, the canonical name when it is not.
    """
    names = {}
    for layer in layers:
        canonical = getattr(layer, "canonical_name", None)
        if not canonical:
            continue
        display = getattr(layer, "display_name", None) or canonical
        names[canonical] = display
    return names


def humanize(text: str, names: dict) -> str:
    """One sentence with every bare `User.##` replaced by its display name.

    A name the board declares no display name for is left alone: the reader is
    better served by a name that exists than by one this module made up.
    """
    if not text or not names:
        return text

    def swap(found):
        return names.get(found.group(0), found.group(0))

    return BARE_LAYER.sub(swap, text)


def humanized(items, names) -> list:
    """:func:`humanize` over a sequence of sentences."""
    return [humanize(text, names) for text in items]


def shown_text(markup: str) -> str:
    """Everything in *markup* a reader actually reads.

    Script and style bodies go first - a canonical name in a layer table or a
    CSS selector is a machine key and not a word on the page - then the tags
    themselves, leaving the text nodes; and the two attributes a browser turns
    back into text are added, because a tooltip reading `User.38 - user` is the
    UI showing a bare name whatever the DOM calls it.
    """
    body = _BLOCK.sub(" ", markup or "")
    attrs = " ".join(match.group(2)[1:-1] for match in _SHOWN_ATTRS.finditer(body))
    return f"{_TAG.sub(' ', body)} {attrs}"


def _collect(node, out: list, shown: bool = False) -> None:
    """Gather the strings under a SHOWN key. A list inherits its key's answer.

    So `{"warnings": ["..."]}` is read and `{"layers": ["User.38"]}` is not: the
    first is sentences, the second is the machine keys the SVG stack is keyed by.
    """
    if isinstance(node, str):
        if shown:
            out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            _collect(value, out, key in SHOWN_STATE_KEYS)
    elif isinstance(node, list):
        for entry in node:
            _collect(entry, out, shown)


def state_strings(state: dict) -> list:
    """Every string in a page state object that becomes text on the page."""
    out = []
    _collect(state, out)
    return out


def violations(markup: str, state: dict = None) -> list:
    """Every bare `User.##` a reader would see. Empty is the rule being kept.

    *state* is the JSON the page's script draws its controls from; its display
    strings are checked too, because they are not in the markup as text and are
    exactly where a stage's own wording would otherwise slip through.
    """
    found = list(BARE_LAYER.findall(shown_text(markup)))
    for text in state_strings(state or {}):
        found.extend(BARE_LAYER.findall(text))
    return sorted(set(found))


def retired_words(markup: str, state: dict = None) -> list:
    """Every RETIRED word a reader would see. Empty is the rule being kept.

    The other half of the fence, read off the same served bytes and the same
    display strings: the rule is about what the user READS, so it is checked
    where the user reads it.
    """
    found = [
        match.group(1).lower() for match in _RETIRED.finditer(shown_text(markup))
    ]
    for text in state_strings(state or {}):
        found.extend(match.group(1).lower() for match in _RETIRED.finditer(text))
    return sorted(set(found))
