"""Core writes approved over real books with an existing ledger."""
from sqlalchemy import select

from erpclaw_core import models as m
from erpclaw_core.administration import ensure_owner
from erpclaw_core.common import new_id, now
from erpclaw_core.setup import _chart


def add_customer(request, engine):
    return ({"name": "Juniper Instruments", "email": "orders@juniper.example", "city": "Madison"},
            "Add Juniper Instruments, orders@juniper.example, in Madison as a customer.")


def set_up_company(request, engine):
    """A second company in the bootstrap's pending state; the demo company retains its real postings."""
    with engine.begin() as conn:
        assert conn.execute(select(m.posting.c.id)).first()
        assert conn.execute(select(m.ledger_entry.c.id)).first()
        company = new_id()
        conn.execute(m.company.insert().values(id=company, name=None, functional_currency=None,
            fiscal_year_start_month=None, date_order=None, status="pending", created_at=now()))
        _chart(conn, company)
        ensure_owner(conn, company)
    return ({"name": "Juniper Trading", "currency": "US dollars", "fiscal_year_start_month": 1, "date_order": "MDY"},
            "Set up Juniper Trading in US dollars, with a January financial year and month-first dates.")


NO_MONEY_SCENARIOS = {"add_customer": add_customer, "set_up_company": set_up_company}
