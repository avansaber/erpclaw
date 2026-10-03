"""Part of migration 050: register the `expense_claim` voucher type for payment_allocation.

Paying an approved expense claim allocates a submitted payment to the claim,
and allocation voucher types are gated against `voucher_type_registry`
(target_table='payment_allocation'). Fresh installs carry the row from
init_schema's seed; this migration brings existing installs to the same shape.

The pins are weighted toward what the migration must NOT do:

  * it must not touch a row an operator deactivated: a pair that exists,
    whatever its `is_active`, is left alone and listed under `already`;
  * it must not seed twice: a second run inserts nothing;
  * it declares `MIGRATION_DATA_CLASS == "none"`: one fixed catalog row,
    identical on every install, so it writes no audit trail;
  * it must not touch the (`expense_claim`, `gl_entry`) row the claim
    approval has always carried.

Every pin runs the REAL migration module against a real database
initialized by `init_schema`, rewound to its genuine pre-050 shape (the
payment_allocation row deleted).
"""
import importlib.util
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "050_expense_claim_payment_allocation_voucher_type.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import call_action, is_ok, load_db_query, ns

from erpclaw_lib.query import Field, P, Q, Table


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_050", _MIGRATION)

mod = load_db_query()


def _rewind_to_pre050(conn):
    """Delete the payment_allocation row, returning the database to its pre-050 shape."""
    t = Table("voucher_type_registry")
    conn.execute(
        Q.from_(t).delete()
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("expense_claim", "payment_allocation"))
    conn.commit()


def _row(conn, target):
    t = Table("voucher_type_registry")
    return conn.execute(
        Q.from_(t).select(t.star)
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("expense_claim", target)).fetchone()


def _count(conn, target):
    t = Table("voucher_type_registry")
    return conn.execute(
        Q.from_(t).select(Field("voucher_type"))
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("expense_claim", target)).fetchall()


def test_run_seeds_the_missing_row(conn, db_path):
    _rewind_to_pre050(conn)
    assert _row(conn, "payment_allocation") is None

    result = mig.run_migration(db_path)
    assert result == {"seeded": ["expense_claim"], "already": []}

    row = dict(_row(conn, "payment_allocation"))
    assert row["is_active"] == 1
    assert row["skill_name"] == "erpclaw-hr"
    assert row["label"] == "Expense Claim"
    assert row["target_table"] == "payment_allocation"


def test_second_run_seeds_nothing(conn, db_path):
    _rewind_to_pre050(conn)
    assert mig.run_migration(db_path) == {
        "seeded": ["expense_claim"], "already": []}

    result = mig.run_migration(db_path)
    assert result == {"seeded": [], "already": ["expense_claim"]}
    assert len(_count(conn, "payment_allocation")) == 1
    assert dict(_row(conn, "payment_allocation"))["is_active"] == 1


def test_deactivated_row_is_left_alone(conn, db_path):
    _rewind_to_pre050(conn)
    assert mig.run_migration(db_path)["seeded"] == ["expense_claim"]

    off = call_action(mod.deactivate_voucher_type, conn, ns(
        voucher_type="expense_claim", target_table="payment_allocation"))
    assert is_ok(off), off
    assert dict(_row(conn, "payment_allocation"))["is_active"] == 0

    result = mig.run_migration(db_path)
    assert result == {"seeded": [], "already": ["expense_claim"]}
    assert len(_count(conn, "payment_allocation")) == 1
    assert dict(_row(conn, "payment_allocation"))["is_active"] == 0


def test_gl_entry_row_is_untouched_by_every_run(conn, db_path):
    before = dict(_row(conn, "gl_entry"))
    assert before["skill_name"] == "erpclaw-hr"

    _rewind_to_pre050(conn)
    assert mig.run_migration(db_path)["seeded"] == ["expense_claim"]
    assert mig.run_migration(db_path)["already"] == ["expense_claim"]

    after = dict(_row(conn, "gl_entry"))
    assert after == before


def test_migration_data_class_is_none():
    assert mig.MIGRATION_DATA_CLASS == "none"
