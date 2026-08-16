"""Migration 024: Displace the hardcoded opportunity.stage CHECK + add pipeline_stage_id (Wave 1B F3).

Per ADR-0023 (foundation FK columns for addon-owned entities),
foundation `opportunity` gains a nullable FK column `pipeline_stage_id` pointing at the
addon-owned `crm_pipeline_stage` table (owning addon: erpclaw-growth). The hardcoded
7-value `stage` CHECK is dropped so customizable pipelines can introduce novel stage
names; app-side `VALID_OPP_STAGES` (erpclaw-crm/db_query.py) remains the text-path
enforcement that replaces the CHECK on the legacy `stage` column.

Owning addon: erpclaw-growth (crm_pipeline, crm_pipeline_stage tables).

This migration:

1. Drops the `stage` CHECK from `opportunity` (dialect-aware):
     - SQLite: rename -> recreate WITHOUT the stage CHECK + WITH the nullable
       pipeline_stage_id column -> copy -> drop -> reindex. SQLite cannot ALTER
       TABLE DROP a CHECK. FK enforcement is OFF and legacy_alter_table is ON
       during the rebuild so the inbound crm_activity.opportunity_id FK keeps
       pointing at "opportunity" (not the temp *_f3_old name). opportunity_type
       CHECK + UNIQUE are retained. All opportunity ids preserved.
     - PostgreSQL: ALTER TABLE opportunity DROP CONSTRAINT IF EXISTS
       opportunity_stage_check (the name Postgres assigns the inline CHECK) +
       ADD COLUMN IF NOT EXISTS pipeline_stage_id TEXT.
2. Seeds the default "Standard Sales" 7-stage pipeline (Option A — self-contained,
   like the M0 seed migrations) IF the growth-owned crm_pipeline / crm_pipeline_stage
   tables exist. This guarantees the backfill in step 3 has a pipeline to point at
   regardless of growth-init ordering. On a foundation-only install (growth absent)
   the seed + backfill are skipped; pipeline_stage_id stays NULL (safe per ADR-0023).
3. Backfills opportunity.pipeline_stage_id from the legacy `stage` text by joining
   the seeded default pipeline's stage whose name == opportunity.stage. Only fires
   for rows where pipeline_stage_id IS NULL and the default pipeline exists.

Per ADR-0023 the pipeline_stage_id column is OPAQUE TEXT — NOT a SQL-level inline
FK. SQLite resolves a column's REFERENCES target at INSERT time even for a NULL
value, so an inline REFERENCES crm_pipeline_stage(id) would break every add-opportunity
on a foundation-only install where crm_pipeline_stage does not exist. (Same probe as
migration 023.) FK integrity is enforced application-side: growth (the sole writer)
validates the target stage exists before populating the column.

Idempotent: detects whether the CHECK is already gone and skips the rebuild; the seed +
backfill are guarded (INSERT only when missing; backfill only NULL rows). Dialect-aware.

Usage:
    python3 024_displace_opportunity_stage_check.py [--db-path PATH]
"""
import argparse
import os
import sqlite3
import uuid

# M102: rebuilds a table and copies every row verbatim; backfills a column it
# adds in the same run; seeds the shipped pipeline catalog rows (see the
# exemptions below) — nothing a row held before this run is different
# afterwards.
MIGRATION_DATA_CLASS = "none"
# Statements the classifier flags that this file's own shape explains.
# Keyed per statement, never per file, so anything it does not name
# still fails the gate.
MIGRATION_DATA_EXEMPTIONS = {
    ("INSERT", "crm_pipeline"):
        "the shipped default 'Standard Sales' pipeline — a fixed catalog row, "
        "identical on every install, in a table without the _registry suffix "
        "the classifier keys on",
    ("INSERT", "crm_pipeline_stage"):
        "its seven shipped stages, same reason",
}

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# Default "Standard Sales" pipeline: the existing 7 stages, in order. The terminal
# flags + default probability mirror the legacy hardcoded semantics. Kept in sync
# with init_db.create_crmadv_tables() DEFAULT_PIPELINE_SEED at authoring time.
DEFAULT_PIPELINE_NAME = "Standard Sales"
# (stage_order, name, is_terminal_won, is_terminal_lost, default_probability)
DEFAULT_PIPELINE_STAGES = [
    (1, "new", 0, 0, "0"),
    (2, "contacted", 0, 0, "10"),
    (3, "qualified", 0, 0, "25"),
    (4, "proposal_sent", 0, 0, "50"),
    (5, "negotiation", 0, 0, "75"),
    (6, "won", 1, 0, "100"),
    (7, "lost", 0, 1, "0"),
]

# The `opportunity` table WITHOUT the stage CHECK + WITH pipeline_stage_id.
# Must match init_schema.py exactly (post-F1 columns + the new F3 column).
_OPPORTUNITY_DDL_NO_CHECK = """
CREATE TABLE opportunity (
    id              TEXT PRIMARY KEY,
    naming_series   TEXT,
    opportunity_name TEXT NOT NULL,
    lead_id         TEXT REFERENCES lead(id) ON DELETE RESTRICT,
    customer_id     TEXT REFERENCES customer(id) ON DELETE RESTRICT,
    opportunity_type TEXT NOT NULL DEFAULT 'sales'
                    CHECK(opportunity_type IN ('sales','support','maintenance')),
    source          TEXT,
    expected_closing_date TEXT,
    probability     TEXT NOT NULL DEFAULT '0',
    expected_revenue TEXT NOT NULL DEFAULT '0',
    weighted_revenue TEXT NOT NULL DEFAULT '0',
    stage           TEXT NOT NULL DEFAULT 'new',
    lost_reason     TEXT,
    assigned_to     TEXT,
    next_follow_up_date TEXT,
    quotation_id    TEXT,
    crm_contact_id  TEXT,
    crm_company_id  TEXT,
    pipeline_stage_id TEXT,
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

# Columns to copy from the old table (NOT pipeline_stage_id — it's new, stays NULL
# until backfill). Order-independent (explicit column list on both sides).
_OPPORTUNITY_COLS = (
    "id, naming_series, opportunity_name, lead_id, customer_id, opportunity_type, "
    "source, expected_closing_date, probability, expected_revenue, weighted_revenue, "
    "stage, lost_reason, assigned_to, next_follow_up_date, quotation_id, "
    "crm_contact_id, crm_company_id, company_id, created_at, updated_at"
)

_OPPORTUNITY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_opportunity_stage ON opportunity(stage)",
    "CREATE INDEX IF NOT EXISTS idx_opportunity_company ON opportunity(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_opportunity_customer ON opportunity(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_opportunity_pipeline_stage ON opportunity(pipeline_stage_id)",
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _sqlite_stage_check_present(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='opportunity'"
    ).fetchone()
    return bool(row) and "stage IN" in (row[0] or "")


def _seed_and_backfill_sqlite(conn):
    """Seed the default pipeline + backfill pipeline_stage_id. Skips if growth
    tables are absent (foundation-only install). Idempotent."""
    if not (_sqlite_has_table(conn, "crm_pipeline")
            and _sqlite_has_table(conn, "crm_pipeline_stage")):
        return  # foundation-only install: nothing to seed, pipeline_stage_id stays NULL

    # Seed the default pipeline once per business company that has opportunities,
    # or globally if none — but pipelines are not company-scoped (crm_pipeline has no
    # company_id; they are catalog rows shared across the install). Seed a single
    # global default if absent.
    pipeline_id = _ensure_default_pipeline_sqlite(conn)

    # Backfill: map each NULL pipeline_stage_id opportunity to the default pipeline's
    # stage whose name matches the legacy stage text.
    conn.execute(
        """UPDATE opportunity
           SET pipeline_stage_id = (
               SELECT s.id FROM crm_pipeline_stage s
               WHERE s.crm_pipeline_id = ? AND s.name = opportunity.stage
           )
           WHERE pipeline_stage_id IS NULL
             AND EXISTS (
               SELECT 1 FROM crm_pipeline_stage s
               WHERE s.crm_pipeline_id = ? AND s.name = opportunity.stage
             )""",
        (pipeline_id, pipeline_id),
    )


def _ensure_default_pipeline_sqlite(conn):
    """Return the id of the default 'Standard Sales' pipeline, creating it + its 7
    stages if absent. Idempotent."""
    row = conn.execute(
        "SELECT id FROM crm_pipeline WHERE is_default = 1 ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    # Fall back to one matching the canonical name (in case is_default got cleared).
    row = conn.execute(
        "SELECT id FROM crm_pipeline WHERE name = ? ORDER BY created_at LIMIT 1",
        (DEFAULT_PIPELINE_NAME,),
    ).fetchone()
    if row:
        return row[0]

    pipeline_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO crm_pipeline (id, name, description, is_default, is_active) "
        "VALUES (?, ?, ?, 1, 1)",
        (pipeline_id, DEFAULT_PIPELINE_NAME,
         "Default sales pipeline (seeded for backfill of legacy opportunity.stage)"),
    )
    for order_no, name, won, lost, prob in DEFAULT_PIPELINE_STAGES:
        conn.execute(
            "INSERT INTO crm_pipeline_stage "
            "(id, crm_pipeline_id, stage_order, name, is_terminal_won, "
            " is_terminal_lost, default_probability, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (str(uuid.uuid4()), pipeline_id, order_no, name, won, lost, prob),
        )
    return pipeline_id


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    if not _sqlite_stage_check_present(conn):
        # CHECK already gone (re-run or fresh DB from new init_schema). Ensure the
        # pipeline_stage_id column exists (defensive) then seed + backfill.
        if _sqlite_has_table(conn, "opportunity") and not any(
                r[1] == "pipeline_stage_id" for r in conn.execute("PRAGMA table_info(opportunity)")):
            conn.execute("ALTER TABLE opportunity ADD COLUMN pipeline_stage_id TEXT")
        _seed_and_backfill_sqlite(conn)
        conn.commit()
        print("  opportunity.stage CHECK already absent; seed + backfill ensured (idempotent).")
        conn.close()
        return

    conn.execute("PRAGMA foreign_keys=OFF")  # required during table rebuild
    # legacy_alter_table=ON keeps inbound FK refs (crm_activity.opportunity_id)
    # pointing at "opportunity" rather than redirecting to the temp *_f3_old table.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE opportunity RENAME TO opportunity_f3_old")
        conn.execute(_OPPORTUNITY_DDL_NO_CHECK)
        conn.execute(
            f"INSERT INTO opportunity ({_OPPORTUNITY_COLS}) "
            f"SELECT {_OPPORTUNITY_COLS} FROM opportunity_f3_old"
        )
        conn.execute("DROP TABLE opportunity_f3_old")
        for idx in _OPPORTUNITY_INDEXES:
            conn.execute(idx)
        _seed_and_backfill_sqlite(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    # FK integrity sanity after rebuild.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError(f"Migration 024 left {len(violations)} FK violations: {violations[:5]}")
    dangling = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_f3_old%'"
    ).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError(f"Migration 024 left dangling FK refs to *_f3_old: {[r[0] for r in dangling]}")
    n = conn.execute("SELECT COUNT(*) FROM opportunity").fetchone()[0]
    print(f"  opportunity rebuilt without stage CHECK; {n} rows preserved, FK check clean.")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE opportunity DROP CONSTRAINT IF EXISTS opportunity_stage_check"
            )
            cur.execute(
                "ALTER TABLE opportunity ADD COLUMN IF NOT EXISTS pipeline_stage_id TEXT"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_opportunity_pipeline_stage "
                "ON opportunity(pipeline_stage_id)"
            )
            # Seed + backfill only when the growth tables exist.
            cur.execute("SELECT to_regclass('public.crm_pipeline_stage')")
            has_growth = cur.fetchone()[0] is not None
            if has_growth:
                cur.execute(
                    "SELECT id FROM crm_pipeline WHERE is_default = 1 "
                    "ORDER BY created_at LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT id FROM crm_pipeline WHERE name = %s "
                        "ORDER BY created_at LIMIT 1", (DEFAULT_PIPELINE_NAME,))
                    row = cur.fetchone()
                if row is None:
                    pipeline_id = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO crm_pipeline (id, name, description, is_default, is_active) "
                        "VALUES (%s, %s, %s, 1, 1)",
                        (pipeline_id, DEFAULT_PIPELINE_NAME,
                         "Default sales pipeline (seeded for backfill of legacy opportunity.stage)"))
                    for order_no, name, won, lost, prob in DEFAULT_PIPELINE_STAGES:
                        cur.execute(
                            "INSERT INTO crm_pipeline_stage "
                            "(id, crm_pipeline_id, stage_order, name, is_terminal_won, "
                            " is_terminal_lost, default_probability, is_active) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, 1)",
                            (str(uuid.uuid4()), pipeline_id, order_no, name, won, lost, prob))
                else:
                    pipeline_id = row[0]
                cur.execute(
                    """UPDATE opportunity o
                       SET pipeline_stage_id = s.id
                       FROM crm_pipeline_stage s
                       WHERE s.crm_pipeline_id = %s
                         AND s.name = o.stage
                         AND o.pipeline_stage_id IS NULL""",
                    (pipeline_id,))
        conn.commit()
        print("  Postgres: opportunity stage CHECK dropped; pipeline_stage_id added; seed/backfill applied.")
    finally:
        conn.close()


def run_migration(db_path=None):
    dialect = _get_dialect()
    if dialect == "postgresql":
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
    parser = argparse.ArgumentParser(description="Migration 024: Displace opportunity.stage CHECK + pipeline_stage_id")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 024 complete.")
