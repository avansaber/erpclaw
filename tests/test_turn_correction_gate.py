"""Pending-card routing honors the owner's words, regardless of the model's call."""
import re
from decimal import Decimal

import pytest

from conftest import PG_URL
from bookdiff import changes, snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from ownership_helpers import owner_check
from erpclaw_core import approvals
from erpclaw_cli.evals.run import EvalBooks
from erpclaw_cli.evals.seeds import services_co


CUSTOMERS_REPLY = {"type": "answer", "text": "Customers, page 1:\nTotal customers: 5.\n"
                   "Acme Corporation\nHooli\nInitech\nPied Piper\nWayne Enterprises",
                   "amounts": [], "currency": "USD"}


def assert_books_not_written(engine, before):
    assert set(changes(before, snapshot(engine))) <= {"approval"}


@pytest.fixture(scope="module")
def books():
    b = EvalBooks(PG_URL)
    try:
        b.seed(services_co)
        yield b
    finally:
        b.close()


@pytest.fixture
def trial(books, monkeypatch):
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-11-03")
    eng = books.trial()
    yield eng
    books.finish(eng)


@pytest.mark.parametrize("message,reply", [
    ("yes approve it", Reply("", [call("submit_invoice", invoice="INV-2026-00001")])),
    ("yes approve it", Reply("Okay.")),
    ("approve it", Reply("", [call("submit_invoice", invoice="INV-2026-00001")])),
])
def test_typed_approval_preserves_card(trial, message, reply):
    invoice = call("create_invoice", customer="Initech", items=[{"item": "Support retainer", "quantity": 1}], submit=True)
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    [card] = a.turn("approval_gate", "Invoice Initech for one support retainer.")
    assert card["type"] == "card"
    events = a.turn("approval_gate", message)
    assert [e["type"] for e in events] == ["answer"], events
    assert events[0]["text"] == "Nothing has been saved yet. Approve the card to save it."
    assert approvals.pending_card(trial, "approval_gate", ownership_check=owner_check(trial))["id"] == card["approval_id"]
    assert approvals.approve(trial, card["approval_id"], card["digest"], ownership_check=owner_check(trial)).status == "committed"


@pytest.mark.parametrize("message,expected", [("Wayne Enterprises", "Wayne Enterprises"),
                                               ("2", "2")])
def test_field_value_overrides_unrelated_model_call(trial, message, expected):
    invoice = call("create_invoice", customer="Initech", items=[{"item": "Support retainer", "quantity": 1}], submit=True)
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [card] = a.turn("field_gate", "Invoice Initech for one support retainer.")
    events = a.turn("field_gate", message)
    assert events[0]["code"] == "card_set_aside", events
    assert events[-1]["type"] in ("card", "ask") and expected in str(events[-1]), events
    assert approvals.approve(trial, card["approval_id"], card["digest"], ownership_check=owner_check(trial)).status == "superseded"


def test_complete_request_with_pending_fields_is_a_correction(engine):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("complete_field", "Invoice Acme Corporation for 5 copper wire")
    from sqlalchemy import func, select
    from erpclaw_sales import tables
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("complete_field", "invoice Acme Corporation for 7 copper wire")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1]["code"] == "item_ambiguous"
    assert events[1]["text"] == "What is the copper wire for? I couldn't place it in this request."
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before


@pytest.mark.parametrize("message", ["at 80", "at 80 each", "please at 80", "no, at 80"])
def test_price_rate_overrides_unrelated_call(engine, message):
    from sqlalchemy import func, select
    from erpclaw_core import models

    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    assert a.turn("price_field", "Add item Consulting at 50 per hour")[-1]["code"] == "item_kind_missing"
    [first] = a.turn("price_field", "A service I sell")
    assert first["type"] == "card", first
    first_price = next(v for k, v in first["card"]["lines"] if k == "Selling price")
    assert Decimal(first_price.split()[0]) == Decimal("50.00")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(models.item)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("price_field", message)
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside"
    second = events[1]
    assert second["replaces"] == first["approval_id"]
    assert Decimal("80.00") == Decimal(str(next(v for k, v in second["card"]["lines"] if k == "Selling price").split()[0]))
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(models.item)).scalar_one() == before
    assert approvals.approve(engine, second["approval_id"], second["digest"], ownership_check=owner_check(engine)).status == "committed"
    with engine.connect() as conn:
        assert Decimal(str(conn.execute(select(models.item.c.standard_rate).where(models.item.c.name == "Consulting")).scalar_one())) == Decimal("80.00")



@pytest.mark.parametrize("correction", ["at 80, not 8", "at 80, not 50"])
def test_ambiguous_rate_keeps_fail_safe(engine, correction):
    from sqlalchemy import func, select
    from erpclaw_core import models

    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    a.turn("ambiguous_rate", "Add item Consulting at 50 per hour")
    [first] = a.turn("ambiguous_rate", "A service I sell")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(models.item)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("ambiguous_rate", correction)
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1]["code"] == "rate_ambiguous"
    assert events[1]["text"].count("?") == 1
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(models.item)).scalar_one() == before


def test_invoice_rate_correction_preserves_quantity(engine):
    from sqlalchemy import func, select
    from erpclaw_sales import tables

    invoice = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("invoice_rate_field", "invoice Hooli for 5 copper wire")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("invoice_rate_field", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1] == CUSTOMERS_REPLY
    assert all(e["type"] != "card" for e in events)
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before

def test_different_complete_request_follows_main(engine):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("different_request", "Invoice Acme Corporation for 5 copper wire")
    events = a.turn("different_request", "invoice Wayne Enterprises for 2 circuit boards")
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[-1]["code"] == "item_ambiguous"
    assert events[-1]["text"] == "Which item: Circuit Board, Copper Wire?"
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"


def test_owner_invoice_rate_replaces_only_the_rate(engine):
    from sqlalchemy import func, select
    from erpclaw_sales import tables

    invoice = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("owner_invoice_rate", "invoice Hooli for 5 copper wire at 5 each")
    assert first["type"] == "card", first
    assert next(v for k, v in first["card"]["lines"] if k == "Item") == "5 x Copper Wire @ 5.00 = 25.00"
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("owner_invoice_rate", "at 80 each")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1]["replaces"] == first["approval_id"]
    lines = events[1]["card"]["lines"]
    line = next(v for k, v in lines if k == "Item")
    assert "Copper Wire" in line
    assert next(v for k, v in lines if k == "Customer").startswith("Hooli")
    assert next(v for k, v in lines if k == "Date") == next(v for k, v in first["card"]["lines"] if k == "Date")
    match = re.search(r"(\d+(?:\.\d+)?).*? @ ([\d.]+) = ([\d.]+)", line)
    assert match and tuple(map(Decimal, match.groups())) == (Decimal(5), Decimal("80.00"), Decimal("400.00"))
    assert Decimal(next(v for k, v in lines if k == "Total").split()[0]) == Decimal("400.00")
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before
    assert approvals.approve(engine, events[1]["approval_id"], events[1]["digest"], ownership_check=owner_check(engine)).status == "committed"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before + 1


def test_item_rate_with_numeric_name_keeps_name_and_corrects_price(engine):
    item = ToolCall("item", "add_item", {"name": "Zebra 3", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    assert a.turn("numeric_item", "Add item Zebra 3 at 50 per hour")[-1]["code"] == "item_kind_missing"
    [first] = a.turn("numeric_item", "A service I sell")
    assert first["type"] == "card", first
    assert next(v for k, v in first["card"]["lines"] if k == "Item") == "Zebra 3"
    assert Decimal(next(v for k, v in first["card"]["lines"] if k == "Selling price").split()[0]) == Decimal("50.00")
    book_state = snapshot(engine)
    events = a.turn("numeric_item", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1]["replaces"] == first["approval_id"]
    lines = events[1]["card"]["lines"]
    assert next(v for k, v in lines if k == "Item") == "Zebra 3"
    assert Decimal(next(v for k, v in lines if k == "Selling price").split()[0]) == Decimal("80.00")
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


def test_item_rate_with_minimum_hours_asks_about_the_other_figure(engine):
    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item])]))
    book_state = snapshot(engine)
    events = a.turn("item_minimum", "Add item Consulting at 50 per hour, 3 hours minimum")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["ask"], events
    assert events[0]["code"] == "figure_ambiguous"
    assert events[0]["text"] == "What is the 3 for? I couldn't place it in this request."
    assert approvals.pending_card(engine, "item_minimum", ownership_check=owner_check(engine)) is None


def test_owner_invoice_rate_with_distinct_quantity_and_original_rate(engine):
    invoice = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 7}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("distinct_invoice_rate", "invoice Hooli for 7 copper wire at 30 each")
    assert first["type"] == "card", first
    assert next(v for k, v in first["card"]["lines"] if k == "Item") == "7 x Copper Wire @ 30.00 = 210.00"
    book_state = snapshot(engine)
    events = a.turn("distinct_invoice_rate", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside"
    assert events[1]["replaces"] == first["approval_id"]
    lines = events[1]["card"]["lines"]
    line = next(v for k, v in lines if k == "Item")
    match = re.search(r"(\d+(?:\.\d+)?).*? @ ([\d.]+) = ([\d.]+)", line)
    assert match and tuple(map(Decimal, match.groups())) == (Decimal(7), Decimal("80.00"), Decimal("560.00"))
    assert "Copper Wire" in line
    assert next(v for k, v in lines if k == "Customer").startswith("Hooli")
    assert Decimal(next(v for k, v in lines if k == "Total").split()[0]) == Decimal("560.00")
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


@pytest.mark.parametrize("correction,reply,expected", [
    ("actually 35 each", Reply("Okay."), "card"),
    ("actually 35 each", Reply("", [call("list_customers")]), "card"),
    ("no, 35 each", Reply("", [call("list_customers")]), "card"),
])
def test_marked_catalogue_price_keeps_main_outcome(engine, correction, reply, expected):
    from sqlalchemy import func, select
    from erpclaw_sales import tables

    invoice = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), reply]))
    [first] = a.turn("marked_catalogue", "invoice Hooli for 5 copper wire")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("marked_catalogue", correction)
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", expected], events
    notice = f"Your pending card ({first['card']['title']}) was set aside; nothing was saved from it."
    if expected == "answer":
        notice += " Ask again to get it back."
    assert events[0] == {"type": "notice", "code": "card_set_aside", "text": notice}
    if expected == "card":
        import json
        from erpclaw_core import models
        from erpclaw_core.common import company_ctx
        from erpclaw_core.plugins import REGISTRY

        assert events[1]["replaces"] == first["approval_id"]
        with engine.connect() as conn:
            original = json.loads(conn.execute(select(models.approval.c.args_json).where(
                models.approval.c.id == first["approval_id"])).scalar_one())
            original["items"][0]["rate"] = "35"
            contract = REGISTRY.contracts["create_invoice"]
            expected_card = approvals._preview(company_ctx(conn), contract,
                                               contract.input_model.model_validate(original),
                                               first["card"].get("date_source"))
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
        assert canonical(events[1]["card"]) == canonical(expected_card)
        lines = events[1]["card"]["lines"]
        assert next(v for k, v in lines if k == "Item") == "5 x Copper Wire @ 35.00 = 175.00"
        assert Decimal(next(v for k, v in lines if k == "Total").split()[0]) == Decimal("175.00")
    else:
        assert events[1] == {"type": "answer", "text": "Customers, page 1:\nTotal customers: 5.\n"
                            "Acme Corporation\nHooli\nInitech\nPied Piper\nWayne Enterprises", "amounts": [],
                            "currency": "USD"}
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before


def test_two_rates_keep_main_outcome(engine):
    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    a.turn("two_rates", "Add item Consulting at 50 per hour")
    [first] = a.turn("two_rates", "A service I sell")
    book_state = snapshot(engine)
    events = a.turn("two_rates", "at 80 at 90")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside" and events[1] == CUSTOMERS_REPLY
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


def test_two_equal_owner_rates_keep_main_outcome(engine):
    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    a.turn("duplicate_rates", "Add item Consulting at 50 per hour at 50")
    [first] = a.turn("duplicate_rates", "A service I sell")
    assert first["type"] == "card", first
    first_price = next(v for k, v in first["card"]["lines"] if k == "Selling price")
    assert Decimal(first_price.split()[0]) == Decimal("50.00")
    book_state = snapshot(engine)
    events = a.turn("duplicate_rates", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside" and events[1] == CUSTOMERS_REPLY
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


def test_short_quantity_keeps_main_outcome(engine):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("short_quantity", "Invoice Acme Corporation for 5 copper wire")
    book_state = snapshot(engine)
    events = a.turn("short_quantity", "actually make it 7")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[0]["code"] == "card_set_aside"
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)




@pytest.mark.parametrize("first_message", ["invoice Hooli for 5 copper wire and 2 circuit boards",
                                           "invoice Hooli for 5 copper wire at 5 each and 2 circuit boards"])
def test_multiple_invoice_lines_keep_main_outcome(engine, first_message):
    invoice = call("create_invoice", customer="Hooli", items=[
        {"item": "copper wire", "quantity": 5}, {"item": "circuit board", "quantity": 2}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("multiple_lines", first_message)
    assert first["type"] == "card", first
    item_lines = [v for k, v in first["card"]["lines"] if k == "Item"]
    assert len(item_lines) == 2
    if "at 5 each" in first_message:
        assert "5 x Copper Wire @ 5.00 = 25.00" in item_lines
    book_state = snapshot(engine)
    events = a.turn("multiple_lines", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside" and "Total customers:" in events[1]["text"]
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


@pytest.mark.parametrize("message", ["at 80%", "at 80 percent", "at 80 on 2026-12-01"])
def test_rate_with_percentage_or_date_keeps_main_outcome(engine, message):
    from sqlalchemy import func, select
    from erpclaw_core import models

    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    a.turn("price_decoy", "Add item Consulting at 50 per hour")
    [first] = a.turn("price_decoy", "A service I sell")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(models.item)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("price_decoy", message)
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside" and "Total customers:" in events[1]["text"]
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(models.item)).scalar_one() == before


def test_ambiguous_or_percentage_source_has_no_provenance(engine):
    from erpclaw_assistant import resolve
    from erpclaw_core.common import company_ctx
    from erpclaw_core.plugins import REGISTRY

    contract = REGISTRY.contracts["add_item"]
    args = {"name": "Consulting", "price": 50}
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        for earlier in ("Add item Consulting at 50 per hour at 50", "Add item Consulting at 50% per hour"):
            indices, replacement = resolve.owner_price_correction(ctx, contract, args, [earlier, "at 80"])
            assert indices == frozenset() and replacement is None


def test_model_supplied_price_update_uses_pending_card(engine):
    from sqlalchemy import func, select
    from erpclaw_core import models

    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    updated = ToolCall("updated", "add_item", {"name": "Consulting", "unit": "hour", "price": 80})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [updated])]))
    a.turn("model_price", "Add item Consulting at 50 per hour")
    [first] = a.turn("model_price", "A service I sell")
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(models.item)).scalar_one()
    book_state = snapshot(engine)
    events = a.turn("model_price", "at 80")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside" and events[1]["replaces"] == first["approval_id"]
    price = next(v for k, v in events[1]["card"]["lines"] if k == "Selling price")
    assert Decimal(price.split()[0]) == Decimal("80.00")
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(models.item)).scalar_one() == before


def test_marked_owner_rate_keeps_main_read_outcome(engine):
    invoice = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [first] = a.turn("marked_owner", "invoice Hooli for 5 copper wire at 5 each")
    book_state = snapshot(engine)
    events = a.turn("marked_owner", "actually 35 each")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert events[0]["code"] == "card_set_aside" and "Total customers:" in events[1]["text"]
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)


def test_different_operation_keeps_main_outcome(engine):
    item = ToolCall("item", "add_item", {"name": "Consulting", "unit": "hour", "price": 50})
    a = Assistant(engine, ScriptedModel([Reply("", [item]), Reply("", [item]), Reply("", [call("list_customers")])]))
    a.turn("different_operation", "Add item Consulting at 50 per hour")
    [first] = a.turn("different_operation", "A service I sell")
    book_state = snapshot(engine)
    events = a.turn("different_operation", "invoice Hooli for 2 copper wire")
    assert_books_not_written(engine, book_state)
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert events[0]["code"] == "card_set_aside" and events[1]["code"] == "item_ambiguous"
    assert events[1]["text"] == "What is the copper wire for? I couldn't place it in this request."
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert_books_not_written(engine, book_state)
