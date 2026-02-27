"""Tests for stock revaluation (V3).

12 tests:
- Revalue up: rate increase creates positive adjustment (1)
- Revalue down: rate decrease creates negative adjustment (1)
- GL balanced on rate increase (1)
- GL balanced on rate decrease (1)
- Zero stock rejection (1)
- Cancel reverses SLE + GL (1)
- List revaluations (1)
- Get revaluation detail (1)
- Multi-warehouse isolation (1)
- Re-revalue same item (1)
- Same-rate rejection (1)
- GL invariant: total debits = total credits (1)
"""
import json

import db_query
from helpers import (
    _call_action,
    setup_inventory_environment,
    create_test_stock_entry,
    submit_test_stock_entry,
)
from decimal import Decimal
from erpclaw_lib.decimal_utils import to_decimal


def _receive_stock(conn, env, qty="100", rate="25.00", posting_date="2026-02-16"):
    """Helper: receive stock into the test warehouse."""
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": qty,
        "rate": rate,
    }])
    se_id = create_test_stock_entry(
        conn, env["company_id"], "receive", items_json,
        posting_date=posting_date,
    )
    submit_test_stock_entry(conn, se_id)
    return se_id


def _revalue(conn, env, new_rate, posting_date="2026-02-20", reason=None):
    """Helper: call revalue-stock."""
    return _call_action(
        db_query.ACTIONS["revalue-stock"], conn,
        item_id=env["item_id"],
        warehouse_id=env["warehouse_id"],
        new_rate=new_rate,
        posting_date=posting_date,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 1. Revalue up: rate increase creates positive adjustment
# ---------------------------------------------------------------------------

def test_revalue_up(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    r = _revalue(fresh_db, env, new_rate="30.00", reason="Purchase price correction")

    assert r["status"] == "ok"
    assert r["old_rate"] == "25.00"
    assert r["new_rate"] == "30.00"
    # 100 * 30 - 100 * 25 = 500
    assert to_decimal(r["adjustment_amount"]) == Decimal("500.00")
    assert r["gl_entries_created"] == 2


# ---------------------------------------------------------------------------
# 2. Revalue down: rate decrease creates negative adjustment
# ---------------------------------------------------------------------------

def test_revalue_down(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="50", rate="40.00")

    r = _revalue(fresh_db, env, new_rate="35.00")

    assert r["status"] == "ok"
    assert r["old_rate"] == "40.00"
    assert r["new_rate"] == "35.00"
    # 50 * 35 - 50 * 40 = -250
    assert to_decimal(r["adjustment_amount"]) == Decimal("-250.00")
    assert r["gl_entries_created"] == 2


# ---------------------------------------------------------------------------
# 3. GL balanced on rate increase
# ---------------------------------------------------------------------------

def test_gl_balanced_on_increase(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    r = _revalue(fresh_db, env, new_rate="30.00")
    reval_id = r["revaluation_id"]

    # Check GL entries sum to zero
    gl_rows = fresh_db.execute(
        """SELECT decimal_sum(debit) as total_debit, decimal_sum(credit) as total_credit
           FROM gl_entry
           WHERE voucher_type = 'stock_revaluation' AND voucher_id = ?
             AND is_cancelled = 0""",
        (reval_id,),
    ).fetchone()
    assert to_decimal(gl_rows["total_debit"]) == to_decimal(gl_rows["total_credit"])
    assert to_decimal(gl_rows["total_debit"]) == Decimal("500.00")


# ---------------------------------------------------------------------------
# 4. GL balanced on rate decrease
# ---------------------------------------------------------------------------

def test_gl_balanced_on_decrease(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="200", rate="10.00")

    r = _revalue(fresh_db, env, new_rate="8.00")
    reval_id = r["revaluation_id"]

    gl_rows = fresh_db.execute(
        """SELECT decimal_sum(debit) as total_debit, decimal_sum(credit) as total_credit
           FROM gl_entry
           WHERE voucher_type = 'stock_revaluation' AND voucher_id = ?
             AND is_cancelled = 0""",
        (reval_id,),
    ).fetchone()
    assert to_decimal(gl_rows["total_debit"]) == to_decimal(gl_rows["total_credit"])
    # 200 * 10 - 200 * 8 = 400
    assert to_decimal(gl_rows["total_debit"]) == Decimal("400.00")


# ---------------------------------------------------------------------------
# 5. Zero stock rejection
# ---------------------------------------------------------------------------

def test_zero_stock_rejection(fresh_db):
    env = setup_inventory_environment(fresh_db)
    # No stock received — qty is 0

    r = _revalue(fresh_db, env, new_rate="30.00")
    assert r["status"] == "error"
    assert "no stock" in r["message"].lower()


# ---------------------------------------------------------------------------
# 6. Cancel reverses SLE + GL
# ---------------------------------------------------------------------------

def test_cancel_reverses_sle_gl(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    r = _revalue(fresh_db, env, new_rate="30.00")
    reval_id = r["revaluation_id"]

    # Verify stock balance after revaluation
    bal = _call_action(
        db_query.ACTIONS["get-stock-balance"], fresh_db,
        item_id=env["item_id"], warehouse_id=env["warehouse_id"],
    )
    assert to_decimal(bal["valuation_rate"]) == Decimal("30.00")

    # Cancel
    r2 = _call_action(
        db_query.ACTIONS["cancel-stock-revaluation"], fresh_db,
        revaluation_id=reval_id,
    )
    assert r2["status"] == "ok"
    assert r2["cancelled"] is True

    # Stock balance should revert to original rate
    bal2 = _call_action(
        db_query.ACTIONS["get-stock-balance"], fresh_db,
        item_id=env["item_id"], warehouse_id=env["warehouse_id"],
    )
    assert to_decimal(bal2["valuation_rate"]) == Decimal("25.00")

    # All GL should net to zero (cancelled entries cancel out)
    gl_sum = fresh_db.execute(
        """SELECT decimal_sum(debit) as d, decimal_sum(credit) as c
           FROM gl_entry
           WHERE voucher_type = 'stock_revaluation' AND voucher_id = ?""",
        (reval_id,),
    ).fetchone()
    assert to_decimal(gl_sum["d"]) == to_decimal(gl_sum["c"])


# ---------------------------------------------------------------------------
# 7. List revaluations
# ---------------------------------------------------------------------------

def test_list_revaluations(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    _revalue(fresh_db, env, new_rate="28.00")

    r = _call_action(
        db_query.ACTIONS["list-stock-revaluations"], fresh_db,
        company_id=env["company_id"],
    )
    assert r["status"] == "ok"
    assert r["total"] == 1
    assert len(r["revaluations"]) == 1
    assert r["revaluations"][0]["new_rate"] == "28.00"


# ---------------------------------------------------------------------------
# 8. Get revaluation detail
# ---------------------------------------------------------------------------

def test_get_revaluation_detail(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    r = _revalue(fresh_db, env, new_rate="32.00", reason="Market correction")
    reval_id = r["revaluation_id"]

    detail = _call_action(
        db_query.ACTIONS["get-stock-revaluation"], fresh_db,
        revaluation_id=reval_id,
    )
    assert detail["status"] == "ok"
    assert detail["reason"] == "Market correction"
    assert detail["old_rate"] == "25.00"
    assert detail["new_rate"] == "32.00"
    assert len(detail["sle_entries"]) >= 1
    assert len(detail["gl_entries"]) == 2


# ---------------------------------------------------------------------------
# 9. Multi-warehouse isolation
# ---------------------------------------------------------------------------

def test_multi_warehouse_isolation(fresh_db):
    """Revaluing stock in one warehouse should not affect another."""
    env = setup_inventory_environment(fresh_db)

    # Create a second warehouse
    from helpers import create_test_warehouse
    wh2_id = create_test_warehouse(
        fresh_db, env["company_id"], "Warehouse B",
        account_id=env["stock_in_hand_id"],
    )

    # Receive 100 @ $25 in each warehouse
    items1 = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": "100", "rate": "25.00",
    }])
    items2 = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": wh2_id,
        "qty": "100", "rate": "25.00",
    }])
    se1 = create_test_stock_entry(fresh_db, env["company_id"], "receive", items1)
    submit_test_stock_entry(fresh_db, se1)
    se2 = create_test_stock_entry(fresh_db, env["company_id"], "receive", items2)
    submit_test_stock_entry(fresh_db, se2)

    # Revalue only warehouse 1 to $30
    r = _call_action(
        db_query.ACTIONS["revalue-stock"], fresh_db,
        item_id=env["item_id"],
        warehouse_id=env["warehouse_id"],
        new_rate="30.00",
        posting_date="2026-02-20",
    )
    assert r["status"] == "ok"

    # Warehouse 1: rate should be $30
    from erpclaw_lib.stock_posting import get_stock_balance
    bal1 = get_stock_balance(fresh_db, env["item_id"], env["warehouse_id"])
    assert to_decimal(bal1["valuation_rate"]) == Decimal("30.00")

    # Warehouse 2: rate should still be $25
    bal2 = get_stock_balance(fresh_db, env["item_id"], wh2_id)
    assert to_decimal(bal2["valuation_rate"]) == Decimal("25.00")


# ---------------------------------------------------------------------------
# 10. Re-revalue same item
# ---------------------------------------------------------------------------

def test_re_revalue(fresh_db):
    """Revalue twice — second revaluation should use first's new rate as old."""
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="50", rate="20.00")

    # First revaluation: 20 -> 25
    r1 = _revalue(fresh_db, env, new_rate="25.00", posting_date="2026-02-20")
    assert r1["old_rate"] == "20.00"
    assert r1["new_rate"] == "25.00"
    # 50 * (25 - 20) = 250
    assert to_decimal(r1["adjustment_amount"]) == Decimal("250.00")

    # Second revaluation: 25 -> 22
    r2 = _revalue(fresh_db, env, new_rate="22.00", posting_date="2026-02-21")
    assert r2["old_rate"] == "25.00"
    assert r2["new_rate"] == "22.00"
    # 50 * (22 - 25) = -150
    assert to_decimal(r2["adjustment_amount"]) == Decimal("-150.00")

    # Final stock balance
    from erpclaw_lib.stock_posting import get_stock_balance
    bal = get_stock_balance(fresh_db, env["item_id"], env["warehouse_id"])
    assert to_decimal(bal["valuation_rate"]) == Decimal("22.00")
    assert to_decimal(bal["stock_value"]) == Decimal("1100.00")


# ---------------------------------------------------------------------------
# 11. Same-rate rejection
# ---------------------------------------------------------------------------

def test_same_rate_rejection(fresh_db):
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")

    r = _revalue(fresh_db, env, new_rate="25.00")
    assert r["status"] == "error"
    assert "same" in r["message"].lower()


# ---------------------------------------------------------------------------
# 12. GL invariant: global debits = credits across all GL
# ---------------------------------------------------------------------------

def test_gl_invariant_after_revaluation(fresh_db):
    """After receive + revalue, total GL debits must equal total credits."""
    env = setup_inventory_environment(fresh_db)
    _receive_stock(fresh_db, env, qty="100", rate="25.00")
    _revalue(fresh_db, env, new_rate="30.00")

    # Global GL invariant
    totals = fresh_db.execute(
        """SELECT decimal_sum(debit) as total_debit,
                  decimal_sum(credit) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    assert to_decimal(totals["total_debit"]) == to_decimal(totals["total_credit"])
    # Receive: 2500 DR/CR + Revalue: 500 DR/CR = 3000 total
    assert to_decimal(totals["total_debit"]) == Decimal("3000.00")
