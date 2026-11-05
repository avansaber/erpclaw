"""The stock-backed sales answer keys (tests/expected_books/), end to end through real cards: an invoice that takes
stock from the warehouse that holds it, a sales order above the stock on hand, an order delivered in two parts then
invoiced once, and credit notes that bring stock back at the cost it left at and credit the invoice, never pay it.
Every figure is the key's; each test ends with the stock books agreeing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from erpclaw_core import models as m
from erpclaw_sales import tables as sales_t

from sales_books import Books

D = Decimal


def invoice_row(books: Books, number: str | None = None) -> dict:
    with books.engine.connect() as conn:
        q = select(sales_t.invoice).where(sales_t.invoice.c.company_id == books.company)
        q = q.where(sales_t.invoice.c.number == number) if number else q.order_by(sales_t.invoice.c.created_at.desc())
        return dict(conn.execute(q).mappings().first())


def count(books: Books, table) -> int:
    with books.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# ---------------------------------------------------------------- invoice_stock_from_stocked_warehouse.md

@pytest.fixture
def stark(engine):
    b = Books(engine, "Stark Manufacturing Inc.")
    raw = b.stock_account("1131", "Raw Materials")
    b.warehouse("Stark Manufacturing Inc. - Main Warehouse", raw, default=True)
    b.warehouse("Raw Materials Store", raw)
    b.warehouse("Finished Goods Store", b.stock_account("1133", "Finished Goods"))
    b.customer("Acme Corporation")
    return b


@pytest.mark.parametrize("price, sales", [("28.00", "140.00"), ("35.00", "175.00")])  # the key, and its variant b
def test_an_invoice_takes_stock_from_the_only_warehouse_holding_it(stark, price, sales):
    stark.item("Copper Wire", price)
    stark.receive("Copper Wire", "Raw Materials Store", "200", "28.00", date(2026, 1, 1))
    card, out = stark.run("create_invoice", customer="Acme Corporation",
                          items=[{"item": "Copper Wire", "quantity": 5}])
    lines = [tuple(x) for x in card.card["lines"]]
    assert any(label == "From" and value.startswith("Raw Materials Store") for label, value in lines), lines
    assert not any(stark.company in v or stark.ids["Raw Materials Store"] in v for _, v in lines)  # no ids
    assert out.status == "committed", out.text
    inv = invoice_row(stark)
    assert (inv["total"], inv["outstanding"], inv["status"]) == (D(sales), D(sales), "submitted")
    assert stark.posting("sales_invoice", inv["id"]) == sorted([
        ("1200", "Acme Corporation", D(sales), D("0.00")), ("4100", None, D("0.00"), D(sales)),
        ("5100", None, D("140.00"), D("0.00")), ("1131", None, D("0.00"), D("140.00"))])
    assert stark.stock_rows(inv["id"]) == [("out", "Raw Materials Store", D("5"), D("140.00"))]
    assert stark.head("Copper Wire", "Raw Materials Store") == (D("195"), D("5460.00"))
    assert stark.head("Copper Wire", "Stark Manufacturing Inc. - Main Warehouse") == (D(0), D("0.00"))  # no transfer
    stark.verify()


# ---------------------------------------------------------------- sales_order_above_available_stock.md

@pytest.mark.parametrize("price, total", [("15.00", "7500.00"), ("22.00", "11000.00")])
def test_an_order_above_stock_states_the_shortfall_and_is_saved_whole(stark, price, total):
    stark.customer("Wayne Industries")
    stark.item("Circuit Board", price)
    stark.receive("Circuit Board", "Raw Materials Store", "200", "15.00", date(2026, 1, 1))
    stark.receive("Circuit Board", "Raw Materials Store", "100", "15.00", date(2026, 1, 12))
    before = (count(stark, m.posting), count(stark, m.ledger_entry))
    card, out = stark.run("create_sales_order", customer="Wayne Industries",
                          items=[{"item": "Circuit Board", "quantity": 500}], delivery_date="2026-10-27")
    text = " ".join(v for _, v in card.card["lines"])
    assert "300 available" in text and "200 short" in text, card.card["lines"]
    assert out.status == "committed", out.text
    with stark.engine.connect() as conn:
        order = dict(conn.execute(select(sales_t.order).where(sales_t.order.c.company_id == stark.company))
                     .mappings().one())
        qty = conn.execute(select(sales_t.order_line.c.quantity).where(
            sales_t.order_line.c.order_id == order["id"])).scalar_one()
    assert qty == D("500") and order["total"] == D(total) and order["status"] == "submitted"  # never 50, never 300
    assert (count(stark, m.posting), count(stark, m.ledger_entry)) == before  # no ledger, no stock, no reservation
    assert stark.head("Circuit Board", "Raw Materials Store") == (D("300"), D("4500.00"))


# ---------------------------------------------------------------- sales_order_delivered_in_two_parts.md

def test_an_order_delivered_in_two_parts_then_invoiced_once(engine):
    b = Books(engine, "Oscorp Supplier")
    b.warehouse("Main Warehouse", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Oscorp Industries")
    b.item("Bearing 6205", "9.95", method="fifo")
    b.receive("Bearing 6205", "Main Warehouse", "60", "6.25", date(2026, 5, 1))
    b.receive("Bearing 6205", "Main Warehouse", "50", "6.70", date(2026, 5, 4))
    _, so = b.run("create_sales_order", customer="Oscorp Industries", items=[{"item": "Bearing 6205", "quantity": 80}],
                  delivery_date="2026-05-15")
    order = so.data["number"]
    _, d1 = b.run("deliver_order", order=order, items=[{"item": "Bearing 6205", "quantity": 50}], date="2026-05-12")
    assert d1.status == "committed" and b.head("Bearing 6205", "Main Warehouse") == (D("60"), D("397.50"))
    _, d2 = b.run("deliver_order", order=order, date="2026-05-15")  # "the rest": exactly 30
    assert d2.status == "committed" and b.head("Bearing 6205", "Main Warehouse") == (D("30"), D("201.00"))
    _, inv = b.run("create_invoice", customer="Oscorp Industries", order=order)
    assert inv.status == "committed", inv.text
    row = invoice_row(b)
    assert b.posting("sales_invoice", row["id"]) == sorted([("1200", "Oscorp Industries", D("796.00"), D("0.00")),
                                                            ("4100", None, D("0.00"), D("796.00"))])  # no cost again
    assert b.stock_rows(row["id"]) == []  # the invoice moves no stock
    with b.engine.connect() as conn:
        status = conn.execute(select(sales_t.order.c.status).where(sales_t.order.c.number == order)).scalar_one()
    assert status == "completed"
    b.verify()


# ---------------------------------------------------------------- credit_note_full_return.md (cost, not price)

@pytest.mark.parametrize("returns_role, income", [(False, "4100"), (True, "4130")])  # the key, and its 4130 variant
def test_a_full_credit_brings_stock_back_at_its_cost_and_credits_the_invoice(engine, returns_role, income):
    b = Books(engine, "Hooli Supplier", returns_role=returns_role)
    b.warehouse("Finished Goods Store", b.stock_account("1133", "Finished Goods"), default=True)
    b.customer("Hooli Technologies")
    b.item("Motor Housing Unit", "349.00")
    b.receive("Motor Housing Unit", "Finished Goods Store", "15", "210.00", date(2026, 1, 1))
    _, sold = b.run("create_invoice", customer="Hooli Technologies",
                    items=[{"item": "Motor Housing Unit", "quantity": 8}])
    inv = invoice_row(b)
    payments_before = count(b, sales_t.payment)
    card, out = b.run("create_credit_note", invoice=inv["number"], everything=True)
    text = " ".join(v for _, v in card.card["lines"])
    assert "submitted" in text and "2,792.00" in text and "nothing paid" in text, card.card["lines"]
    assert out.status == "committed", out.text
    with b.engine.connect() as conn:
        cn = dict(conn.execute(select(sales_t.credit_note).where(sales_t.credit_note.c.company_id == b.company))
                  .mappings().one())
    assert b.posting("sales_credit_note", cn["id"]) == sorted([
        (income, None, D("2792.00"), D("0.00")), ("1200", "Hooli Technologies", D("0.00"), D("2792.00")),
        ("1133", None, D("1680.00"), D("0.00")), ("5100", None, D("0.00"), D("1680.00"))])
    assert b.stock_rows(cn["id"]) == [("in", "Finished Goods Store", D("8"), D("1680.00"))]  # 210.00 each, not 349.00
    after = invoice_row(b, inv["number"])
    assert (after["outstanding"], after["status"]) == (D("0.00"), "credited")
    assert count(b, sales_t.payment) == payments_before  # no payment, no refund
    b.verify()


# ---------------------------------------------------------------- credit_note_partial_return_after_cost_change.md

def test_a_partial_credit_after_a_cost_change_returns_at_the_issue_cost(engine, monkeypatch):
    """The key's own dates, each step on its day (the test clock moved per step)."""
    b = Books(engine, "Wayne Supplier", returns_role=False)
    b.warehouse("Main Warehouse", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Wayne Industries")
    b.item("Circuit Board", "24.99")
    b.receive("Circuit Board", "Main Warehouse", "40", "15.00", date(2026, 3, 31))
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-04-01")
    _, sold = b.run("create_invoice", customer="Wayne Industries", items=[{"item": "Circuit Board", "quantity": 12}])
    inv = invoice_row(b)
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-04-03")
    _, paid = b.run("record_payment", customer="Wayne Industries", amount="100.00", invoice=inv["number"])
    assert paid.status == "committed", paid.text  # payments are switched on in the tests (every_feature)
    b.receive("Circuit Board", "Main Warehouse", "20", "18.30", date(2026, 4, 6))
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-04-08")
    _, out = b.run("create_credit_note", invoice=inv["number"], items=[{"item": "Circuit Board", "quantity": 5}])
    assert out.status == "committed", out.text
    with b.engine.connect() as conn:
        cn = dict(conn.execute(select(sales_t.credit_note).where(sales_t.credit_note.c.company_id == b.company))
                  .mappings().one())
    assert ("1131", None, D("75.00"), D("0.00")) in b.posting("sales_credit_note", cn["id"])  # not 81.88
    after = invoice_row(b, inv["number"])
    assert (after["outstanding"], after["status"]) == (D("74.93"), "partly_paid")
    b.verify()
