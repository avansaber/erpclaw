from sqlalchemy import select

from erpclaw_assistant import resolve
from erpclaw_assistant.model import call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.db import read_only

from test_turn_steps import run_read, steps, turn


def test_seeded_receivables_step_counts_ledger_entries(engine):
    _, _, result = run_read(engine, "get_metric", metric="receivables")
    with read_only(engine) as conn:
        ctx = company_ctx(conn)
        entries = conn.execute(
            select(m.ledger_entry.c.id)
            .join(m.account, m.account.c.id == m.ledger_entry.c.account_id)
            .join(m.posting, m.posting.c.id == m.ledger_entry.c.posting_id)
            .where(m.ledger_entry.c.company_id == ctx.company_id,
                   m.account.c.account_type == "receivable", m.posting.c.posting_date <= ctx.today)
        ).all()
    assert result.data["row_count"] == len(entries) > 1
    events = turn(engine, [call("get_metric", metric="receivables")], text="What do customers owe us?")
    recorded = steps(events)
    assert len(recorded) == 1
    assert f"across {result.data['row_count']} ledger entries" in recorded[0]["returned"]
    assert "invoices" not in recorded[0]["asked"] + recorded[0]["returned"]


def test_receivables_step_with_one_ledger_entry(engine):
    args = dict(metric="receivables", customer="Initech", period="November 2025")
    _, _, result = run_read(engine, "get_metric", **args)
    assert result.data["row_count"] == 1
    events = turn(engine, [call("get_metric", **args)], text="What did Initech owe us in November 2025?")
    assert "across 1 ledger entry;" in steps(events)[0]["returned"]


def test_sales_step_keeps_invoice_wording(engine):
    events = turn(engine, [call("get_metric", metric="sales", period="this year", customer="Wayne Enterprises")])
    assert steps(events)[0]["returned"] == "Sales 2,340.00 USD, across 2 invoices."


def test_unknown_metric_step_falls_back_to_invoice_wording():
    rendered = resolve.step_text("get_metric", metric="unknown", label="Sales", value="5.00 USD",
                                 row_count=2, rows=[])
    assert rendered == ("Read Sales.", "Sales 5.00 USD, across 2 invoices.")


def test_receivables_headline_still_sanitises_labels_and_names():
    rendered = resolve.step_text("get_metric", metric="receivables", label="\x1b[31mOwed to us\x1b[0m\x00",
                                 value="5.00 USD", row_count=2, rows=[("<b>Acme</b>", "5.00 USD")])
    assert rendered == ("Read Owed to us.", "Owed to us 5.00 USD, across 2 ledger entries.")
