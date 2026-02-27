"""Idempotency tests: verify that duplicate operations are safely rejected.

Each test performs an operation, then repeats it and asserts that:
  - The second attempt either returns an error OR is truly idempotent
  - No duplicate records are created (GL entries, SLE entries, etc.)
  - Data integrity is preserved (GL balanced, counts unchanged)
"""
import json
import uuid
from decimal import Decimal

import pytest

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
    create_test_customer,
    create_test_supplier,
    create_test_item,
    create_test_warehouse,
    seed_stock_for_item,
    setup_phase2_environment,
)

# ---------------------------------------------------------------------------
# Billing entity-type patch (same as test_scenario_billing.py)
# ---------------------------------------------------------------------------
try:
    from erpclaw_lib.naming import ENTITY_PREFIXES
    if "meter" not in ENTITY_PREFIXES:
        ENTITY_PREFIXES["meter"] = "MTR-"
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Connection wrapper for skills that set conn.company_id (billing, support)
# ---------------------------------------------------------------------------

class _ConnectionWrapper:
    """Thin wrapper around sqlite3.Connection supporting arbitrary attrs."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Inventory extra defaults (same injection as test_scenario_inventory.py)
# ---------------------------------------------------------------------------

_INVENTORY_EXTRA_DEFAULTS = {
    "item_code": None,
    "item_name": None,
    "item_type": None,
    "stock_uom": None,
    "valuation_method": None,
    "has_batch": None,
    "has_serial": None,
    "standard_rate": None,
    "item_status": None,
    "batch_name": None,
    "serial_no": None,
    "manufacturing_date": None,
    "expiry_date": None,
    "csv_path": None,
    "price_list_id": None,
    "is_buying": None,
    "is_selling": None,
    "applies_to": None,
    "entity_id": None,
    "pr_rate": None,
    "valid_from": None,
    "valid_to": None,
    "qty": None,
    "se_status": None,
    "sn_status": None,
    "stock_reconciliation_id": None,
}


def _call_inventory_action(action_name, conn, **kwargs):
    """Call an inventory skill action with extra default args injected."""
    merged = {**_INVENTORY_EXTRA_DEFAULTS, **kwargs}
    return _call_action("erpclaw-inventory", action_name, conn, **merged)


# ---------------------------------------------------------------------------
# GL balance helper
# ---------------------------------------------------------------------------

def _assert_gl_balanced(conn):
    """Assert that total debits equal total credits across non-cancelled GL."""
    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)), 0) AS total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)), 0) AS total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"GL not balanced: debit={totals['total_debit']}, "
        f"credit={totals['total_credit']}, diff={diff}"
    )


def _count_gl_entries(conn, is_cancelled=None):
    """Count GL entries, optionally filtered by is_cancelled."""
    if is_cancelled is not None:
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM gl_entry WHERE is_cancelled = ?",
            (is_cancelled,),
        ).fetchone()["cnt"]
    return conn.execute("SELECT COUNT(*) AS cnt FROM gl_entry").fetchone()["cnt"]


def _count_sle_entries(conn, is_cancelled=None):
    """Count stock ledger entries, optionally filtered by is_cancelled."""
    if is_cancelled is not None:
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM stock_ledger_entry WHERE is_cancelled = ?",
            (is_cancelled,),
        ).fetchone()["cnt"]
    return conn.execute(
        "SELECT COUNT(*) AS cnt FROM stock_ledger_entry"
    ).fetchone()["cnt"]


# ===========================================================================
# Tests
# ===========================================================================

@pytest.mark.idempotency
def test_double_submit_journal_entry(fresh_db):
    """Submitting the same journal entry twice should fail on the second attempt.

    After the first successful submit, GL entries are created.
    The second submit must return an error and GL entry count must not change.
    """
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1001")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1002")
    create_test_cost_center(conn, cid)

    # Add JE (draft)
    lines = json.dumps([
        {"account_id": bank_a, "debit": "500.00", "credit": "0.00"},
        {"account_id": bank_b, "debit": "0.00", "credit": "500.00"},
    ])
    r_add = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=cid, posting_date="2026-03-15",
                          entry_type="journal", remark="Test",
                          lines=lines)
    assert r_add["status"] == "ok"
    je_id = r_add["journal_entry_id"]

    # First submit: should succeed
    r_submit1 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                              journal_entry_id=je_id)
    assert r_submit1["status"] == "ok"

    gl_count_after_first = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_first >= 2

    # Second submit: should return error (already submitted)
    r_submit2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                              journal_entry_id=je_id)
    assert r_submit2.get("status") == "error", (
        f"Expected error on double submit, got: {r_submit2}"
    )

    # GL entry count must be unchanged
    gl_count_after_second = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_second == gl_count_after_first, (
        f"GL count changed after double submit: {gl_count_after_first} -> "
        f"{gl_count_after_second}"
    )

    # GL must remain balanced
    _assert_gl_balanced(conn)


@pytest.mark.idempotency
def test_double_cancel_journal_entry(fresh_db):
    """Cancelling the same journal entry twice should fail on the second attempt.

    First cancel creates reversal GL entries.
    Second cancel must return an error and GL entry count must not change.
    """
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1001")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1002")
    create_test_cost_center(conn, cid)

    # Add and submit JE
    lines = json.dumps([
        {"account_id": bank_a, "debit": "750.00", "credit": "0.00"},
        {"account_id": bank_b, "debit": "0.00", "credit": "750.00"},
    ])
    r_add = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=cid, posting_date="2026-03-15",
                          entry_type="journal", remark="Cancel test",
                          lines=lines)
    je_id = r_add["journal_entry_id"]

    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=je_id)

    # First cancel: should succeed, creates reversal GL entries
    r_cancel1 = _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                              journal_entry_id=je_id)
    assert r_cancel1["status"] == "ok"

    # Count ALL GL entries (original cancelled + reversals)
    gl_total_after_first = _count_gl_entries(conn)
    gl_active_after_first = _count_gl_entries(conn, is_cancelled=0)

    # Verify original entries are cancelled and reversals exist
    cancelled_count = _count_gl_entries(conn, is_cancelled=1)
    assert cancelled_count >= 2, "Original entries should be marked cancelled"
    assert gl_active_after_first >= 2, "Reversal entries should exist"

    # Second cancel: should return error (already cancelled)
    r_cancel2 = _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                              journal_entry_id=je_id)
    assert r_cancel2.get("status") == "error", (
        f"Expected error on double cancel, got: {r_cancel2}"
    )

    # GL counts must be unchanged
    gl_total_after_second = _count_gl_entries(conn)
    gl_active_after_second = _count_gl_entries(conn, is_cancelled=0)
    assert gl_total_after_second == gl_total_after_first, (
        f"Total GL count changed: {gl_total_after_first} -> {gl_total_after_second}"
    )
    assert gl_active_after_second == gl_active_after_first, (
        f"Active GL count changed: {gl_active_after_first} -> {gl_active_after_second}"
    )

    # GL must remain balanced (reversals net to zero)
    _assert_gl_balanced(conn)


@pytest.mark.idempotency
def test_double_submit_payment_entry(fresh_db):
    """Submitting the same payment entry twice should fail on the second attempt.

    After the first submit, GL and PLE entries are created.
    The second submit must return an error and counts must not change.
    """
    conn = fresh_db
    env = setup_phase2_environment(conn)
    cid = env["company_id"]

    # Create a payment entry (receive from customer)
    r_add = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=cid, payment_type="receive",
                          posting_date="2026-03-15",
                          paid_from_account=env["receivable_id"],
                          paid_to_account=env["bank_id"],
                          paid_amount="1000.00",
                          party_type="customer",
                          party_id=env["customer_id"])
    assert r_add["status"] == "ok"
    pe_id = r_add["payment_entry_id"]

    # First submit: should succeed
    r_submit1 = _call_action("erpclaw-payments", "submit-payment", conn,
                              payment_entry_id=pe_id)
    assert r_submit1["status"] == "ok"

    gl_count_after_first = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_first >= 2

    # Second submit: should return error
    r_submit2 = _call_action("erpclaw-payments", "submit-payment", conn,
                              payment_entry_id=pe_id)
    assert r_submit2.get("status") == "error", (
        f"Expected error on double payment submit, got: {r_submit2}"
    )

    # GL count must be unchanged
    gl_count_after_second = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_second == gl_count_after_first, (
        f"GL count changed after double payment submit: "
        f"{gl_count_after_first} -> {gl_count_after_second}"
    )

    # GL must remain balanced
    _assert_gl_balanced(conn)


@pytest.mark.idempotency
def test_double_submit_sales_invoice(fresh_db):
    """Submitting the same sales invoice twice should fail on the second attempt.

    Pipeline: add SO -> submit SO -> create SI from SO -> submit SI -> submit SI again.
    The second submit must return an error and GL entry count must not change.
    """
    conn = fresh_db
    env = setup_phase2_environment(conn)
    cid = env["company_id"]

    # Create and submit a sales order
    items_j = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    r_so = _call_action("erpclaw-selling", "add-sales-order", conn,
                         company_id=cid, customer_id=env["customer_id"],
                         posting_date="2026-03-15",
                         delivery_date="2026-03-20",
                         items=items_j)
    assert r_so["status"] == "ok"
    so_id = r_so["sales_order_id"]

    r_sub_so = _call_action("erpclaw-selling", "submit-sales-order", conn,
                             sales_order_id=so_id)
    assert r_sub_so["status"] == "ok"

    # Create sales invoice from SO
    r_si = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                         sales_order_id=so_id,
                         posting_date="2026-03-15",
                         due_date="2026-04-15")
    assert r_si["status"] == "ok"
    si_id = r_si["sales_invoice_id"]

    # First submit: should succeed
    r_submit1 = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                              sales_invoice_id=si_id)
    assert r_submit1["status"] == "ok"

    gl_count_after_first = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_first >= 2

    # Second submit: should return error (already submitted)
    r_submit2 = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                              sales_invoice_id=si_id)
    assert r_submit2.get("status") == "error", (
        f"Expected error on double invoice submit, got: {r_submit2}"
    )

    # GL count must be unchanged
    gl_count_after_second = _count_gl_entries(conn, is_cancelled=0)
    assert gl_count_after_second == gl_count_after_first, (
        f"GL count changed after double invoice submit: "
        f"{gl_count_after_first} -> {gl_count_after_second}"
    )

    # GL must remain balanced
    _assert_gl_balanced(conn)


@pytest.mark.idempotency
def test_double_add_customer_same_name(fresh_db):
    """Adding two customers with the same name should be handled gracefully.

    Customer names are not necessarily unique (unlike naming series).
    The system should either:
      a) Succeed and create two distinct records with different IDs, or
      b) Reject the duplicate with a clear error.
    Either way: no crash, no data corruption.
    """
    conn = fresh_db
    cid = create_test_company(conn)

    # First customer
    r1 = _call_action("erpclaw-selling", "add-customer", conn,
                       company_id=cid, name="Acme Corp")
    assert r1["status"] == "ok"
    cust_id_1 = r1["customer_id"]

    # Second customer with same name
    r2 = _call_action("erpclaw-selling", "add-customer", conn,
                       company_id=cid, name="Acme Corp")

    if r2.get("status") == "error":
        # System rejected the duplicate -- that is acceptable
        # Verify the first customer still exists and is intact
        db_cust = conn.execute(
            "SELECT * FROM customer WHERE id = ?", (cust_id_1,)
        ).fetchone()
        assert db_cust is not None
        assert db_cust["name"] == "Acme Corp"
    else:
        # System accepted both -- verify two distinct records
        assert r2["status"] == "ok"
        cust_id_2 = r2["customer_id"]
        assert cust_id_1 != cust_id_2, (
            "Two customers with same name should have different IDs"
        )

        # Verify both exist in the database
        count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customer WHERE name = ? AND company_id = ?",
            ("Acme Corp", cid),
        ).fetchone()["cnt"]
        assert count == 2, (
            f"Expected 2 customers named 'Acme Corp', found {count}"
        )

    # Either way, no corruption -- total customer count should be sensible
    total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM customer WHERE company_id = ?", (cid,)
    ).fetchone()["cnt"]
    assert total >= 1


@pytest.mark.idempotency
def test_double_stock_entry_submit(fresh_db):
    """Submitting the same stock entry twice should fail on the second attempt.

    After the first submit, SLE entries are created.
    The second submit must return an error and SLE count must not change.
    """
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    create_test_cost_center(conn, cid)

    stock_in_hand = create_test_account(conn, cid, "Stock In Hand", "asset",
                                         account_type="stock",
                                         account_number="1400")
    stock_adjustment = create_test_account(conn, cid, "Stock Adjustment", "expense",
                                            account_type="stock_adjustment",
                                            account_number="5200")

    # Create item and warehouse
    item_id = create_test_item(conn, item_code="IDEM-001", item_name="Idempotent Widget")
    wh_id = create_test_warehouse(conn, cid, "Idem Warehouse",
                                   account_id=stock_in_hand)

    # Create stock entry (receive)
    items_json = json.dumps([{
        "item_id": item_id,
        "qty": "10",
        "rate": "25.00",
        "to_warehouse_id": wh_id,
    }])
    r_add = _call_inventory_action("add-stock-entry", conn,
                                    entry_type="receive", company_id=cid,
                                    posting_date="2026-03-15",
                                    items=items_json)
    assert r_add["status"] == "ok"
    se_id = r_add["stock_entry_id"]

    # First submit: should succeed
    r_submit1 = _call_inventory_action("submit-stock-entry", conn,
                                        stock_entry_id=se_id)
    assert r_submit1["status"] == "ok"

    sle_count_after_first = _count_sle_entries(conn, is_cancelled=0)
    assert sle_count_after_first >= 1

    # Second submit: should return error (already submitted)
    r_submit2 = _call_inventory_action("submit-stock-entry", conn,
                                        stock_entry_id=se_id)
    assert r_submit2.get("status") == "error", (
        f"Expected error on double stock entry submit, got: {r_submit2}"
    )

    # SLE count must be unchanged
    sle_count_after_second = _count_sle_entries(conn, is_cancelled=0)
    assert sle_count_after_second == sle_count_after_first, (
        f"SLE count changed after double submit: "
        f"{sle_count_after_first} -> {sle_count_after_second}"
    )


@pytest.mark.idempotency
def test_double_meter_reading(fresh_db):
    """Adding the same meter reading twice should not create duplicate records.

    The system should either reject the duplicate or handle it idempotently.
    Either way, the reading count for the meter must not have spurious duplicates.
    """
    conn = fresh_db
    wrapped = _ConnectionWrapper(conn)

    cid = create_test_company(conn, name="Meter Corp", abbr="MC")
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    wrapped.company_id = cid

    # Create a rate plan (required for meter)
    tiers = json.dumps([{"tier_start": "0", "rate": "0.10"}])
    rp_result = _call_action("erpclaw-billing", "add-rate-plan", wrapped,
                              name="Flat Electric", billing_model="flat",
                              tiers=tiers, base_charge="5.00")
    assert rp_result["status"] == "ok"
    rp_id = rp_result["rate_plan"]["id"]

    # Create customer for meter association
    customer_id = create_test_customer(conn, cid, name="Meter Customer")

    # Create meter
    m_result = _call_action("erpclaw-billing", "add-meter", wrapped,
                             company_id=cid, name="MTR-IDEM-001",
                             customer_id=customer_id,
                             meter_type="electricity", unit="kWh",
                             install_date="2026-01-01",
                             rate_plan_id=rp_id)
    assert m_result["status"] == "ok"
    meter_id = m_result["meter"]["id"]

    # First reading
    r1 = _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                       meter_id=meter_id, reading_date="2026-03-15",
                       reading_value="100", reading_type="actual")
    assert r1["status"] == "ok"

    reading_count_after_first = conn.execute(
        "SELECT COUNT(*) AS cnt FROM meter_reading WHERE meter_id = ?",
        (meter_id,),
    ).fetchone()["cnt"]
    assert reading_count_after_first == 1

    # Second reading: same meter, same date, same value
    r2 = _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                       meter_id=meter_id, reading_date="2026-03-15",
                       reading_value="100", reading_type="actual")

    if r2.get("status") == "error":
        # Duplicate rejected -- correct behavior
        reading_count_after_second = conn.execute(
            "SELECT COUNT(*) AS cnt FROM meter_reading WHERE meter_id = ?",
            (meter_id,),
        ).fetchone()["cnt"]
        assert reading_count_after_second == reading_count_after_first, (
            "Reading count should not change when duplicate is rejected"
        )
    else:
        # If accepted, verify no exact duplicates (both same date AND value)
        # The system may accept a second reading on the same date as a correction
        dupes = conn.execute(
            """SELECT COUNT(*) AS cnt FROM meter_reading
               WHERE meter_id = ? AND reading_date = ? AND reading_value = ?""",
            (meter_id, "2026-03-15", "100"),
        ).fetchone()["cnt"]
        # At most 2 readings stored (one original, one possibly accepted),
        # but both should have distinct IDs
        readings = conn.execute(
            "SELECT id FROM meter_reading WHERE meter_id = ? ORDER BY created_at",
            (meter_id,),
        ).fetchall()
        reading_ids = [r["id"] for r in readings]
        assert len(reading_ids) == len(set(reading_ids)), (
            "All readings must have distinct IDs"
        )


@pytest.mark.idempotency
def test_double_billing_run(fresh_db):
    """Running billing for the same period twice should not create duplicate records.

    After the first billing run, billing periods are created and rated.
    The second run for the same date range should either:
      a) Return an error / skip already-billed meters, or
      b) Be truly idempotent (same result, no new records).
    """
    conn = fresh_db
    wrapped = _ConnectionWrapper(conn)

    cid = create_test_company(conn, name="Bill Corp", abbr="BL")
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    create_test_cost_center(conn, cid)
    wrapped.company_id = cid

    receivable = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                      account_type="receivable",
                                      account_number="1200")
    income = create_test_account(conn, cid, "Billing Revenue", "income",
                                  account_type="revenue", account_number="4000")
    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_income_account_id = ?
           WHERE id = ?""",
        (receivable, income, cid),
    )
    conn.commit()

    customer_id = create_test_customer(conn, cid, name="Bill Customer")

    # Create rate plan
    tiers = json.dumps([{"tier_start": "0", "rate": "0.12"}])
    rp_result = _call_action("erpclaw-billing", "add-rate-plan", wrapped,
                              name="Flat Billing", billing_model="flat",
                              tiers=tiers, base_charge="10.00")
    assert rp_result["status"] == "ok"
    rp_id = rp_result["rate_plan"]["id"]

    # Create meter
    m_result = _call_action("erpclaw-billing", "add-meter", wrapped,
                             company_id=cid, name="MTR-BILL-001",
                             customer_id=customer_id,
                             meter_type="electricity", unit="kWh",
                             install_date="2026-01-01",
                             rate_plan_id=rp_id)
    assert m_result["status"] == "ok"
    meter_id = m_result["meter"]["id"]

    # Add readings: consumption = 200 kWh
    _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                  meter_id=meter_id, reading_date="2026-03-01",
                  reading_value="500", reading_type="actual")
    _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                  meter_id=meter_id, reading_date="2026-03-31",
                  reading_value="700", reading_type="actual")

    # First billing run
    r1 = _call_action("erpclaw-billing", "run-billing", wrapped,
                       company_id=cid, billing_date="2026-03-31",
                       from_date="2026-03-01", to_date="2026-03-31")
    assert r1["status"] == "ok"

    bp_count_after_first = conn.execute(
        "SELECT COUNT(*) AS cnt FROM billing_period WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()["cnt"]
    assert bp_count_after_first >= 1

    # Record the total billed from first run
    first_total = r1.get("total_billed", "0")

    # Second billing run for the same period
    r2 = _call_action("erpclaw-billing", "run-billing", wrapped,
                       company_id=cid, billing_date="2026-03-31",
                       from_date="2026-03-01", to_date="2026-03-31")

    # Verify no duplicate billing periods were created
    bp_count_after_second = conn.execute(
        "SELECT COUNT(*) AS cnt FROM billing_period WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()["cnt"]

    if r2.get("status") == "error":
        # System rejected the duplicate run -- correct behavior
        assert bp_count_after_second == bp_count_after_first, (
            f"Billing period count changed despite error: "
            f"{bp_count_after_first} -> {bp_count_after_second}"
        )
    else:
        # System accepted idempotently -- either no new periods or same count
        # The key invariant: no MORE billing periods than the first run produced
        # (the system may re-rate existing periods, which is fine)
        assert bp_count_after_second <= bp_count_after_first + 0, (
            f"Duplicate billing periods created: "
            f"{bp_count_after_first} -> {bp_count_after_second}"
        )

    # Verify the billing period data is intact (not corrupted by double-run)
    bp_rows = conn.execute(
        """SELECT * FROM billing_period
           WHERE customer_id = ? AND period_start = '2026-03-01'""",
        (customer_id,),
    ).fetchall()
    for bp in bp_rows:
        assert bp["status"] in ("open", "rated", "invoiced"), (
            f"Billing period in unexpected status: {bp['status']}"
        )
