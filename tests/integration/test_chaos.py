"""Chaos testing suite for ERPClaw data integrity.

Tests extreme failure scenarios to verify that the system maintains
data integrity under adverse conditions: mid-transaction failures,
corrupt data, invalid operations, and concurrency conflicts.

All 12 tests marked with @pytest.mark.chaos.
"""
import json
import uuid
import sqlite3
from decimal import Decimal
from unittest.mock import patch, MagicMock

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
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Shared setup helper for journal-entry-centric chaos tests
# ---------------------------------------------------------------------------

def _setup_je_environment(conn):
    """Create company, FY, naming series, accounts, and cost center.

    Returns a dict with all IDs needed for journal entry tests.
    """
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                               account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4001")
    expense = create_test_account(conn, cid, "Rent Expense", "expense",
                                  account_type="expense", account_number="5001")
    retained_earnings = create_test_account(conn, cid, "Retained Earnings", "equity",
                                            account_type="equity", account_number="3001")

    cc = create_test_cost_center(conn, cid)

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cash_id": cash,
        "revenue_id": revenue,
        "expense_id": expense,
        "retained_earnings_id": retained_earnings,
        "cost_center_id": cc,
    }


def _create_and_submit_je(conn, env, debit_acct=None, credit_acct=None,
                          amount="1000.00", posting_date="2026-03-15"):
    """Helper to create and submit a balanced journal entry.

    Uses cash (debit) and revenue (credit) by default.
    Automatically assigns cost_center_id for P&L accounts.
    Returns the journal_entry_id.
    """
    if debit_acct is None:
        debit_acct = env["cash_id"]
    if credit_acct is None:
        credit_acct = env["revenue_id"]

    # Determine which accounts are P&L (income/expense) and need cost center
    pl_accounts = {env["revenue_id"], env["expense_id"]}

    dr_line = {"account_id": debit_acct, "debit": amount, "credit": "0"}
    cr_line = {"account_id": credit_acct, "debit": "0", "credit": amount}

    if debit_acct in pl_accounts:
        dr_line["cost_center_id"] = env["cost_center_id"]
    if credit_acct in pl_accounts:
        cr_line["cost_center_id"] = env["cost_center_id"]

    lines = json.dumps([dr_line, cr_line])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date=posting_date,
                     entry_type="journal",
                     remark="Chaos test JE",
                     lines=lines)
    assert r["status"] != "error", f"Failed to create JE: {r.get('message')}"
    je_id = r["journal_entry_id"]

    r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=je_id)
    assert r2["status"] != "error", f"Failed to submit JE: {r2.get('message')}"

    return je_id


# ===========================================================================
# Chaos Tests
# ===========================================================================


@pytest.mark.chaos
def test_kill_db_mid_gl_posting(fresh_db):
    """CHAOS-01: Unbalanced journal entry must not create partial GL entries.

    Simulates a mid-transaction failure by attempting to submit a journal
    entry with unbalanced lines (total debit != total credit). The validation
    should reject it before any GL entries are written.

    This verifies that the atomic transaction model prevents partial writes:
    if validation fails at any step, gl_entry table remains untouched.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Create a draft JE with balanced lines first (required by add-journal-entry)
    lines = json.dumps([
        {"account_id": env["cash_id"], "debit": "500.00", "credit": "0",
         "cost_center_id": env["cost_center_id"]},
        {"account_id": env["revenue_id"], "debit": "0", "credit": "500.00",
         "cost_center_id": env["cost_center_id"]},
    ])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date="2026-03-15",
                     entry_type="journal",
                     remark="Will corrupt before submit",
                     lines=lines)
    assert r["status"] != "error", f"Failed to add JE: {r.get('message')}"
    je_id = r["journal_entry_id"]

    # Corrupt the journal entry lines: make them unbalanced by direct SQL
    # Change credit on the second line so it does not balance
    conn.execute(
        """UPDATE journal_entry_line SET credit = '999.99'
           WHERE journal_entry_id = ? AND CAST(credit AS REAL) > 0""",
        (je_id,),
    )
    conn.commit()

    # Attempt to submit — should fail validation (debit != credit)
    r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=je_id)
    assert r2["status"] == "error", "Submit should have failed on unbalanced lines"

    # Verify NO GL entries exist — atomic rollback
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, (
        f"Expected 0 GL entries after failed submit, found {gl_count}"
    )


@pytest.mark.chaos
def test_corrupt_gl_hash_chain(fresh_db):
    """CHAOS-02: Tampered GL entry detected by SHA-256 chain integrity check.

    Submits a valid JE to create GL entries with checksums, then directly
    corrupts one entry's posting_date. The check-gl-integrity action should
    detect the broken hash chain.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Submit a valid JE to create GL entries with checksums
    je_id = _create_and_submit_je(conn, env)

    # Verify GL entries were created
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count > 0, "Expected GL entries after submit"

    # Directly corrupt one GL entry's posting_date (simulating tampering)
    first_gl = conn.execute(
        "SELECT id FROM gl_entry ORDER BY rowid LIMIT 1"
    ).fetchone()
    conn.execute(
        "UPDATE gl_entry SET posting_date = '2025-01-01' WHERE id = ?",
        (first_gl["id"],),
    )
    conn.commit()

    # Check GL integrity — should detect the corruption
    r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                     company_id=env["company_id"])
    assert r["status"] == "ok", f"Integrity check failed to run: {r.get('message')}"

    # The chain should NOT be intact (hash mismatch after tampering)
    # Either chain_intact is False or broken_links > 0
    assert not r.get("chain_intact") or r.get("broken_links", 0) > 0, (
        f"Expected chain corruption to be detected. "
        f"chain_intact={r.get('chain_intact')}, broken_links={r.get('broken_links')}"
    )


@pytest.mark.chaos
def test_delete_referenced_account(fresh_db):
    """CHAOS-03: Cannot delete an account referenced by GL entries (FK constraint).

    Creates GL entries referencing an account, then attempts to DELETE that
    account directly via SQL. The foreign key constraint should prevent it.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Submit a JE to create GL entries referencing the cash account
    _create_and_submit_je(conn, env)

    # Verify GL entries reference the cash account
    refs = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE account_id = ?",
        (env["cash_id"],),
    ).fetchone()["cnt"]
    assert refs > 0, "Expected GL entries referencing cash account"

    # Attempt to delete the account directly — FK should prevent it
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM account WHERE id = ?", (env["cash_id"],))


@pytest.mark.chaos
def test_submit_to_closed_fiscal_year(fresh_db):
    """CHAOS-04: Cannot submit a JE to a closed fiscal year.

    Closes the fiscal year, then attempts to submit a JE with a posting_date
    inside the closed period. The system should reject the submission.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # First submit a JE so there is P&L activity to close
    _create_and_submit_je(conn, env, posting_date="2026-06-15")

    # Close the fiscal year
    r_close = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                           fiscal_year_id=env["fy_id"],
                           closing_account_id=env["retained_earnings_id"],
                           posting_date="2026-12-31")
    assert r_close["status"] != "error", (
        f"Failed to close FY: {r_close.get('message')}"
    )

    # Count GL entries after closing
    gl_before = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]

    # Now try to submit a new JE in the closed fiscal year
    lines = json.dumps([
        {"account_id": env["cash_id"], "debit": "200.00", "credit": "0"},
        {"account_id": env["revenue_id"], "debit": "0", "credit": "200.00",
         "cost_center_id": env["cost_center_id"]},
    ])
    r_add = _call_action("erpclaw-journals", "add-journal-entry", conn,
                         company_id=env["company_id"],
                         posting_date="2026-06-15",
                         entry_type="journal",
                         remark="Post to closed FY",
                         lines=lines)

    if r_add["status"] == "error":
        # Rejected at creation — acceptable behavior
        gl_after = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
        assert gl_after == gl_before, "GL entries should not change on rejected add"
    else:
        # Created as draft — submit should be rejected
        je_id = r_add["journal_entry_id"]
        r_submit = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                                journal_entry_id=je_id)
        assert r_submit["status"] == "error", (
            "Submit to closed fiscal year should have been rejected"
        )
        # GL entries should not have increased
        gl_after = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
        assert gl_after == gl_before, (
            f"GL entries changed after rejected submit to closed FY: "
            f"before={gl_before}, after={gl_after}"
        )


@pytest.mark.chaos
def test_submit_without_cost_center(fresh_db):
    """CHAOS-05: P&L accounts require cost_center_id — rejected without it.

    Creates a JE with income/expense lines but deliberately omits the
    cost_center_id. GL validation step 6 should reject the submission.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Create JE with P&L accounts but NO cost center
    lines = json.dumps([
        {"account_id": env["expense_id"], "debit": "750.00", "credit": "0"},
        {"account_id": env["revenue_id"], "debit": "0", "credit": "750.00"},
    ])
    # Note: both accounts are P&L (expense and income), neither has cost_center_id

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date="2026-04-15",
                     entry_type="journal",
                     remark="Missing cost center",
                     lines=lines)
    assert r["status"] != "error", f"Failed to create draft JE: {r.get('message')}"
    je_id = r["journal_entry_id"]

    # Submit should fail — GL validation step 6 requires cost_center for P&L
    r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=je_id)
    assert r2["status"] == "error", (
        "Submit should fail without cost_center on P&L accounts"
    )
    assert "cost_center" in r2.get("message", "").lower() or "step 6" in r2.get("message", "").lower(), (
        f"Error should mention cost_center, got: {r2.get('message')}"
    )

    # Verify GL is empty — no partial entries written
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, (
        f"Expected 0 GL entries after rejected submit, found {gl_count}"
    )


@pytest.mark.chaos
def test_cross_company_account(fresh_db):
    """CHAOS-06: Cannot use an account from Company B in Company A's JE.

    Creates two companies with separate accounts, then tries to submit a JE
    for Company A using an account from Company B. GL validation step 3
    (account-company affinity) should reject it.
    """
    conn = fresh_db

    # Company A
    cid_a = create_test_company(conn, name="Company A", abbr="CA")
    create_test_fiscal_year(conn, cid_a, name="FY-A 2026")
    seed_naming_series(conn, cid_a)
    cash_a = create_test_account(conn, cid_a, "Cash A", "asset",
                                 account_type="bank", account_number="1001")

    # Company B
    cid_b = create_test_company(conn, name="Company B", abbr="CB")
    create_test_fiscal_year(conn, cid_b, name="FY-B 2026")
    revenue_b = create_test_account(conn, cid_b, "Revenue B", "income",
                                    account_type="revenue", account_number="4002")

    cc_a = create_test_cost_center(conn, cid_a)

    # Create a JE for Company A using Company B's revenue account
    lines = json.dumps([
        {"account_id": cash_a, "debit": "500.00", "credit": "0"},
        {"account_id": revenue_b, "debit": "0", "credit": "500.00",
         "cost_center_id": cc_a},
    ])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=cid_a,
                     posting_date="2026-05-15",
                     entry_type="journal",
                     remark="Cross-company abuse",
                     lines=lines)
    assert r["status"] != "error", f"Failed to create draft: {r.get('message')}"
    je_id = r["journal_entry_id"]

    # Submit should fail — GL validation step 3 (company affinity)
    r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=je_id)
    assert r2["status"] == "error", (
        "Submit should fail when using cross-company account"
    )
    assert "company" in r2.get("message", "").lower() or "step 3" in r2.get("message", "").lower(), (
        f"Error should mention company mismatch, got: {r2.get('message')}"
    )

    # Verify GL is empty
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, (
        f"Expected 0 GL entries after cross-company rejection, found {gl_count}"
    )


@pytest.mark.chaos
def test_double_submit_journal_entry(fresh_db):
    """CHAOS-07: Submitting an already-submitted JE is rejected.

    Submits a JE successfully, counts GL entries, then tries to submit
    the same JE again. The second submit should be rejected and GL count
    should not change.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Create and submit a JE
    je_id = _create_and_submit_je(conn, env)

    # Count GL entries after first submit
    gl_count_after_first = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry"
    ).fetchone()["cnt"]
    assert gl_count_after_first > 0, "Expected GL entries after submit"

    # Try to submit again
    r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                     journal_entry_id=je_id)
    assert r["status"] == "error", "Double submit should be rejected"
    assert "draft" in r.get("message", "").lower() or "submitted" in r.get("message", "").lower(), (
        f"Error should mention status constraint, got: {r.get('message')}"
    )

    # GL count should not change
    gl_count_after_second = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry"
    ).fetchone()["cnt"]
    assert gl_count_after_second == gl_count_after_first, (
        f"GL entries changed after double submit: "
        f"{gl_count_after_first} -> {gl_count_after_second}"
    )


@pytest.mark.chaos
def test_cancel_already_cancelled(fresh_db):
    """CHAOS-08: Cancelling an already-cancelled JE is rejected.

    Submits and cancels a JE (creates reversal GL entries), then tries to
    cancel it again. The second cancellation should be rejected and GL count
    should not change.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Create, submit, then cancel a JE
    je_id = _create_and_submit_je(conn, env)

    r_cancel = _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                            journal_entry_id=je_id)
    assert r_cancel["status"] != "error", (
        f"First cancel failed: {r_cancel.get('message')}"
    )

    # Count total GL entries (originals + reversals)
    gl_count_after_cancel = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry"
    ).fetchone()["cnt"]

    # Try to cancel again
    r_cancel2 = _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                             journal_entry_id=je_id)
    assert r_cancel2["status"] == "error", (
        "Second cancel should be rejected"
    )
    assert "cancelled" in r_cancel2.get("message", "").lower() or "submitted" in r_cancel2.get("message", "").lower(), (
        f"Error should mention status constraint, got: {r_cancel2.get('message')}"
    )

    # GL count should not change
    gl_count_after_double = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry"
    ).fetchone()["cnt"]
    assert gl_count_after_double == gl_count_after_cancel, (
        f"GL entries changed after double cancel: "
        f"{gl_count_after_cancel} -> {gl_count_after_double}"
    )


@pytest.mark.chaos
def test_submit_with_zero_amounts(fresh_db):
    """CHAOS-09: JE lines with all-zero amounts are rejected.

    Creates a JE where both debit and credit are "0.00" on every line.
    The validation should reject this since every line must have at least
    one non-zero amount.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Lines with zero amounts on both sides
    lines = json.dumps([
        {"account_id": env["cash_id"], "debit": "0.00", "credit": "0.00"},
        {"account_id": env["revenue_id"], "debit": "0.00", "credit": "0.00",
         "cost_center_id": env["cost_center_id"]},
    ])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date="2026-03-15",
                     entry_type="journal",
                     remark="Zero amounts",
                     lines=lines)

    # Should be rejected at creation (validation rejects zero amounts)
    assert r["status"] == "error", (
        "JE with all-zero amounts should be rejected"
    )
    assert "debit" in r.get("message", "").lower() or "credit" in r.get("message", "").lower() or "0" in r.get("message", ""), (
        f"Error should mention zero amounts, got: {r.get('message')}"
    )

    # Verify no GL entries
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, f"Expected 0 GL entries, found {gl_count}"


@pytest.mark.chaos
def test_submit_with_negative_amounts(fresh_db):
    """CHAOS-10: JE lines with negative debit amounts are rejected.

    Creates a JE with a negative debit value. The line-level validation
    should reject this since amounts must be >= 0.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Lines with negative debit
    lines = json.dumps([
        {"account_id": env["cash_id"], "debit": "-500.00", "credit": "0"},
        {"account_id": env["revenue_id"], "debit": "0", "credit": "-500.00",
         "cost_center_id": env["cost_center_id"]},
    ])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date="2026-03-15",
                     entry_type="journal",
                     remark="Negative amounts",
                     lines=lines)

    # Should be rejected at creation
    assert r["status"] == "error", (
        "JE with negative amounts should be rejected"
    )

    # Verify no GL entries
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, f"Expected 0 GL entries, found {gl_count}"


@pytest.mark.chaos
def test_rollback_on_unbalanced_entry(fresh_db):
    """CHAOS-11: Unbalanced JE (debit != credit) is rejected with no GL residue.

    Creates a JE with deliberately unbalanced lines. The add-journal-entry
    action validates balance before even creating the draft, so it should
    be rejected outright with zero GL entries.
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Unbalanced: debit 1000, credit 500
    lines = json.dumps([
        {"account_id": env["cash_id"], "debit": "1000.00", "credit": "0"},
        {"account_id": env["revenue_id"], "debit": "0", "credit": "500.00",
         "cost_center_id": env["cost_center_id"]},
    ])

    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=env["company_id"],
                     posting_date="2026-03-15",
                     entry_type="journal",
                     remark="Unbalanced entry",
                     lines=lines)

    # Should be rejected at creation
    assert r["status"] == "error", (
        "Unbalanced JE should be rejected"
    )
    assert "debit" in r.get("message", "").lower() or "credit" in r.get("message", "").lower() or "balance" in r.get("message", "").lower(), (
        f"Error should mention balance mismatch, got: {r.get('message')}"
    )

    # Verify GL is completely empty
    gl_count = conn.execute("SELECT COUNT(*) as cnt FROM gl_entry").fetchone()["cnt"]
    assert gl_count == 0, (
        f"Expected 0 GL entries after unbalanced rejection, found {gl_count}"
    )

    # Also verify no journal_entry was created
    je_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry"
    ).fetchone()["cnt"]
    assert je_count == 0, (
        f"Expected 0 journal entries after unbalanced rejection, found {je_count}"
    )


@pytest.mark.chaos
def test_db_locked_during_write(fresh_db):
    """CHAOS-12: Concurrent database access under WAL mode.

    Opens a second connection to the same database file, starts a write
    transaction on it (without committing), then verifies the primary
    connection can still read/write thanks to WAL mode. WAL allows
    concurrent readers and a single writer, so the primary connection
    should succeed or get a clean busy/locked error (not corruption).
    """
    conn = fresh_db
    env = _setup_je_environment(conn)

    # Get the database file path from the primary connection
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    # Open a second connection with WAL mode
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    conn2.execute("PRAGMA journal_mode=WAL")
    conn2.execute("PRAGMA foreign_keys=ON")
    conn2.execute("PRAGMA busy_timeout=5000")

    try:
        # Start a write transaction on conn2 (hold the write lock)
        conn2.execute("BEGIN IMMEDIATE")
        conn2.execute(
            "INSERT INTO audit_log (id, skill, action, entity_type, entity_id) "
            "VALUES (?, 'test', 'chaos', 'test', 'test')",
            (str(uuid.uuid4()),),
        )
        # Intentionally do NOT commit — conn2 holds the write lock

        # Now try to submit a JE on the primary connection
        # WAL mode should allow this to either succeed (WAL concurrent writes
        # are serialized via busy_timeout) or fail cleanly
        lines = json.dumps([
            {"account_id": env["cash_id"], "debit": "300.00", "credit": "0"},
            {"account_id": env["revenue_id"], "debit": "0", "credit": "300.00",
             "cost_center_id": env["cost_center_id"]},
        ])

        try:
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                             company_id=env["company_id"],
                             posting_date="2026-03-15",
                             entry_type="journal",
                             remark="Concurrent write test",
                             lines=lines)
        except (sqlite3.OperationalError, json.JSONDecodeError) as e:
            # OperationalError ("database is locked") is expected when
            # another connection holds a write lock. This is clean failure.
            assert "locked" in str(e).lower() or "busy" in str(e).lower() or "json" in type(e).__name__.lower(), (
                f"Expected locked/busy error or empty output, got: {e}"
            )
            r = None

        # Under WAL with busy_timeout=5000, this could succeed (if conn2's
        # transaction finishes or WAL allows it) or fail with a locked error.
        # Either outcome is acceptable — what matters is NO corruption.
        if r is None:
            pass  # Already validated above as a clean OperationalError
        elif r["status"] == "error":
            # Clean error is acceptable — should mention locked/busy
            assert "locked" in r.get("message", "").lower() or "busy" in r.get("message", "").lower() or "database" in r.get("message", "").lower(), (
                f"Expected clean locked/busy error, got: {r.get('message')}"
            )
        else:
            # Success is also valid under WAL — verify data integrity
            assert "journal_entry_id" in r, "Expected journal_entry_id in response"

        # Verify database is not corrupted: can still query
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM company"
        ).fetchone()["cnt"]
        assert count >= 1, "Database should still be queryable after concurrency test"

    finally:
        # Clean up: rollback conn2's uncommitted transaction and close
        conn2.rollback()
        conn2.close()
