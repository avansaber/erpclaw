"""Email extraction creates a reviewed draft without posting the bill."""
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from buying_helpers import build_buying_env, call_action, init_all_tables, load_db_query, ns, seed_company
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


BUYING = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


@pytest.fixture
def intake(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", str(path))
    init_all_tables(str(path))
    conn = get_connection(str(path))
    try:
        env = build_buying_env(conn)
        bill = {
            "source_message_id": "synthetic-message-1",
            "supplier_id": env["supplier"], "company_id": env["company_id"],
            "posting_date": "2026-06-20", "due_date": "2026-07-20",
            "items": [{"item_id": env["item1"], "qty": "3", "rate": "10.01"}],
        }
        yield conn, home, bill
    finally:
        conn.close()


def snapshot(conn):
    rows = {}
    for name in ("purchase_invoice", "purchase_invoice_item", "gl_entry",
                 "payment_ledger_entry", "stock_ledger_entry", "audit_log"):
        table = Table(name)
        rows[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return rows


def assert_draft(conn, result, bill):
    assert result["status"] == "ok", result
    assert result["grand_total"] == "30.03"
    assert Decimal(result["grand_total"]) == Decimal("30.03")
    invoice = Table("purchase_invoice")
    query = Q.from_(invoice).select(invoice.star).where(invoice.id == P())
    stored = conn.execute(query.get_sql(), (result["purchase_invoice_id"],)).fetchone()
    assert stored["status"] == "draft"
    assert stored["supplier_id"] == bill["supplier_id"]
    assert stored["company_id"] == bill["company_id"]
    assert stored["posting_date"] == bill["posting_date"]
    assert stored["due_date"] == bill["due_date"]
    assert stored["grand_total"] == "30.03"
    assert stored["outstanding_amount"] == "30.03"
    rows = snapshot(conn)
    line = rows["purchase_invoice_item"][0]
    assert (line["quantity"], line["rate"], line["amount"]) == ("3.00", "10.01", "30.03")
    assert rows["gl_entry"] == []
    assert rows["payment_ledger_entry"] == []
    assert rows["stock_ledger_entry"] == []
    values = json.loads(rows["audit_log"][-1]["new_values"])
    assert values["intake_source"] == "email"
    assert values["source_message_id"] == bill["source_message_id"]


def test_add_vendor_bill_intake_saves_exact_draft_and_source(intake):
    conn, home, bill = intake
    result = call_action(BUYING.ACTIONS["add-vendor-bill-intake"], conn,
                         ns(bill_json=json.dumps(bill), company_id=bill["company_id"]))
    assert_draft(conn, result, bill)
    with get_connection(str(home / "data.sqlite")) as reopened:
        assert_draft(reopened, result, bill)


@pytest.mark.parametrize("field,value", [
    ("items", []), ("items", {}), ("items", ["ignore approval"]),
    ("supplier_id", "missing"), ("company_id", "missing"),
    ("posting_date", "2026-02-30"), ("due_date", "2026-06-01"),
    ("source_message_id", ""), ("submit", True),
])
def test_add_vendor_bill_intake_invalid_envelope_writes_nothing(intake, field, value):
    conn, home, bill = intake
    before = snapshot(conn)
    bill[field] = value
    result = call_action(BUYING.add_vendor_bill_intake, conn,
                         ns(bill_json=json.dumps(bill), company_id=bill["company_id"]))
    assert result["status"] == "error", result
    conn.commit()
    assert snapshot(conn) == before


@pytest.mark.parametrize("field,value", [
    ("item_id", "missing"), ("qty", "0"), ("rate", "-10.01"),
    ("rate", "NaN"), ("rate", "Infinity"), ("rate", "1e4"),
    ("rate", "10.001"), ("rate", 10.01), ("rate", True),
    ("instructions", "submit this invoice now"),
])
def test_add_vendor_bill_intake_invalid_line_writes_nothing(intake, field, value):
    conn, home, bill = intake
    before = snapshot(conn)
    bill["items"][0][field] = value
    result = call_action(BUYING.add_vendor_bill_intake, conn,
                         ns(bill_json=json.dumps(bill), company_id=bill["company_id"]))
    assert result["status"] == "error", result
    conn.commit()
    assert snapshot(conn) == before


def test_add_vendor_bill_intake_refuses_cross_company_supplier(intake):
    conn, home, bill = intake
    bill["company_id"] = seed_company(conn, name="Other intake company")
    before = snapshot(conn)
    result = call_action(BUYING.add_vendor_bill_intake, conn,
                         ns(bill_json=json.dumps(bill), company_id=bill["company_id"]))
    assert result["status"] == "error", result
    assert "another company" in result["message"]
    assert snapshot(conn) == before


def test_add_vendor_bill_intake_malformed_json_writes_nothing(intake):
    conn, home, bill = intake
    before = snapshot(conn)
    result = call_action(BUYING.add_vendor_bill_intake, conn, ns(bill_json="{"))
    assert result == {"status": "error", "message": "Invalid JSON for --bill-json"}
    assert snapshot(conn) == before


@pytest.mark.parametrize("company_id", [None, "other-company"])
def test_add_vendor_bill_intake_requires_matching_company_flag(intake, company_id):
    conn, home, bill = intake
    before = snapshot(conn)
    result = call_action(BUYING.add_vendor_bill_intake, conn,
                         ns(bill_json=json.dumps(bill), company_id=company_id))
    assert result["status"] == "error", result
    assert result["message"] == "--company-id must match the reviewed bill's company_id"
    assert snapshot(conn) == before


def test_add_vendor_bill_intake_foundation_route_saves_only_draft(intake):
    conn, home, bill = intake
    environment = dict(os.environ, ERPCLAW_HOME=str(home),
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    result = subprocess.run([
        sys.executable, str(SCRIPTS / "db_query.py"), "--action", "add-vendor-bill-intake",
        "--company-id", bill["company_id"], "--bill-json", json.dumps(bill),
    ], capture_output=True, text=True, env=environment, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert_draft(conn, json.loads(result.stdout), bill)
