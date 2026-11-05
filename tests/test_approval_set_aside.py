"""Setting a pending card aside: a turn that ends with a question, a confirm, a refusal or an answer while a card is
pending supersedes that card with the turn's own decision stored, under the conversation's live token. An approval
racing it on the card's row: exactly one wins, and the other is told what happened. PostgreSQL only."""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as t

INVOICE = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}
QUESTION = Refusal("Do you mean Wayne Enterprises?", "customer_ambiguous", ["Wayne Enterprises", "someone else"],
                   field="customer", question_id="q3",
                   choices=[{"code": "q3-1", "label": "Wayne Enterprises"}, {"code": "q3-2", "label": "someone else"}])


def pending(engine, generation=1):
    """A conversation fenced at `generation` with one pending card under its token."""
    conv, token = new_id(), new_id()
    with engine.connect() as conn:
        company = company_ctx(conn).company_id
    assert approvals.activate(engine, conv, company, generation, token).status == "active"
    out = approvals.propose(engine, REGISTRY.contracts["create_invoice"], INVOICE, conv, request_card_token=token)
    return conv, token, out


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def status(engine, aid) -> str:
    with engine.connect() as conn:
        return conn.execute(select(m.approval.c.status).where(m.approval.c.id == aid)).scalar_one()


def test_a_set_aside_card_is_superseded_with_the_turn_s_question_and_a_repeat_approval_returns_it(engine):
    conv, token, card = pending(engine)
    out = approvals.set_aside(engine, conv, token, card.approval_id, QUESTION.message, QUESTION)
    assert out.status == "set_aside" and out.data["title"] == card.text, out
    assert status(engine, card.approval_id) == "superseded"
    invoices = count(engine, t.invoice)
    for _ in range(2):  # the same question every time, unchanged, and nothing written
        again = approvals.approve(engine, card.approval_id, card.digest)
        assert again.status == "superseded" and again.text == QUESTION.message, again
        assert again.data["code"] == "customer_ambiguous" and again.data["options"] == QUESTION.options
        assert again.data["data"]["choices"] == QUESTION.data["choices"], again.data
    assert count(engine, t.invoice) == invoices


def test_a_refusal_or_an_answer_is_stored_as_its_text(engine):
    conv, token, card = pending(engine)
    text = "ERPClaw does not do that yet."
    assert approvals.set_aside(engine, conv, token, card.approval_id, text, None).status == "set_aside"
    again = approvals.approve(engine, card.approval_id, card.digest)
    assert again.status == "superseded" and again.text == text and "code" not in again.data, again


def test_a_stale_token_is_refused_and_the_card_stays(engine):
    conv, token, card = pending(engine)
    with engine.connect() as conn:
        company = company_ctx(conn).company_id
    newer = new_id()
    assert approvals.activate(engine, conv, company, 2, newer).status == "active"  # a takeover: the card is retired
    with pytest.raises(Refusal) as r:
        approvals.set_aside(engine, conv, token, card.approval_id, QUESTION.message, QUESTION)
    assert r.value.code == "fenced"
    conv2, token2, card2 = pending(engine)
    with pytest.raises(Refusal) as r:
        approvals.set_aside(engine, conv2, new_id(), card2.approval_id, QUESTION.message, QUESTION)
    assert r.value.code == "fenced" and status(engine, card2.approval_id) == "pending"


def test_a_card_of_another_conversation_is_never_set_aside(engine):
    conv, token, card = pending(engine)
    other, other_token, _ = pending(engine)
    with pytest.raises(Refusal) as r:
        approvals.set_aside(engine, other, other_token, card.approval_id, QUESTION.message, QUESTION)
    assert r.value.code == "replaces_mismatch" and status(engine, card.approval_id) == "pending"


def test_an_approval_that_won_is_reported_and_never_set_aside(engine):
    conv, token, card = pending(engine)
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    out = approvals.set_aside(engine, conv, token, card.approval_id, QUESTION.message, QUESTION)
    assert out.status == "replaced_card_ended" and out.data["predecessor"]["status"] == "consumed", out
    assert "already approved" in out.text


@pytest.mark.parametrize("trial", range(6))
def test_an_approval_racing_a_set_aside_exactly_one_wins_and_the_other_is_told(engine, trial):
    conv, token, card = pending(engine)
    invoices = count(engine, t.invoice)
    start, results = threading.Barrier(2), {}

    def approve():
        start.wait()
        results["approve"] = approvals.approve(engine, card.approval_id, card.digest)

    def set_aside():
        start.wait()
        results["set_aside"] = approvals.set_aside(engine, conv, token, card.approval_id, QUESTION.message, QUESTION)

    threads = [threading.Thread(target=f) for f in ((approve, set_aside) if trial % 2 else (set_aside, approve))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    a, s = results["approve"], results["set_aside"]
    if s.status == "set_aside":  # the set-aside won: the approval commits nothing and returns the question
        assert a.status == "superseded" and a.text == QUESTION.message, (a, s)
        assert status(engine, card.approval_id) == "superseded" and count(engine, t.invoice) == invoices
    else:  # the approval won: committed once, and the set-aside says so
        assert a.status == "committed" and s.status == "replaced_card_ended", (a, s)
        assert s.data["predecessor"]["status"] == "consumed" and status(engine, card.approval_id) == "consumed"
        assert count(engine, t.invoice) == invoices + 1
