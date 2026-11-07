"""Outbox: a message is written in the business transaction and sent after commit, at most once unless the owner
sends it again. One owner of a send at a time, decided under the outbox row's lock; a body is sent only after its
data mark has committed; nothing is accepted without that mark; no plugin writes a status."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from threading import Condition
from typing import Callable

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine

from . import files, ledger, models as m
from .common import Ctx, Refusal, company_ctx, new_id
from .db import DEMO_MARK, EVAL_MARK, owner, read_only
from .plugins import REGISTRY, EmptySlot, enabled_plugins

# Delivering messages is off in this release, with emailing an invoice (planned for the core release); it comes
# back on through its own specification and review. Until then nothing is delivered, and nothing is queued.
PLANNED = "Core"

LEASE = dt.timedelta(minutes=5)
BACKOFF = (dt.timedelta(minutes=1), dt.timedelta(minutes=5), dt.timedelta(minutes=30), dt.timedelta(hours=2),
           dt.timedelta(hours=6))
REASONS = {  # core words every reason; a connector never supplies owner text
    "password_missing": "the mail account's password is not set up",
    "password_refused": "the mail account's password was refused",
    "certificate_unverified": "the mail server's certificate could not be verified",
    "tls_unavailable": "the mail server does not offer a secure connection",
    "unreachable": "the mail server could not be reached since {first}",
    "recipient_refused": "the mail server refused the recipient address ({code})",
    "message_refused": "the mail server refused the message ({code})",
    "attachment_unavailable": "the attached file could not be read",
}
FOLDER_ONLY = ("This connector writes messages to a folder for the demo and the evals; set up an email account to "
               "send.")
FOLDER_DONE = "written to the mail folder"
UNMARKED = "the connector reported a send it did not mark"


def clock() -> dt.datetime:
    """The time every step reads; a test replaces it with a fake clock."""
    return dt.datetime.now(dt.timezone.utc)


def fault(point: str) -> None:
    """Called at after_claim, after_data_mark, after_answer and after_result; a test replaces it to crash or pause."""


@dataclass(frozen=True)
class SenderSnapshot:
    from_address: str
    from_name: str
    reply_to: str | None
    read_entry: tuple | None  # (table, key) of the sender's settings row, as a card's read set records rows


@dataclass(frozen=True)
class Attachment:
    """A stored file to attach, as the caller's card approved it: the file, its name, and the approved sha256."""
    file_id: str
    name: str
    sha256: str


@dataclass(frozen=True)
class OutboundAttachment:
    """An attachment as the connector receives it at send time, read through the file store's check."""
    name: str
    media_type: str
    sha256: str
    data: bytes


@dataclass(frozen=True)
class OutboundMessage:
    id: str
    company_id: str
    message_id: str
    recipient: str
    from_address: str | None
    from_name: str | None
    reply_to: str | None
    subject: str
    body: str
    attachments: tuple = ()  # of OutboundAttachment


@dataclass(frozen=True)
class SendOutcome:
    stage: str  # before_data or after_data
    kind: str  # accepted, temporary, permanent, no_answer, lost_ownership
    code: int | None
    text: str
    reason: str | None = None


class ReadRefused(Exception):
    """A connector read outside its own tables, or after its settings context closed."""


IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class ReadCtx:
    """A read-only view of the calling plugin's own tables for one company, in its own short transaction, never
    inside the outbox row's lock; closed at the data mark or when the connector returns."""

    def __init__(self, engine: Engine, company_id: str, plugin: str):
        self.company_id, self._plugin = company_id, plugin
        self._tables = set(REGISTRY.plugins[plugin].manifest.owned_tables) if plugin in REGISTRY.plugins else set()
        self._conn: Connection | None = engine.connect()
        self._conn.begin()

    def _select(self, table: str, where: dict) -> list[dict]:
        if self._conn is None:
            raise ReadRefused("the settings context is closed")
        if table not in self._tables or not IDENT.match(table):
            raise ReadRefused(f"{table} is not a table of plugin {self._plugin}")
        cols = list(where)
        if any(not IDENT.match(c) for c in cols):
            raise ReadRefused("a read names columns by plain names only")
        clause = " AND ".join(["company_id = :_company", *(f"{c} = :{c}" for c in cols)])
        rows = self._conn.execute(text(f'SELECT * FROM "{table}" WHERE {clause}'),
                                  {"_company": self.company_id, **where}).mappings().all()
        return [dict(r) for r in rows]

    def one(self, table: str, **where) -> dict | None:
        rows = self._select(table, where)
        return rows[0] if rows else None

    def all(self, table: str, **where) -> list[dict]:
        return self._select(table, where)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.rollback()
            self._conn.close()
            self._conn = None


# --- queueing --------------------------------------------------------------------------------------------------------

ADDRESS = re.compile(r"^[^@\s,;:<>\"()\[\]\\]+@([^@\s,;:<>\"()\[\]\\]+)$")


def _header(value: str | None, what: str) -> None:
    if value is not None and ("\r" in value or "\n" in value):
        raise Refusal(f"The {what} holds a line break, which a message header cannot.", "header_line_break")


def _one_address(value: str | None, what: str) -> str:
    """One plain address, its domain encodable for mail; returns the domain."""
    match = ADDRESS.match(value or "")
    if not match or not value.isprintable():
        raise Refusal(f"The {what} must be one plain email address.", "not_one_address")
    try:
        match.group(1).encode("idna")
    except UnicodeError:
        raise Refusal(f"The {what}'s domain cannot be used for email.", "address_domain") from None
    return match.group(1)


def _attachment_name(name) -> None:
    """A name a header can carry as a file name: 1 to 200 characters, no control character, no path separator."""
    if (not isinstance(name, str) or not 1 <= len(name) <= 200 or "/" in name or "\\" in name
            or any(unicodedata.category(c) == "Cc" for c in name)):
        raise Refusal("An attachment's name is 1 to 200 characters, with no line break, control character, slash or "
                      "backslash.", "attachment_name")


def _attachment_rows(ctx: Ctx, attachments: tuple) -> list[dict]:
    """Each attachment's row values, refusing another company's file and any sha256 but the approved one."""
    rows = []
    for a in attachments:
        if not isinstance(a, Attachment):
            raise Refusal("An attachment names a stored file.", "attachment_not_found")
        _attachment_name(a.name)
        try:
            stored = files.row(ctx, a.file_id)
        except Refusal:
            raise Refusal("The attached file is not one of this company's stored files.",
                          "attachment_not_found") from None
        if stored["sha256"] != a.sha256:
            raise Refusal("The attached file is not the one that was approved.", "attachment_not_approved")
        rows.append({"name": a.name, "media_type": stored["media_type"], "sha256": a.sha256, "file_id": a.file_id})
    return rows


def enqueue(ctx: Ctx, slot: str, recipient: str, subject: str, body: str, operation_key: str | None, *,
            sender: SenderSnapshot | None = None, attachments: tuple = ()) -> str:
    """Queue the approved message, refusing a header that could inject, anything but one plain recipient, and an
    attachment that is not the caller's approved stored file."""
    for value, what in ((recipient, "recipient"), (subject, "subject")):
        _header(value, what)
    domain = _one_address(recipient, "recipient")
    if sender is not None:
        for value, what in ((sender.from_address, "sender address"), (sender.from_name, "sender name"),
                            (sender.reply_to, "reply-to address")):
            _header(value, what)
        domain = _one_address(sender.from_address, "sender address")
        if sender.reply_to:
            _one_address(sender.reply_to, "reply-to address")
    parts = _attachment_rows(ctx, tuple(attachments))
    oid = new_id()
    ctx.conn.execute(m.outbox.insert().values(
        id=oid, company_id=ctx.company_id, operation_key=operation_key, slot=slot, recipient=recipient,
        subject=subject, body=body, status="queued", created_at=clock(), message_id=f"<{oid}@{domain}>",
        from_address=sender.from_address if sender else None, from_name=sender.from_name if sender else None,
        reply_to=sender.reply_to if sender else None, tries=0))
    for part in parts:
        ctx.conn.execute(m.outbox_attachment.insert().values(id=new_id(), outbox_id=oid, **part))
    return oid


def slot_available(conn: Connection, slot: str) -> bool:
    """Whether a plugin enabled in these books fills the slot."""
    try:
        REGISTRY.slot_filler(slot, enabled_plugins(conn))
    except EmptySlot:
        return False
    return True


def sender(ctx: Ctx) -> SenderSnapshot:
    """The company's email sender, from whichever plugin fills message_sender in these books; without one, only
    emailing is refused."""
    try:
        _, fn = REGISTRY.slot_filler("message_sender", enabled_plugins(ctx.conn))
    except EmptySlot:
        raise Refusal("No email account is set up for this company.", "no_email_sender") from None
    return fn(ctx)


# --- the four locked steps -------------------------------------------------------------------------------------------

def _locked(conn: Connection, oid: str) -> dict | None:
    r = conn.execute(ledger.for_update(None, select(m.outbox).where(m.outbox.c.id == oid))).mappings().first()
    return dict(r) if r else None


def _attempt(conn: Connection, aid: str) -> dict | None:
    r = conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.id == aid)).mappings().first()
    return dict(r) if r else None


def claim(engine: Engine, oid: str) -> str | None:
    """A queued row that is due becomes sending, with a new attempt as its one owner; None when it is not claimable."""
    now = clock()
    with engine.begin() as conn:
        r = _locked(conn, oid)
        if r is None or r["status"] != "queued" or (r["next_try_at"] is not None and r["next_try_at"] > now):
            return None
        aid = new_id()
        conn.execute(m.outbox_attempt.insert().values(id=aid, outbox_id=oid, started_at=now, lease_until=now + LEASE))
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status="sending", current_attempt_id=aid))
    return aid


def mark_data(engine: Engine, oid: str, aid: str) -> bool:
    """The data mark: True only after it has committed, for the row's current, open, unmarked attempt."""
    with engine.begin() as conn:
        r = _locked(conn, oid)
        a = _attempt(conn, aid)
        if (r is None or a is None or r["status"] != "sending" or r["current_attempt_id"] != aid
                or a["finished_at"] is not None or a["data_at"] is not None):
            return False
        conn.execute(m.outbox_attempt.update().where(m.outbox_attempt.c.id == aid).values(data_at=clock()))
    return True


def _reason_words(outcome: SendOutcome, first: dt.datetime | None) -> str:
    words = REASONS.get(outcome.reason or "")
    if words:
        return words.format(code=outcome.code, first=_when(first))
    return f"the mail server answered {outcome.code}" if outcome.code else "the connector gave no answer"


def record_result(engine: Engine, oid: str, aid: str, outcome: SendOutcome,
                  accepted_detail: str | None = None) -> None:
    """The result, mapped by core: the row moves only while this attempt is its current one. `accepted_detail`, when
    given, is the row's detail if it is accepted (the folder adapter's marker), written with the result."""
    now = clock()
    with engine.begin() as conn:
        r = _locked(conn, oid)
        a = _attempt(conn, aid)
        if r is None or a is None or a["finished_at"] is not None:
            return
        kind = outcome.kind
        unmarked = kind == "accepted" and a["data_at"] is None
        result = "permanent" if unmarked else kind
        conn.execute(m.outbox_attempt.update().where(m.outbox_attempt.c.id == aid).values(
            finished_at=now, code=outcome.code, result=result, text=(outcome.text or "")[:200]))
        if r["current_attempt_id"] != aid or kind == "lost_ownership":
            return  # a replaced attempt closes alone; a lost one leaves the row to its owner
        row = m.outbox.c.id == oid  # each write is spelled out, so the runtime grants check reads its columns
        if unmarked:
            conn.execute(m.outbox.update().where(row).values(status="failed", detail=UNMARKED))
        elif kind == "accepted":
            conn.execute(m.outbox.update().where(row).values(status="accepted", accepted_at=now,
                                                            detail=accepted_detail or outcome.text[:200] or None))
        elif kind == "no_answer":
            conn.execute(m.outbox.update().where(row).values(status="unknown"))
        elif kind == "temporary":
            tries, first = (r["tries"] or 0) + 1, r["first_failed_at"] or now
            if tries > len(BACKOFF):
                conn.execute(m.outbox.update().where(row).values(
                    status="failed", tries=tries, first_failed_at=first, current_attempt_id=None,
                    detail=REASONS["unreachable"].format(first=_when(first))))
            else:
                conn.execute(m.outbox.update().where(row).values(
                    status="queued", tries=tries, first_failed_at=first, current_attempt_id=None,
                    next_try_at=now + BACKOFF[tries - 1]))
        else:  # permanent
            conn.execute(m.outbox.update().where(row).values(status="failed",
                                                            detail=_reason_words(outcome, r["first_failed_at"])))


def recover(engine: Engine) -> None:
    """A sending row whose lease passed: unmarked, it is requeued and its attempt abandoned; marked, it is unknown
    and keeps its attempt (only that attempt's own late answer can change it)."""
    now = clock()
    with engine.connect() as conn:
        ids = conn.execute(select(m.outbox.c.id).where(m.outbox.c.status == "sending")).scalars().all()
    for oid in ids:
        with engine.begin() as conn:
            r = _locked(conn, oid)
            a = _attempt(conn, r["current_attempt_id"]) if r and r["current_attempt_id"] else None
            if r is None or r["status"] != "sending" or a is None or a["lease_until"] > now:
                continue
            if a["data_at"] is None:
                conn.execute(m.outbox_attempt.update().where(m.outbox_attempt.c.id == a["id"]).values(
                    finished_at=now, result="abandoned"))
                conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status="queued",
                                                                                  current_attempt_id=None))
            else:
                conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status="unknown"))


def send_again(ctx: Ctx, oid: str) -> None:
    """The owner's choice, through a card's commit: an unknown or failed message is queued again, same Message-ID."""
    if PLANNED:
        from .contracts import planned_text

        raise Refusal(planned_text("sending a message again", PLANNED), "planned", capability="planned.messaging")
    r = _locked(ctx.conn, oid)
    if r is None or r["company_id"] != ctx.company_id:
        raise Refusal("There is no such message.", "no_message")
    if r["status"] not in ("unknown", "failed"):
        raise Refusal("Only a message whose sending is unknown, or that failed, can be sent again.", "not_resendable")
    ctx.conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(
        status="queued", current_attempt_id=None, tries=0, next_try_at=None, first_failed_at=None))


# --- delivering ------------------------------------------------------------------------------------------------------

class AttachmentUnavailable(Exception):
    """An attached file could not be read through the file store's check."""


def _read_attachments(engine: Engine, r: dict) -> tuple:
    """The message's attachments, each read with the file store's own hash check; AttachmentUnavailable if any is
    missing or altered."""
    with read_only(engine) as conn:
        parts = conn.execute(select(m.outbox_attachment).where(m.outbox_attachment.c.outbox_id == r["id"])
                             .order_by(m.outbox_attachment.c.name, m.outbox_attachment.c.id)).mappings().all()
        if not parts:
            return ()
        ctx = company_ctx(conn, company_id=r["company_id"])
        out = []
        for p in parts:
            try:
                data = files.get(ctx, p["file_id"])
            except Refusal:
                raise AttachmentUnavailable(p["name"]) from None
            out.append(OutboundAttachment(name=p["name"], media_type=p["media_type"], sha256=p["sha256"], data=data))
    return tuple(out)


UNAVAILABLE = SendOutcome("before_data", "permanent", None, "", reason="attachment_unavailable")


def _message(r: dict, attachments: tuple = ()) -> OutboundMessage:
    return OutboundMessage(id=r["id"], company_id=r["company_id"], message_id=r["message_id"] or f"<{r['id']}>",
                           recipient=r["recipient"], from_address=r["from_address"], from_name=r["from_name"],
                           reply_to=r["reply_to"], subject=r["subject"], body=r["body"], attachments=attachments)


def _marked_for_folder(conn: Connection) -> bool:
    return bool(conn.execute(select(owner.c.mark).where(owner.c.mark.in_((DEMO_MARK, EVAL_MARK)))).first())


class _MarkScope:
    """One connector invocation's mark; not approval authority or a SQL sandbox.

    The condition linearizes admission and closing, never database work. Local
    draining does not bound DB completion or make concurrent settings reads safe.
    """

    def __init__(self, engine: Engine, oid: str, aid: str, settings: ReadCtx):
        self._engine, self._oid, self._aid, self._settings = engine, oid, aid, settings
        self._condition = Condition()
        self._phase = "live"
        self._reserved = self._inflight = self._marked = self._faulted = False

    def __call__(self) -> bool:
        with self._condition:
            if self._phase != "live" or self._reserved:
                return False
            self._reserved = self._inflight = True
        try:
            self._settings.close()  # failure propagates; final close retries it
            try:
                ok = mark_data(self._engine, self._oid, self._aid)
            except Exception:  # preserve existing failed-mark behavior
                return False
            if ok:
                with self._condition:
                    self._marked = True
                try:
                    fault("after_data_mark")
                except BaseException:
                    with self._condition:
                        self._faulted = True
                    raise
            return ok
        finally:
            with self._condition:
                self._inflight = False
                self._condition.notify_all()

    def finish(self) -> tuple[bool, bool]:
        """Revoke on every connector exit; a cleanup error never records a result."""
        interrupted = None
        with self._condition:
            self._phase = "closing"
            while self._inflight:
                try:
                    self._condition.wait()  # releases mutex while waiting
                except BaseException as exc:
                    if interrupted is None:
                        interrupted = (exc, exc.__traceback__)
                    # Keep admission closed and drain even after interruption.
        try:
            self._settings.close()  # preserve existing finally/repeated-close behavior
        except BaseException as exc:
            if interrupted is not None:
                raise exc from interrupted[0]
            raise
        finally:
            with self._condition:
                self._phase = "revoked"
        if interrupted is not None:
            raise interrupted[0].with_traceback(interrupted[1])
        with self._condition:
            return self._marked, self._faulted


def _send(engine: Engine, r: dict, plugin: str, fn: Callable) -> None:
    aid = claim(engine, r["id"])
    if aid is None:
        return
    fault("after_claim")
    try:
        attachments = _read_attachments(engine, r)
    except AttachmentUnavailable:  # fails before the connector is called: nothing connects
        record_result(engine, r["id"], aid, UNAVAILABLE)
        return
    settings = ReadCtx(engine, r["company_id"], plugin)
    mark = _MarkScope(engine, r["id"], aid, settings)
    cleaned = False
    try:
        try:
            outcome = fn(settings, _message(r, attachments), mark)
        finally:
            marked, faulted = mark.finish()
            cleaned = True
    except Exception as e:
        if not cleaned or faulted:
            raise  # cleanup and controlled faults retain traceback/chaining
        outcome = SendOutcome("after_data" if marked else "before_data", "no_answer" if marked else "temporary",
                              None, type(e).__name__)  # class only: exception text may contain a secret
    if not isinstance(outcome, SendOutcome):
        outcome = SendOutcome("after_data" if marked else "before_data", "no_answer" if marked else "temporary",
                              None, "the connector returned no outcome")
    fault("after_answer")
    record_result(engine, r["id"], aid, outcome)
    fault("after_result")


def _adapt(engine: Engine, r: dict, fn: Callable) -> None:
    """The folder connector, through the same steps: claim, mark, call, result (demo and eval books only)."""
    aid = claim(engine, r["id"])
    if aid is None:
        return
    fault("after_claim")
    try:
        _read_attachments(engine, r)
    except AttachmentUnavailable:
        record_result(engine, r["id"], aid, UNAVAILABLE)
        return
    if not mark_data(engine, r["id"], aid):
        return
    fault("after_data_mark")
    try:
        detail = fn(r)
        outcome = SendOutcome("after_data", "accepted", None, str(detail))
    except Exception as e:
        outcome = SendOutcome("after_data", "no_answer", None, type(e).__name__)
    fault("after_answer")
    # the file it wrote on the attempt, the marker on the row, in the result's one transaction
    record_result(engine, r["id"], aid, outcome, accepted_detail=FOLDER_DONE)
    fault("after_result")


def deliver_pending(engine: Engine) -> list[dict]:
    """Recover first, then send each due queued message through the connector these books have now: the enabled
    send_message filler, else the folder connector in demo or eval books; otherwise it stays queued with the reason."""
    if PLANNED:
        return []
    recover(engine)
    now = clock()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(m.outbox).where(m.outbox.c.status == "queued")).mappings()
                if r["next_try_at"] is None or r["next_try_at"] <= now]
        enabled = enabled_plugins(conn)
        folder_ok = _marked_for_folder(conn)
    results = []
    for r in rows:
        try:
            plugin, fn = REGISTRY.slot_filler("send_message", enabled)
            _send(engine, r, plugin, fn)
        except EmptySlot:
            try:
                _, fn = REGISTRY.slot_filler("deliver_message", enabled)
            except EmptySlot as e:
                _set_detail(engine, r["id"], str(e))
                results.append(_result(engine, r["id"]))
                continue
            if not folder_ok:
                _set_detail(engine, r["id"], FOLDER_ONLY)
            else:
                _adapt(engine, r, fn)
        results.append(_result(engine, r["id"]))
    return results


def _set_detail(engine: Engine, oid: str, detail: str) -> None:
    with engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.id == oid, m.outbox.c.status == "queued").values(
            detail=detail))


def _result(engine: Engine, oid: str) -> dict:
    with engine.connect() as conn:
        r = dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())
    return {"id": r["id"], "recipient": r["recipient"], "subject": r["subject"], "status": r["status"],
            "operation_key": r["operation_key"], "detail": r["detail"], "text": owner_text(r)}


# --- what the owner sees ---------------------------------------------------------------------------------------------

def _when(t: dt.datetime | None) -> str:
    return t.strftime("%H:%M on %d %b %Y") if t else "an unknown time"


def owner_text(r: dict) -> str:
    """What the owner sees for a row; it never says sent or delivered for SMTP acceptance."""
    status = r["status"]
    if status == "queued":
        return "Waiting to send"
    if status == "sending":
        return "Sending"
    if status == "accepted":
        if r.get("detail") == FOLDER_DONE:
            return f"Written to the mail folder at {_when(r.get('accepted_at'))}."
        return (f"Handed to the mail server at {_when(r.get('accepted_at'))}. "
                "Delivery to the inbox is not confirmed.")
    if status == "unknown":
        return "We don't know whether this was sent. Nothing will be resent unless you ask."
    return f"Not sent: {r.get('detail') or 'the reason was not recorded'}"
