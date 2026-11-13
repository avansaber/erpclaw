"""Memo recognition and boundaries, importing resolution only through its facade."""
from decimal import Decimal
from random import SystemRandom

import pytest
from sqlalchemy import select

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli.evals.gate_oracle import take_snapshot
from erpclaw_core import approvals
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as sales


@pytest.fixture
def books(engine):
    random = SystemRandom()
    customer = f"Harbor{random.randrange(1000, 10000)} Works"
    item = f"Assembly{random.randrange(1000, 10000)}"
    quantity = random.randrange(2, 10)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        customer_id = create_party(ctx, "customer", customer)
        create_item(ctx, item, Decimal(random.randrange(20, 100)))
    draft = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {
        "customer": customer, "items": [{"item": item, "quantity": quantity}], "submit": True})
    assert approvals.approve(engine, draft.approval_id, draft.digest).status == "committed"
    with engine.connect() as conn:
        number = conn.execute(select(sales.invoice.c.number).where(
            sales.invoice.c.customer_id == customer_id)).scalar_one()
    return customer, item, quantity, number


def unchanged(engine, before, *, card=False):
    ignored = {"conversation_fence", "approval"} if card else {"conversation_fence"}
    assert {k: v for k, v in take_snapshot(engine).items() if k not in ignored} == {
        k: v for k, v in before.items() if k not in ignored}


def invoice_call(books):
    customer, item, quantity, _ = books
    return call("create_invoice", customer=customer, items=[{"item": item, "quantity": quantity}])


def invoice_words(books):
    customer, item, quantity, _ = books
    return f"invoice {customer} for {quantity} {item}"


def memo_question(events, said):
    assert [e["type"] for e in events] == ["ask"], events
    question = events[0]
    qualifier, notice = rv.memo_qualifier(said)
    assert question["code"] == "memo_choice_missing", events
    assert question["text"] == (f"{rv._say(notice, qualifier=qualifier)} "
                                f"{rv._say('memo_none_or_leave', qualifier=qualifier)}")
    assert question["options"] == [rv._say("memo_none", qualifier=qualifier), rv._say("leave_it")]
    return question


@pytest.mark.parametrize("reference", ['"ABC note 5"', "ABC note 5", '"ABC and note 5"'])
def test_payment_reference_owns_its_inner_cue_words(engine, reference):
    before = take_snapshot(engine)
    said = f"Hooli paid 100, reference {reference}"
    assistant = Assistant(engine, ScriptedModel([Reply("", [call(
        "record_payment", customer="Hooli", amount=100, reference=reference.strip('"'))])]))
    events = assistant.turn("reference", said)
    assert [e["type"] for e in events] == ["card"], events
    assert [value for label, value in events[0]["card"]["lines"] if label == "Reference"] == [reference]
    with engine.connect() as conn:
        [memo] = [o for o in rv.inventory(company_ctx(conn), said, [], incoming=True) if o.kind == "memo"]
    assert memo.said == f"reference {reference}"
    assert memo.value == reference
    unchanged(engine, before, card=True)


@pytest.mark.parametrize("value", ["see note 5", '"see and note 5"'])
def test_invoice_memo_owns_its_inner_cue_words(engine, books, value):
    before = take_snapshot(engine)
    said = f"{invoice_words(books)}, memo: {value}"
    with engine.connect() as conn:
        [memo] = [o for o in rv.inventory(company_ctx(conn), said, []) if o.kind == "memo"]
    assert memo.said == f"memo: {value}"
    assert memo.value == value
    reply = Reply("", [invoice_call(books)])
    assistant = Assistant(engine, ScriptedModel([reply, reply]))
    question = memo_question(assistant.turn("memo-value", said), said)
    unchanged(engine, before)
    events = assistant.turn("memo-value", question["options"][0])
    assert [e["type"] for e in events] == ["not_done", "card"], events
    assert events[0]["part"] == "memo"
    unchanged(engine, before, card=True)


def test_credit_followup_invoice_answer_reaches_card_without_memo_question(engine, books):
    customer, _, _, number = books
    before = take_snapshot(engine)
    assistant = Assistant(engine, ScriptedModel([
        Reply("", [call("create_credit_note", everything=True)]),
        Reply("", [call("create_credit_note", everything=True, invoice=number)])]))
    [question] = assistant.turn("return", f"{customer} returns everything on its last invoice; make the credit note.")
    assert (question["type"], question["code"], question["text"]) == (
        "ask", "invoice_missing", rv._say("document_missing", what="invoice"))
    unchanged(engine, before)
    events = assistant.turn("return", number)
    assert [e["type"] for e in events] == ["card"], events
    assert number in str(events[0]["card"]) and customer in str(events[0]["card"])
    unchanged(engine, before, card=True)


def test_credit_single_turn_with_invoice_number_has_no_memo_question(engine, books):
    customer, _, _, number = books
    before = take_snapshot(engine)
    assistant = Assistant(engine, ScriptedModel([
        Reply("", [call("create_credit_note", everything=True, invoice=number)])]))
    events = assistant.turn("single", f"make the credit note for {customer} {number}; return everything")
    assert [e["type"] for e in events] == ["card"], events
    unchanged(engine, before, card=True)


@pytest.mark.parametrize("tail", ["memo: returned goods", "note it as returned goods", "memo: returned goods."])
def test_single_real_memo_is_still_read_once(engine, books, tail):
    before = take_snapshot(engine)
    said = f"{invoice_words(books)}, {tail}"
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice_call(books)])]))
    memo_question(assistant.turn("memo", said), said)
    with engine.connect() as conn:
        assert len([o for o in rv.inventory(company_ctx(conn), said, []) if o.kind == "memo"]) == 1
    unchanged(engine, before)


@pytest.mark.parametrize("tail", ["memo: returned goods", "memo: returned goods and note returned goods"])
def test_credit_with_omitted_lines_and_one_memo_uses_existing_notice(engine, books, tail):
    _, _, _, number = books
    assert "memo" not in REGISTRY.contracts["create_credit_note"].input_model.model_fields
    before = take_snapshot(engine)
    said = f"return everything from invoice {number}, {tail}"
    reply = Reply("", [call("create_credit_note", everything=True, invoice=number)])
    assistant = Assistant(engine, ScriptedModel([reply, reply]))
    question = memo_question(assistant.turn("credit-memo", said), said)
    unchanged(engine, before)
    events = assistant.turn("credit-memo", question["options"][0])
    assert [e["type"] for e in events] == ["not_done", "card"], events
    qualifier, notice = rv.memo_qualifier(said)
    assert events[0] == {"type": "not_done", "part": "memo", "capability": "planned.invoice_fields",
                         "text": rv._say(notice, qualifier=qualifier)}
    unchanged(engine, before, card=True)


def test_invoice_two_distinct_memos_keeps_one_choice_question(engine, books):
    before = take_snapshot(engine)
    said = f"{invoice_words(books)}, memo Alpha and note Beta"
    reply = Reply("", [invoice_call(books)])
    assistant = Assistant(engine, ScriptedModel([reply, reply]))
    question = memo_question(assistant.turn("two-invoice", said), said)
    unchanged(engine, before)
    events = assistant.turn("two-invoice", question["options"][0])
    assert [e["type"] for e in events] == ["not_done", "card"], events
    unchanged(engine, before, card=True)


def test_two_different_memos_still_ask_memo_ambiguous(engine, books):
    _, _, _, number = books
    before = take_snapshot(engine)
    said = f"submit {number} with memo Alpha and note Beta"
    assistant = Assistant(engine, ScriptedModel([Reply("", [call("submit_invoice", invoice=number)])]))
    events = assistant.turn("two-submit", said)
    assert [e["type"] for e in events] == ["ask"], events
    assert events[0]["code"] == "memo_ambiguous", events
    assert events[0]["text"] == rv._say("figure_unused", figure="memo Alpha and note Beta")
    unchanged(engine, before)


@pytest.mark.parametrize("name", ["Harbor Notebooks", "Harbor Note Works", "Harbor Memo Works"])
def test_memo_word_inside_a_party_name_is_not_a_memo(engine, books, name):
    _, item, quantity, _ = books
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", name)
    before = take_snapshot(engine)
    assistant = Assistant(engine, ScriptedModel([Reply("", [call(
        "create_invoice", customer=name, items=[{"item": item, "quantity": quantity}])])]))
    events = assistant.turn("party", f"invoice {name} for {quantity} {item}")
    assert [e["type"] for e in events] == ["card"], events
    unchanged(engine, before, card=True)


@pytest.mark.parametrize("text", ["INV-2026-00005", "PO-2026-00005", "credit note.", "credit  note for Acme"])
def test_document_words_and_numbers_are_never_memos(engine, text):
    with engine.connect() as conn:
        assert not [o for o in rv.inventory(company_ctx(conn), text, []) if o.kind == "memo"]


@pytest.mark.parametrize("ending", ["returned goods", "returned goods."])
def test_memo_ends_at_the_message_break(engine, ending):
    with engine.connect() as conn:
        text = f"memo: {ending} {rv.MESSAGE_BREAK} INV-2026-00005"
        occurrences = rv.inventory(company_ctx(conn), text, [])
    [memo] = [o for o in occurrences if o.kind == "memo"]
    assert memo.said == "memo: returned goods"
    assert memo.end == len(rv.words("memo: returned goods"))
    assert [o.said for o in occurrences if o.kind == "document"] == ["INV-2026-00005"]


@pytest.mark.parametrize("answer_tail", ["", " returned goods"])
def test_dangling_memo_does_not_take_the_invoice_answer_and_asks_once(engine, books, answer_tail):
    customer, _, _, number = books
    before = take_snapshot(engine)
    assistant = Assistant(engine, ScriptedModel([
        Reply("", [call("create_credit_note", everything=True)]),
        Reply("", [call("create_credit_note", everything=True, invoice=number)]),
        Reply("", [call("create_credit_note", everything=True, invoice=number)])]))
    first = f"{customer} returns everything on its last invoice with memo:"
    [question] = assistant.turn("split", first)
    assert question["code"] == "invoice_missing"
    unchanged(engine, before)
    answer = number + answer_tail
    question = memo_question(assistant.turn("split", answer), first)
    unchanged(engine, before)
    with engine.connect() as conn:
        text = rv.request_words(company_ctx(conn), [first, answer], [
            (REGISTRY.contracts["create_credit_note"], {"everything": True, "invoice": number})])
        [memo] = [o for o in rv.inventory(company_ctx(conn), text, []) if o.kind == "memo"]
        assert memo.said == "memo:"
        assert memo.value == ""
    events = assistant.turn("split", question["options"][0])
    assert [e["type"] for e in events] == ["not_done", "card"], events
    unchanged(engine, before, card=True)
