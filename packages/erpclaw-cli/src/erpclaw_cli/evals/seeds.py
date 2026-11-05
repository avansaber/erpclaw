"""Starting books for the eval sets, built through the engine's own services as the server's role."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import Ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.setup import create_company

COMPANY = "Harbor Lane Consulting LLC"
CUSTOMERS = [("Acme Corporation", "billing@acme.example"), ("Wayne Enterprises", "ap@wayne.example"),
             ("Pied Piper", "finance@piedpiper.example"), ("Hooli", "payables@hooli.example"),
             ("Initech", "accounts@initech.example")]
SERVICES = [("Consulting", "hour", "150.00"), ("Website audit", "each", "900.00"),
            ("Support retainer", "month", "400.00"), ("Training day", "day", "1200.00")]


def services_co(engine: Engine) -> None:
    """A US services business (USD, fiscal year from January, no sales tax, net 30): five customers, four services,
    two open invoices (INV-2026-00007, 2400.00 unpaid; INV-2026-00008, 900.00 with 400.00 paid), and January 2026
    locked, so that nothing posts into it. The engine closes a period only with its fiscal year, so a locked period
    is how a closed month looks while the year is open: a posting into it is refused with `period_closed`."""
    from erpclaw_sales import service as s
    from erpclaw_sales import tables as t

    cid = create_company(engine, COMPANY, currency="USD", fy_start_month=1)
    with engine.begin() as conn:
        ctx = Ctx(conn, cid, COMPANY, "USD", date(2026, 11, 3), actor="seed")
        conn.execute(update(m.account).where(m.account.c.company_id == cid, m.account.c.number == "4100")
                     .values(name="Service Revenue"))
        for name, email in CUSTOMERS:
            create_party(ctx, "customer", name, email)
        for name, unit, rate in SERVICES:
            create_item(ctx, name, Decimal(rate), unit)
        cust = {r["name"]: dict(r) for r in conn.execute(select(m.party)).mappings()}
        item = {r["name"]: dict(r) for r in conn.execute(select(m.item)).mappings()}
        # the open invoices already in the books are numbered 7 and 8, so the next one is INV-2026-00009
        conn.execute(m.number_series.insert().values(company_id=cid, series="INV", fiscal_year=2026, next_value=7))
        s.create_invoice(ctx, cust["Wayne Enterprises"], [(item["Training day"], Decimal(2))], date(2026, 10, 15))
        pied = s.create_invoice(ctx, cust["Pied Piper"], [(item["Website audit"], Decimal(1))], date(2026, 10, 20))
        ledger.lock_chain(ctx)
        inv = dict(conn.execute(select(t.invoice).where(t.invoice.c.id == pied["id"])).mappings().one())
        s.record_payment(ctx, s.plan_payment(ctx, cust["Pied Piper"], Decimal("400.00"), date(2026, 10, 25),
                                             invoice=inv, method="bank transfer"))
        ledger.set_period_status(ctx, ledger.find_period(ctx, date(2026, 1, 15))["id"], "locked")
