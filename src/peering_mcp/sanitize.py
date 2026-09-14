"""Cleaning the free text that has to pass through.

Most upstream text never reaches this module, because it never gets past
`models/upstream.py`: a field nobody declared cannot survive parsing, so
`notes` and `aka` simply do not exist on this side of the boundary. That
allowlist is the strong defence. This module handles the remainder — names,
and the handful of other strings an interconnection decision actually turns on,
which are written by whoever owns the record and cannot be dropped.

**What this module can and cannot do, stated plainly, because the difference
matters.** It removes *structure*: characters that let a value stop looking
like a value and start looking like a section heading, a tag, a code fence or a
new turn in a conversation. It cannot remove *meaning*. A network that names
itself "Ignore all previous instructions" still says that after cleaning, and
no character filter will fix it. That case is covered by the other three
defences in the design — dropping the fields nobody needs, keeping upstream
text out of prose the model might read as instruction, and saying in every tool
description that this text is data. A denylist of suspicious phrases would add
nothing but false confidence and broken names.

**The rules are calibrated against real data, not guessed.** Measured over
2,310 names and long names from every exchange in PeeringDB on 2026-09-14:

* **No real name contains a control, format or private-use character.**
  Stripping them costs nothing and removes the bidi-override trick, where text
  renders in a different order from the one it is read in.
* **Unicode normalisation changes 2 names**, both a fullwidth tilde becoming an
  ordinary one. It is what stops the fullwidth forms of the angle brackets
  walking past a filter that only knows the ASCII ones. (Writing that example
  out here trips the lint rule against confusable characters, which is the same
  point from the other side.)
* **The structural characters below appear in 7 names of 2,310**, and all seven
  stay readable: `Nuernberger Internet Exchange [N-IX.de]` loses its brackets
  and nothing else.
* **The longest real name is 78 characters**, so the 200-character cap is not a
  constraint on legitimate data. It exists for the pathological case.

Pure functions, no I/O. Applied at the boundary in `models/upstream.py` rather
than in shaping, so that no future tool can be written that forgets to call it.
"""

from __future__ import annotations

import re
import unicodedata

#: Longest value that leaves the server. Real names run to 78 characters;
#: anything near this is either a mistake or a payload.
DEFAULT_MAX_LENGTH = 200

#: Marks a value that was cut, because a silently truncated name is a wrong
#: value presented as a whole one.
TRUNCATION_MARK = "…"

#: Characters replaced by a space. Each one lets text stop reading as a value:
#: angle brackets and pipes build tags and chat-turn markers, backticks build
#: code fences, square and curly brackets build `[INST]` and template holes,
#: asterisks and hashes build emphasis and headings, backslashes build escapes,
#: and double quotes end a quoted string.
#:
#: Replaced with a space rather than deleted, on purpose, and the same goes
#: for every character this module removes. Deleting would let `IG<>NORE`
#: become a word that was never visibly written; a space can only ever break a
#: token apart, never assemble one.
STRUCTURAL_CHARACTERS = frozenset('<>[]{}|`*#\\"')

#: Left alone deliberately, each because real names need it: `-` (766 names),
#: `.` (228), `()` (168), `,` (30), `/` (9), `'` (8), `&` (8), `:` (3) and `_`.
#: Their worst case is cosmetic markdown; the cost of removing them is not.

_DISCARDED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_WHITESPACE = re.compile(r"\s+")


def clean(value: str, *, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Return `value` with its structure removed. May return an empty string.

    Empty means nothing survived — a value that was only punctuation, or only
    invisible characters. The caller treats that as absent rather than as a
    name, which is why `models/upstream.py` refuses a record whose identity
    cleans away to nothing.
    """
    normalised = unicodedata.normalize("NFKC", value)

    # Everything removed becomes a space, never nothing. Deleting would splice
    # the text either side together, and a zero-width space between two halves
    # of a word is the cheapest way there is to smuggle one past a reader.
    kept = [
        " "
        if character in STRUCTURAL_CHARACTERS
        or unicodedata.category(character) in _DISCARDED_CATEGORIES
        else character
        for character in normalised
    ]

    # One pass folds newlines, tabs, non-breaking spaces and the gaps left
    # above into single spaces, so no value can span what looks like a line.
    collapsed = _WHITESPACE.sub(" ", "".join(kept)).strip()

    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK


def clean_optional(value: object, *, max_length: int = DEFAULT_MAX_LENGTH) -> str | None:
    """Clean anything that might be a string. Non-strings and blanks are None.

    PeeringDB uses `""` and `null` interchangeably for "not filled in", and the
    difference is never meaningful.
    """
    if not isinstance(value, str):
        return None
    return clean(value, max_length=max_length) or None
