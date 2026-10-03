"""Buying stamps the posting company's own fiscal year on its ledger rows.

Every test seeds a decoy company FIRST (its year ``FY-DECOY`` covers the
posting date and sorts first under the old company-blind lookup), then
builds company A with ``build_buying_env`` and renames A's own year to
``FY-A-2026``. Each voucher asserts that every ledger row it writes carries
``FY-A-2026`` plus the full sorted list of stored ``(debit, credit)`` pairs,
so on the old company-blind stamp the rows read ``FY-DECOY`` and fail.

The ``conn`` fixture serves both backends: SQLite files by default, or the
shared PostgreSQL target when ``ERPCLAW_DB_DIALECT=postgresql`` with
``ERPCLAW_PG_TEST_URL`` set (first-row order is not guaranteed there, so
that lane proves the pass after the change only).
"""
import json
import os
import sys
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

import buying_helpers as helpers
from buying_helpers import (
    build_buying_env, seed_company, seed_fiscal_year,
    call_action, ns, is_ok, is_error, load_db_query,
)

mod = load_db_query()

POSTING_DATE = "2026-03-15"
OWN_YEAR = "FY-A-2026"
DECOY_YEAR = "FY-DECOY"


def _two_company_env(conn):
    """Seed decoy B first, then build company A and rename A's own year."""
    seed_fy_decoy(conn)
    env = build_buying_env(conn)
    conn.execute(
        "UPDATE fiscal_year SET name = ? WHERE company_id = ?",
        (OWN_YEAR, env["company_id"]),
    )
    conn.commit()
    return env


def seed_fy_decoy(conn):
    b = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(conn, b, name=DECOY_YEAR,
                     start="2025-07-01", end="2026-12-31")
    return b


def _stock_items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _confirmed_po(conn, env, items_str):
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=POSTING_DATE, items=items_str,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit), f"PO submit failed: {submit}"
    return po["purchase_order_id"]


def _submitted_receipt(conn, env, items_str):
    po_id = _confirmed_po(conn, env, items_str)
    pr = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date=POSTING_DATE, items=None,
        purchase_receipt_id=None,
    ))
    assert is_ok(pr), f"receipt creation failed: {pr}"
    submit = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr["purchase_receipt_id"],
    ))
    assert is_ok(submit), f"receipt submit failed: {submit}"
    return pr["purchase_receipt_id"]


def _gl_pairs(conn, voucher_id):
    rows = conn.execute(
        "SELECT debit, credit, fiscal_year FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (voucher_id,),
    ).fetchall()
    return rows


def test_purchase_receipt_stamps_own_company_year(conn):
    env = _two_company_env(conn)
    pr_id = _submitted_receipt(
        conn, env, _stock_items(env, ("item1", "10", "50.00")))

    rows = _gl_pairs(conn, pr_id)
    assert len(rows) == 2
    assert sorted((r["debit"], r["credit"]) for r in rows) == [
        ("0.00", "500.00"), ("500.00", "0.00"),
    ]
    assert all(r["fiscal_year"] == OWN_YEAR for r in rows)

    sle = conn.execute(
        "SELECT actual_qty, stock_value_difference, valuation_rate, "
        "fiscal_year FROM stock_ledger_entry "
        "WHERE voucher_type = 'purchase_receipt' AND voucher_id = ? "
        "AND is_cancelled = 0",
        (pr_id,),
    ).fetchall()
    assert len(sle) == 1
    assert sle[0]["actual_qty"] == "10.00"
    assert sle[0]["stock_value_difference"] == "500.00"
    assert sle[0]["valuation_rate"] == "50.00"
    assert sle[0]["fiscal_year"] == OWN_YEAR


def test_purchase_invoice_stamps_own_company_year(conn):
    env = _two_company_env(conn)
    items = json.dumps([{
        "item_id": env["item1"], "qty": "2", "rate": "100.00",
        "warehouse_id": env["warehouse"],
    }])
    pi = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=POSTING_DATE, due_date=None,
        items=items, tax_template_id=None,
    ))
    assert is_ok(pi), f"invoice creation failed: {pi}"
    submit = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi["purchase_invoice_id"],
    ))
    assert is_ok(submit), f"invoice submit failed: {submit}"

    rows = _gl_pairs(conn, pi["purchase_invoice_id"])
    assert len(rows) == 4
    assert sorted((r["debit"], r["credit"]) for r in rows) == [
        ("0.00", "200.00"), ("0.00", "200.00"),
        ("200.00", "0.00"), ("200.00", "0.00"),
    ]
    assert all(r["fiscal_year"] == OWN_YEAR for r in rows)

    sle = conn.execute(
        "SELECT actual_qty, stock_value_difference, valuation_rate, "
        "fiscal_year FROM stock_ledger_entry "
        "WHERE voucher_type = 'purchase_invoice' AND voucher_id = ? "
        "AND is_cancelled = 0",
        (pi["purchase_invoice_id"],),
    ).fetchall()
    assert len(sle) == 1
    assert sle[0]["actual_qty"] == "2.00"
    assert sle[0]["stock_value_difference"] == "200.00"
    assert sle[0]["valuation_rate"] == "100.00"
    assert sle[0]["fiscal_year"] == OWN_YEAR


def _landed_cost_voucher(conn, env, pr_id):
    return call_action(mod.add_landed_cost_voucher, conn, ns(
        purchase_receipt_ids=json.dumps([pr_id]),
        charges=json.dumps([{
            "description": "Ocean freight", "amount": "100.00",
            "expense_account_id": env["expense"],
        }]),
        company_id=env["company_id"],
    ))


def test_landed_cost_voucher_stamps_own_company_year(conn, monkeypatch):
    env = _two_company_env(conn)
    monkeypatch.setattr(mod, "_today", lambda: POSTING_DATE)
    pr_id = _submitted_receipt(
        conn, env, _stock_items(env, ("item1", "10", "50.00")))
    result = _landed_cost_voucher(conn, env, pr_id)
    assert is_ok(result), f"landed cost voucher failed: {result}"
    lcv_id = result["landed_cost_voucher_id"]
    assert result["total_landed_cost"] == "100.00"

    rows = _gl_pairs(conn, lcv_id)
    assert len(rows) == 2
    assert sorted((r["debit"], r["credit"]) for r in rows) == [
        ("0.00", "100.00"), ("100.00", "0.00"),
    ]
    assert all(r["fiscal_year"] == OWN_YEAR for r in rows)

    sle = conn.execute(
        "SELECT stock_value_difference, valuation_rate, fiscal_year "
        "FROM stock_ledger_entry "
        "WHERE voucher_type = 'landed_cost_voucher' AND voucher_id = ? "
        "AND is_cancelled = 0",
        (lcv_id,),
    ).fetchall()
    assert len(sle) == 1
    assert sle[0]["stock_value_difference"] == "100.00"
    assert sle[0]["valuation_rate"] == "60.00"
    assert sle[0]["fiscal_year"] == OWN_YEAR


def test_landed_cost_voucher_cancel_stamps_own_company_year(conn, monkeypatch):
    env = _two_company_env(conn)
    monkeypatch.setattr(mod, "_today", lambda: POSTING_DATE)
    pr_id = _submitted_receipt(
        conn, env, _stock_items(env, ("item1", "10", "50.00")))
    created = _landed_cost_voucher(conn, env, pr_id)
    assert is_ok(created), f"landed cost voucher failed: {created}"
    lcv_id = created["landed_cost_voucher_id"]
    cancelled = call_action(mod.cancel_landed_cost_voucher, conn, ns(
        landed_cost_voucher_id=lcv_id,
    ))
    assert is_ok(cancelled), f"cancel failed: {cancelled}"

    sle = conn.execute(
        "SELECT stock_value_difference, fiscal_year "
        "FROM stock_ledger_entry "
        "WHERE voucher_type = 'landed_cost_voucher' AND voucher_id = ?",
        (lcv_id,),
    ).fetchall()
    assert len(sle) == 2
    assert sorted(r["stock_value_difference"] for r in sle) == [
        "-100.00", "100.00",
    ]
    assert all(r["fiscal_year"] == OWN_YEAR for r in sle)


def test_invoice_without_company_year_is_refused(conn):
    env = _two_company_env(conn)
    conn.execute(
        "DELETE FROM fiscal_year WHERE company_id = ?",
        (env["company_id"],),
    )
    conn.commit()

    items = json.dumps([{
        "item_id": env["item1"], "qty": "1", "rate": "10.00",
        "warehouse_id": env["warehouse"],
    }])
    pi = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=POSTING_DATE, due_date=None,
        items=items, tax_template_id=None,
    ))
    assert is_ok(pi), f"invoice creation failed: {pi}"
    submit = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi["purchase_invoice_id"],
    ))
    assert is_error(submit)
    assert ("GL Validation Step 9 Failed: No open fiscal year found "
            "for posting date 2026-03-15") in submit["message"]
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id = ?",
        (pi["purchase_invoice_id"],),
    ).fetchone()["n"]
    assert str(Decimal(str(count))) == "0"


def test_recurring_bill_stamps_own_company_year(conn):
    env = _two_company_env(conn)
    items = json.dumps([{
        "item_id": env["item1"], "qty": "1", "rate": "500.00",
    }])
    tmpl = call_action(mod.add_recurring_bill_template, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=items, frequency="monthly",
        start_date="2026-03-01", end_date="2026-12-31",
        tax_template_id=None, auto_submit=True,
        posting_date=None, name=None,
        blanket_order_id=None, blanket_status=None,
        sales_order_id=None, template_id=None, as_of_date=None,
        template_status=None, limit="20", offset="0",
    ))
    assert is_ok(tmpl), f"template creation failed: {tmpl}"
    conn.execute(
        "UPDATE recurring_bill_template SET status = 'active' WHERE id = ?",
        (tmpl["template_id"],),
    )
    conn.commit()

    result = call_action(mod.generate_recurring_bills, conn, ns(
        supplier_id=None, company_id=env["company_id"],
        items=None, frequency=None,
        start_date=None, end_date=None,
        tax_template_id=None, auto_submit=False,
        posting_date=None, name=None,
        blanket_order_id=None, blanket_status=None,
        sales_order_id=None, template_id=None, as_of_date="2026-03-15",
        template_status=None, limit="20", offset="0",
    ))
    assert is_ok(result), f"bill generation failed: {result}"
    assert result["bills_generated"] == 1
    bill = result["bills"][0]
    assert bill["status"] == "submitted"
    assert str(Decimal(bill["amount"])) == "500.00"

    rows = _gl_pairs(conn, bill["invoice_id"])
    assert len(rows) == 2
    assert sorted((r["debit"], r["credit"]) for r in rows) == [
        ("0.00", "500.00"), ("500.00", "0.00"),
    ]
    assert all(r["fiscal_year"] == OWN_YEAR for r in rows)


class _FakeCursor:
    def fetchone(self):
        return ("production_db",)


class _FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return _FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


def test_pg_reset_guard_refuses_other_databases(monkeypatch):
    opened = []

    def fake_get_connection(db_path=None):
        conn = _FakeConnection()
        opened.append(conn)
        return conn

    monkeypatch.setattr(helpers, "get_connection", fake_get_connection)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL",
                       "postgresql://localhost:5432/production_db")
    with pytest.raises(RuntimeError):
        helpers._reset_pg_schema()
    assert [s for conn in opened for s in conn.statements] == [], \
        "a refused reset must issue no SQL"

    assert helpers.assert_pg_test_database(
        "postgresql://localhost:5432/erpclaw_muse_t_case42") == \
        "erpclaw_muse_t_case42"
    assert helpers.assert_pg_test_database(
        "postgresql://localhost:5432/erpclaw_integration") == \
        "erpclaw_integration"
    assert helpers.assert_pg_test_database(
        "postgresql://localhost:5432/erpclaw_muse_test_b") == \
        "erpclaw_muse_test_b"
