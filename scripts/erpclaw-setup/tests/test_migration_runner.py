"""Tests for the foundation migration runner (Wave 0 — audit P0).

Verifies discovery, dry-run, apply, ledger recording, idempotent re-run, and the
bootstrap-backfill case (DB already at final schema but no ledger).
"""
import importlib.util
import os
import sqlite3
import pytest

_SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_runner():
    p = os.path.join(_SETUP_DIR, "migration_runner.py")
    spec = importlib.util.spec_from_file_location("migration_runner", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


runner = _load_runner()


def test_discover_finds_numbered_migrations():
    ids = [mid for mid, _ in runner.discover()]
    assert ids == sorted(ids), "must be in numeric order"
    assert "001_registry_tables" in ids
    assert "009_custom_field_value_table" in ids
    # every id is NNN_ prefixed
    assert all(mid[:3].isdigit() and mid[3] == "_" for mid in ids)


def test_dry_run_lists_all_pending_on_fresh_db(db_path):
    res = runner.run_pending(db_path, dry_run=True)
    assert res["dry_run"] is True
    assert res["already_applied"] == []
    assert len(res["pending"]) == len(runner.discover())


def test_apply_then_ledger_records_all(db_path):
    res = runner.run_pending(db_path)
    assert res["ok"] is True
    assert len(res["applied"]) == len(runner.discover())
    # ledger table now exists and marks each applied
    conn = sqlite3.connect(db_path)
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT id, status FROM erpclaw_schema_migration")}
    conn.close()
    for mid, _ in runner.discover():
        assert rows.get(mid) == "applied"


def test_rerun_is_noop(db_path):
    runner.run_pending(db_path)
    res = runner.run_pending(db_path)
    assert res["ok"] is True
    assert res["applied"] == []
    assert len(res["already_applied"]) == len(runner.discover())


def test_dry_run_after_apply_shows_none_pending(db_path):
    runner.run_pending(db_path)
    res = runner.run_pending(db_path, dry_run=True)
    assert res["pending"] == []


def test_module_migrations_namespaced(db_path, tmp_path):
    """P1: a module can ship its own migrations/ dir; the runner applies them and
    records them namespaced (module:stem) so two modules' 001s don't collide."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_widget_table.py").write_text(
        "import sqlite3\n"
        "def run_migration(db_path=None):\n"
        "    c = sqlite3.connect(db_path)\n"
        "    c.execute('CREATE TABLE IF NOT EXISTS demo_widget (id TEXT PRIMARY KEY)')\n"
        "    c.commit(); c.close()\n"
    )
    res = runner.run_pending(str(db_path), migrations_dir=str(mdir), module_name="demo-mod")
    assert res["ok"] is True and res["applied"] == ["001_widget_table"]
    import sqlite3
    conn = sqlite3.connect(db_path)
    # table created, and ledger row is namespaced
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='demo_widget'").fetchone() is not None
    row = conn.execute(
        "SELECT module_name, status FROM erpclaw_schema_migration WHERE id='demo-mod:001_widget_table'"
    ).fetchone()
    conn.close()
    assert row == ("demo-mod", "applied")
    # re-run is a no-op
    res2 = runner.run_pending(str(db_path), migrations_dir=str(mdir), module_name="demo-mod")
    assert res2["applied"] == []


def test_bootstrap_backfill(db_path):
    """A DB already at the final schema (fresh init) but with NO ledger: the
    runner re-runs the idempotent migrations as no-ops and backfills the ledger
    rather than erroring."""
    # no ledger yet
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='erpclaw_schema_migration'"
    ).fetchone() is None
    conn.close()
    res = runner.run_pending(db_path)
    assert res["ok"] is True
    # core tables still intact after the no-op re-runs
    conn = sqlite3.connect(db_path)
    for t in ("account", "gl_entry", "custom_field_value", "asset_status_registry"):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone() is not None, f"{t} missing after backfill run"
    conn.close()
