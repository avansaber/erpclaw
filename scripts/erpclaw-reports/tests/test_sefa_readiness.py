"""Company-scoped award worksheets, exact amounts and read-only execution."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from erpclaw_lib.query import Q, P, Table
from payments_helpers import call_action, ns

ROOT = Path(__file__).resolve().parents[5]
SPEC = importlib.util.spec_from_file_location("sefa_reports", Path(__file__).parents[1] / "db_query.py")
REPORTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTS)
SCHEMA_SPEC = importlib.util.spec_from_file_location("sefa_nonprofit_schema", ROOT / "source/nonprofitclaw/init_db.py")
SCHEMA = importlib.util.module_from_spec(SCHEMA_SPEC)
SCHEMA_SPEC.loader.exec_module(SCHEMA)


def insert(conn, table_name, **values):
    conn.execute(Q.into(Table(table_name)).columns(*values).insert(
        *(P() for _ in values)).get_sql(), tuple(values.values()))


@pytest.fixture
def book(conn, db_path):
    SCHEMA.create_nonprofitclaw_tables(db_path)
    company, other, fy, foreign_fy, grant, foreign_grant = [str(uuid4()) for _ in range(6)]
    for cid, name in ((company, "Community Books"), (other, "Other Books")):
        insert(conn, "company", id=cid, name=name, abbr=cid[:5], default_currency="USD")
    for fid, cid in ((fy, company), (foreign_fy, other)):
        insert(conn, "fiscal_year", id=fid, company_id=cid, name=fid,
               start_date="2026-01-01", end_date="2026-12-31")
    for gid, cid in ((grant, company), (foreign_grant, other)):
        insert(conn, "nonprofitclaw_grant", id=gid, name="Community Award",
               grantor_name="Explicit Agency", company_id=cid, amount="9999.99",
               received_amount="8000.00", spent_amount="7000.00")
    conn.commit()
    return dict(company=company, other=other, fy=fy, foreign_fy=foreign_fy,
                grant=grant, foreign_grant=foreign_grant)


def expense(conn, book, amount, date="2026-04-01", status="approved", **overrides):
    eid = str(uuid4())
    values = dict(id=eid, grant_id=book["grant"], company_id=book["company"],
                  amount=amount, expense_date=date, status=status)
    values.update(overrides)
    insert(conn, "nonprofitclaw_grant_expense", **values)
    conn.commit()
    return eid


def award(book, **overrides):
    return dict(grant_id=book["grant"], agency_name="Explicit Agency",
                assistance_listing_number="12.345", award_identifier="Award A",
                **overrides)


def run(conn, book, awards=None, **overrides):
    values = dict(company_id=book["company"], fiscal_year_id=book["fy"],
                  federal_awards=json.dumps([award(book)] if awards is None else awards))
    values.update(overrides)
    return call_action(REPORTS.sefa_readiness_report, conn, ns(**values))


def test_exact_period_status_and_company_totals(conn, book):
    included = [expense(conn, book, "0.10"), expense(conn, book, "0.20"),
                expense(conn, book, "1000.01", date="2026-01-01"),
                expense(conn, book, "2.03", date="2026-12-31")]
    for date in ("2025-12-31", "2027-01-01"):
        expense(conn, book, "900.00", date=date)
    for status in ("draft", "submitted", "rejected"):
        expense(conn, book, "900.00", status=status)
    expense(conn, book, "900.00", company_id=book["other"], grant_id=book["foreign_grant"])
    result = run(conn, book)
    assert result["status"] == "ok"
    assert result["ready_for_review"] is True
    assert result["total_expenditures"] == "1002.34"
    assert Decimal(result["awards"][0]["expenditures"]) == Decimal("1002.34")
    assert set(result["awards"][0]["expense_ids"]) == set(included)
    assert result["awards"][0]["expense_count"] == 4
    assert result["currency"] == "USD"


def test_unclassified_expenses_are_visible_not_inferred_federal(conn, book):
    expense(conn, book, "123.45")
    result = run(conn, book, awards=[])
    assert result["status"] == "ok"
    assert result["total_expenditures"] == "0.00"
    assert result["ready_for_review"] is False
    assert result["warnings"][0]["code"] == "unclassified_approved_expenses"
    assert result["warnings"][0]["amount"] == "123.45"


def test_missing_metadata_is_visible(conn, book):
    result = run(conn, book, awards=[{"grant_id": book["grant"]}])
    assert result["ready_for_review"] is False
    assert result["warnings"][0]["fields"] == ["agency_name", "assistance_listing_number", "award_identifier"]


@pytest.mark.parametrize("field", ["fiscal_year_id", "federal_awards"])
def test_foreign_scope_refused_without_writes(conn, book, db_path, field):
    overrides = ({field: book["foreign_fy"]} if field == "fiscal_year_id"
                 else {field: json.dumps([{**award(book), "grant_id": book["foreign_grant"]}])})
    before = Path(db_path).read_bytes()
    assert run(conn, book, **overrides)["status"] == "error"
    assert Path(db_path).read_bytes() == before


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", "not money", "1E100"])
def test_invalid_expense_is_a_clean_refusal(conn, book, db_path, value):
    expense(conn, book, value)
    before = Path(db_path).read_bytes()
    assert run(conn, book)["status"] == "error"
    assert Path(db_path).read_bytes() == before


@pytest.mark.parametrize("value", ["{}", "null", "bad json", '[{"grant_id":1}]'])
def test_invalid_award_map_refused(conn, book, value):
    assert run(conn, book, federal_awards=value)["status"] == "error"


def test_duplicate_award_refused(conn, book):
    assert run(conn, book, awards=[award(book), award(book)])["status"] == "error"


def test_missing_source_is_not_a_zero_expenditure_claim(conn):
    cid, fid = str(uuid4()), str(uuid4())
    insert(conn, "company", id=cid, name="Foundation Only", abbr="FO")
    insert(conn, "fiscal_year", id=fid, name=fid, company_id=cid,
           start_date="2026-01-01", end_date="2026-12-31")
    conn.commit()
    result = run(conn, {"company": cid, "fy": fid}, awards=[])
    assert result["status"] == "ok"
    assert result["source_available"] is False
    assert result["total_expenditures"] is None
    assert result["ready_for_review"] is False


def test_real_readonly_router_answers_twice_without_writes(conn, book, db_path, tmp_path):
    expense(conn, book, "500.55")
    install = tmp_path / "install"
    install.mkdir()
    snapshot = install / "data.sqlite"
    # The same compacted rollback-journal snapshot used by the read-only sweep.
    conn.execute("VACUUM INTO ?", (str(snapshot),))
    snapshot.chmod(0o444)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    home = tmp_path / "home"
    home.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict({k: v for k, v in os.environ.items() if not k.startswith("ERPCLAW_")},
               ERPCLAW_HOME=str(install),
               ERPCLAW_DB_READONLY="1", ERPCLAW_DB_PATH=str(snapshot),
               PYTHONDONTWRITEBYTECODE="1", HOME=str(home), TMPDIR=str(scratch),
               PYTHONPATH=str(ROOT / "source/erpclaw/scripts/erpclaw-setup/lib"))
    argv = [sys.executable, str(ROOT / "source/erpclaw/scripts/db_query.py"),
            "--action", "sefa-readiness-report", "--company-id", book["company"],
            "--fiscal-year-id", book["fy"], "--federal-awards", json.dumps([award(book)])]
    for _ in range(2):
        proc = subprocess.run(argv, env=env, cwd=scratch, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["total_expenditures"] == "500.55"
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
        assert snapshot.stat().st_mode & 0o777 == 0o444
        assert sorted(p.name for p in install.iterdir()) == ["data.sqlite"]
        assert not list(home.rglob("*"))
        assert not list(scratch.rglob("*"))
