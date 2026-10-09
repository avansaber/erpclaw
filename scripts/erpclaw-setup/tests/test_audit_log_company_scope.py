"""Company-scoped ordinary audit reads and explicit system compatibility."""
import os
import sys

import pytest

from setup_helpers import (
    call_action,
    freeze_snapshot,
    init_all_tables,
    is_error,
    is_ok,
    load_db_query,
    ns,
)

from erpclaw_lib import actor, company_scope
from erpclaw_lib.audit import audit
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


MOD = load_db_query()
FILTERS = {
    "entity_type": None,
    "entity_id": None,
    "audit_action": None,
    "from_date": None,
    "to_date": None,
    "offset": None,
}


def _args(company_id=None, limit=None, **updates):
    values = dict(FILTERS)
    values.update(company_id=company_id, limit=limit)
    values.update(updates)
    return ns(**values)


def _insert_company(conn, company_id):
    company = Table("company")
    query = Q.into(company).columns(
        "id", "name", "abbr", "default_currency", "country",
        "fiscal_year_start_month",
    ).insert(P(), P(), P(), P(), P(), P())
    conn.execute(query.get_sql(), (
        company_id, "Company " + company_id, company_id,
        "USD", "United States", 1,
    ))


def _scoped_row(conn, row_id, company_ids, status="in_scope",
                new_values=None, action="update"):
    audit(
        conn, "erpclaw-test", action, "supplier", row_id,
        new_values=new_values or {"email": row_id + "@example.invalid"},
        scope_company_ids=company_ids, scope_status=status,
    )


def _malformed_foreign_row(conn, row_id, company_id):
    audit_log = Table("audit_log")
    query = Q.into(audit_log).columns(
        "id", "timestamp", "skill", "action", "entity_type", "entity_id",
        "new_values", "scope_company_ids", "scope_status",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P())
    conn.execute(query.get_sql(), (
        row_id, "9999-01-01T00:00:00Z", "erpclaw-test", "update",
        "supplier", row_id, "{not-json", company_id, "in_scope",
    ))


def test_exact_company_token_is_filtered_before_limit_and_json_decode(
        conn, db_path):
    _insert_company(conn, "co_1%")
    _insert_company(conn, "co_1%0")
    _insert_company(conn, "CO_1%")
    _scoped_row(conn, "own", ["co_1%"])
    _scoped_row(conn, "shared", ["co_1%0", "co_1%"])
    _scoped_row(conn, "prefix-only", ["co_1%0"])
    _scoped_row(conn, "case-foreign", ["CO_1%"])
    # If the action fetched globally then decoded in Python, this newer
    # foreign row would either consume the limit or raise JSONDecodeError.
    _malformed_foreign_row(conn, "foreign-secret", "co_1%0")
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))

    result = call_action(MOD.get_audit_log, conn, _args("co_1%", limit=2))

    assert is_ok(result), result
    assert {entry["entity_id"] for entry in result["entries"]} == {
        "own", "shared"}
    assert all(entry["entity_id"] != "prefix-only"
               for entry in result["entries"])
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


@pytest.mark.parametrize("status", [
    "out_of_scope", "underived", "no_scope", "no_principal",
    "not_applicable",
])
def test_non_company_scope_statuses_are_never_ordinary_rows(
        conn, db_path, status):
    _insert_company(conn, "co-a")
    _scoped_row(conn, "allowed", ["co-a"])
    _scoped_row(conn, "hidden-" + status, ["co-a"], status=status)
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))

    result = call_action(MOD.get_audit_log, conn, _args("co-a"))

    assert [entry["entity_id"] for entry in result["entries"]] == ["allowed"]
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


def test_legacy_null_scope_is_hidden_and_missing_company_is_refused(
        conn, db_path):
    _insert_company(conn, "co-a")
    _scoped_row(conn, "allowed", ["co-a"])
    _scoped_row(conn, "legacy", None, status=None)
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))

    visible = call_action(MOD.get_audit_log, conn, _args("co-a"))
    refused = call_action(MOD.get_audit_log, conn, _args())

    assert [entry["entity_id"] for entry in visible["entries"]] == ["allowed"]
    assert is_error(refused)
    assert refused["message"] == "--company-id is required"
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


def test_missing_scope_columns_fail_closed_without_output(
        conn, db_path, monkeypatch):
    _insert_company(conn, "co-a")
    _scoped_row(conn, "would-have-been-visible", ["co-a"])
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))
    monkeypatch.setattr(MOD, "scope_columns_present", lambda _conn: False)

    result = call_action(MOD.get_audit_log, conn, _args("co-a"))

    assert is_error(result)
    assert result["message"] == "AUDIT_SCOPE_UNAVAILABLE"
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


@pytest.mark.parametrize("limit", [0, -1, 1001, "1.0", "x", True])
def test_limit_is_a_bounded_positive_integer(conn, db_path, limit):
    _insert_company(conn, "co-a")
    _scoped_row(conn, "allowed", ["co-a"])
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))

    result = call_action(MOD.get_audit_log, conn, _args("co-a", limit=limit))

    assert is_error(result)
    assert result["message"] == (
        "--limit must be a positive integer no greater than 1000")
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


def test_system_surface_preserves_migration_and_legacy_diagnostics(
        conn, db_path):
    _insert_company(conn, "co-a")
    _scoped_row(
        conn, "migration-company", ["co-a"],
        action="migration:099_example", new_values={"result": "company"})
    _scoped_row(
        conn, "migration-legacy", None, status=None,
        action="migration:099_example", new_values={"result": "legacy"})
    conn.commit()
    before = freeze_snapshot(conn, db_path, ("audit_log",))

    result = call_action(
        MOD.get_system_audit_log, conn,
        _args(audit_action="migration:099_example"))

    assert is_ok(result), result
    assert {entry["entity_id"] for entry in result["entries"]} == {
        "migration-company", "migration-legacy"}
    assert {entry["new_values"]["result"] for entry in result["entries"]} == {
        "company", "legacy"}
    assert freeze_snapshot(conn, db_path, ("audit_log",)) == before


def test_active_principal_gate_refuses_omitted_and_foreign_company(
        tmp_path, monkeypatch):
    import authority_fixtures as fixtures

    path = str(tmp_path / "audit-scope.sqlite")
    init_all_tables(path)
    handle = get_connection(path)
    try:
        _insert_company(handle, "co-a")
        _insert_company(handle, "co-b")
        handle.commit()
    finally:
        handle.close()
    fixtures.seed_authority(path, "co-a")
    fixtures.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor.ActorContext(
            None, None, fixtures.SERVICE, (), actor.ATTESTED)
        monkeypatch.setattr(actor, "current", lambda: attested)
        assert company_scope.gate_note(
            handle, "get-audit-log", ["--company-id", "co-a"],
            "ACTIVE") == company_scope.Note(("co-a",), "in_scope")
        with pytest.raises(company_scope.ScopeRefused):
            company_scope.gate_note(
                handle, "get-audit-log", ["--company-id", "co-b"],
                "ACTIVE")
        with pytest.raises(company_scope.ScopeRefused):
            company_scope.gate_note(
                handle, "get-audit-log", [], "ACTIVE")
        # A global diagnostic is refused until the attested principal covers
        # every installed company.
        with pytest.raises(company_scope.ScopeRefused):
            company_scope.gate_note(
                handle, "get-system-audit-log", [], "ACTIVE")
        install = Table("authority_install")
        install_id = handle.execute(
            Q.from_(install).select(install.install_id).get_sql()
        ).fetchone()["install_id"]
        membership = Table("authority_membership")
        handle.execute(
            Q.into(membership).columns(
                "install_id", "principal_id", "company_id", "effect"
            ).insert(P(), P(), P(), P()).get_sql(),
            (install_id, fixtures.SERVICE, "co-b", "allow"),
        )
        handle.commit()
        assert company_scope.gate_note(
            handle, "get-system-audit-log", [], "ACTIVE"
        ) == company_scope.Note(None, "not_applicable")
    finally:
        handle.close()


def test_pins_split_ordinary_from_system_surface():
    erpclaw_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", ".."))
    if erpclaw_root not in sys.path:
        sys.path.insert(0, erpclaw_root)
    from mcp import confirm
    assert "get-audit-log" in confirm.PINNED_READS
    assert "get-system-audit-log" not in confirm.PINNED_READS
