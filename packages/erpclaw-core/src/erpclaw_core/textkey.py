"""The key by which owner text is compared without regard to case.

The engine computes it in Python, stores it beside the text (a party's city has its city_key) and compares keys by
equality, the owner's words folded the same way. SQL never folds the case of owner text: under the books' builtin
C.UTF-8 locale PostgreSQL's case rules are not Python's (its lower() turns "İ" into "i", Python's into "i" and a
combining dot), and no lower() makes "Straße" meet "STRASSE".

The key is the text in Unicode's compatibility form (NFKC), so a composed or decomposed letter, a full-width letter
or a ligature is the same text, without surrounding whitespace, then fully case folded (str.casefold, Unicode's
default folding, not a language's): "ß" folds to "ss"; the dotted capital "İ" to "i" with a combining dot above, so it
does not meet a plain "i"; and the dotless "ı" stays as it is.
"""
from __future__ import annotations

import unicodedata


def text_key(text: str | None) -> str | None:
    """The comparison key of `text`; no text has no key."""
    if text is None:
        return None
    return unicodedata.normalize("NFKC", text).strip().casefold()
