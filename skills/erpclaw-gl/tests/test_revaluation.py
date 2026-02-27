"""Tests for revalue-foreign-balances action.

Tests: basic revaluation gain/loss, no foreign accounts, missing rate.
"""
import uuid
from decimal import Decimal

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    set_company_defaults,
)


def _ensure_currency(conn, code):
    """Insert currency if not exists."""
    existing = conn.execute(
        "SELECT code FROM currency WHERE code = ?", (code,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO currency (code, name, symbol, decimal_places, enabled) "
            "VALUES (?, ?, ?, 2, 1)",
            (code, code, code),
        )
        conn.commit()


def _insert_rate(conn, from_curr, to_curr, date, rate):
    """Insert an exchange rate."""
    _ensure_currency(conn, from_curr)
    _ensure_currency(conn, to_curr)
    conn.execute(
        "INSERT INTO exchange_rate (id, from_currency, to_currency, rate, "
        "effective_date, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (str(uuid.uuid4()), from_curr, to_curr, str(rate), date),
    )
    conn.commit()


def _setup_fx_company(conn):
    """Create company with FX gain/loss account and EUR bank account."""
    _ensure_currency(conn, "USD")
    _ensure_currency(conn, "EUR")
    company_id = create_test_company(conn, "FX Corp", "FX")
    create_test_fiscal_year(conn, company_id)
    cc_id = create_test_cost_center(conn, company_id, "Main")

    # Create FX gain/loss account (income/expense)
    fx_acct = create_test_account(conn, company_id, "FX Gain/Loss",
                                  "expense", account_type="expense")

    # Set company's exchange_gain_loss_account_id and default_cost_center_id
    set_company_defaults(conn, company_id,
                         exchange_gain_loss_account_id=fx_acct,
                         default_cost_center_id=cc_id)

    # Create a EUR bank account
    eur_bank = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO account (id, name, root_type, account_type, currency,
           is_group, balance_direction, company_id, depth)
           VALUES (?, 'EUR Bank', 'asset', 'bank', 'EUR', 0,
                   'debit_normal', ?, 0)""",
        (eur_bank, company_id),
    )

    # Create an equity account for balancing
    equity = create_test_account(conn, company_id, "Equity", "equity")
    conn.commit()

    return company_id, eur_bank, equity, fx_acct, cc_id


def test_revaluation_gain(fresh_db):
    """FX revaluation creates gain entries when rate increases."""
    company_id, eur_bank, equity, fx_acct, cc_id = _setup_fx_company(fresh_db)

    # Post 1000 EUR at rate 1.10 (1100 USD base)
    fresh_db.execute(
        """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
           debit_base, credit_base, currency, exchange_rate,
           voucher_type, voucher_id, is_cancelled)
           VALUES (?, '2026-01-15', ?, '1000', '0', '1100', '0', 'EUR', '1.10',
                   'payment_entry', ?, 0)""",
        (str(uuid.uuid4()), eur_bank, str(uuid.uuid4())),
    )
    fresh_db.commit()

    # Rate increases to 1.15 at revaluation date
    _insert_rate(fresh_db, "EUR", "USD", "2026-01-31", "1.15")

    result = _call_action(db_query.revalue_foreign_balances, fresh_db,
                          company_id=company_id, as_of_date="2026-01-31")
    assert result["status"] == "ok"
    assert len(result["revaluations"]) == 1

    reval = result["revaluations"][0]
    assert reval["currency"] == "EUR"
    # 1000 EUR * 1.15 = 1150 base, was 1100 → gain of 50
    assert Decimal(reval["gain_loss"]) == Decimal("50.00")
    assert Decimal(result["total_gain_loss"]) == Decimal("50.00")


def test_revaluation_loss(fresh_db):
    """FX revaluation creates loss entries when rate decreases."""
    company_id, eur_bank, equity, fx_acct, cc_id = _setup_fx_company(fresh_db)

    # Post 1000 EUR at rate 1.10 (1100 USD base)
    fresh_db.execute(
        """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
           debit_base, credit_base, currency, exchange_rate,
           voucher_type, voucher_id, is_cancelled)
           VALUES (?, '2026-01-15', ?, '1000', '0', '1100', '0', 'EUR', '1.10',
                   'payment_entry', ?, 0)""",
        (str(uuid.uuid4()), eur_bank, str(uuid.uuid4())),
    )
    fresh_db.commit()

    # Rate decreases to 1.05
    _insert_rate(fresh_db, "EUR", "USD", "2026-01-31", "1.05")

    result = _call_action(db_query.revalue_foreign_balances, fresh_db,
                          company_id=company_id, as_of_date="2026-01-31")
    assert result["status"] == "ok"

    reval = result["revaluations"][0]
    # 1000 EUR * 1.05 = 1050 base, was 1100 → loss of -50
    assert Decimal(reval["gain_loss"]) == Decimal("-50.00")


def test_revaluation_no_foreign_accounts(fresh_db):
    """No revaluation when all accounts are in base currency."""
    company_id = create_test_company(fresh_db, "Domestic Corp", "DC")
    create_test_fiscal_year(conn=fresh_db, company_id=company_id)

    # Create FX account and set on company
    fx_acct = create_test_account(fresh_db, company_id, "FX GL",
                                  "expense", account_type="expense")
    set_company_defaults(fresh_db, company_id,
                         exchange_gain_loss_account_id=fx_acct)

    result = _call_action(db_query.revalue_foreign_balances, fresh_db,
                          company_id=company_id, as_of_date="2026-01-31")
    assert result["status"] == "ok"
    assert result["revaluations"] == []


def test_revaluation_missing_rate(fresh_db):
    """Skips account when no exchange rate is available."""
    company_id, eur_bank, equity, fx_acct, cc_id = _setup_fx_company(fresh_db)

    # Post EUR balance but no rate available for revaluation date
    fresh_db.execute(
        """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
           debit_base, credit_base, currency, exchange_rate,
           voucher_type, voucher_id, is_cancelled)
           VALUES (?, '2026-01-15', ?, '500', '0', '550', '0', 'EUR', '1.10',
                   'payment_entry', ?, 0)""",
        (str(uuid.uuid4()), eur_bank, str(uuid.uuid4())),
    )
    fresh_db.commit()

    result = _call_action(db_query.revalue_foreign_balances, fresh_db,
                          company_id=company_id, as_of_date="2026-06-30")
    assert result["status"] == "ok"
    assert len(result["revaluations"]) == 1
    assert result["revaluations"][0]["skipped"] is True
    assert "No exchange rate" in result["revaluations"][0]["reason"]
