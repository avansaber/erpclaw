"""Fixture for the static ownership check: not a real plugin, never imported."""
from sqlalchemy import insert, select, text

from erpclaw_core import models as m
from erpclaw_core.masters import create_party


def allowed(ctx):
    rows = ctx.conn.execute(select(m.party)).all()  # a read across ownership: allowed
    create_party(ctx, "customer", "Acme")  # a core service writing core's table: allowed
    return rows


def planted(ctx):
    ctx.conn.execute(m.party.insert().values(name="x"))  # a write to core's party table
    ctx.conn.execute(insert(m.ledger_entry).values())  # the ledger, outside the posting service
    ctx.conn.execute(text("DELETE FROM posting"))  # raw SQL
