"""Tests for erpclaw-setup payment terms and UoM actions.

Actions tested:
  - add-payment-terms, list-payment-terms
  - add-uom, list-uoms, add-uom-conversion
"""
import pytest
from setup_helpers import call_action, ns, seed_uom, is_error, is_ok, load_db_query

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Payment Terms
# ──────────────────────────────────────────────────────────────────────────────

class TestAddPaymentTerms:
    def test_basic_create(self, conn):
        result = call_action(mod.add_payment_terms, conn, ns(
            name="Net 30", due_days=30,
            discount_percentage=None, discount_days=None, description=None,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT * FROM payment_terms WHERE name='Net 30'").fetchone()
        assert row is not None
        assert row["due_days"] == 30

    def test_with_discount(self, conn):
        result = call_action(mod.add_payment_terms, conn, ns(
            name="2/10 Net 30", due_days=30,
            discount_percentage="2.0", discount_days=10,
            description="2% discount if paid within 10 days",
        ))
        assert is_ok(result)
        row = conn.execute("SELECT * FROM payment_terms WHERE name='2/10 Net 30'").fetchone()
        assert row["discount_percentage"] == "2.0"
        assert row["discount_days"] == 10

    def test_duplicate_name_fails(self, conn):
        call_action(mod.add_payment_terms, conn, ns(
            name="Due on Receipt", due_days=0,
            discount_percentage=None, discount_days=None, description=None,
        ))
        result = call_action(mod.add_payment_terms, conn, ns(
            name="Due on Receipt", due_days=0,
            discount_percentage=None, discount_days=None, description=None,
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn):
        result = call_action(mod.add_payment_terms, conn, ns(
            name=None, due_days=30,
            discount_percentage=None, discount_days=None, description=None,
        ))
        assert is_error(result)


class TestListPaymentTerms:
    def test_list_empty(self, conn):
        result = call_action(mod.list_payment_terms, conn, ns(limit=None, offset=None))
        assert result["terms"] == []

    def test_list_returns_created(self, conn):
        call_action(mod.add_payment_terms, conn, ns(
            name="Net 15", due_days=15,
            discount_percentage=None, discount_days=None, description=None,
        ))
        result = call_action(mod.list_payment_terms, conn, ns(limit=None, offset=None))
        assert result["total_count"] >= 1
        names = [pt["name"] for pt in result["terms"]]
        assert "Net 15" in names


# ──────────────────────────────────────────────────────────────────────────────
# Units of Measure
# ──────────────────────────────────────────────────────────────────────────────

class TestAddUom:
    def test_basic_create(self, conn):
        result = call_action(mod.add_uom, conn, ns(
            name="Kilogram", must_be_whole_number=False,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT * FROM uom WHERE name='Kilogram'").fetchone()
        assert row is not None
        assert row["must_be_whole_number"] == 0

    def test_whole_number_uom(self, conn):
        result = call_action(mod.add_uom, conn, ns(
            name="Piece", must_be_whole_number=True,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT * FROM uom WHERE name='Piece'").fetchone()
        assert row["must_be_whole_number"] == 1

    def test_duplicate_name_fails(self, conn):
        call_action(mod.add_uom, conn, ns(name="Box", must_be_whole_number=False))
        result = call_action(mod.add_uom, conn, ns(name="Box", must_be_whole_number=False))
        assert is_error(result)

    def test_missing_name_fails(self, conn):
        result = call_action(mod.add_uom, conn, ns(name=None, must_be_whole_number=False))
        assert is_error(result)


class TestListUoms:
    def test_list_returns_created(self, conn):
        call_action(mod.add_uom, conn, ns(name="Liter", must_be_whole_number=False))
        result = call_action(mod.list_uoms, conn, ns(limit=None, offset=None))
        assert result["total_count"] >= 1
        names = [u["name"] for u in result["uoms"]]
        assert "Liter" in names


class TestUomConversion:
    def test_basic_conversion(self, conn):
        uid_kg = seed_uom(conn, "Kg")
        uid_g = seed_uom(conn, "Gram")
        result = call_action(mod.add_uom_conversion, conn, ns(
            from_uom=uid_kg, to_uom=uid_g,
            conversion_factor="1000", item_id=None,
        ))
        assert is_ok(result)
        row = conn.execute(
            "SELECT * FROM uom_conversion WHERE from_uom=? AND to_uom=?",
            (uid_kg, uid_g)
        ).fetchone()
        assert row["conversion_factor"] == "1000"

    def test_missing_from_uom_fails(self, conn):
        uid_g = seed_uom(conn, "Gram2")
        result = call_action(mod.add_uom_conversion, conn, ns(
            from_uom=None, to_uom=uid_g,
            conversion_factor="1000", item_id=None,
        ))
        assert is_error(result)
