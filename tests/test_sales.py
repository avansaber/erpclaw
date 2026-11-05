"""Invoices, numbering, payments, cancellation."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as s
from erpclaw_sales import tables as t


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def approve_write(engine, contract, **args):
    card = approvals.propose(engine, REGISTRY.contracts[contract], args)
    return card, approvals.approve(engine, card.approval_id, card.digest)


def test_07_invoice_140_two_ledger_lines(engine):
    le_before = count(engine, m.ledger_entry)
    card, out = approve_write(engine, "create_invoice", customer="Acme Corporation",
                              items=[{"item": "copper wire", "quantity": 5}])
    assert ("Total", "140.00 USD") in [tuple(x) for x in card.card["lines"]]
    assert out.status == "committed" and "140.00 USD" in out.text
    with engine.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == out.operation_key)).mappings().one()
    assert inv["total"] == Decimal("140.00") and inv["status"] == "submitted"
    assert count(engine, m.ledger_entry) == le_before + 2


def test_08_quantity_500_saved_as_500(engine):
    _, out = approve_write(engine, "create_invoice", customer="Hooli", items=[{"item": "Copper Wire", "quantity": 500}])
    with engine.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == out.operation_key)).mappings().one()
        line = conn.execute(select(t.invoice_line).where(t.invoice_line.c.invoice_id == inv["id"])).mappings().one()
    assert line["quantity"] == Decimal("500") and inv["total"] == Decimal("14000.00")


def test_09_number_uses_posting_date_fiscal_year(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        ctx.today = date(2027, 1, 2)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        item = dict(conn.execute(select(m.item).where(m.item.c.name == "Copper Wire")).mappings().one())
        r = s.create_invoice(ctx, cust, [(item, Decimal("1"))], date(2026, 12, 31))
    assert r["number"].startswith("INV-2026-")


def test_10_unknown_customer_writes_nothing_and_asks(engine):
    before = count(engine, t.invoice), count(engine, m.approval)
    with pytest.raises(Refusal) as e:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                          {"customer": "ABC", "items": [{"item": "circuit board", "quantity": 3}]})
    assert e.value.code == "customer_unknown" and "Known customers" not in e.value.message  # never the table
    assert (count(engine, t.invoice), count(engine, m.approval)) == before


def test_11_payment_clears_invoice(engine):
    _, out = approve_write(engine, "record_payment", customer="Pied Piper", amount="2050.00", oldest_first=True, method="bank transfer")
    assert out.status == "committed"
    with engine.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.posting_date == date(2026, 6, 9))).mappings().one()
        pay = conn.execute(select(t.payment).where(t.payment.c.operation_key == out.operation_key)).mappings().one()
    assert inv["outstanding"] == Decimal("0.00") and inv["status"] == "paid"
    assert pay["method"] == "bank transfer" and pay["unallocated"] == Decimal("0.00")


def test_12_cancel_reverses_and_paid_invoice_refused(engine):
    _, out = approve_write(engine, "create_invoice", customer="Initech", items=[{"item": "Circuit Board", "quantity": 1}])
    number = out.text.split("invoice ")[1].split(" ")[0]
    _, c = approve_write(engine, "cancel_invoice", invoice=number)
    assert c.status == "committed" and f"Cancelled {number}" in c.text and "the original entries are kept" in c.text
    with engine.connect() as conn:
        paid = conn.execute(select(t.invoice.c.number).where(t.invoice.c.status == "paid")).scalars().first()
    before = count(engine, m.approval)
    with pytest.raises(Refusal) as e:  # a payment still stands on it: refused before any card
        approvals.propose(engine, REGISTRY.contracts["cancel_invoice"], {"invoice": paid})
    assert e.value.code == "has_payments" and count(engine, m.approval) == before


def test_11b_amounts_are_stored_in_currency_form(engine):
    from sqlalchemy import text
    _, out = approve_write(engine, "record_payment", customer="Pied Piper", amount=500, oldest_first=True, method="check")
    with engine.connect() as conn:
        raw = conn.execute(text("select amount from sales_payment where operation_key = :k"),
                           {"k": out.operation_key}).scalar_one()
        led = conn.execute(text("select e.debit from ledger_entry e join posting p on p.id = e.posting_id "
                                "where p.voucher_type = 'sales_payment' and e.debit <> '0.00' "
                                "order by p.seq desc")).scalars().first()
    assert raw == "500.00" and led == "500.00"


def test_10b_name_inside_extra_words_is_a_confirm_never_a_silent_match(engine):
    """Masters never match silently inside extra words (the resolution step's rule A): one candidate is a confirm."""
    from erpclaw_core.masters import find_customer
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as e:
            find_customer(ctx, "bill hooli")
        assert (e.value.code, e.value.options) == ("customer_ambiguous", ["Hooli"])
        with pytest.raises(Refusal):
            find_customer(ctx, "hooli and initech")


def test_list_totals_leave_out_drafts_and_cancelled(engine):
    from erpclaw_sales import ListInvoices, list_invoices
    with engine.connect() as conn:
        r = list_invoices(company_ctx(conn), ListInvoices(status="all"))
    assert "Submitted invoices total 16,100.00 USD, still to collect 4,870.00 USD (2 draft or cancelled invoices not counted)" in r.text
