"""Engine API. Both chat windows (CLI now, browser later) use only this.

Every call but /v1/health is made by an authenticated principal: `Authorization:
Bearer <token>`. The embedded CLI builds the app
in-process with `embedded_owner=True`, so a call without a token is the owner
there; `serve` never does. A message is only a message: no text approves
anything. A card is approved by POST /v1/cards/{id}/approve with its digest, by
a principal holding `approve` and `post` on its company.

Clients ignore an event type or field they do not know: within v1 the engine
adds events and fields, never removes or renames them.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Model
from erpclaw_core import approvals
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, check_events, is_question, new_id, public_data
from erpclaw_core.db import read_only
from erpclaw_core.files import check_root
from erpclaw_core.grants import validate_runtime_session
from erpclaw_core.outbox import deliver_pending
from erpclaw_core.plugins import check_installed, load_plugins


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    choice: str | None = Field(default=None, min_length=1, max_length=200)  # the code of a choice the engine issued


class Approval(BaseModel):
    digest: str = Field(min_length=64, max_length=64)


def create_app(engine: Engine, model: Model, *, embedded_owner: bool = False) -> FastAPI:
    load_plugins()
    validate_runtime_session(engine)  # PostgreSQL: the runtime role, no owner authority
    check_installed(engine)  # the books' plugin installs must match this engine, or they do not open
    check_root(engine)  # books with stored files open only with their own files
    app = FastAPI(title="ERPClaw", version="0.0.1")
    assistant = Assistant(engine, model)
    app.state.engine, app.state.assistant = engine, assistant

    def principal(authorization: str | None = Header(default=None)) -> dict:
        """The authenticated principal of this call, as seen now (its grants version is rechecked at commit)."""
        with read_only(engine) as conn:
            if authorization is None and embedded_owner:
                return pr.owner(conn)
            scheme, _, token = (authorization or "").partition(" ")
            try:
                return pr.authenticate(conn, token if scheme.lower() == "bearer" else None)
            except Refusal as r:
                raise HTTPException(401, r.message) from None

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
        if o.status == "committed":
            for d in deliver_pending(engine):
                if d["operation_key"] == o.operation_key:
                    ev.append({"type": "email", "status": d["status"],
                               "text": f"Email to {d['recipient']}: {d['text']}"})
        return ev

    @app.get("/v1/health")
    def health():
        return {"ok": True, "model": getattr(model, "name", "unknown")}

    @app.post("/v1/conversations")
    def new_conversation(who: dict = Depends(principal)):
        return {"id": new_id()}

    @app.post("/v1/conversations/{cid}/messages")
    def message(cid: str, body: Message, who: dict = Depends(principal)):
        try:
            events = assistant.turn(cid, body.text.strip(), principal=who, choice=body.choice)
        except Refusal as r:
            if r.code == "forbidden":  # no view on these books: nothing of them is shown
                raise HTTPException(403, r.message) from None
            # a busy conversation (one turn at a time) is refused with its code, and changes nothing
            events = [{"type": "refused", "code": r.code, "text": r.message,
                       **({"capability": r.capability} if r.capability else {})}]
        # a refusal of something not offered always names it; every event names the books' currency
        return {"events": approvals.with_currency(check_events(events), approvals.books_currency(engine))}

    @app.post("/v1/cards/{aid}/approve")
    def approve(aid: str, body: Approval, conversation_id: str | None = None, who: dict = Depends(principal)):
        try:
            o = approvals.approve(engine, aid, body.digest, principal=who, actor=who["name"])
            evs = approvals.with_currency(check_events(outcome_events(o)),  # a refusal not offered always names it
                                          approvals.books_currency(engine, approval_id=aid))
        except Refusal as r:
            raise HTTPException(403 if r.code == "forbidden" else 409, r.message) from None
        # the card's own conversation, whatever the caller names; a card made outside any
        # conversation is noted into the one supplied, if any
        target = approvals.conversation_of(engine, aid) or conversation_id
        if target:
            ask = next((e for e in evs if e["type"] == "ask"), None)
            if ask:
                assistant.note(target, ask["text"], question={"code": ask["code"], "options": ask["options"],
                                                              "data": o.data.get("data") or {}})  # refs kept
            else:
                assistant.note(target, " ".join(e["text"] for e in evs if e.get("text")))
        return {"events": evs}

    @app.post("/v1/cards/{aid}/decline")
    def decline(aid: str, conversation_id: str | None = None, who: dict = Depends(principal)):
        try:
            evs = approvals.with_currency(check_events(outcome_events(approvals.decline(engine, aid, principal=who))),
                                          approvals.books_currency(engine, approval_id=aid))
        except Refusal as r:
            raise HTTPException(403 if r.code == "forbidden" else 409, r.message) from None
        if conversation_id:
            assistant.note(conversation_id, " ".join(e["text"] for e in evs if e.get("text")))
        return {"events": evs}

    @app.get("/v1/operations/{key}")
    def operation(key: str, who: dict = Depends(principal)):
        try:
            o = approvals.read_back(engine, key, principal=who)
        except Refusal as r:
            raise HTTPException(403, r.message) from None
        return {"status": o.status, "text": o.text, "amounts": o.amounts,
                "currency": approvals.books_currency(engine, key=key)}

    return app
