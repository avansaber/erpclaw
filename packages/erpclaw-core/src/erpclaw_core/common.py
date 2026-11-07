"""Shared context, clock, ids, refusals and the audit helper."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import models as m


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


def today() -> date:
    """Today, pinnable for tests and evals (ERPCLAW_TODAY=YYYY-MM-DD)."""
    pinned = os.environ.get("ERPCLAW_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


QUESTION_SUFFIXES = ("_unknown", "_ambiguous", "_missing")


def is_question(code: str) -> bool:
    """The one rule for which refusals are the engine's questions (the turn's and the approval's alike): a code that
    ends in _unknown, _ambiguous or _missing, or is one of those words."""
    return code.endswith(QUESTION_SUFFIXES) or code in ("unknown", "ambiguous", "missing")


PUBLIC_CHOICE_KEYS = ("code", "label")


def public_data(data: dict) -> dict:
    """A refusal's data as an event shows it: each of a question's choices by its code and label only, never its
    ref (the canonical reference stays inside the engine: in the stored outcome and the conversation note)."""
    out = dict(data)
    if isinstance(out.get("choices"), list):
        out["choices"] = [{k: c[k] for k in PUBLIC_CHOICE_KEYS if k in c} if isinstance(c, dict) else c
                          for c in out["choices"]]
    return out


# A refusal of something ERPClaw does not offer names the capability it refused, by its catalogue id: what is
# switched off until a release (planned.*), what is never offered (unsupported.*), or what is in a plugin these books
# have not installed (available.*). It travels on the event that leaves the engine as its `capability` field.
NOT_OFFERED_CODES = ("planned", "unsupported", "available")
CAPABILITY = re.compile(r"^(planned|unsupported|available)\.[a-z0-9_]+(\.[a-z0-9_]+)*$")


def check_capability(capability: str | None) -> str:
    """A capability id as the engine sends it, or ValueError: a refusal of something not offered always names one."""
    if not isinstance(capability, str) or not CAPABILITY.match(capability):
        raise ValueError(f"a refusal of something ERPClaw does not offer needs its catalogue id, not {capability!r}")
    return capability


def not_offered(event: dict) -> bool:
    """Whether an event refuses something ERPClaw does not offer: a catalogue outcome b, c or d, or a refusal coded
    as not offered."""
    return event.get("outcome") in ("b", "c", "d") or event.get("code") in NOT_OFFERED_CODES


def check_events(events: list[dict]) -> list[dict]:
    """The events as they leave the engine, or ValueError: every refusal of something not offered names its
    catalogue id, and every id an event names is one."""
    for e in events:
        if not_offered(e) or "capability" in e:
            check_capability(e.get("capability"))
    return events


class Refusal(Exception):
    """A known refusal: nothing was written. `message` is owner-facing wording; `options` are the choices a question
    offers, in the owner's words (for instance the matching customers), empty unless the refusal is a question.
    `capability` is the catalogue id of what was refused when it is not offered; a refusal coded as not offered
    cannot be made without it."""

    def __init__(self, message: str, code: str = "refused", options: list[str] | None = None,
                 capability: str | None = None, **data):
        super().__init__(message)
        self.message = message
        self.code = code
        self.options = list(options or [])
        self.capability = check_capability(capability) if (capability or code in NOT_OFFERED_CODES) else None
        self.data = data


@dataclass
class Ctx:
    conn: Connection
    company_id: str
    company_name: str
    currency: str
    today: date
    actor: str = "owner"
    extra: dict = field(default_factory=dict)  # "commit": the approval id, set only for an approved card's commit
    checks: dict[str, Callable[["Ctx"], None]] = field(default_factory=dict)
    # the company's date order: "MDY", "DMY", or None when unset (then a figure date is asked); last, so every
    # construction by position stays as it was
    date_order: str | None = None

    def register_check(self, key: str, fn: Callable[["Ctx"], None]) -> None:
        """Register a check the approved commit runs after the contract's commit returns and before the commit ends,
        in the same transaction; a Refusal from it rolls the whole effect back. One per key: a second registration
        under the same key is a no-op. Checks run in registration order. A check reads; it does not write."""
        from .approval_scope import _can_register
        if not _can_register(self):
            raise Refusal("A check can only be registered inside an approved commit.", "service_outside_commit")
        self.checks.setdefault(key, fn)
        # Observational only: retain original callable, de-duplication and refusal.
        from .approval_scope import _capture_check
        _capture_check(self, key, self.checks[key])


def company_ctx(conn: Connection, actor: str = "owner", company_name: str | None = None,
                company_id: str | None = None) -> Ctx:
    q = select(m.company)
    if company_name:
        q = q.where(m.company.c.name == company_name)
    if company_id:
        q = q.where(m.company.c.id == company_id)
    rows = conn.execute(q).mappings().all()
    if not rows:
        raise Refusal("No company is set up yet. Run `erpclaw init` first.", "no_company")
    if len(rows) > 1 and not company_name:
        raise Refusal("More than one company exists; say which one.", "company_ambiguous")
    c = rows[0]
    return Ctx(conn, c["id"], c["name"], c["functional_currency"], today(), actor, date_order=c["date_order"])


def audit(ctx: Ctx, action: str, document_type: str | None = None, document_id: str | None = None,
          approval_id: str | None = None, detail: dict | None = None) -> None:
    ctx.conn.execute(
        m.audit_log.insert().values(
            id=new_id(), company_id=ctx.company_id, at=now(), actor=ctx.actor, action=action,
            document_type=document_type, document_id=document_id, approval_id=approval_id,
            detail=json.dumps(detail, default=str, sort_keys=True) if detail else None,
        )
    )
