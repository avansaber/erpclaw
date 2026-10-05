"""Standard cost variance report (floor-o032 v1).

Read-only report over posted stock ledger rows: each row's quantity is
valued at the item's standard_rate (standard value) and compared with the
posted stock_value_difference (actual value); variance is actual minus
standard. Company scope comes from the row's warehouse; the inclusive
date window is required; optional item/warehouse filters narrow the rows
and refuse foreign records. Posts nothing.
"""
import os
import re
import sys
import uuid
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest
from erpclaw_lib.dependencies import table_exists

from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_warehouse,
)

mod = load_db_query()

MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")


def _seed_sle(conn, item_id, warehouse_id, qty, valuation_rate,
              posting_date="2026-03-10", cancelled=0):
    sid = str(uuid.uuid4())
    value = str(Decimal(qty) * Decimal(valuation_rate))
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, actual_qty,
            qty_after_transaction, valuation_rate, stock_value,
            stock_value_difference, voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'stock_entry', ?, ?)""",
        (sid, item_id, warehouse_id, posting_date, qty, qty,
         valuation_rate, value, value, f"VAR-{sid[:8]}", cancelled))
    conn.commit()
    return sid


def _report(conn, env, **over):
    base = dict(company_id=env["company_id"],
                from_date="2026-03-01", to_date="2026-03-31",
                item_id=None, warehouse_id=None)
    base.update(over)
    return call_action(mod.standard_cost_variance_report, conn, ns(**base))


def _table_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


class TestExactMoney:
    def test_two_place_values_and_variance(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        result = _report(conn, env)
        assert is_ok(result)
        assert result["scope"] == "recorded_stock_rows"
        assert result["row_count"] == 1
        row = result["details"][0]
        assert row["quantity"] == "10.00"
        assert row["standard_rate"] == "50.00"
        assert row["standard_value"] == "500.00"
        assert row["actual_value"] == "550.00"
        assert row["variance"] == "50.00"
        assert result["total_quantity"] == "10.00"
        assert result["total_standard_value"] == "500.00"
        assert result["total_actual_value"] == "550.00"
        assert result["total_variance"] == "50.00"

    def test_all_money_fields_two_places(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "3", "52.335", posting_date="2026-03-11")
        result = _report(conn, env)
        assert is_ok(result)
        for row in result["details"]:
            for key in ("standard_rate", "standard_value",
                        "actual_value", "variance", "quantity"):
                assert MONEY_RE.match(row[key]), (key, row[key])
        for key in ("total_quantity", "total_standard_value",
                    "total_actual_value", "total_variance"):
            assert MONEY_RE.match(result[key]), (key, result[key])

    def test_negative_variance(self, conn, env):
        _seed_sle(conn, env["item2"], env["warehouse"],
                  "2", "90.00", posting_date="2026-03-12")
        result = _report(conn, env, item_id=env["item2"])
        assert is_ok(result)
        assert result["details"][0]["standard_value"] == "200.00"
        assert result["details"][0]["actual_value"] == "180.00"
        assert result["details"][0]["variance"] == "-20.00"
        assert result["total_variance"] == "-20.00"

    def test_cancelled_rows_excluded(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10", cancelled=1)
        result = _report(conn, env)
        assert is_ok(result)
        assert result["row_count"] == 0
        assert result["total_variance"] == "0.00"


class TestCompanyIsolation:
    def test_other_company_rows_excluded(self, conn, env):
        other_company = seed_company(conn, name="Other Co", abbr="OC")
        other_wh = seed_warehouse(conn, other_company, "Other Warehouse")
        _seed_sle(conn, env["item1"], other_wh,
                  "10", "55.00", posting_date="2026-03-10")
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        result = _report(conn, env)
        assert is_ok(result)
        assert result["row_count"] == 1
        assert result["total_actual_value"] == "550.00"
        other = _report(conn, env, company_id=other_company)
        assert is_ok(other)
        assert other["row_count"] == 1
        assert other["total_actual_value"] == "550.00"


class TestFilters:
    def test_item_filter(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        _seed_sle(conn, env["item2"], env["warehouse"],
                  "2", "90.00", posting_date="2026-03-10")
        only1 = _report(conn, env, item_id=env["item1"])
        assert is_ok(only1)
        assert only1["row_count"] == 1
        assert only1["details"][0]["item_id"] == env["item1"]
        only2 = _report(conn, env, item_id=env["item2"])
        assert is_ok(only2)
        assert only2["row_count"] == 1
        assert only2["details"][0]["item_id"] == env["item2"]

    def test_warehouse_filter(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        _seed_sle(conn, env["item1"], env["warehouse2"],
                  "4", "50.00", posting_date="2026-03-10")
        first = _report(conn, env, warehouse_id=env["warehouse"])
        assert is_ok(first)
        assert first["row_count"] == 1
        assert first["details"][0]["warehouse_id"] == env["warehouse"]
        second = _report(conn, env, warehouse_id=env["warehouse2"])
        assert is_ok(second)
        assert second["row_count"] == 1
        assert second["details"][0]["warehouse_id"] == env["warehouse2"]

    def test_date_window(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-04-10")
        result = _report(conn, env)
        assert is_ok(result)
        assert result["row_count"] == 1
        assert result["details"][0]["posting_date"] == "2026-03-10"

    def test_foreign_warehouse_refused(self, conn, env):
        other_company = seed_company(conn, name="Foreign Co", abbr="FC")
        other_wh = seed_warehouse(conn, other_company, "Foreign Warehouse")
        result = _report(conn, env, warehouse_id=other_wh)
        assert is_error(result)

    def test_missing_item_refused(self, conn, env):
        result = _report(conn, env, item_id=str(uuid.uuid4()))
        assert is_error(result)

    def test_missing_warehouse_refused(self, conn, env):
        result = _report(conn, env, warehouse_id=str(uuid.uuid4()))
        assert is_error(result)


class TestRequiredBounds:
    def test_missing_company_refused(self, conn, env):
        result = call_action(mod.standard_cost_variance_report, conn, ns(
            company_id=None, company_name=None,
            from_date="2026-03-01", to_date="2026-03-31",
            item_id=None, warehouse_id=None))
        assert is_error(result)

    def test_missing_dates_refused(self, conn, env):
        missing_from = call_action(mod.standard_cost_variance_report, conn, ns(
            company_id=env["company_id"], from_date=None, to_date="2026-03-31",
            item_id=None, warehouse_id=None))
        assert is_error(missing_from)
        missing_to = call_action(mod.standard_cost_variance_report, conn, ns(
            company_id=env["company_id"], from_date="2026-03-01", to_date=None,
            item_id=None, warehouse_id=None))
        assert is_error(missing_to)

    def test_reversed_dates_refused(self, conn, env):
        result = call_action(mod.standard_cost_variance_report, conn, ns(
            company_id=env["company_id"], from_date="2026-03-31",
            to_date="2026-03-01", item_id=None, warehouse_id=None))
        assert is_error(result)

    def test_malformed_date_refused(self, conn, env):
        result = call_action(mod.standard_cost_variance_report, conn, ns(
            company_id=env["company_id"], from_date="03/01/2026",
            to_date="2026-03-31", item_id=None, warehouse_id=None))
        assert is_error(result)


class TestDeterminism:
    def test_stable_ordering(self, conn, env):
        for day in ("2026-03-12", "2026-03-10", "2026-03-11"):
            _seed_sle(conn, env["item1"], env["warehouse"],
                      "1", "50.00", posting_date=day)
        result = _report(conn, env)
        assert is_ok(result)
        assert [r["posting_date"] for r in result["details"]] == [
            "2026-03-10", "2026-03-11", "2026-03-12"]

    def test_two_identical_calls(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        first = _report(conn, env)
        second = _report(conn, env)
        assert is_ok(first) and is_ok(second)
        assert first == second


class TestReadOnly:
    def test_no_writes(self, conn, env):
        _seed_sle(conn, env["item1"], env["warehouse"],
                  "10", "55.00", posting_date="2026-03-10")
        before = {
            table: _table_count(conn, table)
            for table in ("stock_ledger_entry", "stock_entry",
                          "general_ledger_entry", "audit_log")
            if table_exists(conn, table)
        }
        changes_before = conn.total_changes
        result = _report(conn, env)
        assert is_ok(result)
        _report(conn, env)
        assert conn.total_changes == changes_before
        for table, count in before.items():
            assert _table_count(conn, table) == count

    def test_action_registered(self):
        assert mod.ACTIONS["standard-cost-variance-report"] is \
            mod.standard_cost_variance_report
