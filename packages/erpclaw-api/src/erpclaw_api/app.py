"""Engine API. Both chat windows (CLI now, browser later) use only this.

Protected calls authenticate a principal with `Authorization: Session <secret>`
on the host listener or `Authorization: Bearer <token>` on bearer listeners.
The embedded CLI builds the app
in-process with `embedded_owner=True`, so a call without a token is the owner
there; `serve` never does. A message is only a message: no text approves
anything. A card is approved by POST /v1/cards/{id}/approve with its digest, by
a principal holding `approve` and `post` on its company.

Clients ignore an event type or response field they do not know: within v1 the
engine adds events and fields, never removes or renames them. Request bodies
refuse undeclared fields. HTTP errors carry owner-facing detail and a code;
validation errors also name the failing fields without echoing their values.
A 500 internal_error means a failure before any commit; the request's own transaction rolled back.
A queued message is sent at the next delivery pass when due and delivery is enabled; this app runs delivery
only after a card approval.
"""
from __future__ import annotations

import logging
import json

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Model
from erpclaw_core import approvals, models as m
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, check_events, is_question, new_id, public_data
from erpclaw_core.db import read_only
from erpclaw_core.files import check_root
from erpclaw_core.grants import validate_runtime_session
from erpclaw_core.outbox import deliver_pending
from erpclaw_core.plugins import check_installed, load_plugins

from . import session
from .me import register_me
from .stream import Streams

log = logging.getLogger(__name__)


class Body(BaseModel):
    """Request bodies accept only the fields their route declares."""
    model_config = ConfigDict(extra="forbid")


class Message(Body):
    text: str = Field(min_length=1, max_length=4000)
    choice: str | None = Field(default=None, min_length=1, max_length=200)  # the code of a choice the engine issued
    client_message_id: str | None = None


class Approval(Body):
    digest: str = Field(min_length=64, max_length=64)


INTERNAL = "The engine hit an internal error."
HTTP_REFUSALS = {
    401: ("unauthenticated", "Sign in to use the engine."),
    403: ("forbidden", "You may not do that."),
    404: ("not_found", "There is no such route."),
    405: ("method_not_allowed", "That route does not take this method."),
    422: ("invalid_request", "That request is not valid; check the named fields."),
    500: ("internal_error", INTERNAL),
}


class Refused(Exception):
    """A core refusal answered with an HTTP status and its original code."""

    def __init__(self, status: int, refusal: Refusal):
        super().__init__(refusal.message)
        self.status, self.refusal = status, refusal


def refusal_status(r: Refusal) -> int:
    return {"forbidden": 403, "no_card": 404, "no_conversation": 404}.get(r.code, 409)


def error(status: int, detail: str, code: str, headers: dict | None = None, **more) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code, **more}, status_code=status, headers=headers)


def create_app(engine: Engine, model: Model, *, embedded_owner: bool = False,
               run_id: str | None = None) -> FastAPI:
    load_plugins()
    validate_runtime_session(engine)  # PostgreSQL: the runtime role, no owner authority
    check_installed(engine)  # the books' plugin installs must match this engine, or they do not open
    check_root(engine)  # books with stored files open only with their own files
    # No interactive documentation: /docs, /redoc and /openapi.json would show the API to an unauthenticated caller on the
    # host pipe, and the app never needs them.
    app = FastAPI(title="ERPClaw", version="0.0.1", redirect_slashes=False,
                  docs_url=None, redoc_url=None, openapi_url=None)
    assistant = Assistant(engine, model)
    app.state.engine, app.state.assistant = engine, assistant
    streams = getattr(engine, "_erpclaw_streams", None)
    if streams is None:
        streams = engine._erpclaw_streams = Streams()
    engine._erpclaw_decision_emit = streams.emit

    @app.exception_handler(Refused)
    def refused(request: Request, e: Refused) -> JSONResponse:
        return error(e.status, e.refusal.message, e.refusal.code)

    @app.exception_handler(Refusal)
    def core_refused(request: Request, e: Refusal) -> JSONResponse:
        return error(refusal_status(e), e.message, e.code)

    @app.exception_handler(HTTPException)
    def http_refused(request: Request, e: HTTPException) -> JSONResponse:
        code, detail = HTTP_REFUSALS.get(e.status_code, ("http_error", "That request was refused."))
        return error(e.status_code, detail, code, e.headers, **({"fields": []} if e.status_code == 422 else {}))

    @app.exception_handler(RequestValidationError)
    def invalid(request: Request, e: RequestValidationError) -> JSONResponse:
        # Validator messages and inputs can contain private values; expose only the distinct field paths.
        fields = [list(loc) for loc in dict.fromkeys(tuple(x["loc"]) for x in e.errors())]
        code, detail = HTTP_REFUSALS[422]
        return error(422, detail, code, fields=fields)

    @app.exception_handler(Exception)
    def internal(request: Request, e: Exception) -> JSONResponse:
        # The request's own transaction unwinds before this response is built.
        return error(500, INTERNAL, "internal_error")

    def principal(request: Request, authorization: str | None = Header(default=None)) -> dict:
        """Authenticate using the listener's one credential mode and the current principal row."""
        listener = request.scope.get("erpclaw_listener")
        try:
            scheme, token = session.credentials(request)
            if listener == "host":
                if scheme != "session":
                    raise Refusal("Sign in to use the engine.", "unauthenticated")
                with engine.begin() as conn:
                    return session.authenticate(conn, token)
            with read_only(engine) as conn:
                if authorization is None and embedded_owner and listener is None:
                    return pr.owner(conn)
                return pr.authenticate(conn, token if scheme == "bearer" else None)
        except Refusal as r:
            raise Refused(401, r) from None

    register_me(app, engine, model, principal)
    session.register_session(app, engine, run_id=run_id)

    def outcome_events(o: approvals.Outcome) -> list[dict]:
        if o.status == "superseded" and o.card:  # the stale path's fresh card, which replaces the approved one
            return [{"type": "refused", "code": "card_changed", "text": o.text}, *approvals.card_events(o)]
        code = o.data.get("code")
        # a card that ended on a refusal, a question or not, and a refusal inside the effect that is a question:
        # the engine's question, with its options and its public data (choices by code and label, never a ref)
        if code and (o.status == "superseded" or (o.status == "refused" and is_question(code))):
            data = public_data(o.data.get("data") or {})
            ev = {"type": "ask" if is_question(code) else "refused", "status": o.status, "text": o.text,
                  "code": code, "options": o.data.get("options", []), "data": data,
                  **({"capability": o.data["capability"]} if o.data.get("capability") else {})}
            if ev["type"] == "ask":  # the question's fields, public choices only
                ev |= {"question_id": data.get("question_id") or data.get("question"), "kind": data.get("kind"),
                       "choices": data.get("choices") or []}
            return [ev]
        ev = [{"type": "result", "status": o.status, "text": o.text, "operation_key": o.operation_key,
               "amounts": o.amounts}]
        if o.status == "refused" and code:  # a refusal inside the commit: its code, and what it refused if not offered
            ev[0]["code"] = code
            if o.data.get("capability"):
                ev[0]["capability"] = o.data["capability"]
        return ev

    def card_response(o: approvals.Outcome, aid: str, conversation_id: str | None, currency: str | None) -> dict:
        """A failed delivery returns immediately and skips the conversation history note."""
        evs = outcome_events(o)
        if o.status == "committed":
            try:
                currency = approvals.books_currency(engine, approval_id=aid)
            except Exception as e:
                log.warning("Currency follow-up did not finish (%s).", type(e).__name__)
                currency = currency or o.data.get("currency")
            try:
                for d in deliver_pending(engine):
                    if d["operation_key"] == o.operation_key:
                        evs.append({"type": "email", "status": d["status"],
                                    "text": f"Email to {d['recipient']}: {d['text']}"})
            except Exception as e:
                log.warning("Email follow-up did not finish (%s).", type(e).__name__)
                text = "It's saved. Sending the email didn't finish."
                try:
                    with read_only(engine) as conn:
                        queued = conn.execute(select(m.outbox.c.id).where(
                            m.outbox.c.operation_key == o.operation_key, m.outbox.c.status == "queued").limit(1)).first()
                    if queued:
                        text = "It's saved. Sending the email didn't finish; the message is still waiting to be sent."
                except Exception:
                    pass  # An unavailable queue cannot justify claiming the message is waiting.
                evs.append({"type": "answer", "text": text})
                return {"events": approvals.with_currency(check_events(evs), currency)}
        try:
            # A card's own conversation takes precedence over the one supplied by the caller.
            target = approvals.conversation_of(engine, aid) or conversation_id
            if target:
                ask = next((e for e in evs if e["type"] == "ask"), None)
                if ask:
                    assistant.note(target, ask["text"], question={"code": ask["code"], "options": ask["options"],
                                                                  "data": o.data.get("data") or {}})  # refs kept
                else:
                    assistant.note(target, " ".join(e["text"] for e in evs if e.get("text")))
        except Exception as e:
            log.warning("Conversation history follow-up did not finish (%s).", type(e).__name__)
            prefix = "It's saved. " if o.status == "committed" else ""
            evs.append({"type": "answer", "text": prefix + "Adding it to this conversation's history "
                        "didn't finish; nothing else is affected."})
        return {"events": approvals.with_currency(check_events(evs), currency)}

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    @app.post("/v1/conversations")
    def new_conversation(body: Body | None = None, who: dict = Depends(principal)):
        cid = new_id()
        store.create_conversation(cid, who["id"])
        return {"id": cid}

    @app.post("/v1/conversations/{cid}/messages")
    def message(cid: str, body: Message, who: dict = Depends(principal)):
        try:
            events = assistant.turn(cid, body.text.strip(), principal=who, choice=body.choice, create=False,
                                    client_message_id=body.client_message_id)
        except Refusal as r:
            if r.code in ("forbidden", "no_conversation", "client_message_id_reused"):
                raise Refused(refusal_status(r), r) from None
            # a busy conversation (one turn at a time) is refused with its code, and changes nothing
            events = [{"type": "refused", "code": r.code, "text": r.message,
                       **({"capability": r.capability} if r.capability else {})}]
        # a refusal of something not offered always names it; every event names the books' currency
        return {"events": approvals.with_currency(check_events(events), approvals.books_currency(engine))}

    def check_card_owner(who: dict, *, aid: str | None = None, key: str | None = None) -> None:
        with read_only(engine) as conn:
            card_id = conn.execute(select(m.approval.c.id).where(
                m.approval.c.id == aid if aid is not None else m.approval.c.operation_key == key)).scalar()
        cid = approvals.conversation_of(engine, card_id or "")
        owner = store.conversation_owner(cid or "")
        if card_id is None or (cid is not None and owner != who["id"]):
            raise Refusal("That conversation was not found.", "no_conversation")

    def check_unbound_target(aid: str, conversation_id: str | None, who: dict) -> None:
        if (conversation_id is not None and not approvals.conversation_of(engine, aid)
                and store.conversation_owner(conversation_id) != who["id"]):
            raise Refusal("That conversation was not found.", "no_conversation")

    @app.get("/v1/conversations/{cid}/stream")
    def conversation_stream(cid: str, request: Request, after: str = "0", who: dict = Depends(principal)):
        if store.conversation_owner(cid) != who["id"]:
            raise Refusal("That conversation was not found.", "no_conversation")
        cursor_text = request.headers.get("last-event-id", after)
        if len(cursor_text) > 19 or not cursor_text.isascii() or not cursor_text.isdecimal():
            return error(422, "That request is not valid; check the named fields.", "invalid_request", fields=[])
        after = int(cursor_text)
        if after > 9223372036854775807:
            return error(422, "That request is not valid; check the named fields.", "invalid_request", fields=[])
        key = streams.subscribe(who["id"], cid)
        if key is None:
            return error(429, "Too many open streams.", "stream_limit")
        listener = request.scope.get("erpclaw_listener")
        scheme, token = session.credentials(request)
        embedded = request.headers.get("authorization") is None and embedded_owner and listener is None

        def authorized():
            try:
                with read_only(engine) as conn:
                    if listener == "host":
                        found = session.live_session(conn, token, session.now()) if scheme == "session" else None
                        current = found[1] if found else None
                    elif embedded:
                        current = pr.owner(conn)
                        if current["disabled"]:
                            return False
                    else:
                        current = pr.authenticate(conn, token if scheme == "bearer" else None)
                    return current is not None and current["id"] == who["id"]
            except Refusal:
                return False

        return streams.response(key, cid, after, who["id"], authorized)

    @app.get("/v1/cards/{aid}")
    def read_card(aid: str, who: dict = Depends(principal)):
        check_card_owner(who, aid=aid)
        with read_only(engine) as conn:
            card = approvals._card(conn, aid)
            approvals._check_ownership(card["conversation_id"],
                                       lambda cid: store.conversation_owner(cid) == who["id"])
            pr.require(conn, pr.by_id(conn, who["id"]), card["company_id"], "view")
            status = approvals._status_now(conn, card)
        return {"approval_id": aid, "status": status, "card": json.loads(card["card_json"]),
                "expires_at": card["expires_at"].isoformat(), "conversation_id": card["conversation_id"],
                **({"digest": card["digest"]} if status == "pending" else {})}

    @app.post("/v1/cards/{aid}/approve")
    def approve(aid: str, body: Approval, conversation_id: str | None = None, who: dict = Depends(principal)):
        try:
            check_card_owner(who, aid=aid)
            check_unbound_target(aid, conversation_id, who)
            currency = approvals.books_currency(engine, approval_id=aid)
            o = approvals.approve(engine, aid, body.digest, principal=who, actor=who["name"],
                                  ownership_check=lambda cid: store.conversation_owner(cid) == who["id"])
        except Refusal as r:
            raise Refused(refusal_status(r), r) from None
        return card_response(o, aid, conversation_id, currency)

    @app.post("/v1/cards/{aid}/decline")
    def decline(aid: str, body: Body | None = None, conversation_id: str | None = None,
                who: dict = Depends(principal)):
        try:
            check_card_owner(who, aid=aid)
            check_unbound_target(aid, conversation_id, who)
            currency = approvals.books_currency(engine, approval_id=aid)
            o = approvals.decline(engine, aid, principal=who,
                                  ownership_check=lambda cid: store.conversation_owner(cid) == who["id"])
        except Refusal as r:
            raise Refused(refusal_status(r), r) from None
        return card_response(o, aid, conversation_id, currency)

    @app.get("/v1/operations/{key}")
    def operation(key: str, who: dict = Depends(principal)):
        try:
            check_card_owner(who, key=key)
            o = approvals.read_back(engine, key, principal=who,
                                    ownership_check=lambda cid: store.conversation_owner(cid) == who["id"])
        except Refusal as r:
            raise Refused(refusal_status(r), r) from None
        return {"status": o.status, "text": o.text, "amounts": o.amounts,
                "currency": approvals.books_currency(engine, key=key)}

    return app
