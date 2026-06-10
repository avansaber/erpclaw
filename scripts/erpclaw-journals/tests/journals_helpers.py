"""Shared helper functions for ERPClaw Journals unit tests.

Journal-entry actions are normally exercised at the foundation contract tier
(see README.md). This local harness exists for the S3 CWIP JE hook (AVA-43),
which is sub-skill logic that routes a journal entry's CWIP debit leg into a
cwip_cost_accumulation row in the submit transaction.
"""
import argparse
import importlib.util
import io
import json
import os
import sqlite3
import sys
import uuid
from decimal import Decimal
from unittest.mock import patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib.db import setup_pragmas


def load_db_query():
    db_query_path = os.path.join(MODULE_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_journals", db_query_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def init_all_tables(db_path: str):
    spec = importlib.util.spec_from_file_location("init_schema", INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


class _DecimalSum:
    def __init__(self):
        self.total = Decimal("0")
    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))
    def finalize(self):
        return str(self.total)


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    setup_pragmas(conn)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    return conn


def call_action(fn, conn, args) -> dict:
    buf = io.StringIO()
    def _fake_exit(code=0):
        raise SystemExit(code)
    try:
        with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
            fn(conn, args)
    except SystemExit:
        pass
    output = buf.getvalue().strip()
    if not output:
        return {"status": "error", "message": "no output captured"}
    return json.loads(output)


def ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def is_error(result: dict) -> bool:
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    return result.get("status") == "ok"


def _uuid() -> str:
    return str(uuid.uuid4())


def seed_account(conn, company_id, name, root_type, account_type, number):
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        "balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (aid, name, number, root_type, account_type, direction, company_id))
    conn.commit()
    return aid


def build_journals_env(conn) -> dict:
    cid = _uuid()
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, f"Test Co {cid[:6]}", f"TC{cid[:4]}"))
    fyid = _uuid()
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', ?)",
        (fyid, f"FY-{fyid[:6]}", cid))
    conn.execute(
        "INSERT OR IGNORE INTO naming_series (id, entity_type, prefix, current_value, "
        "company_id) VALUES (?, 'journal_entry', 'JE-', 0, ?)", (_uuid(), cid))
    ccid = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, 'Main CC', ?, 0)",
        (ccid, cid))
    conn.commit()
    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    cwip = seed_account(conn, cid, "CWIP", "asset", "capital_work_in_progress", "1800")
    expense = seed_account(conn, cid, "Purchases", "expense", "expense", "5000")
    return {"company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
            "cash": cash, "cwip": cwip, "expense": expense}


def seed_cwip_asset(conn, company_id, status="under_construction"):
    cat_id = _uuid()
    conn.execute(
        "INSERT INTO asset_category (id, name, company_id) VALUES (?, ?, ?)",
        (cat_id, f"Buildings {cat_id[:6]}", company_id))
    asset_id = _uuid()
    conn.execute(
        "INSERT INTO asset (id, asset_name, naming_series, asset_category_id, "
        "gross_value, current_book_value, status, company_id) "
        "VALUES (?, ?, ?, ?, '0', '0', ?, ?)",
        (asset_id, "Project Beta Plant", "ASSET-CWIP-J1", cat_id, status, company_id))
    conn.commit()
    return asset_id
