"""Validation and the owner-approved sender settings write."""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, field_validator
from sqlalchemy import select

from erpclaw_core import approvals, models as m, principals
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Card, Input, Preview, Result, WriteContract
from erpclaw_core.outbox import ReadCtx, SenderSnapshot

from . import smtp, tables

NAME = "update_email_sender"
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_LOCAL = re.compile(r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+)*")


def _hostname(value: str) -> str:
    if not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError("a host must be a hostname")
    try:
        host = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("a host must be a hostname") from None
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError("a host must be a hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("a host must be a hostname")


def _address(value: str) -> str:
    if not value.isprintable() or value.count("@") != 1:
        raise ValueError("give one plain email address")
    local, domain = value.rsplit("@", 1)
    if not _LOCAL.fullmatch(local) or len(local) > 64:
        raise ValueError("give one plain email address")
    # Store SMTP's IDNA form so core also builds an ASCII Message-ID.
    address = local + "@" + _hostname(domain)
    # IDNA can expand the input beyond the frozen outbox header columns.
    if len(address) > 200:
        raise ValueError("the email address is too long")
    return address


class UpdateEmailSender(Input):
    # Reject CR/LF before any whitespace normalization could hide them.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    # Match core's frozen outbox header columns before approving settings.
    from_address: str = Field(min_length=3, max_length=200)
    display_name: str = Field(max_length=200)
    smtp_host: str = Field(min_length=1, max_length=253)
    port: int = Field(strict=True, ge=1, le=65535)
    security: Literal["starttls", "tls"]
    username: str = Field(min_length=1, max_length=200)
    reply_to: str | None = Field(default=None, max_length=200)

    @field_validator("from_address", "reply_to")
    @classmethod
    def address(cls, value):
        return None if value is None else _address(value)

    @field_validator("smtp_host")
    @classmethod
    def hostname(cls, value):
        return _hostname(value)

    @field_validator("display_name", "username")
    @classmethod
    def one_line(cls, value):
        if value and not value.isprintable():
            raise ValueError("use one line without control characters")
        return value

    def takes_no_date(self, ctx):
        return True


def sender_row(ctx: Ctx) -> dict | None:
    settings = ReadCtx(ctx.conn.engine, ctx.company_id, "email")
    try:
        return settings.one("email_sender")
    finally:
        settings.close()


def message_sender(ctx: Ctx) -> SenderSnapshot:
    row = sender_row(ctx)
    if row is None:
        raise Refusal("No email account is set up for this company.", "no_email_sender")
    return SenderSnapshot(row["from_address"], row["display_name"], row["reply_to"],
                          ("email_sender", row["id"]))


def preview_sender(ctx: Ctx, inp: UpdateEmailSender) -> Preview:
    # Read through the same company-scoped read interface as the connector.
    sender_row(ctx)
    card = Card("Update email sender", [
        ("From address", inp.from_address), ("Display name", inp.display_name),
        ("SMTP host", inp.smtp_host), ("Port", str(inp.port)), ("Security", inp.security),
        ("Username", inp.username), ("Reply-To", inp.reply_to or "Not set"),
        ("Password", "set" if smtp.company_password(ctx.company_id) else "not set"),
    ], [], currency=ctx.currency)
    return Preview(card, approvals.digest(card.as_dict()))


def sender_read_set(ctx: Ctx, inp: UpdateEmailSender) -> list[tuple[str, str]]:
    # A stable company-derived key binds both absence and an existing row.
    return [("company", ctx.company_id), ("email_sender", ctx.company_id)]


def commit_sender(ctx: Ctx, inp: UpdateEmailSender, key: str) -> Result:
    aid = approvals.committing(ctx)
    card = ctx.conn.execute(select(m.approval).where(
        m.approval.c.id == aid, m.approval.c.company_id == ctx.company_id,
        m.approval.c.contract == NAME, m.approval.c.operation_key == key,
        m.approval.c.status == "consumed",
    )).mappings().first() if aid else None
    if card is None:
        raise Refusal("Sender settings need an approved settings card.", "not_approved")
    # There is no sender-settings permission in PERMISSIONS. Require the actual
    # owner as both drafter and approver, never ctx.actor or a caller-supplied ID.
    owner = principals.owner(ctx.conn)
    if owner["disabled"] or card["principal_id"] != owner["id"] or card["approved_by"] != owner["id"]:
        raise Refusal("Only the owner may change email sender settings.", "forbidden")
    values = UpdateEmailSender.model_validate(inp.model_dump()).model_dump()
    # Approval holds the company's read-set/chain locks, including when no
    # sender exists yet. Name each updated column so the runtime-policy audit
    # can derive exactly the privileges this write needs.
    changed = ctx.conn.execute(tables.email_sender.update().where(
        tables.email_sender.c.id == ctx.company_id,
        tables.email_sender.c.company_id == ctx.company_id,
    ).values(from_address=values["from_address"], display_name=values["display_name"],
             smtp_host=values["smtp_host"], port=values["port"], security=values["security"],
             username=values["username"], reply_to=values["reply_to"]))
    if changed.rowcount == 0:
        ctx.conn.execute(tables.email_sender.insert().values(
            id=ctx.company_id, company_id=ctx.company_id, **values))
    # Core stores this result under its minted key in this same transaction.
    return Result("Email sender settings updated.", {"company_id": ctx.company_id})


def find_sender(ctx: Ctx, key: str) -> Result | None:
    o, a = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(o.c.result_json).join(a, a.c.id == o.c.approval_id).where(
        o.c.company_id == ctx.company_id, o.c.key == key, o.c.status == "committed", a.c.contract == NAME
    )).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}))


UPDATE_EMAIL_SENDER = WriteContract(
    NAME, "email", "write", "Update the company's email sender settings, with the owner's approval.",
    UpdateEmailSender, preview=preview_sender, commit=commit_sender, find_by_operation_key=find_sender,
    read_set=sender_read_set, plain="update email sender settings", synonyms=("email settings", "sender settings"),
    moves_money=False, no_date_reason={"en": "Changing email settings posts nothing to the books."},
)
