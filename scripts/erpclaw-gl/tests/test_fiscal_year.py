"""Tests for erpclaw-gl fiscal year management.

Actions tested:
  - add-fiscal-year
  - list-fiscal-years
  - validate-period-close
  - close-fiscal-year
  - reopen-fiscal-year
"""
import json
import pytest
from decimal import Decimal
from gl_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    load_db_query,
)

mod = load_db_query()


class TestAddFiscalYear:
    def test_basic_create(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_fiscal_year, conn, ns(
            name="FY 2027", start_date="2027-01-01",
            end_date="2027-12-31", company_id=cid,
        ))
        assert is_ok(result)
        assert "fiscal_year_id" in result

        row = conn.execute("SELECT * FROM fiscal_year WHERE id=?",
                           (result["fiscal_year_id"],)).fetchone()
        assert row["name"] == "FY 2027"
        assert row["is_closed"] == 0

    def test_overlapping_dates_fails(self, conn):
        cid = seed_company(conn)
        seed_fiscal_year(conn, cid, "FY 2026", "2026-01-01", "2026-12-31")
        result = call_action(mod.add_fiscal_year, conn, ns(
            name="FY 2026 Overlap", start_date="2026-06-01",
            end_date="2027-05-31", company_id=cid,
        ))
        assert is_error(result)

    def test_missing_dates_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_fiscal_year, conn, ns(
            name="Bad FY", start_date=None,
            end_date="2027-12-31", company_id=cid,
        ))
        assert is_error(result)


class TestListFiscalYears:
    def test_list_by_company(self, conn):
        cid = seed_company(conn)
        seed_fiscal_year(conn, cid, "FY A", "2025-01-01", "2025-12-31")
        seed_fiscal_year(conn, cid, "FY B", "2026-01-01", "2026-12-31")
        result = call_action(mod.list_fiscal_years, conn, ns(
            company_id=cid, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2


class TestCloseFiscalYear:
    def test_close_empty_fy(self, conn):
        """Close a fiscal year with no GL entries (P&L = 0)."""
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid, "FY Close", "2025-01-01", "2025-12-31")
        closing_acct = seed_account(conn, cid, "Retained Earnings", "equity",
                                    "equity", "3000")
        result = call_action(mod.close_fiscal_year, conn, ns(
            fiscal_year_id=fyid,
            closing_account_id=closing_acct,
            posting_date="2025-12-31",
        ))
        assert is_ok(result)

        # FY should be marked closed
        fy = conn.execute("SELECT is_closed FROM fiscal_year WHERE id=?",
                          (fyid,)).fetchone()
        assert fy["is_closed"] == 1

    def test_close_with_pl_entries(self, conn):
        """Close FY that has income/expense entries → creates PCV."""
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid, "FY PL", "2026-01-01", "2026-12-31")
        cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
        revenue = seed_account(conn, cid, "Revenue", "income", "revenue", "4000")
        expense = seed_account(conn, cid, "Expenses", "expense", "expense", "5000")
        retained = seed_account(conn, cid, "Retained Earnings", "equity",
                                "equity", "3000")
        ccid = seed_cost_center(conn, cid, "Main CC")

        # Post income
        entries = json.dumps([
            {"account_id": cash, "debit": "10000.00", "credit": "0"},
            {"account_id": revenue, "debit": "0", "credit": "10000.00",
             "cost_center_id": ccid},
        ])
        post1 = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-INC",
            posting_date="2026-06-15", company_id=cid,
            entries=entries,
        ))
        assert is_ok(post1), f"Income posting failed: {post1}"

        # Post expense
        entries2 = json.dumps([
            {"account_id": expense, "debit": "3000.00", "credit": "0",
             "cost_center_id": ccid},
            {"account_id": cash, "debit": "0", "credit": "3000.00"},
        ])
        post2 = call_action(mod.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id="JE-EXP",
            posting_date="2026-06-15", company_id=cid,
            entries=entries2,
        ))
        assert is_ok(post2), f"Expense posting failed: {post2}"

        # Verify GL entries exist
        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled=0"
        ).fetchone()["cnt"]
        assert gl_count == 4, f"Expected 4 GL entries, got {gl_count}"

        # Close FY
        result = call_action(mod.close_fiscal_year, conn, ns(
            fiscal_year_id=fyid,
            closing_account_id=retained,
            posting_date="2026-12-31",
        ))
        assert is_ok(result)
        # If there were P&L entries, PCV should be created
        assert result.get("pcv_id") is not None

    def test_close_already_closed_fails(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid, "FY Closed2", "2025-01-01", "2025-12-31")
        closing = seed_account(conn, cid, "RE", "equity", "equity", "3001")
        call_action(mod.close_fiscal_year, conn, ns(
            fiscal_year_id=fyid, closing_account_id=closing,
            posting_date="2025-12-31",
        ))
        result = call_action(mod.close_fiscal_year, conn, ns(
            fiscal_year_id=fyid, closing_account_id=closing,
            posting_date="2025-12-31",
        ))
        assert is_error(result)


class TestReopenFiscalYear:
    def test_reopen_closed_fy(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid, "FY Reopen", "2025-01-01", "2025-12-31")
        closing = seed_account(conn, cid, "RE2", "equity", "equity", "3002")
        call_action(mod.close_fiscal_year, conn, ns(
            fiscal_year_id=fyid, closing_account_id=closing,
            posting_date="2025-12-31",
        ))
        result = call_action(mod.reopen_fiscal_year, conn, ns(
            fiscal_year_id=fyid,
        ))
        assert is_ok(result)

        fy = conn.execute("SELECT is_closed FROM fiscal_year WHERE id=?",
                          (fyid,)).fetchone()
        assert fy["is_closed"] == 0

    def test_reopen_open_fy_fails(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid, "FY Open", "2025-01-01", "2025-12-31")
        result = call_action(mod.reopen_fiscal_year, conn, ns(
            fiscal_year_id=fyid,
        ))
        assert is_error(result)
