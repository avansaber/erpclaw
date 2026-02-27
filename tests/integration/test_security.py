"""Security tests for ERPClaw.

Tests SQL injection, XSS storage, RBAC company isolation, and
malicious/edge-case payload handling. All 18 tests verify that
bad input is handled safely — parameterized queries, proper validation,
and company isolation are the primary defenses.
"""
import json
import uuid
import sqlite3
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
    setup_phase2_environment,
)


# ===========================================================================
# SQL Injection Tests (6)
# ===========================================================================

@pytest.mark.security
def test_sql_injection_in_customer_name(fresh_db):
    """SQL injection in customer name is stored literally, not executed."""
    conn = fresh_db
    cid = create_test_company(conn)

    injection_name = "'; DROP TABLE customer; --"

    result = _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid, name=injection_name,
        customer_type="company", territory="United States",
    )
    assert result.get("status") == "ok" or "customer_id" in result

    # Table must still exist and have rows
    count = conn.execute("SELECT count(*) FROM customer").fetchone()[0]
    assert count >= 1, "customer table should still have rows after injection attempt"

    # The name must be stored literally
    row = conn.execute(
        "SELECT name FROM customer WHERE id = ?",
        (result["customer_id"],),
    ).fetchone()
    assert row["name"] == injection_name, (
        "Injection payload should be stored as literal text"
    )


@pytest.mark.security
def test_sql_injection_in_search(fresh_db):
    """SQL injection in search parameter does not return all customers."""
    conn = fresh_db
    cid = create_test_company(conn)

    # Add a few customers with normal names
    for name in ("Acme Corp", "Beta LLC", "Gamma Inc"):
        _call_action(
            "erpclaw-selling", "add-customer", conn,
            company_id=cid, name=name, customer_type="company",
        )

    # Attempt SQL injection via search
    result = _call_action(
        "erpclaw-selling", "list-customers", conn,
        company_id=cid, search="' OR 1=1 --",
    )
    assert result.get("status") == "ok" or "customers" in result

    # Should return 0 results — the injection string matches no real names
    customers = result.get("customers", [])
    assert len(customers) == 0, (
        f"SQL injection in search returned {len(customers)} results; expected 0"
    )


@pytest.mark.security
def test_sql_injection_in_company_id(fresh_db):
    """SQL injection in company_id returns error, not data leakage."""
    conn = fresh_db

    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id="' UNION SELECT * FROM company --",
        posting_date="2026-03-15",
        entry_type="journal",
        remark="test",
        lines="[]",
    )

    # Must get an error response — not a success with leaked data
    assert result.get("status") == "error", (
        "SQL injection in company_id should produce an error response"
    )


@pytest.mark.security
def test_sql_injection_in_list_filter(fresh_db):
    """SQL injection in a list filter parameter does not bypass filtering."""
    conn = fresh_db
    cid = create_test_company(conn)

    # Add customers with a known group
    for name in ("Alpha Co", "Bravo Co"):
        _call_action(
            "erpclaw-selling", "add-customer", conn,
            company_id=cid, name=name, customer_type="company",
        )

    # Inject via customer_group (which IS a filter field in list-customers)
    result = _call_action(
        "erpclaw-selling", "list-customers", conn,
        company_id=cid, customer_group="retail' OR 1=1 --",
    )
    assert result.get("status") == "ok" or "customers" in result

    # Parameterized query: the literal string won't match any group
    customers = result.get("customers", [])
    assert len(customers) == 0, (
        f"SQL injection in customer_group returned {len(customers)} results; expected 0"
    )


@pytest.mark.security
def test_sql_injection_in_account_name(fresh_db):
    """SQL injection in account name is stored literally or rejected."""
    conn = fresh_db
    cid = create_test_company(conn)

    injection_name = "test' OR '1'='1"

    result = _call_action(
        "erpclaw-gl", "add-account", conn,
        company_id=cid, name=injection_name,
        root_type="asset", account_type="bank",
        account_number="1099",
    )

    if result.get("status") == "error":
        # Rejected — that's safe too
        pass
    else:
        # Stored literally — verify
        acct_id = result.get("account_id")
        assert acct_id is not None
        row = conn.execute(
            "SELECT name FROM account WHERE id = ?", (acct_id,)
        ).fetchone()
        assert row["name"] == injection_name, (
            "Injection payload in account name should be stored as literal text"
        )


@pytest.mark.security
def test_sql_injection_via_json_field(fresh_db):
    """SQL injection inside JE line JSON is caught during validation."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    # Injection in account_id within JSON lines
    malicious_lines = json.dumps([
        {"account_id": "' OR 1=1 --", "debit": "100.00", "credit": "0.00"},
        {"account_id": "' OR 1=1 --", "debit": "0.00", "credit": "100.00"},
    ])

    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid, posting_date="2026-03-15",
        entry_type="journal", remark="injection test",
        lines=malicious_lines,
    )

    # Should fail because account_id is not a valid UUID / not found
    assert result.get("status") == "error", (
        "SQL injection in account_id within JSON lines should produce an error"
    )


# ===========================================================================
# XSS and Content Tests (3)
# ===========================================================================

@pytest.mark.security
def test_xss_in_remark_stored_safely(fresh_db):
    """XSS payload in JE remark is stored as plain text, not executed."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4001")
    cc = create_test_cost_center(conn, cid)

    xss_payload = "<script>alert('xss')</script><img onerror=alert(1) src=x>"

    lines = json.dumps([
        {"account_id": bank, "debit": "500.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "500.00",
         "cost_center_id": cc},
    ])

    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid, posting_date="2026-03-15",
        entry_type="journal", remark=xss_payload,
        lines=lines,
    )
    assert result.get("status") in ("ok", "created") or "journal_entry_id" in result
    je_id = result["journal_entry_id"]

    # Verify the remark is stored exactly as-is (plain text)
    row = conn.execute(
        "SELECT remark FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert row["remark"] == xss_payload, (
        "XSS payload should be stored literally as plain text"
    )


@pytest.mark.security
def test_xss_in_customer_name(fresh_db):
    """XSS payload in customer name is stored literally."""
    conn = fresh_db
    cid = create_test_company(conn)

    xss_name = "<b onmouseover=alert(1)>Test</b>"

    result = _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid, name=xss_name, customer_type="company",
    )
    assert result.get("status") == "ok" or "customer_id" in result

    row = conn.execute(
        "SELECT name FROM customer WHERE id = ?",
        (result["customer_id"],),
    ).fetchone()
    assert row["name"] == xss_name, (
        "XSS payload in customer name should be stored literally"
    )


@pytest.mark.security
def test_null_byte_injection(fresh_db):
    """Null byte in customer name is either rejected or stored safely."""
    conn = fresh_db
    cid = create_test_company(conn)

    null_name = "Test\x00Customer"

    result = _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid, name=null_name, customer_type="company",
    )

    if result.get("status") == "error":
        # Rejected — safe behavior
        pass
    else:
        # Stored — verify it's retrievable (null byte stripped or literal)
        cust_id = result.get("customer_id")
        assert cust_id is not None
        row = conn.execute(
            "SELECT name FROM customer WHERE id = ?", (cust_id,)
        ).fetchone()
        assert row is not None, "Customer with null byte name should be retrievable"
        # The name is either the literal (with embedded null) or stripped
        assert "Test" in row["name"], "Customer name should contain 'Test'"


# ===========================================================================
# RBAC / Company Isolation Tests (4)
# ===========================================================================

@pytest.mark.security
def test_rbac_company_isolation_gl(fresh_db):
    """GL entries for company A are not visible when filtering by company B."""
    conn = fresh_db

    # Company A
    cid_a = create_test_company(conn, name="Company A", abbr="CA")
    create_test_fiscal_year(conn, cid_a, name="FY-A 2026")
    seed_naming_series(conn, cid_a)
    bank_a = create_test_account(conn, cid_a, "Bank A", "asset",
                                 account_type="bank", account_number="1010")
    rev_a = create_test_account(conn, cid_a, "Revenue A", "income",
                                account_type="revenue", account_number="4001")
    cc_a = create_test_cost_center(conn, cid_a, name="CC-A")

    # Company B
    cid_b = create_test_company(conn, name="Company B", abbr="CB")
    create_test_fiscal_year(conn, cid_b, name="FY-B 2026")
    seed_naming_series(conn, cid_b)
    bank_b = create_test_account(conn, cid_b, "Bank B", "asset",
                                 account_type="bank", account_number="1011")

    # Submit a JE for company A
    lines = json.dumps([
        {"account_id": bank_a, "debit": "1000.00", "credit": "0"},
        {"account_id": rev_a, "debit": "0", "credit": "1000.00",
         "cost_center_id": cc_a},
    ])
    r = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid_a, posting_date="2026-03-15",
        entry_type="journal", lines=lines,
    )
    je_id = r["journal_entry_id"]
    _call_action(
        "erpclaw-journals", "submit-journal-entry", conn,
        journal_entry_id=je_id,
    )

    # Query GL for company B — should find nothing from company A
    gl_result = _call_action(
        "erpclaw-gl", "list-gl-entries", conn,
        company_id=cid_b,
    )
    entries = gl_result.get("entries", [])
    company_a_entries = [e for e in entries if e.get("account_id") in (bank_a, rev_a)]
    assert len(company_a_entries) == 0, (
        "GL entries for company A must not appear when filtering by company B"
    )


@pytest.mark.security
def test_rbac_company_isolation_customers(fresh_db):
    """Customer in company A is not visible in company B's customer list."""
    conn = fresh_db

    cid_a = create_test_company(conn, name="Company A", abbr="CA")
    cid_b = create_test_company(conn, name="Company B", abbr="CB")

    # Create customer in company A
    r = _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid_a, name="Secret Customer",
        customer_type="company",
    )
    cust_a_id = r["customer_id"]

    # List customers for company B
    result = _call_action(
        "erpclaw-selling", "list-customers", conn,
        company_id=cid_b,
    )
    customers_b = result.get("customers", [])
    ids_b = [c["id"] for c in customers_b]
    assert cust_a_id not in ids_b, (
        "Company A's customer must not appear in company B's customer list"
    )


@pytest.mark.security
def test_rbac_company_isolation_items_global(fresh_db):
    """Items are global (no company_id) — verify they are shared by design."""
    conn = fresh_db

    cid_a = create_test_company(conn, name="Company A", abbr="CA")
    cid_b = create_test_company(conn, name="Company B", abbr="CB")

    # Create a global item (items have no company_id)
    item_id = create_test_item(conn, item_code="GLOBAL-001",
                               item_name="Global Widget")

    # list-items should return this item regardless of which company context
    result = _call_action(
        "erpclaw-inventory", "list-items", conn,
    )
    items = result.get("items", [])
    item_ids = [i["id"] for i in items]
    assert item_id in item_ids, (
        "Global items should be visible regardless of company context"
    )


@pytest.mark.security
def test_rbac_cross_company_je_rejected(fresh_db):
    """Submitting a JE for company A using company B's account is rejected."""
    conn = fresh_db

    # Company A
    cid_a = create_test_company(conn, name="Company A", abbr="CA")
    create_test_fiscal_year(conn, cid_a, name="FY-A 2026")
    seed_naming_series(conn, cid_a)
    bank_a = create_test_account(conn, cid_a, "Bank A", "asset",
                                 account_type="bank", account_number="1010")

    # Company B
    cid_b = create_test_company(conn, name="Company B", abbr="CB")
    create_test_fiscal_year(conn, cid_b, name="FY-B 2026")
    seed_naming_series(conn, cid_b)
    bank_b = create_test_account(conn, cid_b, "Bank B", "asset",
                                 account_type="bank", account_number="1011")
    rev_b = create_test_account(conn, cid_b, "Revenue B", "income",
                                account_type="revenue", account_number="4002")

    # Create JE for company A but using company B's accounts
    lines = json.dumps([
        {"account_id": bank_b, "debit": "500.00", "credit": "0"},
        {"account_id": rev_b, "debit": "0", "credit": "500.00"},
    ])

    r = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid_a, posting_date="2026-03-15",
        entry_type="journal", lines=lines,
    )

    if r.get("status") == "error":
        # Rejected at add time — safe
        return

    # If add succeeded (accounts exist, just wrong company), submit must fail
    je_id = r["journal_entry_id"]
    submit_result = _call_action(
        "erpclaw-journals", "submit-journal-entry", conn,
        journal_entry_id=je_id,
    )

    # GL Validation Step 3 (Account-Company Affinity) should reject this
    assert submit_result.get("status") == "error", (
        "Cross-company JE should be rejected by GL validation step 3 "
        "(Account-Company Affinity)"
    )


# ===========================================================================
# Payload and Input Tests (5)
# ===========================================================================

@pytest.mark.security
def test_large_payload_lines(fresh_db):
    """JE with 5000 lines (2500 balanced pairs) succeeds or rejects cleanly."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    expense = create_test_account(conn, cid, "Expense", "expense",
                                  account_type="expense", account_number="5001")
    cc = create_test_cost_center(conn, cid)

    # Build 5000 lines: 2500 debit/credit pairs of $1.00 each
    lines = []
    for _ in range(2500):
        lines.append({"account_id": expense, "debit": "1.00", "credit": "0",
                       "cost_center_id": cc})
        lines.append({"account_id": bank, "debit": "0", "credit": "1.00"})

    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid, posting_date="2026-03-15",
        entry_type="journal", remark="large payload test",
        lines=json.dumps(lines),
    )

    # Must either succeed or give a clear error — never crash
    assert result.get("status") == "error" or "journal_entry_id" in result, (
        "Large payload should either succeed or return a clear error"
    )

    if "journal_entry_id" in result:
        # Verify all 5000 lines were stored
        je_id = result["journal_entry_id"]
        line_count = conn.execute(
            "SELECT count(*) FROM journal_entry_line WHERE journal_entry_id = ?",
            (je_id,),
        ).fetchone()[0]
        assert line_count == 5000, (
            f"Expected 5000 JE lines stored, got {line_count}"
        )


@pytest.mark.security
def test_integer_overflow_in_limit(fresh_db):
    """Extremely large limit value does not crash the action."""
    conn = fresh_db
    cid = create_test_company(conn)

    # Add a customer so there's at least one result
    _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid, name="Test Customer", customer_type="company",
    )

    result = _call_action(
        "erpclaw-selling", "list-customers", conn,
        company_id=cid, limit=99999999999,
    )

    # Must return results or an error — never crash
    assert result.get("status") == "error" or "customers" in result, (
        "Huge limit should return results or a clear error, not crash"
    )

    if "customers" in result:
        # Should have at least the one customer we added
        assert len(result["customers"]) >= 1


@pytest.mark.security
def test_csv_injection_in_field(fresh_db):
    """CSV formula injection characters in customer names are stored safely."""
    conn = fresh_db
    cid = create_test_company(conn)

    dangerous_names = [
        "=CMD('calc')",
        "+cmd|'calc'",
        "-1+1",
        "@SUM(A1:A10)",
    ]

    stored_ids = []
    for name in dangerous_names:
        result = _call_action(
            "erpclaw-selling", "add-customer", conn,
            company_id=cid, name=name, customer_type="company",
        )
        assert result.get("status") == "ok" or "customer_id" in result, (
            f"Customer with name '{name}' should be stored safely"
        )
        stored_ids.append(result["customer_id"])

    # Verify all names are stored literally
    for cust_id, expected_name in zip(stored_ids, dangerous_names):
        row = conn.execute(
            "SELECT name FROM customer WHERE id = ?", (cust_id,)
        ).fetchone()
        assert row["name"] == expected_name, (
            f"CSV injection name '{expected_name}' should be stored literally"
        )


@pytest.mark.security
def test_path_traversal_in_name(fresh_db):
    """Path traversal string in customer name is stored literally."""
    conn = fresh_db
    cid = create_test_company(conn)

    traversal_name = "../../etc/passwd"

    result = _call_action(
        "erpclaw-selling", "add-customer", conn,
        company_id=cid, name=traversal_name, customer_type="company",
    )
    assert result.get("status") == "ok" or "customer_id" in result

    row = conn.execute(
        "SELECT name FROM customer WHERE id = ?",
        (result["customer_id"],),
    ).fetchone()
    assert row["name"] == traversal_name, (
        "Path traversal string should be stored literally — it's just a text field"
    )


@pytest.mark.security
def test_extremely_nested_json(fresh_db):
    """Deeply nested JSON in JE lines is processed or rejected cleanly."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4001")
    cc = create_test_cost_center(conn, cid)

    # Valid JE lines with extra deeply nested fields
    nested_extra = {"level1": {"level2": {"level3": {"level4": {"level5": "deep"}}}}}
    lines = json.dumps([
        {"account_id": bank, "debit": "200.00", "credit": "0",
         "extra_nested": nested_extra},
        {"account_id": revenue, "debit": "0", "credit": "200.00",
         "cost_center_id": cc, "metadata": nested_extra},
    ])

    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=cid, posting_date="2026-03-15",
        entry_type="journal", remark="nested json test",
        lines=lines,
    )

    # Must either succeed (ignoring extra fields) or reject cleanly
    assert result.get("status") == "error" or "journal_entry_id" in result, (
        "Deeply nested JSON should be processed or rejected, never crash"
    )
