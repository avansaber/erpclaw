"""ACTIVE ledger writes pass only inside their own gate consumption."""
import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
_SETUP_TESTS = os.path.join(os.path.dirname(_TESTS_DIR), "erpclaw-setup", "tests")
if _SETUP_TESTS not in sys.path:
    sys.path.insert(0, _SETUP_TESTS)

import journals_helpers as helpers  # noqa: E402
import test_journal_envelope_gate as gate_tests  # noqa: E402
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Q, ValueWrapper  # noqa: E402

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


def _build(tmp_path, monkeypatch, tag):
    path = gate_tests._fresh_db(tmp_path, monkeypatch, tag)
    handle = get_connection(path)
    try:
        env = gate_tests._env(handle)
    finally:
        handle.close()
    return path, env


def _select_one(handle):
    row = handle.execute(Q.select(ValueWrapper(1)).get_sql()).fetchone()
    assert row[0] == 1


def _spy_calls(monkeypatch, module, name):
    real = getattr(module, name)
    calls = []

    def _spy(*args, **kwargs):
        outcome = real(*args, **kwargs)
        calls.append((args, kwargs, outcome))
        return outcome

    monkeypatch.setattr(module, name, _spy)
    return calls


def _active_issue(tmp_path, monkeypatch, tag, amount="100.00"):
    import authority_fixtures as fx
    path, env = _build(tmp_path, monkeypatch, tag)
    je = gate_tests._je(path, env, amount=amount)
    gate_tests._grant(path, env["company_id"], je, "submit-journal-entry")
    fx.make_active(path)
    gate_tests._patch_ready(monkeypatch)
    gate_tests._patch_actor(monkeypatch)
    issued = gate_tests._issue_full(
        path, "submit-journal-entry",
        gate_tests._std("submit-journal-entry", path, je))
    assert issued["issued_route"] == "delegation"
    return path, env, je, issued["authorization_id"]


def _gl_for(path, je):
    rows = gate_tests._read_all(
        path, "gl_entry", ["id", "voucher_id", "debit", "credit"])
    return sorted(
        (row for row in rows if row["voucher_id"] == je),
        key=lambda row: row["id"])


def _chain_head(path, company_id):
    rows = gate_tests._read_all(
        path, "gl_chain_head",
        ["company_id", "last_sequence", "last_checksum"])
    return next(
        (row for row in rows if row["company_id"] == company_id), None)


def _usage_used(path, action):
    rows = gate_tests._read_all(
        path, "authority_delegation_usage", ["action", "used"])
    return sorted(row["used"] for row in rows if row["action"] == action)


@pytest.mark.parametrize("via", ["direct", "router"])
def test_active_submit_posts(tmp_path, monkeypatch, via):
    """Enveloped submit posts its legs at ACTIVE; not qualification."""
    from erpclaw_lib import authority_sink
    path, env, je, auth_id = _active_issue(
        tmp_path, monkeypatch, "posts-%s" % via)
    head_before = _chain_head(path, env["company_id"])
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    unbind_calls = _spy_calls(monkeypatch, authority_sink, "unbind")
    clear_calls = _spy_calls(monkeypatch, authority_sink, "clear")
    if via == "direct":
        code, payload = gate_tests._direct(
            gate_tests._std("submit-journal-entry", path, je,
                            ["--authorization-id", auth_id]))
    else:
        code, payload = gate_tests._via_router(
            gate_tests._std("submit-journal-entry", path, je,
                            ["--user-confirmed", "--authorization-id",
                             auth_id]),
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    legs = _gl_for(path, je)
    assert len(legs) == 2
    assert sorted(row["debit"] for row in legs) == ["0.00", "100.00"]
    assert sorted(row["credit"] for row in legs) == ["0.00", "100.00"]
    head_after = _chain_head(path, env["company_id"])
    assert head_after is not None
    assert (head_before is None
            or head_after["last_sequence"] > head_before["last_sequence"])
    auth = gate_tests._read_one(
        path, "operation_authorization",
        ["id", "consumed_at", "consumed_txn"], auth_id)
    assert auth["consumed_at"] is not None
    result = gate_tests._result_row(path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("journal-entry", je, "submitted")
    audits = [row for row in gate_tests._read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == auth_id]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    assert "100.00" in _usage_used(path, "submit-journal-entry")
    assert len(bind_calls) == 1
    txn = bind_calls[0][0][1]
    assert txn.authorization_id == auth_id
    assert txn.action == "submit-journal-entry"
    assert txn.label == "verified"
    assert txn.txn_id == auth["consumed_txn"]
    assert len(unbind_calls) == 1
    assert unbind_calls[0][0][1] == bind_calls[0][2]
    assert clear_calls == []


def test_staged_unattested_envelope_refused_at_active(tmp_path, monkeypatch):
    """A STAGED envelope cannot spend at ACTIVE; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import authority_sink
    path, env = _build(tmp_path, monkeypatch, "staged-spend")
    je = gate_tests._je(path, env)
    gate_tests._grant(path, env["company_id"], je, "submit-journal-entry")
    staged_id = gate_tests._issue(
        path, "submit-journal-entry",
        gate_tests._std("submit-journal-entry", path, je))
    fx.make_active(path)
    gate_tests._patch_ready(monkeypatch)
    gate_tests._patch_actor(monkeypatch)
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    before = gate_tests._snapshot(path)
    code, payload = gate_tests._direct(
        gate_tests._std("submit-journal-entry", path, je,
                        ["--authorization-id", staged_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    assert gate_tests._snapshot(path) == before
    assert bind_calls == []
    auth = gate_tests._read_one(
        path, "operation_authorization", ["id", "consumed_at"], staged_id)
    assert auth["consumed_at"] is None
    second, env2 = _build(tmp_path, monkeypatch, "staged-control")
    je2 = gate_tests._je(second, env2)
    gate_tests._grant(second, env2["company_id"], je2, "submit-journal-entry")
    staged2 = gate_tests._issue(
        second, "submit-journal-entry",
        gate_tests._std("submit-journal-entry", second, je2))
    code2, payload2 = gate_tests._direct(
        gate_tests._std("submit-journal-entry", second, je2,
                        ["--authorization-id", staged2]))
    assert code2 == 0, payload2
    assert payload2.get("document_status") == "submitted"
    assert len(_gl_for(second, je2)) == 2


def test_readiness_lost_after_bind_rolls_back(tmp_path, monkeypatch):
    """Losing readiness after spend refuses and unwinds; not qualification.

    The direct submit in test_active_submit_posts is the control with
    readiness kept, so no second STAGED run is needed here.
    """
    import authority_fixtures as fx
    from erpclaw_lib import authority_gate
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import authority_sink
    path, env = _build(tmp_path, monkeypatch, "readiness-lost")
    je = gate_tests._je(path, env)
    gate_tests._grant(path, env["company_id"], je, "submit-journal-entry")
    fx.make_active(path)
    gate_tests._patch_ready(monkeypatch)
    gate_tests._patch_actor(monkeypatch)
    issued = gate_tests._issue_full(
        path, "submit-journal-entry",
        gate_tests._std("submit-journal-entry", path, je))
    assert issued["issued_route"] == "delegation"
    auth_id = issued["authorization_id"]
    spent = {"done": False}
    real_consume = authority_gate.consume

    def _spy_consume(conn_arg, **kwargs):
        try:
            return real_consume(conn_arg, **kwargs)
        finally:
            spent["done"] = True

    monkeypatch.setattr(authority_gate, "consume", _spy_consume)
    monkeypatch.setattr(
        authority_readiness, "is_ready",
        lambda conn_arg: not spent["done"])
    mod = helpers.load_db_query()
    argv = gate_tests._std("submit-journal-entry", path, je)

    def _handler(handle):
        mod.submit_journal_entry(handle, helpers.ns(journal_entry_id=je))

    before = gate_tests._snapshot(path)
    head_before = _chain_head(path, env["company_id"])
    usage_before = _usage_used(path, "submit-journal-entry")
    conn = get_connection(path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
            authority_gate.verify_and_consume(
                conn, authorization_id=auth_id,
                action="submit-journal-entry", argv=list(argv),
                handler=_handler)
        assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
        assert authority_sink.current(conn) is None
        _select_one(conn)
    finally:
        conn.close()
    auth = gate_tests._read_one(
        path, "operation_authorization",
        ["id", "consumed_at", "consumed_txn"], auth_id)
    assert auth["consumed_at"] is None
    assert auth["consumed_txn"] is None
    entry = gate_tests._read_one(
        path, "journal_entry", ["id", "status"], je)
    assert entry["status"] == "draft"
    assert _gl_for(path, je) == []
    assert _chain_head(path, env["company_id"]) == head_before
    assert _usage_used(path, "submit-journal-entry") == usage_before
    assert gate_tests._snapshot(path) == before

    path2, env2 = _build(tmp_path, monkeypatch, "readiness-caught")
    je2 = gate_tests._je(path2, env2)
    gate_tests._grant(path2, env2["company_id"], je2, "submit-journal-entry")
    fx.make_active(path2)
    spent["done"] = False
    issued2 = gate_tests._issue_full(
        path2, "submit-journal-entry",
        gate_tests._std("submit-journal-entry", path2, je2))
    auth2 = issued2["authorization_id"]
    argv2 = gate_tests._std("submit-journal-entry", path2, je2)

    def _handler2(handle):
        try:
            mod.submit_journal_entry(
                handle, helpers.ns(journal_entry_id=je2))
        except authority_sink.LedgerWriteRefused:
            pass
        print(json.dumps({"status": "ok", "journal_entry_id": je2,
                          "document_status": "submitted"}))
        handle.commit()

    before2 = gate_tests._snapshot(path2)
    head_before2 = _chain_head(path2, env2["company_id"])
    usage_before2 = _usage_used(path2, "submit-journal-entry")
    conn2 = get_connection(path2)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
            authority_gate.verify_and_consume(
                conn2, authorization_id=auth2,
                action="submit-journal-entry", argv=list(argv2),
                handler=_handler2)
        assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
        assert authority_sink.current(conn2) is None
        _select_one(conn2)
    finally:
        conn2.close()
    auth_b = gate_tests._read_one(
        path2, "operation_authorization",
        ["id", "consumed_at", "consumed_txn"], auth2)
    assert auth_b["consumed_at"] is None
    assert auth_b["consumed_txn"] is None
    entry2 = gate_tests._read_one(
        path2, "journal_entry", ["id", "status"], je2)
    assert entry2["status"] == "draft"
    assert _gl_for(path2, je2) == []
    assert _chain_head(path2, env2["company_id"]) == head_before2
    assert _usage_used(path2, "submit-journal-entry") == usage_before2
    assert gate_tests._snapshot(path2) == before2


def test_replay_never_reaches_the_seam(tmp_path, monkeypatch):
    """Replaying a spent envelope writes nothing; not qualification."""
    from erpclaw_lib import authority_sink
    path, env, je, auth_id = _active_issue(
        tmp_path, monkeypatch, "replay")
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    argv = gate_tests._std("submit-journal-entry", path, je,
                           ["--authorization-id", auth_id])
    code, payload = gate_tests._direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    assert len(bind_calls) == 1
    count_before = len(_gl_for(path, je))
    assert count_before == 2
    before = gate_tests._snapshot(path)
    check_calls = _spy_calls(monkeypatch, authority_sink, "check_statement")
    code2, payload2 = gate_tests._direct(argv)
    assert code2 == 0, payload2
    assert payload2.get("replayed") is True
    assert payload2.get("authorization_id") == auth_id
    assert payload2.get("result_kind") == "journal-entry"
    assert payload2.get("result_id") == je
    assert payload2.get("result_status") == "submitted"
    assert check_calls == []
    assert len(bind_calls) == 1
    assert len(_gl_for(path, je)) == count_before
    assert gate_tests._snapshot(path) == before


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres mirrors of submit and readiness loss; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import authority_gate
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import authority_sink
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()

    def _reset_seed_cache():
        gate_tests._SEEDED.discard(None)
        for entry in list(gate_tests._SEED_CAPS):
            if entry[0] is None:
                gate_tests._SEED_CAPS.discard(entry)

    try:
        import setup_helpers as setup
        gate_tests._pg_reset_schema()
        _reset_seed_cache()
        setup.init_all_tables(None)
        env = gate_tests._pg_seed(None)
        je = gate_tests._je(None, env)
        gate_tests._grant(None, env["company_id"], je, "submit-journal-entry")
        fx.make_active(None)
        gate_tests._patch_ready(monkeypatch)
        gate_tests._patch_actor(monkeypatch)
        issued = gate_tests._issue_full(
            None, "submit-journal-entry",
            ["--action", "submit-journal-entry",
             "--journal-entry-id", je])
        assert issued["issued_route"] == "delegation"
        auth_id = issued["authorization_id"]
        bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
        code, payload = gate_tests._direct(
            ["--action", "submit-journal-entry",
             "--journal-entry-id", je,
             "--authorization-id", auth_id])
        assert code == 0, payload
        assert payload.get("document_status") == "submitted"
        assert len(_gl_for(None, je)) == 2
        assert len(bind_calls) == 1

        gate_tests._pg_reset_schema()
        _reset_seed_cache()
        setup.init_all_tables(None)
        env2 = gate_tests._pg_seed(None)
        other = gate_tests._je(None, env2)
        gate_tests._grant(
            None, env2["company_id"], other, "submit-journal-entry")
        fx.make_active(None)
        issued2 = gate_tests._issue_full(
            None, "submit-journal-entry",
            ["--action", "submit-journal-entry",
             "--journal-entry-id", other])
        auth_b = issued2["authorization_id"]
        spent = {"done": False}
        real_consume = authority_gate.consume

        def _spy_consume(conn_arg, **kwargs):
            try:
                return real_consume(conn_arg, **kwargs)
            finally:
                spent["done"] = True

        monkeypatch.setattr(authority_gate, "consume", _spy_consume)
        monkeypatch.setattr(
            authority_readiness, "is_ready",
            lambda conn_arg: not spent["done"])
        mod = helpers.load_db_query()
        argv = ["--action", "submit-journal-entry",
                "--journal-entry-id", other]

        def _pg_handler(handle):
            mod.submit_journal_entry(
                handle, helpers.ns(journal_entry_id=other))

        conn = get_connection(None)
        try:
            with pytest.raises(
                    authority_sink.LedgerWriteRefused) as excinfo:
                authority_gate.verify_and_consume(
                    conn, authorization_id=auth_b,
                    action="submit-journal-entry", argv=list(argv),
                    handler=_pg_handler)
            assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
            assert authority_sink.current(conn) is None
            _select_one(conn)
        finally:
            conn.close()
        auth_row = gate_tests._read_one(
            None, "operation_authorization",
            ["id", "consumed_at", "consumed_txn"], auth_b)
        assert auth_row["consumed_at"] is None
        assert auth_row["consumed_txn"] is None
        assert _gl_for(None, other) == []
    finally:
        _reset_seed_cache()
        seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
