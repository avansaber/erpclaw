"""Detect changed stored audit content against a separately retained digest."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from setup_helpers import call_action, load_db_query, ns
from erpclaw_lib.query import P, Q, Table, insert_row, update_row

mod = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


def populate(conn):
    records = []
    for label in ("first", "second"):
        row = {"id": str(uuid.uuid4()), "timestamp": "2026-10-05 05:30:00",
               "skill": "erpclaw-selling", "action": "add-customer",
               "entity_type": "customer", "entity_id": label,
               "new_values": '{"name":"Synthetic Customer"}',
               "actor_channel": "cli", "scope_company_ids": '["synthetic-company"]'}
        sql, _ = insert_row("audit_log", {key: P() for key in row})
        conn.execute(sql, tuple(row.values()))
        records.append(row)
    conn.commit()
    return records


def snapshot(conn):
    result = {}
    for name in ("audit_log", "company", "gl_entry", "payment_entry"):
        table = Table(name)
        result[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return result


def test_current_digest_and_matching_anchor_write_nothing(conn):
    populate(conn)
    before = snapshot(conn)
    result = call_action(mod.get_audit_checkpoint, conn, ns(audit_checkpoint_sha256=None))
    assert result["status"] == "ok" and result["records"] == 2
    assert result["empty"] is False and result["external_anchor_required"] is True
    assert result["checkpoint_matches"] is None and len(result["sha256"]) == 64
    verified = call_action(mod.get_audit_checkpoint, conn, ns(audit_checkpoint_sha256=result["sha256"]))
    assert verified["checkpoint_matches"] is True
    assert snapshot(conn) == before
    assert "Synthetic Customer" not in json.dumps(result)


@pytest.mark.parametrize("column", ["id", "timestamp", "user_id", "skill", "action",
    "entity_type", "entity_id", "old_values", "new_values", "description",
    "actor_os_account", "actor_channel", "actor_principal_claim", "actor_status",
    "actor_hop", "authorization_id", "authorization_status", "scope_company_ids",
    "scope_status", "actor_session_digest"])
def test_every_stored_field_change_invalidates_anchor(conn, column):
    rows = populate(conn)
    original = mod._audit_checkpoint(conn)["sha256"]
    value = str(uuid.uuid4()) if column == "id" else "changed"
    sql = update_row("audit_log", data={column: P()}, where={"id": P()})
    conn.execute(sql, (value, rows[0]["id"]))
    conn.commit()
    before = snapshot(conn)
    result = mod._audit_checkpoint(conn, original)
    assert result["checkpoint_matches"] is False
    assert snapshot(conn) == before


def test_deletion_and_authorised_append_both_change_whole_table(conn):
    rows = populate(conn)
    original = mod._audit_checkpoint(conn)["sha256"]
    table = Table("audit_log")
    conn.execute(Q.from_(table).delete().where(table.id == P()).get_sql(), (rows[0]["id"],))
    conn.commit()
    assert mod._audit_checkpoint(conn, original)["checkpoint_matches"] is False
    populate(conn)
    assert mod._audit_checkpoint(conn, original)["checkpoint_matches"] is False


def test_row_order_does_not_change_digest(conn):
    populate(conn)
    rows = snapshot(conn)["audit_log"]
    original = mod._audit_checkpoint(conn)["sha256"]
    conn.execute(Q.from_(Table("audit_log")).delete().get_sql())
    for row in reversed(rows):
        sql, _ = insert_row("audit_log", {key: P() for key in row})
        conn.execute(sql, tuple(row.values()))
    conn.commit()
    assert mod._audit_checkpoint(conn)["sha256"] == original


def test_empty_table_is_explicit_and_does_not_claim_historical_integrity(conn):
    result = mod._audit_checkpoint(conn)
    assert result["records"] == 0 and result["empty"] is True
    assert result["checkpoint_matches"] is None and result["external_anchor_required"] is True


@pytest.mark.parametrize("expected", ["", "A" * 64, "0" * 63, "0" * 65, "not-a-digest"])
def test_invalid_anchor_refuses_without_writes(conn, expected):
    populate(conn)
    before = snapshot(conn)
    result = call_action(mod.get_audit_checkpoint, conn, ns(audit_checkpoint_sha256=expected))
    assert result["status"] == "error"
    assert snapshot(conn) == before


def test_limit_refuses_instead_of_certifying_a_partial_table(conn, monkeypatch):
    populate(conn)
    monkeypatch.setattr(mod, "AUDIT_CHECKPOINT_MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="no partial digest"):
        mod._audit_checkpoint(conn)


def test_actual_root_read_and_sweep_preserve_fresh_books(conn, db_path, tmp_path):
    populate(conn)
    before = snapshot(conn)
    expected = mod._audit_checkpoint(conn)["sha256"]
    environment = dict(os.environ, ERPCLAW_HOME=str(Path(db_path).parent),
                       ERPCLAW_DB_PATH=db_path, PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"),
                       PYTHONDONTWRITEBYTECODE="1")
    environment.pop("ERPCLAW_DB_URL", None)
    environment.pop("ERPCLAW_TEST_SESSION", None)
    environment["ERPCLAW_DB_READONLY"] = ""
    run = subprocess.run([sys.executable, str(SCRIPTS / "db_query.py"), "--action",
                          "get-audit-checkpoint", "--audit-checkpoint-sha256", expected],
                         env=environment, text=True, capture_output=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["checkpoint_matches"] is True
    assert snapshot(conn) == before
    sys.path.insert(0, str(SCRIPTS.parents[2] / "testing"))
    import readonly_sweep
    # Use the sweep's rollback-journal snapshot pattern. Opening a WAL
    # database read-only can create SQLite sidecars beside the copied file.
    sealed = tmp_path / "audit-snapshot.sqlite"
    conn.execute("VACUUM INTO ?", (str(sealed),))
    result = readonly_sweep.run_sweep(str(sealed), jobs=1, names=["get-audit-checkpoint"])
    assert result["passed"] == ["get-audit-checkpoint"], json.dumps(result["results"], sort_keys=True)
    assert not result["failed"] and not result["unproven"]
