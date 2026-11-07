"""The chart: every account states its normal balance, the stock and exchange roles are fixed to their root and side,
a new company is seeded with the accounts stock books need, and a lookup by role never chooses."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from erpclaw_core import backup as bk, ledger, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.setup import create_company
from test_backup import _restored, restored_databases  # noqa: F401 (the fixture removes the restore)

ROOTS = {"asset": "debit", "liability": "credit", "equity": "credit", "income": "credit", "expense": "debit"}
OTHER_ROOT = {"asset": "liability", "liability": "asset", "equity": "asset", "income": "expense", "expense": "income"}
FLIP = {"debit": "credit", "credit": "debit"}

# number: (name, root, role, is group, parent, normal side), as a new company holds them
SEEDED = {
    "1000": ("Assets", "asset", None, True, None, "debit"),
    "1100": ("Bank", "asset", "bank", False, "1000", "debit"),
    "1200": ("Accounts Receivable", "asset", "receivable", False, "1000", "debit"),
    "1300": ("Inventory", "asset", "stock", False, "1000", "debit"),
    "2000": ("Liabilities", "liability", None, True, None, "credit"),
    "2100": ("Accounts Payable", "liability", "payable", False, "2000", "credit"),
    "2300": ("Stock Received Not Billed", "liability", "stock_received_not_billed", False, "2000", "credit"),
    "2310": ("Asset Received Not Billed", "liability", None, False, "2000", "credit"),
    "3000": ("Equity", "equity", None, True, None, "credit"),
    "3100": ("Retained Earnings", "equity", "retained_earnings", False, "3000", "credit"),
    "3900": ("Opening Balance Equity", "equity", "opening_balance", False, "3000", "credit"),
    "4000": ("Income", "income", None, True, None, "credit"),
    "4100": ("Sales", "income", "income", False, "4000", "credit"),
    "4130": ("Sales Returns", "income", "sales_returns", False, "4000", "debit"),
    "5000": ("Cost of Goods Sold", "expense", None, True, None, "debit"),
    "5100": ("Cost of Goods Sold", "expense", "cost_of_goods_sold", False, "5000", "debit"),
    "5200": ("Stock Adjustment", "expense", "stock_adjustment", False, "5000", "debit"),
    "5210": ("Inventory Write-offs", "expense", "stock_write_off", False, "5000", "debit"),
    "6000": ("Expenses", "expense", None, True, None, "debit"),
    "6810": ("Realized Exchange Gain/Loss", "expense", "exchange_gain_loss_realised", False, "6000", "debit"),
    "6820": ("Unrealized Exchange Gain/Loss", "expense", "exchange_gain_loss_unrealised", False, "6000", "debit"),
    "6900": ("Rounding", "expense", "rounding", False, "6000", "debit"),
}


def _company(engine, name="Chart Co") -> str:
    return create_company(engine, name)


def _account(conn, cid, number, root, role=None, **kw) -> str:
    aid = new_id()
    conn.execute(m.account.insert().values(id=aid, company_id=cid, number=number, name=f"Account {number}",
                                           root_type=root, account_type=role, is_group=False, **kw))
    return aid


def _side(conn, aid) -> str:
    return conn.execute(select(m.account.c.normal_side).where(m.account.c.id == aid)).scalar_one()


def _refused(engine, cid, number, root, role, side, constraint):
    with pytest.raises(IntegrityError, match=constraint):
        with engine.begin() as conn:
            _account(conn, cid, number, root, role, normal_side=side)


# --- the seed --------------------------------------------------------------------------------------------------------

def test_a_new_company_holds_the_seeded_chart_with_each_accounts_side(engine):
    cid = _company(engine)
    with engine.connect() as conn:
        a = m.account
        rows = conn.execute(select(a).where(a.c.company_id == cid)).mappings().all()
    number_of = {r["id"]: r["number"] for r in rows}
    got = {r["number"]: (r["name"], r["root_type"], r["account_type"], r["is_group"],
                         number_of.get(r["parent_id"]), r["normal_side"]) for r in rows}
    assert got == SEEDED
    assert not any(r["frozen"] for r in rows)


# --- the column and its default ----------------------------------------------------------------------------------

@pytest.mark.parametrize("root", sorted(ROOTS))
def test_an_insert_that_leaves_the_side_out_gets_its_roots(engine, root):
    cid = _company(engine)
    with engine.begin() as conn:
        assert _side(conn, _account(conn, cid, "8000", root)) == ROOTS[root]


def test_the_default_is_per_row_in_one_insert_many_rows_and_multi_row_values(engine):
    cid = _company(engine)
    mixed = [("8001", "asset"), ("8002", "income"), ("8003", "expense"), ("8004", "liability"), ("8005", "equity")]

    def row(number, root):
        return {"id": new_id(), "company_id": cid, "number": number, "name": number, "root_type": root,
                "account_type": None, "is_group": False}

    with engine.begin() as conn:
        conn.execute(insert(m.account), [row(n, r) for n, r in mixed])  # executemany
        conn.execute(insert(m.account).values([row("9" + n[1:], r) for n, r in mixed]))  # one multi-row VALUES
        conn.execute(insert(m.account).values(row("7000", "income")))  # a single row
        got = dict(conn.execute(select(m.account.c.number, m.account.c.normal_side).where(
            m.account.c.company_id == cid, m.account.c.number.in_(
                [n for n, _ in mixed] + ["9" + n[1:] for n, _ in mixed] + ["7000"]))).all())
    want = {n: ROOTS[r] for n, r in mixed} | {"9" + n[1:]: ROOTS[r] for n, r in mixed} | {"7000": "credit"}
    assert got == want


def test_a_raw_insert_that_leaves_the_side_out_is_refused(engine):
    cid = _company(engine)
    with pytest.raises(IntegrityError, match="normal_side"):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO account (id, company_id, number, name, root_type, is_group, frozen) "
                              "VALUES (:id, :c, '8000', 'Raw', 'asset', false, false)"), {"id": new_id(), "c": cid})


@pytest.mark.parametrize("side", ["Debit", "dr", "", "both"])
def test_a_side_other_than_debit_or_credit_is_refused(engine, side):
    _refused(engine, _company(engine), "8000", "asset", None, side, "ck_account_normal_side")


def test_an_account_without_a_role_may_state_either_side(engine):
    """A contra account (accumulated depreciation, say) needs no role: a credit-normal asset is accepted."""
    cid = _company(engine)
    with engine.begin() as conn:
        assert _side(conn, _account(conn, cid, "1590", "asset", normal_side="credit")) == "credit"
        assert _side(conn, _account(conn, cid, "4190", "income", normal_side="debit")) == "debit"


# --- the roles -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(m.ROLE_SIDES))
def test_each_fixed_role_is_refused_on_another_root_or_side_and_accepted_on_its_own(engine, role):
    root, side = m.ROLE_SIDES[role]
    cid = _company(engine)
    _refused(engine, cid, "8000", OTHER_ROOT[root], role, side, "ck_account_role_side")  # its side, another root
    _refused(engine, cid, "8000", root, role, FLIP[side], "ck_account_role_side")  # its root, the other side
    with engine.begin() as conn:
        aid = _account(conn, cid, "8000", root, role, normal_side=side)
        assert conn.execute(select(m.account.c.account_type).where(m.account.c.id == aid)).scalar_one() == role


def test_the_longest_role_is_stored_whole_and_an_unknown_role_is_refused(engine):
    cid = _company(engine)
    with engine.begin() as conn:
        aid = _account(conn, cid, "8000", "liability", "stock_received_not_billed")
        stored = conn.execute(select(m.account.c.account_type).where(m.account.c.id == aid)).scalar_one()
    assert stored == "stock_received_not_billed" and len(stored) == 25
    _refused(engine, cid, "8001", "asset", "inventory", "debit", "ck_account_type")


def test_each_account_check_in_the_books_is_the_one_the_models_declare(engine):
    """The books are made by the revisions; each check, as PostgreSQL normalises it, equals the models' own check
    put on a scratch table of the same column types."""
    checks = {c.name: str(c.sqltext) for c in m.account.constraints
              if c.name in ("ck_account_type", "ck_account_role_side", "ck_account_normal_side")}
    assert len(checks) == 3
    with engine.connect() as conn:
        defs = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = :t ::regclass AND conname = :n"
        conn.execute(text("CREATE TEMP TABLE scratch_account (root_type varchar(10), account_type varchar(40), "
                          "normal_side varchar(6))"))
        for name, sql in checks.items():
            conn.execute(text(f"ALTER TABLE scratch_account ADD CONSTRAINT {name} CHECK ({sql})"))
            books = conn.execute(text(defs), {"t": "account", "n": name}).scalar_one()
            declared = conn.execute(text(defs), {"t": "scratch_account", "n": name}).scalar_one()
            assert books == declared, name
        conn.rollback()


# --- lookup by role ------------------------------------------------------------------------------------------------

def _lookup(engine, cid, role, table_order=False):
    with engine.connect() as conn:
        if table_order:  # read the table itself, not the (company, number) index, which returns number order anyway
            for scan in ("indexscan", "indexonlyscan", "bitmapscan"):
                conn.execute(text(f"SET LOCAL enable_{scan} = off"))
        return ledger.account_by_type(company_ctx(conn, company_id=cid), role)


def _refusal(engine, cid, role, table_order=False) -> Refusal:
    with pytest.raises(Refusal) as e:
        _lookup(engine, cid, role, table_order)
    return e.value


def test_a_lookup_by_role_returns_the_one_account_or_refuses_without_choosing(engine):
    cid = _company(engine)
    with engine.begin() as conn:
        seeded = conn.execute(select(m.account.c.id).where(m.account.c.company_id == cid,
                                                           m.account.c.number == "1300")).scalar_one()
    assert _lookup(engine, cid, "stock")["id"] == seeded  # one

    with engine.begin() as conn:  # only frozen: the lowest-numbered is named, whatever order the rows were made in
        conn.execute(update(m.account).where(m.account.c.id == seeded).values(frozen=True))
        _account(conn, cid, "1310", "asset", "stock", frozen=True)
        _account(conn, cid, "1290", "asset", "stock", frozen=True)
    r = _refusal(engine, cid, "stock", table_order=True)
    assert (r.code, r.message) == ("frozen_account", "1290 Account 1290 is frozen.")

    with engine.begin() as conn:  # one frozen and one not: the one not frozen
        live = _account(conn, cid, "1320", "asset", "stock")
    assert _lookup(engine, cid, "stock")["id"] == live

    with engine.begin() as conn:  # two not frozen: never the first row found
        _account(conn, cid, "1330", "asset", "stock")
    r = _refusal(engine, cid, "stock")
    assert (r.code, r.message) == ("several_stock_accounts",
                                "Chart Co has more than one inventory account; say which one.")

    with engine.begin() as conn:  # none at all
        conn.execute(update(m.account).where(m.account.c.company_id == cid, m.account.c.account_type
                                             == "cost_of_goods_sold").values(account_type=None))
    r = _refusal(engine, cid, "cost_of_goods_sold")
    assert (r.code, r.message) == ("no_cost_of_goods_sold_account", "Chart Co has no cost of goods sold account.")


def test_a_group_account_is_never_returned_by_a_lookup(engine):
    cid = _company(engine)
    with engine.begin() as conn:
        conn.execute(insert(m.account).values(id=new_id(), company_id=cid, number="1390", name="Stock group",
                                              root_type="asset", account_type="stock", is_group=True))
    assert _lookup(engine, cid, "stock")["number"] == "1300"


def test_the_exchange_accounts_are_found_by_their_roles(engine):
    cid = _company(engine)
    assert _lookup(engine, cid, "exchange_gain_loss_realised")["number"] == "6810"
    assert _lookup(engine, cid, "exchange_gain_loss_unrealised")["number"] == "6820"


# --- backup and restore -----------------------------------------------------------------------------------------

def test_a_backup_and_restore_carries_each_accounts_side(engine, tmp_path, restored_databases):
    cid = _company(engine)
    bk.backup(engine, tmp_path / "b")
    restored = _restored(tmp_path / "b")
    assert bk.book_diff(engine, restored) == []
    with restored.connect() as conn:
        sides = dict(conn.execute(select(m.account.c.number, m.account.c.normal_side).where(
            m.account.c.company_id == cid)).all())
    assert sides == {n: row[5] for n, row in SEEDED.items()}
