"""Part of migration 043: register the `revenue_recognition` voucher type.

`recognize-schedule-entry` and `generate-revenue-entries` post DR deferred
revenue / CR revenue through `insert_gl_entries`, which enforces voucher_type
validity against `voucher_type_registry` (target_table='gl_entry'). Fresh
installs carry the row from init_schema's seed; this migration brings
existing installs to the same shape.

The pins are weighted toward what the migration must NOT do:

  * it must not touch a row an operator deactivated: a pair that exists,
    whatever its `is_active`, is left alone and listed under `already`;
  * it must not seed twice: a second run inserts nothing;
  * it declares `MIGRATION_DATA_CLASS == "none"`: one fixed catalog row,
    identical on every install, so it writes no audit trail.

Every pin runs the REAL migration module against a real database
initialized by `init_schema`, rewound to its genuine pre-043 shape (the
row deleted).
"""
import importlib.util
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "043_revenue_recognition_voucher_type.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import call_action, is_ok, load_db_query, ns

from erpclaw_lib.query import Field, P, Q, Table


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_043", _MIGRATION)

mod = load_db_query()


def _rewind_to_pre043(conn):
    """Delete the row, returning the database to its pre-043 shape."""
    t = Table("voucher_type_registry")
    conn.execute(
        Q.from_(t).delete()
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("revenue_recognition", "gl_entry"))
    conn.commit()


def _row(conn):
    t = Table("voucher_type_registry")
    return conn.execute(
        Q.from_(t).select(t.star)
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("revenue_recognition", "gl_entry")).fetchone()


def _count(conn):
    t = Table("voucher_type_registry")
    return conn.execute(
        Q.from_(t).select(Field("voucher_type"))
        .where(t.voucher_type == P())
        .where(t.target_table == P()).get_sql(),
        ("revenue_recognition", "gl_entry")).fetchall()


def test_run_seeds_the_missing_row(conn, db_path):
    _rewind_to_pre043(conn)
    assert _row(conn) is None

    result = mig.run_migration(db_path)
    assert result == {"seeded": ["revenue_recognition"], "already": []}

    row = dict(_row(conn))
    assert row["is_active"] == 1
    assert row["skill_name"] == "erpclaw-accounting-adv"
    assert row["label"] == "Revenue Recognition"
    assert row["target_table"] == "gl_entry"


def test_second_run_seeds_nothing(conn, db_path):
    _rewind_to_pre043(conn)
    assert mig.run_migration(db_path) == {
        "seeded": ["revenue_recognition"], "already": []}

    result = mig.run_migration(db_path)
    assert result == {"seeded": [], "already": ["revenue_recognition"]}
    assert len(_count(conn)) == 1
    assert dict(_row(conn))["is_active"] == 1


def test_deactivated_row_is_left_alone(conn, db_path):
    _rewind_to_pre043(conn)
    assert mig.run_migration(db_path)["seeded"] == ["revenue_recognition"]

    off = call_action(mod.deactivate_voucher_type, conn, ns(
        voucher_type="revenue_recognition", target_table="gl_entry"))
    assert is_ok(off), off
    assert dict(_row(conn))["is_active"] == 0

    result = mig.run_migration(db_path)
    assert result == {"seeded": [], "already": ["revenue_recognition"]}
    assert len(_count(conn)) == 1
    assert dict(_row(conn))["is_active"] == 0


def test_migration_data_class_is_none():
    assert mig.MIGRATION_DATA_CLASS == "none"
