"""Migration 034: payment_deduction.type gains 'write_off' (Wave G F17b).

The CHECK on ``payment_deduction.type`` admits tds / commission /
early_payment_discount / other. It does NOT admit ``write_off``, so the case the
roadmap row named — a customer pays 950.00 against a 1,000.00 invoice and the
50.00 residual is written off AT PAYMENT TIME — cannot be recorded as what it is.
That path is a real deduction against real cash (allocations are present, so the
shipped pro-rata deduction machinery clears the invoice completely), and the only
thing missing was the vocabulary.

SCOPE IS EXACTLY ONE VALUE. ``payment_entry_id`` stays ``NOT NULL``: the
standalone, no-cash write-off moved off this table entirely and onto its own
primitive (``write-off-invoice``, Wave G F17a / plan correction C4), because a
deduction without a payment is not representable here and fabricating a zero-cash
payment to carry one is refused by four separate shipped gates. Nothing else on
this table changes, and no stored value is rewritten — the widen only makes a
previously impossible INSERT possible.

The Python-side gate is the other half and ships in the same change-set:
``VALID_DEDUCTION_TYPES`` in erpclaw-payments/db_query.py rejected 'write_off'
before the CHECK was ever reached, so widening only the schema would have left
the value unreachable through every real entry point.

SQLite has no ALTER for a CHECK, so the table is rebuilt: rename -> recreate with
the widened CHECK -> intersection-copy -> drop -> recreate the captured indexes.
That is migration 006's idiom, including legacy_alter_table=ON so the rename does
not rewrite inbound FK references, the dropped-column abort, and the row-count +
foreign_key_check verification. PostgreSQL drops and re-adds the auto-named
constraint (``payment_deduction_type_check``), the same name migrations 003-006
rely on and that test_migration_pg_drop_constraint.py proves against a live
server.

Every statement below is a FIXED string — no table name, column name or value is
ever formatted into SQL (migration 031's rule, kept here because 006 predates
it). The only substitution is SQLite's '?' -> psycopg2's '%s', and this file has
exactly one parameterized statement.

Idempotent: the DDL-marker probe makes a second run a no-op on both dialects.
``--report-only`` writes nothing and states exactly what the real run would do.
"""
import argparse
import os
import re
import sqlite3

# M102: rebuilds a table and copies every row verbatim — nothing a row held
# before this run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_TABLE = "payment_deduction"
_PG_CONSTRAINT = "payment_deduction_type_check"
_NEW_VALUE = "write_off"

# The idempotence probe parses the CHECK clause; it does NOT substring-match the
# stored DDL. SQLite keeps the CREATE TABLE text VERBATIM in sqlite_master —
# comments included — and init_schema.py documents this very value in a comment
# directly above the column. A substring probe therefore reports "already
# widened" on a database whose CHECK still REJECTS write_off, and the migration
# silently no-ops on exactly the installs that need it. Demonstrated live during
# QA (condition 1); the parse below is comment-immune because it anchors on the
# CHECK clause itself and reads only the value list inside it.
_SQLITE_CHECK_RE = re.compile(r"CHECK\s*\(\s*type\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_QUOTED_RE = re.compile(r"'((?:[^']|'')*)'")


def _sqlite_check_values(ddl):
    """The values ``CHECK(type IN (...))`` admits, or None when there is no CHECK.

    None is a real state, not an error: an absent CHECK constrains nothing, so
    every value is already admitted and there is nothing for 034 to widen.
    """
    match = _SQLITE_CHECK_RE.search(ddl or "")
    if not match:
        return None
    return tuple(v.replace("''", "'") for v in _QUOTED_RE.findall(match.group(1)))


def _pg_check_values(constraintdef):
    """The values a Postgres CHECK constraint admits.

    ``pg_get_constraintdef`` renders an IN list as ``= ANY (ARRAY['a'::text,
    ...])`` and carries no comments, so scanning its quoted literals is exact.
    """
    return tuple(v.replace("''", "'")
                 for v in _QUOTED_RE.findall(constraintdef or ""))

# Post-migration DDL, byte-aligned with init_schema.py's payment_deduction block
# (the same contract migration 006 keeps with its three tables): if the base
# schema gains a column, this DDL must gain it too or the dropped-column abort
# below fires rather than silently losing data.
_NEW_DDL = """
CREATE TABLE payment_deduction (
    id              TEXT PRIMARY KEY,
    payment_entry_id TEXT NOT NULL REFERENCES payment_entry(id) ON DELETE RESTRICT,
    account_id      TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
    amount          TEXT NOT NULL DEFAULT '0',
    type            TEXT NOT NULL CHECK(type IN ('tds','commission','early_payment_discount','write_off','other')),
    description     TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

_PG_DROP = "ALTER TABLE payment_deduction DROP CONSTRAINT IF EXISTS payment_deduction_type_check"
_PG_ADD = (
    "ALTER TABLE payment_deduction ADD CONSTRAINT payment_deduction_type_check "
    "CHECK (type IN ('tds','commission','early_payment_discount','write_off','other'))"
)
_PG_CONSTRAINT_DEF = ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                      "WHERE conname = ?")

_COUNT = "SELECT COUNT(*) FROM payment_deduction"
_TABLE_DDL = ("SELECT sql FROM sqlite_master WHERE type='table' "
              "AND name='payment_deduction'")
_TABLE_INFO = "PRAGMA table_info(payment_deduction)"
_INDEX_DDL = ("SELECT sql FROM sqlite_master WHERE type='index' "
              "AND tbl_name='payment_deduction' AND sql IS NOT NULL")
_RENAME = "ALTER TABLE payment_deduction RENAME TO payment_deduction_m034_old"
_DROP_OLD = "DROP TABLE payment_deduction_m034_old"
_DANGLING = ("SELECT name FROM sqlite_master WHERE type='table' "
             "AND sql LIKE '%payment_deduction_m034_old%'")

# The intersection-copy. 006 builds its column list at runtime because it rebuilds
# three differently-shaped tables; this migration rebuilds exactly one whose
# column set is fixed above, so the list is written out and the runtime check
# below asserts the DDL and the real table still agree instead of quietly
# copying a subset.
_COLUMNS = ("id", "payment_entry_id", "account_id", "amount", "type",
            "description", "created_at")
_COPY = ("INSERT INTO payment_deduction "
         "(id, payment_entry_id, account_id, amount, type, description, created_at) "
         "SELECT id, payment_entry_id, account_id, amount, type, description, "
         "created_at FROM payment_deduction_m034_old")


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_needs_widen(conn):
    """True when the CHECK still lacks the new value; None when the table is absent.

    Reads sqlite_master rather than probing with a throwaway INSERT: a probe
    would need a real payment_entry to satisfy the FK, and a migration must not
    depend on the data it happens to find. The decision is made on the PARSED
    CHECK values, never on a substring of the DDL text — see _sqlite_check_values.
    """
    row = conn.execute(_TABLE_DDL).fetchone()
    if not row:
        return None
    values = _sqlite_check_values(row[0])
    if values is None:
        return False  # no CHECK at all: nothing constrains the column
    return _NEW_VALUE not in values


def _rebuild_sqlite(conn):
    """Rebuild payment_deduction with the widened CHECK (migration 006 idiom)."""
    old_cols = [r[1] for r in conn.execute(_TABLE_INFO)]
    unknown = [c for c in old_cols if c not in _COLUMNS]
    if unknown:
        raise RuntimeError(
            "Migration 034 abort: payment_deduction has columns absent from the "
            "target DDL that would be dropped: " + repr(unknown) +
            ". Update _NEW_DDL and _COPY together.")
    index_defs = [r[0] for r in conn.execute(_INDEX_DDL)]
    before = conn.execute(_COUNT).fetchone()[0]
    conn.execute(_RENAME)
    conn.execute(_NEW_DDL)
    conn.execute(_COPY)
    conn.execute(_DROP_OLD)
    for ddl in index_defs:
        conn.execute(ddl)
    after = conn.execute(_COUNT).fetchone()[0]
    if after != before:
        raise RuntimeError(
            "Migration 034 row-count mismatch on payment_deduction: "
            f"{before} -> {after}")
    return before


def _run_sqlite(path, report_only=False):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    needs = _sqlite_needs_widen(conn)
    if needs is None:
        print("  payment_deduction: table absent. Nothing to migrate.")
        conn.close()
        return
    if not needs:
        print("  payment_deduction.type already admits 'write_off'; nothing to do.")
        conn.close()
        return
    if report_only:
        rows = conn.execute(_COUNT).fetchone()[0]
        print("  payment_deduction.type would be widened to admit 'write_off' "
              f"(table rebuild, {rows} row(s) copied verbatim, no value "
              "rewritten).")
        conn.close()
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    # See migrations 003/006: without this the RENAME rewrites inbound FK
    # references to the dropped *_m034_old name. Nothing points at
    # payment_deduction today; the guard is part of the idiom and costs nothing.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        copied = _rebuild_sqlite(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError("Migration 034 left " + str(len(violations)) +
                           " FK violations: " + repr(violations[:5]))
    dangling = conn.execute(_DANGLING).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError("Migration 034 left dangling FK refs to "
                           "payment_deduction_m034_old: " +
                           repr([r[0] for r in dangling]))
    print("  payment_deduction.type widened to admit 'write_off' "
          f"({copied} row(s) preserved); FK check clean.")
    conn.close()


def _run_postgres(url, report_only=False):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(_PG_CONSTRAINT_DEF.replace("?", "%s"), (_PG_CONSTRAINT,))
            row = cur.fetchone()
            if row and _NEW_VALUE in _pg_check_values(row[0]):
                print("  Postgres: payment_deduction.type already admits "
                      "'write_off'; nothing to do.")
                return
            if report_only:
                # Report mode never runs DDL — not even a rolled-back one; an
                # ALTER still takes the table lock (migration 031's rule).
                print("  Postgres: payment_deduction.type would be widened to "
                      "admit 'write_off' (DROP + ADD CONSTRAINT "
                      "payment_deduction_type_check).")
                return
            cur.execute(_PG_DROP)
            cur.execute(_PG_ADD)
        conn.commit()
        print("  Postgres: payment_deduction.type widened to admit 'write_off'.")
    finally:
        conn.close()


def run_migration(db_path=None, report_only=False):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). Nothing to migrate.")
            return
        _run_postgres(url, report_only=report_only)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path, report_only=report_only)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 034: widen payment_deduction.type to admit write_off")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would change; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("Migration 034 "
          + ("report complete (no writes)." if args.report_only else "complete."))
