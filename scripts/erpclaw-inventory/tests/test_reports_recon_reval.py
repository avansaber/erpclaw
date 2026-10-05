"""Tests for erpclaw-inventory reports, reconciliation, and revaluation.

Actions tested: get-stock-balance, stock-balance-report, stock-ledger-report,
                add-stock-reconciliation, submit-stock-reconciliation,
                revalue-stock, list-stock-revaluations, get-stock-revaluation,
                cancel-stock-revaluation, check-reorder, status,
                inventory-demand-forecast
"""
import json
import pytest
import uuid
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_item, seed_stock_entry_sle, seed_warehouse,
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


# ──────────────────────────────────────────────────────────────────────────────
# Inventory Demand Forecast
# ──────────────────────────────────────────────────────────────────────────────

def _seed_forecast_sle(conn, item_id, warehouse_id, qty, posting_date):
    """Seed one posted stock ledger row with an explicit posting date."""
    sle_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, actual_qty,
            qty_after_transaction, valuation_rate, stock_value,
            stock_value_difference, voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, ?, ?, ?, '50.00', '0', '0', 'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id, posting_date, qty, qty,
         f"SE-{sle_id[:8]}"),
    )
    conn.commit()
    return sle_id


def _audit_log_count(conn):
    return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def _sle_count(conn):
    return conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0]


class TestInventoryDemandForecast:
    AS_OF = "2026-06-15"

    def _base_ns(self, env, **overrides):
        kw = dict(
            company_id=env["company_id"], item_id=env["item1"],
            warehouse_id=env["warehouse"], as_of_date=self.AS_OF,
            history_days="10", horizon_days="5",
        )
        kw.update(overrides)
        return ns(**kw)

    def _seed_window(self, conn, env):
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "20.000000", "2026-06-10")
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "-3.100000", "2026-06-08")
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "-1.200000", "2026-06-12")
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "-9.000000", "2026-05-01")

    def test_action_registered(self):
        assert "inventory-demand-forecast" in mod.ACTIONS

    def test_consumes_posted_outbound_only(self, conn, env):
        self._seed_window(conn, env)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert result["basis"] == "posted_outbound_stock"
        assert result["window_start"] == "2026-06-06"
        assert result["window_end"] == "2026-06-15"
        assert result["rows_used"] == 2
        assert result["total_consumed"] == "4.300000"

    def test_velocity_and_projection(self, conn, env):
        self._seed_window(conn, env)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert result["history_days"] == 10
        assert result["horizon_days"] == 5
        assert result["daily_velocity"] == "0.430000"
        assert result["projected_demand"] == "2.150000"

    def test_window_bounds_inclusive(self, conn, env):
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "-1.000000", "2026-06-06")
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "-1.000000", "2026-06-15")
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert result["rows_used"] == 2
        assert result["total_consumed"] == "2.000000"

    def test_inbound_only_is_zero(self, conn, env):
        _seed_forecast_sle(conn, env["item1"], env["warehouse"],
                           "20.000000", "2026-06-10")
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert result["rows_used"] == 0
        assert result["total_consumed"] == "0.000000"
        assert result["daily_velocity"] == "0.000000"
        assert result["projected_demand"] == "0.000000"

    def test_other_scope_rows_excluded(self, conn, env):
        self._seed_window(conn, env)
        _seed_forecast_sle(conn, env["item2"], env["warehouse"],
                           "-50.000000", "2026-06-10")
        _seed_forecast_sle(conn, env["item1"], env["warehouse2"],
                           "-50.000000", "2026-06-10")
        other_company = seed_company(conn)
        other_item = seed_item(conn)
        other_warehouse = seed_warehouse(conn, other_company)
        _seed_forecast_sle(conn, other_item, other_warehouse,
                           "-50.000000", "2026-06-10")
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert result["rows_used"] == 2
        assert result["total_consumed"] == "4.300000"
        assert result["daily_velocity"] == "0.430000"
        assert result["projected_demand"] == "2.150000"

    def test_integer_day_inputs(self, conn, env):
        self._seed_window(conn, env)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env, history_days=10,
                                           horizon_days=5))
        assert is_ok(result)
        assert result["total_consumed"] == "4.300000"
        assert result["projected_demand"] == "2.150000"

    def test_missing_inputs_refuse(self, conn, env):
        self._seed_window(conn, env)
        cases = [
            dict(company_id=None),
            dict(item_id=None),
            dict(warehouse_id=None),
            dict(as_of_date=None),
            dict(history_days=None),
            dict(horizon_days=None),
        ]
        for override in cases:
            result = call_action(mod.inventory_demand_forecast, conn,
                                 self._base_ns(env, **override))
            assert is_error(result), override

    def test_zero_and_bad_days_refuse(self, conn, env):
        for flag in ("history_days", "horizon_days"):
            for bad in ("0", "-5", "367", "abc", "5.5", ""):
                result = call_action(mod.inventory_demand_forecast, conn,
                                     self._base_ns(env, **{flag: bad}))
                assert is_error(result), (flag, bad)

    def test_malformed_dates_refuse(self, conn, env):
        for bad in ("not-a-date", "2026-13-01", "2026-02-30",
                    "06/15/2026", "2026-6-15", ""):
            result = call_action(mod.inventory_demand_forecast, conn,
                                 self._base_ns(env, as_of_date=bad))
            assert is_error(result), bad

    def test_unknown_records_refuse(self, conn, env):
        for override in (dict(company_id="no-such-company"),
                         dict(item_id="no-such-item"),
                         dict(warehouse_id="no-such-warehouse")):
            result = call_action(mod.inventory_demand_forecast, conn,
                                 self._base_ns(env, **override))
            assert is_error(result), override

    def test_foreign_warehouse_refuses(self, conn, env):
        other_company = seed_company(conn)
        other_warehouse = seed_warehouse(conn, other_company)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env,
                                           warehouse_id=other_warehouse))
        assert is_error(result)

    def test_refusals_write_nothing(self, conn, env):
        audits_before = _audit_log_count(conn)
        sle_before = _sle_count(conn)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env, history_days="0"))
        assert is_error(result)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env, as_of_date="bogus"))
        assert is_error(result)
        assert _audit_log_count(conn) == audits_before
        assert _sle_count(conn) == sle_before

    def test_success_writes_nothing(self, conn, env):
        self._seed_window(conn, env)
        audits_before = _audit_log_count(conn)
        sle_before = _sle_count(conn)
        result = call_action(mod.inventory_demand_forecast, conn,
                             self._base_ns(env))
        assert is_ok(result)
        assert _audit_log_count(conn) == audits_before
        assert _sle_count(conn) == sle_before
