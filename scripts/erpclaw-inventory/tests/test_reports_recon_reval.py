"""Tests for erpclaw-inventory reports, reconciliation, and revaluation.

Actions tested: get-stock-balance, stock-balance-report, stock-ledger-report,
                add-stock-reconciliation, submit-stock-reconciliation,
                revalue-stock, list-stock-revaluations, get-stock-revaluation,
                cancel-stock-revaluation, check-reorder, status
"""
import json
import pytest
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_stock_entry_sle,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Stock Balance / Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestGetStockBalance:
    def test_get_balance(self, conn, env):
        result = call_action(mod.get_stock_balance_action, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert Decimal(result["qty"]) == Decimal("100")

    def test_missing_item_fails(self, conn, env):
        result = call_action(mod.get_stock_balance_action, conn, ns(
            item_id=None, warehouse_id=env["warehouse"],
        ))
        assert is_error(result)


class TestStockBalanceReport:
    def test_report(self, conn, env):
        result = call_action(mod.stock_balance_report, conn, ns(
            company_id=env["company_id"], warehouse_id=None,
        ))
        assert is_ok(result)
        assert result["row_count"] >= 1
        assert Decimal(result["total_stock_value"]) > 0

    def test_report_by_warehouse(self, conn, env):
        result = call_action(mod.stock_balance_report, conn, ns(
            company_id=env["company_id"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["row_count"] >= 1


class TestStockLedgerReport:
    def test_report(self, conn, env):
        result = call_action(mod.stock_ledger_report, conn, ns(
            item_id=None, warehouse_id=None,
            from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 1

    def test_report_by_item(self, conn, env):
        result = call_action(mod.stock_ledger_report, conn, ns(
            item_id=env["item1"], warehouse_id=None,
            from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Stock Reconciliation
# ──────────────────────────────────────────────────────────────────────────────

class TestAddStockReconciliation:
    def test_basic_create(self, conn, env):
        items = json.dumps([{
            "item_id": env["item1"], "warehouse_id": env["warehouse"],
            "qty": "90", "valuation_rate": "50.00",
        }])
        result = call_action(mod.add_stock_reconciliation, conn, ns(
            posting_date="2026-06-15", items=items,
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "stock_reconciliation_id" in result
        # Difference should be -10 qty * 50 = -500
        assert Decimal(result["difference_amount"]) == Decimal("-500.00")

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_stock_reconciliation, conn, ns(
            posting_date="2026-06-15", items=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)


class TestSubmitStockReconciliation:
    def test_submit(self, conn, env):
        items = json.dumps([{
            "item_id": env["item1"], "warehouse_id": env["warehouse"],
            "qty": "95", "valuation_rate": "50.00",
        }])
        create = call_action(mod.add_stock_reconciliation, conn, ns(
            posting_date="2026-06-15", items=items,
            company_id=env["company_id"],
        ))
        result = call_action(mod.submit_stock_reconciliation, conn, ns(
            stock_reconciliation_id=create["stock_reconciliation_id"],
        ))
        assert is_ok(result)

        row = conn.execute(
            "SELECT status FROM stock_reconciliation WHERE id=?",
            (create["stock_reconciliation_id"],)
        ).fetchone()
        assert row["status"] == "submitted"

        # Check SLE entries were created
        assert result["sle_entries_created"] >= 1

    def test_submit_already_submitted_fails(self, conn, env):
        items = json.dumps([{
            "item_id": env["item1"], "warehouse_id": env["warehouse"],
            "qty": "80",
        }])
        create = call_action(mod.add_stock_reconciliation, conn, ns(
            posting_date="2026-06-15", items=items,
            company_id=env["company_id"],
        ))
        call_action(mod.submit_stock_reconciliation, conn, ns(
            stock_reconciliation_id=create["stock_reconciliation_id"],
        ))
        result = call_action(mod.submit_stock_reconciliation, conn, ns(
            stock_reconciliation_id=create["stock_reconciliation_id"],
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Stock Revaluation  (BUG-006: stock_revaluation table missing from init_schema)
# ──────────────────────────────────────────────────────────────────────────────

class TestRevalueStock:
    @pytest.mark.xfail(reason="BUG-006: stock_revaluation table not in init_schema.py")
    def test_basic_revalue(self, conn, env):
        result = call_action(mod.revalue_stock, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="60.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason="Market adjustment",
        ))
        assert is_ok(result)
        assert "revaluation_id" in result

    def test_missing_item_fails(self, conn, env):
        result = call_action(mod.revalue_stock, conn, ns(
            item_id=None, warehouse_id=env["warehouse"],
            new_rate="60.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason=None,
        ))
        assert is_error(result)


@pytest.mark.xfail(reason="BUG-006: stock_revaluation table not in init_schema.py")
class TestListStockRevaluations:
    def test_list(self, conn, env):
        call_action(mod.revalue_stock, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="55.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason=None,
        ))
        result = call_action(mod.list_stock_revaluations, conn, ns(
            company_id=env["company_id"], item_id=None,
            rv_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


@pytest.mark.xfail(reason="BUG-006: stock_revaluation table not in init_schema.py")
class TestGetStockRevaluation:
    def test_get(self, conn, env):
        rv = call_action(mod.revalue_stock, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="65.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason=None,
        ))
        result = call_action(mod.get_stock_revaluation, conn, ns(
            revaluation_id=rv["revaluation_id"],
        ))
        assert is_ok(result)

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_stock_revaluation, conn, ns(
            revaluation_id="fake-id",
        ))
        assert is_error(result)


@pytest.mark.xfail(reason="BUG-006: stock_revaluation table not in init_schema.py")
class TestCancelStockRevaluation:
    def test_cancel(self, conn, env):
        rv = call_action(mod.revalue_stock, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="70.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason=None,
        ))
        result = call_action(mod.cancel_stock_revaluation, conn, ns(
            revaluation_id=rv["revaluation_id"],
        ))
        assert is_ok(result)

        row = conn.execute(
            "SELECT status FROM stock_revaluation WHERE id=?",
            (rv["revaluation_id"],)
        ).fetchone()
        assert row["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Check Reorder & Status
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckReorder:
    def test_no_items_below_reorder(self, conn, env):
        """No items have reorder_level set, so 0 results."""
        result = call_action(mod.check_reorder, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["items_below_reorder"] == 0

    def test_item_below_reorder(self, conn, env):
        """Set reorder level above current stock to trigger."""
        conn.execute(
            "UPDATE item SET reorder_level = '200', reorder_qty = '50' WHERE id = ?",
            (env["item1"],)
        )
        conn.commit()
        result = call_action(mod.check_reorder, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["items_below_reorder"] >= 1


class TestStatus:
    def test_status(self, conn, env):
        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["items"] >= 2
        assert result["warehouses"] >= 2
