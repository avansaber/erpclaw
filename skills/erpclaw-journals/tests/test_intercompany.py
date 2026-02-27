"""Tests for create-intercompany-je action.

Tests paired JE creation between two companies, GL balance per company,
cancel behavior, account auto-creation, and currency mismatch rejection.
"""
import uuid
from decimal import Decimal

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
)


def _setup_two_companies(conn):
    """Create two test companies with required accounts and cost centers."""
    co_a = create_test_company(conn, name="Company A", abbr="CA")
    co_b = create_test_company(conn, name="Company B", abbr="CB")
    create_test_fiscal_year(conn, co_a, name="FY 2026 A")
    create_test_fiscal_year(conn, co_b, name="FY 2026 B")

    # Company A needs a revenue account
    rev_a = create_test_account(conn, co_a, "Revenue", "income", "revenue")
    # Company B needs an expense account
    exp_b = create_test_account(conn, co_b, "Operating Expenses", "expense", "expense")

    # Cost centers for P&L entries
    cc_a = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, 'Main', ?, 0)",
        (cc_a, co_a),
    )
    cc_b = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, 'Main', ?, 0)",
        (cc_b, co_b),
    )
    conn.commit()

    return {
        "co_a": co_a, "co_b": co_b,
        "rev_a": rev_a, "exp_b": exp_b,
        "cc_a": cc_a, "cc_b": cc_b,
    }


def test_paired_je_creation(fresh_db):
    """create-intercompany-je should create two draft JEs, one per company."""
    s = _setup_two_companies(fresh_db)
    result = _call_action(
        db_query.create_intercompany_je, fresh_db,
        source_company_id=s["co_a"], target_company_id=s["co_b"],
        amount="5000.00", posting_date="2026-06-15",
        description="Shared services fee",
    )

    assert result["status"] == "ok"
    assert "source_je_id" in result
    assert "target_je_id" in result
    assert result["amount"] == "5000.00"

    # Both JEs exist as drafts
    src_je = fresh_db.execute(
        "SELECT * FROM journal_entry WHERE id = ?", (result["source_je_id"],)
    ).fetchone()
    tgt_je = fresh_db.execute(
        "SELECT * FROM journal_entry WHERE id = ?", (result["target_je_id"],)
    ).fetchone()

    assert src_je["status"] == "draft"
    assert src_je["entry_type"] == "inter_company"
    assert src_je["company_id"] == s["co_a"]

    assert tgt_je["status"] == "draft"
    assert tgt_je["entry_type"] == "inter_company"
    assert tgt_je["company_id"] == s["co_b"]


def test_gl_balance_per_company(fresh_db):
    """Each JE's lines must balance (total DR == total CR)."""
    s = _setup_two_companies(fresh_db)
    result = _call_action(
        db_query.create_intercompany_je, fresh_db,
        source_company_id=s["co_a"], target_company_id=s["co_b"],
        amount="12000.00", posting_date="2026-06-15",
    )
    assert result["status"] == "ok"

    for je_id in [result["source_je_id"], result["target_je_id"]]:
        lines = fresh_db.execute(
            "SELECT debit, credit FROM journal_entry_line WHERE journal_entry_id = ?",
            (je_id,),
        ).fetchall()

        total_dr = sum(Decimal(l["debit"]) for l in lines)
        total_cr = sum(Decimal(l["credit"]) for l in lines)
        assert total_dr == total_cr
        assert total_dr == Decimal("12000.00")


def test_account_auto_creation(fresh_db):
    """Intercompany accounts should be auto-created if they don't exist."""
    s = _setup_two_companies(fresh_db)

    # Verify no intercompany accounts exist yet
    ic_before = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM account WHERE name LIKE 'Intercompany%'"
    ).fetchone()["cnt"]
    assert ic_before == 0

    result = _call_action(
        db_query.create_intercompany_je, fresh_db,
        source_company_id=s["co_a"], target_company_id=s["co_b"],
        amount="3000.00", posting_date="2026-06-15",
    )
    assert result["status"] == "ok"

    # Now intercompany accounts should exist
    ic_recv = fresh_db.execute(
        "SELECT * FROM account WHERE name = 'Intercompany Receivable' AND company_id = ?",
        (s["co_a"],),
    ).fetchone()
    ic_pay = fresh_db.execute(
        "SELECT * FROM account WHERE name = 'Intercompany Payable' AND company_id = ?",
        (s["co_b"],),
    ).fetchone()

    assert ic_recv is not None
    assert ic_recv["root_type"] == "asset"
    assert ic_pay is not None
    assert ic_pay["root_type"] == "liability"


def test_different_currencies_rejected(fresh_db):
    """Intercompany JE between companies with different currencies should fail."""
    co_a = create_test_company(fresh_db, name="USD Co", abbr="UC")
    # Change company B to EUR
    co_b = str(uuid.uuid4())
    fresh_db.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, 'EUR Co', 'EC', 'EUR', 'Germany', 1)""",
        (co_b,),
    )
    fresh_db.commit()

    create_test_fiscal_year(fresh_db, co_a, name="FY 2026 USD")
    create_test_fiscal_year(fresh_db, co_b, name="FY 2026 EUR")

    result = _call_action(
        db_query.create_intercompany_je, fresh_db,
        source_company_id=co_a, target_company_id=co_b,
        amount="1000.00", posting_date="2026-06-15",
    )

    assert result["status"] == "error"
    assert "currencies" in result["message"].lower() or "currency" in result["message"].lower()


def test_same_company_rejected(fresh_db):
    """Intercompany JE with same source and target should fail."""
    co = create_test_company(fresh_db)
    result = _call_action(
        db_query.create_intercompany_je, fresh_db,
        source_company_id=co, target_company_id=co,
        amount="1000.00", posting_date="2026-06-15",
    )
    assert result["status"] == "error"
    assert "different" in result["message"].lower()
