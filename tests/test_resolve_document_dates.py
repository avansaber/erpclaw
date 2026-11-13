"""Document recency is not a posting date; calendar readings retain their meaning."""
from datetime import date
from decimal import Decimal
from random import SystemRandom

import pytest
from sqlalchemy import select

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli.evals.gate_oracle import take_snapshot
from erpclaw_core import approvals
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as sales


TODAY = date(2026, 10, 26)
DOCUMENTS = sorted(rv.table()["document_nouns"] | {"order", "delivery"})


def test_full_return_on_its_last_invoice_reaches_the_card_or_next_non_date_question(engine):
    """The public credit request and invoice follow-up, with generated books and no approval."""
    random = SystemRandom()
    short = f"Harbor{random.randrange(1000, 10000)}"
    customer, item = f"{short} Works", f"Assembly{random.randrange(1000, 10000)}"
    quantity, rate = random.randrange(2, 10), Decimal(random.randrange(20, 100))
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        customer_id = create_party(ctx, "customer", customer)
        create_item(ctx, item, rate)
    draft = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {
        "customer": customer, "items": [{"item": item, "quantity": quantity}], "submit": True})
    outcome = approvals.approve(engine, draft.approval_id, draft.digest)
    assert outcome.status == "committed"
    with engine.connect() as conn:
        number = conn.execute(select(sales.invoice.c.number).where(
            sales.invoice.c.customer_id == customer_id)).scalar_one()
    before = take_snapshot(engine)
    assistant = Assistant(engine, ScriptedModel([
        Reply("", [call("create_credit_note", everything=True)]),
        Reply("", [call("create_credit_note", everything=True, invoice=number)]),
    ]))
    [question] = assistant.turn("return", f"{short} returns everything on its last invoice; make the credit note.")
    assert (question["type"], question["code"]) == ("ask", "invoice_missing")
    after_question = take_snapshot(engine)
    assert {k: v for k, v in after_question.items() if k != "conversation_fence"} == {
        k: v for k, v in before.items() if k != "conversation_fence"}
    events = assistant.turn("return", number)
    after = take_snapshot(engine)
    assert {k: v for k, v in after.items() if k not in {"conversation_fence", "approval"}} == {
        k: v for k, v in before.items() if k not in {"conversation_fence", "approval"}}
    if [e["type"] for e in events] == ["card"]:
        assert events[0]["card"]["date_source"] == {"default": True}
        assert number in str(events[0]["card"]) and customer in str(events[0]["card"])
    else:
        assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_ambiguous")], events
        assert {k: v for k, v in after.items() if k != "conversation_fence"} == {
            k: v for k, v in before.items() if k != "conversation_fence"}


@pytest.mark.parametrize("document", DOCUMENTS)
@pytest.mark.parametrize("determiner", ["its", "the", "my", "his", "her", "our", "their", "your", ""])
@pytest.mark.parametrize("position", ["before", "middle", "after", "on"])
def test_last_document_is_no_date(document, determiner, position):
    phrase = f"{determiner} last {document}"
    text = {
        "before": f"{phrase}; please check the books",
        "middle": f"please check {phrase} for me",
        "after": f"please check {phrase}",
        "on": f"returns everything on {phrase}; make the credit note",
    }[position]
    assert rv.date_phrases(text, TODAY) == []
    assert rv.resolve_date(text, TODAY, rv.Session(), "date") is None


@pytest.mark.parametrize("phrase, expected", [
    ("last Friday", [[date(2026, 10, 23)]]),
    ("today", [[TODAY]]),
    ("last night", [[date(2026, 10, 25)]]),
    ("Friday before last", [[date(2026, 10, 16)]]),
    ("2026-03-31", [[date(2026, 3, 31)]]),
])
def test_document_reference_preserves_a_real_date(phrase, expected):
    assert rv.date_phrases(phrase, TODAY) == expected
    assert rv.date_phrases(f"its last invoice; {phrase}", TODAY) == expected
    assert rv.resolve_date(f"on its last invoice; {phrase}", TODAY, rv.Session(), "date") == expected[0][0]


@pytest.mark.parametrize("phrase", [
    "last month", "last week", "the last day of March", "sometime", "on last",
    "on its last meeting", "on its last invoiceholder", "on its last invoiced", "on his last deliveryman",
    "last invoice; on blursday", "on its last invoice; on blursday",
    "on blursday; on its last invoice", "on its last invoice; last month",
    "on its last invoice; the last day of March", "on its last; invoice",
])
def test_an_unclear_date_still_asks(phrase):
    with pytest.raises(Refusal) as caught:
        rv.resolve_date(phrase, TODAY, rv.Session(), "date")
    assert caught.value.code == "date_unknown"
    assert caught.value.message == "Which date do you mean?"


@pytest.mark.parametrize("phrase", [
    "last", "last meeting", "last invoiceholder", "last invoiced", "last deliveryman", "last invoice; last",
])
def test_bare_last_and_non_calendar_words_are_still_dropped(phrase):
    assert rv.date_phrases(phrase, TODAY) == []
    assert rv.resolve_date(phrase, TODAY, rv.Session(), "date") is None


def test_document_reference_keeps_the_unread_model_date_question():
    with pytest.raises(Refusal) as caught:
        rv.resolve_date("on its last invoice", TODAY, rv.Session(), "date", "2026-03-31")
    assert caught.value.code == "date_unknown"


@pytest.mark.parametrize("phrase", ["ON ITS LAST INVOICE", "on\tmy last\norder", "on her last payment."])
def test_document_reference_accepts_case_whitespace_and_final_punctuation(phrase):
    assert rv.resolve_date(phrase, TODAY, rv.Session(), "date") is None
