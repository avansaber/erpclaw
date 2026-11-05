"""Demo books for one company, built through the engine's own services."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Engine

from erpclaw_core import models as m
from erpclaw_core.common import Ctx
from erpclaw_core.db import mark_demo
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.setup import create_company

from . import service as s

CUSTOMERS = [("Acme Corporation", "billing@acme.example", "Pune"),
             ("Wayne Enterprises", "ap@wayne.example", "Gotham"),
             ("Pied Piper", "finance@piedpiper.example", "Palo Alto"),
             ("Hooli", "payables@hooli.example", "Mountain View"),
             ("Initech", "accounts@initech.example", "Pune")]
ITEMS = [("Copper Wire", "28.00"), ("Circuit Board", "45.00"), ("Motor Housing Unit", "120.00")]


def seed_demo(engine: Engine) -> None:
    """Stark Manufacturing with a short, realistic history."""
    cid = create_company(engine, "Stark Manufacturing")
    mark_demo(engine)
    with engine.begin() as conn:
        ctx = Ctx(conn, cid, "Stark Manufacturing", "USD", date(2026, 10, 26), actor="seed")
        for name, email, city in CUSTOMERS:
            create_party(ctx, "customer", name, email, city)
        for name, rate in ITEMS:
            create_item(ctx, name, Decimal(rate))
        cust = {r["name"]: dict(r) for r in conn.execute(select(m.party)).mappings()}
        item = {r["name"]: dict(r) for r in conn.execute(select(m.item)).mappings()}
        cw, cb, mh = item["Copper Wire"], item["Circuit Board"], item["Motor Housing Unit"]

        def inv(c, d, lines, submit=True):
            return s.create_invoice(ctx, cust[c], [(item_, Decimal(q)) for item_, q in lines], d, submit=submit)

        def pay(c, amount, d, method):  # the oldest open invoices first, as the demo history was written
            s.record_payment(ctx, s.plan_payment(ctx, c, amount, d, oldest_first=True, method=method))

        # FY 2025
        inv("Acme Corporation", date(2025, 2, 10), [(cw, "100")])                 # 2,800.00
        inv("Wayne Enterprises", date(2025, 5, 20), [(mh, "10"), (cb, "20")])     # 2,100.00
        inv("Hooli", date(2025, 8, 5), [(cb, "40")])                              # 1,800.00
        inv("Initech", date(2025, 11, 12), [(cw, "50")])                          # 1,400.00
        pay(cust["Acme Corporation"], Decimal("2800.00"), date(2025, 3, 5), "bank transfer")
        pay(cust["Wayne Enterprises"], Decimal("2100.00"), date(2025, 6, 18), "check")
        pay(cust["Hooli"], Decimal("1800.00"), date(2025, 9, 1), "bank transfer")
        pay(cust["Initech"], Decimal("1400.00"), date(2025, 12, 10), "bank transfer")
        # FY 2026
        inv("Acme Corporation", date(2026, 3, 3), [(cw, "60"), (cb, "10")])       # 2,130.00
        inv("Wayne Enterprises", date(2026, 4, 14), [(mh, "15")])                 # 1,800.00
        inv("Pied Piper", date(2026, 6, 9), [(cb, "30"), (cw, "25")])            # 2,050.00
        inv("Wayne Enterprises", date(2026, 7, 21), [(cb, "12")])                 # 540.00
        inv("Initech", date(2026, 8, 30), [(mh, "3")])                            # 360.00
        inv("Pied Piper", date(2026, 9, 15), [(cw, "40")])                        # 1,120.00
        bad = inv("Hooli", date(2026, 9, 22), [(cw, "5")])                        # 140.00, cancelled below
        inv("Initech", date(2026, 10, 2), [(cw, "2")], submit=False)              # draft 56.00
        pay(cust["Acme Corporation"], Decimal("2130.00"), date(2026, 4, 2), "bank transfer")
        pay(cust["Wayne Enterprises"], Decimal("1000.00"), date(2026, 5, 30), "check")
        bad_row = dict(conn.execute(select(s.t.invoice).where(s.t.invoice.c.id == bad["id"])).mappings().one())
        s.cancel_invoice(ctx, s.plan_cancel(ctx, bad_row, bad_row["posting_date"]))
