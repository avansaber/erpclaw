"""Issuer scope refusals at routine issuance."""

import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402
import authority_fixtures as fx  # noqa: E402
from setup_helpers import (  # noqa: E402
    freeze_snapshot, init_all_tables, open_reader, read_all)
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


@pytest.fixture
def two(db_path):
    first = fx.seed_company(db_path)
    second = fx.seed_company(db_path, name="Other Co", abbr="OC")
    info = fx.seed_authority(db_path, first)
    return {"a": first, "b": second, "info": info}


def _gate():
    from erpclaw_lib import authority_gate
    return authority_gate


def _issuance():
    from erpclaw_lib import authorization_issuance
    return authorization_issuance


def _clock():
    from erpclaw_lib import authority_clock
    return authority_clock


def _register(monkeypatch, **over):
    gate = _gate()
    monkeypatch.setitem(
        gate.ENVELOPE_ACTIONS, "add-uom", fx.make_declaration(**over))
    return gate


def _fix_clock(monkeypatch, value):
    clock = _clock()
    monkeypatch.setattr(clock, "now_ms", lambda: value)
    return clock


def _open(db_path):
    return get_connection(db_path)


def _open_company(db_path, company_id, members):
    conn = get_connection(db_path)
    try:
        install_id = fx._install_id(conn)
        fx._insert_row(conn, "authority_right", {
            "install_id": install_id, "principal_id": fx.SERVICE,
            "company_id": company_id, "resource_kind": "company",
            "resource_id": company_id, "action": fx.ACTION,
            "effect": "allow"})
        fx._insert_row(conn, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company_id, "resource_kind": "company",
            "resource_id": company_id, "action": fx.ACTION})
        for principal in members:
            fx._insert_row(conn, "authority_membership", {
                "install_id": install_id, "principal_id": principal,
                "company_id": company_id, "effect": "allow"})
        conn.commit()
    finally:
        conn.close()


def _counts(db_path):
    auth = fx.read_rows(db_path, "operation_authorization", ["id"])
    side = fx.read_rows(
        db_path, "operation_authorization_envelope", ["authorization_id"])
    return len(auth), len(side)


def _snapshot(db_path):
    reader = open_reader(db_path)
    try:
        return freeze_snapshot(reader, db_path, seam.table_names(db_path))
    finally:
        reader.close()


def _active(monkeypatch):
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(
        actor, "current",
        lambda: actor.ActorContext(
            None, None, fx.SERVICE, (), actor.ATTESTED))


def test_in_scope_issue_at_active(db_path, two, monkeypatch):
    """not qualification: allowed path still issues."""
    mod = _issuance()
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    conn = _open(db_path)
    try:
        out = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-in-1")
    finally:
        conn.close()
    assert out["issued_route"] == "delegation"
    assert _counts(db_path) == (1, 1)
    usage = fx.read_rows(db_path, "authority_delegation_usage", ["used"])
    assert len(usage) == 1


def test_principal_out_of_scope_refused_writes_nothing(
        db_path, two, monkeypatch):
    """not qualification: missing rights still refuse."""
    mod = _issuance()
    _open_company(db_path, two["b"], [fx.OWNER])
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    before = _snapshot(db_path)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["b"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-principal-1")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    assert _snapshot(db_path) == before
    assert _counts(db_path) == (0, 0)
    side = fx.read_rows(
        db_path, "operation_authorization_envelope", ["idempotency_key"])
    assert [row for row in side if row["idempotency_key"] == "scope-principal-1"] == []


def test_issuer_out_of_scope_refused(db_path, two, monkeypatch):
    """not qualification: outside issuer scope refuses."""
    mod = _issuance()
    _open_company(db_path, two["b"], [fx.SERVICE])
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    before = _snapshot(db_path)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["b"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-issuer-1")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    assert _snapshot(db_path) == before
    fx.set_principal_disabled(db_path, fx.OWNER, two["info"]["now"])
    before2 = _snapshot(db_path)
    conn2 = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo2:
            mod.issue_envelope(
                conn2, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["a"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-issuer-2")
    finally:
        conn2.close()
    assert excinfo2.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    assert _snapshot(db_path) == before2


def test_refusal_is_indistinguishable(db_path, two, monkeypatch):
    """not qualification: refusals reveal nothing."""
    mod = _issuance()
    _open_company(db_path, two["b"], [fx.SERVICE])
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as first:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["b"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-ind-1")
    finally:
        conn.close()
    fresh = str(uuid.uuid4())
    conn2 = _open(db_path)
    try:
        with pytest.raises(Exception) as second:
            mod.issue_envelope(
                conn2, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(fresh),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-ind-2")
    finally:
        conn2.close()
    assert first.value.args == second.value.args
    assert first.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    rows = fx.read_rows(db_path, "company", ["id", "name"])
    wanted = next(row for row in rows if row["id"] == two["b"])
    other_name = wanted["name"]
    for exc in (first.value, second.value):
        assert two["b"] not in str(exc)
        assert two["b"] not in repr(exc)
        assert other_name not in str(exc)
        assert other_name not in repr(exc)


def test_replay_unaffected(db_path, two, monkeypatch):
    """not qualification: replay returns stored envelope."""
    mod = _issuance()
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    conn = _open(db_path)
    try:
        first = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-replay-1")
    finally:
        conn.close()
    assert first["idempotent"] is False
    conn = _open(db_path)
    try:
        second = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-replay-1")
    finally:
        conn.close()
    assert second["idempotent"] is True
    assert second["authorization_id"] == first["authorization_id"]
    before_counts = _counts(db_path)
    fx.set_principal_disabled(db_path, fx.OWNER, two["info"]["now"])
    conn = _open(db_path)
    try:
        third = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-replay-1")
    finally:
        conn.close()
    assert third["idempotent"] is True
    assert third["authorization_id"] == first["authorization_id"]
    assert _counts(db_path) == before_counts
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["a"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-replay-2")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)


def test_two_company_principal_issues_for_either(
        db_path, two, monkeypatch):
    """not qualification: either allowed value issues."""
    mod = _issuance()
    _open_company(db_path, two["b"], [fx.SERVICE, fx.OWNER])
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    conn = _open(db_path)
    try:
        first = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"], name="Crate"),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-either-a")
    finally:
        conn.close()
    assert first["issued_route"] == "delegation"
    conn = _open(db_path)
    try:
        second = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["b"], name="Box"),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-either-b")
    finally:
        conn.close()
    assert second["issued_route"] == "delegation"
    assert _counts(db_path) == (2, 2)


@pytest.mark.parametrize("mode", ["raise", "bad"])
def test_evaluator_failure_refuses_at_active(db_path, two, monkeypatch, mode):
    """not qualification: evaluator faults refuse closed."""
    from erpclaw_lib import company_scope
    mod = _issuance()
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])
    fx.make_active(db_path)
    _active(monkeypatch)
    original = company_scope.principal_scope
    if mode == "raise":
        def _boom(conn, install_id, principal_id):
            raise RuntimeError("boom")
        monkeypatch.setattr(company_scope, "principal_scope", _boom)
    else:
        class _Bad(object):
            def __contains__(self, item):
                raise RuntimeError("boom")
        monkeypatch.setattr(
            company_scope, "principal_scope",
            lambda conn, install_id, principal_id: _Bad())
    handle = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                handle, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(two["a"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-eval-1")
    finally:
        pass
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    monkeypatch.setattr(company_scope, "principal_scope", original)
    try:
        out = mod.issue_envelope(
            handle, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["a"], name="Box"),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-eval-2")
    finally:
        handle.close()
    assert out["issued_route"] == "delegation"


def test_staged_and_exact_unchanged(db_path, two, monkeypatch):
    """not qualification: early phase skips scope reads."""
    from erpclaw_lib import company_scope
    mod = _issuance()
    _open_company(db_path, two["b"], [fx.SERVICE])
    _register(monkeypatch)
    _fix_clock(monkeypatch, two["info"]["now"])

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(company_scope, "principal_scope", _boom)
    monkeypatch.setattr(company_scope, "check", _boom)
    conn = _open(db_path)
    try:
        out = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=fx.ACTION,
            argv=fx.standard_argv(two["b"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-staged-1")
    finally:
        conn.close()
    assert out["issued_route"] == "staged_unattested"
    conn = _open(db_path)
    try:
        exact = mod.issue_exact_approval(
            conn, issuer_id=fx.OWNER, principal_id=fx.SERVICE,
            action=fx.ACTION, argv=fx.standard_argv(two["b"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="scope-staged-exact-1")
    finally:
        conn.close()
    assert exact["issued_route"] == "staged_unattested"
    fx.make_active(db_path)
    _active(monkeypatch)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_exact_approval(
                conn, issuer_id=fx.OWNER, principal_id=fx.SERVICE,
                action=fx.ACTION, argv=fx.standard_argv(two["b"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-active-exact-1")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUER_UNAVAILABLE,)


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(monkeypatch):
    """not qualification: pg probe runs inside issuance."""
    from test_authorization_gate import _pg_identity  # noqa: E402
    from test_authorization_gate import _pg_reset_schema  # noqa: E402
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(_PG_URL)
        _pg_reset_schema()
        init_all_tables(None)
        target = None
        first = fx.seed_company(target)
        second = fx.seed_company(target, name="Other Co", abbr="OC")
        info = fx.seed_authority(target, first)
        _open_company(target, second, [fx.SERVICE])
        _register(monkeypatch)
        _fix_clock(monkeypatch, info["now"])
        fx.make_active(target)
        _active(monkeypatch)
        mod = _issuance()
        handle = get_connection(target)
        try:
            before_auth = fx.read_rows(
                target, "operation_authorization", ["id"])
            before_side = fx.read_rows(
                target, "operation_authorization_envelope",
                ["authorization_id"])
            before_usage = fx.read_rows(
                target, "authority_delegation_usage", ["used"])
            with pytest.raises(Exception) as excinfo:
                mod.issue_envelope(
                    handle, principal_id=fx.SERVICE,
                    delegation_id=fx.DELEGATION, action=fx.ACTION,
                    argv=fx.standard_argv(second),
                    reason_code="ops-need", reason_text="need units",
                    idempotency_key="scope-pg-1")
            assert excinfo.value.args == (
                mod.AUTHORIZATION_ISSUANCE_REFUSED,)
            assert fx.read_rows(
                target, "operation_authorization", ["id"]) == before_auth
            assert fx.read_rows(
                target, "operation_authorization_envelope",
                ["authorization_id"]) == before_side
            assert fx.read_rows(
                target, "authority_delegation_usage",
                ["used"]) == before_usage
            out = mod.issue_envelope(
                handle, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(first),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="scope-pg-2")
            assert out["issued_route"] == "delegation"
        finally:
            handle.close()
    finally:
        seam.dispose_engines()
