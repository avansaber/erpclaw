"""The retired purchase balance action refuses without changing posted books."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest

from buying_helpers import build_buying_env, call_action, init_all_tables, load_db_query, ns
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


BUYING = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]
MESSAGE = (
    "'update-purchase-outstanding' has been retired: it moved a document's "
    "balance with no ledger posting."
)
FLOWS = (
    "add-payment", "submit-payment", "allocate-payment", "create-credit-note",
    "create-debit-note", "write-off-invoice",
)


@pytest.fixture
def posted_bill(tmp_path, monkeypatch):
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
        created = call_action(BUYING.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            tax_template_id=None, items=json.dumps([{
                "item_id": env["item1"], "qty": "1", "rate": "500.03",
                "warehouse_id": env["warehouse"],
            }]),
        ))
        assert created["status"] == "ok", created
        invoice_id = created["purchase_invoice_id"]
        submitted = call_action(BUYING.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=invoice_id,
        ))
        assert submitted["status"] == "ok", submitted
        invoice = Table("purchase_invoice")
        query = (Q.from_(invoice).select(invoice.outstanding_amount)
                 .where(invoice.id == P()))
        stored = conn.execute(query.get_sql(), (invoice_id,)).fetchone()
        assert stored["outstanding_amount"] == "500.03"
        assert Decimal(stored["outstanding_amount"]) == Decimal("500.03")
        yield conn, home, invoice_id
    finally:
        conn.close()


def books_snapshot(conn):
    result = {}
    for name in ("purchase_invoice", "purchase_invoice_item",
                 "payment_ledger_entry", "gl_entry", "audit_log"):
        table = Table(name)
        query = Q.from_(table).select(table.star).orderby(table.id)
        result[name] = [dict(row) for row in conn.execute(query.get_sql()).fetchall()]
    assert result["payment_ledger_entry"]
    assert result["gl_entry"]
    return result


def assert_refusal(payload):
    assert payload["status"] == "error", payload
    assert payload["message"] == MESSAGE
    assert isinstance(payload["suggestion"], str)
    for action in FLOWS:
        assert action in payload["suggestion"]


def test_update_invoice_outstanding_refuses_and_preserves_posted_rows(posted_bill):
    conn, home, invoice_id = posted_bill
    before = books_snapshot(conn)
    output = io.StringIO()
    with patch("sys.stdout", output), pytest.raises(SystemExit) as stopped:
        BUYING.ACTIONS["update-invoice-outstanding"](conn, ns(
            purchase_invoice_id=invoice_id, amount="200.01",
        ))
    assert stopped.value.code == 1
    assert_refusal(json.loads(output.getvalue()))
    conn.commit()
    assert books_snapshot(conn) == before
    with get_connection(str(home / "data.sqlite")) as fresh:
        assert books_snapshot(fresh) == before


def test_update_invoice_outstanding_public_buying_route_keeps_books(posted_bill):
    conn, home, invoice_id = posted_bill
    before = books_snapshot(conn)
    environment = dict(os.environ, ERPCLAW_HOME=str(home),
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    result = subprocess.run([
        sys.executable, str(SCRIPTS / "db_query.py"),
        "--action", "update-purchase-outstanding",
        "--purchase-invoice-id", invoice_id, "--amount", "200.01",
    ], capture_output=True, text=True, env=environment, timeout=120)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    assert_refusal(json.loads(result.stdout))
    with get_connection(str(home / "data.sqlite")) as fresh:
        assert books_snapshot(fresh) == before
