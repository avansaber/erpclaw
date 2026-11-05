"""Revision 0008 on older books: each account gets its root's normal side, nothing else changes, no account is added,
and a lookup for a role those books do not hold refuses in the owner's words, writing nothing."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import drop_everything
from erpclaw_core import ledger
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.migrate import upgrade
from test_revision_0006 import _rows

NOW = "2026-10-26 12:00:00+00:00"
SIDE = {"asset": "debit", "expense": "debit", "liability": "credit", "equity": "credit", "income": "credit"}

# accounts on all five roots and all six older roles: (id, number, root, role, is group, parent)
OLDER = [
    ("a1000", "1000", "asset", None, True, None),
    ("a1100", "1100", "asset", "bank", False, "a1000"),
    ("a1200", "1200", "asset", "receivable", False, "a1000"),
    ("a2000", "2000", "liability", None, True, None),
    ("a2100", "2100", "liability", "payable", False, "a2000"),
    ("a3100", "3100", "equity", "retained_earnings", False, None),
    ("a4100", "4100", "income", "income", False, None),
    ("a4130", "4130", "income", None, False, None),
    ("a6900", "6900", "expense", "rounding", False, None),
]
WORDS = {
    "stock": "inventory",
    "cost_of_goods_sold": "cost of goods sold",
    "stock_received_not_billed": "stock received not billed",
    "sales_returns": "sales returns",
    "exchange_gain_loss_realised": "realized exchange gain or loss",
    "exchange_gain_loss_unrealised": "unrealized exchange gain or loss",
}


@pytest.fixture
def older(engine):
    """Books as an engine at revision 0007 left them: one company and its accounts, frozen 4100 included."""
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0007")
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES ('c1', 'Old Co', 'USD', 1, :now)"), {"now": NOW})
        for aid, number, root, role, group, parent in OLDER:
            conn.execute(text("INSERT INTO account (id, company_id, number, name, root_type, account_type, is_group, "
                              "parent_id, frozen) VALUES (:id, 'c1', :n, :name, :root, :role, :g, :p, :f)"),
                         {"id": aid, "n": number, "name": f"Account {number}", "root": root, "role": role, "g": group,
                          "p": parent, "f": number == "4100"})
        before = _rows(conn)
    return before


def test_each_older_account_gets_its_roots_side_and_nothing_else_changes(engine, older):
    with engine.begin() as conn:
        upgrade(conn, "0008")  # this revision only: later ones add their own tables
        after = _rows(conn)
        defs = dict(conn.execute(text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                                      "WHERE conrelid = 'account'::regclass AND contype = 'c'")).all())
    accounts = after.pop("account")
    assert len(accounts) == len(OLDER)  # no account added
    root_of = {aid: root for aid, _, root, _, _, _ in OLDER}
    assert {a["id"]: a["normal_side"] for a in accounts} == {aid: SIDE[root] for aid, root in root_of.items()}
    assert [{k: v for k, v in a.items() if k != "normal_side"} for a in accounts] == older["account"]
    for name in ("alembic_version", "account"):
        older.pop(name)
    after.pop("alembic_version")
    assert after == older  # every other row exactly as it was
    assert {"ck_account_type", "ck_account_role_side", "ck_account_normal_side"} <= set(defs)
    assert "exchange_gain_loss_unrealised" in defs["ck_account_type"]


def test_an_older_company_gets_no_new_account_and_each_new_role_refuses_in_words(engine, older):
    with engine.begin() as conn:
        upgrade(conn, "0010")  # Current lookup uses the pre-format-2 head, before the new refusal.
    with engine.connect() as conn:
        ctx = company_ctx(conn, company_id="c1")
        before = _rows(conn)
        for role, words in WORDS.items():
            with pytest.raises(Refusal) as e:
                ledger.account_by_type(ctx, role)
            assert (e.value.code, e.value.message) == (f"no_{role}_account", f"Old Co has no {words} account.")
        assert _rows(conn) == before  # nothing written
        conn.rollback()


def test_an_older_frozen_role_account_still_refuses_as_frozen(engine, older):
    """4100 is frozen and the only income account: the lookup names it, as posting to it always did."""
    with engine.begin() as conn:
        upgrade(conn, "0010")  # Current lookup uses the pre-format-2 head, before the new refusal.
    with engine.connect() as conn:
        with pytest.raises(Refusal) as e:
            ledger.account_by_type(company_ctx(conn, company_id="c1"), "income")
    assert (e.value.code, e.value.message) == ("frozen_account", "4100 Account 4100 is frozen.")
