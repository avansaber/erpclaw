"""Exact-money foreign-currency revaluation (task m630).

Money is text: Decimal in Python, TEXT columns. The balance legs of a
foreign-currency account must be summed with the exact-decimal sum helper and
subtracted in Python. Summing with a numeric cast turns decimal strings into
binary floats (0.10 + 0.20 -> 0.30000000000000004), and the dust flows into
the gain/loss the action posts to the general ledger.

Book (dust case): a USD-base company holds a EUR bank account with debits
0.10 and 0.20 against a credit of 0.10 (exact txn balance 0.20; exact base
balance 0.20), revalued at EUR->USD 1.25. Exact math: new base 0.25,
gain 0.05.

Book (large-magnitude case): the same company shape holds a EUR bank account
with a single 1.00 debit booked long ago at a huge base amount,
300000000000000.07, revalued at EUR->USD 1.25. Exact math: new base 1.25,
loss -299999999999998.82. At this magnitude a binary float cannot hold cents
(the nearest double to ...000.07 is ...000.0625), so the old float path posts
a leg one cent away (...998.81); only the exact path posts ...998.82.
"""
from decimal import Decimal

import pytest

from gl_helpers import (call_action, is_ok, load_db_query, ns,
                        seed_account, seed_company, seed_cost_center,
                        seed_currency, seed_fiscal_year, _uuid)

gl = load_db_query()

D = Decimal
AS_OF = "2026-06-30"

# Large-magnitude book, hand-computed:
#   txn_balance      = 1.00 - 0 = 1.00
#   old_base_balance = 300000000000000.07
#   new_base_balance = 1.00 * 1.25 = 1.25
#   gain_loss        = 1.25 - 300000000000000.07 = -299999999999998.82
WHALE_TXN_DEBIT = "1.00"
WHALE_BASE_DEBIT = "300000000000000.07"
WHALE_RATE = "1.25"
WHALE_TXN_BALANCE = "1.00"
WHALE_OLD_BASE = "300000000000000.07"
WHALE_NEW_BASE = "1.25"
WHALE_GAIN = "-299999999999998.82"
WHALE_LEG = "299999999999998.82"


@pytest.fixture
def dust_env(conn):
    """USD-base company; EUR bank whose legs binary float cannot sum exactly."""
    cid = seed_company(conn)
    seed_fiscal_year(conn, cid)
    cc = seed_cost_center(conn, cid)
    fx_gain_loss = seed_account(conn, cid, "FX Gain/Loss", "expense", "expense",
                                account_number="7100")
    conn.execute(
        "UPDATE company SET exchange_gain_loss_account_id = ?, "
        "default_cost_center_id = ? WHERE id = ?", (fx_gain_loss, cc, cid))

    eur_bank = _uuid()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        " balance_direction, currency, is_group, disabled, company_id, depth) "
        "VALUES (?, 'EUR Bank', '1010', 'asset', 'bank', 'debit_normal', 'EUR', "
        " 0, 0, ?, 0)", (eur_bank, cid))
    # Exact txn balance 0.10 + 0.20 - 0.10 = 0.20; exact base balance the same.
    # Binary float sums the debit leg as 0.30000000000000004, so any float
    # step anywhere in the balance path leaves dust in the posted gain.
    for debit, credit in (("0.10", "0"), ("0.20", "0"), ("0", "0.10")):
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            " debit_base, credit_base, currency, exchange_rate, voucher_type, "
            " voucher_id, entry_set, is_cancelled) "
            "VALUES (?, '2026-03-01', ?, ?, ?, ?, ?, 'EUR', "
            " '1.00', 'journal_entry', ?, 'primary', 0)",
            (_uuid(), eur_bank, debit, credit, debit, credit, _uuid()))
    # Currency seed goes through the suite helper, which renders the portable
    # spelling for the configured backend (SQLite and PostgreSQL differ here).
    for code, name in (("USD", "US Dollar"), ("EUR", "Euro")):
        seed_currency(conn, code, name)
    conn.execute(
        "INSERT INTO exchange_rate (id, from_currency, to_currency, rate, "
        " effective_date) VALUES (?, 'EUR', 'USD', '1.25', ?)", (_uuid(), AS_OF))
    conn.commit()
    return {"company_id": cid, "eur_bank": eur_bank, "fx": fx_gain_loss}


@pytest.fixture
def whale_env(conn):
    """USD-base company; EUR bank with one 1.00 debit booked at a huge base."""
    cid = seed_company(conn)
    seed_fiscal_year(conn, cid)
    cc = seed_cost_center(conn, cid)
    fx_gain_loss = seed_account(conn, cid, "FX Gain/Loss", "expense", "expense",
                                account_number="7100")
    conn.execute(
        "UPDATE company SET exchange_gain_loss_account_id = ?, "
        "default_cost_center_id = ? WHERE id = ?", (fx_gain_loss, cc, cid))

    eur_bank = _uuid()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        " balance_direction, currency, is_group, disabled, company_id, depth) "
        "VALUES (?, 'EUR Bank', '1010', 'asset', 'bank', 'debit_normal', 'EUR', "
        " 0, 0, ?, 0)", (eur_bank, cid))
    # One 1.00 EUR debit whose booked base sits where binary float cannot
    # hold cents, so the old float balance path rounds the posted leg wrong.
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " debit_base, credit_base, currency, exchange_rate, voucher_type, "
        " voucher_id, entry_set, is_cancelled) "
        "VALUES (?, '2026-03-01', ?, ?, '0', ?, '0', 'EUR', "
        " '1.00', 'journal_entry', ?, 'primary', 0)",
        (_uuid(), eur_bank, WHALE_TXN_DEBIT, WHALE_BASE_DEBIT,
         _uuid()))
    for code, name in (("USD", "US Dollar"), ("EUR", "Euro")):
        seed_currency(conn, code, name)
    conn.execute(
        "INSERT INTO exchange_rate (id, from_currency, to_currency, rate, "
        " effective_date) VALUES (?, 'EUR', 'USD', ?, ?)",
        (_uuid(), WHALE_RATE, AS_OF))
    conn.commit()
    return {"company_id": cid, "eur_bank": eur_bank, "fx": fx_gain_loss}


def _reval_rows(fresh_conn):
    from erpclaw_lib.query import P, Q, Table
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit)
         .where(t.voucher_type == P()))
    return fresh_conn.execute(
        q.get_sql(), ("exchange_rate_revaluation",)).fetchall()


def test_revaluation_posts_the_exact_gain_as_text(conn, db_path, dust_env):
    res = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=dust_env["company_id"], as_of_date=AS_OF))
    assert is_ok(res), res

    # Exact strings: any binary-float dust (e.g. 0.04999999999999996 or a
    # 0.30000000000000004 balance) fails these comparisons.
    assert res["total_gain_loss"] == "0.05"
    assert res["accounts_processed"] == 1
    reval = res["revaluations"][0]
    assert reval["txn_balance"] == "0.20"
    assert reval["old_base_balance"] == "0.20"
    assert reval["new_base_balance"] == "0.25"
    assert reval["gain_loss"] == "0.05"

    # Read the posted ledger rows back through a fresh connection.
    from erpclaw_lib.db import get_connection
    fresh = get_connection(db_path)
    try:
        posted = _reval_rows(fresh)
    finally:
        fresh.close()
    assert len(posted) == 2
    by_account = {r["account_id"]: r for r in posted}
    assert by_account[dust_env["eur_bank"]]["debit"] == "0.05"
    assert by_account[dust_env["eur_bank"]]["credit"] == "0.00"
    assert by_account[dust_env["fx"]]["debit"] == "0.00"
    assert by_account[dust_env["fx"]]["credit"] == "0.05"


def test_revaluation_posts_the_exact_loss_at_large_magnitude(conn, db_path,
                                                             whale_env):
    res = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=whale_env["company_id"], as_of_date=AS_OF))
    assert is_ok(res), res

    # Hand-computed exact strings: 1.00 * 1.25 = 1.25, and
    # 1.25 - 300000000000000.07 = -299999999999998.82. The old float balance
    # path reads the booked base as ...000.0625 and posts ...998.81, so this
    # fails on the code before the fix and passes after it.
    assert res["total_gain_loss"] == WHALE_GAIN
    assert res["accounts_processed"] == 1
    reval = res["revaluations"][0]
    assert reval["txn_balance"] == WHALE_TXN_BALANCE
    assert reval["old_base_balance"] == WHALE_OLD_BASE
    assert reval["new_base_balance"] == WHALE_NEW_BASE
    assert reval["gain_loss"] == WHALE_GAIN

    # Read the posted ledger rows back through a fresh connection.
    from erpclaw_lib.db import get_connection
    fresh = get_connection(db_path)
    try:
        posted = _reval_rows(fresh)
    finally:
        fresh.close()
    assert len(posted) == 2
    by_account = {r["account_id"]: r for r in posted}
    assert by_account[whale_env["eur_bank"]]["debit"] == "0.00"
    assert by_account[whale_env["eur_bank"]]["credit"] == WHALE_LEG
    assert by_account[whale_env["fx"]]["debit"] == WHALE_LEG
    assert by_account[whale_env["fx"]]["credit"] == "0.00"
