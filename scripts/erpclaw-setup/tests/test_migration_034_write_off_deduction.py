"""Part A — migration 034: payment_deduction.type gains 'write_off' (Wave G F17b).

Plan §5 row 034 and §4 F17 pin 4: *a 950-on-1,000 payment with a 50 write_off
deduction is accepted post-034 and rejected pre-034.* Both halves are pinned, and
the rejection half is the one that matters — a widen nobody proved was needed is
a widen nobody can trust.

Two gates stand between the operator and the column, and F17b moves BOTH in one
change-set:

  1. ``VALID_DEDUCTION_TYPES`` in erpclaw-payments/db_query.py, which rejects the
     value with clean JSON before any SQL runs (the SIM-0 run-2 residue: widening
     only the schema leaves the value unreachable through every real entry point);
  2. the CHECK on the column itself.

Every pin runs the REAL migration module against a real SQLite file rebuilt into
its genuine pre-034 shape. The migration-class properties from ADR-0028 / plan §5
are pinned too: ``--report-only`` leaves the database byte-identical (canonical
dump compared, not eyeballed), a second real run is a no-op, and no stored row is
rewritten.
"""
import importlib.util
import io
import os
import re
import sqlite3
import sys
import uuid
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_SETUP_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "034_widen_payment_deduction_type.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_034", _MIGRATION)


# ── pre-034 shape ────────────────────────────────────────────────────────────

_PRE034_DDL = """
CREATE TABLE payment_deduction (
    id              TEXT PRIMARY KEY,
    payment_entry_id TEXT NOT NULL REFERENCES payment_entry(id) ON DELETE RESTRICT,
    account_id      TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
    amount          TEXT NOT NULL DEFAULT '0',
    type            TEXT NOT NULL CHECK(type IN ('tds','commission','early_payment_discount','other')),
    description     TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _rewind_to_pre034(conn):
    """Rebuild payment_deduction exactly as it shipped before this migration."""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE payment_deduction RENAME TO payment_deduction_pre034")
    conn.execute(_PRE034_DDL)
    conn.execute(
        "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
        " amount, type, description, created_at) "
        "SELECT id, payment_entry_id, account_id, amount, type, description, "
        "       created_at FROM payment_deduction_pre034")
    conn.execute("DROP TABLE payment_deduction_pre034")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_deduction_pe "
                 "ON payment_deduction(payment_entry_id)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _dump(db_path):
    """Canonical full-database dump — the byte-identical oracle."""
    probe = sqlite3.connect(db_path)
    try:
        return "\n".join(probe.iterdump())
    finally:
        probe.close()


def _run(db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        mig.run_migration(db_path, report_only=report_only)
    return buf.getvalue()


def _uid():
    return str(uuid.uuid4())


def _table_sql(conn):
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='payment_deduction'").fetchone()[0]


def _indexes(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='payment_deduction' AND sql IS NOT NULL"))


# ── a real payment carrying a real deduction ────────────────────────────────

def _seed(conn):
    """950.00 received against a 1,000.00 invoice; the 50.00 residual is the
    write-off. Seeded as rows rather than driven through submit-payment: this
    file's subject is the migration, and the action-level acceptance is pinned
    below through the REAL _insert_deductions gate."""
    company, ar, bank, customer, pe = (_uid() for _ in range(5))
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (company, f"Mig Co {company[:6]}", f"MC{company[:4]}"))
    for acct, name in ((ar, "Debtors"), (bank, "Bank")):
        conn.execute(
            "INSERT INTO account (id, name, account_number, root_type, "
            " balance_direction, company_id, depth) "
            "VALUES (?, ?, ?, 'asset', 'debit_normal', ?, 0)",
            (acct, name, f"ACC-{acct[:6]}", company))
    bad_debt = _uid()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, "
        " balance_direction, company_id, depth) "
        "VALUES (?, 'Bad Debt Expense', ?, 'expense', 'debit_normal', ?, 0)",
        (bad_debt, f"ACC-{bad_debt[:6]}", company))
    conn.execute("INSERT INTO customer (id, name, company_id) VALUES (?, 'Wayne', ?)",
                 (customer, company))
    conn.execute(
        "INSERT INTO payment_entry (id, payment_type, posting_date, party_type, "
        " party_id, paid_from_account, paid_to_account, paid_amount, "
        " unallocated_amount, status, company_id) "
        "VALUES (?, 'receive', '2026-06-01', 'customer', ?, ?, ?, '950.00', "
        " '0', 'submitted', ?)",
        (pe, customer, ar, bank, company))
    # One PRE-EXISTING deduction of a type the old CHECK already admitted, so the
    # rebuild has a real row to preserve.
    conn.execute(
        "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
        " amount, type, description) VALUES (?, ?, ?, '5.00', 'commission', ?)",
        (_uid(), pe, bad_debt, "processor fee"))
    conn.commit()
    return {"company": company, "ar": ar, "bank": bank, "customer": customer,
            "pe": pe, "bad_debt": bad_debt}


def _try_write_off_insert(conn, env):
    """Attempt the F17b row. Returns None on success, the error string on reject."""
    try:
        conn.execute(
            "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
            " amount, type, description) VALUES (?, ?, ?, '50.00', 'write_off', ?)",
            (_uid(), env["pe"], env["bad_debt"], "uncollectable residual"))
        conn.commit()
        return None
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return str(e)


# ── pin 4 — rejected pre-034, accepted post-034 ─────────────────────────────

def test_pin4_write_off_deduction_rejected_pre_034_accepted_post(conn, db_path,
                                                                 capsys):
    """The 950-on-1,000 case: 50.00 written off at payment time."""
    env = _seed(conn)
    _rewind_to_pre034(conn)

    rejected = _try_write_off_insert(conn, env)
    print(f"\npre-034 INSERT type='write_off' -> {rejected}")
    assert rejected is not None, "pre-034 the CHECK must refuse 'write_off'"
    assert "CHECK constraint failed" in rejected
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_deduction WHERE type='write_off'"
    ).fetchone()[0] == 0

    report = _run(db_path)
    print(f"034 -> {report.strip()}")
    assert "widened to admit 'write_off'" in report

    accepted = _try_write_off_insert(conn, env)
    print(f"post-034 INSERT type='write_off' -> {accepted or 'accepted'}")
    assert accepted is None, "post-034 the same INSERT must succeed"
    row = conn.execute(
        "SELECT amount, description FROM payment_deduction WHERE type='write_off'"
    ).fetchone()
    assert row["amount"] == "50.00"
    assert row["description"] == "uncollectable residual"


def test_the_python_gate_moves_with_the_check(conn):
    """SIM-0 run-2 residue: VALID_DEDUCTION_TYPES rejected it before the CHECK.

    Driven through the REAL ``_insert_deductions``, which is the gate every entry
    point goes through — a schema-only widen would leave this red.
    """
    payments = _load("db_query_payments_034",
                     os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py"))
    assert "write_off" in payments.VALID_DEDUCTION_TYPES
    env = _seed(conn)
    total = payments._insert_deductions(
        conn, env["pe"], "receive",
        [{"account_id": env["bad_debt"], "amount": "50.00", "type": "write_off",
          "description": "uncollectable residual"}])
    conn.commit()
    assert str(total) == "50.00"
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_deduction WHERE type='write_off'"
    ).fetchone()[0] == 1


def test_payment_entry_id_stays_not_null(conn, db_path):
    """Correction C4's scope line: 034 widens ONE value and nothing else.

    The standalone no-cash write-off moved to its own primitive precisely because
    a deduction without a payment is not representable here. If this column ever
    goes nullable, that decision has been quietly reversed.
    """
    _seed(conn)
    _rewind_to_pre034(conn)
    _run(db_path)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(payment_deduction)")}
    assert cols["payment_entry_id"][3] == 1, "payment_entry_id must stay NOT NULL"
    assert cols["account_id"][3] == 1
    assert cols["type"][3] == 1


# ── migration-class properties (ADR-0028 / plan §5) ─────────────────────────

def test_report_only_leaves_the_database_byte_identical(conn, db_path):
    _seed(conn)
    _rewind_to_pre034(conn)
    conn.close()

    before = _dump(db_path)
    report = _run(db_path, report_only=True)
    after = _dump(db_path)

    assert "would be widened to admit 'write_off'" in report
    assert "1 row(s) copied verbatim" in report
    assert before == after, "--report-only wrote to the database"


def test_second_run_is_a_no_op(conn, db_path):
    _seed(conn)
    _rewind_to_pre034(conn)
    conn.close()

    first = _run(db_path)
    assert "widened to admit 'write_off'" in first
    after_first = _dump(db_path)

    second = _run(db_path)
    assert "already admits 'write_off'" in second
    assert _dump(db_path) == after_first, "the second run changed the database"


def test_rows_and_indexes_survive_the_rebuild(conn, db_path):
    env = _seed(conn)
    # A second pre-existing row, so "preserved verbatim" is more than one row.
    conn.execute(
        "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
        " amount, type, description) VALUES (?, ?, ?, '12.34', 'tds', 'withheld')",
        (_uid(), env["pe"], env["bad_debt"]))
    conn.commit()
    _rewind_to_pre034(conn)
    before_rows = conn.execute(
        "SELECT id, amount, type, description FROM payment_deduction "
        "ORDER BY id").fetchall()
    before_indexes = _indexes(conn)
    conn.close()

    _run(db_path)

    probe = sqlite3.connect(db_path)
    probe.row_factory = sqlite3.Row
    after_rows = probe.execute(
        "SELECT id, amount, type, description FROM payment_deduction "
        "ORDER BY id").fetchall()
    assert [tuple(r) for r in after_rows] == [tuple(r) for r in before_rows]
    assert sorted(r[0] for r in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='payment_deduction' AND sql IS NOT NULL")) == before_indexes
    assert probe.execute("PRAGMA foreign_key_check").fetchall() == []
    assert probe.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%_m034_old%'"
    ).fetchall() == []
    probe.close()


def test_fresh_install_needs_no_widen(conn, db_path):
    """init_schema already ships the widened CHECK, so 034 is a no-op there.

    Asserted on the PARSED CHECK values, not on a substring of the DDL:
    init_schema documents this same value in a comment directly above the
    column, and SQLite stores comments verbatim in sqlite_master, so a substring
    assertion here passes on a database whose CHECK rejects write_off.
    """
    assert _check_values(_table_sql(conn)) == _EXPECTED_VALUES
    conn.close()
    report = _run(db_path)
    assert "already admits 'write_off'" in report


def test_a_comment_mentioning_the_value_does_not_satisfy_the_probe(conn, db_path):
    """The condition-1 regression, pinned as its own negative control.

    A table whose CHECK REJECTS write_off but whose DDL text mentions it in a
    comment must be reported as needing the widen, and the real run must widen
    it. This is the exact shape init_schema ships (the value is documented in a
    comment above the column), which is why a substring probe silently no-opped
    on precisely the installs that needed the migration.
    """
    _seed(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE payment_deduction RENAME TO payment_deduction_cmt")
    conn.execute("""
CREATE TABLE payment_deduction (
    id              TEXT PRIMARY KEY,
    payment_entry_id TEXT NOT NULL REFERENCES payment_entry(id) ON DELETE RESTRICT,
    account_id      TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
    amount          TEXT NOT NULL DEFAULT '0',
    -- 'write_off' is documented here exactly as init_schema.py documents it.
    type            TEXT NOT NULL CHECK(type IN ('tds','commission','early_payment_discount','other')),
    description     TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)""")
    conn.execute(
        "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
        " amount, type, description, created_at) "
        "SELECT id, payment_entry_id, account_id, amount, type, description, "
        "       created_at FROM payment_deduction_cmt")
    conn.execute("DROP TABLE payment_deduction_cmt")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()

    stored = _table_sql(conn)
    assert "'write_off'" in stored, "fixture precondition: the comment is present"
    assert "write_off" not in _check_values(stored), \
        "fixture precondition: the CHECK still rejects it"
    assert mig._sqlite_needs_widen(conn) is True, \
        "a comment mentioning the value must not satisfy the probe"

    report = _run(db_path)
    assert "widened to admit 'write_off'" in report
    assert _check_values(_table_sql(conn)) == _EXPECTED_VALUES


def test_absent_table_is_reported_not_crashed(tmp_path):
    path = str(tmp_path / "empty.sqlite")
    sqlite3.connect(path).close()
    report = _run(path)
    assert "table absent" in report


def test_missing_database_is_reported_not_crashed(tmp_path):
    report = _run(str(tmp_path / "nope.sqlite"))
    assert "Database not found" in report


# ── PG lane (plan §4 F17 pin 6) ─────────────────────────────────────────────
#
# The Postgres branch is DROP + ADD CONSTRAINT rather than a table rebuild, so
# nothing above exercises it. Two layers, because the live layer cannot run in
# CI: a dialect-free pin on the two things that can be wrong statically (the
# constraint NAME the DROP hardcodes, and the value list the ADD installs), and
# a real live-server lane gated on ERPCLAW_PG_TEST_URL — the same gate and the
# same expendable-database warning as test_migration_pg_drop_constraint.py.

_EXPECTED_VALUES = ("tds", "commission", "early_payment_discount", "write_off",
                    "other")

_CHECK_IN_LIST = re.compile(r"type IN \(([^)]*)\)")


def _check_values(ddl):
    """The tuple of values a ``CHECK(type IN (...))`` admits, in written order."""
    m = _CHECK_IN_LIST.search(ddl)
    assert m, f"no CHECK(type IN (...)) found in: {ddl!r}"
    return tuple(v.strip().strip("'") for v in m.group(1).split(","))


def test_pg_statements_carry_the_auto_name_and_exactly_the_widened_values():
    """Runs on every machine: the PG path's two literals must stay correct.

    The DROP hinges on Postgres' auto-name for an unnamed inline column CHECK,
    ``<table>_<column>_check`` — the same assumption migrations 003-006 make and
    that the live test below proves. The ADD must install the SQLite list
    verbatim, or the two dialects silently disagree about what a deduction is.
    """
    assert mig._PG_CONSTRAINT == "payment_deduction_type_check"
    assert mig._PG_CONSTRAINT in mig._PG_DROP and "IF EXISTS" in mig._PG_DROP
    # EXACTLY these values on both dialects: a missing one makes the feature
    # unreachable, an extra one admits what nothing validates, and a difference
    # between the two lists means SQLite and Postgres disagree about what a
    # deduction is. Parsed out of the CHECK rather than substring-matched so all
    # three failures are caught by the same assertion.
    assert _check_values(mig._PG_ADD) == _EXPECTED_VALUES
    assert _check_values(mig._NEW_DDL) == _EXPECTED_VALUES
    assert _check_values(_PRE034_DDL) == tuple(
        v for v in _EXPECTED_VALUES if v != "write_off"), \
        "the pre-034 fixture must be the genuine shipped shape, one value short"


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

_PG_PRE034_DDL = """
CREATE TABLE payment_deduction (
    id              TEXT PRIMARY KEY,
    payment_entry_id TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    amount          TEXT NOT NULL DEFAULT '0',
    type            TEXT NOT NULL CHECK(type IN ('tds','commission','early_payment_discount','other')),
    description     TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_lane_widens_the_constraint_and_is_idempotent(monkeypatch):
    """DROPS the ``public`` schema — never point ERPCLAW_PG_TEST_URL at real data."""
    import psycopg2

    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)

    setup = psycopg2.connect(_PG_URL)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_PG_PRE034_DDL)
        cur.execute(
            "INSERT INTO payment_deduction (id, payment_entry_id, account_id, "
            " amount, type) VALUES (%s, %s, %s, '5.00', 'commission')",
            (_uid(), _uid(), _uid()))
    setup.close()

    def _constraint_def():
        conn = psycopg2.connect(_PG_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conname = %s", (mig._PG_CONSTRAINT,))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def _insert_write_off():
        conn = psycopg2.connect(_PG_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO payment_deduction (id, payment_entry_id, "
                    " account_id, amount, type) VALUES (%s, %s, %s, '50.00', "
                    " 'write_off')", (_uid(), _uid(), _uid()))
            conn.commit()
            return None
        except psycopg2.errors.CheckViolation as e:
            conn.rollback()
            return str(e)
        finally:
            conn.close()

    # The hardcoded DROP name is the name Postgres actually assigned.
    assert _constraint_def() is not None, (
        f"Postgres did not auto-name the CHECK '{mig._PG_CONSTRAINT}'")
    assert "write_off" not in _constraint_def()
    assert _insert_write_off() is not None, "pre-034 PG must refuse 'write_off'"

    # --report-only states the change and performs none of it.
    report = _run(_PG_URL, report_only=True)
    assert "would be widened to admit 'write_off'" in report
    assert "write_off" not in _constraint_def()

    real = _run(_PG_URL)
    assert "widened to admit 'write_off'" in real
    assert "write_off" in _constraint_def()
    assert _insert_write_off() is None, "post-034 PG must accept 'write_off'"

    # Idempotent: a second run reports the no-op and changes nothing.
    again = _run(_PG_URL)
    assert "already admits 'write_off'" in again

    probe = psycopg2.connect(_PG_URL)
    with probe.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM payment_deduction WHERE type='commission'")
        assert cur.fetchone()[0] == 1, "the pre-existing row survived"
    probe.close()
