"""A rate or a memo the engine cannot set yet is asked about, as an invoice date is: the notice, then two coded
choices. The card is made only on the owner's choice, with the notice beside it, and "Leave it" ends the request with
nothing saved. Emailing keeps its notice beside the card."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item
from erpclaw_sales import tables as t
from erpclaw_assistant import resolve_text


def assistant(engine, *replies):
    return Assistant(engine, ScriptedModel(list(replies)))


def inv(customer, quantity, item="copper wire"):
    return call("create_invoice", customer=customer, items=[{"item": item, "quantity": quantity}])


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def card_of(events):
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    return cards[0]


def asked(events, code):
    assert kinds(events) == ["ask"] and events[0]["code"] == code, events
    return events[0]


RATE = "Invoice Acme Corporation for 5 copper wire at 30 each"
MEMO = "Invoice Acme Corporation for 5 copper wire, memo PO 4471"
# part -> (what the owner wrote, the question's code, the choice that makes the card, how its notice begins)
PARTS = {
    "rate": (RATE, None, None, None),
    "memo": (MEMO, "memo_choice_missing", "Leave out the PO number", "A customer's PO number cannot be recorded yet"),
}


def _question(engine, part, *more):
    said, code, _, _ = PARTS[part]
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), *more)
    return a, asked(a.turn("c1", said), code)


@pytest.mark.parametrize("part", PARTS)
def test_a_part_the_engine_cannot_set_is_asked_about_with_two_coded_choices_and_no_card(engine, part):
    cards = count(engine, m.approval)
    if part == "rate":
        ev = assistant(engine, Reply("", [inv("Acme Corporation", 5)])).turn("c1", RATE)
        assert "@ 30.00 = 150.00" in str(card_of(ev)["card"]["lines"]), ev
        assert not any(e["type"] == "ask" or (e["type"] == "not_done" and e["part"] == "rate") for e in ev), ev
        return
    _, q = _question(engine, part)
    _, _, go, notice = PARTS[part]
    assert q["options"] == [go, "Leave it"] and len(q["choices"]) == 2, q
    assert len({c["code"] for c in q["choices"]}) == 2, q
    assert q["text"].startswith(notice), q
    assert count(engine, m.approval) == cards


@pytest.mark.parametrize("by", ["label", "code"])
@pytest.mark.parametrize("part", PARTS)
def test_the_owner_s_choice_makes_the_card_with_the_notice_beside_it(engine, part, by):
    if part == "rate":
        ev = assistant(engine, Reply("", [inv("Acme Corporation", 5)])).turn("c1", RATE)
        assert "@ 30.00 = 150.00" in str(card_of(ev)["card"]["lines"]), ev
        assert not any(e["type"] == "ask" or (e["type"] == "not_done" and e["part"] == "rate") for e in ev), ev
        return
    a, q = _question(engine, part, Reply("", [inv("Acme Corporation", 5)]))
    _, _, go, notice = PARTS[part]
    code = next(c["code"] for c in q["choices"] if c["label"] == go)
    ev = a.turn("c1", go, choice=code if by == "code" else None)
    card = card_of(ev)
    assert [e["text"][:len(notice)] for e in ev if e["type"] == "not_done" and e["part"] == part] == [notice], ev
    assert "140.00" in str(card["card"]["lines"]), card  # five copper wire at the list price, 28.00
    assert store.open_request("c1")["pending_card"] == card["approval_id"]


@pytest.mark.parametrize("part", PARTS)
def test_leave_it_ends_the_request_and_saves_nothing(engine, part):
    cards, invoices = count(engine, m.approval), count(engine, t.invoice)
    if part == "rate":
        ev = assistant(engine, Reply("", [inv("Acme Corporation", 5)])).turn("c1", RATE)
        assert "@ 30.00 = 150.00" in str(card_of(ev)["card"]["lines"]), ev
        assert not any(e["type"] == "ask" or (e["type"] == "not_done" and e["part"] == "rate") for e in ev), ev
        return
    a, _ = _question(engine, part, Reply("", [inv("Acme Corporation", 5)]))
    ev = a.turn("c1", "Leave it")
    assert ev == [{"currency": "USD", "type": "answer", "text": "Cancelled; nothing was saved.", "checked": "engine"}], ev
    assert (count(engine, m.approval), count(engine, t.invoice)) == (cards, invoices)
    assert store.open_request("c1") is None


def test_a_date_a_rate_and_a_memo_are_asked_one_at_a_time_then_one_card(engine):
    cards = count(engine, m.approval)
    a = assistant(engine, *[Reply("", [inv("Acme Corporation", 5)])] * 4)
    first = asked(a.turn("c1", "Invoice Acme Corporation for 5 copper wire at 30 each dated 05/10/2026, memo PO 4471"),
                  "date_choice_missing")
    second = asked(a.turn("c1", "Date it today"), "memo_choice_missing")
    assert first["question_id"] != second["question_id"]
    assert count(engine, m.approval) == cards
    ev = a.turn("c1", "Leave out the PO number")
    card = card_of(ev)
    assert "@ 30.00 = 150.00" in str(card["card"]["lines"])
    assert {e["part"] for e in ev if e["type"] == "not_done"} == {"date", "memo"}, ev
    assert count(engine, m.approval) == cards + 1


def test_a_rate_at_the_list_price_is_no_question(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]))
    assert kinds(a.turn("c1", "Invoice Acme Corporation for 5 copper wire at 28 each")) == ["card"]


def test_emailing_keeps_its_notice_beside_the_card(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]))
    ev = a.turn("c1", "Invoice Acme Corporation for 5 copper wire and email it to them")
    assert kinds(ev) == ["not_done", "card"] and ev[0]["part"] == "email", ev


@pytest.mark.parametrize("rate", [None, Decimal("0"), Decimal("0.00")], ids=["none", "zero", "zero-cents"])
def test_the_books_refuse_an_item_without_a_positive_list_price(engine, rate):
    # "Use the list price" relies on this: every item has one. A change allowing an unpriced item must also make the
    # rate question refuse plainly for it, with its own test.
    items = count(engine, m.item)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            create_item(company_ctx(conn), "Unpriced thing", rate)
    assert count(engine, m.item) == items


@pytest.mark.parametrize("part, correction, go", [
    ("memo", "add memo PO 4471", "Leave out the PO number"),
    ("rate", "actually at 30 each", "Use the list price"),
])
def test_unchanged_reproposal_asks_about_the_correction_and_sets_aside_the_card(engine, part, correction, go):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    before = [count(engine, table) for table in (m.approval, t.invoice, m.posting)]
    ev = a.turn("c1", correction)
    if part == "rate":
        assert ev[0]["code"] == "card_set_aside", ev
        assert "@ 30.00 = 150.00" in str(card_of(ev)["card"]["lines"]), ev
        with engine.connect() as conn:
            assert conn.execute(select(m.approval.c.status).where(m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"
        return
    assert kinds(ev) == ["notice", "ask"] and ev[0]["code"] == "card_set_aside", ev
    q = ev[1]
    assert q["code"] == f"{part}_choice_missing" and q["options"] == [go, "Leave it"], q
    assert [c["label"] for c in q["choices"]] == [go, "Leave it"], q
    assert len({c["code"] for c in q["choices"]}) == 2 and q["question_id"], q
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"
    assert store.open_request("c1")["pending_card"] is None
    assert [count(engine, table) for table in (m.approval, t.invoice, m.posting)] == before


QUALIFIERS = [
    ("memo PO number 4471", "PO number", "A customer's PO number cannot be recorded yet; the invoice will have none."),
    ("reference REF88", "reference", "Recording a reference on an invoice isn't available yet; the invoice will have none."),
    ("memo deliver upstairs", "memo", "Adding a memo to an invoice isn't available yet; the invoice will have none."),
    ("note deliver upstairs", "note", "Adding a note to an invoice isn't available yet; the invoice will have none."),
    ("memo PO 4471", "PO number", "A customer's PO number cannot be recorded yet; the invoice will have none."),
]


@pytest.mark.parametrize("by", ["label", "code"])
@pytest.mark.parametrize("said, qualifier, notice", QUALIFIERS)
def test_memo_qualifier_is_named_in_the_question_choice_and_later_notice(engine, said, qualifier, notice, by):
    a = assistant(engine, *[Reply("", [inv("Acme Corporation", 5)])] * 2)
    before = [count(engine, table) for table in (t.invoice, m.posting)]
    q = asked(a.turn("c1", f"Invoice Acme Corporation for 5 copper wire, {said}"), "memo_choice_missing")
    go = f"Leave out the {qualifier}"
    assert q["text"] == f"{notice} Shall I draft it without the {qualifier}, or leave it?", q
    assert q["options"] == [go, "Leave it"], q
    assert [c["label"] for c in q["choices"]] == [go, "Leave it"], q
    assert len({c["code"] for c in q["choices"]}) == 2 and q["question_id"], q
    if qualifier in ("PO number", "reference"):
        assert "note" not in q["text"].lower() and "memo" not in q["text"].lower(), q
    ev = a.turn("c1", go, choice=q["choices"][0]["code"] if by == "code" else None)
    assert "140.00" in str(card_of(ev)["card"]["lines"]), ev
    assert [e for e in ev if e["type"] == "not_done"] == [
        {"type": "not_done", "part": "memo", "capability": "planned.invoice_fields", "text": notice}], ev
    assert [count(engine, table) for table in (t.invoice, m.posting)] == before


@pytest.mark.parametrize("earlier, later, qualifier", [
    ("memo PO 4471", "reference REF88", "reference"),
    ("reference REF88", "memo PO 4471", "PO number"),
])
def test_latest_memo_qualifier_reopens_the_question_without_stale_display(engine, earlier, later, qualifier):
    a = assistant(engine, *[Reply("", [inv("Acme Corporation", 5)])] * 2, Reply("Okay."))
    first = asked(a.turn("c1", f"Invoice Acme Corporation for 5 copper wire, {earlier}"), "memo_choice_missing")
    card = card_of(a.turn("c1", first["choices"][0]["label"]))
    before = [count(engine, table) for table in (m.approval, t.invoice, m.posting)]
    ev = a.turn("c1", f"actually {later}")
    assert kinds(ev) == ["notice", "ask"], ev
    q = ev[1]
    assert q["code"] == "memo_choice_missing", q
    assert q["options"] == [f"Leave out the {qualifier}", "Leave it"], q
    assert q["question_id"] != first["question_id"] and not (
        {c["code"] for c in q["choices"]} & {c["code"] for c in first["choices"]}), q
    assert f"without the {qualifier}, or leave it?" in q["text"], q
    assert ("PO number" in q["text"]) == (qualifier == "PO number"), q
    assert "note" not in q["text"].lower(), q
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == "superseded"
    assert [count(engine, table) for table in (m.approval, t.invoice, m.posting)] == before


@pytest.mark.parametrize("said, label, notice", [
    ("annotation orderref 4471", "order tag", "The order tag cannot be recorded."),
    ("tag REF88", "reference tag", "The reference tag cannot be recorded."),
])
def test_memo_qualifier_wording_comes_from_the_active_language_table(engine, monkeypatch, said, label, notice):
    from copy import deepcopy
    from erpclaw_assistant import resolve

    toy = deepcopy(resolve.table())
    toy["memo"] = r"\b(?:annotation|orderref|tag)\b\s*[:#]?\s*\S"
    toy["bound"] |= {"annotation", "orderref", "tag"}
    toy["memo_qualifiers"] = {"annotation": ("annotation", "toy_annotation"),
                              "orderref": ("order tag", "toy_order"), "tag": ("reference tag", "toy_reference")}
    toy["memo_qualifier_default"] = "annotation"
    toy["memo_qualifier_cues"] = ((r"annotation\b\s*[:#]?\s*orderref\b", "orderref"),)
    toy["text"].update(toy_annotation="The annotation cannot be recorded.",
                       toy_order="The order tag cannot be recorded.",
                       toy_reference="The reference tag cannot be recorded.",
                       memo_none_or_leave="Omit {qualifier}, or leave it?", memo_none="Omit {qualifier}")
    monkeypatch.setitem(resolve.TABLES, "toy", toy)
    for _module in (resolve, resolve_text):
        monkeypatch.setattr(_module, 'LANGUAGE', 'toy')
    a = assistant(engine, *[Reply("", [inv("Acme Corporation", 5)])] * 2)
    q = asked(a.turn("c1", f"Invoice Acme Corporation for 5 copper wire, {said}"), "memo_choice_missing")
    assert q["text"] == f"{notice} Omit {label}, or leave it?", q
    assert q["options"] == [f"Omit {label}", "Leave it"], q
    ev = a.turn("c1", q["choices"][0]["label"], choice=q["choices"][0]["code"])
    card_of(ev)
    assert [e["text"] for e in ev if e["type"] == "not_done" and e["part"] == "memo"] == [notice], ev


def test_rate_correction_replaces_the_earlier_card(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    events = a.turn("c1", "actually at 30 each")
    assert events[0]["type"] == "notice" and events[0]["code"] == "card_set_aside", events
    updated = card_of(events)
    assert "@ 30.00 = 150.00" in str(updated["card"]["lines"]), events
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"


def test_the_choice_continues_the_request_across_a_restart(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]))
    q = asked(a.turn("c1", RATE + ", memo PO 4471"), "memo_choice_missing")
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]))
    ev = a.turn("c1", q["choices"][0]["label"], choice=q["choices"][0]["code"])
    assert "@ 30.00 = 150.00" in str(card_of(ev)["card"]["lines"]), ev
    assert any(e["type"] == "not_done" and e["part"] == "memo" for e in ev), ev


@pytest.mark.parametrize("correction", ["actually 35 each", "make it 35 each", "no, 35 each",
                                        "make it $35", "make it USD 35", "make it 35 a unit",
                                        "make it 35 per piece"])
def test_a_later_rate_clears_the_earlier_choice_and_is_asked_about(engine, correction):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    first = card_of(a.turn("c1", RATE))
    before = count(engine, t.invoice)
    ev = a.turn("c1", correction)
    assert ev[0]["type"] == "notice" and ev[0]["code"] == "card_set_aside", ev
    assert "@ 35.00 = 175.00" in str(card_of(ev)["card"]["lines"]), ev
    with engine.connect() as conn:
        import json
        row = conn.execute(select(m.approval.c.args_json).where(m.approval.c.id == card_of(ev)["approval_id"])).scalar_one()
        assert Decimal(str(json.loads(row)["items"][0]["rate"])) == Decimal("35")
        assert conn.execute(select(m.approval.c.status).where(m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"
    assert count(engine, t.invoice) == before


@pytest.mark.parametrize("correction", ["actually $35", "no, $35", "$35 each", "actually USD 35",
                                        "actually 35 USD", "no, 35 USD", "35 USD each", "actually 35 $"])
def test_currency_marked_correction_replaces_the_card(engine, correction):
    from bookdiff import changes, snapshot

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    books_before = snapshot(engine)
    first = card_of(a.turn("c1", RATE))
    assert "@ 30.00 = 150.00" in str(first["card"]["lines"])
    events = a.turn("c1", correction)
    assert [event["type"] for event in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside", events
    assert "@ 35.00 = 175.00" in str(events[1]["card"]["lines"]), events
    assert set(changes(books_before, snapshot(engine))) == {"approval"}, events
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"


def test_foreign_suffix_currency_correction_is_not_priced_in_company_currency(engine):
    from bookdiff import changes, snapshot

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    before = snapshot(engine)
    card_of(a.turn("c1", RATE))
    events = a.turn("c1", "no, 35 EUR")
    assert [e["type"] for e in events] == ["notice", "refused"], events
    assert events[1]["code"] == "foreign_currency", events
    assert set(changes(before, snapshot(engine))) == {"approval"}, events


def test_code_word_suffix_asks_one_question(engine):
    from bookdiff import changes, snapshot

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    before = snapshot(engine)
    card_of(a.turn("c1", RATE))
    events = a.turn("c1", "actually 35 ALL")
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[1]["code"] == "rate_choice_missing", events
    assert set(changes(before, snapshot(engine))) == {"approval"}, events


def test_filling_rate_pattern_does_not_change_language_table():
    raw = resolve_text.TABLES["en"]["rate"]
    assert "<currency_mark>" in raw
    assert "<currency_mark>" not in resolve_text.table("en")["rate"]
    assert resolve_text.TABLES["en"]["rate"] == raw


def test_per_unit_correction_replaces_the_card_at_the_new_rate(engine):
    from bookdiff import changes, snapshot

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay."))
    books_before = snapshot(engine)
    before = count(engine, t.invoice)
    card_of(a.turn("c1", RATE))
    events = a.turn("c1", "make it 35 per unit")
    assert count(engine, t.invoice) == before, events
    assert [event["type"] for event in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside", events
    card = card_of(events)["card"]
    assert any("@ 35.00 = 175.00" in value for key, value in card["lines"] if key == "Item"), events
    assert "Accounts Receivable (Acme Corporation) +175.00" in card["books"], events
    assert set(changes(books_before, snapshot(engine))) == {"approval"}, events
