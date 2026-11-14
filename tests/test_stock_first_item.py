"""First stocked item through the owner's card and the real stock services."""
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from bookdiff import assert_changes, snapshot
from erpclaw_assistant.model import Reply
from erpclaw_core import approvals, catalogue, core_contracts as cc, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_stock import service as stock, tables as st
from test_own_books import (SETUP, approve_event, approved, card_text, client, count, last_ask,
                            only_card, own_books, pick, propose, say, said, tool, chat_setup, setup_words)


ARGS = {"name": "Copper Coil", "unit": "meter", "price": "12.00",
        "kind": "stock", "valuation_method": "fifo"}
STOCK_LINE = "Stock: none yet. Receive or count stock to add some."


@pytest.fixture
def ready(engine):
    own_books(engine)
    approved(engine, propose(engine, "set_up_company", SETUP))
    return engine


def preview(engine, **args):
    with engine.connect() as conn:
        return cc.preview_add_item(company_ctx(conn), cc.AddItem(**args))


def refusal(call):
    with pytest.raises(Refusal) as caught:
        call()
    return caught.value


def test_conversation_questions_refusals_and_service_isolation(ready, monkeypatch):
    calls = []
    real = cc.call_service
    monkeypatch.setattr(cc, "call_service", lambda *a: (calls.append(a[2]), real(*a))[1])
    before = snapshot(ready)
    r = refusal(lambda: preview(ready, **(ARGS | {"kind": None})))
    assert (r.code, r.message, r.data["field"]) == (
        "item_kind_missing", "Is this a service you sell, or goods you keep in stock?", "kind")
    assert [c["ref"] for c in r.data["choices"]] == ["service", "stock"] and calls == []
    service = ARGS | {"kind": "service", "valuation_method": None}
    card = preview(ready, **service).card
    assert ("Kind", "a service, not kept in stock") in card.lines
    assert all(k not in dict(card.lines) for k in ("Valuation method", "Warehouse", "Inventory account"))
    assert STOCK_LINE not in card.books and calls == []
    for field, value in (("valuation_method", "fifo"), ("warehouse", "Main Warehouse"),
                         ("inventory_account", "1300")):
        r = refusal(lambda: preview(ready, **(service | {field: value})))
        assert (r.code, r.message) == ("stock_fields_for_service",
            "A valuation method and warehouse apply only to goods kept in stock.")
    for field, code in (("unit", "item_unit_missing"), ("price", "item_price_missing"),
                        ("valuation_method", "valuation_method_missing")):
        r = refusal(lambda: preview(ready, **(ARGS | {field: None})))
        assert r.code == code and r.data["field"] == field
        if field == "valuation_method":
            assert r.message == ("How should these goods be valued? FIFO uses the oldest stock's cost first; "
                                 "moving average uses the average cost of the stock on hand.")
            assert [c["ref"] for c in r.data["choices"]] == ["fifo", "moving_average"]
    with pytest.raises(ValidationError):
        cc.AddItem(**(ARGS | {"price": 12.0}))
    assert_changes(before, snapshot(ready), {})
    result = approved(ready, propose(ready, "add_item", service))
    assert result.text == "I added Copper Coil at 12.00 USD per meter."
    with ready.connect() as conn:
        assert conn.execute(select(m.item.c.kind)).scalar_one() == "service"
    assert not calls or all(name.startswith("stock.list_warehouses") for name in calls)


def test_warehouse_and_account_questions_and_choice_commit(ready):
    with ready.begin() as conn:
        ctx = company_ctx(conn)
        parent = conn.execute(select(m.account.c.parent_id).where(m.account.c.number == "1300")).scalar_one()
        other = new_id()
        conn.execute(m.account.insert().values(id=other, company_id=ctx.company_id, number="1131",
            name="Raw Materials", root_type="asset", account_type="stock", is_group=False,
            parent_id=parent, normal_side="debit", frozen=False))
    r = refusal(lambda: preview(ready, **ARGS))
    assert (r.code, r.data["field"]) == ("account_ambiguous", "inventory_account")
    assert [c["ref"] for c in r.data["choices"]] == ["1131", "1300"]
    c, cid, _ = client(ready, lambda _: Reply("", [tool("add_item", name="Copper Coil", unit="meter", price="12.00")]))
    ask = last_ask(say(c, cid, "Add an item called Copper Coil, sold by meter at 12.00"))
    ask = last_ask(say(c, cid, "", pick(ask, "Goods")))
    ask = last_ask(say(c, cid, "", pick(ask, "FIFO")))
    assert ask["code"] == "account_ambiguous"
    card = only_card(say(c, cid, "1131"))
    assert ["Inventory account", "1131 Raw Materials"] in card["card"]["lines"]
    assert approve_event(c, cid, card)[0]["status"] == "committed"
    with ready.connect() as conn:
        wh = conn.execute(select(st.warehouse)).mappings().one()
        assert wh["account_id"] == other and wh["is_default"]
    r = refusal(lambda: preview(ready, **(ARGS | {"name": "Bronze Tube", "inventory_account": "1300"})))
    assert (r.code, r.message) == ("warehouse_account_fixed", "An existing warehouse already has its inventory account.")
    r = refusal(lambda: preview(ready, **(ARGS | {"name": "Bronze Tube", "warehouse": "Unknown"})))
    assert r.code == "warehouse_unknown" and [c["ref"] for c in r.data["choices"]] == ["Main Warehouse"]


def test_unknown_account_never_replaced(ready):
    before = snapshot(ready)
    r = refusal(lambda: preview(ready, **(ARGS | {"inventory_account": "9999"})))
    assert (r.code, r.message, r.data["field"]) == (
        "account_unknown", "Choose an inventory account from these books.", "inventory_account")
    assert r.data["choices"] == [{"label": "1300 Inventory", "ref": "1300"}]
    assert_changes(before, snapshot(ready), {})


def test_invalid_methods_reach_stock_without_normalisation(ready):
    before = snapshot(ready)
    with ready.connect() as conn:
        ctx = company_ctx(conn)
        for method in ("FIFO", "fifo ", "lifo", ""):
            # Exercise the stock boundary independently of the hidden choice's
            # input validation, which rejects these values even earlier.
            inp = cc.AddItem.model_construct(**(ARGS | {"valuation_method": method}))
            r = refusal(lambda: cc.preview_add_item(ctx, inp))
            assert r.code == "bad_valuation_method"
    assert_changes(before, snapshot(ready), {})


def test_multiple_warehouses_require_choice_and_commit_selected(ready):
    with ready.begin() as conn:
        ctx = company_ctx(conn)
        main = stock.create_warehouse(ctx, "Main Warehouse", default=True)
        west = stock.create_warehouse(ctx, "West Store")
    before = snapshot(ready)
    r = refusal(lambda: preview(ready, **ARGS))
    assert (r.code, r.message, r.data["field"]) == (
        "warehouse_ambiguous", "Which warehouse should this item use?", "warehouse")
    assert [c["ref"] for c in r.data["choices"]] == ["Main Warehouse", "West Store"]
    assert_changes(before, snapshot(ready), {})
    card = propose(ready, "add_item", ARGS | {"warehouse": "West Store"})
    assert ["Warehouse", "West Store"] in card.card["lines"]
    approved(ready, card)
    with ready.connect() as conn:
        assert conn.execute(select(st.stock_item.c.default_warehouse_id)).scalar_one() == west
        assert main != west


def test_card_commit_zero_stock_and_method_token(ready):
    with ready.connect() as conn:
        ctx = company_ctx(conn)
        fifo, avg = cc.AddItem(**ARGS), cc.AddItem(**(ARGS | {"valuation_method": "moving_average"}))
        assert cc.preview_add_item(ctx, fifo).state_token != cc.preview_add_item(ctx, avg).state_token
        assert cc.read_set_add_item(ctx, fifo) == cc.read_set_add_item(ctx, avg)
    before_postings = count(ready, m.posting)
    card = propose(ready, "add_item", ARGS)
    assert dict(card.card["lines"]) == {"Company": "Lakeside Bakery", "Item": "Copper Coil", "Kind": "goods kept in stock",
        "Unit": "meter", "Selling price": "12.00 USD per meter", "Valuation method": "FIFO",
        "Warehouse": "Create Main Warehouse as the company's default warehouse", "Inventory account": "1300 Inventory"}
    assert STOCK_LINE in card.card["books"] and "Nothing is posted." in card.card["books"]
    assert count(ready, m.item) == count(ready, st.warehouse) == 0
    out = approved(ready, card)
    assert out.text == "I added Copper Coil at 12.00 USD per meter. " + STOCK_LINE
    assert "Nothing is posted." not in out.text and count(ready, m.posting) == before_postings
    with ready.connect() as conn:
        ctx = company_ctx(conn)
        item = conn.execute(select(m.item)).mappings().one()
        wh = conn.execute(select(st.warehouse)).mappings().one()
        si = conn.execute(select(st.stock_item)).mappings().one()
        assert (item["kind"], item["standard_rate"], si["method"], si["default_warehouse_id"], wh["is_default"]) == (
            "stock", Decimal("12.00"), "fifo", wh["id"], True)
        assert stock.stock_on_hand(ctx, item["id"], wh["id"])["warehouses"][0] == {
            "warehouse": wh["id"], "name": "Main Warehouse", "qty": "0", "value": "0.00", "currency": "USD"}
        assert conn.execute(select(st.head)).first() is None
    # Later movements take their cost from the opening purchase rate, not the
    # selling price retained by the item card.
    approved(ready, propose(ready, "add_customer", {"name": "Acme"}))
    approved(ready, propose(ready, "record_opening_stock", {"lines": [{"item": "Copper Coil",
        "warehouse": "Main Warehouse", "quantity": "10", "rate": "4.00"}]}))
    approved(ready, propose(ready, "create_invoice", {"customer": "Acme", "items": [
        {"item": "Copper Coil", "quantity": "2"}]}))
    approved(ready, propose(ready, "record_stock_count", {"lines": [
        {"item": "Copper Coil", "warehouse": "Main Warehouse", "counted": "6"}]}))
    with ready.connect() as conn:
        entries = conn.execute(select(st.entry.c.direction, st.entry.c.qty, st.entry.c.value)
                               .order_by(st.entry.c.seq)).all()
        assert [(r.direction, r.qty, r.value) for r in entries] == [
            ("in", Decimal("10"), Decimal("40.00")),
            ("out", Decimal("2"), Decimal("8.00")),
            ("out", Decimal("2"), Decimal("8.00"))]


def test_changed_account_replaces_card_and_freeze_refuses(ready):
    card = propose(ready, "add_item", ARGS)
    with ready.connect() as conn:
        ctx = company_ctx(conn)
        original_refs = cc.read_set_add_item(ctx, cc.AddItem(**ARGS))
        original = approvals.read_set(conn, REGISTRY.contracts["add_item"], ctx, cc.AddItem(**ARGS))
    with ready.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.number == "1300").values(name="Stock on hand"))
    with ready.connect() as conn:
        ctx = company_ctx(conn)
        assert cc.read_set_add_item(ctx, cc.AddItem(**ARGS)) == original_refs
        assert approvals.read_set(conn, REGISTRY.contracts["add_item"], ctx, cc.AddItem(**ARGS)) != original
    result = approvals.approve(ready, card.approval_id, card.digest)
    assert result.status == "superseded" and result.text == approvals.FRESH
    assert ["Inventory account", "1300 Stock on hand"] in result.card["lines"]
    assert count(ready, m.item) == count(ready, st.warehouse) == 0
    fresh = result
    with ready.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.number == "1300").values(frozen=True))
    result = approvals.approve(ready, fresh.approval_id, fresh.digest)
    assert result.status == "superseded" and result.text != approvals.FRESH
    assert count(ready, m.item) == count(ready, st.warehouse) == 0


def test_stock_disabled_keeps_service_and_planned_reply(ready):
    with ready.begin() as conn:
        disable_plugin(conn, "stock")
    before = snapshot(ready)
    r = refusal(lambda: preview(ready, **(ARGS | {"kind": None})))
    assert (r.message, [c["ref"] for c in r.data["choices"]]) == ("Is this a service you sell?", ["service"])
    r = refusal(lambda: preview(ready, **ARGS))
    assert (r.code, r.message) == ("plugin_not_enabled", "Keeping stock isn't switched on in these books.")
    with ready.connect() as conn:
        entries = catalogue.find_capability(conn, "Add an item I keep in stock")
        entry = next(e for e in entries if e.id == "planned.stock_item")
        assert catalogue.decide(entries, entry.id).outcome == "c"
    assert_changes(before, snapshot(ready), {})
    approved(ready, propose(ready, "add_item", ARGS | {"kind": "service", "valuation_method": None}))


def test_disabling_stock_after_preview_supersedes_without_effect(ready):
    card = propose(ready, "add_item", ARGS)
    with ready.begin() as conn:
        disable_plugin(conn, "stock")
    result = approvals.approve(ready, card.approval_id, card.digest)
    assert result.status == "superseded"
    assert "Keeping stock isn't switched on in these books." in result.text
    assert count(ready, m.item) == count(ready, st.warehouse) == 0


def test_pending_then_first_stock_item_through_chat_and_card(engine):
    own_books(engine)
    before = snapshot(engine)
    r = refusal(lambda: propose(engine, "add_item", ARGS))
    assert r.code == "books_not_set_up"
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_item", name="Copper Coil", unit="meter", price="12.00")]))
    assert "set up" in said(say(c, cid, "Add an item called Copper Coil, sold by meter at 12.00")).lower()
    assert_changes(before, snapshot(engine), {})
    c, cid, _ = client(engine, setup_words)
    _, setup_card = chat_setup(c, cid)
    assert approve_event(c, cid, setup_card)[0]["status"] == "committed"
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_item", name="Copper Coil", unit="meter", price="12.00")]))
    ask = last_ask(say(c, cid, "Add an item called Copper Coil, sold by meter at 12.00"))
    assert ask["code"] == "item_kind_missing"
    ask = last_ask(say(c, cid, "", pick(ask, "Goods")))
    assert ask["code"] == "valuation_method_missing"
    card = only_card(say(c, cid, "", pick(ask, "FIFO")))
    assert "Create Main Warehouse" in card_text(card)
    [result] = approve_event(c, cid, card)
    assert result["status"] == "committed" and STOCK_LINE in result["text"]
    with engine.connect() as conn:
        assert conn.execute(select(m.item.c.kind)).scalar_one() == "stock"
        assert conn.execute(select(st.warehouse.c.is_default)).scalar_one() is True
