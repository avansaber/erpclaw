"""Where a credit note's stock goes back, and at what cost: against the deliveries an invoice line covers, latest
delivery first, each part at its own issue's cost and into its own warehouse; and, when an item is on several lines
of the invoice, against the line the owner names. The card shows the split, inside its digest."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from erpclaw_core.common import Refusal
from erpclaw_sales import tables as sales_t

from sales_books import Books

D = Decimal


@pytest.fixture
def delivered(engine):
    """sales_order_delivered_in_two_parts.md through its invoice: 50 delivered at 312.50, then 30 at 196.50."""
    b = Books(engine, "Oscorp Supplier")
    b.warehouse("Main Warehouse", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Oscorp Industries")
    b.item("Bearing 6205", "9.95", method="fifo")
    b.receive("Bearing 6205", "Main Warehouse", "60", "6.25", date(2026, 5, 1))
    b.receive("Bearing 6205", "Main Warehouse", "50", "6.70", date(2026, 5, 4))
    _, so = b.run("create_sales_order", customer="Oscorp Industries", items=[{"item": "Bearing 6205", "quantity": 80}],
                  delivery_date="2026-05-15")
    b.ids["order"] = so.data["number"]
    b.run("deliver_order", order=b.ids["order"], items=[{"item": "Bearing 6205", "quantity": 50}], date="2026-05-12")
    b.run("deliver_order", order=b.ids["order"], date="2026-05-15")
    _, inv = b.run("create_invoice", customer="Oscorp Industries", order=b.ids["order"])
    b.ids["invoice"] = inv.data["number"]
    return b


def _credit(b: Books) -> dict:
    with b.engine.connect() as conn:
        return dict(conn.execute(select(sales_t.credit_note).where(sales_t.credit_note.c.company_id == b.company)
                                 .order_by(sales_t.credit_note.c.created_at.desc())).mappings().first())


def test_a_return_goes_against_the_latest_delivery_first_at_its_cost(delivered):
    card, out = delivered.run("create_credit_note", invoice=delivered.ids["invoice"],
                              items=[{"item": "Bearing 6205", "quantity": 5}])
    assert out.status == "committed", out.text
    text = " ".join(v for _, v in card.card["lines"])
    assert "32.75" in text  # the card names the delivery and the cost the owner approves
    cn = _credit(delivered)
    assert delivered.stock_rows(cn["id"]) == [("in", "Main Warehouse", D("5"), D("32.75"))]  # 196.50 x 5 / 30, not 31.25
    delivered.verify()


def test_a_return_spanning_both_deliveries_splits_at_each_cost(delivered):
    _, out = delivered.run("create_credit_note", invoice=delivered.ids["invoice"],
                           items=[{"item": "Bearing 6205", "quantity": 40}])
    assert out.status == "committed", out.text
    cn = _credit(delivered)
    rows = delivered.stock_rows(cn["id"])
    assert rows == [("in", "Main Warehouse", D("30"), D("196.50")), ("in", "Main Warehouse", D("10"), D("62.50"))]
    assert sum(v for *_, v in rows) == D("259.00")
    with delivered.engine.connect() as conn:  # each part recorded with its original voucher and line
        parts = conn.execute(select(sales_t.credit_note_return).where(
            sales_t.credit_note_return.c.company_id == delivered.company)).mappings().all()
    assert len(parts) == 2 and all(p["original_voucher_id"] and p["original_line"] for p in parts)
    delivered.verify()


def test_the_split_is_inside_the_cards_digest(delivered):
    first = delivered.run("create_credit_note", invoice=delivered.ids["invoice"],
                          items=[{"item": "Bearing 6205", "quantity": 5}])[0]
    from erpclaw_core import approvals
    from erpclaw_core.plugins import REGISTRY
    second = approvals.propose(delivered.engine, REGISTRY.contracts["create_credit_note"], dict(
        invoice=delivered.ids["invoice"], items=[{"item": "Bearing 6205", "quantity": 30}]),
        company_id=delivered.company)
    # the first credit took 5 from the latest delivery, so 30 now spans both: a different split, a different card
    text = " ".join(v for _, v in second.card["lines"])
    assert "25" in text and second.digest != first.digest


# ---------------------------------------------------------------- an item on two lines of one invoice

@pytest.fixture
def two_lines(engine):
    b = Books(engine, "Two Lines Co")
    raw = b.stock_account("1131", "Raw Materials")
    b.warehouse("Raw Materials Store", raw, default=True)
    b.warehouse("Main", raw)
    b.customer("Acme Corporation")
    b.item("Copper Wire", "28.00")
    b.receive("Copper Wire", "Raw Materials Store", "10", "20.00", date(2026, 1, 1))
    b.receive("Copper Wire", "Main", "10", "22.00", date(2026, 1, 1))
    _, inv = b.run("create_invoice", customer="Acme Corporation", items=[
        {"item": "Copper Wire", "quantity": 5, "warehouse": "Raw Materials Store"},
        {"item": "Copper Wire", "quantity": 3, "warehouse": "Main"}])
    assert inv.status == "committed", inv.text
    b.ids["invoice"] = inv.data["number"]
    return b


def test_a_credit_for_an_item_on_two_lines_asks_which_line(two_lines):
    with pytest.raises(Refusal) as caught:
        two_lines.run("create_credit_note", invoice=two_lines.ids["invoice"],
                      items=[{"item": "Copper Wire", "quantity": 2}])
    q = caught.value
    assert q.code == "invoice_line_ambiguous" and q.data["field"] == "invoice_line"
    assert [c["ref"] for c in q.data["choices"]] == [1, 2] and "Main" in q.options[1]  # by number, never ids


def test_a_credit_naming_its_line_returns_to_that_lines_warehouse_at_its_cost(two_lines):
    _, out = two_lines.run("create_credit_note", invoice=two_lines.ids["invoice"],
                           items=[{"item": "Copper Wire", "quantity": 2, "line": 2}])
    assert out.status == "committed", out.text
    cn = _credit(two_lines)
    assert two_lines.stock_rows(cn["id"]) == [("in", "Main", D("2"), D("44.00"))]
    with two_lines.engine.connect() as conn:
        line = conn.execute(select(sales_t.credit_note_line).where(
            sales_t.credit_note_line.c.credit_note_id == cn["id"])).mappings().one()
    assert line["amount"] == D("56.00") and line["invoice_line_id"]  # 2 x 28.00, against line 2
    two_lines.verify()


def test_everything_credits_each_line_against_itself(two_lines):
    _, out = two_lines.run("create_credit_note", invoice=two_lines.ids["invoice"], everything=True)
    assert out.status == "committed", out.text
    cn = _credit(two_lines)
    assert sorted(two_lines.stock_rows(cn["id"])) == sorted([("in", "Raw Materials Store", D("5"), D("100.00")),
                                                             ("in", "Main", D("3"), D("66.00"))])
    two_lines.verify()
