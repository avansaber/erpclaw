"""Tests for erpclaw-inventory item, item group, and warehouse management.

Actions tested: add-item, update-item, get-item, list-items,
                add-item-group, list-item-groups,
                add-warehouse, update-warehouse, list-warehouses
"""
import pytest
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Items
# ──────────────────────────────────────────────────────────────────────────────

class TestAddItem:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_item, conn, ns(
            item_code="WDG-001", item_name="Test Widget",
            item_type=None, valuation_method=None, item_group=None,
            stock_uom=None, has_batch=None, has_serial=None,
            standard_rate=None,
        ))
        assert is_ok(result)
        assert "item_id" in result
        assert result["item_code"] == "WDG-001"

    def test_with_options(self, conn, env):
        result = call_action(mod.add_item, conn, ns(
            item_code="SRV-001", item_name="Consulting Service",
            item_type="service", valuation_method="fifo", item_group=None,
            stock_uom="Hour", has_batch=None, has_serial=None,
            standard_rate="150.00",
        ))
        assert is_ok(result)

    def test_missing_code_fails(self, conn, env):
        result = call_action(mod.add_item, conn, ns(
            item_code=None, item_name="No Code Item",
            item_type=None, valuation_method=None, item_group=None,
            stock_uom=None, has_batch=None, has_serial=None,
            standard_rate=None,
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_item, conn, ns(
            item_code="NONAME", item_name=None,
            item_type=None, valuation_method=None, item_group=None,
            stock_uom=None, has_batch=None, has_serial=None,
            standard_rate=None,
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_item, conn, ns(
            item_code="BAD", item_name="Bad Type",
            item_type="invalid", valuation_method=None, item_group=None,
            stock_uom=None, has_batch=None, has_serial=None,
            standard_rate=None,
        ))
        assert is_error(result)


class TestUpdateItem:
    def test_update_name(self, conn, env):
        result = call_action(mod.update_item, conn, ns(
            item_id=env["item1"], item_name="Updated Widget",
            reorder_level=None, reorder_qty=None,
            standard_rate=None, item_status=None,
        ))
        assert is_ok(result)
        assert "item_name" in result["updated_fields"]

    def test_update_rate(self, conn, env):
        result = call_action(mod.update_item, conn, ns(
            item_id=env["item1"], item_name=None,
            reorder_level=None, reorder_qty=None,
            standard_rate="75.00", item_status=None,
        ))
        assert is_ok(result)
        assert "standard_rate" in result["updated_fields"]

    def test_update_no_fields_fails(self, conn, env):
        result = call_action(mod.update_item, conn, ns(
            item_id=env["item1"], item_name=None,
            reorder_level=None, reorder_qty=None,
            standard_rate=None, item_status=None,
        ))
        assert is_error(result)

    def test_update_nonexistent_fails(self, conn, env):
        result = call_action(mod.update_item, conn, ns(
            item_id="fake-id", item_name="Nope",
            reorder_level=None, reorder_qty=None,
            standard_rate=None, item_status=None,
        ))
        assert is_error(result)


class TestGetItem:
    def test_get(self, conn, env):
        result = call_action(mod.get_item, conn, ns(
            item_id=env["item1"],
        ))
        assert is_ok(result)
        assert result["id"] == env["item1"]
        assert "stock_balances" in result

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_item, conn, ns(
            item_id="fake-id",
        ))
        assert is_error(result)


class TestListItems:
    def test_list(self, conn, env):
        result = call_action(mod.list_items, conn, ns(
            search=None, item_group=None, item_type=None,
            warehouse_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2

    def test_list_search(self, conn, env):
        result = call_action(mod.list_items, conn, ns(
            search="Widget", item_group=None, item_type=None,
            warehouse_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Item Groups
# ──────────────────────────────────────────────────────────────────────────────

class TestAddItemGroup:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_item_group, conn, ns(
            name="Electronics", parent_id=None,
        ))
        assert is_ok(result)
        assert "item_group_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_item_group, conn, ns(
            name=None, parent_id=None,
        ))
        assert is_error(result)


class TestListItemGroups:
    def test_list(self, conn, env):
        call_action(mod.add_item_group, conn, ns(
            name="Group A", parent_id=None,
        ))
        result = call_action(mod.list_item_groups, conn, ns(
            parent_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Warehouses
# ──────────────────────────────────────────────────────────────────────────────

class TestAddWarehouse:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_warehouse, conn, ns(
            name="New Warehouse", company_id=env["company_id"],
            warehouse_type=None, parent_id=None,
            account_id=None, is_group=None,
        ))
        assert is_ok(result)
        assert "warehouse_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_warehouse, conn, ns(
            name=None, company_id=env["company_id"],
            warehouse_type=None, parent_id=None,
            account_id=None, is_group=None,
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_warehouse, conn, ns(
            name="Bad WH", company_id=env["company_id"],
            warehouse_type="invalid", parent_id=None,
            account_id=None, is_group=None,
        ))
        assert is_error(result)


class TestUpdateWarehouse:
    def test_update_name(self, conn, env):
        result = call_action(mod.update_warehouse, conn, ns(
            warehouse_id=env["warehouse"], name="Renamed Warehouse",
            account_id=None,
        ))
        assert is_ok(result)
        assert "name" in result["updated_fields"]

    def test_no_fields_fails(self, conn, env):
        result = call_action(mod.update_warehouse, conn, ns(
            warehouse_id=env["warehouse"], name=None,
            account_id=None,
        ))
        assert is_error(result)


class TestListWarehouses:
    def test_list(self, conn, env):
        result = call_action(mod.list_warehouses, conn, ns(
            company_id=env["company_id"], parent_id=None,
            warehouse_type=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
