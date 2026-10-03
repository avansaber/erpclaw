"""Inventory postings stamp the posting company's own fiscal year.

A decoy company B owns FY-DECOY (2025-07-01..2026-12-31), inserted first;
company A (built in-test) owns FY-A-2026 (2026-01-01..2026-12-31). A posting
for A dated 2026-03-15 must carry FY-A-2026 on every gl_entry row and every
stock_ledger_entry row of the voucher. The company-blind lookup returns B's
year first on SQLite, so each test fails on the base.
"""
import json
from decimal import Decimal

from inventory_helpers import (
    build_inventory_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_fiscal_year,
)

mod = load_db_query()

POSTING_DATE = "2026-03-15"
OWN_YEAR = "FY-A-2026"


def _setup_two_companies(conn):
    """Seed decoy B first, then build A in-test and rename A's year."""
    b = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(conn, b, name="FY-DECOY",
                     start="2025-07-01", end="2026-12-31")
    env = build_inventory_env(conn)
    conn.execute(
        "UPDATE fiscal_year SET name = 'FY-A-2026' WHERE company_id = ?",
        (env["company_id"],),
    )
    conn.commit()
    return b, env


def _gl_rows(conn, voucher_id):
    return conn.execute(
        "SELECT fiscal_year, debit, credit FROM gl_entry WHERE voucher_id = ?",
        (voucher_id,),
    ).fetchall()


def _sle_rows(conn, voucher_id):
    return conn.execute(
        "SELECT fiscal_year, actual_qty FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (voucher_id,),
    ).fetchall()


def _assert_own_year(rows):
    assert len(rows) >= 1
    for row in rows:
        assert row["fiscal_year"] == OWN_YEAR


def test_stock_entry_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    items = json.dumps([{
        "item_id": env["item1"], "qty": "10", "rate": "50.00",
        "to_warehouse_id": env["warehouse"],
    }])
    create = call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=POSTING_DATE, items=items,
    ))
    assert is_ok(create), create
    result = call_action(mod.submit_stock_entry, conn, ns(
        stock_entry_id=create["stock_entry_id"],
    ))
    assert is_ok(result), result

    gl_rows = _gl_rows(conn, create["stock_entry_id"])
    _assert_own_year(gl_rows)
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == Decimal("500.00")
    assert total_credit == Decimal("500.00")

    sle_rows = _sle_rows(conn, create["stock_entry_id"])
    _assert_own_year(sle_rows)


def test_stock_reconciliation_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    items = json.dumps([{
        "item_id": env["item1"], "warehouse_id": env["warehouse"],
        "qty": "95", "valuation_rate": "50.00",
    }])
    create = call_action(mod.add_stock_reconciliation, conn, ns(
        posting_date=POSTING_DATE, items=items,
        company_id=env["company_id"],
    ))
    assert is_ok(create), create
    assert Decimal(create["difference_amount"]) == Decimal("-250.00")
    result = call_action(mod.submit_stock_reconciliation, conn, ns(
        stock_reconciliation_id=create["stock_reconciliation_id"],
    ))
    assert is_ok(result), result

    gl_rows = _gl_rows(conn, create["stock_reconciliation_id"])
    _assert_own_year(gl_rows)
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == Decimal("250.00")
    assert total_credit == Decimal("250.00")

    sle_rows = _sle_rows(conn, create["stock_reconciliation_id"])
    _assert_own_year(sle_rows)


def test_revalue_stock_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    result = call_action(mod.revalue_stock, conn, ns(
        item_id=env["item1"], warehouse_id=env["warehouse"],
        new_rate="60.00", posting_date=POSTING_DATE,
        company_id=env["company_id"], reason="Market adjustment",
    ))
    assert is_ok(result), result

    gl_rows = _gl_rows(conn, result["revaluation_id"])
    _assert_own_year(gl_rows)
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == Decimal("1000.00")
    assert total_credit == Decimal("1000.00")

    sle_rows = _sle_rows(conn, result["revaluation_id"])
    assert len(sle_rows) == 1
    assert Decimal(sle_rows[0]["actual_qty"]) == Decimal("0")
    _assert_own_year(sle_rows)


def test_missing_company_year_is_refused(conn):
    _, env = _setup_two_companies(conn)
    conn.execute(
        "DELETE FROM fiscal_year WHERE company_id = ?",
        (env["company_id"],),
    )
    conn.commit()
    items = json.dumps([{
        "item_id": env["item1"], "qty": "10", "rate": "50.00",
        "to_warehouse_id": env["warehouse"],
    }])
    create = call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=POSTING_DATE, items=items,
    ))
    assert is_ok(create), create
    result = call_action(mod.submit_stock_entry, conn, ns(
        stock_entry_id=create["stock_entry_id"],
    ))
    assert is_error(result), result
    message = str(result.get("message", "")) + str(result.get("error", ""))
    assert ("GL Validation Step 9 Failed: No open fiscal year found "
            "for posting date 2026-03-15") in message
    assert _gl_rows(conn, create["stock_entry_id"]) == []
