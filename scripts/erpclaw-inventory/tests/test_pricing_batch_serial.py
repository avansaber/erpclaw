"""Tests for erpclaw-inventory pricing, batches, and serial numbers.

Actions tested: add-price-list, add-item-price, get-item-price, add-pricing-rule,
                add-batch, list-batches, add-serial-number, list-serial-numbers
"""
import pytest
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Price Lists & Item Prices
# ──────────────────────────────────────────────────────────────────────────────

class TestAddPriceList:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_price_list, conn, ns(
            name="Standard Selling", currency=None,
            is_buying=None, is_selling="1",
        ))
        assert is_ok(result)
        assert "price_list_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_price_list, conn, ns(
            name=None, currency=None,
            is_buying=None, is_selling=None,
        ))
        assert is_error(result)


class TestAddItemPrice:
    def test_basic_create(self, conn, env):
        pl = call_action(mod.add_price_list, conn, ns(
            name="Test PL", currency=None,
            is_buying=None, is_selling=None,
        ))
        result = call_action(mod.add_item_price, conn, ns(
            item_id=env["item1"], price_list_id=pl["price_list_id"],
            rate="99.99", min_qty=None,
            valid_from=None, valid_to=None,
        ))
        assert is_ok(result)
        assert Decimal(result["rate"]) == Decimal("99.99")

    def test_missing_rate_fails(self, conn, env):
        pl = call_action(mod.add_price_list, conn, ns(
            name="Test PL 2", currency=None,
            is_buying=None, is_selling=None,
        ))
        result = call_action(mod.add_item_price, conn, ns(
            item_id=env["item1"], price_list_id=pl["price_list_id"],
            rate=None, min_qty=None,
            valid_from=None, valid_to=None,
        ))
        assert is_error(result)


class TestGetItemPrice:
    def test_get_price(self, conn, env):
        pl = call_action(mod.add_price_list, conn, ns(
            name="Retail PL", currency=None,
            is_buying=None, is_selling=None,
        ))
        call_action(mod.add_item_price, conn, ns(
            item_id=env["item1"], price_list_id=pl["price_list_id"],
            rate="75.00", min_qty=None,
            valid_from=None, valid_to=None,
        ))
        result = call_action(mod.get_item_price, conn, ns(
            item_id=env["item1"], price_list_id=pl["price_list_id"],
            qty=None,
        ))
        assert is_ok(result)
        assert Decimal(result["rate"]) == Decimal("75.00")

    def test_no_price_fails(self, conn, env):
        pl = call_action(mod.add_price_list, conn, ns(
            name="Empty PL", currency=None,
            is_buying=None, is_selling=None,
        ))
        result = call_action(mod.get_item_price, conn, ns(
            item_id=env["item2"], price_list_id=pl["price_list_id"],
            qty=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Pricing Rules
# ──────────────────────────────────────────────────────────────────────────────

class TestAddPricingRule:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_pricing_rule, conn, ns(
            name="10% Off Widgets", applies_to="item",
            entity_id=env["item1"], discount_percentage="10",
            pr_rate=None, min_qty=None, max_qty=None,
            valid_from=None, valid_to=None,
            priority=None, company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "pricing_rule_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_pricing_rule, conn, ns(
            name=None, applies_to="item",
            entity_id=env["item1"], discount_percentage="10",
            pr_rate=None, min_qty=None, max_qty=None,
            valid_from=None, valid_to=None,
            priority=None, company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_invalid_applies_to_fails(self, conn, env):
        result = call_action(mod.add_pricing_rule, conn, ns(
            name="Bad Rule", applies_to="invalid",
            entity_id=env["item1"], discount_percentage="10",
            pr_rate=None, min_qty=None, max_qty=None,
            valid_from=None, valid_to=None,
            priority=None, company_id=env["company_id"],
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Batches
# ──────────────────────────────────────────────────────────────────────────────

class TestAddBatch:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_batch, conn, ns(
            item_id=env["item1"], batch_name="BATCH-001",
            manufacturing_date="2026-01-15", expiry_date="2027-01-15",
        ))
        assert is_ok(result)
        assert "batch_id" in result

    def test_missing_item_fails(self, conn, env):
        result = call_action(mod.add_batch, conn, ns(
            item_id=None, batch_name="BATCH-002",
            manufacturing_date=None, expiry_date=None,
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_batch, conn, ns(
            item_id=env["item1"], batch_name=None,
            manufacturing_date=None, expiry_date=None,
        ))
        assert is_error(result)


class TestListBatches:
    def test_list(self, conn, env):
        call_action(mod.add_batch, conn, ns(
            item_id=env["item1"], batch_name="BATCH-L1",
            manufacturing_date=None, expiry_date=None,
        ))
        result = call_action(mod.list_batches, conn, ns(
            item_id=None, warehouse_id=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_item(self, conn, env):
        call_action(mod.add_batch, conn, ns(
            item_id=env["item1"], batch_name="BATCH-L2",
            manufacturing_date=None, expiry_date=None,
        ))
        result = call_action(mod.list_batches, conn, ns(
            item_id=env["item1"], warehouse_id=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Serial Numbers
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSerialNumber:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_serial_number, conn, ns(
            item_id=env["item1"], serial_no="SN-001",
            warehouse_id=env["warehouse"], batch_id=None,
        ))
        assert is_ok(result)
        assert "serial_number_id" in result

    def test_missing_item_fails(self, conn, env):
        result = call_action(mod.add_serial_number, conn, ns(
            item_id=None, serial_no="SN-002",
            warehouse_id=None, batch_id=None,
        ))
        assert is_error(result)

    def test_missing_serial_fails(self, conn, env):
        result = call_action(mod.add_serial_number, conn, ns(
            item_id=env["item1"], serial_no=None,
            warehouse_id=None, batch_id=None,
        ))
        assert is_error(result)


class TestListSerialNumbers:
    def test_list(self, conn, env):
        call_action(mod.add_serial_number, conn, ns(
            item_id=env["item1"], serial_no="SN-L1",
            warehouse_id=env["warehouse"], batch_id=None,
        ))
        result = call_action(mod.list_serial_numbers, conn, ns(
            item_id=None, warehouse_id=None,
            sn_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_item(self, conn, env):
        call_action(mod.add_serial_number, conn, ns(
            item_id=env["item1"], serial_no="SN-L2",
            warehouse_id=env["warehouse"], batch_id=None,
        ))
        result = call_action(mod.list_serial_numbers, conn, ns(
            item_id=env["item1"], warehouse_id=None,
            sn_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
