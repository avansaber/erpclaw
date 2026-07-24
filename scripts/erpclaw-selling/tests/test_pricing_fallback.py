"""Tests for erpclaw-selling F7 invoice-time rate resolution (D6).

Covers _calculate_line_items' rate fallback and the customer
default-price-list wiring:

  - explicit rate in --items wins over everything (respected even at 0)
  - item_price hit via the customer's default price list
  - item_price hit via any enabled selling list when no default is set
  - item_price miss -> item.standard_rate
  - everything misses -> the historical $0 behavior (unchanged)
  - min_qty tiering and valid_from/valid_to windows honored
  - the standalone create-sales-invoice leg resolves the same way
  - add/update-customer persist + validate --default-price-list-id

Resolution is asserted by reading the stored line rate (quotation_item /
sales_invoice_item), with exact Decimal comparisons.
"""
import json
import uuid
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Local seed helpers (price lists / item prices / catalog rate)
# ──────────────────────────────────────────────────────────────────────────────

def _seed_price_list(conn, name, selling=1, enabled=1):
    pid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO price_list (id, name, currency, selling, buying, enabled) "
        "VALUES (?, ?, 'USD', ?, 0, ?)",
        (pid, f"{name}-{pid[:6]}", selling, enabled))
    conn.commit()
    return pid


def _seed_item_price(conn, item_id, price_list_id, rate, min_qty="0",
                     valid_from=None, valid_to=None):
    ipid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO item_price (id, item_id, price_list_id, rate, min_qty, "
        "valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ipid, item_id, price_list_id, rate, min_qty, valid_from, valid_to))
    conn.commit()
    return ipid


def _set_standard_rate(conn, item_id, rate):
    conn.execute("UPDATE item SET standard_rate=? WHERE id=?", (rate, item_id))
    conn.commit()


def _set_customer_default_list(conn, customer_id, price_list_id):
    conn.execute("UPDATE customer SET default_price_list_id=? WHERE id=?",
                 (price_list_id, customer_id))
    conn.commit()


def _quote(conn, env, items, posting_date="2026-06-15"):
    """Create a quotation and return (result, [line dicts])."""
    result = call_action(mod.add_quotation, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=posting_date, items=json.dumps(items),
        valid_till=None, tax_template_id=None,
    ))
    lines = []
    if is_ok(result):
        rows = conn.execute(
            "SELECT item_id, quantity, rate, net_amount FROM quotation_item "
            "WHERE quotation_id=?", (result["quotation_id"],)).fetchall()
        lines = [dict(r) for r in rows]
    return result, lines


# ──────────────────────────────────────────────────────────────────────────────
# Rate resolution order
# ──────────────────────────────────────────────────────────────────────────────

class TestRateResolution:
    def test_explicit_rate_wins_over_everything(self, conn, env):
        # standard_rate AND a customer-default price list both set high...
        _set_standard_rate(conn, env["item1"], "99.00")
        pl = _seed_price_list(conn, "Default")
        _seed_item_price(conn, env["item1"], pl, "50.00")
        _set_customer_default_list(conn, env["customer"], pl)
        # ...but the line carries an explicit rate, which must win.
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "2", "rate": "7.00"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("7.00")
        assert Decimal(result["total_amount"]) == Decimal("14.00")

    def test_item_price_via_customer_default_list(self, conn, env):
        _set_standard_rate(conn, env["item1"], "99.00")  # should be ignored
        pl = _seed_price_list(conn, "Gold")
        _seed_item_price(conn, env["item1"], pl, "50.00")
        _set_customer_default_list(conn, env["customer"], pl)
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "2"}])  # no rate key
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("50.00")
        assert Decimal(result["total_amount"]) == Decimal("100.00")

    def test_item_price_via_any_selling_list_when_no_default(self, conn, env):
        # No customer default. One enabled selling list has a price.
        pl = _seed_price_list(conn, "Standard", selling=1, enabled=1)
        _seed_item_price(conn, env["item1"], pl, "40.00")
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "3"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("40.00")
        assert Decimal(result["total_amount"]) == Decimal("120.00")

    def test_buying_and_disabled_lists_are_ignored(self, conn, env):
        # A buying-only list and a disabled selling list both carry prices,
        # but neither is a valid selling source -> fall through to standard_rate.
        _set_standard_rate(conn, env["item1"], "12.00")
        buying = _seed_price_list(conn, "Buying", selling=0, enabled=1)
        _seed_item_price(conn, env["item1"], buying, "3.00")
        disabled = _seed_price_list(conn, "OldSelling", selling=1, enabled=0)
        _seed_item_price(conn, env["item1"], disabled, "4.00")
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "1"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("12.00")

    def test_item_price_miss_falls_to_standard_rate(self, conn, env):
        _set_standard_rate(conn, env["item1"], "25.00")
        # item_price row exists but for a DIFFERENT item, so no match here.
        pl = _seed_price_list(conn, "Sell")
        _seed_item_price(conn, env["item2"], pl, "5.00")
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "2"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("25.00")
        assert Decimal(result["total_amount"]) == Decimal("50.00")

    def test_default_list_is_authoritative_no_fallthrough(self, conn, env):
        """QA-ratified branch semantics (2026-07-22): when a customer default price
        list is SET but lacks the item, resolution falls straight to standard_rate —
        it never spills over to other selling lists. This is the boundary that
        distinguishes branch from fallthrough; pin it."""
        _set_standard_rate(conn, env["item1"], "50.00")
        default_pl = _seed_price_list(conn, "Customer Default")
        other_pl = _seed_price_list(conn, "Other Sell")
        # item1 is present ONLY in the OTHER list, at a different rate.
        _seed_item_price(conn, env["item1"], other_pl, "70.00")
        _set_customer_default_list(conn, env["customer"], default_pl)
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "1"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("50.00")  # NOT 70.00

    def test_all_miss_keeps_zero_behavior(self, conn, env):
        # item1 has standard_rate '0' (seed default), no price lists, no default.
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "2"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("0.00")
        assert Decimal(result["total_amount"]) == Decimal("0.00")

    def test_explicit_zero_is_respected(self, conn, env):
        # Both catalog + price list would resolve high, but explicit 0 stands.
        _set_standard_rate(conn, env["item1"], "99.00")
        pl = _seed_price_list(conn, "Default")
        _seed_item_price(conn, env["item1"], pl, "50.00")
        _set_customer_default_list(conn, env["customer"], pl)
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "2", "rate": "0"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("0.00")
        assert Decimal(result["total_amount"]) == Decimal("0.00")

    def test_null_rate_key_resolves(self, conn, env):
        # An explicit JSON null is treated as "absent" and resolves.
        _set_standard_rate(conn, env["item1"], "18.00")
        result, lines = _quote(conn, env, [
            {"item_id": env["item1"], "qty": "1", "rate": None}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("18.00")


# ──────────────────────────────────────────────────────────────────────────────
# min_qty tiering + date windows (mirror get-item-price)
# ──────────────────────────────────────────────────────────────────────────────

class TestQtyAndDateWindows:
    def test_min_qty_tier_selection(self, conn, env):
        pl = _seed_price_list(conn, "Tiered")
        _set_customer_default_list(conn, env["customer"], pl)
        _seed_item_price(conn, env["item1"], pl, "10.00", min_qty="1")
        _seed_item_price(conn, env["item1"], pl, "9.00", min_qty="10")
        _seed_item_price(conn, env["item1"], pl, "8.00", min_qty="100")

        # qty 5 -> highest min_qty <= 5 is the min_qty=1 tier -> 10.00
        _, lines = _quote(conn, env, [{"item_id": env["item1"], "qty": "5"}])
        assert Decimal(lines[0]["rate"]) == Decimal("10.00")

        # qty 50 -> min_qty=10 tier -> 9.00
        _, lines = _quote(conn, env, [{"item_id": env["item1"], "qty": "50"}])
        assert Decimal(lines[0]["rate"]) == Decimal("9.00")

        # qty 150 -> min_qty=100 tier -> 8.00
        _, lines = _quote(conn, env, [{"item_id": env["item1"], "qty": "150"}])
        assert Decimal(lines[0]["rate"]) == Decimal("8.00")

    def test_below_all_min_qty_falls_to_standard_rate(self, conn, env):
        _set_standard_rate(conn, env["item1"], "77.00")
        pl = _seed_price_list(conn, "HighTier")
        _set_customer_default_list(conn, env["customer"], pl)
        _seed_item_price(conn, env["item1"], pl, "5.00", min_qty="100")
        # qty 2 is below the only tier -> no item_price match -> standard_rate
        _, lines = _quote(conn, env, [{"item_id": env["item1"], "qty": "2"}])
        assert Decimal(lines[0]["rate"]) == Decimal("77.00")

    def test_date_window_respected(self, conn, env):
        _set_standard_rate(conn, env["item1"], "77.00")
        pl = _seed_price_list(conn, "Seasonal")
        _set_customer_default_list(conn, env["customer"], pl)
        _seed_item_price(conn, env["item1"], pl, "20.00",
                         valid_from="2026-01-01", valid_to="2026-03-31")

        # Inside the window -> price list rate.
        _, lines = _quote(conn, env,
                          [{"item_id": env["item1"], "qty": "1"}],
                          posting_date="2026-02-15")
        assert Decimal(lines[0]["rate"]) == Decimal("20.00")

        # Outside the window -> no match -> standard_rate.
        _, lines = _quote(conn, env,
                          [{"item_id": env["item1"], "qty": "1"}],
                          posting_date="2026-06-15")
        assert Decimal(lines[0]["rate"]) == Decimal("77.00")


# ──────────────────────────────────────────────────────────────────────────────
# Standalone create-sales-invoice leg
# ──────────────────────────────────────────────────────────────────────────────

class TestStandaloneInvoiceLeg:
    def _invoice(self, conn, env, items, posting_date="2026-06-15"):
        result = call_action(mod.create_sales_invoice, conn, ns(
            company_id=env["company_id"], customer_id=env["customer"],
            tax_template_id=None, sales_order_id=None, delivery_note_id=None,
            posting_date=posting_date, items=json.dumps(items),
            due_date=None, payment_terms_id=None,
        ))
        lines = []
        if is_ok(result):
            rows = conn.execute(
                "SELECT item_id, rate, net_amount FROM sales_invoice_item "
                "WHERE sales_invoice_id=?", (result["sales_invoice_id"],)).fetchall()
            lines = [dict(r) for r in rows]
        return result, lines

    def test_standalone_resolves_standard_rate(self, conn, env):
        _set_standard_rate(conn, env["item1"], "30.00")
        result, lines = self._invoice(conn, env, [
            {"item_id": env["item1"], "qty": "2"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("30.00")
        assert Decimal(result["total_amount"]) == Decimal("60.00")

    def test_standalone_resolves_price_list(self, conn, env):
        pl = _seed_price_list(conn, "InvList")
        _seed_item_price(conn, env["item1"], pl, "45.00")
        _set_customer_default_list(conn, env["customer"], pl)
        result, lines = self._invoice(conn, env, [
            {"item_id": env["item1"], "qty": "2"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("45.00")
        assert Decimal(result["total_amount"]) == Decimal("90.00")

    def test_standalone_explicit_rate_wins(self, conn, env):
        _set_standard_rate(conn, env["item1"], "30.00")
        result, lines = self._invoice(conn, env, [
            {"item_id": env["item1"], "qty": "2", "rate": "11.00"}])
        assert is_ok(result)
        assert Decimal(lines[0]["rate"]) == Decimal("11.00")
        assert Decimal(result["total_amount"]) == Decimal("22.00")


# ──────────────────────────────────────────────────────────────────────────────
# Customer default-price-list wiring
# ──────────────────────────────────────────────────────────────────────────────

class TestCustomerDefaultPriceList:
    def _add(self, conn, env, **extra):
        base = dict(
            name="PL Cust", company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        )
        base.update(extra)
        return call_action(mod.add_customer, conn, ns(**base))

    def test_add_persists_valid_price_list(self, conn, env):
        pl = _seed_price_list(conn, "CustDefault")
        result = self._add(conn, env, default_price_list_id=pl)
        assert is_ok(result)
        row = conn.execute(
            "SELECT default_price_list_id FROM customer WHERE id=?",
            (result["customer_id"],)).fetchone()
        assert row["default_price_list_id"] == pl

    def test_add_rejects_unknown_price_list(self, conn, env):
        result = self._add(conn, env, default_price_list_id="no-such-list")
        assert is_error(result)

    def test_add_without_price_list_is_null(self, conn, env):
        result = self._add(conn, env)
        assert is_ok(result)
        row = conn.execute(
            "SELECT default_price_list_id FROM customer WHERE id=?",
            (result["customer_id"],)).fetchone()
        assert row["default_price_list_id"] is None

    def test_update_sets_price_list(self, conn, env):
        pl = _seed_price_list(conn, "UpdList")
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"], name=None, credit_limit=None,
            payment_terms_id=None, customer_group=None, customer_type=None,
            default_price_list_id=pl,
        ))
        assert is_ok(result)
        assert "default_price_list_id" in result["updated_fields"]
        row = conn.execute(
            "SELECT default_price_list_id FROM customer WHERE id=?",
            (env["customer"],)).fetchone()
        assert row["default_price_list_id"] == pl

    def test_update_rejects_unknown_price_list(self, conn, env):
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"], name=None, credit_limit=None,
            payment_terms_id=None, customer_group=None, customer_type=None,
            default_price_list_id="ghost",
        ))
        assert is_error(result)

    def test_update_empty_string_clears(self, conn, env):
        pl = _seed_price_list(conn, "Temp")
        _set_customer_default_list(conn, env["customer"], pl)
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"], name=None, credit_limit=None,
            payment_terms_id=None, customer_group=None, customer_type=None,
            default_price_list_id="",
        ))
        assert is_ok(result)
        assert "default_price_list_id" in result["updated_fields"]
        row = conn.execute(
            "SELECT default_price_list_id FROM customer WHERE id=?",
            (env["customer"],)).fetchone()
        assert row["default_price_list_id"] is None
