"""Migration 023: Foundation nullable FK columns to addon-owned CRM entities (Wave 1B F1).

Per ADR-0023 (foundation FK columns for addon-owned entities),
foundation schema MAY carry nullable FK columns pointing at tables owned by the
erpclaw-growth addon (crm_contact / crm_company), so a "show the contact for this
lead" read needs at most one optional JOIN rather than forcing the addon to be a
runtime dependency of every foundation read. The addon is the SOLE writer of these
columns (Article 5 unchanged); foundation reads them as opaque TEXT references.

Owning addon: erpclaw-growth (crm_contact, crm_company tables).

Adds six nullable FK columns:
  - lead.crm_contact_id          -> crm_contact(id)
  - lead.crm_company_id          -> crm_company(id)
  - opportunity.crm_contact_id   -> crm_contact(id)
  - opportunity.crm_company_id   -> crm_company(id)
  - customer.crm_company_id      -> crm_company(id)   (back-reference)
  - crm_activity.crm_contact_id  -> crm_contact(id)   (4th nullable parent)

All columns are nullable with no DEFAULT, so a foundation-only install (growth
absent) operates with them simply unpopulated. Per ADR-0023 the columns are
OPAQUE TEXT references — NOT SQL-level inline FKs. SQLite resolves a column's
REFERENCES target at INSERT time even for a NULL value, so an inline
`REFERENCES crm_contact(id)` would break every `add-lead` on a foundation-only
install where crm_contact does not exist ("no such table: crm_contact"). Probed
2026-06-15; see the SIM-0 record in the F1 contract. FK integrity is therefore
enforced application-side: growth (the sole writer) validates the target exists
before populating the column. Plain ADD COLUMN of a plain TEXT column (no
rebuild). Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

# M102: adds tables / columns / indexes only — nothing a row held before this
# run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# (table, column). Nullable, no DEFAULT, plain TEXT (opaque reference — see ADR-0023
# + the docstring note on SQLite forward-FK INSERT resolution).
_COLUMNS = [
    ("lead", "crm_contact_id"),
    ("lead", "crm_company_id"),
    ("opportunity", "crm_contact_id"),
    ("opportunity", "crm_company_id"),
    ("customer", "crm_company_id"),
    ("crm_activity", "crm_contact_id"),
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    added = []
    for table, column in _COLUMNS:
        if _sqlite_has_table(conn, table) and not _sqlite_has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            added.append(f"{table}.{column}")
    conn.commit()
    conn.close()
    print(f"  CRM FK columns added: {', '.join(added) if added else '(none — already present)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for table, column in _COLUMNS:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")
        conn.commit()
        print("  Postgres: CRM FK columns ensured.")
    finally:
        conn.close()


def run_migration(db_path=None):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). Nothing to migrate.")
            return
        _run_postgres(url)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migration 023: CRM contact/company FK columns")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 023 complete.")
