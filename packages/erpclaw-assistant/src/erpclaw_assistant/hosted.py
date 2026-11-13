"""What a hosted model may see: the guards every hosted route goes through.

  - books: a hosted model meets only demo or eval books (the marks only their own setup writes), and never runs
    with the data root of the real books of earlier versions
  - credentials: a request is scanned before it is sent; a credential-shaped value refuses the call, and only the
    shape's label is kept, never the value
  - the key: read at run time from its keychain item (the command's output, never an argument), else from its
    environment variable; never stored, logged or shown
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from erpclaw_core.common import Refusal
# Keep the assistant's public guard names and diagnostic helpers backed by the core checker.
from erpclaw_core.credentials import (
    CREDENTIAL_FIELD, CREDENTIAL_NAMES, CREDENTIAL_REFUSED, CREDENTIAL_SHAPES,
    JSON_DEPTH, JSON_MISSES, JSON_START, UNREADABLE, _Unreadable, _check_json_depth,
    _fields, _held_json, _texts, check, scan, scan_text, scrub,
)
from erpclaw_core.db import DEMO_MARK, EVAL_MARK, owner, read_only

SEEDED_ONLY = "seeded_only"
BOOKS_POLICIES = (SEEDED_ONLY,)  # "owner_consented" comes with the owner's own consent card, not before
REAL_HOME = Path.home() / ".openclaw" / "erpclaw"


class HostedRefused(ValueError):
    """A hosted model cannot run here; the message says why. Nothing was sent."""


BOOKS_REFUSED = ("This model runs on a hosted service, so it only works with demo or eval books. These books are not "
                 "marked as either; nothing was sent and nothing was saved.")


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
    """Before storage on every route: refuse a credential-shaped message and record only its label.
    None when the message may go on."""
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
