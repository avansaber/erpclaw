"""Host-only exchange and sign-out; credentials are stored only as domain-separated hashes."""
from __future__ import annotations

import secrets
from datetime import timedelta
from threading import Lock
from weakref import WeakKeyDictionary

from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from erpclaw_core import models as m, principals as pr
from erpclaw_core.common import Refusal, audit, company_ctx, new_id, now

IDLE = timedelta(hours=12)
LIFETIME = timedelta(days=7)
INVALID = "That sign-in link is not valid any more. Ask for a new one."


class SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sign_in_code: str = Field(strict=True, min_length=26, max_length=26)


class _Attempts:
    def __init__(self):
        self.lock = Lock()
        self.minute = None
        self.failed = 0


# App instances using the same engine share its failure budget without extending the engine's lifetime.
_attempts = WeakKeyDictionary()
_attempts_lock = Lock()


def credentials(request: Request) -> tuple[str, str]:
    """A call has at most one credential; combined and repeated headers fail closed."""
    values = request.headers.getlist("authorization")
    if len(values) > 1 or (values and "," in values[0]):
        raise Refusal("Sign in to use the engine.", "unauthenticated")
    scheme, _, value = (values[0] if values else "").partition(" ")
    return scheme.lower(), value


def live_session(conn, secret: str, at):
    """Look up a live session and its principal afresh, without updating activity."""
    if not secret:
        return None
    s = m.principal_session
    row = conn.execute(select(s).where(
        s.c.secret_sha256 == pr.session_digest(secret), s.c.ended_at.is_(None),
        s.c.idle_expires_at > at, s.c.expires_at > at)).mappings().first()
    if row is None:
        return None
    who = pr.by_id(conn, row["principal_id"])
    return None if who["disabled"] else (row, who)


def authenticate(conn, secret: str) -> dict:
    """Authenticate and, at most once a minute, advance only the session's activity columns."""
    at = now()
    found = live_session(conn, secret, at)
    if found is None:
        raise Refusal("Sign in to use the engine.", "unauthenticated")
    row, who = found
    threshold = at - timedelta(minutes=1)
    if row["last_seen_at"] <= threshold:
        s = m.principal_session
        # Recheck under the UPDATE's row lock: concurrent calls cannot both refresh this minute.
        conn.execute(update(s).where(
            s.c.id == row["id"], s.c.last_seen_at <= threshold, s.c.ended_at.is_(None),
            s.c.idle_expires_at > at, s.c.expires_at > at).values(
                last_seen_at=at, idle_expires_at=func.least(at + IDLE, s.c.expires_at)))
    return who


def _audit(conn, action, who, session_id):
    cid = conn.execute(select(m.company.c.id).order_by(m.company.c.id).limit(1)).scalar()
    audit(company_ctx(conn, actor=who["name"], company_id=cid), action, "principal", who["id"],
          detail={"principal": who["name"], "session_id": session_id})


def register_session(app, engine, *, run_id):
    # Imported here so the app can register this module without a circular import.
    from .app import Refused

    with _attempts_lock:
        attempts = _attempts.setdefault(engine, _Attempts())

    def refused(status, detail, code):
        return Refused(status, Refusal(detail, code))

    def host_only(request: Request):
        """These routes exist on the host listener only. Anywhere else (the app served raw, in process or by
        `serve` over HTTP, or on the bearer listeners) they answer the frozen not-found before the body is read."""
        if request.scope.get("erpclaw_listener") != "host":
            raise refused(404, "There is no such route.", "not_found")

    @app.post("/v1/session", openapi_extra={"erpclaw_access": "app"}, dependencies=[Depends(host_only)])
    def sign_in(body: SignIn, request: Request):
        try:
            scheme, value = credentials(request)
        except Refusal as r:
            raise Refused(401, r) from None
        # Serializing the budget check and exchange prevents parallel failures exceeding the limit.
        with attempts.lock:
            at = now()
            minute = int(at.timestamp()) // 60
            if attempts.minute != minute:
                attempts.minute, attempts.failed = minute, 0
            if attempts.failed >= 10:
                raise refused(429, "Too many sign-in attempts. Try again in a minute.", "too_many_attempts")
            try:
                with engine.begin() as conn:
                    if scheme == "bearer" or (scheme == "session" and live_session(conn, value, at)):
                        raise refused(400, "Sign out before signing in again.", "already_signed_in")
                    t = m.principal_sign_in
                    rows = conn.execute(update(t).where(
                        t.c.code_sha256 == pr.code_digest(body.sign_in_code), t.c.used_at.is_(None),
                        t.c.expires_at > func.clock_timestamp(), t.c.expires_at > at
                    ).values(used_at=func.clock_timestamp()).returning(t.c.principal_id, t.c.run_id)).mappings().all()
                    if len(rows) != 1:
                        raise refused(401, INVALID, "sign_in_code_invalid")
                    code = rows[0]
                    who = pr.by_id(conn, code["principal_id"])
                    if who["disabled"] or (code["run_id"] is not None and code["run_id"] != run_id):
                        # Raising rolls back consumption along with any other work in this transaction.
                        raise refused(401, INVALID, "sign_in_code_invalid")
                    secret, sid = secrets.token_urlsafe(32), new_id()
                    conn.execute(m.principal_session.insert().values(
                        id=sid, secret_sha256=pr.session_digest(secret), principal_id=who["id"],
                        created_at=at, last_seen_at=at, idle_expires_at=at + IDLE, expires_at=at + LIFETIME,
                        ended_at=None, ended_why=None, user_agent=request.headers.get("user-agent", "")[:200]))
                    _audit(conn, "sign_in", who, sid)
                return {"name": who["name"], "session_secret": secret}
            except Refused as r:
                if r.refusal.code == "sign_in_code_invalid":
                    attempts.failed += 1
                raise

    @app.delete("/v1/session", status_code=204, openapi_extra={"erpclaw_access": "app"}, dependencies=[Depends(host_only)])
    def sign_out(request: Request):
        try:
            scheme, value = credentials(request)
        except Refusal:
            return Response(status_code=204)
        if scheme == "session":
            with engine.begin() as conn:
                at = now()
                found = live_session(conn, value, at)
                if found:
                    row, who = found
                    s = m.principal_session
                    ended = conn.execute(update(s).where(
                        s.c.id == row["id"], s.c.ended_at.is_(None),
                        s.c.idle_expires_at > at, s.c.expires_at > at
                    ).values(ended_at=at, ended_why="sign_out").returning(s.c.id)).scalar_one_or_none()
                    if ended:
                        _audit(conn, "sign_out", who, ended)
        return Response(status_code=204)
