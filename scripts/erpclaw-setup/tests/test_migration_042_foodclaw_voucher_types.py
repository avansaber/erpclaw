"""Migration 042: register the two FoodClaw voucher types for existing installs.

Fresh installs carry the rows in init_schema's seed; this migration brings
existing databases to the same shape. Idempotent, and it never reactivates a
row an operator switched off.
"""
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "042_foodclaw_voucher_types.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import call_action, is_ok, load_db_query  # noqa: E402
from erpclaw_lib.query import Q, Table, Field, P  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_042", _MIGRATION)


def _rewind_to_pre042(conn):
    """Remove the two FoodClaw rows, the way a pre-042 install holds the table."""
    t = Table("voucher_type_registry")
    for voucher_type in ("food_catering_revenue", "food_franchise_royalty"):
        conn.execute(
            Q.from_(t).delete().where(t.voucher_type == P()).get_sql(),
            (voucher_type,),
        )
    conn.commit()


def _row(conn, voucher_type):
    t = Table("voucher_type_registry")
    q = (Q.from_(t).select(t.voucher_type, t.skill_name, t.label,
                           t.target_table, t.is_active)
         .where(t.voucher_type == P()).where(t.target_table == P()))
    rows = [dict(r) for r in
            conn.execute(q.get_sql(), (voucher_type, "gl_entry")).fetchall()]
    return rows


def _run(db_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mig.run_migration(db_path)
    return result, buf.getvalue()


def _deactivate(conn, voucher_type):
    mod = load_db_query()
    result = call_action(
        mod.deactivate_voucher_type, conn,
        type("Args", (), {"voucher_type": voucher_type,
                          "target_table": "gl_entry"})(),
    )
    assert is_ok(result), result


class TestMigration042:
    def test_registers_both_rows(self, conn, db_path):
        _rewind_to_pre042(conn)
        result, _ = _run(db_path)
        assert result == {"seeded": ["food_catering_revenue",
                                     "food_franchise_royalty"],
                          "already": []}
        catering = _row(conn, "food_catering_revenue")
        assert len(catering) == 1
        assert catering[0]["target_table"] == "gl_entry"
        assert catering[0]["skill_name"] == "foodclaw"
        assert catering[0]["label"] == "Catering Revenue"
        assert catering[0]["is_active"] == 1
        royalty = _row(conn, "food_franchise_royalty")
        assert len(royalty) == 1
        assert royalty[0]["target_table"] == "gl_entry"
        assert royalty[0]["skill_name"] == "foodclaw"
        assert royalty[0]["label"] == "Franchise Royalty"
        assert royalty[0]["is_active"] == 1

    def test_second_run_is_a_no_op(self, conn, db_path):
        _rewind_to_pre042(conn)
        _run(db_path)
        result, _ = _run(db_path)
        assert result["seeded"] == []
        assert _row(conn, "food_catering_revenue") != []
        assert len(_row(conn, "food_catering_revenue")) == 1
        assert len(_row(conn, "food_franchise_royalty")) == 1

    def test_leaves_an_operator_disabled_row_disabled(self, conn, db_path):
        _rewind_to_pre042(conn)
        _run(db_path)
        _deactivate(conn, "food_catering_revenue")
        result, _ = _run(db_path)
        assert "food_catering_revenue" in result["already"]
        assert _row(conn, "food_catering_revenue")[0]["is_active"] == 0

    def test_declares_no_data_change(self):
        assert mig.MIGRATION_DATA_CLASS == "none"
