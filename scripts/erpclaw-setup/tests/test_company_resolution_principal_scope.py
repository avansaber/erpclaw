"""Principal-aware no-company resolution and list-companies at ACTIVE.

Seeds through PyPika inserts on the fixture connection, commits, then calls
the code under test on a get_connection handle. STAGED stays install-wide;
ACTIVE answers from the acting principal's membership scope and refuses
closed on anything unproven or unreadable.
"""
import json
import os
import sqlite3
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from setup_helpers import (
    call_action,
    freeze_snapshot,
    load_db_query,
    ns,
    open_reader,
)
from erpclaw_lib import actor, company_scope, seam
from erpclaw_lib import query_helpers
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table

mod = load_db_query()

P_ONE = "p-one"
P_TWO = "p-two"
CO_A = "co-a"
CO_B = "co-b"
CO_C = "co-c"

SUGGESTION = (
    "Pass the company name (e.g. --company \"Acme\"), "
    "or use --company-id with one of the IDs above."
)

LEGACY_MULTIPLE_LINE = (
    "{\"status\": \"error\", "
    "\"error\": \"Multiple companies found. "
    "Please specify the company by name.\", "
    "\"companies\": [{\"id\": \"co-a\", \"name\": \"Alpha Co\"}, "
    "{\"id\": \"co-b\", \"name\": \"Beta Co\"}], "
    "\"suggestion\": \"Pass the company name (e.g. --company \\\"Acme\\\"), "
    "or use --company-id with one of the IDs above.\", "
    "\"message\": \"Multiple companies found. "
    "Please specify the company by name.\"}"
)

LEGACY_NONE = {
    "status": "error",
    "error": "No company found. Create one first.",
    "suggestion": (
        "Run 'tutorial' to create a demo company, "
        "or 'setup company' to create your own."
    ),
    "message": "No company found. Create one first.",
}

AMBIGUOUS = {
    "status": "error",
    "error": "COMPANY_SCOPE_AMBIGUOUS",
    "companies": [
        {"id": "co-a", "name": "Alpha Co"},
        {"id": "co-b", "name": "Beta Co"},
    ],
    "suggestion": SUGGESTION,
    "message": "COMPANY_SCOPE_AMBIGUOUS",
}

SCOPE_REFUSED = {
    "status": "error",
    "error": "COMPANY_SCOPE_REFUSED",
    "message": "COMPANY_SCOPE_REFUSED",
}

LIST_REFUSED = {
    "status": "error",
    "message": "COMPANY_SCOPE_REFUSED",
}

TABLES = (
    "company",
    "audit_log",
    "authority_install",
    "authority_principal",
    "authority_membership",
)


def _ctx(claim, status):
    return actor.ActorContext(
        os_account=None,
        channel="cli",
        principal_claim=claim,
        hop=(),
        status=status,
    )


def _own_install_id(conn, db_path):
    handle = get_connection(db_path)
    try:
        found = company_scope.install_id(handle)
    finally:
        handle.close()
    if found is None:
        seam.provision_authority_core(db_path)
        handle = get_connection(db_path)
        try:
            found = company_scope.install_id(handle)
        finally:
            handle.close()
    assert found is not None
    return found


def _seed_principal(conn, install, pid, kind="human", disabled_at=None):
    table = Table("authority_principal")
    conn.execute(
        Q.into(table)
        .columns("install_id", "id", "kind", "disabled_at")
        .insert(P(), P(), P(), P())
        .get_sql(),
        (install, pid, kind, disabled_at),
    )


def _seed_membership(conn, install, pid, cid, effect):
    table = Table("authority_membership")
    conn.execute(
        Q.into(table)
        .columns("install_id", "principal_id", "company_id", "effect")
        .insert(P(), P(), P(), P())
        .get_sql(),
        (install, pid, cid, effect),
    )


def _seed_company(conn, cid, name, abbr):
    table = Table("company")
    conn.execute(
        Q.into(table)
        .columns(
            "id",
            "name",
            "abbr",
            "default_currency",
            "country",
            "fiscal_year_start_month",
        )
        .insert(P(), P(), P(), P(), P(), P())
        .get_sql(),
        (cid, name, abbr, "USD", "United States", 1),
    )


def _go_active(conn):
    table = Table("authority_install")
    conn.execute(
        Q.update(table).set(Field("phase"), P()).get_sql(),
        ("ACTIVE",),
    )
    conn.commit()


def _handle(db_path):
    return get_connection(db_path)


def _attested(monkeypatch, claim):
    ctx = _ctx(claim, actor.ATTESTED)
    monkeypatch.setattr(actor, "current", lambda: ctx)
    return ctx


def _resolve_refusal(handle):
    with pytest.raises(SystemExit):
        query_helpers.resolve_company_id(handle)


class _Recorder:
    def __init__(self, inner):
        self.__dict__["_inner"] = inner
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if params is None:
            return self.__dict__["_inner"].execute(sql)
        return self.__dict__["_inner"].execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)


class _FailFirst:
    def __init__(self, inner, table, probe):
        self.__dict__["_inner"] = inner
        self.__dict__["_table"] = table
        self.__dict__["_probe"] = probe

    def execute(self, sql, params=None):
        is_probe = "LIMIT 0" in sql
        if self.__dict__["_table"] in sql and is_probe == self.__dict__["_probe"]:
            raise RuntimeError("boom")
        if params is None:
            return self.__dict__["_inner"].execute(sql)
        return self.__dict__["_inner"].execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)


class _ProbeMissing:
    def __init__(self, inner, error):
        self.__dict__["_inner"] = inner
        self.__dict__["_error"] = error

    def execute(self, sql, params=None):
        if "authority_install" in sql and "LIMIT 0" in sql:
            raise self.__dict__["_error"]
        if params is None:
            return self.__dict__["_inner"].execute(sql)
        return self.__dict__["_inner"].execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)


def test_staged_multiple_is_legacy_text(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        for ctx in (
            _ctx(P_ONE, actor.ATTESTED),
            _ctx(P_ONE, actor.CLAIMED),
            _ctx(None, actor.ABSENT),
        ):
            monkeypatch.setattr(actor, "current", lambda: ctx)
            with pytest.raises(SystemExit):
                query_helpers.resolve_company_id(handle)
            assert capsys.readouterr().out.strip() == LEGACY_MULTIPLE_LINE
            with pytest.raises(SystemExit):
                query_helpers.resolve_scope_company(handle)
            assert capsys.readouterr().out.strip() == LEGACY_MULTIPLE_LINE
    finally:
        handle.close()


def test_staged_sole_and_none(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    conn.commit()
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        assert query_helpers.resolve_company_id(handle) == CO_B
        table = Table("company")
        conn.execute(Q.from_(table).delete().get_sql())
        conn.commit()
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == LEGACY_NONE
    finally:
        handle.close()


def test_staged_list_is_install_wide(conn, db_path, monkeypatch):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_company(conn, CO_C, "Gamma Co", "GC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        result = call_action(
            mod.list_companies, handle, ns(limit=None, offset=None)
        )
        assert [c["id"] for c in result["companies"]] == [CO_A, CO_B, CO_C]
        assert result["total_count"] == 3
        assert result["has_more"] is False
    finally:
        handle.close()


def test_staged_reads_no_membership(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    conn.commit()
    handle = _handle(db_path)
    try:
        wrapper = _Recorder(handle)
        assert query_helpers.resolve_company_id(wrapper) == CO_B
        assert not any(
            "authority_membership" in statement
            or "authority_principal" in statement
            for statement in wrapper.statements
        )
    finally:
        handle.close()


def test_active_one_in_scope_is_default(conn, db_path, monkeypatch):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_company(conn, CO_C, "Gamma Co", "GC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        assert query_helpers.resolve_company_id(handle) == CO_B
        assert query_helpers.resolve_scope_company(handle) == CO_B
    finally:
        handle.close()


def test_active_several_in_scope_is_ambiguous(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_company(conn, CO_C, "Gamma Co", "GC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == AMBIGUOUS
    finally:
        handle.close()


def test_active_denied_company_is_out(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "deny")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        assert query_helpers.resolve_company_id(handle) == CO_A
        result = call_action(
            mod.list_companies, handle, ns(limit=None, offset=None)
        )
        assert [c["id"] for c in result["companies"]] == [CO_A]
        assert result["total_count"] == 1
        membership = Table("authority_membership")
        conn.execute(
            Q.into(membership)
            .columns("install_id", "principal_id", "company_id", "effect")
            .insert(P(), P(), P(), P())
            .get_sql(),
            (install, P_ONE, CO_A, "deny"),
        )
        conn.commit()
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
    finally:
        handle.close()


def test_active_membership_for_missing_company(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, "co-gone", "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        assert query_helpers.resolve_company_id(handle) == CO_A
        membership = Table("authority_membership")
        conn.execute(Q.from_(membership).delete().get_sql())
        conn.commit()
        _seed_membership(conn, install, P_ONE, "co-gone", "allow")
        conn.commit()
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
        result = call_action(
            mod.list_companies, handle, ns(limit=None, offset=None)
        )
        assert result["companies"] == []
        assert result["total_count"] == 0
        assert result["has_more"] is False
    finally:
        handle.close()


def test_active_list_in_scope_only(conn, db_path, monkeypatch):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_company(conn, CO_C, "Gamma Co", "GC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_C, "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        result = call_action(
            mod.list_companies, handle, ns(limit=1, offset=0)
        )
        assert [c["id"] for c in result["companies"]] == [CO_A]
        assert result["total_count"] == 2
        assert result["has_more"] is True
        result = call_action(
            mod.list_companies, handle, ns(limit=None, offset=None)
        )
        assert [c["id"] for c in result["companies"]] == [CO_A, CO_C]
        assert result["total_count"] == 2
        assert result["has_more"] is False
    finally:
        handle.close()


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "absent",
        "claimed",
        "invalid",
        "unknown",
        "nomembership",
        "disabled",
    ],
)
def test_active_unknown_or_unproven_principal(
    conn, db_path, monkeypatch, capsys, case
):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE, disabled_at=7 if case == "disabled" else None)
    if case == "nomembership":
        _seed_principal(conn, install, P_TWO)
    else:
        _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    _go_active(conn)
    contexts = {
        "none": None,
        "absent": _ctx(None, actor.ABSENT),
        "claimed": _ctx(P_ONE, actor.CLAIMED),
        "invalid": _ctx(P_ONE, actor.INVALID),
        "unknown": _ctx("p-nobody", actor.ATTESTED),
        "nomembership": _ctx(P_TWO, actor.ATTESTED),
        "disabled": _ctx(P_ONE, actor.ATTESTED),
    }
    monkeypatch.setattr(actor, "current", lambda: contexts[case])
    handle = _handle(db_path)
    try:
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
        result = call_action(
            mod.list_companies, handle, ns(limit=None, offset=None)
        )
        assert result == LIST_REFUSED
    finally:
        handle.close()


def test_active_zero_companies_in_install(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
    finally:
        handle.close()


@pytest.mark.parametrize(
    "table,probe",
    [
        ("authority_install", True),
        ("authority_install", False),
        ("authority_membership", True),
        ("authority_membership", False),
    ],
)
def test_active_read_failures(conn, db_path, monkeypatch, capsys, table, probe):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    _go_active(conn)
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        wrapper = _FailFirst(handle, table, probe)
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(wrapper)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
        result = call_action(
            mod.list_companies, wrapper, ns(limit=None, offset=None)
        )
        assert result == LIST_REFUSED
    finally:
        handle.close()


def test_resolution_scope_unit(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    assert company_scope.SCOPE_AMBIGUOUS == "COMPANY_SCOPE_AMBIGUOUS"
    assert not issubclass(company_scope.ScopeRefused, ValueError)
    handle = _handle(db_path)
    try:
        assert company_scope.install_phase(handle) == "STAGED"
        assert (
            company_scope.resolution_scope(handle, _ctx(P_ONE, actor.ATTESTED))
            is None
        )
        assert company_scope.resolution_scope(handle, None) is None
    finally:
        handle.close()
    _go_active(conn)
    handle = _handle(db_path)
    try:
        assert company_scope.install_phase(handle) == "ACTIVE"
        assert company_scope.resolution_scope(
            handle, _ctx(P_ONE, actor.ATTESTED)
        ) == frozenset({CO_A})
        with pytest.raises(company_scope.ScopeRefused) as first:
            company_scope.resolution_scope(
                handle, _ctx(P_ONE, actor.CLAIMED)
            )
        assert first.value.code == "COMPANY_SCOPE_REFUSED"
        assert first.value.args == ("COMPANY_SCOPE_REFUSED",)
        with pytest.raises(company_scope.ScopeRefused) as second:
            company_scope.resolution_scope(handle, P_ONE)
        assert second.value.code == "COMPANY_SCOPE_REFUSED"
        assert second.value.args == ("COMPANY_SCOPE_REFUSED",)
    finally:
        handle.close()


def test_no_writes(conn, db_path, monkeypatch, capsys):
    install = _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    _seed_company(conn, CO_C, "Gamma Co", "GC")
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    _seed_principal(conn, install, P_TWO)
    _seed_membership(conn, install, P_TWO, CO_B, "allow")
    conn.commit()
    _go_active(conn)
    reader = open_reader(db_path)
    try:
        before = freeze_snapshot(reader, db_path, TABLES)
    finally:
        reader.close()
    _attested(monkeypatch, P_TWO)
    handle = _handle(db_path)
    try:
        assert query_helpers.resolve_company_id(handle) == CO_B
    finally:
        handle.close()
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert json.loads(capsys.readouterr().out.strip()) == AMBIGUOUS
    finally:
        handle.close()
    handle = _handle(db_path)
    try:
        wrapper = _FailFirst(handle, "authority_install", True)
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(wrapper)
        assert json.loads(capsys.readouterr().out.strip()) == SCOPE_REFUSED
        result = call_action(
            mod.list_companies, wrapper, ns(limit=None, offset=None)
        )
        assert result == LIST_REFUSED
    finally:
        handle.close()
    reader = open_reader(db_path)
    try:
        assert freeze_snapshot(reader, db_path, TABLES) == before
    finally:
        reader.close()


def test_staged_equivalent_when_install_table_missing_sqlite(
    conn, db_path, monkeypatch, capsys
):
    _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    conn.commit()
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        wrapper = _ProbeMissing(
            handle,
            sqlite3.OperationalError("no such table: authority_install"),
        )
        assert company_scope.install_phase(wrapper) is None
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(wrapper)
        assert capsys.readouterr().out.strip() == LEGACY_MULTIPLE_LINE
        result = call_action(
            mod.list_companies, wrapper, ns(limit=None, offset=None)
        )
        assert [c["id"] for c in result["companies"]] == [CO_A, CO_B]
        assert result["total_count"] == 2
    finally:
        handle.close()


def test_staged_equivalent_when_install_table_missing_postgresql(
    conn, db_path, monkeypatch
):
    psycopg2 = pytest.importorskip("psycopg2")
    _own_install_id(conn, db_path)
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    conn.commit()
    ctx = _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        wrapper = _ProbeMissing(
            handle,
            psycopg2.errors.UndefinedTable(
                'relation "authority_install" does not exist'
            ),
        )
        assert company_scope.install_phase(wrapper) is None
        assert company_scope.resolution_scope(wrapper, ctx) is None
        bad_column = _ProbeMissing(
            handle,
            psycopg2.errors.UndefinedColumn('column "phase" does not exist'),
        )
        with pytest.raises(company_scope.ScopeRefused):
            company_scope.resolution_scope(bad_column, ctx)
    finally:
        handle.close()


def test_staged_equivalent_when_install_row_missing(
    conn, db_path, monkeypatch, capsys
):
    _own_install_id(conn, db_path)
    table = Table("authority_install")
    conn.execute(Q.from_(table).delete().get_sql())
    conn.commit()
    _seed_company(conn, CO_A, "Alpha Co", "AC")
    _seed_company(conn, CO_B, "Beta Co", "BC")
    conn.commit()
    _attested(monkeypatch, P_ONE)
    handle = _handle(db_path)
    try:
        assert company_scope.install_phase(handle) is None
        with pytest.raises(SystemExit):
            query_helpers.resolve_company_id(handle)
        assert capsys.readouterr().out.strip() == LEGACY_MULTIPLE_LINE
    finally:
        handle.close()
