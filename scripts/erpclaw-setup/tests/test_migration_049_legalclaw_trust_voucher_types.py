"""Migration 049: register the four LegalClaw trust voucher types for existing installs.

Fresh installs carry the rows in init_schema's seed; this migration brings
existing databases to the same shape. Idempotent, and it never reactivates a
row an operator switched off.
"""
import argparse
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "049_legalclaw_trust_voucher_types.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import call_action, is_ok, load_db_query  # noqa: E402
from erpclaw_lib.query import Q, Table, Field, P  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_049", _MIGRATION)

mod = load_db_query()

TRUST_TYPES = ("Trust Deposit", "Trust Disbursement", "Trust Transfer",
               "Trust Interest")


def _rewind_to_pre049(conn):
    """Remove the four trust rows, the way a pre-049 install holds the table."""
    t = Table("voucher_type_registry")
    for voucher_type in TRUST_TYPES:
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
    result = call_action(
        mod.deactivate_voucher_type, conn,
        argparse.Namespace(voucher_type=voucher_type,
                           target_table="gl_entry"),
    )
    assert is_ok(result), result


class TestMigration049:
    def test_fresh_install_already_has_all_four_active(self, conn):
        for voucher_type in TRUST_TYPES:
            rows = _row(conn, voucher_type)
            assert len(rows) == 1
            assert rows[0]["target_table"] == "gl_entry"
            assert rows[0]["skill_name"] == "legalclaw"
            assert rows[0]["label"] == voucher_type
            assert rows[0]["is_active"] == 1

    def test_registers_all_four_rows(self, conn, db_path):
        _rewind_to_pre049(conn)
        result, _ = _run(db_path)
        assert result == {"seeded": list(TRUST_TYPES), "already": []}
        for voucher_type in TRUST_TYPES:
            rows = _row(conn, voucher_type)
            assert len(rows) == 1
            assert rows[0]["target_table"] == "gl_entry"
            assert rows[0]["skill_name"] == "legalclaw"
            assert rows[0]["label"] == voucher_type
            assert rows[0]["is_active"] == 1

    def test_second_run_is_a_no_op(self, conn, db_path):
        _rewind_to_pre049(conn)
        _run(db_path)
        result, _ = _run(db_path)
        assert result == {"seeded": [], "already": list(TRUST_TYPES)}
        for voucher_type in TRUST_TYPES:
            assert len(_row(conn, voucher_type)) == 1

    def test_leaves_an_operator_disabled_row_disabled(self, conn, db_path):
        _rewind_to_pre049(conn)
        _run(db_path)
        _deactivate(conn, "Trust Disbursement")
        result, _ = _run(db_path)
        assert "Trust Disbursement" in result["already"]
        assert "Trust Disbursement" not in result["seeded"]
        assert _row(conn, "Trust Disbursement")[0]["is_active"] == 0

    def test_declares_no_data_change(self):
        assert mig.MIGRATION_DATA_CLASS == "none"

    def test_hand_registered_row_is_left_exactly_as_it_was(self, conn, db_path):
        _rewind_to_pre049(conn)
        r = call_action(
            mod.add_voucher_type, conn,
            argparse.Namespace(voucher_type="Trust Deposit",
                               target_table="gl_entry", label=None,
                               skill_name=None),
        )
        assert is_ok(r), r
        result, _ = _run(db_path)
        assert result["already"] == ["Trust Deposit"]
        assert result["seeded"] == ["Trust Disbursement", "Trust Transfer",
                                    "Trust Interest"]
        rows = _row(conn, "Trust Deposit")
        assert len(rows) == 1
        assert rows[0]["skill_name"] == "custom"
        assert rows[0]["label"] == "Trust Deposit"
        assert rows[0]["is_active"] == 1

    def test_unknown_voucher_type_refusal_names_add_voucher_type(self, conn):
        from erpclaw_lib.gl_posting import insert_gl_entries
        with pytest.raises(ValueError) as excinfo:
            insert_gl_entries(
                conn,
                [{"account_id": "no-such-account", "debit": "1.00",
                  "credit": "0"}],
                voucher_type="no-such-type-ever",
                voucher_id="no-such-voucher",
                posting_date="2026-03-05",
                company_id="no-such-company",
            )
        message = str(excinfo.value)
        assert message.endswith("Register it with add-voucher-type.")
        assert "seed-registry-defaults" not in message
