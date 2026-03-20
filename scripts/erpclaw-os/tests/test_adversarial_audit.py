#!/usr/bin/env python3
"""Tests for ERPClaw OS Adversarial Audit Agent (Deliverable 2e).

Tests GL balance checks, account anchoring, revenue recognition patterns,
and audit finding persistence.
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

from adversarial_audit import (
    check_account_anchoring,
    check_gl_balance_invariant,
    check_revenue_recognition,
    ensure_audit_tables,
    record_finding,
    run_audit,
    handle_run_audit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Create a DB with GL data for audit testing."""
    path = str(tmp_path / "audit_test.sqlite")
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company (
            id TEXT PRIMARY KEY, name TEXT
        );
        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY, name TEXT, root_type TEXT, account_type TEXT,
            company_id TEXT REFERENCES company(id)
        );
        CREATE TABLE IF NOT EXISTS gl_entry (
            id TEXT PRIMARY KEY,
            account_id TEXT REFERENCES account(id),
            voucher_id TEXT,
            voucher_type TEXT,
            debit_amount TEXT DEFAULT '0.00',
            credit_amount TEXT DEFAULT '0.00',
            company_id TEXT REFERENCES company(id),
            posting_date TEXT
        );
        INSERT INTO company (id, name) VALUES ('c1', 'Test Corp');
        INSERT INTO account (id, name, root_type, account_type, company_id)
            VALUES ('a1', 'Sales Revenue', 'income', 'revenue', 'c1');
        INSERT INTO account (id, name, root_type, account_type, company_id)
            VALUES ('a2', 'Accounts Receivable', 'asset', 'receivable', 'c1');
        INSERT INTO account (id, name, root_type, account_type, company_id)
            VALUES ('a3', 'Cost of Goods Sold', 'expense', 'cost_of_goods_sold', 'c1');
        INSERT INTO account (id, name, root_type, account_type, company_id)
            VALUES ('a4', 'Office Supplies', 'expense', 'expense', 'c1');
    """)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def balanced_gl(db_path):
    """Add balanced GL entries."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g1', 'a2', 'inv-1', 'Sales Invoice', '100.00', '0.00', 'c1');
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g2', 'a1', 'inv-1', 'Sales Invoice', '0.00', '100.00', 'c1');
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def unbalanced_gl(db_path):
    """Add unbalanced GL entries."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g1', 'a2', 'inv-bad', 'Sales Invoice', '100.00', '0.00', 'c1');
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g2', 'a1', 'inv-bad', 'Sales Invoice', '0.00', '95.00', 'c1');
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def misposted_gl(db_path):
    """Revenue credited to expense account (semantic mismatch)."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g1', 'a2', 'inv-mis', 'Sales Invoice', '200.00', '0.00', 'c1');
        INSERT INTO gl_entry (id, account_id, voucher_id, voucher_type, debit_amount, credit_amount, company_id)
            VALUES ('g2', 'a3', 'inv-mis', 'Sales Invoice', '0.00', '200.00', 'c1');
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def subscription_module_no_deferred(tmp_path):
    """Module with subscription tables but no deferred revenue pattern."""
    mod_dir = tmp_path / "subclaw"
    mod_dir.mkdir()
    scripts_dir = mod_dir / "scripts"
    scripts_dir.mkdir()

    (mod_dir / "init_db.py").write_text("""
import sqlite3
def create_module_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sub_subscription (
            id TEXT PRIMARY KEY, plan TEXT, recurring_amount TEXT
        );
    ''')
    conn.commit()
    conn.close()
""")
    (scripts_dir / "billing.py").write_text("""
# Posts subscription revenue immediately
def post_revenue(amount):
    return {"account": "revenue", "amount": amount}
""")
    return str(mod_dir)


@pytest.fixture
def subscription_module_with_deferred(tmp_path):
    """Module with subscription tables AND deferred revenue pattern."""
    mod_dir = tmp_path / "defclaw"
    mod_dir.mkdir()
    scripts_dir = mod_dir / "scripts"
    scripts_dir.mkdir()

    (mod_dir / "init_db.py").write_text("""
import sqlite3
def create_module_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS def_subscription (
            id TEXT PRIMARY KEY, plan TEXT, recurring_amount TEXT
        );
    ''')
    conn.commit()
    conn.close()
""")
    (scripts_dir / "billing.py").write_text("""
# Uses deferred_revenue pattern
def post_revenue(amount):
    return {"account": "deferred_revenue", "amount": amount}
""")
    return str(mod_dir)


# ---------------------------------------------------------------------------
# GL Balance Tests
# ---------------------------------------------------------------------------

class TestGLBalanceInvariant:
    """Test GL balance check (debits == credits)."""

    def test_balanced_gl_passes(self, balanced_gl):
        findings = check_gl_balance_invariant(balanced_gl)
        assert len(findings) == 0

    def test_unbalanced_gl_detected(self, unbalanced_gl):
        findings = check_gl_balance_invariant(unbalanced_gl)
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"
        assert "imbalance" in findings[0]["description"].lower()

    def test_no_gl_table(self, tmp_path):
        """DB without gl_entry table returns no findings."""
        db = str(tmp_path / "empty.sqlite")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE test (id TEXT)")
        conn.commit()
        conn.close()
        findings = check_gl_balance_invariant(db)
        assert findings == []

    def test_nonexistent_db(self, tmp_path):
        findings = check_gl_balance_invariant(str(tmp_path / "nope.sqlite"))
        assert findings == []


# ---------------------------------------------------------------------------
# Account Anchoring Tests
# ---------------------------------------------------------------------------

class TestAccountAnchoring:
    """Test semantic account correctness checks."""

    def test_correct_posting_passes(self, balanced_gl):
        """Revenue credited to income account is correct."""
        findings = check_account_anchoring(balanced_gl)
        assert len(findings) == 0

    def test_revenue_to_expense_account_flagged(self, misposted_gl):
        """Revenue credited to COGS (expense) is flagged."""
        findings = check_account_anchoring(misposted_gl)
        assert len(findings) > 0
        assert any("expense" in f["description"].lower() for f in findings)


# ---------------------------------------------------------------------------
# Revenue Recognition Tests
# ---------------------------------------------------------------------------

class TestRevenueRecognition:
    """Test revenue recognition pattern detection."""

    def test_subscription_without_deferred_flagged(self, subscription_module_no_deferred):
        findings = check_revenue_recognition(subscription_module_no_deferred)
        assert len(findings) == 1
        assert "deferred" in findings[0]["description"].lower()
        assert findings[0]["severity"] == "warning"

    def test_subscription_with_deferred_passes(self, subscription_module_with_deferred):
        findings = check_revenue_recognition(subscription_module_with_deferred)
        assert len(findings) == 0

    def test_no_module_returns_empty(self):
        findings = check_revenue_recognition(None)
        assert findings == []

    def test_module_without_subscriptions_passes(self, tmp_path):
        """Module with no subscription tables passes."""
        mod_dir = tmp_path / "plainmod"
        mod_dir.mkdir()
        (mod_dir / "init_db.py").write_text("""
import sqlite3
def create_module_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS plain_item (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
""")
        findings = check_revenue_recognition(str(mod_dir))
        assert findings == []


# ---------------------------------------------------------------------------
# Finding Persistence Tests
# ---------------------------------------------------------------------------

class TestFindingPersistence:
    """Test audit finding recording."""

    def test_ensure_audit_tables(self, db_path):
        ensure_audit_tables(db_path)
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "erpclaw_audit_finding" in tables
        assert "erpclaw_compliance_period" in tables

    def test_record_finding(self, db_path):
        finding_id = record_finding(
            module_name="testmod",
            finding_type="semantic",
            severity="critical",
            description="Test finding",
            db_path=db_path,
        )
        assert finding_id is not None

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT module_name, severity, status FROM erpclaw_audit_finding WHERE id = ?",
            (finding_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "testmod"
        assert row[1] == "critical"
        assert row[2] == "open"


# ---------------------------------------------------------------------------
# Full Audit Tests
# ---------------------------------------------------------------------------

class TestRunAudit:
    """Test full audit run."""

    def test_clean_audit_passes(self, balanced_gl):
        result = run_audit(db_path=balanced_gl)
        assert result["result"] == "pass"
        assert result["finding_count"] == 0

    def test_audit_with_issues(self, unbalanced_gl):
        result = run_audit(db_path=unbalanced_gl)
        assert result["result"] == "fail"
        assert result["finding_count"] > 0
        assert result["severity_counts"]["critical"] > 0

    def test_audit_records_findings(self, unbalanced_gl):
        result = run_audit(db_path=unbalanced_gl)
        # Check findings were persisted
        conn = sqlite3.connect(unbalanced_gl)
        count = conn.execute(
            "SELECT COUNT(*) FROM erpclaw_audit_finding"
        ).fetchone()[0]
        conn.close()
        assert count > 0

    def test_audit_has_duration(self, balanced_gl):
        result = run_audit(db_path=balanced_gl)
        assert "duration_ms" in result

    def test_handle_run_audit(self, balanced_gl):
        class Args:
            module_path = None
        args = Args()
        args.db_path = balanced_gl
        result = handle_run_audit(args)
        assert result["result"] == "pass"
