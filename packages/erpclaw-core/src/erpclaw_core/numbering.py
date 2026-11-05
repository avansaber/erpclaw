"""Document numbers by fiscal year of the POSTING date, never of the clock."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import models as m
from .common import Ctx
from .ledger import _for_update, open_period


def next_number(ctx: Ctx, series: str, posting_date: date) -> str:
    """The next number of the series, gapless: the series row is created if absent (never twice, even at
    once) and locked for the transaction, so a concurrent caller waits and takes the next value; a
    rolled-back transaction gives its value back. Taken before the chain head by every caller."""
    period = open_period(ctx, posting_date)
    fy = ctx.conn.execute(select(m.fiscal_year.c.label).where(
        m.fiscal_year.c.id == period["fiscal_year_id"])).scalar_one()
    ns = m.number_series
    key = (ns.c.company_id == ctx.company_id) & (ns.c.series == series) & (ns.c.fiscal_year == fy)
    first = dict(company_id=ctx.company_id, series=series, fiscal_year=fy, next_value=1)
    ctx.conn.execute(pg_insert(ns).values(**first).on_conflict_do_nothing())
    value = ctx.conn.execute(_for_update(ctx, select(ns.c.next_value).where(key))).scalar_one()
    ctx.conn.execute(ns.update().where(key).values(next_value=value + 1))
    return f"{series}-{fy}-{value:05d}"
