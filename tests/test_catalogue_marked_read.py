"""Marked invoice corrections with model calls and ambiguous money."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from bookdiff import changes, snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, models
from erpclaw_sales import tables
from ownership_helpers import owner_check


def invoice(quantity=5):
    return call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": quantity}])


@pytest.mark.parametrize("correction,calls", [
    ("actually 35 each", [call("list_customers")]),
    ("actually 35 each", [call("list_items")]),
    ("actually 35 each", [call("get_metric", metric="receivables")]),
    ("make it 35 each", [call("list_customers")]),
    ("no, 35 each", [call("list_customers")]),
    ("actually $35", [call("list_customers")]),
    ("make it $35", [call("list_customers")]),
    ("make it USD 35", [call("list_customers")]),
    ("make it 35 a unit", [call("list_customers")]),
    ("make it 35 per piece", [call("list_customers")]),
    ("no, $35", [call("list_customers")]),
    ("$35 each", [call("list_customers")]),
    ("actually USD 35", [call("list_customers")]),
    ("actually 35 USD", [call("list_customers")]),
    ("no, 35 USD", [call("list_customers")]),
    ("35 USD each", [call("list_customers")]),
    ("actually 35 $", [call("list_customers")]),
    ("actually 35 each", [invoice(7)]),
    ("actually 35 each", [invoice(7), call("list_customers")]),
])
def test_marked_correction_replaces_card_and_ignores_calls(engine, correction, calls):
    a = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", calls)]))
    [first] = a.turn("marked_read", "invoice Hooli for 5 copper wire")
    before = snapshot(engine)
    with engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    events = a.turn("marked_read", correction)
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    assert [e["type"] for e in events] == ["notice", "card"], events
    assert events[0]["code"] == "card_set_aside"
    second = events[1]
    assert second["replaces"] == first["approval_id"]
    lines = dict(second["card"]["lines"])
    assert lines["Customer"].startswith("Hooli")
    assert lines["Item"] == "5 x Copper Wire @ 35.00 = 175.00"
    assert Decimal(lines["Total"].split()[0]) == Decimal("175.00")
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == count
    assert approvals.approve(engine, second["approval_id"], second["digest"], ownership_check=owner_check(engine)).status == "committed"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == count + 1
        row = conn.execute(select(models.party.c.name, models.item.c.name, tables.invoice.c.total,
                                  tables.invoice_line.c.quantity, tables.invoice_line.c.rate,
                                  tables.invoice_line.c.amount).select_from(
            tables.invoice_line.join(tables.invoice, tables.invoice_line.c.invoice_id == tables.invoice.c.id)
            .join(models.party, tables.invoice.c.customer_id == models.party.c.id)
            .join(models.item, tables.invoice_line.c.item_id == models.item.c.id)
        ).order_by(tables.invoice.c.created_at.desc())).first()
        assert row[:2] == ("Hooli", "Copper Wire")
        assert tuple(Decimal(str(value)) for value in row[2:]) == (Decimal("175"), Decimal("5"), Decimal("35"), Decimal("175"))
    assert approvals.approve(engine, second["approval_id"], second["digest"], ownership_check=owner_check(engine)).status == "committed"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == count + 1


@pytest.mark.parametrize("correction,kind", [("actually 35 each and 10%", "ask"), ("actually 35 each on 2026-12-01", "answer"), ("actually 35 each and 7", "ask")])
def test_marked_correction_with_extra_value_keeps_outcome(engine, correction, kind):
    a = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", [call("list_customers")])]))
    [first] = a.turn("marked_decoy", "invoice Hooli for 5 copper wire")
    before = snapshot(engine)
    events = a.turn("marked_decoy", correction)
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    assert [e["type"] for e in events] == ["notice", kind], events
    assert events[0]["code"] == "card_set_aside"
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert set(changes(before, snapshot(engine))) <= {"approval"}


def test_owner_currency_price_keeps_read_outcome(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", [call("list_customers")])]))
    [first] = a.turn("owner_currency", "invoice Hooli for 5 copper wire $5")
    before = snapshot(engine)
    events = a.turn("owner_currency", "actually 35 each")
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert set(changes(before, snapshot(engine))) <= {"approval"}


def test_unclassifiable_earlier_price_keeps_read_outcome(engine, monkeypatch):
    from erpclaw_assistant import resolve_args, resolve_text
    from erpclaw_assistant.resolve_types import Occurrence

    a = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", [call("list_customers")])]))
    [first] = a.turn("unclassified_price", "invoice Hooli for 5 copper wire $5")
    original_inventory = resolve_args.inventory

    def unclassified(ctx, text, items, *args, **kwargs):
        result = original_inventory(ctx, text, items, *args, **kwargs)
        if resolve_text._invoice_rate_context.get() and "copper wire $5" in text:
            return [Occurrence("figure", o.start, o.end, o.said, Decimal("5")) if o.kind == "rate" else o
                    for o in result]
        return result

    monkeypatch.setattr(resolve_args, "inventory", unclassified)
    before = snapshot(engine)
    events = a.turn("unclassified_price", "actually 35 each")
    assert [e["type"] for e in events] == ["notice", "answer"], events
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert set(changes(before, snapshot(engine))) <= {"approval"}


@pytest.mark.parametrize("item_lines", [[], [["Item", "5 x Copper Wire @ 10.00 = 50.00"], ["Item", "1 x Copper Wire @ 10.00 = 10.00"]], [["Item", "5 x Copper Wire @ unknown = 50.00"]], [["Item", "5 x Copper Wire @ 10.00 = 50.00 @ 20.00 = 100.00"]]])
def test_unusable_stored_card_rate_keeps_read_outcome(engine, item_lines):
    import json
    a = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", [call("list_customers")])]))
    [first] = a.turn("unusable_card", "invoice Hooli for 5 copper wire")
    with engine.begin() as conn:
        raw = conn.execute(select(models.approval.c.card_json).where(models.approval.c.id == first["approval_id"])).scalar_one()
        card = json.loads(raw)
        card["lines"] = [row for row in card["lines"] if row[0] != "Item"] + item_lines
        conn.execute(models.approval.update().where(models.approval.c.id == first["approval_id"]).values(card_json=json.dumps(card)))
    before = snapshot(engine)
    events = a.turn("unusable_card", "actually 35 each")
    assert [e["type"] for e in events] == ["notice", "answer"]
    assert set(changes(before, snapshot(engine))) <= {"approval"}
    assert approvals.approve(engine, first["approval_id"], first["digest"], ownership_check=owner_check(engine)).status == "superseded"
    assert set(changes(before, snapshot(engine))) <= {"approval"}
