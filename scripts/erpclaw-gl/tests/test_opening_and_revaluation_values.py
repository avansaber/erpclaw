"""Part A — VALUE tests for `import-opening-balances` and
`revalue-foreign-balances` (Wave G F21).

Both actions write `gl_entry` rows directly and both carried exactly one test:
the generated contract assert that the action dispatches. Opening balances are
the first numbers in a customer's books, and an FX revaluation moves unrealised
gain/loss into the P&L, so "it dispatched" is not coverage of either.

Register rows: `planning/wave_g/F21_TEST_DEPTH_REGISTER_2026-08-11.json`
(`import-opening-balances`, `revalue-foreign-balances`; both
`routability-only`, ledger reach `gl_entry`).
"""
from decimal import Decimal

import pytest
from gl_helpers import (call_action, is_error, is_ok, load_db_query, ns,
                        seed_account, seed_company, seed_cost_center,
                        seed_customer, seed_fiscal_year, _uuid)

gl = load_db_query()

D = Decimal
OPENING_DATE = "2026-01-01"
AS_OF = "2026-06-30"


def _msg(result: dict) -> str:
    return result.get("message", "") + result.get("error", "")


def _write_csv(tmp_path, rows, header="account_number,debit,credit"):
    path = tmp_path / "opening.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


def _gl_rows(conn, voucher_id):
    # NOTE: gl_entry has no `is_opening` column — `is_opening` is a validation
    # mode on the write path (it relaxes the fiscal-period rules), not stored
    # state. What IS observable is the remark and the balanced pair.
    return conn.execute(
        "SELECT account_id, debit, credit, voucher_type, remarks, fiscal_year, "
        "is_cancelled FROM gl_entry WHERE voucher_id = ? "
        "ORDER BY debit DESC", (voucher_id,)).fetchall()


# ── import-opening-balances ──────────────────────────────────────────────────

@pytest.fixture
def opening_env(conn):
    cid = seed_company(conn)
    seed_fiscal_year(conn, cid)
    seed_cost_center(conn, cid)
    cash = seed_account(conn, cid, "Cash", "asset", "cash", account_number="1000")
    equity = seed_account(conn, cid, "Opening Equity", "equity", "equity",
                          account_number="3000")
    ar = seed_account(conn, cid, "Debtors", "asset", "receivable",
                      account_number="1200")
    return {"company_id": cid, "cash": cash, "equity": equity, "ar": ar}


def test_opening_import_posts_the_exact_csv_amounts_as_opening_gl(
        conn, opening_env, tmp_path):
    csv_path = _write_csv(tmp_path, ["1000,15000.00,0", "3000,0,15000.00"])

    res = call_action(gl.import_opening_balances, conn, ns(
        csv_path=csv_path, company_id=opening_env["company_id"],
        posting_date=OPENING_DATE))
    assert is_ok(res), res
    assert res["rows_processed"] == 2
    assert res["gl_entries_created"] == 2

    rows = _gl_rows(conn, res["voucher_id"])
    assert len(rows) == 2
    assert rows[0]["account_id"] == opening_env["cash"]
    assert D(rows[0]["debit"]) == D("15000.00")
    assert D(rows[0]["credit"]) == D("0")
    assert rows[1]["account_id"] == opening_env["equity"]
    assert D(rows[1]["credit"]) == D("15000.00")

    # The batch balances to the cent, is live, and is identifiable as the import.
    assert sum((D(r["debit"]) for r in rows), D("0")) == \
           sum((D(r["credit"]) for r in rows), D("0")) == D("15000.00")
    assert all(r["is_cancelled"] == 0 for r in rows)
    assert all(r["remarks"] == "Opening Balance Import" for r in rows)
    assert all(r["voucher_type"] == "journal_entry" for r in rows)


def test_opening_import_attaches_the_party_to_a_receivable_row(
        conn, opening_env, tmp_path):
    cust = seed_customer(conn, opening_env["company_id"], "Wayne Enterprises")
    csv_path = _write_csv(
        tmp_path,
        ["1200,2500.50,0,customer,Wayne Enterprises", "3000,0,2500.50,,"],
        header="account_number,debit,credit,party_type,party_name")

    res = call_action(gl.import_opening_balances, conn, ns(
        csv_path=csv_path, company_id=opening_env["company_id"],
        posting_date=OPENING_DATE))
    assert is_ok(res), res

    ar_row = conn.execute(
        "SELECT party_type, party_id, debit FROM gl_entry "
        "WHERE account_id = ?", (opening_env["ar"],)).fetchone()
    assert ar_row["party_type"] == "customer"
    assert ar_row["party_id"] == cust
    assert D(ar_row["debit"]) == D("2500.50")


def test_opening_import_refuses_an_unbalanced_file_and_posts_nothing(
        conn, opening_env, tmp_path):
    csv_path = _write_csv(tmp_path, ["1000,15000.00,0", "3000,0,14000.00"])

    bad = call_action(gl.import_opening_balances, conn, ns(
        csv_path=csv_path, company_id=opening_env["company_id"],
        posting_date=OPENING_DATE))
    assert is_error(bad)
    assert "GL posting failed" in _msg(bad)
    assert conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0] == 0


def test_opening_import_refuses_an_unknown_account_number(
        conn, opening_env, tmp_path):
    csv_path = _write_csv(tmp_path, ["9999,100.00,0", "3000,0,100.00"])

    bad = call_action(gl.import_opening_balances, conn, ns(
        csv_path=csv_path, company_id=opening_env["company_id"],
        posting_date=OPENING_DATE))
    assert is_error(bad)
    assert "9999" in _msg(bad)
    assert conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0] == 0


def test_opening_import_refuses_a_non_csv_path(conn, opening_env, tmp_path):
    txt = tmp_path / "balances.txt"
    txt.write_text("account_number,debit,credit\n1000,1,0\n", encoding="utf-8")

    bad = call_action(gl.import_opening_balances, conn, ns(
        csv_path=str(txt), company_id=opening_env["company_id"],
        posting_date=OPENING_DATE))
    assert is_error(bad)
    assert ".csv" in _msg(bad)


# ── revalue-foreign-balances ─────────────────────────────────────────────────

@pytest.fixture
def fx_env(conn):
    """USD-base company holding a EUR bank account with 1,000 EUR booked at 1.10."""
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
    # Seeded history: 1,000.00 EUR recorded at 1.10 -> 1,100.00 USD.
    # (gl_entry carries no company_id — company scope comes from account.)
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " debit_base, credit_base, currency, exchange_rate, voucher_type, "
        " voucher_id, entry_set, is_cancelled) "
        "VALUES (?, '2026-03-01', ?, '1000.00', '0', '1100.00', '0', 'EUR', "
        " '1.10', 'journal_entry', ?, 'primary', 0)",
        (_uuid(), eur_bank, _uuid()))
    # exchange_rate.from_currency/to_currency are FKs into `currency`.
    for code, name in (("USD", "US Dollar"), ("EUR", "Euro")):
        conn.execute("INSERT OR IGNORE INTO currency (code, name) VALUES (?, ?)",
                     (code, name))
    conn.execute(
        "INSERT INTO exchange_rate (id, from_currency, to_currency, rate, "
        " effective_date) VALUES (?, 'EUR', 'USD', '1.25', ?)", (_uuid(), AS_OF))
    conn.commit()
    return {"company_id": cid, "eur_bank": eur_bank, "fx": fx_gain_loss, "cc": cc}


def test_revaluation_posts_the_exact_unrealised_gain(conn, fx_env):
    res = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=fx_env["company_id"], as_of_date=AS_OF))
    assert is_ok(res), res

    # 1,000 EUR at 1.25 = 1,250.00 USD; booked at 1,100.00 -> gain 150.00.
    assert res["total_gain_loss"] == "150.00"
    assert res["accounts_processed"] == 1
    reval = res["revaluations"][0]
    # Decimal comparison, not string: the balances come back from a SQL SUM and
    # carry no fixed scale ("1000"), while the posted amounts do ("150.00").
    assert D(reval["txn_balance"]) == D("1000.00")
    assert D(reval["old_base_balance"]) == D("1100.00")
    assert D(reval["new_base_balance"]) == D("1250.00")
    assert D(reval["gain_loss"]) == D("150.00")
    assert D(reval["exchange_rate"]) == D("1.25")

    posted = conn.execute(
        "SELECT account_id, debit, credit, cost_center_id FROM gl_entry "
        "WHERE voucher_type = 'exchange_rate_revaluation' ORDER BY debit DESC"
    ).fetchall()
    assert len(posted) == 2
    # DR the bank account (it is worth more base currency), CR FX gain/loss.
    assert posted[0]["account_id"] == fx_env["eur_bank"]
    assert D(posted[0]["debit"]) == D("150.00")
    assert posted[1]["account_id"] == fx_env["fx"]
    assert D(posted[1]["credit"]) == D("150.00")
    assert posted[1]["cost_center_id"] == fx_env["cc"], \
        "the P&L leg must carry a cost center or GL step 6 would have refused it"


def test_revaluation_posts_a_loss_when_the_rate_falls(conn, fx_env):
    conn.execute("UPDATE exchange_rate SET rate = '0.90' WHERE to_currency = 'USD'")
    conn.commit()

    res = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=fx_env["company_id"], as_of_date=AS_OF))
    assert is_ok(res), res
    # 1,000 EUR at 0.90 = 900.00; booked at 1,100.00 -> loss of 200.00.
    assert res["total_gain_loss"] == "-200.00"

    posted = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = 'exchange_rate_revaluation' ORDER BY debit DESC"
    ).fetchall()
    assert posted[0]["account_id"] == fx_env["fx"]
    assert D(posted[0]["debit"]) == D("200.00")
    assert posted[1]["account_id"] == fx_env["eur_bank"]
    assert D(posted[1]["credit"]) == D("200.00")


def test_revaluation_skips_an_account_with_no_rate_and_posts_nothing(conn, fx_env):
    conn.execute("DELETE FROM exchange_rate")
    conn.commit()

    res = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=fx_env["company_id"], as_of_date=AS_OF))
    assert is_ok(res), res
    assert res["revaluations"][0]["skipped"] is True
    assert "No exchange rate found" in res["revaluations"][0]["reason"]
    assert res["total_gain_loss"] == "0"
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = "
        "'exchange_rate_revaluation'").fetchone()[0] == 0


def test_revaluation_is_refused_without_an_fx_account_configured(conn, fx_env):
    conn.execute("UPDATE company SET exchange_gain_loss_account_id = NULL "
                 "WHERE id = ?", (fx_env["company_id"],))
    conn.commit()

    bad = call_action(gl.revalue_foreign_balances, conn, ns(
        company_id=fx_env["company_id"], as_of_date=AS_OF))
    assert is_error(bad)
    assert "exchange_gain_loss_account_id" in _msg(bad)
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = "
        "'exchange_rate_revaluation'").fetchone()[0] == 0
