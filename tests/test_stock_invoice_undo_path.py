"""A stored, generated invoice number carries the stock undo through preview and approval."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from bookdiff import snapshot
from erpclaw_assistant import resolve
from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import CancelInvoice, preview_cancel, tables as sales
from erpclaw_stock import service as stock


def test_generated_stock_invoice_number_drives_the_approved_service_undo(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                             m.item.c.name == "Copper Wire")).scalar_one()
        warehouse = stock.create_warehouse(ctx, "Raw Materials Store", default=True)
        stock.make_stock_item(ctx, item, "moving_average", default_warehouse=warehouse)
        trusted = stock.Trusted("starting stock for the complete owner path")
        stock.receive(ctx, "starting-receipt", item, warehouse, "200", "28.00", date(2026, 10, 25), trusted=trusted)
        trusted.verify(ctx)
    creation = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {
        "customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 5}], "submit": True})
    created = approvals.approve(engine, creation.approval_id, creation.digest)
    assert created.status == "committed", created
    with engine.connect() as conn:
        invoice = conn.execute(select(sales.invoice).where(
            sales.invoice.c.operation_key == created.operation_key)).mappings().one()
        number = invoice["number"]
        assert stock.on_hand(company_ctx(conn), item, warehouse) == (Decimal(195), Decimal("5460.00"))
    other = "INV-2026-00005"
    assert other != number
    before = snapshot(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        resolved = resolve.resolve_args(ctx, REGISTRY.contracts["cancel_invoice"], {"invoice": other},
                                        f"undo invoice {number}", resolve.Session())
        preview = preview_cancel(ctx, CancelInvoice.model_validate(resolved.args)).card.as_dict()
    assert dict(preview["lines"])["Invoice"] == number
    assert other not in str(preview)
    assert "Raw Materials Store: restore 5" in str(preview)
    card = approvals.propose(engine, REGISTRY.contracts["cancel_invoice"], resolved.args)
    assert dict(card.card["lines"])["Invoice"] == number
    assert "Raw Materials Store: restore 5" in str(card.card)
    assert before["posting"] == snapshot(engine)["posting"]
    assert before["stock_ledger_entry"] == snapshot(engine)["stock_ledger_entry"]
    undone = approvals.approve(engine, card.approval_id, card.digest)
    assert undone.status == "committed", undone
    with engine.connect() as conn:
        assert conn.execute(select(sales.invoice.c.status).where(
            sales.invoice.c.id == invoice["id"])).scalar_one() == "cancelled"
        assert stock.on_hand(company_ctx(conn), item, warehouse) == (Decimal(200), Decimal("5600.00"))
        assert stock.verify_stock(company_ctx(conn)) == []
