"""One credential check before owner text is stored and before every hosted request.

Only shape labels may be retained from a refused owner message, never its other words.
"""
from __future__ import annotations

import json
import re

from .common import Refusal

CREDENTIAL_NAMES = r"password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key|authorization"
# label -> shape; a label names what was found, so a refusal can be recorded without the value
CREDENTIAL_SHAPES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[0-9a-f]{32,}")),
    ("openai_key", re.compile(r"\bsk-(?!or-v1-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z2-7]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+\S{16,}")),  # "the bearer of this invoice" is ordinary words
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("database_url_password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@[^\s/]+")),
    ("password_assignment",  # a credential's name, its key quoted or not (escaped too), then ':' or '=', then any
     # value at all: password = 'a, b', "api_key": "x", {\"token\": 1}. The value's characters are never read, so no
     # quoting, escape or punctuation can slip one through; a name with no ':' or '=' after it is ordinary words
     re.compile(r"(?i)(?<![A-Za-z0-9])(?:\\?['\"])?(?:" + CREDENTIAL_NAMES + r")(?:\\?['\"])?\s*[:=]\s*(?=\S)")),
]
CREDENTIAL_FIELD = re.compile(r"(?i)(?:" + CREDENTIAL_NAMES + r")")  # a decoded field named as a credential


CREDENTIAL_REFUSED = ("That message holds something shaped like a password or key, so it was not sent to the hosted "
                      "model. Nothing was saved.")


JSON_START = re.compile(r"[\[{]")
JSON_MISSES = 64  # starts that hold no JSON, per text; past it the text is refused, never read only in part
JSON_DEPTH = 256  # bound recursive decoding independently of the interpreter's native stack limit
UNREADABLE = "unreadable_json"  # the label of a text the check cannot read whole in bounded time


class _Unreadable(Exception):
    """A text past the check's failed-start or nesting budget."""


def _check_json_depth(text: str, at: int) -> None:
    """Bound a candidate's nesting before the C decoder can allocate a deep tree or exhaust its native stack."""
    depth, quoted, escaped = 0, False, False
    for i in range(at, len(text)):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > JSON_DEPTH:
                raise _Unreadable
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return


def _held_json(text: str):
    """Every JSON object or array a text holds ("use this: {...}"), with bounded nesting and failed starts."""
    decoder, at, misses = json.JSONDecoder(), 0, 0
    while (start := JSON_START.search(text, at)) is not None:
        # Otherwise 3.14 may retry a full-depth parse 65 times on a large-stack runner,
        # while the same input stops at the first RecursionError on a smaller stack.
        _check_json_depth(text, start.start())
        try:
            held, end = decoder.raw_decode(text, start.start())
        except ValueError:
            misses += 1
            if misses > JSON_MISSES:
                raise _Unreadable from None
            at = start.start() + 1
            continue
        yield held
        at = end


def _texts(value):
    """Every string in a value as it reads, never JSON-escaped, and every string inside the JSON a string holds."""
    if isinstance(value, str):
        yield value
        for held in _held_json(value):
            yield from _texts(held)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _texts(k)
            yield from _texts(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _texts(v)


def _fields(value):
    """Every (name, value) pair of every object in a value, the JSON a string holds included."""
    if isinstance(value, str):
        for held in _held_json(value):
            yield from _fields(held)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _fields(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _fields(v)


def scan_text(text: str) -> str | None:
    """The label of the first credential shape in one text as written, or None. Only `check` decides a refusal."""
    return next((label for label, shape in CREDENTIAL_SHAPES if shape.search(text)), None)


def check(value) -> str | None:
    """The one credential check, made before the owner's message is stored and before every hosted request: the label
    of the first credential in a text or a request, or None. It reads every string as written and every string inside
    the JSON one holds, and a field named as a credential holding any value, whatever its characters."""
    try:
        found = next((label for text in _texts(value) if (label := scan_text(text))), None)
        # a field's value, never a schema: a tool may name a parameter "token"
        named = any(isinstance(k, str) and CREDENTIAL_FIELD.fullmatch(k) and isinstance(v, (str, int, float))
                    and str(v).strip() for k, v in _fields(value))
    except (_Unreadable, RecursionError):  # never read in part, never raised: a text it cannot read whole is refused
        return UNREADABLE
    return found or ("password_assignment" if named else None)


def scan(messages: list[dict], tools: list[dict]) -> str | None:
    """The label of the first credential anywhere in the request, or None."""
    return check([messages, tools])


WITHHELD = "(message withheld: it held a credential)"


def scrub(text: str) -> str:
    """Keep only unique shape labels and a fixed sentence when any match is found.

    The whole message is withheld: pattern matches need not span a private-key body,
    an assignment's value, or the surrounding owner words to protect all of them.
    """
    label = check(text)
    if label is None:
        return text
    labels = {label}
    if label != UNREADABLE:
        try:
            for held in _texts(text):
                labels.update(name for name, shape in CREDENTIAL_SHAPES if shape.search(held))
            if any(isinstance(k, str) and CREDENTIAL_FIELD.fullmatch(k) and isinstance(v, (str, int, float))
                   and str(v).strip() for k, v in _fields(text)):
                labels.add("password_assignment")
        except (_Unreadable, RecursionError):
            labels = {UNREADABLE}
    return " ".join(sorted(labels)) + " " + WITHHELD


def refuse(value) -> None:
    """Refuse before a write, using only fixed text and never retaining the matched value."""
    if check(value):
        raise Refusal(CREDENTIAL_REFUSED, "credential_refused")
