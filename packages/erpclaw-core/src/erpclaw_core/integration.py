"""The integration kernel: working alongside other systems.

Each company has a system of record per area, native by default. When its
accounting is external, every posting is a sub-ledger posting, and an
outbound journal is staged in the same transaction for a connector to send;
nothing is sent in this version. External ids and account codes live in one
map table each; exchanges are deduplicated per company and per record in the
sync journal, and a changed record never overwrites a confirmed one.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import models as m
from .common import Ctx, Refusal, new_id, now


def system_of_record(ctx: Ctx, area: str) -> tuple[str, str | None]:
    """(mode, external system) for an area; native when nothing is set."""
    if area not in m.AREAS:
        raise Refusal(f"There is no area called {area}.", "no_area")
    row = ctx.conn.execute(select(m.system_of_record).where(
        m.system_of_record.c.company_id == ctx.company_id, m.system_of_record.c.area == area)).mappings().first()
    return ("native", None) if row is None else (row["mode"], row["external_system"])


def set_system_of_record(ctx: Ctx, area: str, mode: str, external_system: str | None = None) -> None:
    """For accounting, take the chain head first and refuse once any posting
    exists, so a setter and a first posting serialize."""
    from .ledger import _lock_head

    if area not in m.AREAS:
        raise Refusal(f"There is no area called {area}.", "no_area")
    if mode not in ("native", "external") or (mode == "external") != bool(external_system):
        raise Refusal("An external system of record names the system; a native one names none.", "bad_mode")
    t = m.system_of_record
    if area == "accounting":
        _lock_head(ctx)
        if ctx.conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == ctx.company_id).limit(1)).first():
            raise Refusal("Accounting's system of record is set before the first posting; this company already "
                          "has postings, and moving it is a migration, not a setting.", "accounting_has_postings")
    ctx.conn.execute(t.delete().where(t.c.company_id == ctx.company_id, t.c.area == area))
    ctx.conn.execute(t.insert().values(company_id=ctx.company_id, area=area, mode=mode,
                                       external_system=external_system if mode == "external" else None,
                                       set_at=now(), set_by=ctx.actor))


def map_account(ctx: Ctx, external_system: str, account_id: str, external_code: str) -> None:
    ctx.conn.execute(m.account_map.insert().values(id=new_id(), company_id=ctx.company_id,
                                                   external_system=external_system, account_id=account_id,
                                                   external_code=external_code))


def account_codes(ctx: Ctx, external_system: str, account_ids: list[str]) -> dict[str, str]:
    """External codes for the given accounts; refuses, naming them, when any is unmapped."""
    rows = dict(ctx.conn.execute(select(m.account_map.c.account_id, m.account_map.c.external_code).where(
        m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external_system,
        m.account_map.c.account_id.in_(account_ids))).all())
    missing = [a for a in dict.fromkeys(account_ids) if a not in rows]
    if missing:
        names = ctx.conn.execute(select(m.account.c.number, m.account.c.name).where(
            m.account.c.id.in_(missing)).order_by(m.account.c.number)).all()
        listed = ", ".join(f"{n} {name}" for n, name in names)
        raise Refusal(f"Account {listed} has no {external_system} account mapped yet." if len(names) == 1 else
                      f"Accounts {listed} have no {external_system} accounts mapped yet.", "unmapped_account")
    return rows


def _exact(value):
    """Money is exact: a binary float anywhere in a payload is
    refused, and a Decimal travels as its decimal text."""
    if isinstance(value, float):
        raise Refusal("A payload carries a binary floating-point number; amounts travel as exact decimal text.",
                      "float_in_payload")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: _exact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_exact(v) for v in value]
    return value


def _canonical(body: dict) -> tuple[str, str]:
    text = json.dumps(_exact(body), sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


def _payload_exists(ctx: Ctx, digest: str) -> bool:
    return ctx.conn.execute(select(m.sync_payload.c.digest).where(
        m.sync_payload.c.company_id == ctx.company_id, m.sync_payload.c.digest == digest)).first() is not None


def _store_payload(ctx: Ctx, body: dict) -> str:
    """Write a payload once per company; the same body again is a no-op, also
    when another transaction writes it at the same moment."""
    text, digest = _canonical(body)
    if not _payload_exists(ctx, digest):
        try:
            with ctx.conn.begin_nested():
                ctx.conn.execute(m.sync_payload.insert().values(company_id=ctx.company_id, digest=digest, body=text))
        except IntegrityError:
            pass  # written concurrently: the same body, so the same row
    return digest


def stage_outbound_posting(ctx: Ctx, external_system: str, posting_id: str, record_key: str, body: dict) -> str:
    """Stage the journal for a sub-ledger posting, in the posting's transaction."""
    digest = _store_payload(ctx, body)
    jid = new_id()
    ctx.conn.execute(m.sync_journal.insert().values(
        id=jid, company_id=ctx.company_id, external_system=external_system, direction="out", object_type="posting",
        local_id=posting_id, external_id=None, operation_key=record_key,
        record_identity=f"{posting_id}:{record_key}", payload_digest=digest, status="staged", at=now()))
    return jid


def stage_inbound(ctx: Ctx, external_system: str, object_type: str, external_id: str | None, body: dict) -> dict:
    """Stage an inbound record. Returns the journal row it
    landed in: a new staged row, the existing row for the same record and
    payload, or a new conflict row when a confirmed version differs."""
    if not external_id:
        raise Refusal("An inbound record without an external id cannot be identified, so it is not taken.",
                      "unidentifiable")
    from .ledger import _lock_head

    _lock_head(ctx)  # every writer of a company's books takes its chain head first
    digest = _store_payload(ctx, body)
    j = m.sync_journal
    same = (j.c.company_id == ctx.company_id, j.c.external_system == external_system, j.c.direction == "in",
            j.c.object_type == object_type, j.c.record_identity == external_id)
    existing = ctx.conn.execute(select(j).where(*same, j.c.payload_digest == digest)).mappings().first()
    if existing:
        return dict(existing)
    confirmed = ctx.conn.execute(select(j.c.id).where(*same, j.c.status == "confirmed").limit(1)).first()
    row = dict(id=new_id(), company_id=ctx.company_id, external_system=external_system, direction="in",
               object_type=object_type, local_id=None, external_id=external_id, operation_key=None,
               record_identity=external_id, payload_digest=digest,
               status="conflict" if confirmed else "staged", at=now())
    try:
        with ctx.conn.begin_nested():
            ctx.conn.execute(j.insert().values(**row))
    except IntegrityError:  # the same exchange staged concurrently: it is one exchange
        return dict(ctx.conn.execute(select(j).where(*same, j.c.payload_digest == digest)).mappings().one())
    return row


def payload(ctx: Ctx, digest: str) -> dict:
    text = ctx.conn.execute(select(m.sync_payload.c.body).where(
        m.sync_payload.c.company_id == ctx.company_id, m.sync_payload.c.digest == digest)).scalar_one()
    return json.loads(text)


def link_external(ctx: Ctx, external_system: str, object_type: str, local_id: str, external_id: str) -> None:
    ctx.conn.execute(m.external_ref.insert().values(id=new_id(), company_id=ctx.company_id,
                                                    external_system=external_system, object_type=object_type,
                                                    local_id=local_id, external_id=external_id, last_synced_at=now()))
