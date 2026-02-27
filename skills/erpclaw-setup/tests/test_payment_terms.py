"""Tests for payment terms actions.

Test IDs: S-PT-01 through S-PT-03
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-PT-01: Add payment terms and verify id
# ---------------------------------------------------------------------------
def test_add_payment_terms(fresh_db):
    result = _call_action(
        db_query.add_payment_terms, fresh_db,
        name="Net 30", due_days=30
    )

    assert result["status"] == "ok"
    assert "payment_terms_id" in result
    assert len(result["payment_terms_id"]) == 36
    assert result["name"] == "Net 30"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM payment_terms WHERE id = ?",
        (result["payment_terms_id"],)
    ).fetchone()
    assert row["due_days"] == 30


# ---------------------------------------------------------------------------
# S-PT-02: Add "2/10 Net 30" with discount fields and verify them
# ---------------------------------------------------------------------------
def test_add_discount_terms(fresh_db):
    result = _call_action(
        db_query.add_payment_terms, fresh_db,
        name="2/10 Net 30",
        due_days=30,
        discount_percentage="2.00",
        discount_days=10,
        description="2% discount if paid within 10 days"
    )

    assert result["status"] == "ok"
    assert result["name"] == "2/10 Net 30"

    # Verify discount fields in DB
    row = fresh_db.execute(
        "SELECT * FROM payment_terms WHERE id = ?",
        (result["payment_terms_id"],)
    ).fetchone()
    assert row["due_days"] == 30
    assert row["discount_percentage"] == "2.00"
    assert row["discount_days"] == 10
    assert row["description"] == "2% discount if paid within 10 days"


# ---------------------------------------------------------------------------
# S-PT-03: Add 3 payment terms, list returns all 3
# ---------------------------------------------------------------------------
def test_list_payment_terms(fresh_db):
    _call_action(db_query.add_payment_terms, fresh_db, name="Net 15", due_days=15)
    _call_action(db_query.add_payment_terms, fresh_db, name="Net 30", due_days=30)
    _call_action(db_query.add_payment_terms, fresh_db, name="Net 60", due_days=60)

    result = _call_action(db_query.list_payment_terms, fresh_db)

    assert result["status"] == "ok"
    assert len(result["terms"]) == 3
    names = [t["name"] for t in result["terms"]]
    assert "Net 15" in names
    assert "Net 30" in names
    assert "Net 60" in names
