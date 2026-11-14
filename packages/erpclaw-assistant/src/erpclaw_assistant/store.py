"""The assistant's own store: a SQLite file of its own beside
the books, never the books' database. It holds demand evidence (requests the
engine could not fulfil) and the conversation's requests: what the owner wrote,
what the engine asked and did, which answers bind, and which card is pending.
The books stay the source of truth for every card and outcome; this store is
reconciled against them, never the reverse.

SQLite has one writer for the whole file, so every write here is a short
transaction of a few rows, never one held while the model runs."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from erpclaw_core import credentials
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, audit, company_ctx
from erpclaw_core.db import data_root

VERSION = 5
LEASE = timedelta(seconds=300)  # a turn's claim on its conversation, renewed before every model call and `propose`
# Longer than a healthy turn ever goes between two renewals: a holder silent for longer has stopped partway. Set above
# the longest gap measured on the slowest model's calls, so a live turn is never told it stopped.
STALLED_AFTER = timedelta(seconds=120)
# Configured bound: MAX_MODEL_REQUESTS (4) * hosted and Muse timeout_s (120 seconds) = 480 seconds.
# On the one evaluation route, 396 trials measured a longest whole trial (several turns) of 31.8 seconds
# and a 95th percentile of at most 14.1 seconds per run. This leaves real turns ample room while ending a stuck one.
TURN_TIMEOUT = timedelta(seconds=480)
SUMMARY_CHARS = 2000  # the ended requests the model sees, in all

_V1 = ["CREATE TABLE IF NOT EXISTS capability_demand (at TEXT NOT NULL, company_id TEXT, outcome TEXT NOT NULL, "
       "entry_id TEXT, request TEXT NOT NULL)"]
_V2 = [
    # one row per conversation: its version, the claim of the turn working on it, the claim's generation (only
    # grows), the question serial (codes never reused), and the turn's proposal intent
    "CREATE TABLE IF NOT EXISTS conversation (id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0, "
    "generation INTEGER NOT NULL DEFAULT 0, claimed_by TEXT, claim_until TEXT, renewed_at TEXT, "
    "serial INTEGER NOT NULL DEFAULT 0, intent_request TEXT, intent_state TEXT)",
    "CREATE TABLE IF NOT EXISTS request (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, token TEXT NOT NULL, "
    "state TEXT NOT NULL, ended_why TEXT, pending_question TEXT, pending_card TEXT, created_at TEXT NOT NULL, "
    "ended_at TEXT)",
    "CREATE INDEX IF NOT EXISTS ix_request_conversation ON request (conversation_id, state)",
    # a message is stored unassigned when its turn claims the conversation, and assigned after reconciliation
    "CREATE TABLE IF NOT EXISTS request_message (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, "
    "request_id TEXT, turn_id TEXT NOT NULL, text TEXT NOT NULL, choice TEXT, answer INTEGER NOT NULL DEFAULT 0, "
    "at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS request_event (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, "
    "turn_id TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL)",
    # an answer's binding, scoped to its request and its field path (the write's scope and the field)
    "CREATE TABLE IF NOT EXISTS request_binding (request_id TEXT NOT NULL, path TEXT NOT NULL, value TEXT, "
    "question_id TEXT, message_id INTEGER, PRIMARY KEY (request_id, path))",
    # a card's outcome from the books, applied to a request at most once
    "CREATE TABLE IF NOT EXISTS applied_outcome (approval_id TEXT NOT NULL, status TEXT NOT NULL, request_id TEXT, "
    "applied TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (approval_id, status))",
]


_V3 = [
    "CREATE TABLE IF NOT EXISTS message_resolution (message_id INTEGER PRIMARY KEY REFERENCES request_message(id), "
    "completed INTEGER NOT NULL DEFAULT 0, question_id TEXT, field TEXT, choice_label TEXT)",
    "CREATE TABLE IF NOT EXISTS message_name_span (id TEXT PRIMARY KEY, message_id INTEGER NOT NULL "
    "REFERENCES request_message(id), start INTEGER NOT NULL, end INTEGER NOT NULL, kind TEXT NOT NULL, "
    "record_id TEXT NOT NULL, name TEXT NOT NULL, area TEXT)",
    "CREATE INDEX IF NOT EXISTS ix_message_name_span ON message_name_span(message_id, start, end)",
    "CREATE TABLE IF NOT EXISTS span_confirmation (span_id TEXT PRIMARY KEY REFERENCES message_name_span(id), "
    "question TEXT, classification TEXT CHECK (classification IN ('name', 'instruction')))",
]

_V5 = [
    "CREATE INDEX IF NOT EXISTS ix_conversation_owner ON conversation (owner_principal_id)",
    "CREATE INDEX IF NOT EXISTS ix_request_message_conversation ON request_message (conversation_id, id)",
    "CREATE TABLE IF NOT EXISTS conversation_participant (conversation_id TEXT NOT NULL, principal_id TEXT NOT NULL, "
    "added_at TEXT NOT NULL, PRIMARY KEY (conversation_id, principal_id))",
    "CREATE TABLE IF NOT EXISTS store_receipt (step TEXT PRIMARY KEY, owner_principal_id TEXT NOT NULL, "
    "assigned_count INTEGER NOT NULL, assigned_at TEXT NOT NULL, audited_at TEXT)",
]

# Shared by every starter recovering this step; the books transaction holds it through commit.
LEGACY_OWNER_LOCK = 0x455250434C41574F


def _stored(value):
    """The single boundary for every bound store value, including encoded JSON.

    A match withholds the complete value. JSON keeps only a sentence object;
    plain text keeps only the labels and sentence. Numbers and NULL pass through.
    """
    if not isinstance(value, str) or credentials.check(value) is None:
        return value
    text = credentials.scrub(value).removesuffix(credentials.WITHHELD) + "(withheld: it held a credential)"
    try:
        json.loads(value)
    except (ValueError, RecursionError):
        return text
    return json.dumps({"text": text})


def path() -> Path:
    return data_root() / "assistant.sqlite"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_ready: set[str] = set()  # store files whose tables this process has made current


def _backfill_key(row) -> tuple:
    """Unknown dates first; otherwise original date text, table name, then row id."""
    at = row["at"]
    try:
        datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return (0, "", row["table_name"], row["id"])
    return (1, at, row["table_name"], row["id"])


def _migrate_seq(conn: sqlite3.Connection) -> None:
    """Version 4, entirely inside _open's transaction; safe to rerun on a partial old schema."""
    for table, column, definition in (
        ("conversation", "last_seq", "INTEGER NOT NULL DEFAULT 0"),
        ("request_message", "seq", "INTEGER"),
        ("request_event", "seq", "INTEGER"),
        ("request_event", "conversation_id", "TEXT"),
    ):
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for table in ("request_message", "request_event"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_conversation_seq ON {table} (conversation_id, seq)")

    # A rerun must be able to overwrite its own backfill, including a partially filled schema.
    for table in ("request_message", "request_event"):
        for suffix in ("seq_insert", "seq_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix}")
    for name in ("request_event_conversation_insert", "request_event_conversation_update",
                 "request_conversation_update"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for event in conn.execute("SELECT e.id, r.conversation_id FROM request_event e "
                              "LEFT JOIN request r ON r.id = e.request_id").fetchall():
        conn.execute("UPDATE request_event SET conversation_id = ? WHERE id = ?",
                     (_stored(event["conversation_id"]), _stored(event["id"])))
    if conn.execute("SELECT 1 FROM request_event WHERE conversation_id IS NULL LIMIT 1").fetchone():
        raise sqlite3.IntegrityError("event has no owning request conversation")
    for conversation in conn.execute("SELECT id FROM conversation").fetchall():
        cid = conversation["id"]
        items = conn.execute(
            "SELECT id, at, 'request_message' AS table_name FROM request_message WHERE conversation_id = ? UNION ALL "
            "SELECT id, at, 'request_event' AS table_name FROM request_event WHERE conversation_id = ?", (cid, cid)).fetchall()
        for seq, item in enumerate(sorted(items, key=_backfill_key), 1):
            table = item["table_name"]
            conn.execute(f"UPDATE {table} SET seq = ? WHERE id = ?", (_stored(seq), _stored(item["id"])))
        conn.execute("UPDATE conversation SET last_seq = ? WHERE id = ?", (_stored(len(items)), _stored(cid)))

    for table in ("request_message", "request_event"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE seq IS NULL LIMIT 1").fetchone():
            raise sqlite3.IntegrityError("item has no conversation for seq backfill")
        conn.execute(f"""CREATE TRIGGER {table}_seq_insert BEFORE INSERT ON {table} BEGIN
            SELECT CASE WHEN NEW.seq IS NULL THEN RAISE(ABORT, 'seq is required') END;
            SELECT CASE WHEN typeof(NEW.seq) != 'integer' OR NEW.seq < 1
                OR NEW.seq IS NOT (SELECT last_seq FROM conversation WHERE id = NEW.conversation_id)
                OR EXISTS (SELECT 1 FROM request_message
                           WHERE conversation_id = NEW.conversation_id AND seq >= NEW.seq)
                OR EXISTS (SELECT 1 FROM request_event
                           WHERE conversation_id = NEW.conversation_id AND seq >= NEW.seq)
                THEN RAISE(ABORT, 'seq must advance the conversation') END;
        END""")
        conn.execute(f"""CREATE TRIGGER {table}_seq_update BEFORE UPDATE OF seq, conversation_id ON {table}
            WHEN NEW.seq IS NOT OLD.seq OR NEW.conversation_id IS NOT OLD.conversation_id
            BEGIN SELECT RAISE(ABORT, 'seq and its conversation are immutable'); END""")
    for action, columns in (("INSERT", ""), ("UPDATE", " OF request_id, conversation_id")):
        conn.execute(f"""CREATE TRIGGER request_event_conversation_{action.lower()}
            BEFORE {action}{columns} ON request_event
            WHEN NEW.conversation_id IS NULL OR NOT EXISTS
                (SELECT 1 FROM request WHERE id = NEW.request_id AND conversation_id = NEW.conversation_id)
            BEGIN SELECT RAISE(ABORT, 'event conversation must match its request'); END""")
    conn.execute("""CREATE TRIGGER request_conversation_update BEFORE UPDATE OF conversation_id ON request
        WHEN EXISTS (SELECT 1 FROM request_event WHERE request_id = OLD.id
                     AND conversation_id IS NOT NEW.conversation_id)
        BEGIN SELECT RAISE(ABORT, 'event conversation must match its request'); END""")


def _open() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        _ready.discard(str(p))
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # private from the start
        except FileExistsError:
            pass  # another caller created it after our observation; SQLite serializes schema initialization
        else:
            os.close(fd)
    conn = sqlite3.connect(p, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(p) in _ready:
        return conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS store_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM store_version").fetchone()
        for ddl in _V1 + _V2 + _V3:
            conn.execute(ddl)
        if "renewed_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(conversation)")}:
            conn.execute("ALTER TABLE conversation ADD COLUMN renewed_at TEXT")  # a version 2 store made before it
        if "turn_deadline_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(conversation)")}:
            # Existing rows keep NULL (no deadline) until their next fresh claim.
            conn.execute("ALTER TABLE conversation ADD COLUMN turn_deadline_at TEXT")
        if "choice_label" not in {r["name"] for r in conn.execute("PRAGMA table_info(message_resolution)")}:
            conn.execute("ALTER TABLE message_resolution ADD COLUMN choice_label TEXT")
        for column in ("client_message_id", "outcome"):
            if column not in {r["name"] for r in conn.execute("PRAGMA table_info(request_message)")}:
                conn.execute(f"ALTER TABLE request_message ADD COLUMN {column} TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_request_message_client_id "
                     "ON request_message (conversation_id, client_message_id) WHERE client_message_id IS NOT NULL")
        if row is None or row["version"] < 4:
            _migrate_seq(conn)
        for table, column in (("conversation", "owner_principal_id"),
                              ("request_message", "author_principal_id"), ("request_message", "author_name")):
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        for ddl in _V5:
            conn.execute(ddl)
        if row is None:
            conn.execute("INSERT INTO store_version VALUES (?)", (_stored(VERSION),))
        elif row["version"] < VERSION:
            conn.execute("UPDATE store_version SET version = ?", (_stored(VERSION),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    _ready.add(str(p))
    return conn


@contextmanager
def _tx():
    """One short write transaction, taken at once (BEGIN IMMEDIATE), so two turns never interleave inside it."""
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _next_seq(conn: sqlite3.Connection, conversation_id: str) -> int:
    """Allocate only under the caller's BEGIN IMMEDIATE, together with its durable row."""
    row = conn.execute("UPDATE conversation SET last_seq = last_seq + 1 WHERE id = ? RETURNING last_seq",
                       (_stored(conversation_id),)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("missing conversation for seq")
    return row["last_seq"]


def _insert_events(conn: sqlite3.Connection, request_id: str, turn_id: str, turn_events: list[dict],
                   at: str | None = None) -> None:
    # The caller receives exactly the same withheld objects as history and retries.
    durable = []
    for i, event in enumerate(turn_events):
        encoded = json.dumps(event, default=str)
        stored = _stored(encoded)
        screened = json.loads(stored)
        if stored != encoded:
            screened = {"type": "refused", "code": "credential_refused", "text": screened["text"]}
        turn_events[i] = screened
        if event.get("type") not in {"working", "decision", "delivery"}:
            durable.append(screened)
    if not durable:
        return
    req = conn.execute("SELECT conversation_id FROM request WHERE id = ?", (request_id,)).fetchone()
    if req is None:
        raise sqlite3.IntegrityError("event has no owning request conversation")
    cid = req["conversation_id"]
    for event in durable:
        seq = _next_seq(conn, cid)
        conn.execute("INSERT INTO request_event (request_id, conversation_id, turn_id, event, at, seq) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (_stored(request_id), _stored(cid), _stored(turn_id), _stored(json.dumps(event, default=str)), _stored(at or _stamp(_now())), _stored(seq)))


def version() -> int:
    with _tx() as conn:
        return conn.execute("SELECT version FROM store_version").fetchone()["version"]


def assign_legacy_owner(engine, owner_principal_id: str) -> None:
    """Recover the owner assignment and its books audit across three short, durable transactions."""
    # A missing store has no legacy conversations; leave its creation lazy.
    if not path().exists():
        return
    with _tx() as conn:
        receipt = conn.execute("SELECT * FROM store_receipt WHERE step = 'legacy_owner'").fetchone()
        if receipt is None:
            assigned = conn.execute("UPDATE conversation SET owner_principal_id = ? WHERE owner_principal_id IS NULL",
                                    (_stored(owner_principal_id),)).rowcount
            conn.execute("INSERT INTO store_receipt (step, owner_principal_id, assigned_count, assigned_at) "
                         "VALUES ('legacy_owner', ?, ?, ?)", (_stored(owner_principal_id), _stored(assigned), _stored(_stamp(_now()))))
            receipt = conn.execute("SELECT * FROM store_receipt WHERE step = 'legacy_owner'").fetchone()
        if receipt["audited_at"] is not None:
            return
        detail = {key: receipt[key] for key in ("step", "owner_principal_id", "assigned_count")}
    with engine.begin() as conn:
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (LEGACY_OWNER_LOCK,))
        prior = conn.execute(select(m.audit_log.c.detail).where(
            m.audit_log.c.action == "assistant.legacy_owner_assigned")).scalars()
        if detail["assigned_count"] > 0 and not any(
                json.loads(raw or "{}").get("step") == "legacy_owner" for raw in prior):
            # Audit rows require a company: use the founding owner's earliest granted company,
            # with its id breaking equal creation times, regardless of how many companies exist.
            company_id = conn.execute(select(m.company.c.id).where(m.company.c.id.in_(
                select(m.principal_grant.c.company_id).where(
                    m.principal_grant.c.principal_id == detail["owner_principal_id"])
            )).order_by(m.company.c.created_at, m.company.c.id).limit(1)).scalar_one()
            audit(company_ctx(conn, actor="engine", company_id=company_id),
                  "assistant.legacy_owner_assigned", detail=detail)
    with _tx() as conn:
        conn.execute("UPDATE store_receipt SET audited_at = ? WHERE step = 'legacy_owner' AND audited_at IS NULL",
                     (_stored(_stamp(_now())),))


def create_conversation(conversation_id: str, principal_id: str) -> None:
    """Record the chosen id and its owner once; an existing owner is never replaced."""
    with _tx() as conn:
        conn.execute("INSERT OR IGNORE INTO conversation (id, owner_principal_id) VALUES (?, ?)",
                     (_stored(conversation_id), _stored(principal_id)))


def conversation_owner(conversation_id: str) -> str | None:
    conn = _open()
    try:
        row = conn.execute("SELECT owner_principal_id FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        return row["owner_principal_id"] if row is not None else None
    finally:
        conn.close()


def may_start_turn(conversation_id: str, principal_id: str, *, create: bool) -> bool:
    """Check ownership without taking a claim; direct callers may create a new conversation."""
    conn = _open()
    try:
        row = conn.execute("SELECT owner_principal_id FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        return create if row is None else row["owner_principal_id"] is not None and row["owner_principal_id"] == principal_id
    finally:
        conn.close()


def list_conversations(owner_principal_id: str, after: tuple[str, str] | None = None, limit: int = 50) -> dict:
    """Owned conversations with messages, ordered by last_at descending then id ascending.

    Activity timestamps come from acceptance order (seq), so a backward clock step
    can move a conversation backwards in this list. Paging during such a step may
    repeat or miss a conversation on that pass. next is the last returned pair
    only when a lookahead row exists; empty conversations are omitted.
    """
    if (type(limit) is not int or not 1 <= limit <= 50 or
            (after is not None and (not isinstance(after, tuple) or len(after) != 2 or
                                    any(not isinstance(part, str) for part in after)))):
        raise ValueError("after must be a (last_at, id) string pair or None and limit must be between 1 and 50")
    rows = []
    with _tx() as conn:
        owned = conn.execute("SELECT id FROM conversation WHERE owner_principal_id = ? ORDER BY id",
                             (owner_principal_id,)).fetchall()
        for conversation in owned:
            cid = conversation["id"]
            first = conn.execute("SELECT at, text FROM request_message WHERE conversation_id = ? "
                                 "ORDER BY seq LIMIT 1", (cid,)).fetchone()
            if first is None:
                continue
            last_message = conn.execute("SELECT at, seq FROM request_message WHERE conversation_id = ? "
                                        "ORDER BY seq DESC LIMIT 1", (cid,)).fetchone()
            last_event = conn.execute("SELECT at, seq FROM request_event WHERE conversation_id = ? "
                                      "ORDER BY seq DESC LIMIT 1", (cid,)).fetchone()
            last = last_event if last_event is not None and last_event["seq"] > last_message["seq"] else last_message
            last_at = last["at"]
            if after is not None and not (last_at < after[0] or (last_at == after[0] and cid > after[1])):
                continue
            opened = conn.execute("SELECT pending_card FROM request WHERE conversation_id = ? AND state = 'open' "
                                  "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
            state = "open" if opened is not None else "none"
            if opened is None and conn.execute("SELECT 1 FROM request WHERE conversation_id = ? AND state = 'ended' "
                                               "LIMIT 1", (cid,)).fetchone() is not None:
                state = "ended"
            messages = conn.execute("SELECT COUNT(*) FROM request_message WHERE conversation_id = ?",
                                    (cid,)).fetchone()[0]
            text = first["text"]
            preview = text
            if len(text) > 120:
                boundary = next((k for k in range(119, -1, -1) if text[k].isspace()), 120)
                preview = text[:boundary]
            rows.append({"id": cid, "started_at": first["at"], "last_at": last_at, "state": state,
                         "pending_card": opened["pending_card"] if opened is not None else None,
                         "messages": messages, "first_message": preview})
    # The SQL id order supplies the ascending tie-break for this stable sort.
    rows.sort(key=lambda row: row["last_at"], reverse=True)
    page = rows[:limit + 1]
    conversations = page[:limit]
    following = (conversations[-1]["last_at"], conversations[-1]["id"]) if len(page) > limit else None
    return {"conversations": conversations, "next": following}


def record_demand(company_id: str | None, outcome: str, entry_id: str | None, request: str) -> None:
    with _tx() as conn:
        conn.execute("INSERT INTO capability_demand VALUES (?, ?, ?, ?, ?)",
                     (_stored(_stamp(_now())), _stored(company_id), _stored(outcome), _stored(entry_id), _stored(request)))


def demand() -> list[tuple]:
    if not path().exists():
        return []
    with _tx() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT at, company_id, outcome, entry_id, request FROM capability_demand ORDER BY rowid")]


# ---------- the claim ----------

@dataclass
class Claim:
    conversation_id: str
    turn_id: str
    generation: int
    version: int
    message_id: int
    took_over: bool = False  # the claim was taken from a turn whose lease lapsed: its request gets a fresh token
    rerun: bool = False
    principal_id: str | None = None

    def owns(self, conversation_id: str) -> bool:
        return self.principal_id is not None and conversation_owner(conversation_id) == self.principal_id


@dataclass
class ClaimResult:
    kind: str
    events: list[dict] | None = None


def _claimable(row, turn_id: str | None, at: datetime) -> bool:
    return row["claimed_by"] in (None, turn_id) or row["claim_until"] is None or row["claim_until"] <= _stamp(at)


def claim(conversation_id: str, text: str, choice: str | None = None, turn_id: str | None = None, *,
          principal_id: str, principal_name: str, create: bool = True,
          client_message_id: str | None = None, choice_codes: set[str] | None = None) -> Claim | ClaimResult | None:
    """Take the conversation's claim at the next generation and store the owner's message, not yet assigned to any
    request, in one transaction. Ownership and retry identity are checked before taking a claim.
    None when a live turn holds the claim; other retry results leave it untouched.
    A stopped newest message runs again under the new turn without allocating another message or seq."""
    conversation_id, text, choice, turn_id, principal_id, principal_name, client_message_id = (
        _stored(v) for v in (conversation_id, text, choice, turn_id, principal_id, principal_name, client_message_id))
    if not principal_id:
        raise Refusal("That conversation was not found.", "no_conversation")
    turn_id = turn_id or secrets.token_hex(8)
    at = _now()
    with _tx() as conn:
        row = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        if row is None and create:
            conn.execute("INSERT INTO conversation (id, owner_principal_id) VALUES (?, ?)",
                         (_stored(conversation_id), _stored(principal_id)))
            row = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        if row is None or row["owner_principal_id"] != principal_id:
            raise Refusal("That conversation was not found.", "no_conversation")
        message = None
        if client_message_id is not None:
            message = conn.execute("SELECT * FROM request_message WHERE conversation_id = ? AND client_message_id = ?",
                                   (conversation_id, client_message_id)).fetchone()
        if message is not None:
            if message["author_principal_id"] != principal_id:
                return ClaimResult("reused")
            if message["text"] != text or message["choice"] != choice:
                return ClaimResult("reused")
            if not _claimable(row, None, at):  # a retry waits for every live holder, including this turn id
                return None
            if message["outcome"] == "overtaken":
                return ClaimResult("overtaken")
            events = [json.loads(r["event"]) for r in conn.execute(
                "SELECT event FROM request_event WHERE turn_id = ? AND conversation_id = ? ORDER BY id",
                (message["turn_id"], conversation_id))]
            if events:
                return ClaimResult("replay", events)
            if conn.execute("SELECT 1 FROM request_message WHERE conversation_id = ? AND id > ? LIMIT 1",
                            (conversation_id, message["id"])).fetchone():
                conn.execute("UPDATE request_message SET outcome = 'overtaken' WHERE id = ?", (_stored(message["id"]),))
                return ClaimResult("overtaken")
        elif choice is not None and choice_codes is not None:
            pending = conn.execute("SELECT pending_question FROM request WHERE conversation_id = ? AND state = 'open' "
                                   "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
            question = json.loads(pending["pending_question"]) if pending and pending["pending_question"] else {}
            offered = (question.get("event") or {}).get("choices", question.get("choices") or [])
            if choice not in choice_codes or not any(item.get("code") == choice for item in offered):
                raise Refusal(credentials.CREDENTIAL_REFUSED, "credential_refused")
        if not _claimable(row, turn_id, at):
            return None
        took_over = row["claimed_by"] not in (None, turn_id)  # a lapsed claim of another turn
        gen = row["generation"] + 1
        deadline = row["turn_deadline_at"] if row["claimed_by"] == turn_id else _stamp(at + TURN_TIMEOUT)
        conn.execute("UPDATE conversation SET claimed_by = ?, claim_until = ?, renewed_at = ?, generation = ?, "
                     "turn_deadline_at = ? WHERE id = ?",
                     (_stored(turn_id), _stored(_stamp(at + LEASE)), _stored(_stamp(at)), _stored(gen), _stored(deadline), _stored(conversation_id)))
        if message is not None:
            mid = message["id"]
            conn.execute("UPDATE request_message SET turn_id = ? WHERE id = ?", (_stored(turn_id), _stored(mid)))
        else:
            seq = _next_seq(conn, conversation_id)
            previous = conn.execute("SELECT request_id, outcome FROM request_message "
                                    "WHERE conversation_id = ? ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
            opened = conn.execute("SELECT id, pending_card FROM request WHERE conversation_id = ? "
                                  "AND state = 'open' ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
            continuation = bool(not took_over and previous and opened and not opened["pending_card"] and
                                previous["request_id"] == opened["id"] and
                                previous["outcome"] == "reads_only_turn")
            mid = conn.execute("INSERT INTO request_message (conversation_id, request_id, turn_id, text, choice, answer, "
                               "at, seq, author_principal_id, author_name, client_message_id, outcome) "
                               "VALUES (?, NULL, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                               (_stored(conversation_id), _stored(turn_id), _stored(text), _stored(choice), _stored(_stamp(at)), _stored(seq),
                                _stored(principal_id), _stored(principal_name), _stored(client_message_id),
                                _stored("reads_only_continuation" if continuation else None))).lastrowid
        return Claim(conversation_id, turn_id, gen, row["version"], mid, took_over, message is not None, principal_id)


def _holds(conn, c: Claim, at: datetime | None = None) -> bool:
    row = conn.execute("SELECT claimed_by, claim_until, generation FROM conversation WHERE id = ?",
                       (c.conversation_id,)).fetchone()
    at = at or _now()
    return row is not None and row["claimed_by"] == c.turn_id and row["generation"] == c.generation \
        and row["claim_until"] is not None and row["claim_until"] > _stamp(at)


def renew(c: Claim) -> bool:
    """Extend the lease, only while the claim is still this turn's and unexpired; False means stop at once."""
    at = _now()
    with _tx() as conn:
        if not _holds(conn, c, at):
            return False
        conn.execute("UPDATE conversation SET claim_until = ?, renewed_at = ? WHERE id = ?",
                     (_stored(_stamp(at + LEASE)), _stored(_stamp(at)), _stored(c.conversation_id)))
        return True


def holder_stalled(conversation_id: str) -> bool:
    """Whether the turn holding the conversation has gone longer than STALLED_AFTER since it last claimed or renewed
    (it stopped partway), from the store's own record."""
    with _tx() as conn:
        row = conn.execute("SELECT renewed_at FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    return row is None or row["renewed_at"] is None or row["renewed_at"] < _stamp(_now() - STALLED_AFTER)


def turn_remaining(c: Claim) -> float | None:
    """Seconds left on this holder's immutable deadline; legacy NULL means no deadline.

    A lost claim has no budget. The caller still fences completion through finalise, so a superseded worker
    cannot publish a timeout or change another holder's deadline.
    """
    at = _now()
    with _tx() as conn:
        if not _holds(conn, c, at):
            return 0.0
        row = conn.execute("SELECT turn_deadline_at FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
    deadline = row["turn_deadline_at"]
    return None if deadline is None else (datetime.fromisoformat(deadline) - at).total_seconds()


def catch_up(c: Claim, fence_generation: int) -> Claim | None:
    """A store behind the books: while this turn still holds a live claim, its generation jumps past the fence's and
    the claim is taken again there. None when the claim is no longer this turn's."""
    at = _now()
    with _tx() as conn:
        if not _holds(conn, c, at):
            return None
        gen = max(fence_generation, c.generation) + 1
        conn.execute("UPDATE conversation SET generation = ?, claim_until = ? WHERE id = ?",
                     (_stored(gen), _stored(_stamp(at + LEASE)), _stored(c.conversation_id)))
        return Claim(c.conversation_id, c.turn_id, gen, c.version, c.message_id, c.took_over, c.rerun, c.principal_id)


def next_generation(c: Claim) -> Claim | None:
    """A new request in the same turn (reconciliation ended the open one): the claim at the next generation."""
    return catch_up(c, c.generation)


def release(c: Claim) -> None:
    """Give up the claim without finalising (a turn that stopped); writes nothing else."""
    with _tx() as conn:
        conn.execute("UPDATE conversation SET claimed_by = NULL, claim_until = NULL, turn_deadline_at = NULL "
                     "WHERE id = ? AND claimed_by = ?",
                     (_stored(c.conversation_id), _stored(c.turn_id)))


# ---------- requests ----------

def _request(row) -> dict | None:
    if row is None:
        return None
    r = dict(row)
    r["pending_question"] = json.loads(r["pending_question"]) if r["pending_question"] else None
    return r


def open_request(conversation_id: str) -> dict | None:
    with _tx() as conn:
        return _request(conn.execute("SELECT * FROM request WHERE conversation_id = ? AND state = 'open' "
                                     "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone())


def last_ended_why(conversation_id: str | None) -> str | None:
    """Why the conversation's latest ended request ended: while a request is open, the one just before it."""
    with _tx() as conn:
        row = conn.execute("SELECT ended_why FROM request WHERE conversation_id = ? AND state = 'ended' "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1", (conversation_id,)).fetchone()
    return row["ended_why"] if row else None


def get_request(request_id: str) -> dict | None:
    with _tx() as conn:
        return _request(conn.execute("SELECT * FROM request WHERE id = ?", (request_id,)).fetchone())


def new_request(c: Claim) -> dict | None:
    """A request with a fresh card token; only under a live claim."""
    with _tx() as conn:
        if not _holds(conn, c):
            return None
        rid, token = secrets.token_hex(12), secrets.token_hex(16)
        conn.execute("INSERT INTO request (id, conversation_id, token, state, created_at) VALUES (?, ?, ?, 'open', ?)",
                     (_stored(rid), _stored(c.conversation_id), _stored(token), _stored(_stamp(_now()))))
        return _request(conn.execute("SELECT * FROM request WHERE id = ?", (rid,)).fetchone())


def retoken(c: Claim, request_id: str) -> dict | None:
    """A takeover's fresh card token for the open request, under a live claim: the stalled turn's token dies with
    the fence's next activation, and with it every card it made or would make."""
    with _tx() as conn:
        if not _holds(conn, c):
            return None
        conn.execute("UPDATE request SET token = ? WHERE id = ? AND state = 'open'", (_stored(secrets.token_hex(16)), _stored(request_id)))
        return _request(conn.execute("SELECT * FROM request WHERE id = ?", (request_id,)).fetchone())


def assign(c: Claim, request_id: str) -> bool:
    """Assign the turn's message to its request and record the turn's proposal intent, under a live claim."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        conn.execute("UPDATE request_message SET request_id = ? WHERE id = ? AND request_id IS NULL",
                     (_stored(request_id), _stored(c.message_id)))
        conn.execute("UPDATE conversation SET intent_request = ?, intent_state = 'open' WHERE id = ?",
                     (_stored(request_id), _stored(c.conversation_id)))
        return True


def reassign(c: Claim, request_id: str) -> bool:
    """Move only this re-run's message to its current request, under the live claim."""
    with _tx() as conn:
        if not c.rerun or not _holds(conn, c):
            return False
        if conn.execute("SELECT 1 FROM request WHERE id = ? AND conversation_id = ? AND state = 'open'",
                        (request_id, c.conversation_id)).fetchone() is None:
            return False
        moved = conn.execute("UPDATE request_message SET request_id = ? "
                             "WHERE id = ? AND conversation_id = ? AND turn_id = ?",
                             (_stored(request_id), _stored(c.message_id), _stored(c.conversation_id), _stored(c.turn_id)))
        if moved.rowcount != 1:
            return False
        conn.execute("UPDATE conversation SET intent_request = ?, intent_state = 'open' WHERE id = ?",
                     (_stored(request_id), _stored(c.conversation_id)))
        return True


def mark_answer(c: Claim) -> bool:
    """The turn's message answered the pending question: its words are consumed by the binding, never read again as
    the request's words."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        conn.execute("UPDATE request_message SET answer = 1 WHERE id = ?", (_stored(c.message_id),))
        return True


def intent(conversation_id: str) -> tuple[str | None, str | None]:
    with _tx() as conn:
        row = conn.execute("SELECT intent_request, intent_state FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        return (row["intent_request"], row["intent_state"]) if row else (None, None)


def settle_intent(conversation_id: str, state: str) -> None:
    with _tx() as conn:
        conn.execute("UPDATE conversation SET intent_state = ? WHERE id = ? AND intent_state = 'open'",
                     (_stored(state), _stored(conversation_id)))


def close_retired_intent(c: Claim, request_id: str, retired: list[str], pending_card: str | None) -> bool:
    """Atomically close the intent and clear its observed retired pending card under the claim."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        req = conn.execute("SELECT state, pending_card FROM request WHERE id = ? AND conversation_id = ?",
                           (request_id, c.conversation_id)).fetchone()
        if req is None or req["state"] != "open" or req["pending_card"] != pending_card:
            return False
        row = conn.execute("SELECT intent_request, intent_state FROM conversation WHERE id = ?",
                           (c.conversation_id,)).fetchone()
        if row["intent_state"] == "open" and row["intent_request"] == request_id:
            for aid in retired:
                conn.execute("INSERT OR IGNORE INTO applied_outcome VALUES (?, ?, ?, ?, ?)",
                             (_stored(aid), _stored("superseded"), _stored(request_id), _stored("set_aside"), _stored(_stamp(_now()))))
            conn.execute("UPDATE conversation SET intent_state = ? WHERE id = ?",
                         (_stored("set_aside" if retired else "unused"), _stored(c.conversation_id)))
        if pending_card in retired:
            conn.execute("UPDATE request SET pending_card = NULL WHERE id = ?", (_stored(request_id),))
        return True


def clear_retired_card(c: Claim, request_id: str, approval_id: str) -> bool:
    """Clear the observed retired card only while this claim and request still match."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        result = conn.execute("UPDATE request SET pending_card = NULL WHERE id = ? AND conversation_id = ? "
                              "AND state = 'open' AND pending_card = ?",
                              (_stored(request_id), _stored(c.conversation_id), _stored(approval_id)))
        return result.rowcount == 1


def claim_lapsed(conversation_id: str) -> bool:
    with _tx() as conn:
        row = conn.execute("SELECT claimed_by, claim_until FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        return row is None or row["claimed_by"] is None or row["claim_until"] <= _stamp(_now())


def end_request(request_id: str, why: str, events: list[dict] | None = None, turn_id: str = "reconcile") -> bool:
    """End an open request (reconciliation applying an outcome from the books); its bindings end with it."""
    with _tx() as conn:
        res = conn.execute("UPDATE request SET state = 'ended', ended_why = ?, ended_at = ?, pending_question = NULL, "
                           "pending_card = NULL WHERE id = ? AND state = 'open'", (_stored(why), _stored(_stamp(_now())), _stored(request_id)))
        if res.rowcount != 1:
            return False
        conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        _insert_events(conn, request_id, turn_id, events or [])
        return True


def set_pending(request_id: str, *, card: str | None = None, question: dict | None = None,
                events: list[dict] | None = None, turn_id: str = "reconcile", serial: int | None = None) -> None:
    """Reconciliation's change to an open request's pending card or question; `serial`, the conversation's question
    number after a question it coded, never lowered."""
    with _tx() as conn:
        conn.execute("UPDATE request SET pending_card = ?, pending_question = ? WHERE id = ? AND state = 'open'",
                     (_stored(card), _stored(json.dumps(question) if question else None), _stored(request_id)))
        if serial is not None:
            conn.execute("UPDATE conversation SET serial = MAX(serial, ?) WHERE id = (SELECT conversation_id FROM "
                         "request WHERE id = ?)", (_stored(serial), _stored(request_id)))
        _insert_events(conn, request_id, turn_id, events or [])


def apply_outcome(approval_id: str, status: str, request_id: str | None, applied: str) -> bool:
    """Record a card's outcome as applied, once per (card, status); False when it already was."""
    with _tx() as conn:
        res = conn.execute("INSERT OR IGNORE INTO applied_outcome VALUES (?, ?, ?, ?, ?)",
                           (_stored(approval_id), _stored(status), _stored(request_id), _stored(applied), _stored(_stamp(_now()))))
        return res.rowcount == 1


def reconcile_outcome(c: Claim, approval_id: str, status: str, request_id: str, applied: str, *,
                      card: str | None = None, question: dict | None = None, events: list[dict] | None = None,
                      serial: int | None = None, previous_serial: int | None = None,
                      end_reason: str | None = None) -> str:
    """Apply a pending card's outcome and its request state, events and serial in one fenced transaction.

    A marker already present while this card is still pending is an older, partial application: it is unsafe to
    invent a question or assume which events were shown. Leave it untouched for explicit recovery.
    """
    with _tx() as conn:
        if not _holds(conn, c):
            return "stale"
        req = conn.execute("SELECT state, pending_card, conversation_id FROM request WHERE id = ?", (request_id,)).fetchone()
        if req is None or req["conversation_id"] != c.conversation_id or req["state"] != "open" \
                or req["pending_card"] != approval_id:
            return "stale"
        if previous_serial is not None:
            current = conn.execute("SELECT serial FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
            if current is None or current["serial"] != previous_serial:
                return "stale"
        if conn.execute("SELECT 1 FROM applied_outcome WHERE approval_id = ? AND status = ?",
                        (approval_id, status)).fetchone():
            return "incomplete"
        at = _stamp(_now())
        conn.execute("INSERT INTO applied_outcome VALUES (?, ?, ?, ?, ?)",
                     (_stored(approval_id), _stored(status), _stored(request_id), _stored(applied), _stored(at)))
        if end_reason is not None:
            conn.execute("UPDATE request SET state = 'ended', ended_why = ?, ended_at = ?, pending_question = NULL, "
                         "pending_card = NULL WHERE id = ?", (_stored(end_reason), _stored(at), _stored(request_id)))
            conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        else:
            conn.execute("UPDATE request SET pending_card = ?, pending_question = ? WHERE id = ?",
                         (_stored(card), _stored(json.dumps(question) if question else None), _stored(request_id)))
        if serial is not None:
            conn.execute("UPDATE conversation SET serial = MAX(serial, ?) WHERE id = ?", (_stored(serial), _stored(c.conversation_id)))
        _insert_events(conn, request_id, "reconcile", events or [])
        return "applied"


def outcome_applied(approval_id: str, status: str) -> bool:
    with _tx() as conn:
        return conn.execute("SELECT 1 FROM applied_outcome WHERE approval_id = ? AND status = ?",
                            (approval_id, status)).fetchone() is not None


# ---------- what the request holds ----------

def messages(request_id: str) -> list[dict]:
    with _tx() as conn:
        return [dict(r) for r in conn.execute("SELECT id, text, choice, answer, turn_id FROM request_message "
                                              "WHERE request_id = ? ORDER BY id", (request_id,))]


def unassigned(conversation_id: str) -> list[dict]:
    with _tx() as conn:
        return [dict(r) for r in conn.execute("SELECT id, text, turn_id FROM request_message WHERE conversation_id = ? "
                                              "AND request_id IS NULL ORDER BY id", (conversation_id,))]


def events(request_id: str) -> list[dict]:
    with _tx() as conn:
        return [json.loads(r["event"]) for r in conn.execute(
            "SELECT event FROM request_event WHERE request_id = ? ORDER BY id", (request_id,))]


def bindings(request_id: str) -> dict:
    with _tx() as conn:
        return {r["path"]: json.loads(r["value"]) for r in conn.execute(
            "SELECT path, value FROM request_binding WHERE request_id = ?", (request_id,))}


def serial(conversation_id: str) -> int:
    with _tx() as conn:
        row = conn.execute("SELECT serial FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        return row["serial"] if row else 0


def ended_summaries(conversation_id: str, limit: int = SUMMARY_CHARS) -> list[str]:
    """The conversation's ended requests, newest kept first within the limit, oldest first in the result."""
    out: list[str] = []
    used = 0
    with _tx() as conn:
        rows = conn.execute("SELECT id, ended_why FROM request WHERE conversation_id = ? AND state = 'ended' "
                            "ORDER BY ended_at DESC", (conversation_id,)).fetchall()
        for r in rows:
            said = " / ".join(m["text"] for m in conn.execute(
                "SELECT text FROM request_message WHERE request_id = ? ORDER BY id", (r["id"],)))
            events = [json.loads(e["event"]) for e in conn.execute(
                "SELECT event FROM request_event WHERE request_id = ? ORDER BY id", (r["id"],))]
            did = " / ".join(("Assistant: " if ev.get("type") == "answer" and ev.get("checked") == "passed"
                              else "Engine: ") + ev["text"] for ev in events if ev.get("text"))
            text = f"Owner: {said} -> {did or 'Engine: (nothing)'} [ended: {r['ended_why']}]"
            if used + len(text) > limit:
                break
            out.append(text)
            used += len(text)
    return list(reversed(out))


# ---------- finalise ----------

def message_resolution(message_id: int) -> dict:
    """The immutable message's completed name consumption, including explicit empty results."""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM message_resolution WHERE message_id = ?", (message_id,)).fetchone()
        if row and not row["completed"]:
            row = None
        spans = [dict(r) for r in conn.execute(
            "SELECT s.*, c.question, c.classification FROM message_name_span s LEFT JOIN span_confirmation c "
            "ON c.span_id = s.id WHERE s.message_id = ? ORDER BY s.start, s.end, s.id", (message_id,))] \
            if row and row["completed"] else []
        for span in spans:
            span["question"] = json.loads(span["question"]) if span["question"] else None
        return {"completed": bool(row and row["completed"]), "question_id": row["question_id"] if row else None,
                "field": row["field"] if row else None, "choice_label": row["choice_label"] if row else None,
                "spans": spans}


def save_resolution(c: Claim, request_id: str, resolution: dict) -> bool:
    """Checkpoint name consumption and the resolution state together, before any proposal."""
    with _tx() as conn:
        row = conn.execute("SELECT version FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
        if not _holds(conn, c) or row["version"] != c.version:
            return False
        for msg in resolution.get("owner_messages", []):
            owned = conn.execute("SELECT id FROM request_message WHERE id = ? AND request_id = ?",
                                 (msg["id"], request_id)).fetchone()
            if not owned:
                return False
        for msg in resolution.get("owner_messages", []):
            saved = conn.execute("SELECT completed FROM message_resolution WHERE message_id = ?",
                                 (msg["id"],)).fetchone()
            if saved and saved["completed"]:
                continue
            conn.execute("DELETE FROM span_confirmation WHERE span_id IN "
                         "(SELECT id FROM message_name_span WHERE message_id = ?)", (msg["id"],))
            conn.execute("DELETE FROM message_name_span WHERE message_id = ?", (msg["id"],))
            for span in msg.get("spans", []):
                conn.execute("INSERT INTO message_name_span (id, message_id, start, end, kind, record_id, name, area) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (_stored(span["id"]), _stored(msg["id"]), _stored(span["start"]), _stored(span["end"]), _stored(span["kind"]),
                              _stored(span["record_id"]), _stored(span["name"]), _stored(span.get("area"))))
                if span.get("area"):
                    conn.execute("INSERT INTO span_confirmation (span_id, question, classification) VALUES (?, ?, ?)",
                                 (_stored(span["id"]), _stored(json.dumps(span.get("question")) if span.get("question") else None),
                                  _stored(span.get("classification"))))
            if msg.get("answer"):
                conn.execute("UPDATE request_message SET answer = 1 WHERE id = ?", (_stored(msg["id"]),))
            conn.execute("INSERT INTO message_resolution "
                         "(message_id, completed, question_id, field, choice_label) VALUES (?, 1, ?, ?, ?) "
                         "ON CONFLICT(message_id) DO UPDATE SET completed = 1, question_id = excluded.question_id, "
                         "field = excluded.field, choice_label = excluded.choice_label",
                         (_stored(msg["id"]), _stored(msg.get("question_id")), _stored(msg.get("field")), _stored(msg.get("choice_label"))))
        for msg in resolution.get("owner_messages", []):
            for span in msg.get("spans", []):
                if span.get("area"):
                    conn.execute("UPDATE span_confirmation SET question = COALESCE(question, ?), "
                                 "classification = COALESCE(classification, ?) WHERE span_id = ? AND "
                                 "((question IS NULL AND ? IS NOT NULL) OR (classification IS NULL AND ? IS NOT NULL))",
                                 (_stored(json.dumps(span.get("question")) if span.get("question") else None),
                                  _stored(span.get("classification")), _stored(span["id"]),
                                  _stored(json.dumps(span.get("question")) if span.get("question") else None),
                                  _stored(span.get("classification"))))
        pending = resolution.get("pending")
        conn.execute("UPDATE request SET pending_question = ? WHERE id = ?",
                     (_stored(json.dumps(pending) if pending else None), _stored(request_id)))
        conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        conn.executemany("INSERT INTO request_binding (request_id, path, value) VALUES (?, ?, ?)",
                         [(_stored(request_id), _stored(p), _stored(json.dumps(v))) for p, v in resolution.get("bindings", {}).items()])
        conn.execute("UPDATE conversation SET serial = max(serial, ?) WHERE id = ?",
                     (_stored(resolution.get("serial", 0)), _stored(c.conversation_id)))
        return True


def finalise(c: Claim, request_id: str, turn_events: list[dict], *, pending_question: dict | None,
             pending_card: str | None, state: str, why: str | None, serial_now: int, bindings_now: dict,
             intent_state: str, reads_only_turn: bool = False) -> bool:
    """Write the turn's outcome in one transaction, only while the claim is still this turn's and the version
    unchanged: its events, the request's pending question, card and state, its bindings, the intent, the version,
    and the claim released. False writes nothing."""
    at = _now()
    with _tx() as conn:
        row = conn.execute("SELECT version FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
        if not _holds(conn, c, at) or row["version"] != c.version:
            return False
        _insert_events(conn, request_id, c.turn_id, turn_events, _stamp(at))
        if reads_only_turn:
            conn.execute("UPDATE request_message SET outcome = 'reads_only_turn' WHERE id = ?", (_stored(c.message_id),))
        ended = state == "ended"
        conn.execute("UPDATE request SET state = ?, ended_why = ?, ended_at = ?, pending_question = ?, pending_card = ? "
                     "WHERE id = ?", (_stored(state), _stored(why), _stored(_stamp(at) if ended else None),
                                      _stored(None if ended or not pending_question else json.dumps(pending_question)),
                                      _stored(None if ended else pending_card), _stored(request_id)))
        conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        if not ended:  # bindings end with their request
            conn.executemany("INSERT INTO request_binding (request_id, path, value) VALUES (?, ?, ?)",
                             [(_stored(request_id), _stored(p), _stored(json.dumps(v))) for p, v in bindings_now.items()])
        conn.execute("UPDATE conversation SET version = version + 1, serial = max(serial, ?), claimed_by = NULL, "
                     "claim_until = NULL, turn_deadline_at = NULL, "
                     "intent_state = CASE WHEN intent_state = 'open' THEN ? ELSE intent_state END "
                     "WHERE id = ?", (_stored(serial_now), _stored(intent_state), _stored(c.conversation_id)))
        return True


def finalise_refusal(c: Claim, event: dict) -> bool:
    """End a refused turn and release its claim, without answering or setting aside any pending request.

    Claim has already retained only labels. The refusal's own ended request makes the
    message and fixed event available to history and retries, under the same version fence.
    """
    at = _now()
    with _tx() as conn:
        row = conn.execute("SELECT version FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
        if not _holds(conn, c, at) or row["version"] != c.version:
            return False
        rid = secrets.token_hex(12)
        conn.execute("INSERT INTO request (id, conversation_id, token, state, created_at, ended_at, ended_why) "
                     "VALUES (?, ?, ?, 'ended', ?, ?, 'refused')",
                     (_stored(rid), _stored(c.conversation_id), _stored(secrets.token_hex(16)), _stored(_stamp(at)), _stored(_stamp(at))))
        conn.execute("UPDATE request_message SET request_id = ? WHERE id = ?", (_stored(rid), _stored(c.message_id)))
        _insert_events(conn, rid, c.turn_id, [event], _stamp(at))
        conn.execute("UPDATE conversation SET version = version + 1, claimed_by = NULL, claim_until = NULL, "
                     "turn_deadline_at = NULL WHERE id = ?", (_stored(c.conversation_id),))
        return True


def add_events(request_id: str, turn_events: list[dict], turn_id: str = "outside") -> None:
    """Events that happened outside a turn (an approval's outcome shown to the owner), kept as the request's history."""
    with _tx() as conn:
        _insert_events(conn, request_id, turn_id, turn_events)


def timeline(request_id: str) -> list[tuple[str, object, int]]:
    """The request's owner messages and engine events in acceptance order: ("owner", text, message id) or
    ("engine", event, event id)."""
    with _tx() as conn:
        said = [(r["seq"], ("owner", r["text"], r["id"])) for r in conn.execute(
            "SELECT id, text, seq FROM request_message WHERE request_id = ?", (request_id,))]
        did = [(r["seq"], ("engine", json.loads(r["event"]), r["id"])) for r in conn.execute(
            "SELECT id, event, seq FROM request_event WHERE request_id = ?", (request_id,))]
    return [x[1] for x in sorted(said + did)]


def history_page(conversation_id: str, after: int = 0, limit: int = 200) -> dict:
    """Durable items after a conversation cursor, in seq order (at most 200).

    Owner items carry text, author (falling back to 'owner'), and choice when sent;
    engine items carry the decoded event. Both carry kind, id, request_id, seq and the original at.
    next is the last returned seq, or after for an empty page. Gaps are allowed.
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("after must be nonnegative and limit must be between 1 and 200")
    with _tx() as conn:
        said = []
        for row in conn.execute("SELECT * FROM request_message WHERE conversation_id = ? AND seq > ? "
                                "ORDER BY seq LIMIT ?", (conversation_id, after, limit)):
            said.append({"kind": "owner", "id": row["id"], "request_id": row["request_id"], "seq": row["seq"],
                         "at": row["at"], "text": row["text"],
                         "author": row["author_name"] if row["author_name"] is not None else "owner"})
            if row["choice"] is not None:
                said[-1]["choice"] = row["choice"]
        did = [{"kind": "engine", "id": row["id"], "request_id": row["request_id"], "seq": row["seq"],
                "at": row["at"], "event": json.loads(row["event"])} for row in conn.execute(
                    "SELECT * FROM request_event WHERE conversation_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (conversation_id, after, limit))]
    items = sorted(said + did, key=lambda item: item["seq"])[:limit]
    return {"items": items, "next": items[-1]["seq"] if items else after}


def live_turn(conversation_id: str) -> dict | None:
    """Read the current holder and its lease; only a live holder supplies a turn id."""
    with _tx() as conn:
        row = conn.execute("SELECT claimed_by, claim_until FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        if row is None:
            return None
        live = row["claimed_by"] is not None and row["claim_until"] is not None \
            and row["claim_until"] > _stamp(_now())
        return {"claimed_by": row["claimed_by"], "claim_until": row["claim_until"],
                "turn_id": row["claimed_by"] if live else None, "live": live}


def watched_card_ids(conversation_id: str) -> list[str]:
    """Approval ids from the open pending card and all durable card announcements, without status reads."""
    with _tx() as conn:
        row = conn.execute("SELECT pending_card FROM request WHERE conversation_id = ? AND state = 'open' "
                           "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
        ids = {row["pending_card"]} if row is not None and row["pending_card"] is not None else set()
        for row in conn.execute("SELECT event FROM request_event WHERE conversation_id = ?", (conversation_id,)):
            event = json.loads(row["event"])
            if event.get("type") == "card" and isinstance(event.get("approval_id"), str):
                ids.add(event["approval_id"])
    return sorted(ids)
