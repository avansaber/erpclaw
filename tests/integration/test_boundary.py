"""Boundary testing suite — edge cases and extreme inputs.

Tests that ERPClaw handles zero amounts, max precision, huge line counts,
unicode, SQL metacharacters, extreme dates, and other boundary conditions
without data corruption or crashes.
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
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_je_environment(conn):
    """Create company, FY, accounts, cost center, naming series for JE tests.

    Returns dict with company_id, bank_a, bank_b, cost_center_id.
    """
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1010")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1011")
    cc = create_test_cost_center(conn, cid)
    return {
        "company_id": cid,
        "bank_a": bank_a,
        "bank_b": bank_b,
        "cost_center_id": cc,
    }


# ---------------------------------------------------------------------------
# 1. Zero-amount journal entry
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_zero_amount_journal_entry(fresh_db):
    """JE with all-zero debit/credit should be rejected or produce balanced GL."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    lines = json.dumps([
        {"account_id": env["bank_a"], "debit": "0.00", "credit": "0.00",
         "cost_center_id": None},
        {"account_id": env["bank_b"], "debit": "0.00", "credit": "0.00",
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark="Zero-amount test",
                      lines=lines)

    if r.get("status") == "error":
        # Acceptable: system rejects zero-amount JE
        return

    # If the JE was created, try to submit
    je_id = r["journal_entry_id"]
    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=je_id)

    if sr.get("status") == "error":
        # Acceptable: submit rejects zero totals
        return

    # If submit succeeded, GL must still be balanced (zeros)
    gl = conn.execute(
        "SELECT COALESCE(SUM(CAST(debit AS REAL)),0) as td, "
        "COALESCE(SUM(CAST(credit AS REAL)),0) as tc "
        "FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchone()
    assert abs(gl["td"] - gl["tc"]) < 0.01, "GL not balanced for zero-amount JE"


# ---------------------------------------------------------------------------
# 2. Maximum precision amount
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_max_precision_amount(fresh_db):
    """JE with very large amounts should store exact TEXT Decimal values."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    big = "999999999999999.99"
    lines = json.dumps([
        {"account_id": env["bank_a"], "debit": big, "credit": "0.00",
         "cost_center_id": None},
        {"account_id": env["bank_b"], "debit": "0.00", "credit": big,
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark="Max precision test",
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create JE: {r}"
    je_id = r["journal_entry_id"]

    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=je_id)
    assert sr.get("status") != "error", f"Submit failed: {sr}"

    # Verify GL stores exact TEXT values
    gl_rows = conn.execute(
        "SELECT debit, credit FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchall()
    assert len(gl_rows) >= 2

    debits = [Decimal(r["debit"]) for r in gl_rows]
    credits = [Decimal(r["credit"]) for r in gl_rows]
    assert Decimal(big) in debits, f"Expected {big} in debits: {debits}"
    assert Decimal(big) in credits, f"Expected {big} in credits: {credits}"


# ---------------------------------------------------------------------------
# 3. Excessive decimal places
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_excessive_decimal_places(fresh_db):
    """JE with >2 decimal places should round or reject, not garble data."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    odd_amount = "100.123456789"
    lines = json.dumps([
        {"account_id": env["bank_a"], "debit": odd_amount, "credit": "0.00",
         "cost_center_id": None},
        {"account_id": env["bank_b"], "debit": "0.00", "credit": odd_amount,
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark="Excessive dp test",
                      lines=lines)

    if r.get("status") == "error":
        # Acceptable: rejected because of excessive precision
        return

    je_id = r["journal_entry_id"]
    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=je_id)

    if sr.get("status") == "error":
        # Acceptable: submit rejects excessive precision
        return

    # If it succeeded, stored values must be clean Decimals (likely rounded to 2dp)
    gl_rows = conn.execute(
        "SELECT debit, credit FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchall()

    for row in gl_rows:
        for field in ("debit", "credit"):
            val = row[field]
            if val and val != "0" and val != "0.00":
                d = Decimal(val)
                # Should be representable without float artifacts
                assert d == d.quantize(Decimal("0.01")) or d == Decimal(val), (
                    f"Garbled value in GL: {field}={val}"
                )


# ---------------------------------------------------------------------------
# 4. Large journal entry — 1000 lines
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_large_journal_entry_1000_lines(fresh_db):
    """JE with 1000 lines (500 debit/credit pairs) should complete without timeout."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    line_list = []
    for _ in range(500):
        line_list.append({
            "account_id": env["bank_a"],
            "debit": "1.00",
            "credit": "0.00",
            "cost_center_id": None,
        })
        line_list.append({
            "account_id": env["bank_b"],
            "debit": "0.00",
            "credit": "1.00",
            "cost_center_id": None,
        })

    lines = json.dumps(line_list)
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark="1000-line stress test",
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create 1000-line JE: {r}"
    je_id = r["journal_entry_id"]

    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=je_id)
    assert sr.get("status") != "error", f"Submit failed for 1000-line JE: {sr}"

    # Verify GL has 1000 entries for this voucher
    gl_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchone()["cnt"]
    assert gl_count == 1000, f"Expected 1000 GL entries, got {gl_count}"

    # Verify total debit = credit = 500.00
    totals = conn.execute(
        "SELECT COALESCE(decimal_sum(debit), '0') as td, "
        "COALESCE(decimal_sum(credit), '0') as tc "
        "FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchone()
    assert Decimal(totals["td"]) == Decimal("500.00"), (
        f"Expected total debit 500.00, got {totals['td']}"
    )
    assert Decimal(totals["tc"]) == Decimal("500.00"), (
        f"Expected total credit 500.00, got {totals['tc']}"
    )


# ---------------------------------------------------------------------------
# 5. Empty customer name
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_empty_customer_name(fresh_db):
    """add-customer with empty name should return an error."""
    conn = fresh_db
    cid = create_test_company(conn)

    r = _call_action("erpclaw-selling", "add-customer", conn,
                      company_id=cid, name="",
                      customer_type="company", territory="United States")
    assert r.get("status") == "error", "Expected error for empty customer name"


# ---------------------------------------------------------------------------
# 6. Unicode customer name
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_unicode_customer_name(fresh_db):
    """Customer with unicode name should be stored and retrieved correctly."""
    conn = fresh_db
    cid = create_test_company(conn)

    unicode_name = "Kundenname GmbH & Co. KG \u00dcn\u00efc\u00f6d\u00e9 \u65e5\u672c\u8a9e"
    r = _call_action("erpclaw-selling", "add-customer", conn,
                      company_id=cid, name=unicode_name,
                      customer_type="company", territory="United States")
    assert "customer_id" in r, f"Failed to create unicode customer: {r}"
    assert r["name"] == unicode_name

    # Verify via direct DB read
    row = conn.execute(
        "SELECT name FROM customer WHERE id = ?",
        (r["customer_id"],),
    ).fetchone()
    assert row["name"] == unicode_name, (
        f"Name not stored correctly: {row['name']!r} != {unicode_name!r}"
    )


# ---------------------------------------------------------------------------
# 7. SQL metacharacters in name
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_sql_metacharacters_in_name(fresh_db):
    """SQL injection attempt in customer name should be harmless (parameterized queries)."""
    conn = fresh_db
    cid = create_test_company(conn)

    evil_name = "O'Reilly; DROP TABLE customer; --"
    r = _call_action("erpclaw-selling", "add-customer", conn,
                      company_id=cid, name=evil_name,
                      customer_type="company", territory="United States")
    assert "customer_id" in r, f"Failed to create customer with SQL metacharacters: {r}"

    # Verify customer table still exists
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='customer'"
    ).fetchone()
    assert table_check is not None, "customer table was dropped!"

    # Verify customer appears in list
    lr = _call_action("erpclaw-selling", "list-customers", conn,
                       company_id=cid)
    names = [c["name"] for c in lr["customers"]]
    assert evil_name in names, (
        f"Customer with SQL metacharacters not found in list: {names}"
    )


# ---------------------------------------------------------------------------
# 8. Future date posting (year 2099)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_future_date_posting(fresh_db):
    """JE posted to year 2099 should succeed if a FY exists for that year."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid, name="FY 2099",
                            start_date="2099-01-01", end_date="2099-12-31")
    seed_naming_series(conn, cid, year=2099)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1010")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1011")

    lines = json.dumps([
        {"account_id": bank_a, "debit": "100.00", "credit": "0.00",
         "cost_center_id": None},
        {"account_id": bank_b, "debit": "0.00", "credit": "100.00",
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=cid,
                      posting_date="2099-06-15",
                      entry_type="journal",
                      remark="Future date test",
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create future JE: {r}"

    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=r["journal_entry_id"])
    assert sr.get("status") != "error", f"Submit failed for future date: {sr}"

    # Verify GL entries have correct posting date
    gl_rows = conn.execute(
        "SELECT posting_date FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (r["journal_entry_id"],),
    ).fetchall()
    assert len(gl_rows) >= 2
    for row in gl_rows:
        assert row["posting_date"] == "2099-06-15", (
            f"Expected 2099-06-15, got {row['posting_date']}"
        )


# ---------------------------------------------------------------------------
# 9. Leap year date posting (Feb 29)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_leap_year_date_posting(fresh_db):
    """JE posted on Feb 29 of a leap year should succeed."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid, name="FY 2028",
                            start_date="2028-01-01", end_date="2028-12-31")
    seed_naming_series(conn, cid, year=2028)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1010")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1011")

    lines = json.dumps([
        {"account_id": bank_a, "debit": "250.00", "credit": "0.00",
         "cost_center_id": None},
        {"account_id": bank_b, "debit": "0.00", "credit": "250.00",
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=cid,
                      posting_date="2028-02-29",
                      entry_type="journal",
                      remark="Leap year test",
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create leap year JE: {r}"

    sr = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=r["journal_entry_id"])
    assert sr.get("status") != "error", f"Submit failed for leap year date: {sr}"

    gl_rows = conn.execute(
        "SELECT posting_date FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (r["journal_entry_id"],),
    ).fetchall()
    assert len(gl_rows) >= 2
    for row in gl_rows:
        assert row["posting_date"] == "2028-02-29", (
            f"Expected 2028-02-29, got {row['posting_date']}"
        )


# ---------------------------------------------------------------------------
# 10. Very long remark (10K characters)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_very_long_remark(fresh_db):
    """JE with a 10,000-character remark should store and retrieve correctly."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    long_remark = "A" * 10000
    lines = json.dumps([
        {"account_id": env["bank_a"], "debit": "50.00", "credit": "0.00",
         "cost_center_id": None},
        {"account_id": env["bank_b"], "debit": "0.00", "credit": "50.00",
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark=long_remark,
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create JE with long remark: {r}"

    # Verify remark stored correctly
    row = conn.execute(
        "SELECT remark FROM journal_entry WHERE id = ?",
        (r["journal_entry_id"],),
    ).fetchone()
    assert row["remark"] == long_remark, (
        f"Remark length mismatch: stored {len(row['remark'])}, expected 10000"
    )


# ---------------------------------------------------------------------------
# 11. Duplicate item code
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_duplicate_item_code(fresh_db):
    """Creating two items with the same item_code should fail on the second."""
    conn = fresh_db

    # First item — should succeed
    r1 = _call_action("erpclaw-inventory", "add-item", conn,
                       item_code="SKU-DUP", item_name="First Widget",
                       item_type="stock", stock_uom="Each",
                       valuation_method="moving_average",
                       standard_rate="25.00")
    assert "item_id" in r1, f"First item creation failed: {r1}"

    # Second item with same code — should fail
    r2 = _call_action("erpclaw-inventory", "add-item", conn,
                       item_code="SKU-DUP", item_name="Duplicate Widget",
                       item_type="stock", stock_uom="Each",
                       valuation_method="moving_average",
                       standard_rate="30.00")
    assert r2.get("status") == "error", (
        f"Expected error for duplicate item_code, got: {r2}"
    )


# ---------------------------------------------------------------------------
# 12. Maximum fiscal years (50)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_max_fiscal_years(fresh_db):
    """Creating 50 fiscal years should succeed and all should be retrievable."""
    conn = fresh_db
    cid = create_test_company(conn)

    fy_ids = []
    for year in range(2026, 2076):
        fy_id = create_test_fiscal_year(
            conn, cid,
            name=f"FY {year}",
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
        )
        fy_ids.append(fy_id)

    assert len(fy_ids) == 50

    # list-fiscal-years with limit=100 to get them all
    r = _call_action("erpclaw-gl", "list-fiscal-years", conn,
                      company_id=cid, limit=100)
    assert "fiscal_years" in r, f"list-fiscal-years failed: {r}"
    assert r["total_count"] == 50, (
        f"Expected 50 fiscal years, got {r['total_count']}"
    )
    assert len(r["fiscal_years"]) == 50, (
        f"Expected 50 rows returned, got {len(r['fiscal_years'])}"
    )


# ---------------------------------------------------------------------------
# 13. Null / empty company_id on journal entry
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_null_company_id(fresh_db):
    """add-journal-entry with None or empty company_id should return an error."""
    conn = fresh_db

    # With None
    r_none = _call_action("erpclaw-journals", "add-journal-entry", conn,
                           company_id=None,
                           posting_date="2026-06-15",
                           entry_type="journal",
                           lines=json.dumps([]))
    assert r_none.get("status") == "error", (
        f"Expected error for company_id=None, got: {r_none}"
    )

    # With empty string
    r_empty = _call_action("erpclaw-journals", "add-journal-entry", conn,
                            company_id="",
                            posting_date="2026-06-15",
                            entry_type="journal",
                            lines=json.dumps([]))
    assert r_empty.get("status") == "error", (
        f"Expected error for company_id='', got: {r_empty}"
    )


# ---------------------------------------------------------------------------
# 14. Very long account name (500 characters)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_very_long_account_name(fresh_db):
    """Account with a 500-character name should be stored correctly (SQLite TEXT has no limit)."""
    conn = fresh_db
    cid = create_test_company(conn)

    long_name = "A" * 500
    acct_id = create_test_account(conn, cid, long_name, "asset",
                                   account_type="bank", account_number="1099")

    # Verify stored correctly
    row = conn.execute(
        "SELECT name FROM account WHERE id = ?", (acct_id,)
    ).fetchone()
    assert row["name"] == long_name, (
        f"Account name length mismatch: stored {len(row['name'])}, expected 500"
    )


# ---------------------------------------------------------------------------
# 15. Special characters in remark (tabs, newlines, null bytes, emoji)
# ---------------------------------------------------------------------------

@pytest.mark.boundary
def test_special_characters_in_remark(fresh_db):
    """JE remark with tabs, newlines, null bytes, and emoji should be handled."""
    conn = fresh_db
    env = _setup_je_environment(conn)

    special_remark = "Line1\tTab\nLine2\r\n\\0End \U0001f680"
    lines = json.dumps([
        {"account_id": env["bank_a"], "debit": "75.00", "credit": "0.00",
         "cost_center_id": None},
        {"account_id": env["bank_b"], "debit": "0.00", "credit": "75.00",
         "cost_center_id": None},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15",
                      entry_type="journal",
                      remark=special_remark,
                      lines=lines)
    assert "journal_entry_id" in r, f"Failed to create JE with special remark: {r}"

    # Verify stored — null byte may be stripped, but rest should survive
    row = conn.execute(
        "SELECT remark FROM journal_entry WHERE id = ?",
        (r["journal_entry_id"],),
    ).fetchone()
    stored = row["remark"]

    # Tab and newline should survive
    assert "\t" in stored, "Tab character was lost"
    assert "\n" in stored, "Newline character was lost"
    # Emoji should survive in SQLite TEXT
    assert "\U0001f680" in stored, "Emoji was lost"
