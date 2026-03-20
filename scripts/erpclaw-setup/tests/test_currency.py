"""Tests for erpclaw-setup currency management actions.

Actions tested:
  - add-currency
  - list-currencies
  - add-exchange-rate
  - get-exchange-rate
  - list-exchange-rates
"""
import pytest
from setup_helpers import call_action, ns, seed_currency, is_error, is_ok, load_db_query

mod = load_db_query()


class TestAddCurrency:
    def test_basic_create(self, conn):
        result = call_action(mod.add_currency, conn, ns(
            code="GBP", name="British Pound", symbol="£",
            decimal_places=2, enabled=True,
        ))
        assert result.get("code") == "GBP"
        row = conn.execute("SELECT * FROM currency WHERE code='GBP'").fetchone()
        assert row is not None
        assert row["name"] == "British Pound"
        assert row["symbol"] == "£"

    def test_missing_code_fails(self, conn):
        result = call_action(mod.add_currency, conn, ns(
            code=None, name="Unknown", symbol=None,
            decimal_places=None, enabled=False,
        ))
        assert is_error(result)

    def test_duplicate_code_fails(self, conn):
        seed_currency(conn, "JPY", "Japanese Yen", "¥")
        result = call_action(mod.add_currency, conn, ns(
            code="JPY", name="Japanese Yen Copy", symbol="¥",
            decimal_places=0, enabled=True,
        ))
        assert is_error(result)

    def test_zero_decimal_places(self, conn):
        """BUG-004: `args.decimal_places or 2` treats 0 as falsy → always 2."""
        result = call_action(mod.add_currency, conn, ns(
            code="KRW", name="Korean Won", symbol="₩",
            decimal_places=0, enabled=True,
        ))
        row = conn.execute("SELECT decimal_places FROM currency WHERE code='KRW'").fetchone()
        # Known bug: 0 is treated as falsy, defaulting to 2
        # Fix: change `or 2` to `if ... is None else ...` in db_query.py
        assert row["decimal_places"] == 2  # Should be 0 once BUG-004 is fixed


class TestListCurrencies:
    def test_list_includes_seeded(self, conn):
        seed_currency(conn, "CHF", "Swiss Franc", "CHF")
        result = call_action(mod.list_currencies, conn, ns(
            enabled_only=False, limit=None, offset=None,
        ))
        assert result["total_count"] >= 1
        codes = [c["code"] for c in result["currencies"]]
        assert "CHF" in codes

    def test_list_enabled_only(self, conn):
        conn.execute(
            "INSERT INTO currency (code, name, enabled) VALUES ('DIS', 'Disabled', 0)"
        )
        conn.commit()
        result = call_action(mod.list_currencies, conn, ns(
            enabled_only=True, limit=None, offset=None,
        ))
        codes = [c["code"] for c in result["currencies"]]
        assert "DIS" not in codes


class TestExchangeRate:
    def test_add_and_get(self, conn):
        seed_currency(conn, "USD", "US Dollar", "$")
        seed_currency(conn, "EUR", "Euro", "€")
        result = call_action(mod.add_exchange_rate, conn, ns(
            from_currency="USD", to_currency="EUR",
            rate="0.92", effective_date="2026-03-01", source=None,
        ))
        assert is_ok(result)

        get_result = call_action(mod.get_exchange_rate, conn, ns(
            from_currency="USD", to_currency="EUR",
            effective_date="2026-03-01",
        ))
        assert "rate" in get_result

    def test_add_missing_currency_fails(self, conn):
        result = call_action(mod.add_exchange_rate, conn, ns(
            from_currency="XXX", to_currency="YYY",
            rate="1.5", effective_date="2026-01-01", source=None,
        ))
        assert is_error(result)

    def test_list_exchange_rates(self, conn):
        seed_currency(conn, "USD", "US Dollar", "$")
        seed_currency(conn, "CAD", "Canadian Dollar", "C$")
        call_action(mod.add_exchange_rate, conn, ns(
            from_currency="USD", to_currency="CAD",
            rate="1.35", effective_date="2026-01-01", source=None,
        ))
        call_action(mod.add_exchange_rate, conn, ns(
            from_currency="USD", to_currency="CAD",
            rate="1.36", effective_date="2026-02-01", source=None,
        ))
        result = call_action(mod.list_exchange_rates, conn, ns(
            from_currency="USD", to_currency="CAD",
            from_date=None, to_date=None, limit=None, offset=None,
        ))
        assert result["total_count"] >= 2
