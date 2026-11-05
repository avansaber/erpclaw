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

from erpclaw_core.db import data_root

VERSION = 2
LEASE = timedelta(seconds=300)  # a turn's claim on its conversation, renewed before every model call and `propose`
# Longer than a healthy turn ever goes between two renewals: a holder silent for longer has stopped partway. Set above
# the longest gap measured on the slowest model's calls, so a live turn is never told it stopped.
STALLED_AFTER = timedelta(seconds=120)
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


def path() -> Path:
    return data_root() / "assistant.sqlite"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_ready: set[str] = set()  # store files whose tables this process has made current


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
        for ddl in _V1 + _V2:  # version 1 is migrated by adding version 2's tables
            conn.execute(ddl)
        if "renewed_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(conversation)")}:
            conn.execute("ALTER TABLE conversation ADD COLUMN renewed_at TEXT")  # a version 2 store made before it
        if row is None:
            conn.execute("INSERT INTO store_version VALUES (?)", (VERSION,))
        elif row["version"] < VERSION:
            conn.execute("UPDATE store_version SET version = ?", (VERSION,))
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


def version() -> int:
    with _tx() as conn:
        return conn.execute("SELECT version FROM store_version").fetchone()["version"]


def record_demand(company_id: str | None, outcome: str, entry_id: str | None, request: str) -> None:
    with _tx() as conn:
        conn.execute("INSERT INTO capability_demand VALUES (?, ?, ?, ?, ?)",
                     (_stamp(_now()), company_id, outcome, entry_id, request))


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


def _claimable(row, turn_id: str, at: datetime) -> bool:
    return row["claimed_by"] in (None, turn_id) or row["claim_until"] is None or row["claim_until"] <= _stamp(at)


def claim(conversation_id: str, text: str, choice: str | None = None, turn_id: str | None = None) -> Claim | None:
    """Take the conversation's claim at the next generation and store the owner's message, not yet assigned to any
    request, in one transaction. None when a live turn holds the claim: nothing is written."""
    turn_id = turn_id or secrets.token_hex(8)
    at = _now()
    with _tx() as conn:
        conn.execute("INSERT OR IGNORE INTO conversation (id) VALUES (?)", (conversation_id,))
        row = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        if not _claimable(row, turn_id, at):
            return None
        took_over = row["claimed_by"] not in (None, turn_id)  # a lapsed claim of another turn
        gen = row["generation"] + 1
        conn.execute("UPDATE conversation SET claimed_by = ?, claim_until = ?, renewed_at = ?, generation = ? "
                     "WHERE id = ?", (turn_id, _stamp(at + LEASE), _stamp(at), gen, conversation_id))
        mid = conn.execute("INSERT INTO request_message (conversation_id, request_id, turn_id, text, choice, answer, "
                           "at) VALUES (?, NULL, ?, ?, ?, 0, ?)", (conversation_id, turn_id, text, choice, _stamp(at))).lastrowid
        return Claim(conversation_id, turn_id, gen, row["version"], mid, took_over)


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
                     (_stamp(at + LEASE), _stamp(at), c.conversation_id))
        return True


def holder_stalled(conversation_id: str) -> bool:
    """Whether the turn holding the conversation has gone longer than STALLED_AFTER since it last claimed or renewed
    (it stopped partway), from the store's own record."""
    with _tx() as conn:
        row = conn.execute("SELECT renewed_at FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    return row is None or row["renewed_at"] is None or row["renewed_at"] < _stamp(_now() - STALLED_AFTER)


def catch_up(c: Claim, fence_generation: int) -> Claim | None:
    """A store behind the books: while this turn still holds a live claim, its generation jumps past the fence's and
    the claim is taken again there. None when the claim is no longer this turn's."""
    at = _now()
    with _tx() as conn:
        if not _holds(conn, c, at):
            return None
        gen = max(fence_generation, c.generation) + 1
        conn.execute("UPDATE conversation SET generation = ?, claim_until = ? WHERE id = ?",
                     (gen, _stamp(at + LEASE), c.conversation_id))
        return Claim(c.conversation_id, c.turn_id, gen, c.version, c.message_id, c.took_over)


def next_generation(c: Claim) -> Claim | None:
    """A new request in the same turn (reconciliation ended the open one): the claim at the next generation."""
    return catch_up(c, c.generation)


def release(c: Claim) -> None:
    """Give up the claim without finalising (a turn that stopped); writes nothing else."""
    with _tx() as conn:
        conn.execute("UPDATE conversation SET claimed_by = NULL, claim_until = NULL WHERE id = ? AND claimed_by = ?",
                     (c.conversation_id, c.turn_id))


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
                     (rid, c.conversation_id, token, _stamp(_now())))
        return _request(conn.execute("SELECT * FROM request WHERE id = ?", (rid,)).fetchone())


def retoken(c: Claim, request_id: str) -> dict | None:
    """A takeover's fresh card token for the open request, under a live claim: the stalled turn's token dies with
    the fence's next activation, and with it every card it made or would make."""
    with _tx() as conn:
        if not _holds(conn, c):
            return None
        conn.execute("UPDATE request SET token = ? WHERE id = ? AND state = 'open'", (secrets.token_hex(16), request_id))
        return _request(conn.execute("SELECT * FROM request WHERE id = ?", (request_id,)).fetchone())


def assign(c: Claim, request_id: str) -> bool:
    """Assign the turn's message to its request and record the turn's proposal intent, under a live claim."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        conn.execute("UPDATE request_message SET request_id = ? WHERE id = ? AND request_id IS NULL",
                     (request_id, c.message_id))
        conn.execute("UPDATE conversation SET intent_request = ?, intent_state = 'open' WHERE id = ?",
                     (request_id, c.conversation_id))
        return True


def mark_answer(c: Claim) -> bool:
    """The turn's message answered the pending question: its words are consumed by the binding, never read again as
    the request's words."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        conn.execute("UPDATE request_message SET answer = 1 WHERE id = ?", (c.message_id,))
        return True


def intent(conversation_id: str) -> tuple[str | None, str | None]:
    with _tx() as conn:
        row = conn.execute("SELECT intent_request, intent_state FROM conversation WHERE id = ?",
                           (conversation_id,)).fetchone()
        return (row["intent_request"], row["intent_state"]) if row else (None, None)


def settle_intent(conversation_id: str, state: str) -> None:
    with _tx() as conn:
        conn.execute("UPDATE conversation SET intent_state = ? WHERE id = ? AND intent_state = 'open'",
                     (state, conversation_id))


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
                             (aid, "superseded", request_id, "set_aside", _stamp(_now())))
            conn.execute("UPDATE conversation SET intent_state = ? WHERE id = ?",
                         ("set_aside" if retired else "unused", c.conversation_id))
        if pending_card in retired:
            conn.execute("UPDATE request SET pending_card = NULL WHERE id = ?", (request_id,))
        return True


def clear_retired_card(c: Claim, request_id: str, approval_id: str) -> bool:
    """Clear the observed retired card only while this claim and request still match."""
    with _tx() as conn:
        if not _holds(conn, c):
            return False
        result = conn.execute("UPDATE request SET pending_card = NULL WHERE id = ? AND conversation_id = ? "
                              "AND state = 'open' AND pending_card = ?",
                              (request_id, c.conversation_id, approval_id))
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
                           "pending_card = NULL WHERE id = ? AND state = 'open'", (why, _stamp(_now()), request_id))
        if res.rowcount != 1:
            return False
        conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        for e in events or []:
            conn.execute("INSERT INTO request_event (request_id, turn_id, event, at) VALUES (?, ?, ?, ?)",
                         (request_id, turn_id, json.dumps(e, default=str), _stamp(_now())))
        return True


def set_pending(request_id: str, *, card: str | None = None, question: dict | None = None,
                events: list[dict] | None = None, turn_id: str = "reconcile", serial: int | None = None) -> None:
    """Reconciliation's change to an open request's pending card or question; `serial`, the conversation's question
    number after a question it coded, never lowered."""
    with _tx() as conn:
        conn.execute("UPDATE request SET pending_card = ?, pending_question = ? WHERE id = ? AND state = 'open'",
                     (card, json.dumps(question) if question else None, request_id))
        if serial is not None:
            conn.execute("UPDATE conversation SET serial = MAX(serial, ?) WHERE id = (SELECT conversation_id FROM "
                         "request WHERE id = ?)", (serial, request_id))
        for e in events or []:
            conn.execute("INSERT INTO request_event (request_id, turn_id, event, at) VALUES (?, ?, ?, ?)",
                         (request_id, turn_id, json.dumps(e, default=str), _stamp(_now())))


def apply_outcome(approval_id: str, status: str, request_id: str | None, applied: str) -> bool:
    """Record a card's outcome as applied, once per (card, status); False when it already was."""
    with _tx() as conn:
        res = conn.execute("INSERT OR IGNORE INTO applied_outcome VALUES (?, ?, ?, ?, ?)",
                           (approval_id, status, request_id, applied, _stamp(_now())))
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
                     (approval_id, status, request_id, applied, at))
        if end_reason is not None:
            conn.execute("UPDATE request SET state = 'ended', ended_why = ?, ended_at = ?, pending_question = NULL, "
                         "pending_card = NULL WHERE id = ?", (end_reason, at, request_id))
            conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        else:
            conn.execute("UPDATE request SET pending_card = ?, pending_question = ? WHERE id = ?",
                         (card, json.dumps(question) if question else None, request_id))
        if serial is not None:
            conn.execute("UPDATE conversation SET serial = MAX(serial, ?) WHERE id = ?", (serial, c.conversation_id))
        for event in events or []:
            conn.execute("INSERT INTO request_event (request_id, turn_id, event, at) VALUES (?, 'reconcile', ?, ?)",
                         (request_id, json.dumps(event, default=str), _stamp(_now())))
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

def finalise(c: Claim, request_id: str, turn_events: list[dict], *, pending_question: dict | None,
             pending_card: str | None, state: str, why: str | None, serial_now: int, bindings_now: dict,
             intent_state: str) -> bool:
    """Write the turn's outcome in one transaction, only while the claim is still this turn's and the version
    unchanged: its events, the request's pending question, card and state, its bindings, the intent, the version,
    and the claim released. False writes nothing."""
    at = _now()
    with _tx() as conn:
        row = conn.execute("SELECT version FROM conversation WHERE id = ?", (c.conversation_id,)).fetchone()
        if not _holds(conn, c, at) or row["version"] != c.version:
            return False
        for e in turn_events:
            conn.execute("INSERT INTO request_event (request_id, turn_id, event, at) VALUES (?, ?, ?, ?)",
                         (request_id, c.turn_id, json.dumps(e, default=str), _stamp(at)))
        ended = state == "ended"
        conn.execute("UPDATE request SET state = ?, ended_why = ?, ended_at = ?, pending_question = ?, pending_card = ? "
                     "WHERE id = ?", (state, why, _stamp(at) if ended else None,
                                      None if ended or not pending_question else json.dumps(pending_question),
                                      None if ended else pending_card, request_id))
        conn.execute("DELETE FROM request_binding WHERE request_id = ?", (request_id,))
        if not ended:  # bindings end with their request
            conn.executemany("INSERT INTO request_binding (request_id, path, value) VALUES (?, ?, ?)",
                             [(request_id, p, json.dumps(v)) for p, v in bindings_now.items()])
        conn.execute("UPDATE conversation SET version = version + 1, serial = max(serial, ?), claimed_by = NULL, "
                     "claim_until = NULL, intent_state = CASE WHEN intent_state = 'open' THEN ? ELSE intent_state END "
                     "WHERE id = ?", (serial_now, intent_state, c.conversation_id))
        return True


def add_events(request_id: str, turn_events: list[dict], turn_id: str = "outside") -> None:
    """Events that happened outside a turn (an approval's outcome shown to the owner), kept as the request's history."""
    with _tx() as conn:
        conn.executemany("INSERT INTO request_event (request_id, turn_id, event, at) VALUES (?, ?, ?, ?)",
                         [(request_id, turn_id, json.dumps(e, default=str), _stamp(_now())) for e in turn_events])


def timeline(request_id: str) -> list[tuple[str, object, int]]:
    """The request's owner messages and engine events in the order they happened: ("owner", text, message id) or
    ("engine", event, event id)."""
    with _tx() as conn:
        said = [(r["at"], 0, r["id"], ("owner", r["text"], r["id"])) for r in conn.execute(
            "SELECT id, text, at FROM request_message WHERE request_id = ?", (request_id,))]
        did = [(r["at"], 1, r["id"], ("engine", json.loads(r["event"]), r["id"])) for r in conn.execute(
            "SELECT id, event, at FROM request_event WHERE request_id = ?", (request_id,))]
    return [x[3] for x in sorted(said + did)]
