"""Tests for erpclaw-gl GL entry posting and reversal.

Actions tested:
  - post-gl-entries
  - reverse-gl-entries
  - list-gl-entries
  - get-account-balance
  - check-gl-integrity

These tests exercise the 12-step GL validation and immutability rules.
"""
import json
import pytest
from decimal import Decimal
from gl_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    seed_customer, seed_supplier, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gl_setup(conn):
    """Set up company + chart + FY + key accounts + cost center for GL tests."""
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")

    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    ar = seed_account(conn, cid, "Accounts Receivable", "asset", "receivable", "1100")
    ap = seed_account(conn, cid, "Accounts Payable", "liability", "payable", "2000")
    revenue = seed_account(conn, cid, "Sales Revenue", "income", "revenue", "4000")
    expense = seed_account(conn, cid, "Office Supplies", "expense", "expense", "5000")
    equity = seed_account(conn, cid, "Retained Earnings", "equity", "equity", "3000")

    return {
        "company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
        "cash": cash, "ar": ar, "ap": ap,
        "revenue": revenue, "expense": expense, "equity": equity,
    }


def _entries(gl_setup, *pairs):
    """Build GL entries JSON. Each pair = (account_key, debit, credit).
    Automatically adds cost_center_id for income/expense accounts."""
    PL_KEYS = {"revenue", "expense"}
    items = []
    for key, debit, credit in pairs:
        entry = {"account_id": gl_setup[key], "debit": debit, "credit": credit}
        if key in PL_KEYS:
            entry["cost_center_id"] = gl_setup["cc"]
        items.append(entry)
    return json.dumps(items)


# ──────────────────────────────────────────────────────────────────────────────
# post-gl-entries
# ──────────────────────────────────────────────────────────────────────────────

class TestPostGLEntries:
    def test_balanced_entry(self, conn, gl_setup):
        """Post a balanced debit/credit pair."""
        entries = _entries(gl_setup,
            ("cash", "1000.00", "0"),
            ("revenue", "0", "1000.00"),
        )
        result = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-001",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        assert is_ok(result)
        assert result["entries_created"] == 2
        assert len(result["gl_entry_ids"]) == 2

        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_id='JE-001'"
        ).fetchall()
        assert len(rows) == 2

    def test_unbalanced_entry_fails(self, conn, gl_setup):
        """Unbalanced entry should fail GL validation."""
        entries = _entries(gl_setup,
            ("cash", "1000.00", "0"),
            ("revenue", "0", "500.00"),
        )
        result = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-BAD",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        assert is_error(result)

        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_id='JE-BAD'"
        ).fetchall()
        assert len(rows) == 0

    def test_zero_amount_entry_skipped(self, conn, gl_setup):
        """Zero debit AND zero credit entries are skipped (BUG-003: no GL rows created)."""
        entries = _entries(gl_setup,
            ("cash", "0", "0"),
            ("revenue", "0", "0"),
        )
        result = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-ZERO",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        # Zero-amount entries are silently skipped — 0 entries created, no error
        assert result.get("entries_created", 0) == 0
        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_id='JE-ZERO'"
        ).fetchall()
        assert len(rows) == 0

    def test_with_party(self, conn, gl_setup):
        """Post GL entries with party (customer) information."""
        cust_id = seed_customer(conn, gl_setup["company_id"])
        entries = json.dumps([
            {"account_id": gl_setup["ar"], "debit": "500.00", "credit": "0",
             "party_type": "customer", "party_id": cust_id},
            {"account_id": gl_setup["revenue"], "debit": "0", "credit": "500.00",
             "cost_center_id": gl_setup["cc"]},
        ])
        result = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="sales_invoice", voucher_id="SI-001",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        assert is_ok(result)

        row = conn.execute(
            "SELECT party_type, party_id FROM gl_entry WHERE account_id=? AND voucher_id='SI-001'",
            (gl_setup["ar"],)
        ).fetchone()
        assert row["party_type"] == "customer"
        assert row["party_id"] == cust_id

    def test_multiple_vouchers_independent(self, conn, gl_setup):
        """Post two separate vouchers — both should succeed independently."""
        for vid in ["JE-A", "JE-B"]:
            entries = _entries(gl_setup,
                ("cash", "200.00", "0"),
                ("revenue", "0", "200.00"),
            )
            result = call_action(mod.post_gl_entries, conn, ns(
                voucher_type="journal_entry", voucher_id=vid,
                posting_date="2026-06-15",
                company_id=gl_setup["company_id"],
                entries=entries,
            ))
            assert is_ok(result)

        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled=0"
        ).fetchone()["cnt"]
        assert count == 4

    def test_decimal_precision(self, conn, gl_setup):
        """Financial amounts stored as TEXT Decimal — GL rounds to currency precision."""
        entries = _entries(gl_setup,
            ("cash", "1234.56", "0"),
            ("revenue", "0", "1234.56"),
        )
        result = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-PREC",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        assert is_ok(result)

        row = conn.execute(
            "SELECT debit FROM gl_entry WHERE voucher_id='JE-PREC' AND debit != '0'"
        ).fetchone()
        assert Decimal(row["debit"]) == Decimal("1234.56")


# ──────────────────────────────────────────────────────────────────────────────
# reverse-gl-entries
# ──────────────────────────────────────────────────────────────────────────────

class TestReverseGLEntries:
    def test_basic_reversal(self, conn, gl_setup):
        """Reverse creates mirror entries with swapped debit/credit."""
        entries = _entries(gl_setup,
            ("cash", "1000.00", "0"),
            ("revenue", "0", "1000.00"),
        )
        call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-REV",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))

        result = call_action(mod.reverse_gl_entries_action, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-REV",
            posting_date=None,
        ))
        assert is_ok(result)
        assert result["reversed_count"] == 2

        cancelled = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id='JE-REV' AND is_cancelled=1"
        ).fetchone()["cnt"]
        assert cancelled == 2

    def test_reversal_creates_mirror(self, conn, gl_setup):
        """Reversal entries should have opposite debit/credit."""
        entries = _entries(gl_setup,
            ("expense", "300.00", "0"),
            ("cash", "0", "300.00"),
        )
        call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-MIR",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        call_action(mod.reverse_gl_entries_action, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-MIR",
            posting_date=None,
        ))

        total = conn.execute("""
            SELECT SUM(CAST(debit AS REAL)) as total_debit,
                   SUM(CAST(credit AS REAL)) as total_credit
            FROM gl_entry WHERE is_cancelled=0
        """).fetchone()
        assert abs((total["total_debit"] or 0) - (total["total_credit"] or 0)) < 0.01

    def test_reverse_nonexistent_fails(self, conn, gl_setup):
        """Reversing entries for a voucher that doesn't exist should fail."""
        result = call_action(mod.reverse_gl_entries_action, conn, ns(
            voucher_type="journal_entry", voucher_id="NONEXISTENT",
            posting_date=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# list-gl-entries
# ──────────────────────────────────────────────────────────────────────────────

class TestListGLEntries:
    def test_list_by_voucher(self, conn, gl_setup):
        entries = _entries(gl_setup,
            ("cash", "100.00", "0"),
            ("revenue", "0", "100.00"),
        )
        call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-LIST",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        result = call_action(mod.list_gl_entries, conn, ns(
            company_id=None, account_id=None,
            voucher_type="journal_entry", voucher_id="JE-LIST",
            party_type=None, party_id=None,
            from_date=None, to_date=None, is_cancelled=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 2

    def test_list_by_date_range(self, conn, gl_setup):
        for vid, dt in [("JE-JAN", "2026-01-15"), ("JE-JUN", "2026-06-15")]:
            entries = _entries(gl_setup,
                ("cash", "100.00", "0"),
                ("revenue", "0", "100.00"),
            )
            call_action(mod.post_gl_entries, conn, ns(
                voucher_type="journal_entry", voucher_id=vid,
                posting_date=dt,
                company_id=gl_setup["company_id"],
                entries=entries,
            ))
        result = call_action(mod.list_gl_entries, conn, ns(
            company_id=None, account_id=None,
            voucher_type=None, voucher_id=None,
            party_type=None, party_id=None,
            from_date="2026-06-01", to_date="2026-06-30",
            is_cancelled=None, limit=None, offset=None,
        ))
        assert result["total_count"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# get-account-balance
# ──────────────────────────────────────────────────────────────────────────────

class TestGetAccountBalance:
    def test_balance_after_posting(self, conn, gl_setup):
        entries = _entries(gl_setup,
            ("cash", "5000.00", "0"),
            ("revenue", "0", "5000.00"),
        )
        call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-BAL",
            posting_date="2026-06-15",
            company_id=gl_setup["company_id"],
            entries=entries,
        ))
        result = call_action(mod.get_account_balance_action, conn, ns(
            account_id=gl_setup["cash"],
            as_of_date="2026-12-31",
            party_type=None, party_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["debit_total"]) == Decimal("5000.00")

    def test_zero_balance_no_entries(self, conn, gl_setup):
        result = call_action(mod.get_account_balance_action, conn, ns(
            account_id=gl_setup["cash"],
            as_of_date="2026-12-31",
            party_type=None, party_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result.get("balance", "0")) == Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# check-gl-integrity
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckGLIntegrity:
    def test_integrity_clean(self, conn, gl_setup):
        """Empty GL should pass integrity check."""
        result = call_action(mod.check_gl_integrity, conn, ns(
            company_id=gl_setup["company_id"],
        ))
        assert is_ok(result)

    def test_integrity_after_balanced_posts(self, conn, gl_setup):
        """GL integrity should pass after balanced postings."""
        for vid in ["JE-INT-1", "JE-INT-2"]:
            entries = _entries(gl_setup,
                ("cash", "1000.00", "0"),
                ("revenue", "0", "1000.00"),
            )
            call_action(mod.post_gl_entries, conn, ns(
                voucher_type="journal_entry", voucher_id=vid,
                posting_date="2026-06-15",
                company_id=gl_setup["company_id"],
                entries=entries,
            ))
        result = call_action(mod.check_gl_integrity, conn, ns(
            company_id=gl_setup["company_id"],
        ))
        assert is_ok(result)
        assert Decimal(result["total_debit"]) == Decimal(result["total_credit"])
