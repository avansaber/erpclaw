"""Committed stock results state and declare the money they moved."""
from sqlalchemy import select

from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock


def _commit(engine, name, args):
    card = approvals.propose(engine, REGISTRY.contracts[name], args)
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed", result.text
    return result


def _opening(engine, quantity, rate):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Main Store", default=True)
        item = conn.execute(select(m.item.c.id).where(
            m.item.c.company_id == ctx.company_id, m.item.c.name == "Copper Wire"
        )).scalar_one()
        stock.make_stock_item(ctx, item, "moving_average")
    return _commit(engine, "record_opening_stock", {"lines": [
        {"item": "Copper Wire", "warehouse": "Main Store", "quantity": quantity, "rate": rate}
    ]})


def test_cancel_stock_movement_result_names_its_stock_value(engine):
    opening = _opening(engine, "3", "115")
    result = _commit(engine, "cancel_stock_movement", {
        "document": opening.data["number"], "date": "2026-10-26"
    })

    assert result.text == (
        f"Cancelled {opening.data['number']} with reversal entries dated 26 Oct 2026; "
        "345.00 USD of stock value is reversed."
    )
    assert REGISTRY.contracts["cancel_stock_movement"].amounts == ("value",)
    assert result.amounts == [{
        "what": "value", "of": opening.data["number"], "value": "345.00", "currency": "USD"
    }]


def test_record_stock_count_result_names_the_value_it_adjusted(engine):
    _opening(engine, "4", "12.50")
    result = _commit(engine, "record_stock_count", {"lines": [
        {"item": "Copper Wire", "warehouse": "Main Store", "counted": "3"}
    ]})

    assert result.text == (
        f"Recorded stock count {result.data['number']}, dated 26 Oct 2026: "
        "1 stock adjustment, 12.50 USD written off."
    )
    assert REGISTRY.contracts["record_stock_count"].amounts == ("value",)
    assert result.amounts == [{
        "what": "value", "of": f"{result.data['number']} written off", "value": "12.50", "currency": "USD"
    }]


def test_stock_count_surplus_result_names_value_added(engine):
    _opening(engine, "4", "12.50")
    result = _commit(engine, "record_stock_count", {"lines": [
        {"item": "Copper Wire", "warehouse": "Main Store", "counted": "5"}
    ]})

    assert result.text == (
        f"Recorded stock count {result.data['number']}, dated 26 Oct 2026: "
        "1 stock adjustment, 12.50 USD added."
    )
    assert result.amounts == [{
        "what": "value", "of": f"{result.data['number']} added", "value": "12.50", "currency": "USD"
    }]


def test_stock_count_shortage_result_names_value_written_off(engine):
    _opening(engine, "1", "12.50")
    result = _commit(engine, "record_stock_count", {"lines": [
        {"item": "Copper Wire", "warehouse": "Main Store", "counted": "0"}
    ]})

    assert result.text == (
        f"Recorded stock count {result.data['number']}, dated 26 Oct 2026: "
        "1 stock adjustment, 12.50 USD written off."
    )
    assert result.amounts == [{
        "what": "value", "of": f"{result.data['number']} written off", "value": "12.50", "currency": "USD"
    }]


def test_stock_count_mixed_result_names_both_values_in_text_order(engine):
    _opening(engine, "4", "2.50")
    with engine.begin() as conn:
        stock.create_warehouse(company_ctx(conn), "Back Store")
    _commit(engine, "record_opening_stock", {"lines": [
        {"item": "Copper Wire", "warehouse": "Back Store", "quantity": "4", "rate": "2.50"}
    ]})
    result = _commit(engine, "record_stock_count", {"lines": [
        {"item": "Copper Wire", "warehouse": "Main Store", "counted": "3"},
        {"item": "Copper Wire", "warehouse": "Back Store", "counted": "8"}
    ]})

    assert result.text == (
        f"Recorded stock count {result.data['number']}, dated 26 Oct 2026: "
        "2 stock adjustments, 10.00 USD added and 2.50 USD written off."
    )
    assert REGISTRY.contracts["record_stock_count"].amounts == ("value",)
    assert result.amounts == [
        {"what": "value", "of": f"{result.data['number']} added", "value": "10.00", "currency": "USD"},
        {"what": "value", "of": f"{result.data['number']} written off", "value": "2.50", "currency": "USD"}
    ]
