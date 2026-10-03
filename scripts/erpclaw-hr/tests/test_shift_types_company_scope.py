"""Company scope for list-shift-types (task m791)."""
import io
import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from hr_helpers import call_action, is_ok, load_db_query, ns  # noqa: E402
from erpclaw_lib.query import Q, Table  # noqa: E402

H = load_db_query()

ZERO_REFUSAL = {
    "status": "error",
    "error": "No company found. Create one first.",
    "message": "No company found. Create one first.",
    "suggestion": "Run 'tutorial' to create a demo company, or 'setup company' to create your own.",
}

UNKNOWN_REFUSAL = {
    "status": "error",
    "error": "Company not found: no-such-company",
    "message": "Company not found: no-such-company",
}

MULTI_ERROR = "Multiple companies found. Please specify the company by name."
MULTI_SUGGESTION = "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."


def _insert_company(conn, name, abbr):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, fiscal_year_start_month)"
        " VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, name, abbr),
    )
    conn.commit()
    return cid


def _add_shift(conn, company_id, name):
    r = call_action(H.add_shift_type, conn, ns(
        name=name, start_time="08:00", end_time="16:00",
        company_id=company_id, status="active", limit=None, offset=None))
    assert is_ok(r), r
    return r["shift_type_id"]


def _seed_two(conn):
    acme = _insert_company(conn, "Acme Widgets", "ACME")
    wayne = _insert_company(conn, "Wayne Enterprises", "WAYNE")
    _add_shift(conn, acme, "Acme Day")
    _add_shift(conn, wayne, "Wayne Night")
    return {"acme": acme, "wayne": wayne}


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _state(conn):
    return {name: _all(conn, name) for name in ("shift_type", "company", "audit_log")}


def _call(conn, company_id=None, company_name=None):
    return call_action(H.list_shift_types, conn, ns(
        company_id=company_id, company_name=company_name, status=None,
        limit=None, offset=None))


def _multi_refusal(acme, wayne):
    return {
        "status": "error",
        "error": MULTI_ERROR,
        "message": MULTI_ERROR,
        "companies": [
            {"id": acme, "name": "Acme Widgets"},
            {"id": wayne, "name": "Wayne Enterprises"},
        ],
        "suggestion": MULTI_SUGGESTION,
    }


def test_zero_companies_refuses(conn):
    before = _state(conn)
    r = _call(conn, company_id=None, company_name=None)
    assert r == ZERO_REFUSAL, r
    assert _state(conn) == before


def test_two_companies_no_company_refuses(conn):
    ids = _seed_two(conn)
    before = _state(conn)
    r = _call(conn, company_id=None, company_name=None)
    assert r == _multi_refusal(ids["acme"], ids["wayne"]), r
    assert _state(conn) == before


def test_unknown_company_refuses(conn):
    _seed_two(conn)
    before = _state(conn)
    r = _call(conn, company_id="no-such-company", company_name=None)
    assert r == UNKNOWN_REFUSAL, r
    assert _state(conn) == before


def test_explicit_second_company_scopes(conn):
    ids = _seed_two(conn)
    r = _call(conn, company_id=ids["wayne"], company_name=None)
    assert is_ok(r), r
    assert [s["name"] for s in r["shift_types"]] == ["Wayne Night"]


def test_one_company_uses_it(conn):
    acme = _insert_company(conn, "Acme Widgets", "ACME")
    _add_shift(conn, acme, "Acme Day")
    bare = _call(conn, company_id=None, company_name=None)
    scoped = _call(conn, company_id=acme, company_name=None)
    assert is_ok(bare), bare
    assert bare == scoped


def test_list_shift_types_by_company_name(conn):
    ids = _seed_two(conn)
    r = _call(conn, company_id=None, company_name="Wayne Enterprises")
    assert is_ok(r), r
    assert [s["name"] for s in r["shift_types"]] == ["Wayne Night"]


def test_company_flag_resolves_name(conn):
    ids = _seed_two(conn)
    wayne = ids["wayne"]
    args = ns(company_id=None, company_name="wayne enterprises")
    H._resolve_company_flag(conn, args)
    assert args.company_id == wayne
    args2 = ns(company_id=None, company_name=wayne)
    H._resolve_company_flag(conn, args2)
    assert args2.company_id == wayne
    buf = io.StringIO()

    def _fake_exit(code=0):
        raise SystemExit(code)

    with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
        try:
            H._resolve_company_flag(
                conn, ns(company_id=None, company_name="Wayne"))
        except SystemExit:
            pass
    got = json.loads(buf.getvalue().strip())
    assert got == {
        "status": "error",
        "error": "Company 'Wayne' not found.",
        "message": "Company 'Wayne' not found.",
        "available_companies": ["Acme Widgets", "Wayne Enterprises"],
        "suggestion": "Use one of the available company names exactly, or run 'list-companies' to see them.",
    }, got
