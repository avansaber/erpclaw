"""Financial statement CSV preserves company scope and exact amounts."""
import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest

from payments_helpers import call_action, ns

MODULE = os.path.dirname(os.path.dirname(__file__))
SPEC = importlib.util.spec_from_file_location("csv_reports", os.path.join(MODULE, "db_query.py"))
REPORTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTS)


def _id():
    return str(uuid.uuid4())


@pytest.fixture
def book(conn):
    company = _id()
    other = _id()
    for cid, name in ((company, "CSV Books"), (other, "Other Books")):
        conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)", (cid, name, cid[:8]))
        conn.execute("INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
                     "VALUES (?, ?, '2026-01-01', '2026-12-31', ?)", (_id(), cid, cid))
        voucher = _id()
        for name, root, debit, credit in (
            ('Cash, "main"', "asset", "100000000000000.27", "0"),
            ("=SUM(1,2)", "income", "0", "100000000000000.27"),
        ):
            account = _id()
            conn.execute("INSERT INTO account (id, name, account_number, root_type, "
                         "balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, 0)",
                         (account, name if cid == company else "Other company account", account[:8],
                          root, "debit_normal" if root == "asset" else "credit_normal", cid))
            conn.execute("INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                         "debit_base, credit_base, currency, exchange_rate, voucher_type, voucher_id, "
                         "entry_set, is_cancelled) VALUES (?, '2026-02-01', ?, ?, ?, ?, ?, "
                         "'USD', '1', 'journal_entry', ?, 'primary', 0)",
                         (_id(), account, debit, credit, debit, credit, voucher))
    conn.commit()
    return company


def _args(company, action):
    return ns(action=action, company_id=company, from_date="2026-01-01",
              to_date="2026-12-31", as_of_date="2026-12-31", account_id=None,
              party_type=None, party_id=None, voucher_type=None, limit="100", offset="0")


@pytest.mark.parametrize("action", sorted(REPORTS._CSV_REPORTS))
def test_csv_statements_keep_exact_money_company_scope_and_books(conn, book, action):
    before = [tuple(row) for row in conn.execute("SELECT * FROM gl_entry ORDER BY id").fetchall()]
    result = call_action(REPORTS.export_financial_csv, conn, _args(book, action))
    assert result["status"] == "ok", result
    assert result["report"] == action and result["format"] == "csv"
    rows = list(csv.DictReader(io.StringIO(result["csv"])))
    assert len(rows) == result["row_count"]
    assert "Other company account" not in result["csv"]
    assert "100000000000000.27" in result["csv"]
    assert Decimal("100000000000000.27") == Decimal(next(
        value for row in rows for value in row.values() if value == "100000000000000.27"))
    assert before == [tuple(row) for row in conn.execute("SELECT * FROM gl_entry ORDER BY id").fetchall()]


def test_csv_quotes_names_and_neutralises_formula_cells(conn, book):
    result = call_action(REPORTS.export_financial_csv, conn, _args(book, "trial-balance"))
    names = {row.get("account_name") for row in csv.DictReader(io.StringIO(result["csv"]))}
    assert 'Cash, "main"' in names
    assert "'=SUM(1,2)" in names
    assert REPORTS._csv_cell("-25.03") == "-25.03"
    assert REPORTS._csv_cell("  @SUM(1,2)") == "'  @SUM(1,2)"


def test_csv_preserves_report_refusal_without_success_envelope(conn, book):
    args = _args(book, "profit-and-loss")
    args.from_date = None
    result = call_action(REPORTS.export_financial_csv, conn, args)
    assert result["status"] == "error"
    assert "from-date" in result["message"]
    assert "csv" not in result


def test_csv_rejects_unsupported_actions(conn, book):
    result = call_action(REPORTS.export_financial_csv, conn, _args(book, "status"))
    assert result["status"] == "error" and "CSV export supports" in result["message"]


def test_csv_is_available_through_the_foundation_router(db_path, conn, book):
    router = os.path.join(os.path.dirname(MODULE), "db_query.py")
    environment = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(MODULE), "erpclaw-setup", "lib"))
    process = subprocess.run([sys.executable, router, "--action", "trial-balance", "--format", "csv",
                              "--db-path", db_path, "--company-id", book, "--to-date", "2026-12-31"],
                             capture_output=True, text=True, env=environment, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["format"] == "csv"
    assert "100000000000000.27" in result["csv"]
