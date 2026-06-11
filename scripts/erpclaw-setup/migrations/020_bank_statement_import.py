"""Migration 020: M2 bank statement import + matching — bank_statement,
bank_match_rule, bank_statement_line.

Activates the file-import + matching path (OFX / CAMT.053 / MT940 / BAI2). The
three tables are owned + written exclusively by erpclaw-integrations' bank.py;
they are defined in the foundation schema for fresh installs (init_schema.py
BANK_TABLES) and added to existing DBs here, mirroring the M6 dimension_registry
(017) and S3 cwip_cost_accumulation (021) precedent.

  - bank_statement: one row per imported file/feed pull.
  - bank_match_rule: user-configurable auto-match rules (created BEFORE the line
    table so the line's match_rule_id FK target exists at CREATE time — Postgres
    enforces FK targets at table creation).
  - bank_statement_line: one row per parsed transaction. The
    (source, bank_account_id, external_id) UNIQUE is the idempotency guarantee:
    re-importing the same file skips duplicate lines instead of double-booking.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS. Idempotent, dialect-aware, no
rebuild / no FK-rewrite trap. Columns match init_schema exactly. The Plaid stub
(connv2_financial_connector) is untouched — 'plaid' is only a reserved source
enum value here.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

# Order matters: bank_match_rule precedes bank_statement_line so the line's
# match_rule_id FK target exists when Postgres creates it.
_DDL = [
    """CREATE TABLE IF NOT EXISTS bank_statement (
        id                  TEXT PRIMARY KEY,
        bank_account_id     TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
        company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        source              TEXT NOT NULL
                            CHECK(source IN ('ofx','camt053','mt940','bai2','plaid','manual_csv')),
        file_path           TEXT,
        period_start        TEXT,
        period_end          TEXT,
        opening_balance     TEXT,
        closing_balance     TEXT,
        currency            TEXT NOT NULL DEFAULT 'USD',
        import_status       TEXT NOT NULL DEFAULT 'imported'
                            CHECK(import_status IN ('pending','imported','partially_matched','fully_matched','archived')),
        line_count          INTEGER NOT NULL DEFAULT 0,
        imported_at         TEXT NOT NULL,
        imported_by_user_id TEXT,
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bank_statement_account ON bank_statement(bank_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_bank_statement_company ON bank_statement(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_bank_statement_status ON bank_statement(import_status)",
    """CREATE TABLE IF NOT EXISTS bank_match_rule (
        id              TEXT PRIMARY KEY,
        company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        name            TEXT NOT NULL,
        match_field     TEXT NOT NULL
                        CHECK(match_field IN ('description','counterparty_name','reference','amount')),
        match_operator  TEXT NOT NULL
                        CHECK(match_operator IN ('equals','contains','regex','amount_range')),
        match_value     TEXT NOT NULL,
        target_action   TEXT NOT NULL
                        CHECK(target_action IN ('map_to_account','map_to_vendor','map_to_customer','ignore')),
        target_id       TEXT,
        priority        INTEGER NOT NULL DEFAULT 100,
        is_active       INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bank_match_rule_company ON bank_match_rule(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_bank_match_rule_priority ON bank_match_rule(priority)",
    """CREATE TABLE IF NOT EXISTS bank_statement_line (
        id                       TEXT PRIMARY KEY,
        bank_statement_id        TEXT NOT NULL REFERENCES bank_statement(id) ON DELETE CASCADE,
        bank_account_id          TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
        source                   TEXT NOT NULL,
        txn_date                 TEXT NOT NULL,
        value_date               TEXT,
        amount                   TEXT NOT NULL,
        currency                 TEXT NOT NULL DEFAULT 'USD',
        description              TEXT,
        counterparty_name        TEXT,
        counterparty_account     TEXT,
        reference                TEXT,
        external_id              TEXT NOT NULL,
        match_status             TEXT NOT NULL DEFAULT 'unmatched'
                                 CHECK(match_status IN ('unmatched','auto_matched','manual_matched','ignored')),
        matched_gl_entry_id      TEXT,
        matched_payment_entry_id TEXT,
        match_confidence         TEXT,
        match_rule_id            TEXT REFERENCES bank_match_rule(id) ON DELETE SET NULL,
        created_at               TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source, bank_account_id, external_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bank_line_statement ON bank_statement_line(bank_statement_id)",
    "CREATE INDEX IF NOT EXISTS idx_bank_line_account ON bank_statement_line(bank_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_bank_line_match_status ON bank_statement_line(match_status)",
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_statement'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  bank_statement / bank_match_rule / bank_statement_line: "
          f"{'already present' if existed else 'created'} (+ indexes).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: bank_statement / bank_match_rule / bank_statement_line ensured (+ indexes).")
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
    parser = argparse.ArgumentParser(description="Migration 020: M2 bank statement import")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 020 complete.")
