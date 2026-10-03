"""Verify, consume and replay of single-use authorization envelopes."""
import json
import multiprocessing
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402
import authority_fixtures as fx  # noqa: E402
from setup_helpers import (  # noqa: E402
    call_action, freeze_snapshot, init_all_tables, load_db_query, ns,
    open_reader, read_all)
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402

try:
    _CTX = multiprocessing.get_context("spawn")
except ValueError:
    _CTX = multiprocessing.get_default_context()

AUTH_COLS = ["id", "install_id", "principal_id", "action",
             "binding_digest", "delegation_id", "issued_at",
             "expires_at", "revoked_at", "consumed_at", "consumed_txn"]
RESULT_COLS = ["authorization_id", "consumed_txn", "result_kind",
               "result_id", "result_status", "recorded_at"]

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


@pytest.fixture
def seeded(db_path):
    company_id = fx.seed_company(db_path)
    return fx.seed_authority(db_path, company_id)


def _gate():
    from erpclaw_lib import authority_gate
    return authority_gate


def _issuance():
    from erpclaw_lib import authorization_issuance
    return authorization_issuance


def _setup(monkeypatch, fixed):
    gate = _gate()
    monkeypatch.setitem(
        gate.ENVELOPE_ACTIONS, "add-uom", fx.make_declaration())
    if fixed is not None:
        from erpclaw_lib import authority_clock
        monkeypatch.setattr(
            authority_clock, "now_ms", lambda: fixed)
    return gate


def _issue(target, info, key="key-1", amount="60.00", name="Crate",
           action="add-uom"):
    mod = _issuance()
    conn = get_connection(target)
    try:
        return mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=action,
            argv=fx.standard_argv(
                info["company_id"], amount=amount, name=name),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=key)
    finally:
        conn.close()


def _verify(target, auth_id, info, name="Crate", amount="60.00"):
    gate = _gate()
    conn = get_connection(target)
    try:
        return gate.verify_and_consume(
            conn, authorization_id=auth_id, action="add-uom",
            argv=fx.standard_argv(
                info["company_id"], amount=amount, name=name),
            handler=fx.make_handler(name=name))
    finally:
        conn.close()


def _auth_row(target, auth_id):
    rows = fx.read_rows(target, "operation_authorization", AUTH_COLS)
    return next(row for row in rows if row["id"] == auth_id)


def _result_row(target, auth_id):
    found = fx.read_rows(
        target, "operation_authorization_result", RESULT_COLS)
    return next(
        (row for row in found
         if row["authorization_id"] == auth_id), None)


def _usage(target):
    rows = fx.read_rows(
        target, "authority_delegation_usage", ["used"])
    return sorted(row["used"] for row in rows)


def _uom_names(target):
    return sorted(
        row["name"]
        for row in fx.read_rows(target, "uom", ["id", "name"]))


def _fresh_target(tmp_path, tag):
    path = str(tmp_path / ("gate-%s.sqlite" % tag))
    init_all_tables(path)
    company_id = fx.seed_company(path)
    return path, fx.seed_authority(path, company_id)


def test_consume_at_staged(db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    payload = _verify(db_path, auth_id, seeded)
    assert payload["status"] == "ok"
    assert "uom_id" in payload
    assert _uom_names(db_path) == ["Crate"]
    stored = _auth_row(db_path, auth_id)
    assert stored["consumed_at"] is not None
    result = _result_row(db_path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == (
        "uom", payload["uom_id"], "created")
    assert stored["consumed_txn"] == result["consumed_txn"]
    assert _usage(db_path) == ["60.00"]
    reader = open_reader(db_path)
    try:
        rows = [row for row in read_all(
            reader, "audit_log",
            ["action", "authorization_id", "authorization_status"])
            if row["authorization_id"] == auth_id]
    finally:
        reader.close()
    assert len(rows) == 1
    assert rows[0]["authorization_status"] == "staged_unattested"


def test_unknown_and_expired(db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id="auth-never-issued",
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    from erpclaw_lib import authority_clock
    monkeypatch.setattr(
        authority_clock, "now_ms", lambda: out["expires_at"])
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    stored = _auth_row(db_path, auth_id)
    issued_at = stored["issued_at"]
    monkeypatch.setattr(
        authority_clock, "now_ms", lambda: issued_at - 1)
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _auth_row(db_path, auth_id)["consumed_at"] is None
    assert _result_row(db_path, auth_id) is None
    assert _uom_names(db_path) == []
    assert _usage(db_path) == ["0.00"]


@pytest.mark.parametrize("case", ["revoked", "right", "per-op"])
def test_live_rechecks(tmp_path, monkeypatch, case):
    target, info = _fresh_target(tmp_path, case)
    gate = _setup(monkeypatch, info["now"])
    out = _issue(target, info)
    if case == "revoked":
        fx.set_delegation_revoked(target, fx.DELEGATION, info["now"])
    elif case == "right":
        fx.delete_principal_rights(target, fx.SERVICE)
    else:
        fx.set_cap_per_operation(
            target, fx.DELEGATION, fx.ACTION, fx.CURRENCY, "50.00")
    conn = get_connection(target)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=out["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(info["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _auth_row(target, out["authorization_id"])[
        "consumed_at"] is None
    assert _result_row(target, out["authorization_id"]) is None
    assert _uom_names(target) == []


def _issue_exact(target, info, key="exact-1", action="add-uom"):
    mod = _issuance()
    conn = get_connection(target)
    try:
        return mod.issue_exact_approval(
            conn, issuer_id=fx.OWNER, principal_id=fx.SERVICE,
            action=action,
            argv=fx.standard_argv(info["company_id"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=key)
    finally:
        conn.close()


def test_exact_approval_consumes_while_rights_hold(
        db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    before_usage = _usage(db_path)
    out = _issue_exact(db_path, seeded)
    auth_id = out["authorization_id"]
    payload = _verify(db_path, auth_id, seeded)
    assert payload["status"] == "ok"
    assert _uom_names(db_path) == ["Crate"]
    assert _usage(db_path) == before_usage
    stored = _auth_row(db_path, auth_id)
    assert stored["consumed_at"] is not None
    result = _result_row(db_path, auth_id)
    assert stored["consumed_txn"] == result["consumed_txn"]


@pytest.mark.parametrize("case", ["right", "disabled"])
def test_exact_approval_refuses_without_live_rights(
        db_path, seeded, monkeypatch, case):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue_exact(db_path, seeded, key="exact-" + case)
    auth_id = out["authorization_id"]
    if case == "right":
        fx.delete_principal_rights(db_path, fx.SERVICE)
    else:
        fx.set_principal_disabled(
            db_path, fx.SERVICE, seeded["now"])
    reader = open_reader(db_path)
    try:
        before = freeze_snapshot(
            reader, db_path, seam.table_names(db_path))
    finally:
        reader.close()
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id,
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _auth_row(db_path, auth_id)["consumed_at"] is None
    assert _result_row(db_path, auth_id) is None
    check = get_connection(db_path)
    try:
        after = freeze_snapshot(
            check, db_path, seam.table_names(db_path))
    finally:
        check.close()
    assert before == after


def test_changed_binding_same_code(db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    company_id = seeded["company_id"]
    other = fx.seed_company(db_path)

    def _refuse(argv):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom", argv=argv,
                    handler=fx.make_handler())
        finally:
            conn.close()
        assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)

    _refuse(fx.standard_argv(company_id, amount="60.01"))
    rows = fx.read_rows(db_path, "company", ["id", "name"])
    before = next(
        row["name"] for row in rows if row["id"] == company_id)
    fx.rename_company(db_path, company_id, "Renamed Co")
    _refuse(fx.standard_argv(company_id))
    fx.rename_company(db_path, company_id, before)
    _refuse(fx.standard_argv(other))
    _refuse(fx.standard_argv(company_id) + ["--note", "x"])
    payload = _verify(db_path, auth_id, seeded)
    assert payload["status"] == "ok"


def test_aggregate_cap_refusal_keeps_envelope(
        db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    first = _issue(db_path, seeded, key="agg-a", name="Crate")
    second = _issue(db_path, seeded, key="agg-b", name="Box")
    payload = _verify(db_path, first["authorization_id"], seeded)
    assert payload["status"] == "ok"
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=second["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(
                    seeded["company_id"], name="Box"),
                handler=fx.make_handler(name="Box"))
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _auth_row(
        db_path, second["authorization_id"])["consumed_at"] is None
    assert _uom_names(db_path) == ["Crate"]
    assert _usage(db_path) == ["60.00"]
    fx.set_cap_aggregate(
        db_path, fx.DELEGATION, fx.ACTION, fx.CURRENCY, "200.00")
    again = _verify(
        db_path, second["authorization_id"], seeded, name="Box")
    assert again["status"] == "ok"
    assert _usage(db_path) == ["120.00"]


def _failing_handler(mode, db_path=None):
    mod = load_db_query()

    def _raise_after_insert(proxy):
        import uuid
        from erpclaw_lib.query import P, Q, Table
        table = Table("uom")
        query = Q.into(table).columns(
            "id", "name", "must_be_whole_number").insert(
            P(), P(), P()).get_sql()
        proxy.execute(
            query, (str(uuid.uuid4()), "Ghost", 0))
        raise RuntimeError("boom")

    if mode == "raise-after-insert":
        return _raise_after_insert
    if mode == "exit1":
        return lambda proxy: mod.add_uom(
            proxy, ns(name=None, must_be_whole_number=False))
    if mode == "double-commit":
        def _double(proxy):
            proxy.commit()
            proxy.commit()
        return _double
    if mode == "no-commit":
        return lambda proxy: None
    if mode == "context":
        def _ctx(proxy):
            with proxy:
                pass
        return _ctx
    raise AssertionError(mode)


@pytest.mark.parametrize("mode", [
    "raise-after-insert", "exit1", "double-commit", "no-commit",
    "context", "bad-result",
])
def test_handler_failures_roll_back(
        db_path, seeded, monkeypatch, capsys, mode):
    gate = _setup(monkeypatch, seeded["now"])
    if mode == "bad-result":
        monkeypatch.setitem(
            gate.ENVELOPE_ACTIONS, "add-uom",
            dict(fx.make_declaration(),
                 result=lambda payload: 1 / 0))
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    handler = (fx.make_handler()
               if mode == "bad-result"
               else _failing_handler(mode))
    conn = get_connection(db_path)
    try:
        if mode == "exit1":
            with pytest.raises(SystemExit) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=handler)
            assert excinfo.value.code == 1
        elif mode in ("double-commit", "no-commit", "context"):
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=handler)
            assert excinfo.value.args == (
                gate.HANDLER_COMMIT_INVALID,)
        elif mode == "bad-result":
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=handler)
            assert excinfo.value.args == (
                gate.AUTHORIZATION_REFUSED,)
        else:
            with pytest.raises(RuntimeError):
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=handler)
    finally:
        conn.close()
    assert _uom_names(db_path) == []
    assert _result_row(db_path, auth_id) is None
    assert _usage(db_path) == ["0.00"]
    assert _auth_row(db_path, auth_id)["consumed_at"] is None
    assert '"status": "ok"' not in capsys.readouterr().out
    if mode == "bad-result":
        monkeypatch.setitem(
            gate.ENVELOPE_ACTIONS, "add-uom",
            fx.make_declaration())
    payload = _verify(db_path, auth_id, seeded)
    assert payload["status"] == "ok"


def test_replay_zero_writes(db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    payload = _verify(db_path, auth_id, seeded)
    reader = open_reader(db_path)
    try:
        before = freeze_snapshot(
            reader, db_path, seam.table_names(db_path))
    finally:
        reader.close()
    conn = get_connection(db_path)
    try:
        again = gate.verify_and_consume(
            conn, authorization_id=auth_id, action="add-uom",
            argv=fx.standard_argv(seeded["company_id"]),
            handler=fx.make_handler())
    finally:
        conn.close()
    assert again == {
        "status": "ok", "replayed": True,
        "authorization_id": auth_id, "result_kind": "uom",
        "result_id": payload["uom_id"],
        "result_status": "created"}
    check = get_connection(db_path)
    try:
        after = freeze_snapshot(
            check, db_path, seam.table_names(db_path))
    finally:
        check.close()
    assert before == after
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.replay(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(
                    seeded["company_id"], amount="60.01"))
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    fx.delete_principal_rights(db_path, fx.SERVICE)
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.replay(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]))
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)


def test_tampered_sidecar(db_path, seeded, monkeypatch, caplog):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    from erpclaw_lib.query import Field, P, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table("operation_authorization_envelope")
        query = Q.update(table).set(
            Field("reason_code"), P()).where(
            Field("authorization_id") == P()).get_sql()
        conn.execute(query, ("tampered-code", auth_id))
        conn.commit()
    finally:
        conn.close()
    import logging
    with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=fx.make_handler())
        finally:
            conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _auth_row(db_path, auth_id)["consumed_at"] is None
    hits = [record for record in caplog.records
            if record.name == "erpclaw.authority"
            and record.levelname == "WARNING"]
    assert len(hits) == 1
    assert auth_id in hits[0].getMessage()
    assert "need units" not in hits[0].getMessage()


def test_spent_envelope_tampering_warns(
        db_path, seeded, monkeypatch, caplog):
    import logging
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded)
    auth_id = out["authorization_id"]
    payload = _verify(db_path, auth_id, seeded)
    assert payload["status"] == "ok"
    from erpclaw_lib.query import Field, P, Q, Table
    conn = get_connection(db_path)
    try:
        table = Table("operation_authorization_envelope")
        query = Q.update(table).set(
            Field("reason_code"), P()).where(
            Field("authorization_id") == P()).get_sql()
        conn.execute(query, ("tampered-code", auth_id))
        conn.commit()
    finally:
        conn.close()
    reader = open_reader(db_path)
    try:
        before = freeze_snapshot(
            reader, db_path, seam.table_names(db_path))
    finally:
        reader.close()
    base = len(caplog.records)
    with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=fx.make_handler())
        finally:
            conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    hits = [record for record in caplog.records[base:]
            if record.name == "erpclaw.authority"
            and record.levelname == "WARNING"]
    assert len(hits) == 1
    assert auth_id in hits[0].getMessage()
    assert "need units" not in hits[0].getMessage()
    base = len(caplog.records)
    with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.replay(
                    conn, authorization_id=auth_id,
                    action="add-uom",
                    argv=fx.standard_argv(seeded["company_id"]))
        finally:
            conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    hits = [record for record in caplog.records[base:]
            if record.name == "erpclaw.authority"
            and record.levelname == "WARNING"]
    assert len(hits) == 1
    assert auth_id in hits[0].getMessage()
    assert "need units" not in hits[0].getMessage()
    assert _auth_row(db_path, auth_id)["consumed_at"] is not None
    check = get_connection(db_path)
    try:
        after = freeze_snapshot(
            check, db_path, seam.table_names(db_path))
    finally:
        check.close()
    assert before == after


def test_active_phase(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    gate = _setup(monkeypatch, seeded["now"])
    staged = _issue(db_path, seeded, key="staged-1")
    fx.make_active(db_path)
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=staged["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORITY_NOT_READY,)
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import actor
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=staged["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    monkeypatch.setattr(
        actor, "current",
        lambda: actor.ActorContext(
            None, None, None, (), actor.CLAIMED))
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=staged["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    monkeypatch.setattr(
        gate, "install_phase", lambda conn: ("ACTIVE", "other-install"))
    monkeypatch.setattr(
        actor, "current",
        lambda: actor.ActorContext(
            None, None, fx.SERVICE, (), actor.ATTESTED))
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=staged["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    monkeypatch.undo()
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(
        actor, "current",
        lambda: actor.ActorContext(
            None, None, fx.SERVICE, (), actor.ATTESTED))
    monkeypatch.setitem(
        gate.ENVELOPE_ACTIONS, "add-uom", fx.make_declaration())
    from erpclaw_lib import authority_clock
    monkeypatch.setattr(
        authority_clock, "now_ms", lambda: seeded["now"])
    mod = _issuance()
    conn = get_connection(db_path)
    try:
        live = mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action="add-uom",
            argv=fx.standard_argv(seeded["company_id"]),
            reason_code="ops-need", reason_text="need units",
            idempotency_key="live-1")
    finally:
        conn.close()
    assert live["issued_route"] == "delegation"
    conn = get_connection(db_path)
    try:
        payload = gate.verify_and_consume(
            conn, authorization_id=live["authorization_id"],
            action="add-uom",
            argv=fx.standard_argv(seeded["company_id"]),
            handler=fx.make_handler())
    finally:
        conn.close()
    assert payload["status"] == "ok"
    reader = open_reader(db_path)
    try:
        rows = [row for row in read_all(
            reader, "audit_log",
            ["authorization_id", "authorization_status"])
            if row["authorization_id"] == live["authorization_id"]]
    finally:
        reader.close()
    assert [row["authorization_status"] for row in rows] == ["verified"]


@pytest.mark.parametrize("message", ["disk I/O error",
                               "no such column: phase"])
def test_install_read_error_refuses_not_staged(
        db_path, seeded, monkeypatch, message):
    import sqlite3 as _sqlite3
    from erpclaw_lib import db as _db
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded, key="classify-1")
    auth_id = out["authorization_id"]
    fx.make_active(db_path)
    reader = open_reader(db_path)
    try:
        before = freeze_snapshot(
            reader, db_path, seam.table_names(db_path))
    finally:
        reader.close()
    def _patched(self, statement, params=None):
        text = statement if isinstance(statement, str) else ""
        if "phase" in text and "authority_install" in text:
            raise _sqlite3.OperationalError(message)
        raw = object.__getattribute__(self, "_conn")
        if params is None:
            return raw.execute(statement)
        return raw.execute(statement, params)

    monkeypatch.setattr(
        _db.ConnectionWrapper, "execute", _patched, raising=False)
    conn = get_connection(db_path)
    try:
        with pytest.raises(gate.AuthorityRefusal) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(seeded["company_id"]),
                handler=fx.make_handler())
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORITY_NOT_READY,)
    assert _auth_row(db_path, auth_id)["consumed_at"] is None
    assert _result_row(db_path, auth_id) is None
    mod = load_db_query()
    for fn, args in (
            (mod.issue_authorization, ns()),
            (mod.revoke_authorization, ns(envelope_id=auth_id)),
            (mod.get_authorization, ns(envelope_id=auth_id))):
        conn = get_connection(db_path)
        try:
            with pytest.raises(gate.AuthorityRefusal) as excinfo:
                call_action(fn, conn, args)
        finally:
            conn.close()
        assert excinfo.value.args == (gate.AUTHORITY_NOT_READY,)
    monkeypatch.delattr(_db.ConnectionWrapper, "execute", raising=False)
    check = get_connection(db_path)
    try:
        after = freeze_snapshot(
            check, db_path, seam.table_names(db_path))
    finally:
        check.close()
    assert before == after


def _race_worker(tests_dir, target, auth_id, argv, name, ready, go,
                 out, fixed_ms=None):
    try:
        import sys as _sys
        if tests_dir not in _sys.path:
            _sys.path.insert(0, tests_dir)
        from erpclaw_lib import authority_clock as _clock
        if fixed_ms is not None:
            _clock.now_ms = lambda: fixed_ms
        from erpclaw_lib.db import get_connection as _connect
        from erpclaw_lib import authority_gate as _gate
        import authority_fixtures as _fx
        from setup_helpers import load_db_query as _load
        import setup_helpers as _helpers
        _gate.ENVELOPE_ACTIONS["add-uom"] = _fx.make_declaration()
        _mod = _load()
        conn = _connect(target)
        ready.set()
        if not go.wait(timeout=300):
            out.put(("error", "no-go"))
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            def _handler(proxy):
                _mod.add_uom(
                    proxy,
                    _helpers.ns(name=name,
                                must_be_whole_number=False))
            payload = _gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=argv, handler=_handler)
            if payload.get("replayed") is True:
                out.put(("replayed", payload.get("result_id")))
            else:
                out.put(("ok", payload.get("uom_id")))
        except Exception as exc:
            out.put(("refused",
                     exc.args[0] if exc.args else type(exc).__name__))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except BaseException as exc:
        try:
            ready.set()
        except Exception:
            pass
        try:
            out.put(("error", repr(exc)))
        except Exception:
            pass


def _start(proc):
    proc.start()
    return proc


def _reap(procs):
    for proc in procs:
        if proc.is_alive():
            try:
                proc.terminate()
            except Exception:
                pass
    for proc in procs:
        try:
            proc.join(timeout=10)
        except Exception:
            pass
        try:
            proc.close()
        except Exception:
            pass


def _run_workers(target, jobs, fixed_ms=None):
    saved_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = _TESTS_DIR + (
        os.pathsep + saved_path if saved_path else "")
    go = _CTX.Event()
    out = _CTX.Queue()
    procs = []
    readys = []
    for auth_id, argv, name in jobs:
        ready = _CTX.Event()
        readys.append(ready)
        procs.append(_CTX.Process(
            target=_race_worker,
            args=(_TESTS_DIR, target, auth_id, argv, name, ready,
                  go, out, fixed_ms)))
    try:
        for proc in procs:
            _start(proc)
        for ready in readys:
            if not ready.wait(timeout=120):
                drained = []
                while True:
                    try:
                        drained.append(out.get_nowait())
                    except Exception:
                        break
                states = [(proc.is_alive(), proc.exitcode)
                          for proc in procs]
                raise AssertionError(
                    "worker readiness timeout: %r alive/exitcode %r"
                    % (drained, states))
        go.set()
        for proc in procs:
            proc.join(timeout=120)
        if any(proc.is_alive() for proc in procs):
            raise AssertionError("worker overrun")
        return [out.get(timeout=60) for _ in procs]
    finally:
        _reap(procs)
        if saved_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = saved_path


def test_race_one_envelope(db_path, seeded, monkeypatch):
    _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded, key="race-1")
    auth_id = out["authorization_id"]
    argv = fx.standard_argv(seeded["company_id"])
    got = _run_workers(
        db_path, [(auth_id, argv, "Crate"),
                  (auth_id, argv, "Crate")])
    kinds = sorted(kind for kind, _ in got)
    assert kinds in (["ok", "refused"], ["ok", "replayed"]), got
    oks = [value for kind, value in got if kind == "ok"]
    reps = [value for kind, value in got if kind == "replayed"]
    assert len(oks) == 1
    if reps:
        assert reps[0] == oks[0]
    loser = next(entry for entry in got if entry[0] != "ok")
    assert loser in (("refused", "AUTHORIZATION_REFUSED"),
                     ("replayed", oks[0])), loser
    assert _uom_names(db_path) == ["Crate"]
    assert _usage(db_path) == ["60.00"]
    results = [row for row in fx.read_rows(
        db_path, "operation_authorization_result",
        ["authorization_id", "consumed_txn"])
        if row["authorization_id"] == auth_id]
    assert len(results) == 1
    reader = open_reader(db_path)
    try:
        hits = [row for row in read_all(
            reader, "audit_log",
            ["action", "authorization_id", "authorization_status"])
            if row["authorization_id"] == auth_id]
    finally:
        reader.close()
    assert len(hits) == 1
    stored = _auth_row(db_path, auth_id)
    assert stored["consumed_at"] is not None
    assert stored["consumed_txn"] == results[0]["consumed_txn"]


def test_race_aggregate_cap(db_path, seeded, monkeypatch):
    _setup(monkeypatch, seeded["now"])
    first = _issue(db_path, seeded, key="race-a", name="Crate")
    second = _issue(db_path, seeded, key="race-b", name="Box")
    first_id = first["authorization_id"]
    second_id = second["authorization_id"]
    got = _run_workers(db_path, [
        (first_id,
         fx.standard_argv(seeded["company_id"], name="Crate"),
         "Crate"),
        (second_id,
         fx.standard_argv(seeded["company_id"], name="Box"),
         "Box")])
    assert sorted(kind for kind, _ in got) == ["ok", "refused"], got
    loser = next(entry for entry in got if entry[0] != "ok")
    assert loser == ("refused", "AUTHORIZATION_REFUSED"), loser
    winner = [row for row in fx.read_rows(
        db_path, "operation_authorization",
        ["id", "consumed_at", "consumed_txn"])]
    consumed = [row for row in winner if row["consumed_at"] is not None]
    assert len(consumed) == 1
    assert _usage(db_path) == ["60.00"]
    assert _uom_names(db_path) in (["Box"], ["Crate"])
    results = fx.read_rows(
        db_path, "operation_authorization_result",
        ["authorization_id", "consumed_txn"])
    assert len(results) == 1
    assert results[0]["authorization_id"] == consumed[0]["id"]
    reader = open_reader(db_path)
    try:
        audits = [row for row in read_all(
            reader, "audit_log",
            ["action", "authorization_id", "authorization_status"])]
    finally:
        reader.close()
    winner_audits = [row for row in audits
                     if row["authorization_id"] == consumed[0]["id"]]
    assert len(winner_audits) == 1
    loser_id = (second_id if consumed[0]["id"] == first_id
                else first_id)
    assert [row for row in audits
            if row["authorization_id"] == loser_id] == []
    loser_row = next(row for row in winner if row["id"] == loser_id)
    assert loser_row["consumed_at"] is None
    assert loser_row["consumed_txn"] is None


def test_cap_compare_and_set_refuses_a_stale_read(
        db_path, seeded, monkeypatch):
    gate = _setup(monkeypatch, seeded["now"])
    first = _issue(db_path, seeded, key="stale-a", name="Crate")
    second = _issue(db_path, seeded, key="stale-b", name="Box")
    payload = _verify(db_path, first["authorization_id"], seeded)
    assert payload["status"] == "ok"
    assert _usage(db_path) == ["60.00"]
    monkeypatch.setattr(
        gate, "_read_used", lambda conn, key: "0.00")
    conn = get_connection(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=second["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(
                    seeded["company_id"], name="Box"),
                handler=fx.make_handler(name="Box"))
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
    assert _usage(db_path) == ["60.00"]
    assert _auth_row(
        db_path, second["authorization_id"])["consumed_at"] is None


def _pg_reset_schema():
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(None)
    conn = _gc()
    try:
        resolved_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError("pg host/port mismatch")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _pg_identity(url):
    from urllib.parse import urlparse
    from erpclaw_lib.db import get_connection as _gc
    conn = _gc()
    try:
        want_dir = os.environ.get("AUTHORITY_PGDATA_CHECK")
        data_dir = None
        if want_dir:
            data_dir = conn.execute(
                "SELECT current_setting('data_directory')").fetchone()[0]
            listen = conn.execute(
                "SELECT current_setting('listen_addresses')").fetchone()[0]
            assert data_dir == want_dir
            assert listen == ""
        server_addr = conn.execute(
            "SELECT inet_server_addr()").fetchone()[0]
        current_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
    finally:
        conn.close()
    assert server_addr is None
    assert current_db == urlparse(url).path.strip("/")
    return data_dir


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_issue_consume_replay_and_races(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(_PG_URL)
        gate = _setup(monkeypatch, None)
        target = None

        def _fresh():
            _pg_reset_schema()
            init_all_tables(None)
            company_id = fx.seed_company(target)
            info = fx.seed_authority(target, company_id)
            assert info["now"] > 2_147_483_647
            return info

        info = _fresh()
        first = _issue(target, info, key="pg-1")
        payload = _verify(target, first["authorization_id"], info)
        assert payload["status"] == "ok"
        assert _usage(target) == ["60.00"]

        info = _fresh()
        agg_a = _issue(target, info, key="pg-a", name="Crate")
        agg_b = _issue(target, info, key="pg-b", name="Box")
        _verify(target, agg_a["authorization_id"], info)
        conn = get_connection(target)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=agg_b["authorization_id"],
                    action="add-uom",
                    argv=fx.standard_argv(
                        info["company_id"], name="Box"),
                    handler=fx.make_handler(name="Box"))
        finally:
            conn.close()
        assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
        assert _usage(target) == ["60.00"]

        info = _fresh()
        once = _issue(target, info, key="pg-once")
        body = _verify(target, once["authorization_id"], info)
        assert body["status"] == "ok"
        reader = open_reader(target)
        try:
            before = freeze_snapshot(
                reader, target, seam.table_names(target))
        finally:
            reader.close()
        conn = get_connection(target)
        try:
            again = gate.verify_and_consume(
                conn, authorization_id=once["authorization_id"],
                action="add-uom",
                argv=fx.standard_argv(info["company_id"]),
                handler=fx.make_handler())
        finally:
            conn.close()
        assert again["replayed"] is True
        assert again["result_id"] == body["uom_id"]
        check = get_connection(target)
        try:
            after = freeze_snapshot(
                check, target, seam.table_names(target))
        finally:
            check.close()
        assert before == after

        info = _fresh()
        third = _issue(target, info, key="pg-3", name="Crate")
        third_id = third["authorization_id"]
        got = _run_workers(target, [
            (third_id,
             fx.standard_argv(info["company_id"], name="Crate"),
             "Crate"),
            (third_id,
             fx.standard_argv(info["company_id"], name="Crate"),
             "Crate")], fixed_ms=None)
        kinds = sorted(kind for kind, _ in got)
        assert kinds in (["ok", "refused"], ["ok", "replayed"]), got
        oks = [value for kind, value in got if kind == "ok"]
        reps = [value for kind, value in got if kind == "replayed"]
        assert len(oks) == 1
        if reps:
            assert reps[0] == oks[0]
        loser = next(entry for entry in got if entry[0] != "ok")
        assert loser in (("refused", "AUTHORIZATION_REFUSED"),
                         ("replayed", oks[0])), loser
        assert _uom_names(target) == ["Crate"]
        assert _usage(target) == ["60.00"]
        pg_results = [row for row in fx.read_rows(
            target, "operation_authorization_result",
            ["authorization_id", "consumed_txn"])
            if row["authorization_id"] == third_id]
        assert len(pg_results) == 1
        pg_stored = _auth_row(target, third_id)
        assert pg_stored["consumed_at"] is not None
        assert pg_stored["consumed_txn"] == pg_results[0]["consumed_txn"]

        info = _fresh()
        pg_first = _issue(target, info, key="pg-agg-a", name="Crate")
        pg_second = _issue(target, info, key="pg-agg-b", name="Box")
        pg_first_id = pg_first["authorization_id"]
        pg_second_id = pg_second["authorization_id"]
        got = _run_workers(target, [
            (pg_first_id,
             fx.standard_argv(info["company_id"], name="Crate"),
             "Crate"),
            (pg_second_id,
             fx.standard_argv(info["company_id"], name="Box"),
             "Box")], fixed_ms=None)
        assert sorted(kind for kind, _ in got) == ["ok", "refused"], got
        pg_loser = next(entry for entry in got if entry[0] != "ok")
        assert pg_loser == ("refused", "AUTHORIZATION_REFUSED"), pg_loser
        pg_winner = [row for row in fx.read_rows(
            target, "operation_authorization",
            ["id", "consumed_at", "consumed_txn"])]
        pg_consumed = [row for row in pg_winner
                       if row["consumed_at"] is not None]
        assert len(pg_consumed) == 1
        assert _usage(target) == ["60.00"]
        assert _uom_names(target) in (["Box"], ["Crate"])
        pg_agg_results = fx.read_rows(
            target, "operation_authorization_result",
            ["authorization_id", "consumed_txn"])
        assert len(pg_agg_results) == 1
        assert (pg_agg_results[0]["authorization_id"]
                == pg_consumed[0]["id"])
        pg_reader = open_reader(target)
        try:
            pg_audits = [row for row in read_all(
                pg_reader, "audit_log",
                ["action", "authorization_id",
                 "authorization_status"])]
        finally:
            pg_reader.close()
        pg_winner_audits = [row for row in pg_audits
                            if row["authorization_id"]
                            == pg_consumed[0]["id"]]
        assert len(pg_winner_audits) == 1
        pg_loser_id = (pg_second_id
                       if pg_consumed[0]["id"] == pg_first_id
                       else pg_first_id)
        assert [row for row in pg_audits
                if row["authorization_id"] == pg_loser_id] == []
        pg_loser_row = next(
            row for row in pg_winner if row["id"] == pg_loser_id)
        assert pg_loser_row["consumed_at"] is None
        assert pg_loser_row["consumed_txn"] is None

        info = _fresh()
        fifth = _issue(target, info, key="pg-5", name="S1")
        sixth = _issue(target, info, key="pg-6", name="S2")
        payload5 = _verify(
            target, fifth["authorization_id"], info, name="S1")
        assert payload5["status"] == "ok"
        real_read_used = gate._read_used
        monkeypatch.setattr(
            gate, "_read_used", lambda conn, key: "0.00")
        conn = get_connection(target)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id=sixth["authorization_id"],
                    action="add-uom",
                    argv=fx.standard_argv(
                        info["company_id"], name="S2"),
                    handler=fx.make_handler(name="S2"))
        finally:
            conn.close()
        assert excinfo.value.args == (gate.AUTHORIZATION_REFUSED,)
        assert _usage(target) == ["60.00"]
        monkeypatch.setattr(gate, "_read_used", real_read_used)

        info = _fresh()
        issuance = _issuance()
        side_cols = ["authorization_id", "install_id",
                     "principal_id", "envelope_version",
                     "args_digest", "issuer_id", "issued_route",
                     "reason_code", "reason_text",
                     "idempotency_key", "call_id",
                     "envelope_digest"]
        before_auth = fx.read_rows(
            target, "operation_authorization", AUTH_COLS)
        before_side = fx.read_rows(
            target, "operation_authorization_envelope", side_cols)
        before_usage = fx.read_rows(
            target, "authority_delegation_usage", ["used"])
        conn = get_connection(target)
        try:
            raw = object.__getattribute__(conn, "_conn")
            raw.autocommit = True
            with pytest.raises(ValueError) as excinfo:
                issuance.issue_envelope(
                    conn, principal_id=fx.SERVICE,
                    delegation_id=fx.DELEGATION, action="add-uom",
                    argv=fx.standard_argv(info["company_id"]),
                    reason_code="ops-need",
                    reason_text="need units",
                    idempotency_key="pg-autocommit")
        finally:
            conn.close()
        assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
        assert fx.read_rows(
            target, "operation_authorization",
            AUTH_COLS) == before_auth
        assert fx.read_rows(
            target, "operation_authorization_envelope",
            side_cols) == before_side
        assert fx.read_rows(
            target, "authority_delegation_usage",
            ["used"]) == before_usage
    finally:
        seam.dispose_engines()


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_missing_install_table_leaves_transaction_usable(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(_PG_URL)
        gate = _setup(monkeypatch, None)
        target = None
        _pg_reset_schema()
        init_all_tables(None)
        company_id = fx.seed_company(target)
        info = fx.seed_authority(target, company_id)
        assert info["now"] > 2_147_483_647
        conn = get_connection(target)
        try:
            conn.execute("DROP TABLE authority_install CASCADE")
            conn.commit()
        finally:
            conn.close()
        probe = get_connection(target)
        try:
            assert gate.install_phase(probe) == ("STAGED", None)
            rows = read_all(probe, "authority_principal", ["id"])
            assert sorted(row["id"] for row in rows) == sorted(
                [fx.SERVICE, fx.OWNER, fx.OTHER_SERVICE])
        finally:
            probe.close()
        issuance = _issuance()
        handle = get_connection(target)
        try:
            with pytest.raises(Exception) as excinfo:
                issuance.issue_envelope(
                    handle, principal_id=fx.SERVICE,
                    delegation_id=fx.DELEGATION, action="add-uom",
                    argv=fx.standard_argv(info["company_id"]),
                    reason_code="ops-need", reason_text="need units",
                    idempotency_key="pg-no-install")
        finally:
            handle.close()
        assert excinfo.value.args == (
            issuance.AUTHORIZATION_ISSUANCE_REFUSED,)
    finally:
        seam.dispose_engines()
