"""The order card states the same no-tax qualification as an invoice."""
from datetime import date

import pytest

import erpclaw_sales as sales
from erpclaw_core.common import company_ctx
from erpclaw_core.contracts import Shown


TAX_LINE = (
    "Tax",
    "No tax calculated: no tax template is set. That is not a statement that no tax is owed.",
)


def _assert_tax_notice(card, tail):
    lines = [tuple(line) for line in card.lines]
    assert [line for line in lines if line[0] == "Tax"] == [TAX_LINE], "Tax notice must appear exactly once"
    assert [label for label, _ in lines[-len(tail):]] == tail


def _order_card(ctx):
    return sales.preview_sales_order(ctx, sales.CreateSalesOrder(
        customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5}],
        delivery_date=date(2026, 10, 27),
    )).card


def test_order_tax_notice_matches_invoice_after_amount_and_dates(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        invoice = sales.preview_invoice(ctx, sales.CreateInvoice(
            customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5}],
        )).card
        _assert_tax_notice(invoice, ["Total", "Date", "Due", "Tax"])
        _assert_tax_notice(_order_card(ctx), ["Total", "Delivery date", "Tax"])


def test_dropping_order_tax_line_fails_card_assertion(engine, monkeypatch):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        _assert_tax_notice(_order_card(ctx), ["Total", "Delivery date", "Tax"])
        original = Shown.line

        def without_tax(self, label, value):
            if label != "Tax":
                original(self, label, value)

        monkeypatch.setattr(Shown, "line", without_tax)
        with pytest.raises(AssertionError, match="Tax notice must appear exactly once"):
            _assert_tax_notice(_order_card(ctx), ["Total", "Delivery date", "Tax"])
