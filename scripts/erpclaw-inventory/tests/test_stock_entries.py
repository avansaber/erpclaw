"""Tests for erpclaw-inventory stock entry lifecycle.

Actions tested: add-stock-entry, get-stock-entry, list-stock-entries,
                submit-stock-entry, cancel-stock-entry
"""
import json
import pytest
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate, to_wh_key, from_wh_key)."""
    result = []
    for spec in specs:
        item_key, qty, rate = spec[0], spec[1], spec[2]
        entry = {"item_id": env[item_key], "qty": qty, "rate": rate}
        if len(spec) > 3 and spec[3]:
            entry["to_warehouse_id"] = env[spec[3]]
        if len(spec) > 4 and spec[4]:
            entry["from_warehouse_id"] = env[spec[4]]
        result.append(entry)
    return json.dumps(result)


def _create_draft_se(conn, env, entry_type="receive", items_str=None):
    """Create a draft stock entry."""
    if not items_str:
        if entry_type == "receive":
            items_str = _items(env, ("item1", "10", "50.00", "warehouse", None))
        elif entry_type == "issue":
            items_str = _items(env, ("item1", "5", "50.00", None, "warehouse"))
        elif entry_type == "transfer":
            items_str = _items(env, ("item1", "5", "50.00", "warehouse2", "warehouse"))
    result = call_action(mod.add_stock_entry, conn, ns(
        entry_type=entry_type, company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
    ))
    return result


class TestAddStockEntry:
    def test_receive(self, conn, env):
        result = _create_draft_se(conn, env, "receive")
        assert is_ok(result)
        assert "stock_entry_id" in result
        assert Decimal(result["total_incoming_value"]) == Decimal("500.00")

    def test_issue(self, conn, env):
        result = _create_draft_se(conn, env, "issue")
        assert is_ok(result)
        assert Decimal(result["total_outgoing_value"]) == Decimal("250.00")

    def test_transfer(self, conn, env):
        result = _create_draft_se(conn, env, "transfer")
        assert is_ok(result)
        assert Decimal(result["total_incoming_value"]) == Decimal("250.00")
        assert Decimal(result["total_outgoing_value"]) == Decimal("250.00")

    def test_missing_type_fails(self, conn, env):
        result = call_action(mod.add_stock_entry, conn, ns(
            entry_type=None, company_id=env["company_id"],
            posting_date="2026-06-15",
            items=_items(env, ("item1", "10", "50.00", "warehouse", None)),
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_stock_entry, conn, ns(
            entry_type="invalid", company_id=env["company_id"],
            posting_date="2026-06-15",
            items=_items(env, ("item1", "10", "50.00", "warehouse", None)),
        ))
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_stock_entry, conn, ns(
            entry_type="receive", company_id=env["company_id"],
            posting_date="2026-06-15", items=None,
        ))
        assert is_error(result)


class TestGetStockEntry:
    def test_get(self, conn, env):
        se = _create_draft_se(conn, env)
        result = call_action(mod.get_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        assert is_ok(result)
        assert "items" in result

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_stock_entry, conn, ns(
            stock_entry_id="fake-id",
        ))
        assert is_error(result)


class TestListStockEntries:
    def test_list(self, conn, env):
        _create_draft_se(conn, env)
        result = call_action(mod.list_stock_entries, conn, ns(
            company_id=env["company_id"], entry_type=None,
            se_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_type(self, conn, env):
        _create_draft_se(conn, env, "receive")
        result = call_action(mod.list_stock_entries, conn, ns(
            company_id=env["company_id"], entry_type="receive",
            se_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitStockEntry:
    def test_submit_receive(self, conn, env):
        se = _create_draft_se(conn, env, "receive")
        result = call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM stock_entry WHERE id=?",
                           (se["stock_entry_id"],)).fetchone()
        assert row["status"] == "submitted"

        # Check SLE entries were created
        sle_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_ledger_entry WHERE voucher_id=?",
            (se["stock_entry_id"],)
        ).fetchone()["cnt"]
        assert sle_count >= 1

    def test_submit_already_submitted_fails(self, conn, env):
        se = _create_draft_se(conn, env, "receive")
        call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        result = call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        assert is_error(result)


class TestCancelStockEntry:
    def test_cancel(self, conn, env):
        se = _create_draft_se(conn, env, "receive")
        call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        result = call_action(mod.cancel_stock_entry, conn, ns(
            stock_entry_id=se["stock_entry_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM stock_entry WHERE id=?",
                           (se["stock_entry_id"],)).fetchone()
        assert row["status"] == "cancelled"

    def test_cancel_nonexistent_fails(self, conn, env):
        result = call_action(mod.cancel_stock_entry, conn, ns(
            stock_entry_id="fake-id",
        ))
        assert is_error(result)
