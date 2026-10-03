"""Foundation migration runner, behaviour proofs (m323b).

Proves the two runner modes do what they claim, through fixture migrations
run against a real database:

  * report-only writes nothing (full table digest before and after);
  * an applied row-changing migration files its ledger row under its exact
    stem and leaves one audit row per changed document, each carrying only
    the changed column;
  * a migration that raises midway is recorded in the ledger as failed and the
    runner reports failure, not success;
  * a failed migration stays pending: the next run retries it and still holds a
    single failed row until it succeeds;
  * applying twice neither duplicates the ledger row nor rewrites rows.

Catalog questions go through the seam, connections through get_connection,
queries through the PyPika helpers; money stays Decimal over TEXT with
exact string assertions throughout.
"""
import importlib.util
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import init_all_tables  # noqa: E402  (binds erpclaw_lib to this tree)
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.audit import migration_action  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.db import _resolve_pg_url  # noqa: E402
from erpclaw_lib.query import Q, Table, P, insert_row  # noqa: E402


def _load_runner():
    p = os.path.join(_SETUP_DIR, "migration_runner.py")
    spec = importlib.util.spec_from_file_location("migration_runner_behaviour", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()

DEMO = "mig_demo_doc"
LEDGER = "erpclaw_schema_migration"
TRAIL = "audit_log"
CENT = Decimal("0.01")

EXPECTED_DETAIL = ("A failing migration's own changes are rolled back only if it runs "
                   "in a single transaction; anything it committed before failing remains. "
                   "It is recorded in the ledger as failed and is retried on the next run. "
                   "Fix the failing migration and re-run.")

# A row-changing migration in the house shape: the ledger stem is derived
# from the filename (as 035/037 do), money is Decimal over TEXT, each
# changed document gets one audit row naming only the changed column, the
# write and its trail share one transaction, and a second run finds nothing.
FIXTURE_ROWS = '''import os
from decimal import Decimal, ROUND_HALF_UP

from erpclaw_lib import seam
from erpclaw_lib.audit import audit_migration
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"

TARGET = "mig_demo_doc"
CENT = Decimal("0.01")
_SCAN = "SELECT id, amount FROM mig_demo_doc"
_FIX = "UPDATE mig_demo_doc SET amount = ? WHERE id = ?"


def _want(text):
    return str(Decimal(str(text)).quantize(CENT, rounding=ROUND_HALF_UP))


def run_migration(db_path=None, report_only=False):
    if not seam.table_exists(TARGET, db_path):
        return {"migration": MIGRATION_ID, "report_only": report_only,
                "healed": [], "checked": 0}
    conn = get_connection(db_path)
    conn.execute("BEGIN")
    healed = []
    checked = 0
    try:
        found = conn.execute(_SCAN).fetchall()
        checked = len(found)
        for row in found:
            doc_id = row[0]
            before = str(row[1])
            after = _want(before)
            if after == before:
                continue
            if not report_only:
                conn.execute(_FIX, (after, doc_id))
                audit_migration(conn, MIGRATION_ID, TARGET, str(doc_id),
                                old_values={"amount": before},
                                new_values={"amount": after},
                                description="heal money text to two decimals")
            healed.append({"id": str(doc_id), "before": before, "after": after})
        if not report_only:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"migration": MIGRATION_ID, "report_only": report_only,
            "healed": healed, "checked": checked}
'''

# A migration that changes the schema, changes a row, writes its trail, and
# then raises before commit. One transaction holds all three, so the raise
# must roll every one of them back.
FIXTURE_BOOM = '''import os
from decimal import Decimal

from erpclaw_lib.audit import audit_migration
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"

TARGET = "mig_demo_doc"


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE mig_demo_doc ADD COLUMN boom_note TEXT")
        found = conn.execute("SELECT id, amount FROM mig_demo_doc").fetchall()
        doc_id = found[0][0]
        before = str(found[0][1])
        after = str(Decimal("9.99"))
        conn.execute("UPDATE mig_demo_doc SET amount = ? WHERE id = ?",
                     (after, doc_id))
        audit_migration(conn, MIGRATION_ID, TARGET, str(doc_id),
                        old_values={"amount": before},
                        new_values={"amount": after},
                        description="partial write before the boom")
        raise RuntimeError("mid-migration boom (behaviour fixture)")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
'''


FIXTURE_FLAGGED = '''import os
from decimal import Decimal, ROUND_HALF_UP

from erpclaw_lib.audit import audit_migration
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"

TARGET = "mig_demo_doc"
CENT = Decimal("0.01")
_SCAN = "SELECT id, amount FROM mig_demo_doc"
_FIX = "UPDATE mig_demo_doc SET amount = ? WHERE id = ?"


def _want(text):
    return str(Decimal(str(text)).quantize(CENT, rounding=ROUND_HALF_UP))


def run_migration(db_path=None, report_only=False):
    flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allow")
    if not os.path.exists(flag):
        raise RuntimeError("mid-migration boom until flag appears (behaviour fixture)")
    conn = get_connection(db_path)
    conn.execute("BEGIN")
    healed = []
    try:
        found = conn.execute(_SCAN).fetchall()
        for row in found:
            doc_id = row[0]
            before = str(row[1])
            after = _want(before)
            if after == before:
                continue
            conn.execute(_FIX, (after, doc_id))
            audit_migration(conn, MIGRATION_ID, TARGET, str(doc_id),
                            old_values={"amount": before},
                            new_values={"amount": after},
                            description="heal money text to two decimals")
            healed.append(doc_id)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"migration": MIGRATION_ID, "healed": healed}
'''

FIXTURE_OPEN_CONN = '''import os
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    conn.execute("UPDATE mig_demo_doc SET amount = ? WHERE id = ?",
                 ("9.99", "doc-1"))
    raise RuntimeError("mid-migration boom with connection open (behaviour fixture)")
'''

FIXTURE_RAISE = '''import os

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"


def run_migration(db_path=None, report_only=False):
    raise RuntimeError("second migration boom (behaviour fixture)")
'''

FIXTURE_PARTIAL = '''import os
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "schema"


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    try:
        if "extra_note" not in seam.column_names("mig_demo_doc", db_path):
            conn.execute("ALTER TABLE mig_demo_doc ADD COLUMN extra_note TEXT")
            conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    raise RuntimeError("boom after partial commit (behaviour fixture)")
'''


def _norm(text):
    return str(Decimal(str(text)).quantize(CENT, rounding=ROUND_HALF_UP))


def _provision_demo(db_path):
    meta = seam.MetaData()
    seam.Table(DEMO, meta,
               seam.Column("id", seam.Text, primary_key=True),
               seam.Column("amount", seam.Text))
    seam.provision(meta, db_path)


def _seed_demo(db_path, pairs):
    conn = get_connection(db_path)
    try:
        for doc_id, amount in pairs:
            sql, _cols = insert_row(DEMO, {"id": P(), "amount": P()})
            conn.execute(sql, [doc_id, amount])
        conn.commit()
    finally:
        conn.close()


def _row_dict(row):
    return {key: row[key] for key in row.keys()}


def _snapshot(db_path):
    tables = list(seam.table_names(db_path))
    snap = {"tables": tables, "columns": {}, "rows": {}}
    for name in tables:
        snap["columns"][name] = list(seam.column_names(name, db_path))
        tab = Table(name)
        sel = Q.from_(tab).select(tab.star).get_sql()
        conn = get_connection(db_path)
        try:
            fetched = conn.execute(sel).fetchall()
        finally:
            conn.close()
        norm = []
        for row in fetched:
            items = sorted((key, "NULL" if row[key] is None else str(row[key]))
                           for key in row.keys())
            norm.append(tuple(items))
        snap["rows"][name] = sorted(norm)
    return snap


def _ledger_rows(db_path):
    if not seam.table_exists(LEDGER, db_path):
        return []
    tab = Table(LEDGER)
    sel = Q.from_(tab).select(tab.star).get_sql()
    conn = get_connection(db_path)
    try:
        return [_row_dict(r) for r in conn.execute(sel).fetchall()]
    finally:
        conn.close()


def _audit_rows(db_path, action):
    if not seam.table_exists(TRAIL, db_path):
        return []
    tab = Table(TRAIL)
    sel = Q.from_(tab).select(tab.star).where(tab.action == P()).get_sql()
    conn = get_connection(db_path)
    try:
        rows = [_row_dict(r) for r in conn.execute(sel, [action]).fetchall()]
    finally:
        conn.close()
    for row in rows:
        if row.get("old_values"):
            row["old_values"] = json.loads(row["old_values"])
        if row.get("new_values"):
            row["new_values"] = json.loads(row["new_values"])
    return rows


def _amount_of(db_path, doc_id):
    tab = Table(DEMO)
    sel = Q.from_(tab).select(tab.amount).where(tab.id == P()).get_sql()
    conn = get_connection(db_path)
    try:
        row = conn.execute(sel, [doc_id]).fetchone()
    finally:
        conn.close()
    return str(row[0])


def _write_fixture(mdir, stem, body):
    path = os.path.join(str(mdir), stem + ".py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def test_report_only_writes_nothing(db_path, tmp_path):
    """Dry run against pending migrations changes neither schema nor data."""
    stem = "001_demo_amount_heal"
    _write_fixture(tmp_path, stem, FIXTURE_ROWS)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "10.0"), ("doc-2", "7.5"), ("doc-3", "3.00")])
    before = _snapshot(db_path)

    res = runner.run_pending(db_path, dry_run=True, migrations_dir=str(tmp_path))

    assert res["dry_run"] is True
    assert res["pending"] == [stem]
    assert _snapshot(db_path) == before
    assert _ledger_rows(db_path) == []
    assert _audit_rows(db_path, migration_action(stem)) == []


def test_applied_mode_records_ledger_and_trail(db_path, tmp_path):
    """An applied row-changing migration is filed under its stem with one
    audit row per changed document carrying only the changed column."""
    stem = "001_demo_amount_heal"
    _write_fixture(tmp_path, stem, FIXTURE_ROWS)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "10.0"), ("doc-2", "7.5"), ("doc-3", "3.00")])

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is True
    assert res["applied"] == [stem]
    ledger = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(ledger) == 1
    assert ledger[0]["status"] == "applied"
    assert ledger[0]["module_name"] == "erpclaw-setup"
    trail = _audit_rows(db_path, migration_action(stem))
    assert len(trail) == 2
    by_doc = {r["entity_id"]: r for r in trail}
    assert sorted(by_doc) == ["doc-1", "doc-2"]
    for row in trail:
        assert row["entity_type"] == DEMO
        assert row["skill"] == "erpclaw-setup"
        assert set(row["old_values"]) == {"amount"}
        assert set(row["new_values"]) == {"amount"}
    assert by_doc["doc-1"]["old_values"] == {"amount": "10.0"}
    assert by_doc["doc-1"]["new_values"] == {"amount": _norm("10.0")}
    assert by_doc["doc-2"]["old_values"] == {"amount": "7.5"}
    assert by_doc["doc-2"]["new_values"] == {"amount": _norm("7.5")}
    assert _amount_of(db_path, "doc-1") == "10.00"
    assert _amount_of(db_path, "doc-2") == "7.50"
    assert _amount_of(db_path, "doc-3") == "3.00"


def test_failure_is_recorded_as_failed(db_path, tmp_path):
    """A migration that raises midway rolls back and the runner says so."""
    stem = "001_boom_heal"
    _write_fixture(tmp_path, stem, FIXTURE_BOOM)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])
    before = _snapshot(db_path)

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert "boom" in str(res.get("error", "")).lower()
    assert res["detail"] == EXPECTED_DETAIL
    after = _snapshot(db_path)
    assert sorted(n for n in after["tables"] if n != LEDGER) == sorted(
        n for n in before["tables"] if n != LEDGER)
    for name in before["tables"]:
        if name == LEDGER:
            continue
        assert after["columns"][name] == before["columns"][name]
        assert after["rows"][name] == before["rows"][name]
    failed_rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(_ledger_rows(db_path)) == 1
    assert len(failed_rows) == 1
    assert failed_rows[0]["status"] == "failed"
    assert failed_rows[0]["applied_at"] is None
    assert failed_rows[0]["module_name"] == "erpclaw-setup"
    assert _audit_rows(db_path, migration_action(stem)) == []
    assert "boom_note" not in seam.column_names(DEMO, db_path)
    assert _amount_of(db_path, "doc-1") == "4.0"


def test_failed_migration_retries_on_second_run(db_path, tmp_path):
    """A failed migration is recorded as failed yet stays pending, so the next run retries it."""
    stem = "001_boom_heal"
    _write_fixture(tmp_path, stem, FIXTURE_BOOM)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    first = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert first.get("ok") is False
    assert first["failed"] == stem
    assert first["detail"] == EXPECTED_DETAIL
    failed_first = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(_ledger_rows(db_path)) == 1
    assert len(failed_first) == 1
    assert failed_first[0]["status"] == "failed"
    assert failed_first[0]["applied_at"] is None
    assert failed_first[0]["module_name"] == "erpclaw-setup"

    preview = runner.run_pending(db_path, dry_run=True, migrations_dir=str(tmp_path))

    assert preview["pending"] == [stem]

    second = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert second.get("ok") is False
    assert second["failed"] == stem
    assert second["detail"] == EXPECTED_DETAIL
    failed_second = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(_ledger_rows(db_path)) == 1
    assert len(failed_second) == 1
    assert failed_second[0]["status"] == "failed"
    assert failed_second[0]["applied_at"] is None
    assert failed_second[0]["module_name"] == "erpclaw-setup"
    assert _audit_rows(db_path, migration_action(stem)) == []
    assert _amount_of(db_path, "doc-1") == "4.0"


def test_applied_ids_asks_seam_not_driver_wording(db_path, tmp_path, monkeypatch):
    """A missing ledger reads as empty without consulting driver wording."""
    fresh = str(tmp_path / "fresh.sqlite")
    conn = get_connection(fresh)
    conn.close()

    reads = []
    foreign = "fremde meldung: tabelle fehlt"

    class _ForeignCursor:
        def execute(self, *args, **kwargs):
            reads.append(args)
            raise Exception(foreign)

    class _ForeignConn:
        def cursor(self):
            return _ForeignCursor()

        def close(self):
            pass

    monkeypatch.setattr(runner, "_connect", lambda p: (_ForeignConn(), "?"))

    assert runner._applied_ids(fresh) == set()
    assert reads == []
    assert seam.table_exists(LEDGER, fresh) is False

    monkeypatch.undo()
    runner._record(fresh, "001_probe", "applied", "erpclaw-setup")
    assert runner._applied_ids(fresh) == {"001_probe"}


def test_rerun_is_idempotent(db_path, tmp_path):
    """Applying twice neither duplicates the ledger row nor rewrites rows."""
    stem = "001_demo_amount_heal"
    _write_fixture(tmp_path, stem, FIXTURE_ROWS)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "10.0"), ("doc-2", "7.5")])

    first = runner.run_pending(db_path, migrations_dir=str(tmp_path))
    assert first.get("ok") is True
    assert first["applied"] == [stem]
    after_first = _snapshot(db_path)
    trail_first = _audit_rows(db_path, migration_action(stem))
    assert len(trail_first) == 2

    second = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert second.get("ok") is True
    assert second["applied"] == []
    assert [r for r in _ledger_rows(db_path) if r["id"] == stem][0]["status"] == "applied"
    assert len([r for r in _ledger_rows(db_path) if r["id"] == stem]) == 1
    assert _audit_rows(db_path, migration_action(stem)) == trail_first
    assert _snapshot(db_path) == after_first
    assert _amount_of(db_path, "doc-1") == "10.00"


def test_applied_ids_uses_env_target_on_postgresql(monkeypatch, tmp_path):
    """On PostgreSQL the ledger question must ask the same database the
    connect helper opens: ERPCLAW_DB_URL wins over a file path."""
    url = "postgresql://runner-test@db.invalid/runner_test"
    file_path = str(tmp_path / "module.sqlite")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    calls = []

    def _stub_table_exists(name, db_arg=None):
        calls.append((name, db_arg))
        return False

    def _no_connect(db_arg):
        raise AssertionError("connect helper must not run on a missing ledger")

    monkeypatch.setattr(seam, "table_exists", _stub_table_exists)
    monkeypatch.setattr(runner, "_connect", _no_connect)

    assert runner._applied_ids(file_path) == set()
    assert calls == [(LEDGER, url)]


def test_applied_ids_sqlite_ignores_env_url(monkeypatch, tmp_path):
    """On SQLite the ledger question keeps using the file path exactly as
    passed, even when ERPCLAW_DB_URL happens to be set."""
    url = "postgresql://runner-test@db.invalid/runner_test"
    file_path = str(tmp_path / "module.sqlite")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    calls = []

    def _stub_table_exists(name, db_arg=None):
        calls.append((name, db_arg))
        return False

    def _no_connect(db_arg):
        raise AssertionError("connect helper must not run on a missing ledger")

    monkeypatch.setattr(seam, "table_exists", _stub_table_exists)
    monkeypatch.setattr(runner, "_connect", _no_connect)

    assert runner._applied_ids(file_path) == set()
    assert calls == [(LEDGER, file_path)]


def test_run_pending_hands_migration_the_resolved_target_on_postgresql(
        monkeypatch, tmp_path):
    """The runner hands each migration the database it uses itself.

    Under the PostgreSQL dialect with ERPCLAW_DB_URL set, a file-path db_path
    must still reach the migration as the URL. Fails on the tree this task
    started from (the fixture would receive the file path)."""
    sidecar = tmp_path / "seen.txt"
    mig_dir = tmp_path / "migs_target_pg"
    mig_dir.mkdir()
    stem = "001_probe_target"
    _write_fixture(
        mig_dir, stem,
        "def run_migration(db_path=None):\n"
        "    with open(%r, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(repr(db_path) + chr(10))\n" % str(sidecar))
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv(
        "ERPCLAW_DB_URL", "postgresql://runner-test@db.invalid/runner_test")
    monkeypatch.setattr(seam, "table_exists",
                        lambda name, db_arg=None: False)
    monkeypatch.setattr(runner, "_record",
                        lambda *args, **kwargs: None)
    file_path = str(tmp_path / "module.sqlite")

    res = runner.run_pending(file_path, migrations_dir=str(mig_dir))

    assert res.get("ok") is True
    assert res["applied"] == [stem]
    assert sidecar.read_text(encoding="utf-8").splitlines() == [
        repr("postgresql://runner-test@db.invalid/runner_test")]


def test_run_pending_hands_migration_the_file_path_on_sqlite(
        monkeypatch, tmp_path):
    """On SQLite the migration keeps receiving the file path exactly as passed,
    even when ERPCLAW_DB_URL happens to be set. Passes on the tree this task
    started from."""
    sidecar = tmp_path / "seen.txt"
    mig_dir = tmp_path / "migs_target_lite"
    mig_dir.mkdir()
    stem = "001_probe_target"
    _write_fixture(
        mig_dir, stem,
        "def run_migration(db_path=None):\n"
        "    with open(%r, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(repr(db_path) + chr(10))\n" % str(sidecar))
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.setenv(
        "ERPCLAW_DB_URL", "postgresql://runner-test@db.invalid/runner_test")
    monkeypatch.setattr(seam, "table_exists",
                        lambda name, db_arg=None: False)
    monkeypatch.setattr(runner, "_record",
                        lambda *args, **kwargs: None)
    file_path = str(tmp_path / "module.sqlite")

    res = runner.run_pending(file_path, migrations_dir=str(mig_dir))

    assert res.get("ok") is True
    assert res["applied"] == [stem]
    assert sidecar.read_text(encoding="utf-8").splitlines() == [repr(file_path)]


def test_postgresql_live_target_end_to_end(monkeypatch, tmp_path, capsys):
    """Live PostgreSQL check: the ledger question and the runner both hit
    the database the test URL names. Skipped without ERPCLAW_PG_TEST_URL.

    The probe goes through get_connection under the PostgreSQL dialect, and
    the closing leg runs the runner in the module manager's real shape — a
    file path with ERPCLAW_DB_URL set — so the migration receives the
    resolved target. That leg fails on the tree this task started from (the
    fixture migration would receive the file path)."""
    import urllib.parse

    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("no PostgreSQL test URL")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)
    expected_db = urllib.parse.urlparse(pg_url).path.rsplit("/", 1)[-1]

    probe = get_connection(pg_url)
    try:
        version = probe.execute("SHOW server_version").fetchone()[0]
        current = probe.execute("SELECT current_database()").fetchone()[0]
    finally:
        probe.close()
    print("server_version:", version)
    print("current_database:", current)
    if current != expected_db:
        pytest.fail("probe landed on %r, expected %r; stopping" % (
            current, expected_db))

    runner_conn, _runner_ph = runner._connect(str(tmp_path / "module.sqlite"))
    try:
        runner_cur = runner_conn.cursor()
        runner_cur.execute("SELECT current_database()")
        runner_current = runner_cur.fetchone()[0]
        runner_cur.close()
    finally:
        runner_conn.close()
    print("runner current_database:", runner_current)
    if runner_current != expected_db:
        pytest.fail("runner connection landed on %r, expected %r; stopping "
                    "before any reset" % (runner_current, expected_db))

    smoke_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "..", "..", "testing", "integration", "smoke")
    if smoke_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(smoke_dir))
    import smoke_helpers

    # Start from a clean database so a leftover demo table from an
    # interrupted run cannot collide with the seed below.
    smoke_helpers.init_all_tables(pg_url)

    mig_dir = tmp_path / "pg_migs"
    mig_dir.mkdir()
    stem = "001_demo_amount_heal"
    _write_fixture(mig_dir, stem, FIXTURE_ROWS)
    _provision_demo(pg_url)
    _seed_demo(pg_url, [("doc-1", "10.0"), ("doc-2", "7.5")])
    try:
        dry = runner.run_pending(pg_url, dry_run=True, migrations_dir=str(mig_dir))
        print("dry_run:", dry)
        assert dry["pending"] == [stem]
        applied = runner.run_pending(pg_url, migrations_dir=str(mig_dir))
        print("applied:", applied)
        assert applied.get("ok") is True
        assert applied["applied"] == [stem]
        assert _amount_of(pg_url, "doc-1") == "10.00"
    finally:
        smoke_helpers.init_all_tables(pg_url)

    # The module manager's real shape: a file path while ERPCLAW_DB_URL names
    # the live database. The runner resolves the target, so the fixture
    # migration heals the live rows and ledgers there.
    smoke_helpers.init_all_tables(pg_url)
    _provision_demo(pg_url)
    _seed_demo(pg_url, [("doc-1", "10.0"), ("doc-2", "7.5")])
    try:
        shaped = runner.run_pending(
            str(tmp_path / "module.sqlite"), migrations_dir=str(mig_dir))
        print("shaped:", shaped)
        assert shaped.get("ok") is True
        assert shaped["applied"] == [stem]
        ledger = [r for r in _ledger_rows(pg_url) if r["id"] == stem]
        assert len(ledger) == 1
        assert ledger[0]["status"] == "applied"
        assert _amount_of(pg_url, "doc-1") == "10.00"
    finally:
        smoke_helpers.init_all_tables(pg_url)


def test_failed_migration_recovery_replaces_failed_row(db_path, tmp_path):
    """A later success replaces the failed row with an applied one."""
    stem = "001_flagged_heal"
    _write_fixture(tmp_path, stem, FIXTURE_FLAGGED)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    failed = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert failed.get("ok") is False
    assert failed["failed"] == stem
    failed_rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(failed_rows) == 1
    assert failed_rows[0]["status"] == "failed"
    assert failed_rows[0]["applied_at"] is None

    with open(os.path.join(str(tmp_path), "allow"), "w", encoding="utf-8") as fh:
        fh.write("ok")

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is True
    assert res["applied"] == [stem]
    rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["applied_at"] is not None
    assert [r for r in rows if r["status"] == "failed"] == []
    assert _amount_of(db_path, "doc-1") == "4.00"


def test_failure_with_connection_open_still_records_failed(db_path, tmp_path):
    """A migration that raises with its connection open is still ledgered."""
    stem = "001_open_conn_boom"
    _write_fixture(tmp_path, stem, FIXTURE_OPEN_CONN)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert "ledger_error" not in res
    rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["applied_at"] is None
    assert rows[0]["module_name"] == "erpclaw-setup"
    assert _amount_of(db_path, "doc-1") == "4.0"


def test_applied_then_failed_records_both_rows(db_path, tmp_path):
    """An applied migration keeps its row while the failed one is marked failed."""
    first = "001_first_ok"
    second = "002_second_boom"
    _write_fixture(tmp_path, first, FIXTURE_ROWS)
    _write_fixture(tmp_path, second, FIXTURE_RAISE)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "10.0")])

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["applied"] == [first]
    assert res["failed"] == second
    first_rows = [r for r in _ledger_rows(db_path) if r["id"] == first]
    second_rows = [r for r in _ledger_rows(db_path) if r["id"] == second]
    assert len(first_rows) == 1
    assert first_rows[0]["status"] == "applied"
    assert len(second_rows) == 1
    assert second_rows[0]["status"] == "failed"
    assert second_rows[0]["applied_at"] is None


def test_partial_commit_leaves_committed_work_and_failed_row(db_path, tmp_path):
    """A migration that commits part of its work keeps that part plus a failed row."""
    stem = "001_partial_commit"
    _write_fixture(tmp_path, stem, FIXTURE_PARTIAL)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert res["applied"] == []
    assert "extra_note" in seam.column_names(DEMO, db_path)
    rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"

    again = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert again.get("ok") is False
    assert again["failed"] == stem
    assert again["applied"] == []
    assert seam.column_names(DEMO, db_path).count("extra_note") == 1
    rows = [r for r in _ledger_rows(db_path) if r["id"] == stem]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


def test_failed_ledger_write_keeps_migration_error(db_path, tmp_path, monkeypatch):
    """A failed ledger write never hides the migration's own error."""
    stem = "001_boom_heal"
    _write_fixture(tmp_path, stem, FIXTURE_BOOM)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    real_record = runner._record

    def _flaky(db_arg, ledger_id, status, module_name):
        if status == "failed":
            raise OSError("disk full")
        return real_record(db_arg, ledger_id, status, module_name)

    monkeypatch.setattr(runner, "_record", _flaky)

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert "boom" in str(res.get("error", "")).lower()
    assert "disk full" in str(res.get("ledger_error", ""))


def test_dry_run_on_fresh_database_creates_no_ledger(tmp_path):
    """A dry run against a fresh database creates no ledger table."""
    fresh = str(tmp_path / "fresh.sqlite")
    conn = get_connection(fresh)
    conn.close()
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    _write_fixture(mig_dir, "001_demo_amount_heal", FIXTURE_ROWS)

    res = runner.run_pending(fresh, dry_run=True, migrations_dir=str(mig_dir))

    assert res["pending"] == ["001_demo_amount_heal"]
    assert seam.table_exists(LEDGER, fresh) is False


def test_migrate_action_reports_failure(db_path, monkeypatch, capsys):
    """The migrate action names the failed migration and what applied before it."""
    from setup_helpers import load_db_query, ns
    mod = load_db_query()
    assert hasattr(mod, "_load_migration_runner")

    class _Stub:
        def run_pending(self, db_arg=None, dry_run=False):
            return {"ok": False, "applied": ["001_ok"], "failed": "002_boom",
                    "error": "boom", "detail": EXPECTED_DETAIL}

    monkeypatch.setattr(mod, "_load_migration_runner", lambda: _Stub())
    with pytest.raises(SystemExit) as exc:
        mod.migrate_action(None, ns(db_path=db_path, dry_run=False))
    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == ("Migration '002_boom' failed: boom. "
                                  "Applied before failure: ['001_ok'].")
    assert payload["suggestion"] == EXPECTED_DETAIL


def test_migrate_action_reports_ledger_error(db_path, monkeypatch, capsys):
    """The migrate action appends the ledger-write failure when present."""
    from setup_helpers import load_db_query, ns
    mod = load_db_query()
    assert hasattr(mod, "_load_migration_runner")

    class _Stub:
        def run_pending(self, db_arg=None, dry_run=False):
            return {"ok": False, "applied": ["001_ok"], "failed": "002_boom",
                    "error": "boom", "detail": EXPECTED_DETAIL,
                    "ledger_error": "disk full"}

    monkeypatch.setattr(mod, "_load_migration_runner", lambda: _Stub())
    with pytest.raises(SystemExit) as exc:
        mod.migrate_action(None, ns(db_path=db_path, dry_run=False))
    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == ("Migration '002_boom' failed: boom. "
                                  "Applied before failure: ['001_ok']. "
                                  "The failure could not be recorded in the ledger: "
                                  "disk full.")
    assert payload["suggestion"] == EXPECTED_DETAIL


EXPECTED_RECORD_DETAIL = ("The migration ran and its changes are committed, but its "
                          "ledger row could not be written, so it will run again on "
                          "the next run and must be idempotent. Fix the ledger write "
                          "and re-run.")

NO_TARGET_MESSAGE = ("ERPCLAW_DB_DIALECT=postgresql but the migration runner has no target: "
                     "set ERPCLAW_DB_URL, pass a postgresql:// URL, or set ERPCLAW_DB_PATH to a postgresql:// URL.")

FALLBACK_URL = "postgresql://runner-test@db.invalid/fallback_db"
ENV_URL = "postgresql://runner-test@db.invalid/url_db"

FIXTURE_SECOND_OK = '''import os
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE mig_demo_doc SET amount = ? WHERE id = ?",
                     ("77.77", "doc-2"))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"migration": MIGRATION_ID, "updated": "doc-2"}
'''

FIXTURE_THIRD_NEVER = '''import os
from erpclaw_lib.db import get_connection

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]
MIGRATION_DATA_CLASS = "rows"


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE mig_demo_doc SET amount = ? WHERE id = ?",
                     ("88.88", "doc-2"))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"migration": MIGRATION_ID, "updated": "doc-2"}
'''


def test_applied_record_failure_returns_record_stage(db_path, tmp_path, monkeypatch):
    """A committed migration whose applied ledger write fails reports stage record."""
    first = "001_demo_amount_heal"
    second = "002_second_commit"
    third = "003_third_never"
    _write_fixture(tmp_path, first, FIXTURE_ROWS)
    _write_fixture(tmp_path, second, FIXTURE_SECOND_OK)
    _write_fixture(tmp_path, third, FIXTURE_THIRD_NEVER)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "10.0"), ("doc-2", "7.5")])

    real_record = runner._record

    def _flaky(db_arg, ledger_id, status, module_name):
        if status == "applied" and ledger_id == second:
            raise OSError("disk full")
        return real_record(db_arg, ledger_id, status, module_name)

    monkeypatch.setattr(runner, "_record", _flaky)

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == second
    assert res["stage"] == "record"
    assert res["error"] == "disk full"
    assert res["ledger_error"] == "disk full"
    assert res["detail"] == EXPECTED_RECORD_DETAIL
    assert res["applied"] == [first]
    rows = _ledger_rows(db_path)
    first_rows = [r for r in rows if r["id"] == first]
    assert len(first_rows) == 1
    assert first_rows[0]["status"] == "applied"
    assert [r for r in rows if r["id"] == second] == []
    assert [r for r in rows if r["id"] == third] == []
    assert _amount_of(db_path, "doc-1") == "10.00"
    assert _amount_of(db_path, "doc-2") == "77.77"


def test_migrate_action_reports_unrecorded_success(db_path, monkeypatch, capsys):
    """The migrate action reports a ran-but-unrecorded migration distinctly."""
    from setup_helpers import load_db_query, ns
    mod = load_db_query()
    assert hasattr(mod, "_load_migration_runner")

    class _Stub:
        def run_pending(self, db_arg=None, dry_run=False):
            return {"ok": False, "module": "erpclaw-setup", "applied": ["001_ok"],
                    "failed": "002_commit", "stage": "record", "error": "disk full",
                    "ledger_error": "disk full", "detail": EXPECTED_RECORD_DETAIL}

    monkeypatch.setattr(mod, "_load_migration_runner", lambda: _Stub())
    with pytest.raises(SystemExit) as exc:
        mod.migrate_action(None, ns(db_path=db_path, dry_run=False))
    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == ("Migration '002_commit' ran but could not be "
                                  "recorded in the ledger: disk full. "
                                  "Applied before it: ['001_ok'].")
    assert payload["suggestion"] == EXPECTED_RECORD_DETAIL


def test_run_failure_carries_run_stage(db_path, tmp_path):
    """A plain migration failure reports stage run."""
    stem = "001_boom_heal"
    _write_fixture(tmp_path, stem, FIXTURE_BOOM)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert res["stage"] == "run"


def test_run_failure_ledger_error_carries_run_stage(db_path, tmp_path, monkeypatch):
    """A migration failure with a failed ledger write still reports stage run."""
    stem = "001_boom_heal"
    _write_fixture(tmp_path, stem, FIXTURE_BOOM)
    _provision_demo(db_path)
    _seed_demo(db_path, [("doc-1", "4.0")])

    real_record = runner._record

    def _flaky(db_arg, ledger_id, status, module_name):
        if status == "failed":
            raise OSError("disk full")
        return real_record(db_arg, ledger_id, status, module_name)

    monkeypatch.setattr(runner, "_record", _flaky)

    res = runner.run_pending(db_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is False
    assert res["failed"] == stem
    assert res["stage"] == "run"
    assert res["ledger_error"] == "disk full"


def test_postgresql_no_target_refused_before_any_work(monkeypatch, tmp_path):
    """With no PostgreSQL target the runner raises before touching anything."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    empty = tmp_path / "empty_migs"
    empty.mkdir()
    seam_calls = []
    connect_calls = []
    record_calls = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        return False

    def _stub_connect(db_arg):
        connect_calls.append(db_arg)
        raise AssertionError("must not connect without a target")

    def _stub_record(db_arg, ledger_id, status, module_name):
        record_calls.append((db_arg, ledger_id, status, module_name))

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)

    with pytest.raises(RuntimeError) as exc:
        runner.run_pending(None, migrations_dir=str(empty))
    assert str(exc.value) == NO_TARGET_MESSAGE
    assert seam_calls == []
    assert connect_calls == []
    assert record_calls == []


def test_migrate_action_reports_missing_postgresql_target(monkeypatch, capsys):
    """The migrate action surfaces the missing-target refusal as an error."""
    from setup_helpers import load_db_query
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    mod = load_db_query()
    monkeypatch.setattr(sys, "argv", ["db_query.py", "--action", "migrate"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == NO_TARGET_MESSAGE


def _pg_fake_connection():
    class _FakeCursor:
        def execute(self, *args, **kwargs):
            return None

        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def close(self):
            pass

    return _FakeConn()


def _pg_sidecar_fixture(sidecar_path):
    return ("import os\n"
            "MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]\n"
            "MIGRATION_DATA_CLASS = \"rows\"\n"
            "SIDECAR = %r\n"
            "def run_migration(db_path=None, report_only=False):\n"
            "    with open(SIDECAR, \"w\", encoding=\"utf-8\") as fh:\n"
            "        fh.write(repr(db_path))\n"
            "    return {\"migration\": MIGRATION_ID}\n" % (str(sidecar_path),))


def test_postgresql_fallback_url_agreement(monkeypatch, tmp_path):
    """With only ERPCLAW_DB_PATH set, the runner's own steps use that URL, and the migration receives the runner's resolved target."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", FALLBACK_URL)
    seam_calls = []
    connect_args = []
    record_calls = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        return True

    def _stub_connect(db_arg):
        connect_args.append(db_arg)
        return (_pg_fake_connection(), "?")

    def _stub_record(db_arg, ledger_id, status, module_name):
        record_calls.append((db_arg, ledger_id, status, module_name))

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)

    sidecar = tmp_path / "sidecar.txt"
    _write_fixture(tmp_path, "001_sidecar_probe", _pg_sidecar_fixture(str(sidecar)))

    res = runner.run_pending(None, migrations_dir=str(tmp_path))

    assert res.get("ok") is True
    assert seam_calls != []
    assert all(name == LEDGER and arg == FALLBACK_URL for name, arg in seam_calls)
    assert len(connect_args) >= 1
    assert all(runner._resolve_target(a) == FALLBACK_URL for a in connect_args)
    # The migration receives the runner's resolved target, so the sidecar
    # holds that URL.
    assert sidecar.read_text(encoding="utf-8") == repr(FALLBACK_URL)


def test_postgresql_env_url_wins_over_file_path(monkeypatch, tmp_path):
    """ERPCLAW_DB_URL wins over a passed file path and ERPCLAW_DB_PATH, and the migration receives the runner's resolved target."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", ENV_URL)
    monkeypatch.setenv("ERPCLAW_DB_PATH", FALLBACK_URL)
    file_path = str(tmp_path / "module.sqlite")
    seam_calls = []
    connect_args = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        return True

    def _stub_connect(db_arg):
        connect_args.append(db_arg)
        return (_pg_fake_connection(), "?")

    def _stub_record(db_arg, ledger_id, status, module_name):
        return None

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)

    sidecar = tmp_path / "sidecar.txt"
    _write_fixture(tmp_path, "001_sidecar_probe", _pg_sidecar_fixture(str(sidecar)))

    res = runner.run_pending(file_path, migrations_dir=str(tmp_path))

    assert res.get("ok") is True
    assert seam_calls != []
    assert all(name == LEDGER and arg == ENV_URL for name, arg in seam_calls)
    assert len(connect_args) >= 1
    assert all(runner._resolve_target(a) == ENV_URL for a in connect_args)
    # The migration receives the runner's resolved target, so the sidecar
    # holds the environment URL.
    assert sidecar.read_text(encoding="utf-8") == repr(ENV_URL)


def test_postgresql_file_argument_falls_back_to_db_path_url(monkeypatch, tmp_path):
    """A file path argument is ignored on PostgreSQL; ERPCLAW_DB_PATH wins."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", FALLBACK_URL)
    seam_calls = []
    connect_args = []
    record_calls = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        return True

    def _stub_connect(db_arg):
        connect_args.append(db_arg)
        return (_pg_fake_connection(), "?")

    def _stub_record(db_arg, ledger_id, status, module_name):
        record_calls.append((db_arg, ledger_id, status, module_name))

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)

    sidecar = tmp_path / "sidecar.txt"
    _write_fixture(tmp_path, "001_sidecar_probe", _pg_sidecar_fixture(str(sidecar)))

    res = runner.run_pending("/tmp/erpclaw-home/data.sqlite", migrations_dir=str(tmp_path))

    assert res.get("ok") is True
    assert seam_calls != []
    assert all(name == LEDGER and arg == FALLBACK_URL for name, arg in seam_calls)
    assert len(connect_args) >= 1
    assert all(runner._resolve_target(a) == FALLBACK_URL for a in connect_args)
    assert sidecar.read_text(encoding="utf-8") == repr(FALLBACK_URL)


def test_postgresql_non_url_everywhere_is_refused(monkeypatch, tmp_path):
    """Non-URL locations everywhere on PostgreSQL refuse before any work."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", "/tmp/x.sqlite")
    seam_calls = []
    connect_calls = []
    record_calls = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        return False

    def _stub_connect(db_arg):
        connect_calls.append(db_arg)
        raise AssertionError("must not connect without a target")

    def _stub_record(db_arg, ledger_id, status, module_name):
        record_calls.append((db_arg, ledger_id, status, module_name))

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)

    empty = tmp_path / "empty_migs"
    empty.mkdir()

    with pytest.raises(RuntimeError) as exc:
        runner.run_pending("/tmp/y.sqlite", migrations_dir=str(empty))
    assert str(exc.value) == NO_TARGET_MESSAGE
    assert seam_calls == []
    assert connect_calls == []
    assert record_calls == []


@pytest.mark.parametrize(("dialect", "url_env", "path_env", "arg", "expected"), [
    ("postgresql", None, ENV_URL, "postgres://h/b", "postgres://h/b"),
    ("postgresql", None, ENV_URL, "postgresql://h/b", "postgresql://h/b"),
    ("postgresql", None, ENV_URL, "host=h dbname=b", ENV_URL),
    ("postgresql", None, ENV_URL, None, ENV_URL),
    ("postgresql", None, ENV_URL, "/tmp/z.sqlite", ENV_URL),
    ("postgresql", ENV_URL, FALLBACK_URL, "postgresql://h/b", ENV_URL),
    ("sqlite", ENV_URL, ENV_URL, "/tmp/z.sqlite", "/tmp/z.sqlite"),
    ("postgresql", None, None, "host=h dbname=b", NO_TARGET_MESSAGE),
])
def test_resolve_target_postgresql_url_rule(monkeypatch, dialect, url_env, path_env, arg, expected):
    """Only URL locations count as a PostgreSQL target; SQLite passes through."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", dialect)
    if url_env is None:
        monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    else:
        monkeypatch.setenv("ERPCLAW_DB_URL", url_env)
    if path_env is None:
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("ERPCLAW_DB_PATH", path_env)
    if expected == NO_TARGET_MESSAGE and url_env is None and path_env is None:
        with pytest.raises(RuntimeError) as exc:
            runner._resolve_target(arg)
        assert str(exc.value) == NO_TARGET_MESSAGE
    else:
        assert runner._resolve_target(arg) == expected


@pytest.mark.parametrize(("url_env", "path_env", "expected"), [
    (ENV_URL, None, ENV_URL),
    (ENV_URL, FALLBACK_URL, ENV_URL),
    (None, FALLBACK_URL, FALLBACK_URL),
])
def test_runner_and_library_agree_without_an_argument(monkeypatch, url_env, path_env, expected):
    """With no explicit argument the runner and the library resolve the same PostgreSQL target."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    if url_env is None:
        monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    else:
        monkeypatch.setenv("ERPCLAW_DB_URL", url_env)
    if path_env is None:
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("ERPCLAW_DB_PATH", path_env)
    assert runner._resolve_target(None) == _resolve_pg_url(None)
    assert runner._resolve_target(None) == expected


def test_postgresql_shopify_migration_uses_runner_target(monkeypatch, tmp_path):
    """The Shopify pairing migration questions the runner's resolved target."""
    import erpclaw_lib.db as db_module
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", "postgresql://runner-test@db.invalid/a")
    expected = "postgresql://runner-test@db.invalid/b"
    seam_calls = []
    record_calls = []

    def _stub_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        if name == LEDGER:
            return False
        if name == "shopify_account":
            return True
        return False

    def _stub_columns(table, db_arg=None):
        seam_calls.append((table, db_arg))
        return ["pairing_method", "hmac_secret_enc", "last_status_push_at",
                "disconnect_state", "status_mode", "erpclaw_url_override"]

    def _stub_connect(db_arg):
        return (_pg_fake_connection(), "?")

    def _stub_record(db_arg, ledger_id, status, module_name):
        record_calls.append((db_arg, ledger_id, status, module_name))

    def _no_pending_columns(db_path=None):
        raise AssertionError("no column may be pending")

    monkeypatch.setattr(seam, "table_exists", _stub_exists)
    monkeypatch.setattr(seam, "column_names", _stub_columns)
    monkeypatch.setattr(runner, "_connect", _stub_connect)
    monkeypatch.setattr(runner, "_record", _stub_record)
    monkeypatch.setattr(db_module, "get_connection", _no_pending_columns)

    mig_dir = tmp_path / "shopify_migs"
    mig_dir.mkdir()
    src = os.path.normpath(os.path.join(
        _SETUP_DIR, "..", "..", "..", "erpclaw-addons",
        "erpclaw-integrations-shopify", "migrations",
        "001_shopify_account_v11_pairing_columns.py"))
    with open(src, "r", encoding="utf-8") as fh:
        body = fh.read()
    with open(os.path.join(str(mig_dir),
                           "001_shopify_account_v11_pairing_columns.py"),
              "w", encoding="utf-8") as fh:
        fh.write(body)

    res = runner.run_pending(expected, migrations_dir=str(mig_dir),
                             module_name="erpclaw-integrations-shopify")

    shopify_calls = [arg for name, arg in seam_calls if name == "shopify_account"]
    assert shopify_calls != []
    assert all(arg == expected for arg in shopify_calls)
    ledger_calls = [arg for name, arg in seam_calls if name == LEDGER]
    assert ledger_calls != []
    assert all(arg == expected for arg in ledger_calls)
    assert len(record_calls) == 1
    assert record_calls[0][1] == "erpclaw-integrations-shopify:001_shopify_account_v11_pairing_columns"
    assert record_calls[0][2] == "applied"
    assert res.get("ok") is True
    assert res["applied"] == ["001_shopify_account_v11_pairing_columns"]
