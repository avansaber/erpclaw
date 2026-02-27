"""Shared test fixtures for erpclaw-region-uk tests."""
import json
import os
import sqlite3
import sys
import tempfile
import uuid

import pytest

# Ensure the script directory is importable
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(REPO_DIR, "scripts")
ASSETS_DIR = os.path.join(REPO_DIR, "assets")
sys.path.insert(0, SCRIPT_DIR)


@pytest.fixture
def tmp_db(tmp_path):
    """Create a minimal SQLite DB with required tables for testing."""
    db_path = str(tmp_path / "test_data.sqlite")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create minimal tables needed for UK skill tests
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            country TEXT DEFAULT 'GB',
            region TEXT DEFAULT 'ENG',
            address_line_1 TEXT DEFAULT '',
            city TEXT DEFAULT '',
            postal_code TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS regional_settings (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            account_type TEXT DEFAULT 'asset',
            company_id TEXT,
            parent_account_id TEXT,
            is_group INTEGER DEFAULT 0,
            account_number TEXT DEFAULT '',
            description TEXT DEFAULT '',
            balance TEXT DEFAULT '0',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tax_category (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tax_template (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tax_type TEXT DEFAULT 'both',
            is_default INTEGER DEFAULT 0,
            company_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tax_template_line (
            id TEXT PRIMARY KEY,
            tax_template_id TEXT NOT NULL,
            tax_account_id TEXT NOT NULL,
            rate TEXT DEFAULT '0',
            charge_type TEXT DEFAULT 'on_net_total',
            row_order INTEGER DEFAULT 0,
            add_deduct TEXT DEFAULT 'add',
            included_in_print_rate INTEGER DEFAULT 0,
            description TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sales_invoice (
            id TEXT PRIMARY KEY,
            name TEXT,
            company_id TEXT,
            customer_id TEXT,
            posting_date TEXT,
            net_total TEXT DEFAULT '0',
            total_tax TEXT DEFAULT '0',
            grand_total TEXT DEFAULT '0',
            rounded_total TEXT DEFAULT '0',
            docstatus INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sales_invoice_item (
            id TEXT PRIMARY KEY,
            sales_invoice_id TEXT NOT NULL,
            item_id TEXT,
            item_name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            qty TEXT DEFAULT '0',
            rate TEXT DEFAULT '0',
            amount TEXT DEFAULT '0',
            tax_amount TEXT DEFAULT '0',
            uom TEXT DEFAULT 'EA'
        );

        CREATE TABLE IF NOT EXISTS purchase_invoice (
            id TEXT PRIMARY KEY,
            name TEXT,
            company_id TEXT,
            supplier_id TEXT,
            posting_date TEXT,
            net_total TEXT DEFAULT '0',
            total_tax TEXT DEFAULT '0',
            grand_total TEXT DEFAULT '0',
            docstatus INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS customer (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tax_id TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS supplier (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tax_id TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS employee (
            id TEXT PRIMARY KEY,
            employee_name TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            nino TEXT DEFAULT '',
            student_loan_plan TEXT DEFAULT '',
            company_id TEXT,
            date_of_joining TEXT DEFAULT '',
            date_of_leaving TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS salary_component (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT DEFAULT 'deduction',
            description TEXT DEFAULT '',
            company_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS salary_slip (
            id TEXT PRIMARY KEY,
            employee_id TEXT,
            company_id TEXT,
            posting_date TEXT,
            gross_pay TEXT DEFAULT '0',
            total_deduction TEXT DEFAULT '0',
            net_pay TEXT DEFAULT '0',
            payroll_period TEXT DEFAULT '',
            docstatus INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            skill TEXT,
            action TEXT,
            entity_type TEXT,
            entity_id TEXT,
            old_values TEXT,
            new_values TEXT,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def uk_company(tmp_db):
    """Create a UK test company and return (db_path, company_id)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, country, region) VALUES (?, ?, ?, ?)",
        (company_id, "British Trading Co Ltd", "GB", "ENG"),
    )
    conn.commit()
    conn.close()
    return tmp_db, company_id


@pytest.fixture
def non_uk_company(tmp_db):
    """Create a US test company and return (db_path, company_id)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, country) VALUES (?, ?, ?)",
        (company_id, "Test US Corp", "US"),
    )
    conn.commit()
    conn.close()
    return tmp_db, company_id


@pytest.fixture
def scottish_company(tmp_db):
    """Create a Scottish test company and return (db_path, company_id)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, country, region) VALUES (?, ?, ?, ?)",
        (company_id, "Highland Enterprises Ltd", "GB", "SCO"),
    )
    conn.commit()
    conn.close()
    return tmp_db, company_id


def run_action(db_path, action, **kwargs):
    """Run a db_query.py action and return parsed JSON output."""
    import subprocess
    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, "db_query.py"),
        "--action", action,
        "--db-path", db_path,
    ]
    for key, value in kwargs.items():
        flag = f"--{key.replace('_', '-')}"
        if value is not None:
            cmd.extend([flag, str(value)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"Action '{action}' produced invalid JSON.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}\n"
            f"returncode: {result.returncode}"
        )
    return output, result.returncode
