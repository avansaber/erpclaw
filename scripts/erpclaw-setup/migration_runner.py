"""Foundation migration runner (Wave 0 — migration framework, audit P0).

Discovers `migrations/NNN_*.py`, runs the pending ones in numeric order, and
records each in the `erpclaw_schema_migration` ledger — the same table the
OS-engine's `schema_migrator` uses (audit F2: reuse, don't invent a new ledger).

Properties:
  - Idempotent twice over: each migration is individually idempotent (IF NOT
    EXISTS / column-presence guards) AND skipped if already in the ledger as
    'applied'.
  - Dialect-aware: each migration already branches on get_dialect() for its DDL;
    the ledger here works on SQLite and Postgres.
  - Bootstrap-safe: on a DB where 001-NNN were applied by hand (not ledgered),
    the first run re-runs them (no-ops) and backfills the ledger.

Entry points: `run_pending(db_path, dry_run=False)` and `discover()`.
Exposed to users as the `migrate` action in db_query.py.
"""
import contextlib
import gc
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_NUM_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.py$")

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS erpclaw_schema_migration (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    migration_type  TEXT NOT NULL,
    ddl_statements  TEXT NOT NULL,
    status          TEXT NOT NULL,
    previous_schema TEXT,
    planned_at      TEXT,
    applied_at      TEXT,
    rolled_back_at  TEXT,
    applied_by      TEXT
)
"""


def _dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover(migrations_dir=MIGRATIONS_DIR):
    """Return [(migration_id, path), ...] sorted by the leading NNN number."""
    out = []
    if not os.path.isdir(migrations_dir):
        return out
    for fn in sorted(os.listdir(migrations_dir)):
        if _NUM_RE.match(fn):
            out.append((fn[:-3], os.path.join(migrations_dir, fn)))
    return out


def _ledger_id(module_name, stem):
    """Ledger primary key for a migration. Foundation keeps bare stems
    ('003_displace_account_check') for backward-compat with already-populated
    ledgers; other modules are namespaced ('erpclaw-billing:001_add_x') so two
    modules can both have a 001 without colliding."""
    return stem if module_name == "erpclaw-setup" else f"{module_name}:{stem}"


def _resolve_target(db_path):
    """On PostgreSQL `ERPCLAW_DB_URL` wins when set, otherwise only a `postgresql://` or `postgres://` URL from the `db_path` argument or `ERPCLAW_DB_PATH` is used and a file path or other non-URL location (including a libpq keyword string such as `host=h dbname=b`, which is not a URL) is ignored; on SQLite `db_path` is used exactly as passed."""
    if _dialect() == "postgresql":
        env_url = os.environ.get("ERPCLAW_DB_URL")
        if env_url:
            from erpclaw_lib.db import require_pg_url
            return require_pg_url(env_url, source="ERPCLAW_DB_URL")
        if isinstance(db_path, str) and (db_path.startswith("postgresql://")
                                         or db_path.startswith("postgres://")):
            return db_path
        env_path = os.environ.get("ERPCLAW_DB_PATH")
        if isinstance(env_path, str) and (env_path.startswith("postgresql://")
                                          or env_path.startswith("postgres://")):
            return env_path
        raise RuntimeError(
            "ERPCLAW_DB_DIALECT=postgresql but the migration runner has no target: "
            "set ERPCLAW_DB_URL, pass a postgresql:// URL, or set ERPCLAW_DB_PATH to a postgresql:// URL.")
    return db_path


def _connect(db_path):
    """Return (connection, placeholder) for the active dialect."""
    if _dialect() == "postgresql":
        import psycopg2
        return psycopg2.connect(_resolve_target(db_path)), "%s"
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    return conn, "?"


def _applied_ids(db_path):
    """Ids ledgered as 'applied'. Reads only: on a database the runner has
    never written to there is no ledger yet, and a read-only question (dry
    run) or a failed run must not be the thing that first writes one. The
    existence question goes to the seam, which answers from the backend
    catalog over its own connection, so a missing ledger reads as empty on
    any backend with no raw catalog reads here. The ledger comes into being
    with the first recorded row in `_record` (applied or failed)."""
    from erpclaw_lib import seam
    if not seam.table_exists("erpclaw_schema_migration", _resolve_target(db_path)):
        return set()
    conn, _ = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM erpclaw_schema_migration WHERE status = 'applied'")
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _record(db_path, ledger_id, status, module_name):
    """Upsert a ledger row for a migration (delete+insert keeps it cross-DB simple)."""
    conn, ph = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(_LEDGER_DDL)
        now = _now()
        cur.execute(f"DELETE FROM erpclaw_schema_migration WHERE id = {ph}", (ledger_id,))
        cur.execute(
            f"INSERT INTO erpclaw_schema_migration "
            f"(id, module_name, migration_type, ddl_statements, status, "
            f" planned_at, applied_at, applied_by) "
            f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
            (ledger_id, module_name, "alter", f"{module_name} migration {ledger_id}",
             status, now, now if status == "applied" else None, "migration_runner"),
        )
        conn.commit()
    finally:
        conn.close()


def run_pending(db_path, dry_run=False, migrations_dir=MIGRATIONS_DIR,
                module_name="erpclaw-setup"):
    """Run all not-yet-applied migrations in `migrations_dir` in order, recording
    each under `module_name` in the shared ledger. Works for the foundation
    (default) and for any module that ships its own migrations/ dir (P1)."""
    if _dialect() == "postgresql":
        _resolve_target(db_path)
    applied = _applied_ids(db_path)
    discovered = discover(migrations_dir)
    pending = [(stem, p) for stem, p in discovered
               if _ledger_id(module_name, stem) not in applied]

    if dry_run:
        return {"dry_run": True, "module": module_name,
                "already_applied": sorted(_ledger_id(module_name, s) for s, _ in discovered
                                          if _ledger_id(module_name, s) in applied),
                "pending": [s for s, _ in pending]}

    ran = []
    for stem, path in pending:
        lid = _ledger_id(module_name, stem)
        spec = importlib.util.spec_from_file_location(f"_erpclaw_mig_{module_name}_{stem}", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            # migrations print human progress; keep this action's stdout pure JSON
            # by routing their output to stderr (still visible in server logs).
            with contextlib.redirect_stdout(sys.stderr):
                spec.loader.exec_module(mod)
                mod.run_migration(_resolve_target(db_path))
        except Exception as e:  # noqa: BLE001 — surface, don't swallow
            error = str(e)
        else:
            try:
                _record(db_path, lid, "applied", module_name)
            except Exception as record_e:  # noqa: BLE001 — report, don't mask
                record_error = str(record_e)
                return {"ok": False, "module": module_name, "applied": ran, "failed": stem,
                        "stage": "record",
                        "error": record_error,
                        "detail": "The migration ran and its changes are committed, but its "
                                  "ledger row could not be written, so it will run again on "
                                  "the next run and must be idempotent. Fix the ledger write "
                                  "and re-run.",
                        "ledger_error": record_error}
            ran.append(stem)
            continue
        gc.collect()
        try:
            _record(db_path, lid, "failed", module_name)
        except Exception as ledger_e:  # noqa: BLE001 — report, don't mask
            ledger_error = str(ledger_e)
            return {"ok": False, "module": module_name, "applied": ran, "failed": stem,
                    "stage": "run",
                    "error": error,
                    "detail": "A failing migration's own changes are rolled back only if it "
                              "runs in a single transaction; anything it committed before "
                              "failing remains. It is recorded in the ledger as failed and is "
                              "retried on the next run. Fix the failing migration and re-run.",
                    "ledger_error": ledger_error}
        return {"ok": False, "module": module_name, "applied": ran, "failed": stem,
                "stage": "run",
                "error": error,
                "detail": "A failing migration's own changes are rolled back only if it "
                          "runs in a single transaction; anything it committed before "
                          "failing remains. It is recorded in the ledger as failed and is "
                          "retried on the next run. Fix the failing migration and re-run."}

    return {"ok": True, "module": module_name, "applied": ran,
            "already_applied": [s for s, _ in discovered if s not in [r for r in ran]
                                and _ledger_id(module_name, s) in applied],
            "total_known": len(discovered)}
