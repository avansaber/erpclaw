"""Government-ID shape detection for free-text fields (defense-in-depth).

Runtime companion to the release-time source scanner. Two entry points:

  scan_value(v)  -> list of neutral shape-kind labels found in v (never the
                    matched substring itself). Drives a non-blocking caution
                    on write actions so a careless paste is surfaced without
                    blocking legitimate numeric business text.
  mask_value(v)  -> v with any shaped substring reduced to its last four
                    characters. Display-layer masking on read actions; the
                    value stored at rest is never changed by this module.

No literal example identifiers appear anywhere in this file. Labels are
purely shape-descriptive so the catalog carries no domain vocabulary.
"""
from __future__ import annotations

import re

# Shape set mirrors release/scripts/security_audit.py:123-139 (pattern source
# of record) — the identity/account-number subset only. Labels are neutral.
GOVID_PATTERNS = [
    ("nine_digit_dashed", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("letter_prefixed_id", re.compile(r"\bA[0-9]{8,9}\b")),
    ("prefixed_ten_digit", re.compile(r"\b(?:EAC|WAC|LIN|SRC|MSC|IOE|NBC|YSC)[0-9]{10}\b")),
    ("letter_prefixed_ten_digit", re.compile(r"\bN[0-9]{10}\b")),
    ("mixed_alnum_fifteen", re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d][A-Z]\d\b")),
    ("intl_bank_account", re.compile(r"\b[A-Z]{2}\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{2}\b")),
]

# Plain-English, non-blocking caution added to a write response when a shaped
# substring is seen. Never carries the matched value.
CAUTION_MESSAGE = (
    "this looks like it may contain a government ID number; the books store "
    "it in plain text — consider keeping ID numbers out of notes"
)


def scan_text(value):
    """Return the neutral shape-kind labels matched in a single string.

    Returns [] for non-strings or empty strings. Never returns the matched
    substring itself — only the shape labels.
    """
    if not isinstance(value, str) or not value:
        return []
    return [kind for kind, pat in GOVID_PATTERNS if pat.search(value)]


def scan_value(value):
    """Recurse into dict / list / str and return the sorted set of shape kinds.

    Used for the parsed JSON blob args so a shaped value nested inside an
    emergency-contact or bank-details object is still surfaced.
    """
    found = set()
    _collect(value, found)
    return sorted(found)


def _collect(value, found):
    if isinstance(value, str):
        found.update(scan_text(value))
    elif isinstance(value, dict):
        for v in value.values():
            _collect(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, found)


def caution_for(*values):
    """Return CAUTION_MESSAGE if any of the given values contains a shaped
    substring, else None. Values may be strings or already-parsed JSON."""
    for v in values:
        if scan_value(v):
            return CAUTION_MESSAGE
    return None


def _mask_match(m):
    s = m.group(0)
    if len(s) <= 4:
        return s
    head, tail = s[:-4], s[-4:]
    masked_head = "".join("*" if ch.isalnum() else ch for ch in head)
    return masked_head + tail


def mask_text(value):
    """Replace any shaped substring in a string with a last-4 masked form,
    preserving separators (e.g. a nine-digit-dashed value becomes ***-**-1234).
    Non-strings pass through unchanged."""
    if not isinstance(value, str) or not value:
        return value
    out = value
    for _kind, pat in GOVID_PATTERNS:
        out = pat.sub(_mask_match, out)
    return out


def mask_value(value):
    """Recurse into dict / list / str applying mask_text to every string.
    Other scalar types pass through unchanged."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(v) for v in value]
    return value
