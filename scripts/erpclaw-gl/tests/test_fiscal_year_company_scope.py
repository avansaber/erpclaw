"""The ledger's fiscal-year checks look at the posting company's years only.

Step 9 refuses a posting unless the POSTING company's own open year covers
the date, even when another company's year does; Step 12 binds the budget
lookup and the spent-to-date window to the Step 9 year instead of
company-less subqueries.
"""
import uuid
from decimal import Decimal

import pytest

from gl_helpers import (
    seed_account,
    seed_company,
    seed_cost_center,
    seed_fiscal_year,
)
from erpclaw_lib.gl_posting import insert_gl_entries
from erpclaw_lib.query_helpers import get_fiscal_year

D = Decimal
POSTING = "2026-03-15"


def _bs_legs(asset_id, liab_id, amount="100.00"):
    return [
        {"account_id": asset_id, "debit": amount, "credit": "0"},
        {"account_id": liab_id, "debit": "0", "credit": amount},
    ]


def _seed_bs_accounts(conn, company_id, asset_name="Cash", liab_name="Payables"):
    asset = seed_account(conn, company_id, asset_name, "asset")
    liab = seed_account(conn, company_id, liab_name, "liability")
    return asset, liab


def _close_year(conn, company_id, name):
    conn.execute(
        "UPDATE fiscal_year SET is_closed = 1 WHERE company_id = ? AND name = ?",
        (company_id, name),
    )
    conn.commit()


def _gl_count(conn, voucher_id):
    return conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ?", (voucher_id,)
    ).fetchone()[0]


def test_other_company_open_year_does_not_open_a_closed_year(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    asset, liab = _seed_bs_accounts(conn, a)
    seed_fiscal_year(conn, a, "FY-A-2026", "2026-01-01", "2026-12-31")
    seed_fiscal_year(conn, b, "FY-B-2026", "2026-01-01", "2026-12-31")
    _close_year(conn, a, "FY-A-2026")
    vid = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        insert_gl_entries(
            conn, _bs_legs(asset, liab),
            voucher_type="journal_entry", voucher_id=vid,
            posting_date=POSTING, company_id=a, remarks="m810 closed stays closed",
        )
    assert str(excinfo.value) == (
        "GL Validation Step 9 Failed: Fiscal year 'FY-A-2026' is closed "
        "-- cannot post to closed fiscal years"
    )
    assert _gl_count(conn, vid) == 0


def test_other_company_year_does_not_cover_a_missing_year(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    c = seed_company(conn, name="C Co", abbr="CC")
    a_asset, a_liab = _seed_bs_accounts(conn, a)
    c_asset, c_liab = _seed_bs_accounts(conn, c)
    seed_fiscal_year(conn, b, "FY-B-2026", "2026-01-01", "2026-12-31")
    for company_id, legs in (
        (a, _bs_legs(a_asset, a_liab)),
        (c, _bs_legs(c_asset, c_liab)),
    ):
        vid = str(uuid.uuid4())
        with pytest.raises(ValueError) as excinfo:
            insert_gl_entries(
                conn, legs,
                voucher_type="journal_entry", voucher_id=vid,
                posting_date=POSTING, company_id=company_id,
                remarks="m810 missing year",
            )
        assert str(excinfo.value) == (
            "GL Validation Step 9 Failed: No open fiscal year found for "
            "posting date 2026-03-15"
        )
        assert _gl_count(conn, vid) == 0


def test_own_open_year_posts(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    a_asset, a_liab = _seed_bs_accounts(conn, a)
    b_asset, b_liab = _seed_bs_accounts(conn, b)
    seed_fiscal_year(conn, a, "FY-A-2026", "2026-01-01", "2026-12-31")
    seed_fiscal_year(conn, b, "FY-B-2026", "2026-01-01", "2026-12-31")
    _close_year(conn, b, "FY-B-2026")
    vid_a = str(uuid.uuid4())
    ids = insert_gl_entries(
        conn, _bs_legs(a_asset, a_liab),
        voucher_type="journal_entry", voucher_id=vid_a,
        posting_date=POSTING, company_id=a, remarks="m810 own year posts",
    )
    assert len(ids) == 2
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_id = ? "
        "ORDER BY CAST(debit AS NUMERIC) DESC",
        (vid_a,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["debit"] == "100.00"
    assert D(rows[0]["debit"]) == D("100.00")
    assert rows[1]["credit"] == "100.00"
    assert D(rows[1]["credit"]) == D("100.00")
    vid_b = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        insert_gl_entries(
            conn, _bs_legs(b_asset, b_liab),
            voucher_type="journal_entry", voucher_id=vid_b,
            posting_date=POSTING, company_id=b, remarks="m810 closed refused",
        )
    assert str(excinfo.value) == (
        "GL Validation Step 9 Failed: Fiscal year 'FY-B-2026' is closed "
        "-- cannot post to closed fiscal years"
    )
    assert _gl_count(conn, vid_b) == 0


def _seed_budget_case(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    seed_fiscal_year(conn, b, "FY-B", "2025-07-01", "2026-06-30")
    seed_fiscal_year(conn, a, "FY-A", "2026-01-01", "2026-12-31")
    expense = seed_account(conn, a, "Office Expense", "expense", "expense")
    cc = seed_cost_center(conn, a, "Main CC")
    asset = seed_account(conn, a, "Cash", "asset", "cash")
    fy_a = conn.execute(
        "SELECT id FROM fiscal_year WHERE company_id = ? AND name = ?",
        (a, "FY-A"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO budget (id, fiscal_year_id, account_id, cost_center_id, "
        "budget_amount, action_if_exceeded, company_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), fy_a, expense, cc, "100.00", "stop", a),
    )
    conn.commit()
    return a, expense, cc, asset


def _pl_legs(expense_id, asset_id, cc_id, amount):
    return [
        {"account_id": expense_id, "debit": amount, "credit": "0",
         "cost_center_id": cc_id},
        {"account_id": asset_id, "debit": "0", "credit": amount},
    ]


def test_budget_uses_own_company_year(conn):
    a, expense, cc, asset = _seed_budget_case(conn)
    vid = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        insert_gl_entries(
            conn, _pl_legs(expense, asset, cc, "150.00"),
            voucher_type="journal_entry", voucher_id=vid,
            posting_date=POSTING, company_id=a, remarks="m810 over budget",
        )
    message = str(excinfo.value)
    assert message.startswith("GL Validation Step 12 Failed: Budget exceeded")
    assert "budget=100.00" in message
    assert "this_posting=150.00" in message
    assert _gl_count(conn, vid) == 0
    vid_ok = str(uuid.uuid4())
    ids = insert_gl_entries(
        conn, _pl_legs(expense, asset, cc, "50.00"),
        voucher_type="journal_entry", voucher_id=vid_ok,
        posting_date=POSTING, company_id=a, remarks="m810 within budget",
    )
    assert len(ids) == 2
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_id = ? "
        "ORDER BY CAST(debit AS NUMERIC) DESC",
        (vid_ok,),
    ).fetchall()
    assert rows[0]["debit"] == "50.00"
    assert D(rows[0]["debit"]) == D("50.00")
    assert rows[1]["credit"] == "50.00"
    assert D(rows[1]["credit"]) == D("50.00")


def test_budget_window_starts_at_own_year(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    seed_fiscal_year(conn, b, "FY-B", "2025-07-01", "2026-06-30")
    seed_fiscal_year(conn, a, "FY-A-2025", "2025-01-01", "2025-12-31")
    seed_fiscal_year(conn, a, "FY-A-2026", "2026-01-01", "2026-12-31")
    expense = seed_account(conn, a, "Office Expense", "expense", "expense")
    cc = seed_cost_center(conn, a, "Main CC")
    asset = seed_account(conn, a, "Cash", "asset", "cash")
    prior = str(uuid.uuid4())
    insert_gl_entries(
        conn, _pl_legs(expense, asset, cc, "800.00"),
        voucher_type="journal_entry", voucher_id=prior,
        posting_date="2025-12-15", company_id=a, remarks="m810 prior year spend",
    )
    fy_2026 = conn.execute(
        "SELECT id FROM fiscal_year WHERE company_id = ? AND name = ?",
        (a, "FY-A-2026"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO budget (id, fiscal_year_id, account_id, cost_center_id, "
        "budget_amount, action_if_exceeded, company_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), fy_2026, expense, cc, "1000.00", "stop", a),
    )
    conn.commit()
    vid = str(uuid.uuid4())
    ids = insert_gl_entries(
        conn, _pl_legs(expense, asset, cc, "500.00"),
        voucher_type="journal_entry", voucher_id=vid,
        posting_date="2026-03-01", company_id=a, remarks="m810 window start",
    )
    assert len(ids) == 2
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_id = ? "
        "ORDER BY CAST(debit AS NUMERIC) DESC",
        (vid,),
    ).fetchall()
    assert rows[0]["debit"] == "500.00"
    assert D(rows[0]["debit"]) == D("500.00")
    assert rows[1]["credit"] == "500.00"
    assert D(rows[1]["credit"]) == D("500.00")


def test_get_fiscal_year_company_scope(conn):
    a = seed_company(conn, name="A Co", abbr="AA")
    b = seed_company(conn, name="B Co", abbr="BB")
    c = seed_company(conn, name="C Co", abbr="CC")
    seed_fiscal_year(conn, a, "FY-A", "2026-01-01", "2026-12-31")
    seed_fiscal_year(conn, b, "FY-B", "2026-01-01", "2026-12-31")
    _close_year(conn, b, "FY-B")
    assert get_fiscal_year(conn, POSTING, company_id=a) == "FY-A"
    assert get_fiscal_year(conn, POSTING, company_id=b) is None
    assert get_fiscal_year(conn, POSTING, company_id=c) is None
    with pytest.raises(ValueError) as exc:
        get_fiscal_year(conn, POSTING, None)
    assert str(exc.value) == "get_fiscal_year needs the posting company"
    with pytest.raises(ValueError) as exc:
        get_fiscal_year(conn, POSTING, "")
    assert str(exc.value) == "get_fiscal_year needs the posting company"
