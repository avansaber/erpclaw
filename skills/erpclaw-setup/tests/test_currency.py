"""Tests for currency management actions.

Test IDs: S-CU-01 through S-CU-03
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-CU-01: Add a currency and verify status=ok
# ---------------------------------------------------------------------------
def test_add_currency(fresh_db):
    result = _call_action(
        db_query.add_currency, fresh_db,
        code="EUR", name="Euro", symbol="E", decimal_places=2, enabled=True
    )

    assert result["status"] == "ok"
    assert result["code"] == "EUR"
    assert result["name"] == "Euro"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM currency WHERE code = 'EUR'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "Euro"
    assert row["enabled"] == 1


# ---------------------------------------------------------------------------
# S-CU-02: Add USD and EUR, list returns both
# ---------------------------------------------------------------------------
def test_list_currencies(fresh_db):
    _call_action(
        db_query.add_currency, fresh_db,
        code="USD", name="US Dollar", symbol="$", enabled=True
    )
    _call_action(
        db_query.add_currency, fresh_db,
        code="EUR", name="Euro", symbol="E", enabled=True
    )

    result = _call_action(db_query.list_currencies, fresh_db)

    assert result["status"] == "ok"
    codes = [c["code"] for c in result["currencies"]]
    assert "USD" in codes
    assert "EUR" in codes
    assert len(result["currencies"]) >= 2


# ---------------------------------------------------------------------------
# S-CU-03: Adding the same currency code twice fails
# ---------------------------------------------------------------------------
def test_duplicate_currency(fresh_db):
    _call_action(
        db_query.add_currency, fresh_db,
        code="GBP", name="British Pound"
    )
    result = _call_action(
        db_query.add_currency, fresh_db,
        code="GBP", name="British Pound Again"
    )

    assert result["status"] == "error"
    assert "already exists" in result["message"]
