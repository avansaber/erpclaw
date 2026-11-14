"""Unknown invoice customers keep the request open without changing the books.

The empty diff and owner-facing question follow unclear_invoice_request_no_write.md.
"""
import pytest

from bookdiff import assert_changes, snapshot
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_assistant.resolve import Session, resolve_record
from erpclaw_core.common import Refusal


def invoice(customer, quantity=None):
    return call("create_invoice", customer=customer,
                items=[] if quantity is None else [{"item": "copper wire", "quantity": quantity}])


def question(events):
    assert [e["type"] for e in events] == ["ask"], events
    return events[0]


def test_invoice_abc_asks_who_to_invoice_without_a_card_or_book_changes(engine):
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice("ABC")])]))
    before = snapshot(engine)
    q = question(assistant.turn("conversation", "invoice ABC"))
    assert_changes(before, snapshot(engine), {})
    assert q["code"] == "customer_offer_unknown"
    assert q["text"] == "I can't find a customer called 'ABC'. Which customer is it for, or shall I add it first? After approving each card, say continue to proceed with the invoice."
    assert q["options"] == ["Add it", "No thanks"]
    assert q["kind"] == "field"
    assert [c["label"] for c in q["choices"] if not c["control"]] == ["Add it", "No thanks"]
    assert any(c["label"] == "Cancel" and c["control"] for c in q["choices"])
    assert store.open_request("conversation")["pending_question"]["id"] == q["question_id"]


def test_near_customer_is_offered_without_a_card_or_book_changes(engine):
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice("Acme Corp")])]))
    before = snapshot(engine)
    q = question(assistant.turn("conversation", "invoice Acme Corp"))
    assert_changes(before, snapshot(engine), {})
    assert "Acme Corporation" in q["options"]
    assert any(c["label"] == "Acme Corporation" and not c["control"] for c in q["choices"])
    assert any(c["label"] == "Cancel" and c["control"] for c in q["choices"])
    assert any(c["label"].lower() == "someone else" for c in q["choices"])


@pytest.mark.parametrize("rows", [[{"name": "ABC"}], []])
def test_unknown_customer_uses_existing_question_words_and_masters_near_names(rows):
    session = Session()
    with pytest.raises(Refusal) as caught:
        # The short typo has no word-level candidate; masters' whole-name rule offers ABC.
        resolve_record(rows, "customer", "invoice AB", "AB", session, "customer", item=False)
    q = caught.value
    assert q.code == "customer_unknown"
    assert q.message == "I can't find a customer called 'AB'. Which customer is it for?"
    assert q.options == (["ABC"] if rows else [])
    if rows:
        assert session.answer(q.data["choices"][0]["code"])
        assert resolve_record(rows, "customer", "invoice AB", "AB", session, "customer", item=False) == "ABC"


def test_answer_with_real_customer_continues_original_invoice(engine):
    model = ScriptedModel([Reply("", [invoice("ABC", 3)]), Reply("", [invoice("ABC", 3)])])
    assistant = Assistant(engine, model)
    before = snapshot(engine)
    q = question(assistant.turn("conversation", "invoice ABC for 3 copper wire"))
    assert_changes(before, snapshot(engine), {})
    assert q["text"] == "I can't find a customer called 'ABC'. Which customer is it for, or shall I add it first? After approving each card, say continue to proceed with the invoice."
    original = store.open_request("conversation")["id"]
    events = assistant.turn("conversation", "Acme Corporation")
    assert [e["type"] for e in events] == ["card"], events
    lines = events[0]["card"]["lines"]
    assert ["Customer", "Acme Corporation"] in lines
    assert ["Total", "84.00 USD"] in lines  # the original request's quantity survives the customer answer
    assert store.open_request("conversation")["id"] == original
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"status": "pending"}]}})


@pytest.mark.parametrize("by", ["label", "code"])
def test_near_name_answer_continues_original_invoice(engine, by):
    model = ScriptedModel([Reply("", [invoice("Acme Corp", 3)]), Reply("", [invoice("ABC", 3)])])
    assistant = Assistant(engine, model)
    q = question(assistant.turn("conversation", "invoice Acme Corp for 3 copper wire"))
    original = store.open_request("conversation")["id"]
    choice = next(c for c in q["choices"] if c["label"] == "Acme Corporation")
    events = assistant.turn("conversation", "Acme Corporation", choice=choice["code"] if by == "code" else None)
    assert [e["type"] for e in events] == ["card"], events
    assert ["Customer", "Acme Corporation"] in events[0]["card"]["lines"]
    assert ["Total", "84.00 USD"] in events[0]["card"]["lines"]
    assert store.open_request("conversation")["id"] == original
