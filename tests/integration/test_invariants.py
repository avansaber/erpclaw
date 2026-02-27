"""Invariant validation tests (INV-01 through INV-07).

These invariants must ALWAYS hold after any sequence of operations.
Each test builds up a complex scenario, then asserts the invariant.
"""
import json
import re
from decimal import Decimal, InvalidOperation

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    create_test_customer,
    create_test_supplier,
    seed_naming_series,
)


def _build_complex_scenario(conn):
    """Build a scenario with multiple JEs, payments, and cancellations.

    Returns a dict with all created IDs for further inspection.
    """
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    expense = create_test_account(conn, cid, "Rent", "expense",
                                   account_type="expense", account_number="5001")
    receivable = create_test_account(conn, cid, "AR", "asset",
                                      account_type="receivable", account_number="1200")
    payable = create_test_account(conn, cid, "AP", "liability",
                                   account_type="payable", account_number="2100")

    cc = create_test_cost_center(conn, cid)
    customer_id = create_test_customer(conn, cid)
    supplier_id = create_test_supplier(conn, cid)

    # P&L accounts that need cost_center_id
    pl_accounts = {revenue, expense}

    je_ids = []
    pe_ids = []

    # Submit 4 journal entries
    for i, (dr_acct, cr_acct, amt) in enumerate([
        (cash, revenue, "5000.00"),
        (expense, cash, "2000.00"),
        (cash, revenue, "3000.00"),
        (expense, cash, "1500.00"),
    ]):
        dr_line = {"account_id": dr_acct, "debit": amt, "credit": "0"}
        cr_line = {"account_id": cr_acct, "debit": "0", "credit": amt}
        if dr_acct in pl_accounts:
            dr_line["cost_center_id"] = cc
        if cr_acct in pl_accounts:
            cr_line["cost_center_id"] = cc
        lines = json.dumps([dr_line, cr_line])
        r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=cid, posting_date=f"2026-0{i+1}-15", lines=lines)
        _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=r["journal_entry_id"])
        je_ids.append(r["journal_entry_id"])

    # Submit 2 payments
    for i, (ptype, from_a, to_a, party_t, party, amt) in enumerate([
        ("receive", receivable, cash, "customer", customer_id, "2500.00"),
        ("pay", cash, payable, "supplier", supplier_id, "1000.00"),
    ]):
        r = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=cid, payment_type=ptype,
                          posting_date=f"2026-0{i+5}-15",
                          party_type=party_t, party_id=party,
                          paid_from_account=from_a,
                          paid_to_account=to_a,
                          paid_amount=amt)
        _call_action("erpclaw-payments", "submit-payment", conn,
                      payment_entry_id=r["payment_entry_id"])
        pe_ids.append(r["payment_entry_id"])

    # Cancel 1 JE and 1 payment
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_ids[1])
    _call_action("erpclaw-payments", "cancel-payment", conn,
                  payment_entry_id=pe_ids[0])

    return {
        "company_id": cid,
        "fiscal_year_id": fy_id,
        "accounts": {"cash": cash, "revenue": revenue, "expense": expense,
                      "receivable": receivable, "payable": payable},
        "cost_center_id": cc,
        "je_ids": je_ids,
        "pe_ids": pe_ids,
        "customer_id": customer_id,
        "supplier_id": supplier_id,
    }


# ---------------------------------------------------------------------------
# INV-01: Global double-entry balance
# ---------------------------------------------------------------------------

def test_INV01_global_double_entry_balance(fresh_db):
    """SUM(debit) = SUM(credit) across all non-cancelled gl_entry rows."""
    conn = fresh_db
    _build_complex_scenario(conn)

    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)),0) as total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)),0) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()

    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"Global GL not balanced: debit={totals['total_debit']}, "
        f"credit={totals['total_credit']}, diff={diff}"
    )


# ---------------------------------------------------------------------------
# INV-02: Per-voucher balance
# ---------------------------------------------------------------------------

def test_INV02_per_voucher_balance(fresh_db):
    """For every voucher_id, SUM(debit) = SUM(credit) among non-cancelled entries."""
    conn = fresh_db
    _build_complex_scenario(conn)

    vouchers = conn.execute(
        """SELECT voucher_type, voucher_id,
                  SUM(CAST(debit AS REAL)) as total_debit,
                  SUM(CAST(credit AS REAL)) as total_credit
           FROM gl_entry WHERE is_cancelled = 0
           GROUP BY voucher_type, voucher_id"""
    ).fetchall()

    for v in vouchers:
        diff = abs(v["total_debit"] - v["total_credit"])
        assert diff < 0.01, (
            f"Voucher {v['voucher_type']}:{v['voucher_id']} not balanced: "
            f"debit={v['total_debit']}, credit={v['total_credit']}"
        )


# ---------------------------------------------------------------------------
# INV-03: GL immutability — no gl_entry row should have updated_at
# ---------------------------------------------------------------------------

def test_INV03_gl_immutability(fresh_db):
    """GL entries are immutable — no row should have an updated_at value."""
    conn = fresh_db
    _build_complex_scenario(conn)

    # The gl_entry table should NOT have an updated_at column at all
    # (per convention: cancel = reverse, not update)
    cols = conn.execute("PRAGMA table_info(gl_entry)").fetchall()
    col_names = [c["name"] for c in cols]

    if "updated_at" in col_names:
        # If column exists, no row should have it set to non-NULL
        updated = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE updated_at IS NOT NULL"
        ).fetchone()["cnt"]
        assert updated == 0, f"{updated} GL entries have updated_at set"


# ---------------------------------------------------------------------------
# INV-04: Cancellation symmetry
# ---------------------------------------------------------------------------

def test_INV04_cancellation_symmetry(fresh_db):
    """Every cancelled entry has a matching reversal entry."""
    conn = fresh_db
    _build_complex_scenario(conn)

    # Get all cancelled entries grouped by voucher
    cancelled_vouchers = conn.execute(
        """SELECT voucher_type, voucher_id,
                  SUM(CAST(debit AS REAL)) as c_debit,
                  SUM(CAST(credit AS REAL)) as c_credit,
                  COUNT(*) as cnt
           FROM gl_entry WHERE is_cancelled = 1
           GROUP BY voucher_type, voucher_id"""
    ).fetchall()

    for cv in cancelled_vouchers:
        # There should be non-cancelled entries for the same voucher (reversals)
        reversals = conn.execute(
            """SELECT SUM(CAST(debit AS REAL)) as r_debit,
                      SUM(CAST(credit AS REAL)) as r_credit,
                      COUNT(*) as cnt
               FROM gl_entry WHERE voucher_type = ? AND voucher_id = ?
                 AND is_cancelled = 0""",
            (cv["voucher_type"], cv["voucher_id"]),
        ).fetchone()

        # Reversal entries should exist
        assert reversals["cnt"] > 0, (
            f"No reversals for cancelled {cv['voucher_type']}:{cv['voucher_id']}"
        )

        # The reversal entries should be balanced on their own
        r_diff = abs(reversals["r_debit"] - reversals["r_credit"])
        assert r_diff < 0.01, (
            f"Reversals for {cv['voucher_type']}:{cv['voucher_id']} not balanced"
        )


# ---------------------------------------------------------------------------
# INV-05: No orphaned GL entries
# ---------------------------------------------------------------------------

def test_INV05_no_orphaned_gl_entries(fresh_db):
    """Every gl_entry.voucher_id maps to a document that exists."""
    conn = fresh_db
    _build_complex_scenario(conn)

    # Check that all voucher_types map to known tables
    valid_types = {
        "journal_entry": "journal_entry",
        "payment_entry": "payment_entry",
        "period_closing": "period_closing_voucher",
    }

    # Get distinct voucher references
    vouchers = conn.execute(
        "SELECT DISTINCT voucher_type, voucher_id FROM gl_entry"
    ).fetchall()

    for v in vouchers:
        vtype = v["voucher_type"]
        vid = v["voucher_id"]

        if vtype in valid_types:
            table = valid_types[vtype]
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (vid,)
            ).fetchone()
            assert exists is not None, (
                f"Orphaned GL entry: {vtype}:{vid} not found in {table}"
            )


# ---------------------------------------------------------------------------
# INV-06: Naming series monotonic
# ---------------------------------------------------------------------------

def test_INV06_naming_series_monotonic(fresh_db):
    """Naming series values are strictly increasing per entity type."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    cc = create_test_cost_center(conn, cid)

    # Create multiple JEs and extract sequence numbers
    je_sequences = []
    for i in range(5):
        lines = json.dumps([
            {"account_id": cash, "debit": "100.00", "credit": "0"},
            {"account_id": revenue, "debit": "0", "credit": "100.00", "cost_center_id": cc},
        ])
        r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=cid, posting_date="2026-06-15", lines=lines)
        naming = r["naming_series"]
        # Extract sequence number from e.g., "JV-2026-00001"
        seq = int(naming.rsplit("-", 1)[-1])
        je_sequences.append(seq)

    # Verify strictly increasing
    for i in range(1, len(je_sequences)):
        assert je_sequences[i] > je_sequences[i-1], (
            f"Naming series not monotonic: {je_sequences[i-1]} >= {je_sequences[i]}"
        )


# ---------------------------------------------------------------------------
# INV-07: Decimal precision — no float artifacts
# ---------------------------------------------------------------------------

def test_INV07_decimal_precision(fresh_db):
    """No float artifacts in any GL amount field."""
    conn = fresh_db
    _build_complex_scenario(conn)

    # Check every debit and credit value is a clean decimal string
    entries = conn.execute(
        "SELECT id, debit, credit FROM gl_entry"
    ).fetchall()

    float_artifact_pattern = re.compile(r"\d+\.\d{3,}")  # More than 2 decimal places

    for e in entries:
        for field in ("debit", "credit"):
            val = e[field]
            if val is None or val == "0":
                continue
            # Should be parseable as Decimal
            try:
                d = Decimal(val)
            except InvalidOperation:
                raise AssertionError(f"GL entry {e['id']} has non-decimal {field}: {val}")
            # Check for float artifacts (e.g., 1000.0000000000001)
            if "." in val:
                decimal_places = len(val.split(".")[1])
                assert decimal_places <= 2, (
                    f"GL entry {e['id']} has {decimal_places} decimal places in {field}: {val}"
                )
