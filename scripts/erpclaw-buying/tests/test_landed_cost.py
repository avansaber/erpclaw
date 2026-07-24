"""Part A tests for the D1 landed-cost voucher lifecycle (WS1 / ADR-0030).

Actions tested:
  - add-landed-cost-voucher   (GL capitalisation + zero-qty SLE valuation half,
                               FIFO layer rate bump, moving-average variant)
  - list-landed-cost-vouchers, get-landed-cost-voucher
  - cancel-landed-cost-voucher (constitutional GL reversal + negated SLE reprice)
  - INV-24 (stock-account GL ≡ SLE valuation): negative control (a deliberate
    GL-stock post with no SLE delta MUST redden) and green-across-cancel (the
    BDFL's reversal-inclusive collision case).

All money assertions are exact Decimal, never float.
"""
import importlib.util
import json
import os

import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid,
)

mod = load_db_query()

# ── INV-24: import the check straight from testing/invariant_engine.py ──────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", "..", "..", "..", ".."))
# The invariant engine lives in the monorepo test harness (testing/), which is NOT part
# of the published skill tree. Load it defensively so this file collects in the published-
# repo CI (where testing/ is absent) and skips the INV-24 assertions there; the full
# monorepo CI runs them.
_INV_PATH = os.path.join(_REPO_ROOT, "testing", "invariant_engine.py")
if os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_lcv", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _inv24(conn):
    """Run INV-24 directly; None = GREEN, violation string = RED."""
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    return inv_engine._check_inv24_stock_account_gl_matches_ledger(conn)


# ── Local seed / flow helpers ────────────────────────────────────────────────

def _seed_fifo_item(conn, name="FIFO Import Widget"):
    """Seed an item with valuation_method='fifo' (buying seed_item defaults to
    the schema default 'moving_average')."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom,
           is_stock_item, item_type, valuation_method, standard_rate, status)
           VALUES (?, ?, ?, 'Each', 1, 'stock', 'fifo', '0', 'active')""",
        (iid, name, f"FIFO-{iid[:6]}")
    )
    conn.commit()
    return iid


def _submitted_receipt(conn, env, item_id, qty="10", rate="50.00"):
    """PO → confirm → PR → submit; returns the purchase_receipt id."""
    items = json.dumps([{"item_id": item_id, "qty": qty, "rate": rate,
                         "warehouse_id": env["warehouse"]}])
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit_po = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit_po), f"PO submit failed: {submit_po}"
    pr = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po["purchase_order_id"], company_id=env["company_id"],
        posting_date="2026-06-20", items=None, purchase_receipt_id=None,
    ))
    assert is_ok(pr), f"PR creation failed: {pr}"
    submit_pr = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr["purchase_receipt_id"],
    ))
    assert is_ok(submit_pr), f"PR submit failed: {submit_pr}"
    return pr["purchase_receipt_id"]


def _add_lcv(conn, env, pr_ids, charges):
    return call_action(mod.add_landed_cost_voucher, conn, ns(
        purchase_receipt_ids=json.dumps(pr_ids),
        charges=json.dumps(charges),
        company_id=env["company_id"],
    ))


def _freight_100(env):
    return [{"description": "Ocean freight", "amount": "100.00",
             "expense_account_id": env["expense"]}]


def _gl_rows(conn, lcv_id):
    return conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type = 'landed_cost_voucher' "
        "AND voucher_id = ? ORDER BY rowid", (lcv_id,)
    ).fetchall()


def _sle_rows(conn, lcv_id):
    return conn.execute(
        "SELECT * FROM stock_ledger_entry WHERE voucher_type = 'landed_cost_voucher' "
        "AND voucher_id = ? ORDER BY rowid", (lcv_id,)
    ).fetchall()


def _layers(conn, item_id):
    return conn.execute(
        "SELECT * FROM stock_fifo_layer WHERE item_id = ? ORDER BY posting_date, created_at",
        (item_id,)
    ).fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# (1) add-landed-cost-voucher on a submitted PR — FIFO item
# ─────────────────────────────────────────────────────────────────────────────

class TestAddLandedCostVoucherFifo:
    """PR: 10 units @ 50.00 (stock value 500.00). Charge: 100.00 freight.
    Expect GL DR stock 100.00 / CR expense 100.00, one zero-qty SLE row with
    stock_value_difference 100.00 (rate 500→600 over 10 = 60.00), and the
    receipt-sourced FIFO layer bumped 50.00 → 60.00."""

    def _build(self, conn, env):
        fifo_item = _seed_fifo_item(conn)
        pr_id = _submitted_receipt(conn, env, fifo_item)
        result = _add_lcv(conn, env, [pr_id], _freight_100(env))
        assert is_ok(result), f"LCV failed: {result}"
        return fifo_item, pr_id, result

    def test_gl_posts_exact_decimals(self, conn, env):
        fifo_item, pr_id, result = self._build(conn, env)
        assert result["total_landed_cost"] == "100.00"
        assert result["gl_entries_created"] == 2
        assert result["sle_repricings"] == 1

        rows = _gl_rows(conn, result["landed_cost_voucher_id"])
        assert len(rows) == 2
        by_acct = {r["account_id"]: r for r in rows}
        stock_row = by_acct[env["stock_acct"]]
        expense_row = by_acct[env["expense"]]
        assert Decimal(stock_row["debit"]) == Decimal("100.00")
        assert Decimal(stock_row["credit"]) == Decimal("0")
        assert Decimal(expense_row["credit"]) == Decimal("100.00")
        assert Decimal(expense_row["debit"]) == Decimal("0")
        assert stock_row["is_cancelled"] == 0
        assert expense_row["is_cancelled"] == 0
        assert expense_row["cost_center_id"] == env["cc"]

    def test_sle_zero_qty_valuation_row(self, conn, env):
        fifo_item, pr_id, result = self._build(conn, env)
        rows = _sle_rows(conn, result["landed_cost_voucher_id"])
        assert len(rows) == 1
        sle = rows[0]
        assert Decimal(sle["actual_qty"]) == Decimal("0")
        assert Decimal(sle["stock_value_difference"]) == Decimal("100.00")
        assert Decimal(sle["qty_after_transaction"]) == Decimal("10")
        assert Decimal(sle["valuation_rate"]) == Decimal("60.00")
        assert Decimal(sle["stock_value"]) == Decimal("600.00")
        assert sle["item_id"] == fifo_item
        assert sle["warehouse_id"] == env["warehouse"]
        assert sle["is_cancelled"] == 0

    def test_fifo_layer_rate_bumped(self, conn, env):
        fifo_item, pr_id, result = self._build(conn, env)
        layers = _layers(conn, fifo_item)
        assert len(layers) == 1
        layer = layers[0]
        assert layer["source_voucher_type"] == "purchase_receipt"
        assert layer["source_voucher_id"] == pr_id
        # 100.00 spread over 10 received units = +10.00/unit: 50.00 → 60.00
        assert Decimal(layer["rate"]) == Decimal("60.00")
        assert Decimal(layer["qty"]) == Decimal("10")
        assert Decimal(layer["remaining_qty"]) == Decimal("10")

    def test_inv24_green_after_add(self, conn, env):
        self._build(conn, env)
        assert _inv24(conn) is None


# ─────────────────────────────────────────────────────────────────────────────
# (2) moving-average item variant
# ─────────────────────────────────────────────────────────────────────────────

class TestAddLandedCostVoucherMovingAverage:
    """env['item1'] keeps the schema default valuation_method='moving_average'.
    PR: 10 @ 50.00; charge 25.50 → rate 50.00 → 52.55, value 500.00 → 525.50,
    and NO FIFO layers are involved."""

    def _build(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env, [pr_id], [
            {"description": "Customs duty", "amount": "25.50",
             "expense_account_id": env["expense"]},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        return pr_id, result

    def test_sle_delta_and_new_rate(self, conn, env):
        pr_id, result = self._build(conn, env)
        assert result["total_landed_cost"] == "25.50"
        rows = _sle_rows(conn, result["landed_cost_voucher_id"])
        assert len(rows) == 1
        sle = rows[0]
        assert Decimal(sle["actual_qty"]) == Decimal("0")
        assert Decimal(sle["stock_value_difference"]) == Decimal("25.50")
        assert Decimal(sle["valuation_rate"]) == Decimal("52.55")
        assert Decimal(sle["stock_value"]) == Decimal("525.50")

    def test_no_fifo_layers_for_moving_average(self, conn, env):
        pr_id, result = self._build(conn, env)
        assert _layers(conn, env["item1"]) == []

    def test_gl_exact_and_inv24_green(self, conn, env):
        pr_id, result = self._build(conn, env)
        rows = _gl_rows(conn, result["landed_cost_voucher_id"])
        assert len(rows) == 2
        by_acct = {r["account_id"]: r for r in rows}
        assert Decimal(by_acct[env["stock_acct"]]["debit"]) == Decimal("25.50")
        assert Decimal(by_acct[env["expense"]]["credit"]) == Decimal("25.50")
        assert _inv24(conn) is None


# ─────────────────────────────────────────────────────────────────────────────
# Mixed valuation methods across two receipts (value + qty allocation)
# ─────────────────────────────────────────────────────────────────────────────

class TestLandedCostVoucherMixedAllocation:
    def test_value_allocation_across_ma_and_fifo(self, conn, env):
        """MA item 10@50 (500) + FIFO item 5@20 (100); 60.00 charge by value
        → 50.00 / 10.00 split; SLE deltas sum to the charge (INV-24 green)."""
        fifo_item = _seed_fifo_item(conn)
        pr_ma = _submitted_receipt(conn, env, env["item1"], qty="10", rate="50.00")
        pr_fifo = _submitted_receipt(conn, env, fifo_item, qty="5", rate="20.00")

        result = _add_lcv(conn, env, [pr_ma, pr_fifo], [
            {"description": "Freight", "amount": "60.00",
             "expense_account_id": env["expense"], "allocation_method": "value"},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        assert result["sle_repricings"] == 2

        rows = _sle_rows(conn, result["landed_cost_voucher_id"])
        by_item = {r["item_id"]: r for r in rows}
        assert Decimal(by_item[env["item1"]]["stock_value_difference"]) == Decimal("50.00")
        assert Decimal(by_item[fifo_item]["stock_value_difference"]) == Decimal("10.00")
        # FIFO layer: 10.00 over 5 units = +2.00/unit → 22.00
        layers = _layers(conn, fifo_item)
        assert len(layers) == 1
        assert Decimal(layers[0]["rate"]) == Decimal("22.00")
        assert _inv24(conn) is None

    def test_qty_allocation(self, conn, env):
        """Same stock, 30.00 charge by qty: 10/15 → 20.00, 5/15 → 10.00."""
        fifo_item = _seed_fifo_item(conn)
        pr_ma = _submitted_receipt(conn, env, env["item1"], qty="10", rate="50.00")
        pr_fifo = _submitted_receipt(conn, env, fifo_item, qty="5", rate="20.00")

        result = _add_lcv(conn, env, [pr_ma, pr_fifo], [
            {"description": "Handling", "amount": "30.00",
             "expense_account_id": env["expense"], "allocation_method": "qty"},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        rows = _sle_rows(conn, result["landed_cost_voucher_id"])
        by_item = {r["item_id"]: r for r in rows}
        assert Decimal(by_item[env["item1"]]["stock_value_difference"]) == Decimal("20.00")
        assert Decimal(by_item[fifo_item]["stock_value_difference"]) == Decimal("10.00")
        assert _inv24(conn) is None


class TestAddLandedCostVoucherValidation:
    def test_missing_receipts_fails(self, conn, env):
        result = call_action(mod.add_landed_cost_voucher, conn, ns(
            purchase_receipt_ids=None, charges=json.dumps(_freight_100(env)),
            company_id=env["company_id"],
        ))
        assert is_error(result)
        conn.rollback()

    def test_missing_charges_fails(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = call_action(mod.add_landed_cost_voucher, conn, ns(
            purchase_receipt_ids=json.dumps([pr_id]), charges=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)
        conn.rollback()

    def test_unsubmitted_receipt_fails(self, conn, env):
        # Draft PR (created but not submitted) must be rejected.
        items = json.dumps([{"item_id": env["item1"], "qty": "10",
                             "rate": "50.00", "warehouse_id": env["warehouse"]}])
        po = call_action(mod.add_purchase_order, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            tax_template_id=None, name=None,
        ))
        call_action(mod.submit_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"]))
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po["purchase_order_id"], company_id=env["company_id"],
            posting_date="2026-06-20", items=None, purchase_receipt_id=None,
        ))
        result = _add_lcv(conn, env, [pr["purchase_receipt_id"]], _freight_100(env))
        assert is_error(result)
        assert "not found or not submitted" in result.get("error", result.get("message", ""))
        conn.rollback()

    def test_charge_without_expense_account_fails(self, conn, env):
        """INV-24 guard: a charge with no credit-side account would post the SLE
        half without the GL half — must be rejected."""
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env, [pr_id], [
            {"description": "Freight", "amount": "100.00"},
        ])
        assert is_error(result)
        assert "expense_account_id" in result.get("error", result.get("message", ""))
        conn.rollback()

    def test_zero_amount_charge_fails(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env, [pr_id], [
            {"description": "Freight", "amount": "0",
             "expense_account_id": env["expense"]},
        ])
        assert is_error(result)
        conn.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# (3) list / get
# ─────────────────────────────────────────────────────────────────────────────

class TestListLandedCostVouchers:
    def test_list_returns_voucher(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        created = _add_lcv(conn, env, [pr_id], _freight_100(env))
        result = call_action(mod.list_landed_cost_vouchers, conn, ns(
            company_id=env["company_id"], lcv_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 1
        row = result["landed_cost_vouchers"][0]
        assert row["id"] == created["landed_cost_voucher_id"]
        assert row["status"] == "submitted"
        assert Decimal(row["total_landed_cost"]) == Decimal("100.00")

    def test_list_status_filter(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        _add_lcv(conn, env, [pr_id], _freight_100(env))
        submitted = call_action(mod.list_landed_cost_vouchers, conn, ns(
            company_id=env["company_id"], lcv_status="submitted",
            limit=None, offset=None,
        ))
        assert submitted["total_count"] == 1
        cancelled = call_action(mod.list_landed_cost_vouchers, conn, ns(
            company_id=env["company_id"], lcv_status="cancelled",
            limit=None, offset=None,
        ))
        assert cancelled["total_count"] == 0

    def test_list_missing_company_fails(self, conn, env):
        result = call_action(mod.list_landed_cost_vouchers, conn, ns(
            company_id=None, lcv_status=None, limit=None, offset=None,
        ))
        assert is_error(result)


class TestGetLandedCostVoucher:
    def test_get_returns_charges_and_items(self, conn, env):
        pr_id = _submitted_receipt(conn, env, env["item1"])
        created = _add_lcv(conn, env, [pr_id], _freight_100(env))
        result = call_action(mod.get_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id=created["landed_cost_voucher_id"],
        ))
        assert is_ok(result)
        # NB: the ok() envelope overwrites the top-level "status" key with "ok"
        # (module-wide convention for get-* actions); document status is
        # asserted via the DB row.
        row = conn.execute(
            "SELECT status FROM landed_cost_voucher WHERE id = ?",
            (created["landed_cost_voucher_id"],)
        ).fetchone()
        assert row["status"] == "submitted"
        assert Decimal(result["total_landed_cost"]) == Decimal("100.00")

        assert len(result["charges"]) == 1
        charge = result["charges"][0]
        assert charge["description"] == "Ocean freight"
        assert Decimal(charge["amount"]) == Decimal("100.00")
        assert charge["expense_account_id"] == env["expense"]
        assert charge["allocation_method"] == "by_amount"

        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["purchase_receipt_id"] == pr_id
        assert Decimal(item["applicable_charges"]) == Decimal("100.00")
        assert Decimal(item["original_rate"]) == Decimal("50.00")
        assert Decimal(item["final_rate"]) == Decimal("60.00")

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id="no-such-lcv",
        ))
        assert is_error(result)


# ─────────────────────────────────────────────────────────────────────────────
# (4) cancel-landed-cost-voucher
# ─────────────────────────────────────────────────────────────────────────────

class TestCancelLandedCostVoucher:
    def _build_and_cancel(self, conn, env):
        fifo_item = _seed_fifo_item(conn)
        pr_id = _submitted_receipt(conn, env, fifo_item)
        created = _add_lcv(conn, env, [pr_id], _freight_100(env))
        assert is_ok(created)
        cancelled = call_action(mod.cancel_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id=created["landed_cost_voucher_id"],
        ))
        assert is_ok(cancelled), f"Cancel failed: {cancelled}"
        return fifo_item, created["landed_cost_voucher_id"], cancelled

    def test_gl_reversed_via_constitutional_helper(self, conn, env):
        fifo_item, lcv_id, cancelled = self._build_and_cancel(conn, env)
        assert cancelled["gl_reversals"] == 2

        rows = _gl_rows(conn, lcv_id)
        assert len(rows) == 4  # 2 originals + 2 active mirrors
        originals = [r for r in rows if r["is_cancelled"] == 1]
        mirrors = [r for r in rows if r["is_cancelled"] == 0]
        assert len(originals) == 2
        assert len(mirrors) == 2
        # Mirror rows swap debit <-> credit: stock account gets the 100.00 credit.
        stock_mirror = [r for r in mirrors if r["account_id"] == env["stock_acct"]][0]
        assert Decimal(stock_mirror["credit"]) == Decimal("100.00")
        assert Decimal(stock_mirror["debit"]) == Decimal("0")
        expense_mirror = [r for r in mirrors if r["account_id"] == env["expense"]][0]
        assert Decimal(expense_mirror["debit"]) == Decimal("100.00")
        # Reversal-inclusive net over ALL rows is zero on both sides.
        net = sum((Decimal(r["debit"]) - Decimal(r["credit"]) for r in rows),
                  Decimal("0"))
        assert net == Decimal("0")

    def test_sle_delta_reversed_and_layer_restored(self, conn, env):
        fifo_item, lcv_id, cancelled = self._build_and_cancel(conn, env)
        assert cancelled["sle_repricings"] == 1

        rows = _sle_rows(conn, lcv_id)
        assert len(rows) == 2
        deltas = sorted(Decimal(r["stock_value_difference"]) for r in rows)
        assert deltas == [Decimal("-100.00"), Decimal("100.00")]
        for r in rows:
            assert Decimal(r["actual_qty"]) == Decimal("0")
        # The reversal row restores value 600.00 → 500.00 (rate back to 50.00).
        reversal = [r for r in rows
                    if Decimal(r["stock_value_difference"]) == Decimal("-100.00")][0]
        assert Decimal(reversal["stock_value"]) == Decimal("500.00")
        assert Decimal(reversal["valuation_rate"]) == Decimal("50.00")

        layers = _layers(conn, fifo_item)
        assert len(layers) == 1
        assert Decimal(layers[0]["rate"]) == Decimal("50.00")
        assert Decimal(layers[0]["remaining_qty"]) == Decimal("10")

    def test_status_cancelled_and_second_cancel_refused(self, conn, env):
        fifo_item, lcv_id, cancelled = self._build_and_cancel(conn, env)
        row = conn.execute(
            "SELECT status FROM landed_cost_voucher WHERE id = ?", (lcv_id,)
        ).fetchone()
        assert row["status"] == "cancelled"

        second = call_action(mod.cancel_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id=lcv_id,
        ))
        assert is_error(second)
        assert "already cancelled" in second.get("error", second.get("message", ""))
        conn.rollback()

    def test_cancel_nonexistent_fails(self, conn, env):
        result = call_action(mod.cancel_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id="no-such-lcv",
        ))
        assert is_error(result)


# ─────────────────────────────────────────────────────────────────────────────
# (5) ADR-0030 negative control: INV-24 must REDDEN on GL-without-SLE
# ─────────────────────────────────────────────────────────────────────────────

class TestInv24NegativeControl:
    def test_stock_gl_without_sle_delta_reddens(self, conn, env):
        """Seed a deliberate stock-account GL post with NO SLE delta via raw SQL
        and assert INV-24 catches the divergence (the check is not vacuous)."""
        conn.execute(
            """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
               voucher_type, voucher_id, fiscal_year, is_cancelled)
               VALUES (?, '2026-06-20', ?, '25.00', '0', 'journal_entry', ?, ?, 0)""",
            (_uuid(), env["stock_acct"], _uuid(), env["fiscal_year_id"]),
        )
        conn.execute(
            """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
               voucher_type, voucher_id, fiscal_year, is_cancelled)
               VALUES (?, '2026-06-20', ?, '0', '25.00', 'journal_entry', ?, ?, 0)""",
            (_uuid(), env["expense"], _uuid(), env["fiscal_year_id"]),
        )
        conn.commit()

        violation = _inv24(conn)
        assert violation is not None, (
            "INV-24 stayed green on a stock-account GL post with no SLE delta "
            "— the negative control failed (check is vacuous)"
        )
        assert "divergence" in violation

    def test_inv24_registered_in_engine(self, conn, env):
        if inv_engine is None:
            pytest.skip("invariant_engine harness not present (published skill tree)")
        entries = [c for c in inv_engine.INVARIANT_CHECKS if c[0] == "INV-24"]
        assert len(entries) == 1
        assert entries[0][2] is inv_engine._check_inv24_stock_account_gl_matches_ledger


# ─────────────────────────────────────────────────────────────────────────────
# (6) INV-24 GREEN across the full add+cancel cycle (BDFL collision case)
# ─────────────────────────────────────────────────────────────────────────────

class TestInv24GreenAcrossCancel:
    def test_green_at_every_stage_of_lcv_cycle(self, conn, env):
        """The reversal-inclusive all-rows formula must hold at every stage:
        after PR submit, after LCV add, and after LCV cancel."""
        fifo_item = _seed_fifo_item(conn)
        pr_id = _submitted_receipt(conn, env, fifo_item)
        assert _inv24(conn) is None, "RED after PR submit"

        created = _add_lcv(conn, env, [pr_id], _freight_100(env))
        assert is_ok(created)
        assert _inv24(conn) is None, "RED after LCV add"

        cancelled = call_action(mod.cancel_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id=created["landed_cost_voucher_id"],
        ))
        assert is_ok(cancelled)
        assert _inv24(conn) is None, "RED after LCV cancel"

    def test_green_through_asymmetric_pr_cancel_too(self, conn, env):
        """The BDFL's exact collision case: cancel helpers are asymmetric (GL
        reversal rows are ACTIVE, SLE reversal rows are is_cancelled=1 audit
        records). Only reversal-INCLUSIVE netting stays balanced when the
        underlying purchase receipt is cancelled after the LCV cycle."""
        fifo_item = _seed_fifo_item(conn)
        pr_id = _submitted_receipt(conn, env, fifo_item)
        created = _add_lcv(conn, env, [pr_id], _freight_100(env))
        assert is_ok(created)
        cancelled = call_action(mod.cancel_landed_cost_voucher, conn, ns(
            landed_cost_voucher_id=created["landed_cost_voucher_id"],
        ))
        assert is_ok(cancelled)

        pr_cancel = call_action(mod.cancel_purchase_receipt, conn, ns(
            purchase_receipt_id=pr_id,
        ))
        assert is_ok(pr_cancel), f"PR cancel failed: {pr_cancel}"

        # Asymmetry really present: PR SLE reversal rows are audit records...
        sle_flags = conn.execute(
            "SELECT is_cancelled FROM stock_ledger_entry "
            "WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?",
            (pr_id,)
        ).fetchall()
        assert all(r["is_cancelled"] == 1 for r in sle_flags)
        # ...while PR GL reversal rows are active mirrors.
        gl_active = conn.execute(
            "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_type = 'purchase_receipt' "
            "AND voucher_id = ? AND is_cancelled = 0",
            (pr_id,)
        ).fetchone()["n"]
        assert gl_active == 2

        assert _inv24(conn) is None, "RED after asymmetric PR cancel"
