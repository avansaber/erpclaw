"""Disposable starting books for the five public keys."""
from datetime import date
import os
from sqlalchemy import select, update

from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock

CUSTOMERS = ("Acme Corporation", "Wayne Industries", "Oscorp Industries", "Pied Piper Inc",
             "Hooli Technologies", "Stark Retail Outlets", "Parker Electronics", "Xavier Laboratories",
             "LexCorp", "Daily Planet Media", "Initech")


def seed_gate_books(engine, case=None):
    """Keep the original invoice baseline; add only the selected key's starting books."""
    cid = create_company(engine, "Stark Manufacturing Inc.")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=cid)
        # Account numbers are mapped by role under the public conventions.
        for number, name in (("1200", "Trade Receivables"), ("4100", "Sales Revenue"),
                             ("5100", "COGS - Materials")):
            conn.execute(update(m.account).where(m.account.c.company_id == cid,
                                                m.account.c.number == number).values(name=name))
        parent = conn.execute(select(m.account.c.parent_id).where(m.account.c.company_id == cid,
                                                                 m.account.c.number == "1300")).scalar_one()
        accounts = {}
        for number, name in (("1131", "Raw Materials"), ("1133", "Finished Goods")):
            accounts[name] = new_id()
            conn.execute(m.account.insert().values(id=accounts[name], company_id=cid, number=number, name=name,
                         root_type="asset", account_type="stock", is_group=False, parent_id=parent,
                         normal_side="debit"))
        for name in CUSTOMERS:
            conn.execute(m.party.insert().values(id=new_id(), company_id=cid, kind="customer", name=name))
        main = stock.create_warehouse(ctx, "Stark Manufacturing Inc. - Main Warehouse", accounts["Raw Materials"],
                                      default=True)
        raw = stock.create_warehouse(ctx, "Raw Materials Store", accounts["Raw Materials"])
        stock.create_warehouse(ctx, "Finished Goods Store", accounts["Finished Goods"])
        wire = new_id()
        conn.execute(m.item.insert().values(id=wire, company_id=cid, name="Copper Wire", unit="Meter",
                                           standard_rate="28.00"))
        stock.make_stock_item(ctx, wire, "moving_average", main)
        trusted = stock.Trusted("public invoice key's starting stock")
        stock.receive(ctx, new_id(), wire, raw, "200", "28.00", date(2026, 1, 1), trusted=trusted)
        trusted.verify(ctx)
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                            {"customer": "Initech", "items": [{"item": "Copper Wire", "quantity": 2}],
                             "submit": False}, company_id=cid)
    out = approvals.approve(engine, card.approval_id, card.digest)
    if out.status != "committed":
        raise RuntimeError(f"seed draft: {out.status}: {out.text}")
    operation = case.operation if case else "create_invoice"
    if operation in {"create_invoice", ""}:
        return
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=cid)
        if operation == "create_sales_order":
            name, rate, qty, cost, warehouse = "Circuit Board", "15.00", "200", "15.00", raw
        elif operation == "create_credit_note":
            name, rate, qty, cost = "Motor Housing Unit", "349.00", "15", "210.00"
            from erpclaw_stock import tables as st
            warehouse = conn.execute(select(st.warehouse.c.id).where(
                st.warehouse.c.name == "Finished Goods Store")).scalar_one()
            # This primary key uses sales income; the separate returns-account variant is not selected.
            conn.execute(update(m.account).where(m.account.c.company_id == cid,
                         m.account.c.account_type == "sales_returns").values(account_type=None))
        else:
            name, rate, qty, cost, warehouse = "Payment invoice goods", "2235.00", "1", "1000.00", raw
            conn.execute(update(m.account).where(m.account.c.company_id == cid,
                         m.account.c.account_type == "bank").values(number="1112", name="Operating Checking Account"))
            parent = conn.execute(select(m.account.c.parent_id).where(
                m.account.c.company_id == cid, m.account.c.number == "1112")).scalar_one()
            conn.execute(m.account.insert().values(id=new_id(), company_id=cid, number="1113",
                         name="Savings Account", root_type="asset", account_type="bank", is_group=False,
                         parent_id=parent, normal_side="debit"))
        iid = new_id()
        conn.execute(m.item.insert().values(id=iid, company_id=cid, name=name, unit="Nos", standard_rate=rate))
        stock.make_stock_item(ctx, iid, "moving_average", warehouse)
        trusted = stock.Trusted("the selected public key's starting stock")
        stock.receive(ctx, new_id(), iid, warehouse, qty, cost, date(2026, 1, 1), trusted=trusted)
        trusted.verify(ctx)
        if operation == "create_sales_order":
            trusted = stock.Trusted("the public order key's second receipt")
            stock.receive(ctx, new_id(), iid, warehouse, "100", cost, date(2026, 1, 12), trusted=trusted)
            trusted.verify(ctx)
    if operation == "create_sales_order":
        return
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=cid)
        customer = "Hooli Technologies" if operation == "create_credit_note" else "Pied Piper Inc"
        conn.execute(update(m.number_series).where(m.number_series.c.company_id == cid,
                     m.number_series.c.series == "INV").values(
                         next_value=5 if operation == "create_credit_note" else 4))
    # Stock-backed invoices require the approval service's commit context even during bootstrap.
    prior_date = os.environ.get("ERPCLAW_TODAY")
    try:
        os.environ["ERPCLAW_TODAY"] = "2026-01-20"
        card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                                {"customer": customer, "items": [{"item": name,
                                 "quantity": 8 if operation == "create_credit_note" else 1}],
                                 "submit": True}, company_id=cid)
        out = approvals.approve(engine, card.approval_id, card.digest)
    finally:
        if prior_date is None:
            os.environ.pop("ERPCLAW_TODAY", None)
        else:
            os.environ["ERPCLAW_TODAY"] = prior_date
    if out.status != "committed":
        raise RuntimeError(f"seed invoice: {out.status}: {out.text}")
