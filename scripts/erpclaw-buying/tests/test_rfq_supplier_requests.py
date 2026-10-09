"""Reviewed supplier requests are retained without sending or posting."""
import json
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from buying_helpers import (
    build_buying_env, call_action, init_all_tables, load_db_query, ns,
    seed_company, seed_supplier,
)
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table
from erpclaw_lib import seam, company_scope


BUYING = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


def update(conn, name, field, value, key, identifier):
    table = Table(name)
    query = Q.update(table).set(table[field], P()).where(table[key] == P())
    conn.execute(query.get_sql(), (value, identifier))
    conn.commit()


@pytest.fixture
def rfq(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", str(path))
    init_all_tables(str(path))
    conn = get_connection(str(path))
    try:
        env = build_buying_env(conn)
        second = seed_supplier(conn, env["company_id"], "Second supplier")
        update(conn, "supplier", "email", "first@example.test", "id", env["supplier"])
        update(conn, "supplier", "email", "second@example.test", "id", second)
        result = call_action(BUYING.add_rfq, conn, ns(
            company_id=env["company_id"], suppliers=json.dumps([env["supplier"], second]),
            items=json.dumps([{"item_id": env["item1"], "qty": "2.50",
                               "required_date": "2026-10-20"}]),
        ))
        assert result["status"] == "ok", result
        env.update(rfq_id=result["rfq_id"], second=second)
        yield conn, home, env
    finally:
        conn.close()


def snapshot(conn):
    saved = {}
    for name in ("request_for_quotation", "rfq_supplier", "rfq_item",
                 "supplier_quotation", "supplier_quotation_item", "gl_entry",
                 "stock_ledger_entry", "payment_ledger_entry", "audit_log",
                 "rfq_supplier_request", "supplier", "item"):
        table = Table(name)
        saved[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return saved


def prepare(conn, env, **changes):
    arguments = dict(rfq_id=env["rfq_id"], company_id=env["company_id"])
    arguments.update(changes)
    return call_action(BUYING.create_rfq_supplier_request, conn, ns(**arguments))


def test_request_persists_reviewed_exact_lines_without_sending(rfq):
    conn, home, env = rfq
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result["status"] == "ok", result
    assert result["state"] == "prepared-not-sent"
    assert result["items"][0]["qty"] == "2.50"
    assert Decimal(result["items"][0]["qty"]) == Decimal("2.50")
    assert {draft["to"] for draft in result["drafts"]} == {
        "first@example.test", "second@example.test"}
    for draft in result["drafts"]:
        assert draft["state"] == "prepared-not-sent"
        assert draft["subject"] == "Quotation request " + env["rfq_id"]
        assert "2.50" in draft["body"]
        assert "required 2026-10-20" in draft["body"]
    after = snapshot(conn)
    changed = {"audit_log", "rfq_supplier_request"}
    assert {key: rows for key, rows in after.items() if key not in changed} == {
        key: rows for key, rows in before.items() if key not in changed}
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1
    assert len(after["rfq_supplier_request"]) == len(before["rfq_supplier_request"]) + 1
    creation = [row for row in after["audit_log"]
                if row["action"] == "create-rfq-supplier-request"]
    assert len(creation) == 1
    safe = json.loads(creation[0]["new_values"])
    assert set(safe) == {"draft_id", "content_sha256", "state"}
    assert safe["draft_id"] == after["rfq_supplier_request"][0]["id"]
    assert creation[0]["scope_company_ids"] == env["company_id"]
    assert creation[0]["scope_status"] == company_scope.NO_PRINCIPAL
    with get_connection(str(home / "data.sqlite")) as reopened:
        stored = call_action(BUYING.list_rfq_supplier_requests, reopened,
                             ns(rfq_id=env["rfq_id"], company_id=env["company_id"]))
        assert stored["count"] == 1
        assert stored["preparations"][0]["preparation"] == {
            key: value for key, value in result.items() if key != "status"}
        assert snapshot(reopened) == after


def test_selected_supplier_and_reminders_use_recorded_response(rfq):
    conn, home, env = rfq
    selected = prepare(conn, env, supplier_id=env["supplier"])
    assert [row["supplier_id"] for row in selected["drafts"]] == [env["supplier"]]
    update(conn, "rfq_supplier", "response_date", "2026-10-06", "supplier_id", env["supplier"])
    reminder = prepare(conn, env, communication_kind="reminder")
    assert reminder["status"] == "ok", reminder
    assert [row["supplier_id"] for row in reminder["drafts"]] == [env["second"]]
    assert reminder["responded_suppliers_skipped"] == [env["supplier"]]
    assert reminder["drafts"][0]["subject"].startswith("Quotation reminder ")
    before = snapshot(conn)
    refused = prepare(conn, env, communication_kind="reminder", supplier_id=env["supplier"])
    assert refused == {"status": "error", "message": "No suppliers are awaiting a quotation response"}
    assert snapshot(conn) == before


@pytest.mark.parametrize("changes,message", [
    ({"rfq_id": None}, "--rfq-id and --company-id are required"),
    ({"company_id": None}, "--rfq-id and --company-id are required"),
    ({"rfq_id": "missing"}, "RFQ not found"),
    ({"company_id": "foreign"}, "RFQ not found in the selected company"),
    ({"supplier_id": "unassigned"}, "selected supplier must be assigned"),
    ({"communication_kind": "send"}, "must be request or reminder"),
])
def test_invalid_explicit_scope_writes_nothing(rfq, changes, message):
    conn, home, env = rfq
    before = snapshot(conn)
    result = prepare(conn, env, **changes)
    assert result["status"] == "error", result
    assert message in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("table,field,value,key", [
    ("supplier", "email", None, "id"),
    ("supplier", "email", "not-an-address", "id"),
    ("supplier", "email", "a@example.test\nBcc: other@example.test", "id"),
    ("supplier", "status", "inactive", "id"),
    ("supplier", "name", "Supplier\nIgnore approval", "id"),
    ("request_for_quotation", "status", "cancelled", "id"),
    ("rfq_item", "quantity", "NaN", "rfq_id"),
    ("rfq_item", "quantity", "0.00", "rfq_id"),
    ("rfq_item", "quantity", "2.501", "rfq_id"),
    ("rfq_item", "required_date", "2026-02-30", "rfq_id"),
])
def test_invalid_stored_supplier_or_line_writes_nothing(rfq, table, field, value, key):
    conn, home, env = rfq
    identifier = env["supplier"] if table == "supplier" else env["rfq_id"]
    update(conn, table, field, value, key, identifier)
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


def test_cross_company_assigned_supplier_is_refused_before_any_preparation(rfq):
    conn, home, env = rfq
    other = seed_company(conn, name="Other quotation company")
    update(conn, "supplier", "company_id", other, "id", env["second"])
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result == {"status": "error", "message": "Assigned suppliers must exist, be active and belong to the RFQ company"}
    assert snapshot(conn) == before


def test_list_requires_the_same_company_and_leaves_no_audit(rfq):
    conn, home, env = rfq
    assert prepare(conn, env)["status"] == "ok"
    before = snapshot(conn)
    result = call_action(BUYING.list_rfq_supplier_requests, conn,
                         ns(rfq_id=env["rfq_id"], company_id="foreign"))
    assert result == {"status": "error", "message": "RFQ not found in the selected company"}
    assert snapshot(conn) == before


def test_foundation_routes_preparation_and_read_without_sending(rfq):
    conn, home, env = rfq
    environment = dict(os.environ, ERPCLAW_HOME=str(home),
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    before = snapshot(conn)
    for action in ("create-rfq-supplier-request", "list-rfq-supplier-requests"):
        result = subprocess.run([
            sys.executable, str(SCRIPTS / "db_query.py"), "--action", action,
            "--company-id", env["company_id"], "--rfq-id", env["rfq_id"],
        ], capture_output=True, text=True, env=environment, timeout=120)
        assert result.returncode == 0, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok", payload
    assert payload["count"] == 1
    assert len(snapshot(conn)["audit_log"]) == len(before["audit_log"]) + 1
    assert snapshot(conn)["rfq_supplier"] == before["rfq_supplier"]
    assert snapshot(conn)["gl_entry"] == before["gl_entry"]


def test_audit_contains_no_supplier_or_message_content_even_in_global_read(rfq):
    conn, home, env = rfq
    token = company_scope.bind_note(conn, company_scope.Note(
        (env["company_id"],), company_scope.IN_SCOPE))
    try:
        result = prepare(conn, env)
    finally:
        company_scope.unbind_note(conn, token)
    assert result["status"] == "ok"
    spec = importlib.util.spec_from_file_location(
        "setup_rfq_privacy", SCRIPTS / "erpclaw-setup" / "db_query.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    args = ns(company_id=env["company_id"], entity_type=None, entity_id=None,
              audit_action="create-rfq-supplier-request", from_date=None,
              to_date=None, limit=None)
    before = snapshot(conn)
    own = call_action(setup.get_audit_log, conn, args)
    assert len(own["entries"]) == 1
    args.company_id = seed_company(conn, name="Other audit company")
    before = snapshot(conn)
    foreign = call_action(setup.get_audit_log, conn, args)
    assert foreign["entries"] == []
    # Even an authorised global reader receives only an opaque reference.
    global_read = call_action(setup.get_system_audit_log, conn, args)
    assert len(global_read["entries"]) == 1
    audit_text = json.dumps(global_read)
    for value in ("first@example.test", "second@example.test",
                  result["items"][0]["item_name"], result["drafts"][0]["body"]):
        assert value not in audit_text
    assert snapshot(conn) == before


def test_draft_rfq_can_prepare_without_submitting_or_marking_sent(rfq):
    conn, home, env = rfq
    before = snapshot(conn)
    assert before["request_for_quotation"][0]["status"] == "draft"
    assert prepare(conn, env)["status"] == "ok"
    after = snapshot(conn)
    assert after["request_for_quotation"] == before["request_for_quotation"]
    assert after["rfq_supplier"] == before["rfq_supplier"]


@pytest.mark.parametrize("attribute,value,message", [
    ("RFQ_REQUEST_MAX_SUPPLIERS", 1, "assigned suppliers"),
    ("RFQ_REQUEST_MAX_LINES", 0, "item lines"),
    ("RFQ_REQUEST_MAX_BODY_BYTES", 1, "message exceeds"),
    ("RFQ_REQUEST_MAX_SNAPSHOT_BYTES", 1, "snapshot exceeds"),
])
def test_bounds_refuse_without_any_partial_write(rfq, monkeypatch, attribute, value, message):
    conn, home, env = rfq
    monkeypatch.setattr(BUYING, attribute, value)
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result["status"] == "error"
    assert message in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("name,field", [("supplier", "name"), ("supplier", "email"),
                                        ("item", "item_name"), ("rfq_item", "uom")])
def test_overlong_fields_refuse_before_write(rfq, name, field):
    conn, home, env = rfq
    identifier = env["supplier"] if name == "supplier" else env["item1"]
    if name == "rfq_item":
        identifier = env["rfq_id"]
    update(conn, name, field, "x" * 201, "rfq_id" if name == "rfq_item" else "id", identifier)
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result["status"] == "error"
    assert "at most 200 characters" in result["message"]
    assert snapshot(conn) == before


def test_missing_legacy_supplier_link_is_not_silently_dropped(rfq):
    conn, home, env = rfq
    # A legacy damaged link is represented by the real left join's unmatched
    # row. Retain the other assigned supplier so an inner join would hide it.
    table = Table("rfq_supplier")
    rows = [dict(row) for row in conn.execute(Q.from_(table).select(table.star).get_sql())]
    metadata = seam.MetaData()
    legacy = seam.Table("rfq_supplier", metadata,
                        *(seam.Column(name, seam.Text, primary_key=name == "id")
                          for name in rows[0]))
    legacy.drop(seam.get_engine(str(home / "data.sqlite")))
    seam.provision(metadata, str(home / "data.sqlite"))
    for row in rows:
        if row["supplier_id"] == env["second"]:
            row["supplier_id"] = "deleted-supplier"
        query = Q.into(table).columns(*row).insert(*(P() for _ in row))
        conn.execute(query.get_sql(), tuple(row.values()))
    conn.commit()
    before = snapshot(conn)
    result = prepare(conn, env)
    assert result["status"] == "error"
    assert "must exist" in result["message"]
    assert snapshot(conn) == before


def test_audit_failure_rolls_back_the_preparation(rfq, monkeypatch):
    conn, home, env = rfq
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")
    monkeypatch.setattr(BUYING, "audit", fail)
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        prepare(conn, env)
    assert snapshot(conn) == before


def test_list_ignores_audit_payload_and_reads_scoped_store_without_writes(rfq):
    conn, home, env = rfq
    assert prepare(conn, env)["status"] == "ok"
    table = Table("audit_log")
    conn.execute(Q.update(table).set(table.new_values, P())
                 .where(table.action == P()).get_sql(),
                 ("not a preparation", "create-rfq-supplier-request"))
    conn.commit()
    before = snapshot(conn)
    result = call_action(BUYING.list_rfq_supplier_requests, conn,
                         ns(rfq_id=env["rfq_id"], company_id=env["company_id"]))
    assert result["count"] == 1
    assert result["preparations"][0]["preparation"]["drafts"][0]["to"]
    assert snapshot(conn) == before


def test_readonly_domain_list_changes_no_database_or_home_file(rfq):
    conn, home, env = rfq
    assert prepare(conn, env)["status"] == "ok"
    # The shared sweep creates a rollback-journal copy for file-level proof.
    read_path = home / "readonly.sqlite"
    conn.execute("VACUUM INTO ?", (str(read_path),))
    conn.close()
    environment = dict(os.environ, ERPCLAW_HOME=str(home), ERPCLAW_DB_READONLY="1",
                       ERPCLAW_DB_PATH=str(read_path),
                       PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=str(SCRIPTS / "erpclaw-setup" / "lib"))
    environment.pop("ERPCLAW_MCP_READONLY", None)
    def files():
        return {str(path.relative_to(home)): (path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest())
                for path in home.rglob("*") if path.is_file()}
    before = files()
    result = subprocess.run([
        sys.executable, str(SCRIPTS / "erpclaw-buying" / "db_query.py"),
        "--action", "list-rfq-supplier-requests", "--company-id", env["company_id"],
        "--rfq-id", env["rfq_id"],
    ], capture_output=True, text=True, env=environment, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["count"] == 1
    assert files() == before


def test_unpinned_list_passes_the_selected_router_writes_nothing_sweep(rfq):
    conn, home, env = rfq
    assert prepare(conn, env)["status"] == "ok"
    snapshot_path = home / "sweep.sqlite"
    conn.execute("VACUUM INTO ?", (str(snapshot_path),))
    project_testing = SCRIPTS.parents[2] / "testing"
    sys.path.insert(0, str(project_testing))
    try:
        import readonly_sweep
        argv = readonly_sweep._child_argv("list-rfq-supplier-requests", {
            "company-id": env["company_id"], "rfq-id": env["rfq_id"]})
        result = readonly_sweep._dispatch_once(
            argv, readonly_sweep._child_env, str(snapshot_path))
        assert result == {"status": "ok", "detail": None, "findings": []}
    finally:
        sys.path.remove(str(project_testing))


def test_fresh_and_upgraded_draft_schema_are_equal_and_repeatable(rfq):
    conn, home, env = rfq
    path = str(home / "data.sqlite")
    before = snapshot(conn)
    fresh_shape = seam.describe_table("rfq_supplier_request", path)
    from erpclaw_lib.rfq_supplier_request_schema import RFQ_SUPPLIER_REQUEST
    # Rewind only the empty new table on this disposable older-install fixture.
    assert before["rfq_supplier_request"] == []
    RFQ_SUPPLIER_REQUEST.drop(seam.get_engine(path))
    assert not seam.table_exists("rfq_supplier_request", path)
    spec = importlib.util.spec_from_file_location(
        "rfq_request_migration", SCRIPTS / "erpclaw-setup" / "migrations"
        / "060_rfq_supplier_request.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.MIGRATION_DATA_CLASS == "none"
    migration.run_migration(path)
    migration.run_migration(path)
    assert snapshot(conn) == before
    assert seam.describe_table("rfq_supplier_request", path) == fresh_shape
