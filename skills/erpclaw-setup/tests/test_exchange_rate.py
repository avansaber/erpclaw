"""Tests for exchange rate actions.

Test IDs: S-EX-01 through S-EX-03

NOTE: exchange_rate has FK to currency(code), so we insert currencies first.
"""
import db_query
from helpers import _call_action


def _seed_currencies(conn):
    """Insert USD and EUR so FK constraints are satisfied."""
    conn.execute("INSERT OR IGNORE INTO currency (code, name) VALUES ('USD', 'US Dollar')")
    conn.execute("INSERT OR IGNORE INTO currency (code, name) VALUES ('EUR', 'Euro')")
    conn.commit()


# ---------------------------------------------------------------------------
# S-EX-01: Add an exchange rate and verify id is returned
# ---------------------------------------------------------------------------
def test_add_exchange_rate(fresh_db):
    _seed_currencies(fresh_db)

    result = _call_action(
        db_query.add_exchange_rate, fresh_db,
        from_currency="USD", to_currency="EUR",
        rate="0.92", effective_date="2026-01-01"
    )

    assert result["status"] == "ok"
    assert "exchange_rate_id" in result
    assert len(result["exchange_rate_id"]) == 36
    assert result["effective_date"] == "2026-01-01"


# ---------------------------------------------------------------------------
# S-EX-02: Add a rate, then get it for the same date
# ---------------------------------------------------------------------------
def test_get_exchange_rate(fresh_db):
    _seed_currencies(fresh_db)

    _call_action(
        db_query.add_exchange_rate, fresh_db,
        from_currency="USD", to_currency="EUR",
        rate="0.92", effective_date="2026-01-15"
    )

    result = _call_action(
        db_query.get_exchange_rate, fresh_db,
        from_currency="USD", to_currency="EUR",
        effective_date="2026-01-15"
    )

    assert result["status"] == "ok"
    assert result["rate"] == "0.92"
    assert result["effective_date"] == "2026-01-15"
    assert result["source"] == "manual"


# ---------------------------------------------------------------------------
# S-EX-03: Add rate for Jan 1, query Jan 15 -> returns Jan 1 rate (fallback)
# ---------------------------------------------------------------------------
def test_get_rate_fallback(fresh_db):
    _seed_currencies(fresh_db)

    _call_action(
        db_query.add_exchange_rate, fresh_db,
        from_currency="USD", to_currency="EUR",
        rate="0.90", effective_date="2026-01-01"
    )

    # Query a later date — should fall back to Jan 1 rate
    result = _call_action(
        db_query.get_exchange_rate, fresh_db,
        from_currency="USD", to_currency="EUR",
        effective_date="2026-01-15"
    )

    assert result["status"] == "ok"
    assert result["rate"] == "0.90"
    assert result["effective_date"] == "2026-01-01"
