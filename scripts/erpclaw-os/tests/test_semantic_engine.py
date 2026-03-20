#!/usr/bin/env python3
"""Tests for ERPClaw OS Semantic Correctness Engine (Deliverable 3a).

Tests account classification, posting pattern, and period validation rules.
Verifies findings are persisted, status workflow, and no false positives
on existing modules.
"""
import json
import os
import sqlite3
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.db import setup_pragmas

from semantic_engine import (
    DEFAULT_RULES,
    _check_account_classification,
    _check_period_validation,
    _check_posting_patterns,
    _record_finding,
    _seed_default_rules,
    ensure_semantic_tables,
    handle_semantic_check,
    handle_semantic_rules_list,
    list_semantic_rules,
    semantic_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path, name="semantic_test.sqlite"):
    """Create a test DB with company, account, and gl_entry tables."""
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company (
            id TEXT PRIMARY KEY, name TEXT
        );
        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY,
            name TEXT,
            root_type TEXT NOT NULL CHECK(root_type IN ('asset','liability','equity','income','expense')),
            account_type TEXT,
            company_id TEXT REFERENCES company(id)
        );
        CREATE TABLE IF NOT EXISTS gl_entry (
            id TEXT PRIMARY KEY,
            posting_date TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES account(id),
            party_type TEXT,
            party_id TEXT,
            debit TEXT NOT NULL DEFAULT '0',
            credit TEXT NOT NULL DEFAULT '0',
            voucher_type TEXT NOT NULL,
            voucher_id TEXT NOT NULL,
            entry_set TEXT NOT NULL DEFAULT 'primary',
            remarks TEXT,
            is_cancelled INTEGER NOT NULL DEFAULT 0
        );

        INSERT INTO company (id, name) VALUES ('c1', 'Test Corp');

        -- Standard chart of accounts
        INSERT INTO account VALUES ('a-revenue', 'Sales Revenue', 'income', 'revenue', 'c1');
        INSERT INTO account VALUES ('a-cogs', 'Cost of Goods Sold', 'expense', 'cost_of_goods_sold', 'c1');
        INSERT INTO account VALUES ('a-receivable', 'Accounts Receivable', 'asset', 'receivable', 'c1');
        INSERT INTO account VALUES ('a-payable', 'Accounts Payable', 'liability', 'payable', 'c1');
        INSERT INTO account VALUES ('a-bank', 'Bank Account', 'asset', 'bank', 'c1');
        INSERT INTO account VALUES ('a-expense', 'Office Supplies', 'expense', 'expense', 'c1');
        INSERT INTO account VALUES ('a-equity', 'Retained Earnings', 'equity', 'equity', 'c1');
        INSERT INTO account VALUES ('a-liability', 'Unearned Revenue', 'liability', 'payable', 'c1');
        INSERT INTO account VALUES ('a-stock', 'Inventory', 'asset', 'stock', 'c1');
        INSERT INTO account VALUES ('a-depreciation', 'Depreciation Expense', 'expense', 'depreciation', 'c1');
        INSERT INTO account VALUES ('a-accum-dep', 'Accumulated Depreciation', 'asset', 'accumulated_depreciation', 'c1');
    """)
    conn.commit()
    return path, conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Create a fresh test DB with semantic tables seeded."""
    path, conn = _make_db(tmp_path)
    ensure_semantic_tables(conn)
    _seed_default_rules(conn)
    yield path, conn
    conn.close()


@pytest.fixture
def conn(db):
    """Return just the connection from db fixture."""
    return db[1]


@pytest.fixture
def db_path(db):
    """Return just the path from db fixture."""
    return db[0]


# ---------------------------------------------------------------------------
# Test: Seed Default Rules
# ---------------------------------------------------------------------------

class TestSeedDefaultRules:
    """Test that default semantic rules are properly seeded."""

    def test_seed_creates_rules(self, tmp_path):
        path, conn = _make_db(tmp_path, "seed_test.sqlite")
        ensure_semantic_tables(conn)
        _seed_default_rules(conn)
        count = conn.execute("SELECT COUNT(*) FROM erpclaw_semantic_rule").fetchone()[0]
        assert count == len(DEFAULT_RULES)
        conn.close()

    def test_seed_idempotent(self, conn):
        """Seeding twice does not duplicate rules."""
        _seed_default_rules(conn)
        count = conn.execute("SELECT COUNT(*) FROM erpclaw_semantic_rule").fetchone()[0]
        assert count == len(DEFAULT_RULES)

    def test_all_categories_present(self, conn):
        categories = {r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM erpclaw_semantic_rule"
        ).fetchall()}
        assert categories == {"account_classification", "posting_pattern", "period_validation"}

    def test_all_severities_present(self, conn):
        severities = {r[0] for r in conn.execute(
            "SELECT DISTINCT severity FROM erpclaw_semantic_rule"
        ).fetchall()}
        assert "critical" in severities
        assert "warning" in severities


# ---------------------------------------------------------------------------
# Test: List Semantic Rules
# ---------------------------------------------------------------------------

class TestListSemanticRules:
    """Test semantic-rules-list action."""

    def test_returns_all_active_rules(self, conn):
        rules = list_semantic_rules(conn)
        assert len(rules) == len(DEFAULT_RULES)
        for rule in rules:
            assert rule["is_active"] == 1

    def test_deactivated_rule_excluded(self, conn):
        conn.execute(
            "UPDATE erpclaw_semantic_rule SET is_active = 0 "
            "WHERE rule_name = 'revenue_account_root_type'"
        )
        conn.commit()
        rules = list_semantic_rules(conn)
        assert len(rules) == len(DEFAULT_RULES) - 1
        names = {r["rule_name"] for r in rules}
        assert "revenue_account_root_type" not in names

    def test_handle_semantic_rules_list(self, db_path):
        class Args:
            pass
        args = Args()
        args.db_path = db_path
        result = handle_semantic_rules_list(args)
        assert "rules" in result
        assert result["count"] == len(DEFAULT_RULES)
        assert "by_category" in result


# ---------------------------------------------------------------------------
# Test: Account Classification — Revenue to Income
# ---------------------------------------------------------------------------

class TestAccountClassificationRevenue:
    """Revenue must post to Income-type accounts."""

    def test_revenue_to_income_no_finding(self, conn):
        """Sales invoice crediting revenue (income) account is correct."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-receivable', '100.00', '0', "
            "'sales_invoice', 'inv-1', 'primary', 0)"
        )
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g2', '2026-03-01', 'a-revenue', '0', '100.00', "
            "'sales_invoice', 'inv-1', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        # Filter for this specific rule
        rev_findings = [f for f in findings if f["rule_name"] == "revenue_account_root_type"]
        assert len(rev_findings) == 0

    def test_revenue_to_expense_critical_finding(self, conn):
        """Sales invoice crediting expense account (COGS) is a critical finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-receivable', '200.00', '0', "
            "'sales_invoice', 'inv-bad', 'primary', 0)"
        )
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g2', '2026-03-01', 'a-cogs', '0', '200.00', "
            "'sales_invoice', 'inv-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        rev_findings = [f for f in findings if f["rule_name"] == "revenue_account_root_type"]
        assert len(rev_findings) == 1
        assert rev_findings[0]["severity"] == "critical"
        assert rev_findings[0]["evidence"]["actual_type"] == "expense"

    def test_cogs_entry_set_excluded(self, conn):
        """COGS entry_set credits to expense are intentional and excluded."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-cogs', '0', '50.00', "
            "'sales_invoice', 'inv-cogs', 'cogs', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        rev_findings = [f for f in findings if f["rule_name"] == "revenue_account_root_type"]
        assert len(rev_findings) == 0


# ---------------------------------------------------------------------------
# Test: Account Classification — Expense to Expense
# ---------------------------------------------------------------------------

class TestAccountClassificationExpense:
    """Expense must post to Expense-type accounts."""

    def test_expense_to_expense_no_finding(self, conn):
        """Purchase invoice debiting expense account is correct."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-expense', '150.00', '0', "
            "'purchase_invoice', 'pi-1', 'primary', 0)"
        )
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g2', '2026-03-01', 'a-payable', '0', '150.00', "
            "'purchase_invoice', 'pi-1', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        exp_findings = [f for f in findings if f["rule_name"] == "expense_account_root_type"]
        assert len(exp_findings) == 0

    def test_expense_to_income_critical_finding(self, conn):
        """Purchase invoice debiting income account is a critical finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '150.00', '0', "
            "'purchase_invoice', 'pi-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        exp_findings = [f for f in findings if f["rule_name"] == "expense_account_root_type"]
        assert len(exp_findings) == 1
        assert exp_findings[0]["severity"] == "critical"
        assert exp_findings[0]["evidence"]["actual_type"] == "income"


# ---------------------------------------------------------------------------
# Test: Account Classification — Asset to Asset
# ---------------------------------------------------------------------------

class TestAccountClassificationAsset:
    """Asset acquisition must debit Asset-type accounts."""

    def test_asset_to_asset_no_finding(self, conn):
        """Stock entry debiting stock (asset) account is correct."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-stock', '500.00', '0', "
            "'stock_entry', 'se-1', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        asset_findings = [f for f in findings if f["rule_name"] == "asset_account_root_type"]
        assert len(asset_findings) == 0

    def test_asset_to_liability_critical_finding(self, conn):
        """Stock entry debiting liability account is a critical finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-payable', '500.00', '0', "
            "'stock_entry', 'se-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_account_classification(conn, "testmod")
        asset_findings = [f for f in findings if f["rule_name"] == "asset_account_root_type"]
        assert len(asset_findings) == 1
        assert asset_findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# Test: Posting Patterns — Sales Invoice
# ---------------------------------------------------------------------------

class TestPostingPatternSalesInvoice:
    """Sales invoice revenue must credit Income, not Expense."""

    def test_correct_sales_invoice_pattern(self, conn):
        """Revenue credited to income account — no finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '0', '300.00', "
            "'sales_invoice', 'si-ok', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        si_findings = [f for f in findings if f["rule_name"] == "sales_invoice_revenue_pattern"]
        assert len(si_findings) == 0

    def test_sales_invoice_revenue_to_cogs_critical(self, conn):
        """Revenue credited to COGS (expense) — critical posting pattern finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-cogs', '0', '300.00', "
            "'sales_invoice', 'si-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        si_findings = [f for f in findings if f["rule_name"] == "sales_invoice_revenue_pattern"]
        assert len(si_findings) == 1
        assert si_findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# Test: Posting Patterns — Purchase Invoice
# ---------------------------------------------------------------------------

class TestPostingPatternPurchaseInvoice:
    """Purchase invoice expense must debit Expense, not Income."""

    def test_correct_purchase_invoice_pattern(self, conn):
        """Expense debited to expense account — no finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-expense', '250.00', '0', "
            "'purchase_invoice', 'pi-ok', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        pi_findings = [f for f in findings if f["rule_name"] == "purchase_invoice_expense_pattern"]
        assert len(pi_findings) == 0

    def test_purchase_invoice_expense_to_revenue_critical(self, conn):
        """Expense debited to revenue (income) — critical finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '250.00', '0', "
            "'purchase_invoice', 'pi-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        pi_findings = [f for f in findings if f["rule_name"] == "purchase_invoice_expense_pattern"]
        assert len(pi_findings) == 1
        assert pi_findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# Test: Posting Patterns — Payment
# ---------------------------------------------------------------------------

class TestPostingPatternPayment:
    """Payment entries must debit/credit receivable/payable correctly."""

    def test_customer_payment_debits_receivable_no_finding(self, conn):
        """Customer payment touching receivable account — no finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, party_type, "
            "debit, credit, voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-receivable', 'customer', "
            "'0', '100.00', 'payment_entry', 'pay-ok', 'primary', 0)"
        )
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, party_type, "
            "debit, credit, voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g2', '2026-03-01', 'a-bank', 'customer', "
            "'100.00', '0', 'payment_entry', 'pay-ok', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        pay_findings = [f for f in findings if f["rule_name"] == "payment_receivable_pattern"]
        assert len(pay_findings) == 0

    def test_customer_payment_to_revenue_warning(self, conn):
        """Customer payment crediting revenue account directly — warning."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, party_type, "
            "debit, credit, voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', 'customer', "
            "'0', '100.00', 'payment_entry', 'pay-bad', 'primary', 0)"
        )
        conn.commit()
        findings = _check_posting_patterns(conn, "testmod")
        pay_findings = [f for f in findings if f["rule_name"] == "payment_receivable_pattern"]
        assert len(pay_findings) == 1
        assert pay_findings[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test: Period Validation — Revenue Before Service Date
# ---------------------------------------------------------------------------

class TestPeriodValidationRevenueTiming:
    """Revenue cannot be recognized before service date (ASC 606)."""

    def test_revenue_before_service_date_warning(self, conn):
        """Revenue recognized before obligation satisfaction — warning."""
        # Create revenue_contract and performance_obligation tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS revenue_contract (
                id TEXT PRIMARY KEY, name TEXT
            );
            CREATE TABLE IF NOT EXISTS performance_obligation (
                id TEXT PRIMARY KEY,
                contract_id TEXT REFERENCES revenue_contract(id),
                satisfaction_date TEXT
            );
            INSERT INTO revenue_contract VALUES ('rc-1', 'Service Contract');
            INSERT INTO performance_obligation VALUES ('po-1', 'rc-1', '2026-06-01');
        """)
        # Revenue GL entry posted before satisfaction date
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '0', '500.00', "
            "'sales_invoice', 'rc-1', 'primary', 0)"
        )
        conn.commit()
        findings = _check_period_validation(conn, "testmod")
        timing_findings = [f for f in findings if f["rule_name"] == "revenue_before_service_date"]
        assert len(timing_findings) == 1
        assert timing_findings[0]["severity"] == "warning"
        assert "ASC 606" in timing_findings[0]["description"]

    def test_revenue_after_service_date_no_finding(self, conn):
        """Revenue recognized after satisfaction — no finding."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS revenue_contract (
                id TEXT PRIMARY KEY, name TEXT
            );
            CREATE TABLE IF NOT EXISTS performance_obligation (
                id TEXT PRIMARY KEY,
                contract_id TEXT REFERENCES revenue_contract(id),
                satisfaction_date TEXT
            );
            INSERT INTO revenue_contract VALUES ('rc-2', 'Delivered Service');
            INSERT INTO performance_obligation VALUES ('po-2', 'rc-2', '2026-01-01');
        """)
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '0', '500.00', "
            "'sales_invoice', 'rc-2', 'primary', 0)"
        )
        conn.commit()
        findings = _check_period_validation(conn, "testmod")
        timing_findings = [f for f in findings if f["rule_name"] == "revenue_before_service_date"]
        assert len(timing_findings) == 0


# ---------------------------------------------------------------------------
# Test: Period Validation — Prepayment to Liability
# ---------------------------------------------------------------------------

class TestPeriodValidationPrepayment:
    """Prepayments must post to liability accounts."""

    def test_prepayment_to_revenue_critical(self, conn):
        """Advance payment to revenue (income) instead of liability — critical."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled, remarks) "
            "VALUES ('g1', '2026-03-01', 'a-revenue', '0', '1000.00', "
            "'payment_entry', 'pay-adv', 'primary', 0, 'advance payment from customer')"
        )
        conn.commit()
        findings = _check_period_validation(conn, "testmod")
        prepay_findings = [f for f in findings if f["rule_name"] == "prepayment_to_liability"]
        assert len(prepay_findings) == 1
        assert prepay_findings[0]["severity"] == "critical"
        assert "liability" in prepay_findings[0]["description"].lower()

    def test_prepayment_to_liability_no_finding(self, conn):
        """Advance payment to liability account — no finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled, remarks) "
            "VALUES ('g1', '2026-03-01', 'a-liability', '0', '1000.00', "
            "'payment_entry', 'pay-adv-ok', 'primary', 0, 'advance payment from customer')"
        )
        conn.commit()
        findings = _check_period_validation(conn, "testmod")
        prepay_findings = [f for f in findings if f["rule_name"] == "prepayment_to_liability"]
        assert len(prepay_findings) == 0


# ---------------------------------------------------------------------------
# Test: Finding Persistence
# ---------------------------------------------------------------------------

class TestFindingPersistence:
    """Test that findings are persisted to erpclaw_semantic_finding table."""

    def test_findings_persisted(self, conn):
        """semantic_check persists findings to DB."""
        # Create a misposted GL entry
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g1', '2026-03-01', 'a-cogs', '0', '200.00', "
            "'sales_invoice', 'inv-persist', 'primary', 0)"
        )
        conn.commit()

        findings = semantic_check(conn, "testmod")
        assert len(findings) > 0

        count = conn.execute(
            "SELECT COUNT(*) FROM erpclaw_semantic_finding WHERE module_name = 'testmod'"
        ).fetchone()[0]
        assert count > 0

    def test_finding_has_evidence_json(self, conn):
        """Persisted findings contain evidence JSON with required fields."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-ev', '2026-03-01', 'a-cogs', '0', '200.00', "
            "'sales_invoice', 'inv-ev', 'primary', 0)"
        )
        conn.commit()

        semantic_check(conn, "testmod")

        row = conn.execute(
            "SELECT evidence FROM erpclaw_semantic_finding "
            "WHERE module_name = 'testmod' LIMIT 1"
        ).fetchone()
        assert row is not None
        evidence = json.loads(row[0])
        assert "gl_entry_id" in evidence
        assert "account_id" in evidence
        assert "expected_type" in evidence
        assert "actual_type" in evidence

    def test_finding_default_status_open(self, conn):
        """New findings default to 'open' status."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-st', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-st', 'primary', 0)"
        )
        conn.commit()

        semantic_check(conn, "testmod")

        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM erpclaw_semantic_finding WHERE module_name = 'testmod'"
        ).fetchall()]
        assert all(s == "open" for s in statuses)


# ---------------------------------------------------------------------------
# Test: Finding Status Workflow
# ---------------------------------------------------------------------------

class TestFindingStatusWorkflow:
    """Test finding status transitions: open -> acknowledged -> resolved."""

    def test_open_to_acknowledged(self, conn):
        """Finding can be moved from open to acknowledged."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-wf1', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-wf1', 'primary', 0)"
        )
        conn.commit()
        semantic_check(conn, "testmod")

        finding_id = conn.execute(
            "SELECT id FROM erpclaw_semantic_finding WHERE module_name = 'testmod' LIMIT 1"
        ).fetchone()[0]

        conn.execute(
            "UPDATE erpclaw_semantic_finding SET status = 'acknowledged' WHERE id = ?",
            (finding_id,),
        )
        conn.commit()

        status = conn.execute(
            "SELECT status FROM erpclaw_semantic_finding WHERE id = ?",
            (finding_id,),
        ).fetchone()[0]
        assert status == "acknowledged"

    def test_acknowledged_to_resolved(self, conn):
        """Finding can be moved from acknowledged to resolved."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-wf2', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-wf2', 'primary', 0)"
        )
        conn.commit()
        semantic_check(conn, "testmod")

        finding_id = conn.execute(
            "SELECT id FROM erpclaw_semantic_finding WHERE module_name = 'testmod' LIMIT 1"
        ).fetchone()[0]

        conn.execute(
            "UPDATE erpclaw_semantic_finding SET status = 'resolved', "
            "resolved_at = datetime('now'), resolved_by = 'admin' WHERE id = ?",
            (finding_id,),
        )
        conn.commit()

        row = conn.execute(
            "SELECT status, resolved_at, resolved_by FROM erpclaw_semantic_finding WHERE id = ?",
            (finding_id,),
        ).fetchone()
        assert row[0] == "resolved"
        assert row[1] is not None
        assert row[2] == "admin"


# ---------------------------------------------------------------------------
# Test: False Positive Status
# ---------------------------------------------------------------------------

class TestFalsePositiveStatus:
    """Test false_positive status for findings."""

    def test_mark_as_false_positive(self, conn):
        """Finding can be marked as false_positive."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-fp', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-fp', 'primary', 0)"
        )
        conn.commit()
        semantic_check(conn, "testmod")

        finding_id = conn.execute(
            "SELECT id FROM erpclaw_semantic_finding WHERE module_name = 'testmod' LIMIT 1"
        ).fetchone()[0]

        conn.execute(
            "UPDATE erpclaw_semantic_finding SET status = 'false_positive' WHERE id = ?",
            (finding_id,),
        )
        conn.commit()

        status = conn.execute(
            "SELECT status FROM erpclaw_semantic_finding WHERE id = ?",
            (finding_id,),
        ).fetchone()[0]
        assert status == "false_positive"


# ---------------------------------------------------------------------------
# Test: Deactivated Rule Not Checked
# ---------------------------------------------------------------------------

class TestDeactivatedRule:
    """Deactivated rules should not produce findings."""

    def test_deactivated_rule_skipped(self, conn):
        """Deactivating a rule means it produces no findings."""
        # Create a misposted entry that would normally trigger a finding
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-da', '2026-03-01', 'a-cogs', '0', '999.00', "
            "'sales_invoice', 'inv-da', 'primary', 0)"
        )
        conn.commit()

        # Verify it would produce findings when active
        findings_before = _check_account_classification(conn, "testmod")
        rev_before = [f for f in findings_before if f["rule_name"] == "revenue_account_root_type"]
        assert len(rev_before) > 0

        # Deactivate the rule
        conn.execute(
            "UPDATE erpclaw_semantic_rule SET is_active = 0 "
            "WHERE rule_name = 'revenue_account_root_type'"
        )
        conn.commit()

        # Now check again — should produce no findings for that rule
        findings_after = _check_account_classification(conn, "testmod")
        rev_after = [f for f in findings_after if f["rule_name"] == "revenue_account_root_type"]
        assert len(rev_after) == 0


# ---------------------------------------------------------------------------
# Test: Operation Phantom Profit (subscription immediate revenue)
# ---------------------------------------------------------------------------

class TestOperationPhantomProfit:
    """Subscription module posting immediate revenue instead of deferred."""

    def test_phantom_profit_detected(self, conn):
        """Advance payment posted to revenue instead of deferred — critical finding."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled, remarks) "
            "VALUES ('g-pp', '2026-03-01', 'a-revenue', '0', '1200.00', "
            "'payment_entry', 'sub-pay-1', 'primary', 0, "
            "'advance payment for annual subscription')"
        )
        conn.commit()

        findings = semantic_check(conn, "subclaw")
        prepay_findings = [f for f in findings if f["rule_name"] == "prepayment_to_liability"]
        assert len(prepay_findings) == 1
        assert prepay_findings[0]["severity"] == "critical"
        assert prepay_findings[0]["evidence"]["actual_type"] == "income"
        assert prepay_findings[0]["evidence"]["expected_type"] == "liability"


# ---------------------------------------------------------------------------
# Test: Evidence JSON Structure
# ---------------------------------------------------------------------------

class TestEvidenceStructure:
    """Verify evidence JSON contains required fields."""

    def test_evidence_has_all_fields(self, conn):
        """Evidence must contain gl_entry_id, account_id, expected_type, actual_type."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-evi', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-evi', 'primary', 0)"
        )
        conn.commit()

        findings = semantic_check(conn, "testmod")
        assert len(findings) > 0

        for finding in findings:
            evidence = finding.get("evidence", {})
            assert "gl_entry_id" in evidence, f"Missing gl_entry_id in {finding['rule_name']}"
            assert "account_id" in evidence, f"Missing account_id in {finding['rule_name']}"
            assert "expected_type" in evidence, f"Missing expected_type in {finding['rule_name']}"
            assert "actual_type" in evidence, f"Missing actual_type in {finding['rule_name']}"


# ---------------------------------------------------------------------------
# Test: Full semantic-check Action (CLI handler)
# ---------------------------------------------------------------------------

class TestHandleSemanticCheck:
    """Test the semantic-check CLI handler."""

    def test_clean_module_passes(self, db_path):
        """Module with no GL entries passes clean."""
        class Args:
            pass
        args = Args()
        args.module_name = "clean-module"
        args.db_path = db_path
        result = handle_semantic_check(args)
        assert result["result"] == "pass"
        assert result["finding_count"] == 0

    def test_module_with_issues_fails(self, db_path, conn):
        """Module with misposted GL entries fails."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-cli', '2026-03-01', 'a-cogs', '0', '100.00', "
            "'sales_invoice', 'inv-cli', 'primary', 0)"
        )
        conn.commit()

        class Args:
            pass
        args = Args()
        args.module_name = "bad-module"
        args.db_path = db_path
        result = handle_semantic_check(args)
        assert result["result"] == "fail"
        assert result["severity_counts"]["critical"] > 0
        assert "duration_ms" in result

    def test_missing_module_name_error(self):
        class Args:
            pass
        args = Args()
        args.module_name = None
        args.db_path = None
        result = handle_semantic_check(args)
        assert "error" in result

    def test_checks_run_list(self, db_path):
        class Args:
            pass
        args = Args()
        args.module_name = "testmod"
        args.db_path = db_path
        result = handle_semantic_check(args)
        assert set(result["checks_run"]) == {
            "account_classification", "posting_pattern", "period_validation"
        }


# ---------------------------------------------------------------------------
# Test: No GL tables (graceful handling)
# ---------------------------------------------------------------------------

class TestNoGLTables:
    """Engine handles missing tables gracefully."""

    def test_no_gl_entry_table(self, tmp_path):
        """DB without gl_entry table returns no findings."""
        path = str(tmp_path / "empty.sqlite")
        conn = sqlite3.connect(path)
        setup_pragmas(conn)
        conn.execute("CREATE TABLE test (id TEXT)")
        ensure_semantic_tables(conn)
        _seed_default_rules(conn)
        conn.commit()

        findings = semantic_check(conn, "testmod")
        assert findings == []
        conn.close()

    def test_no_account_table(self, tmp_path):
        """DB without account table returns no findings."""
        path = str(tmp_path / "no_acct.sqlite")
        conn = sqlite3.connect(path)
        setup_pragmas(conn)
        conn.executescript("""
            CREATE TABLE gl_entry (
                id TEXT PRIMARY KEY, account_id TEXT, voucher_type TEXT,
                voucher_id TEXT, debit TEXT, credit TEXT, entry_set TEXT,
                is_cancelled INTEGER, posting_date TEXT
            );
        """)
        ensure_semantic_tables(conn)
        _seed_default_rules(conn)
        conn.commit()

        findings = semantic_check(conn, "testmod")
        assert findings == []
        conn.close()


# ---------------------------------------------------------------------------
# Test: Cancelled GL entries excluded
# ---------------------------------------------------------------------------

class TestCancelledEntriesExcluded:
    """Cancelled GL entries should not produce findings."""

    def test_cancelled_entry_ignored(self, conn):
        """is_cancelled=1 entries are skipped by all checks."""
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id, entry_set, is_cancelled) "
            "VALUES ('g-can', '2026-03-01', 'a-cogs', '0', '500.00', "
            "'sales_invoice', 'inv-can', 'primary', 1)"
        )
        conn.commit()
        findings = semantic_check(conn, "testmod")
        # Should be no findings for cancelled entries
        cancelled_findings = [
            f for f in findings
            if f.get("evidence", {}).get("voucher_id") == "inv-can"
        ]
        assert len(cancelled_findings) == 0


# ---------------------------------------------------------------------------
# Test: Existing modules pass clean (no false positives)
# ---------------------------------------------------------------------------

# All 43 registered modules should pass semantic-check clean when the DB
# has no GL entries (the engine should not produce false positives from
# an empty data set).

ALL_MODULES = [
    "agricultureclaw", "automotiveclaw", "constructclaw",
    "educlaw", "educlaw-finaid", "educlaw-highered", "educlaw-k12",
    "educlaw-lms", "educlaw-scheduling", "educlaw-statereport",
    "erpclaw", "erpclaw-alerts", "erpclaw-approvals", "erpclaw-compliance",
    "erpclaw-documents", "erpclaw-esign", "erpclaw-fleet", "erpclaw-growth",
    "erpclaw-integrations", "erpclaw-loans", "erpclaw-logistics",
    "erpclaw-maintenance", "erpclaw-ops", "erpclaw-planning", "erpclaw-pos",
    "erpclaw-region-ca", "erpclaw-region-eu", "erpclaw-region-in",
    "erpclaw-region-uk", "erpclaw-selfservice", "erpclaw-treasury",
    "foodclaw", "healthclaw", "healthclaw-dental", "healthclaw-homehealth",
    "healthclaw-mental", "healthclaw-vet", "hospitalityclaw", "legalclaw",
    "nonprofitclaw", "propertyclaw", "propertyclaw-commercial", "retailclaw",
]


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_existing_module_no_false_positives(tmp_path, module_name):
    """All 43 registered modules pass semantic-check clean on empty DB."""
    path, conn = _make_db(tmp_path, f"{module_name}_test.sqlite")
    ensure_semantic_tables(conn)
    _seed_default_rules(conn)
    conn.commit()

    findings = semantic_check(conn, module_name)
    conn.close()
    assert findings == [], (
        f"Module '{module_name}' produced {len(findings)} false positive(s) "
        f"on an empty DB: {[f['description'] for f in findings]}"
    )
