"""Tests for unit of measure actions.

Test IDs: S-UO-01 through S-UO-03

NOTE: uom_conversion has FK to uom(id), so we need actual UoM IDs.
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-UO-01: Add a UoM "Each" and verify id
# ---------------------------------------------------------------------------
def test_add_uom(fresh_db):
    result = _call_action(
        db_query.add_uom, fresh_db,
        name="Each", must_be_whole_number=True
    )

    assert result["status"] == "ok"
    assert "uom_id" in result
    assert len(result["uom_id"]) == 36
    assert result["name"] == "Each"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM uom WHERE id = ?", (result["uom_id"],)
    ).fetchone()
    assert row["name"] == "Each"
    assert row["must_be_whole_number"] == 1


# ---------------------------------------------------------------------------
# S-UO-02: Add Box and Each, then add conversion Box->Each=12
# ---------------------------------------------------------------------------
def test_add_uom_conversion(fresh_db):
    box = _call_action(
        db_query.add_uom, fresh_db,
        name="Box", must_be_whole_number=True
    )
    each = _call_action(
        db_query.add_uom, fresh_db,
        name="Each", must_be_whole_number=True
    )

    box_id = box["uom_id"]
    each_id = each["uom_id"]

    result = _call_action(
        db_query.add_uom_conversion, fresh_db,
        from_uom=box_id, to_uom=each_id, conversion_factor="12"
    )

    assert result["status"] == "ok"
    assert "uom_conversion_id" in result
    assert len(result["uom_conversion_id"]) == 36

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM uom_conversion WHERE id = ?",
        (result["uom_conversion_id"],)
    ).fetchone()
    assert row["from_uom"] == box_id
    assert row["to_uom"] == each_id
    assert row["conversion_factor"] == "12"


# ---------------------------------------------------------------------------
# S-UO-03: Add several UoMs, list returns all of them
# ---------------------------------------------------------------------------
def test_list_uoms(fresh_db):
    _call_action(db_query.add_uom, fresh_db, name="Each")
    _call_action(db_query.add_uom, fresh_db, name="Kilogram")
    _call_action(db_query.add_uom, fresh_db, name="Litre")
    _call_action(db_query.add_uom, fresh_db, name="Box")

    result = _call_action(db_query.list_uoms, fresh_db)

    assert result["status"] == "ok"
    assert len(result["uoms"]) == 4
    names = [u["name"] for u in result["uoms"]]
    assert "Each" in names
    assert "Kilogram" in names
    assert "Litre" in names
    assert "Box" in names
