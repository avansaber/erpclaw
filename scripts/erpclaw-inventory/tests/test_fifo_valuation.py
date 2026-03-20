"""Tests for FIFO valuation in stock_posting.py.

Tests that the FIFO valuation method correctly:
- Creates layers on incoming stock
- Consumes oldest layers first on outgoing stock
- Handles partial consumption
- Fully depletes layers
- Calculates weighted rates from consumed layers
- Produces different rates than moving average when prices vary
"""
import json
import pytest
import uuid
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    init_all_tables, get_conn, seed_company, seed_account,
    seed_fiscal_year, seed_cost_center, seed_item, seed_warehouse,
    seed_naming_series, _uuid,
)

mod = load_db_query()


def _seed_fifo_item(conn, name="FIFO Item", standard_rate="0"):
    """Seed an item with valuation_method='fifo'."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom,
           is_stock_item, item_type, valuation_method, standard_rate, status)
           VALUES (?, ?, ?, 'Each', 1, 'stock', 'fifo', ?, 'active')""",
        (iid, name, f"FIFO-{iid[:6]}", standard_rate)
    )
    conn.commit()
    return iid


def _build_fifo_env(conn):
    """Build a test environment with FIFO items."""
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")

    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    stock_acct = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")
    stock_adj = seed_account(conn, cid, "Stock Adjustment", "expense",
                             "stock_adjustment", "5200")
    cogs_acct = seed_account(conn, cid, "COGS", "expense",
                             "cost_of_goods_sold", "5100")
    srnb = seed_account(conn, cid, "SRNB", "liability",
                        "stock_received_not_billed", "2100")

    wh = seed_warehouse(conn, cid, "Main Warehouse", stock_acct)

    conn.execute(
        """UPDATE company SET
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ccid, wh, cid)
    )
    conn.commit()

    fifo_item = _seed_fifo_item(conn, "FIFO Widget", "50.00")
    ma_item = seed_item(conn, "MA Widget", "Each", "stock", "50.00")

    seed_naming_series(conn, cid)

    return {
        "company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
        "cash": cash, "stock_acct": stock_acct, "stock_adj": stock_adj,
        "cogs_acct": cogs_acct, "srnb": srnb,
        "warehouse": wh,
        "fifo_item": fifo_item, "ma_item": ma_item,
    }


def _stock_entry_receive(conn, env, item_id, qty, rate, posting_date="2026-06-15"):
    """Create and submit a stock entry (receive) for testing."""
    items = json.dumps([{
        "item_id": item_id,
        "qty": str(qty),
        "rate": str(rate),
        "to_warehouse_id": env["warehouse"],
    }])
    result = call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=posting_date, items=items,
    ))
    assert is_ok(result), f"Stock entry create failed: {result}"
    se_id = result["stock_entry_id"]
    submit = call_action(mod.submit_stock_entry, conn, ns(
        stock_entry_id=se_id,
    ))
    assert is_ok(submit), f"Stock entry submit failed: {submit}"
    return se_id


def _stock_entry_issue(conn, env, item_id, qty, posting_date="2026-06-15"):
    """Create and submit a stock entry (issue) for testing."""
    items = json.dumps([{
        "item_id": item_id,
        "qty": str(qty),
        "rate": "0",
        "from_warehouse_id": env["warehouse"],
    }])
    result = call_action(mod.add_stock_entry, conn, ns(
        entry_type="issue", company_id=env["company_id"],
        posting_date=posting_date, items=items,
    ))
    assert is_ok(result), f"Stock entry create failed: {result}"
    se_id = result["stock_entry_id"]
    submit = call_action(mod.submit_stock_entry, conn, ns(
        stock_entry_id=se_id,
    ))
    assert is_ok(submit), f"Stock entry submit failed: {submit}"
    return se_id


@pytest.fixture
def fifo_env(conn):
    return _build_fifo_env(conn)


class TestFifoIncomingCreatesLayer:
    def test_fifo_incoming_creates_layer(self, conn, fifo_env):
        """Receiving stock for a FIFO item creates a layer in stock_fifo_layer."""
        env = fifo_env
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "50.00")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ?",
            (env["fifo_item"],)
        ).fetchall()

        assert len(layers) == 1
        layer = layers[0]
        assert Decimal(layer["qty"]) == Decimal("10")
        assert Decimal(layer["rate"]) == Decimal("50.00")
        assert Decimal(layer["remaining_qty"]) == Decimal("10")

    def test_multiple_receives_create_multiple_layers(self, conn, fifo_env):
        """Multiple receives at different rates create separate layers."""
        env = fifo_env
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "50.00",
                             posting_date="2026-06-01")
        _stock_entry_receive(conn, env, env["fifo_item"], 5, "60.00",
                             posting_date="2026-06-10")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ? "
            "ORDER BY posting_date ASC",
            (env["fifo_item"],)
        ).fetchall()

        assert len(layers) == 2
        assert Decimal(layers[0]["rate"]) == Decimal("50.00")
        assert Decimal(layers[1]["rate"]) == Decimal("60.00")

    def test_no_fifo_layer_for_moving_average_item(self, conn, fifo_env):
        """Moving average items should NOT create FIFO layers."""
        env = fifo_env
        _stock_entry_receive(conn, env, env["ma_item"], 10, "50.00")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ?",
            (env["ma_item"],)
        ).fetchall()

        assert len(layers) == 0


class TestFifoOutgoingConsumesOldestFirst:
    def test_fifo_outgoing_consumes_oldest_first(self, conn, fifo_env):
        """Issuing FIFO stock consumes the oldest layer first."""
        env = fifo_env
        # Layer 1: 10 @ $50 (oldest)
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "50.00",
                             posting_date="2026-06-01")
        # Layer 2: 10 @ $80 (newer)
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "80.00",
                             posting_date="2026-06-10")

        # Issue 10 units — should consume entirely from layer 1
        _stock_entry_issue(conn, env, env["fifo_item"], 10,
                           posting_date="2026-06-15")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ? "
            "ORDER BY posting_date ASC",
            (env["fifo_item"],)
        ).fetchall()

        # Layer 1 should be fully consumed
        assert Decimal(layers[0]["remaining_qty"]) == Decimal("0")
        # Layer 2 should be untouched
        assert Decimal(layers[1]["remaining_qty"]) == Decimal("10")


class TestFifoPartialConsumption:
    def test_fifo_partial_consumption(self, conn, fifo_env):
        """Issuing less than a full layer leaves remainder."""
        env = fifo_env
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "50.00",
                             posting_date="2026-06-01")

        # Issue 3 from the 10-unit layer
        _stock_entry_issue(conn, env, env["fifo_item"], 3,
                           posting_date="2026-06-15")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ?",
            (env["fifo_item"],)
        ).fetchall()

        assert len(layers) == 1
        assert Decimal(layers[0]["remaining_qty"]) == Decimal("7")


class TestFifoFullDepletion:
    def test_fifo_full_depletion(self, conn, fifo_env):
        """Issuing all stock depletes all layers to zero."""
        env = fifo_env
        _stock_entry_receive(conn, env, env["fifo_item"], 5, "40.00",
                             posting_date="2026-06-01")
        _stock_entry_receive(conn, env, env["fifo_item"], 5, "60.00",
                             posting_date="2026-06-05")

        # Issue all 10 units
        _stock_entry_issue(conn, env, env["fifo_item"], 10,
                           posting_date="2026-06-15")

        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ? "
            "AND CAST(remaining_qty AS REAL) > 0",
            (env["fifo_item"],)
        ).fetchall()

        assert len(layers) == 0  # All layers fully consumed


class TestFifoRateCalculationFromLayers:
    def test_fifo_rate_calculation_from_layers(self, conn, fifo_env):
        """FIFO outgoing rate = weighted average of consumed layers."""
        env = fifo_env
        # Layer 1: 4 @ $40
        _stock_entry_receive(conn, env, env["fifo_item"], 4, "40.00",
                             posting_date="2026-06-01")
        # Layer 2: 6 @ $60
        _stock_entry_receive(conn, env, env["fifo_item"], 6, "60.00",
                             posting_date="2026-06-05")

        # Issue 7 units: should consume 4 @ $40 + 3 @ $60
        # Weighted rate = (4*40 + 3*60) / 7 = (160 + 180) / 7 = 340/7 = 48.571...
        _stock_entry_issue(conn, env, env["fifo_item"], 7,
                           posting_date="2026-06-15")

        # Check the SLE for the outgoing entry — valuation_rate should reflect FIFO
        sle = conn.execute(
            """SELECT valuation_rate, actual_qty FROM stock_ledger_entry
               WHERE item_id = ? AND is_cancelled = 0
               AND CAST(actual_qty AS REAL) < 0
               ORDER BY rowid DESC LIMIT 1""",
            (env["fifo_item"],)
        ).fetchone()

        fifo_rate = Decimal(sle["valuation_rate"])
        # Expected: (4*40 + 3*60) / 7 = 48.57 (rounded to 2 dp)
        expected = Decimal("48.57")
        assert fifo_rate == expected, f"FIFO rate {fifo_rate} != expected {expected}"

        # Layer 1 should be fully consumed, layer 2 should have 3 remaining
        layers = conn.execute(
            "SELECT * FROM stock_fifo_layer WHERE item_id = ? "
            "ORDER BY posting_date ASC",
            (env["fifo_item"],)
        ).fetchall()
        assert Decimal(layers[0]["remaining_qty"]) == Decimal("0")
        assert Decimal(layers[1]["remaining_qty"]) == Decimal("3")


class TestFifoVsMovingAverageDifferentRates:
    def test_fifo_vs_moving_average_different_rates(self, conn, fifo_env):
        """FIFO and moving average produce different COGS when prices vary.

        Scenario:
        - Buy 10 @ $40, then 10 @ $80
        - Issue 10 units

        Moving average COGS: 10 * $60 = $600 (avg rate = (400+800)/20 = $60)
        FIFO COGS: 10 * $40 = $400 (oldest layer consumed first)
        """
        env = fifo_env

        # --- FIFO item ---
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "40.00",
                             posting_date="2026-06-01")
        _stock_entry_receive(conn, env, env["fifo_item"], 10, "80.00",
                             posting_date="2026-06-10")
        _stock_entry_issue(conn, env, env["fifo_item"], 10,
                           posting_date="2026-06-15")

        fifo_sle = conn.execute(
            """SELECT valuation_rate FROM stock_ledger_entry
               WHERE item_id = ? AND is_cancelled = 0
               AND CAST(actual_qty AS REAL) < 0
               ORDER BY rowid DESC LIMIT 1""",
            (env["fifo_item"],)
        ).fetchone()
        fifo_rate = Decimal(fifo_sle["valuation_rate"])

        # --- Moving average item ---
        _stock_entry_receive(conn, env, env["ma_item"], 10, "40.00",
                             posting_date="2026-06-01")
        _stock_entry_receive(conn, env, env["ma_item"], 10, "80.00",
                             posting_date="2026-06-10")
        _stock_entry_issue(conn, env, env["ma_item"], 10,
                           posting_date="2026-06-15")

        ma_sle = conn.execute(
            """SELECT valuation_rate FROM stock_ledger_entry
               WHERE item_id = ? AND is_cancelled = 0
               AND CAST(actual_qty AS REAL) < 0
               ORDER BY rowid DESC LIMIT 1""",
            (env["ma_item"],)
        ).fetchone()
        ma_rate = Decimal(ma_sle["valuation_rate"])

        # FIFO should use rate from oldest layer: $40
        assert fifo_rate == Decimal("40.00"), f"FIFO rate {fifo_rate} != 40.00"
        # Moving average should be $60
        assert ma_rate == Decimal("60.00"), f"MA rate {ma_rate} != 60.00"
        # They should be different
        assert fifo_rate != ma_rate
