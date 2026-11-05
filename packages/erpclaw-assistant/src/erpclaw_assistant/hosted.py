"""What a hosted model may see: the guards every hosted route goes through.

  - books: a hosted model meets only demo or eval books (the marks only their own setup writes), and never runs
    with the data root of the real books of earlier versions
  - credentials: a request is scanned before it is sent; a credential-shaped value refuses the call, and only the
    shape's label is kept, never the value
  - the key: read at run time from its keychain item (the command's output, never an argument), else from its
    environment variable; never stored, logged or shown
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from erpclaw_core.common import Refusal
from erpclaw_core.db import DEMO_MARK, EVAL_MARK, owner, read_only

SEEDED_ONLY = "seeded_only"
BOOKS_POLICIES = (SEEDED_ONLY,)  # "owner_consented" comes with the owner's own consent card, not before
REAL_HOME = Path.home() / ".openclaw" / "erpclaw"

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


class HostedRefused(ValueError):
    """A hosted model cannot run here; the message says why. Nothing was sent."""


BOOKS_REFUSED = ("This model runs on a hosted service, so it only works with demo or eval books. These books are not "
                 "marked as either; nothing was sent and nothing was saved.")
CREDENTIAL_REFUSED = ("That message holds something shaped like a password or key, so it was not sent to the hosted "
                      "model. Nothing was saved.")


def refuse_real_home() -> None:
    """A hosted model never runs with the data root of the real books of earlier versions."""
    home = os.environ.get("ERPCLAW_HOME")
    if not home:
        return
    try:
        Path(home).expanduser().resolve().relative_to(REAL_HOME.resolve())
    except ValueError:
        return
    raise HostedRefused(f"ERPCLAW_HOME is {REAL_HOME} or inside it, where real books live; a hosted model does not "
                        "run there. Nothing was sent.")


def check_policy(policy: str) -> str:
    if policy == "owner_consented":
        raise HostedRefused("Using a hosted model with your own books is not available yet.")
    if policy not in BOOKS_POLICIES:
        raise HostedRefused(f"hosted_books must be one of {', '.join(BOOKS_POLICIES)}, not {policy!r}.")
    return policy


def books_seeded(engine: Engine) -> bool:
    """Whether the books carry a demo or eval mark, which only their own setup writes (the runtime role reads it)."""
    with read_only(engine) as conn:
        return conn.execute(select(owner.c.mark).where(owner.c.mark.in_((DEMO_MARK, EVAL_MARK)))).first() is not None


def books_allowed(engine: Engine, model) -> bool:
    """A model that sends nothing out meets any books; a hosted one meets only what its books policy allows."""
    if not getattr(model, "hosted", False):
        return True
    check_policy(getattr(model, "hosted_books", SEEDED_ONLY))
    return books_seeded(engine)


JSON_START = re.compile(r"[\[{]")
JSON_MISSES = 64  # starts that hold no JSON, per text; past it the text is refused, never read only in part
UNREADABLE = "unreadable_json"  # the label of a text the check cannot read whole in bounded time


class _Unreadable(Exception):
    """A text with more brace starts that hold no JSON than the check reads, or nested past what it can decode."""


def _held_json(text: str):
    """Every JSON object or array a text holds, wherever in it it starts ("use this: {...}"), found in one pass."""
    decoder, at, misses = json.JSONDecoder(), 0, 0
    while (start := JSON_START.search(text, at)) is not None:
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


def _record(usage: list[dict], label: str) -> None:
    usage.append({"refused": label, "native_calls": 0, "reasoning_tokens": None, "prompt_tokens": None,
                  "completion_tokens": None, "cost": None, "served_by": None})


def refuse_credentials(usage: list[dict], messages: list[dict], tools: list[dict]) -> None:
    """Before a hosted request: a credential-shaped value refuses the call; its label is recorded, never the value."""
    label = scan(messages, tools)
    if label:
        _record(usage, label)
        raise Refusal(CREDENTIAL_REFUSED, "credential_refused")


def refused_input(model, text: str) -> dict | None:
    """Before the owner's message is stored: on a hosted model, a credential-shaped value refuses the turn, so the
    value is never saved nor sent; its label is recorded, never the value. None when the message may go on."""
    if not getattr(model, "hosted", False):
        return None
    label = check(text)  # the request's own check: the two can never disagree
    if not label:
        return None
    if isinstance(getattr(model, "usage", None), list):
        _record(model.usage, label)
    return {"type": "refused", "code": "credential_refused", "text": CREDENTIAL_REFUSED}


def keychain(item: str, run: Callable = subprocess.run) -> str | None:
    """The key from a keychain item, read from the command's output; the key is never an argument."""
    try:
        out = run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True, text=True,
                  timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    key = (out.stdout or "").strip() if out.returncode == 0 else ""
    return key or None
