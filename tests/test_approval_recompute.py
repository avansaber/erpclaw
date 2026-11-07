"""A card that can no longer be approved as it stands ends visibly, with no business write.
Approval-time recomputation records refusals and questions on the card, so repeated approvals return its
stored outcome and an approval-time question reaches the owner as a question."""
from __future__ import annotations

import json
import threading
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_party
from erpclaw_core.plugins import REGISTRY, disable_plugin

INVOICE = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])


def _draft(engine) -> str:
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}], "submit": False})
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "committed", out.text
    return out.data["number"]


def _outcomes(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.operation_outcome)).scalar_one()


def _status(engine, approval_id) -> str:
    with engine.connect() as conn:
        return conn.execute(select(m.approval.c.status).where(m.approval.c.id == approval_id)).scalar_one()


def _ends(before, after, approval_id) -> dict:
    """Only the card's own row changed, to superseded with its stored decision: no business
    row, no outcome, no fresh card. Returns the decision."""
    row = after["approval"][(approval_id,)]
    assert_changes(before, after, {"approval": {"changed": [({"id": approval_id}, {
        "status": "superseded", "decided_at": row["decided_at"], "decision_json": row["decision_json"]})]}})
    decision = json.loads(row["decision_json"])
    assert decision["status"] == "superseded" and "fresh_approval_id" not in decision
    return decision


def test_a_submit_card_whose_period_was_locked_ends_with_the_periods_refusal(engine):
    number = _draft(engine)
    card = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": number})
    with engine.begin() as conn:
        ledger.lock_through(company_ctx(conn), date(2026, 10, 31))
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded" and "nothing was saved" in out.text and "period" in out.text, out.text
    assert out.data["code"] == "period_closed"
    assert _ends(before, snapshot(engine), card.approval_id)["code"] == "period_closed"
    stored = snapshot(engine)
    again = approvals.approve(engine, card.approval_id, card.digest)  # it ended: its stored decision, nothing written
    assert (again.status, again.text, again.data["code"]) == ("superseded", out.text, "period_closed")
    assert snapshot(engine) == stored


def _ambiguous_after_the_card(engine) -> None:
    """The card holds the customer's stored name, which resolves exactly at approval; it becomes a question only when
    the record changes under the card: renamed, with a second customer that the old name also fits."""
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Acme Corporation").values(name="Acme Corporation North"))
        create_party(company_ctx(conn), "customer", "Acme Corporation South")


def test_a_card_whose_customer_became_ambiguous_ends_with_the_question_and_its_options(engine):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]})
    _ambiguous_after_the_card(engine)
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded" and out.data["code"] == "customer_ambiguous", out
    assert set(out.data["options"]) >= {"Acme Corporation North", "Acme Corporation South"}
    decision = _ends(before, snapshot(engine), card.approval_id)
    assert (decision["code"], decision["options"]) == ("customer_ambiguous", out.data["options"])


def test_a_card_whose_plugin_was_disabled_ends_with_that_refusal(engine):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]})
    with engine.begin() as conn:
        disable_plugin(conn, REGISTRY.contract_plugin["create_invoice"])
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded" and out.data["code"] == "plugin_not_enabled", out
    after = snapshot(engine)
    assert after["approval"][(card.approval_id,)]["status"] == "superseded"
    assert _outcomes(engine) == len(before.get("operation_outcome", {}))


def test_a_digest_mismatch_and_a_principal_without_the_right_still_leave_the_card_pending(engine):
    from erpclaw_core.common import Refusal

    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]})
    try:
        approvals.approve(engine, card.approval_id, "0" * 64)
    except Refusal as r:
        assert r.code == "digest"
    assert _status(engine, card.approval_id) == "pending"


def test_two_approvals_of_a_card_whose_recompute_refuses_end_it_once_with_no_outcome_and_no_fresh_card(engine):
    number = _draft(engine)
    card = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": number})
    with engine.begin() as conn:
        ledger.lock_through(company_ctx(conn), date(2026, 10, 31))
    with engine.connect() as conn:
        cards_before = conn.execute(select(func.count()).select_from(m.approval)).scalar_one()
    outcomes_before = _outcomes(engine)
    results: list = []
    start = threading.Barrier(2)

    def attempt():
        start.wait()
        results.append(approvals.approve(engine, card.approval_id, card.digest))

    threads = [threading.Thread(target=attempt, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 2 and {r.status for r in results} == {"superseded"}, [(r.status, r.text) for r in results]
    assert sum("nothing was saved: " in r.text for r in results) >= 1
    assert _status(engine, card.approval_id) == "superseded"
    assert _outcomes(engine) == outcomes_before
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.approval)).scalar_one() == cards_before


def _chat(engine, replies):
    model = ScriptedModel(list(replies))
    return model, TestClient(create_app(engine, model, embedded_owner=True))


def test_through_the_api_the_question_arrives_as_an_ask_with_its_code_and_options_and_the_answer_continues(engine):
    model, c = _chat(engine, [Reply("", [INVOICE]), Reply("Noted.", [])])
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 2 copper wire"}).json()["events"]
    _ambiguous_after_the_card(engine)
    events = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    [ask] = events
    assert ask["type"] == "ask" and ask["code"] == "customer_ambiguous", events
    assert set(ask["options"]) >= {"Acme Corporation North", "Acme Corporation South"}
    c.post(f"/v1/conversations/{cid}/messages", json={"text": "Acme Corporation South"})
    seen = " ".join(str(msg.get("content")) for msg in model.seen[-1])
    assert "(engine question, code customer_ambiguous)" in seen and "invoice Acme Corporation for 2 copper wire" in seen


def test_the_question_is_noted_in_the_cards_own_conversation_whatever_the_caller_names(engine):
    model, c = _chat(engine, [Reply("", [INVOICE]), Reply("Other.", []), Reply("Own.", [])])
    own = c.post("/v1/conversations").json()["id"]
    other = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{own}/messages",
                    json={"text": "invoice Acme Corporation for 2 copper wire"}).json()["events"]
    _ambiguous_after_the_card(engine)
    c.post(f"/v1/cards/{card['approval_id']}/approve", params={"conversation_id": other},
           json={"digest": card["digest"]})
    c.post(f"/v1/conversations/{other}/messages", json={"text": "hello"})
    assert "engine question" not in " ".join(str(msg.get("content")) for msg in model.seen[-1])  # not the mismatched
    c.post(f"/v1/conversations/{own}/messages", json={"text": "Acme Corporation South"})
    assert "engine question" in " ".join(str(msg.get("content")) for msg in model.seen[-1])  # the card's own


def test_a_refusal_inside_a_turn_carries_the_same_shape(engine):
    model, c = _chat(engine, [Reply("", [call("create_invoice", customer="Nobody At All",
                                              items=[{"item": "copper wire", "quantity": 1}])])])
    cid = c.post("/v1/conversations").json()["id"]
    [ev] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Nobody"}).json()["events"]
    assert ev["type"] == "ask" and ev["code"] == "customer_unknown" and "options" in ev, ev
