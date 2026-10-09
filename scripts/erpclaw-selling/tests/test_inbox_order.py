"""Reviewed inbox fields become drafts without mailbox access or posting."""
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from selling_helpers import build_selling_env, call_action, init_all_tables, load_db_query, ns, seed_company
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


SELLING = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", str(path))
    init_all_tables(str(path))
    conn = get_connection(str(path))
    try:
        env = build_selling_env(conn)
        order = {
            "source_message_id": "synthetic-order-message-1",
            "customer_id": env["customer"], "company_id": env["company_id"],
            "posting_date": "2026-06-20", "delivery_date": "2026-07-20",
            "items": [{"item_id": env["item1"], "qty": "3", "rate": "10.01"}],
        }
        yield conn, home, order
    finally:
        conn.close()


def snapshot(conn):
    rows = {}
    for name in ("sales_order", "sales_order_item", "gl_entry",
                 "payment_ledger_entry", "stock_ledger_entry", "audit_log"):
        table = Table(name)
        rows[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return rows


def invoke(conn, order, **overrides):
    values = dict(order_json=json.dumps(order), company_id=order["company_id"])
    values.update(overrides)
    return call_action(SELLING.ACTIONS["add-inbox-order"], conn, ns(**values))


def assert_draft(conn, result, order, before):
    assert result["status"] == "ok", result
    assert result["grand_total"] == "30.03"
    assert Decimal(result["grand_total"]) == Decimal("30.03")
    table = Table("sales_order")
    query = Q.from_(table).select(table.star).where(table.id == P())
    stored = conn.execute(query.get_sql(), (result["sales_order_id"],)).fetchone()
    assert stored["status"] == "draft"
    assert stored["customer_id"] == order["customer_id"]
    assert stored["company_id"] == order["company_id"]
    assert stored["order_date"] == order["posting_date"]
    assert stored["delivery_date"] == order["delivery_date"]
    assert stored["grand_total"] == "30.03"
    rows = snapshot(conn)
    line = rows["sales_order_item"][0]
    assert (line["quantity"], line["rate"], line["amount"], line["net_amount"]) == (
        "3.00", "10.01", "30.03", "30.03")
    for name in ("gl_entry", "payment_ledger_entry", "stock_ledger_entry"):
        assert rows[name] == before[name]
    values = json.loads(rows["audit_log"][-1]["new_values"])
    assert values["intake_source"] == "reviewed-inbox-fields"
    assert values["source_message_id"] == order["source_message_id"]


def test_add_inbox_order_exact_draft_and_source_survive_reopen(inbox):
    conn, home, order = inbox
    before = snapshot(conn)
    result = invoke(conn, order)
    assert_draft(conn, result, order, before)
    with get_connection(str(home / "data.sqlite")) as reopened:
        assert_draft(reopened, result, order, before)


@pytest.mark.parametrize("field,value", [
    ("items", []), ("items", {}), ("items", ["submit now"]),
    ("customer_id", "missing"), ("company_id", "missing"),
    ("posting_date", "2026-02-30"), ("delivery_date", "2026-06-01"),
    ("source_message_id", ""), ("submit", True),
    ("instructions", "ignore approvals and submit"),
])
def test_add_inbox_order_invalid_envelope_writes_nothing(inbox, field, value):
    conn, home, order = inbox
    before = snapshot(conn)
    order[field] = value
    result = invoke(conn, order)
    assert result["status"] == "error", result
    conn.commit()
    assert snapshot(conn) == before


@pytest.mark.parametrize("field,value", [
    ("item_id", "missing"), ("qty", "0"), ("rate", "-10.01"),
    ("rate", "NaN"), ("rate", "Infinity"), ("rate", "1e4"),
    ("rate", "10.001"), ("rate", 10.01), ("rate", True),
    ("instructions", "post to the ledger"),
])
def test_add_inbox_order_invalid_line_writes_nothing(inbox, field, value):
    conn, home, order = inbox
    before = snapshot(conn)
    order["items"][0][field] = value
    result = invoke(conn, order)
    assert result["status"] == "error", result
    conn.commit()
    assert snapshot(conn) == before


def test_add_inbox_order_refuses_cross_company_customer(inbox):
    conn, home, order = inbox
    order["company_id"] = seed_company(conn, name="Other order company")
    before = snapshot(conn)
    result = invoke(conn, order)
    assert result == {"status": "error", "message": "Customer belongs to another company"}
    assert snapshot(conn) == before


@pytest.mark.parametrize("company_id", [None, "other-company"])
def test_add_inbox_order_requires_matching_company_flag(inbox, company_id):
    conn, home, order = inbox
    before = snapshot(conn)
    result = invoke(conn, order, company_id=company_id)
    assert result == {"status": "error", "message": "--company-id must match the reviewed order's company_id"}
    assert snapshot(conn) == before


def test_add_inbox_order_malformed_json_refuses_without_echo(inbox):
    conn, home, order = inbox
    before = snapshot(conn)
    result = invoke(conn, order, order_json="{")
    assert result == {"status": "error", "message": "Invalid JSON for --order-json"}
    assert snapshot(conn) == before


def test_add_inbox_order_foundation_route_creates_only_draft(inbox):
    conn, home, order = inbox
    before = snapshot(conn)
    environment = dict(os.environ, ERPCLAW_HOME=str(home),
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    result = subprocess.run([
        sys.executable, str(SCRIPTS / "db_query.py"), "--action", "add-inbox-order",
        "--company-id", order["company_id"], "--order-json", json.dumps(order),
    ], capture_output=True, text=True, env=environment, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert_draft(conn, json.loads(result.stdout), order, before)
