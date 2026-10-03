"""Payments scope gate behind the shared gate."""
import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PAY_MODULE = os.path.dirname(_HERE)
_SETUP_TESTS = os.path.join(os.path.dirname(_PAY_MODULE), "erpclaw-setup", "tests")
_JRNL_TESTS = os.path.join(os.path.dirname(_PAY_MODULE), "erpclaw-journals", "tests")
for _p in (_SETUP_TESTS, _JRNL_TESTS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_journal_envelope_gate as jgate
import test_payment_envelope_gate as pgate

DATE = "2026-06-15"

_RIGHTS_CAPS = set()


def _rights(path, company, pe, action):
    from erpclaw_lib.db import get_connection
    import authority_fixtures as fx
    from erpclaw_lib import authority_clock
    handle = get_connection(path)
    try:
        install_id = fx._install_id(handle)
        fx._insert_row(handle, "authority_right", {
            "install_id": install_id, "principal_id": fx.SERVICE,
            "company_id": company, "resource_kind": "payment-entry",
            "resource_id": pe, "action": action, "effect": "allow"})
        fx._insert_row(handle, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company, "resource_kind": "payment-entry",
            "resource_id": pe, "action": action})
        if (path, action) not in _RIGHTS_CAPS:
            now = authority_clock.now_ms()
            fx._insert_row(handle, "authority_delegation_cap", {
                "install_id": install_id, "delegation_id": fx.DELEGATION,
                "action": action, "currency": "USD", "scale": 2,
                "per_operation": "500.00", "aggregate_limit": "1000.00",
                "window_start": now - fx.DAY_MS,
                "window_end": now + 10 * fx.DAY_MS})
            _RIGHTS_CAPS.add((path, action))
        handle.commit()
    finally:
        handle.close()


def _allow_member(path, principal, company, effect="allow"):
    from erpclaw_lib.db import get_connection
    import authority_fixtures as fx
    handle = get_connection(path)
    try:
        install_id = fx._install_id(handle)
        fx._insert_row(handle, "authority_membership", {
            "install_id": install_id, "principal_id": principal,
            "company_id": company, "effect": effect})
        handle.commit()
    finally:
        handle.close()


def _make_draft(handle, mod, helpers, env):
    out = helpers.call_action(
        mod.add_payment, handle,
        helpers.ns(company_id=env["company_id"], payment_type="receive",
                   posting_date=DATE, party_type="customer",
                   party_id=env["customer"],
                   paid_from_account=env["ar"],
                   paid_to_account=env["bank"], paid_amount="100.00",
                   payment_currency="USD", exchange_rate="1",
                   reference_number=None, reference_date=None,
                   allocations=None, deductions=None, dimensions=None,
                   dimension_key=None, dimension_value=None))
    assert out.get("status") == "ok", out
    return out["payment_entry_id"]


def _submit_direct(handle, mod, helpers, pe):
    out = helpers.call_action(
        mod.submit_payment, handle,
        helpers.ns(payment_entry_id=pe))
    assert out.get("status") == "ok", out
    return out


@pytest.fixture
def two_companies(tmp_path, monkeypatch):
    from erpclaw_lib.db import get_connection
    import setup_helpers as setup
    import payments_helpers as helpers
    import authority_fixtures as fx
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    path = str(tmp_path / "scope-two.sqlite")
    setup.init_all_tables(path)
    monkeypatch.setenv("ERPCLAW_DB_PATH", path)
    handle = get_connection(path)
    try:
        env_a = helpers.build_ar_env(handle)
        env_b = helpers.build_ar_env(handle)
    finally:
        handle.close()
    fx.seed_authority(path, env_a["company_id"])
    mod = helpers.load_db_query()
    handle = get_connection(path)
    try:
        draft_a = _make_draft(handle, mod, helpers, env_a)
        draft_b = _make_draft(handle, mod, helpers, env_b)
        _submit_direct(handle, mod, helpers, draft_a)
        _submit_direct(handle, mod, helpers, draft_b)
    finally:
        handle.close()
    from erpclaw_lib import seam as _seam
    _seam.dispose_engines()
    return {"path": path, "env_a": env_a, "env_b": env_b,
            "pe_a": draft_a, "pe_b": draft_b}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    from erpclaw_lib import seam as _seam
    _seam.dispose_engines()


@pytest.mark.parametrize("via", ["direct", "router"])
def test_cross_company_cancel(two_companies, tmp_path, monkeypatch, via):
    """Cross scope cancel stays closed while same scope passes; not qualification."""
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    env_a = bundle["env_a"]
    env_b = bundle["env_b"]
    pe_a = bundle["pe_a"]
    pe_b = bundle["pe_b"]
    comp_a = env_a["company_id"]
    comp_b = env_b["company_id"]
    _rights(path, comp_a, pe_a, "cancel-payment")
    _rights(path, comp_b, pe_b, "cancel-payment")
    _allow_member(path, fx.SERVICE, comp_b, "allow")
    _allow_member(path, fx.OWNER, comp_b, "allow")
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    auth_b = pgate._issue(path, "cancel-payment", ["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_b])
    _allow_member(path, fx.SERVICE, comp_b, "deny")
    before = jgate._snapshot(path)
    if via == "direct":
        code, payload = pgate._direct(["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_b, "--authorization-id", auth_b])
    else:
        code, payload = pgate._via_router(["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_b, "--user-confirmed", "--authorization-id", auth_b], tmp_path, monkeypatch)
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    assert jgate._snapshot(path) == before
    entry_b = jgate._read_one(path, "payment_entry", ["id", "status"], pe_b)
    assert entry_b["status"] == "submitted"
    auth_row = jgate._read_one(path, "operation_authorization", ["id", "consumed_at"], auth_b)
    assert auth_row["consumed_at"] is None
    auth_a = pgate._issue(path, "cancel-payment", ["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a])
    if via == "direct":
        code, payload = pgate._direct(["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a, "--authorization-id", auth_a])
    else:
        code, payload = pgate._via_router(["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a, "--user-confirmed", "--authorization-id", auth_a], tmp_path, monkeypatch)
    assert code == 0, payload
    entry_a = jgate._read_one(path, "payment_entry", ["id", "status"], pe_a)
    assert entry_a["status"] == "cancelled"
    rows = jgate._read_all(path, "audit_log", ["action", "entity_id", "scope_status", "scope_company_ids"])
    found = [r for r in rows if r["action"] == "cancel-payment" and r["entity_id"] == pe_a]
    assert len(found) >= 1
    last = found[-1]
    assert last["scope_status"] == "in_scope"
    assert last["scope_company_ids"] == comp_a


def test_no_company_reads_at_active(two_companies, monkeypatch):
    """Reads without a named scope resolve for one scope only; not qualification."""
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    pe_b = bundle["pe_b"]
    comp_b = bundle["env_b"]["company_id"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    code, payload = pgate._direct(["--action", "list-payments", "--db-path", path])
    assert code == 0, payload
    ids = [r["id"] for r in payload.get("payments", [])]
    assert pe_a in ids
    assert pe_b not in ids
    code, payload = pgate._direct(["--action", "list-payments", "--db-path", path, "--company-id", comp_b])
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    _allow_member(path, fx.SERVICE, comp_b, "allow")
    code, payload = pgate._direct(["--action", "list-payments", "--db-path", path])
    assert code == 1
    assert payload.get("message") == "COMPANY_SCOPE_AMBIGUOUS"
    suggestion = payload.get("suggestion", "")
    assert isinstance(suggestion, str) and suggestion
    assert bundle["env_a"]["company_id"] not in suggestion
    assert comp_b not in suggestion


def test_gate_fails_closed_at_active(two_companies, monkeypatch):
    """Unexpected scope failures refuse closed at ACTIVE; not qualification."""
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    comp_a = bundle["env_a"]["company_id"]
    fx.make_active(path)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    before = jgate._snapshot(path)
    code, payload = pgate._direct(["--action", "get-payment", "--db-path", path, "--payment-entry-id", pe_a])
    assert code == 1
    assert payload == {"status": "error", "message": "AUTHORITY_NOT_READY"}
    assert jgate._snapshot(path) == before
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(audit_mod, "_probe_scope_columns", lambda c: False)
    before = jgate._snapshot(path)
    code, payload = pgate._direct(["--action", "get-payment", "--db-path", path, "--payment-entry-id", pe_a])
    assert code == 1
    assert payload == {"status": "error", "message": "AUTHORITY_NOT_READY"}
    assert jgate._snapshot(path) == before
    _rights(path, comp_a, pe_a, "cancel-payment")
    auth_id = pgate._issue(path, "cancel-payment", ["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a])
    before = jgate._snapshot(path)
    code, payload = pgate._direct(["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a, "--authorization-id", auth_id])
    assert code == 1
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert jgate._snapshot(path) == before
    auth_row = jgate._read_one(path, "operation_authorization", ["id", "consumed_at"], auth_id)
    assert auth_row["consumed_at"] is None
    monkeypatch.setattr(audit_mod, "_probe_scope_columns", lambda c: True)
    _orig_gate = scope_mod.gate_note
    monkeypatch.setattr(scope_mod, "gate_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    before = jgate._snapshot(path)
    code, payload = pgate._direct(["--action", "get-payment", "--db-path", path, "--payment-entry-id", pe_a])
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    assert jgate._snapshot(path) == before
    monkeypatch.setattr(scope_mod, "gate_note", _orig_gate)
    monkeypatch.setattr(scope_mod, "bind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    before = jgate._snapshot(path)
    code, payload = pgate._direct(["--action", "get-payment", "--db-path", path, "--payment-entry-id", pe_a])
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    assert jgate._snapshot(path) == before


def test_staged_records_each_verdict(two_companies, monkeypatch):
    """Each staged verdict is stored with its own row; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import audit as audit_mod
    import authority_fixtures as fx
    import payments_helpers as helpers
    bundle = two_companies
    path = bundle["path"]
    env_a = bundle["env_a"]
    env_b = bundle["env_b"]
    comp_a = env_a["company_id"]
    comp_b = env_b["company_id"]
    mod = helpers.load_db_query()

    def _audit_row(action, entity):
        rows = jgate._read_all(path, "audit_log", ["action", "entity_id", "scope_status", "scope_company_ids", "actor_status"])
        found = [r for r in rows if r["action"] == action and r["entity_id"] == entity]
        assert found, (action, entity)
        return found[-1]

    def _new_draft(env):
        handle = get_connection(path)
        try:
            return _make_draft(handle, mod, helpers, env)
        finally:
            handle.close()

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, None, (), actor.ABSENT))
    draft0 = _new_draft(env_a)
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft0])
    assert code == 0, payload
    row = _audit_row("submit-payment", draft0)
    assert (row["scope_status"], row["scope_company_ids"]) == ("no_principal", comp_a)

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    draft1 = _new_draft(env_a)
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft1])
    assert code == 0, payload
    row = _audit_row("submit-payment", draft1)
    assert (row["scope_status"], row["scope_company_ids"]) == ("in_scope", comp_a)

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.CLAIMED))
    draft2 = _new_draft(env_a)
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft2])
    assert code == 0, payload
    row = _audit_row("submit-payment", draft2)
    assert (row["scope_status"], row["scope_company_ids"]) == ("in_scope", comp_a)
    assert row["actor_status"] == "claimed"

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    code, payload = pgate._direct(["--action", "add-payment", "--db-path", path, "--company-id", comp_b, "--payment-type", "receive", "--posting-date", DATE, "--party-type", "customer", "--party-id", env_b["customer"], "--paid-from-account", env_b["ar"], "--paid-to-account", env_b["bank"], "--paid-amount", "10.00"])
    assert code == 0, payload
    new_id = payload.get("payment_entry_id")
    assert new_id
    row = _audit_row("add-payment", new_id)
    assert (row["scope_status"], row["scope_company_ids"]) == ("out_of_scope", comp_b)

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.OTHER_SERVICE, (), actor.ATTESTED))
    draft4 = _new_draft(env_a)
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft4])
    assert code == 0, payload
    row = _audit_row("submit-payment", draft4)
    assert (row["scope_status"], row["scope_company_ids"]) == ("no_scope", comp_a)

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    draft5 = _new_draft(env_a)
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry", draft5])
    assert code == 0, payload
    row = _audit_row("submit-payment", draft5)
    assert row["scope_status"] == "underived"
    assert row["scope_company_ids"] is None

    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    handle = get_connection(path)
    try:
        def _handler(inner):
            audit_mod.audit(inner, "erpclaw-setup", "add-uom", "uom", "u1")
            inner.commit()
            return {"status": "ok"}
        gate_mod.run(handle, "add-uom", [], _handler)
        handle.commit()
    finally:
        handle.close()
    row = _audit_row("add-uom", "u1")
    assert row["scope_status"] == "not_applicable"
    assert row["scope_company_ids"] is None


def test_staged_never_refuses(two_companies, monkeypatch):
    """Staged runs keep writing when scope helpers fail; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    import payments_helpers as helpers
    bundle = two_companies
    path = bundle["path"]
    env_a = bundle["env_a"]
    mod = helpers.load_db_query()
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))

    def _fresh():
        handle = get_connection(path)
        try:
            return _make_draft(handle, mod, helpers, env_a)
        finally:
            handle.close()

    def _row(pe):
        rows = jgate._read_all(path, "audit_log", ["action", "entity_id", "scope_status", "scope_company_ids"])
        found = [r for r in rows if r["action"] == "submit-payment" and r["entity_id"] == pe]
        assert found
        return found[-1]

    _orig_gate = scope_mod.gate_note
    monkeypatch.setattr(scope_mod, "gate_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    draft0 = _fresh()
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft0])
    assert code == 0, payload
    row = _row(draft0)
    assert (row["scope_status"], row["scope_company_ids"]) == ("underived", None)
    monkeypatch.setattr(scope_mod, "gate_note", _orig_gate)
    _orig_bind = scope_mod.bind_note
    monkeypatch.setattr(scope_mod, "bind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    draft1 = _fresh()
    code, payload = pgate._direct(["--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft1])
    assert code == 0, payload
    row = _row(draft1)
    assert row["scope_status"] is None
    assert row["scope_company_ids"] is None
    monkeypatch.setattr(scope_mod, "bind_note", _orig_bind)
    monkeypatch.setattr(audit_mod, "_probe_scope_columns", lambda c: False)
    draft2 = _fresh()
    import io as _io
    import sys as _sys
    from unittest.mock import patch as _patch
    buf_err = _io.StringIO()
    buf_out = _io.StringIO()
    code = None
    with _patch.object(_sys, "argv", ["db_query.py", "--action", "submit-payment", "--db-path", path, "--payment-entry-id", draft2]):
        with _patch("sys.stdout", buf_out):
            with _patch("sys.stderr", buf_err):
                try:
                    mod2 = helpers.load_db_query()
                    mod2.main()
                except SystemExit as done:
                    code = done.code if isinstance(done.code, int) else 0
    assert code == 0
    assert buf_err.getvalue() == ""
    row = _row(draft2)
    assert row["scope_status"] is None
    assert row["scope_company_ids"] is None


def test_gate_ends_its_transaction_and_unbinds(two_companies, monkeypatch):
    """The gate leaves no open unit and no bound note behind; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    pe_b = bundle["pe_b"]
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    seen = {}

    def _handler(inner):
        gate_mod.open_transaction(inner)
        note = scope_mod.bound_note(inner)
        assert note is not None
        assert note.status == "in_scope"
        seen["note"] = note
        inner.rollback()
        return {"status": "ok"}

    handle = get_connection(path)
    try:
        out = gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert out == {"status": "ok"}
        assert seen.get("note") is not None
        assert scope_mod.bound_note(handle) is None
    finally:
        handle.close()
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    handle = get_connection(path)
    try:
        out = gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert out == {"status": "ok"}
        assert scope_mod.bound_note(handle) is None
    finally:
        handle.close()
    handle = get_connection(path)
    try:
        with pytest.raises(gate_mod.AuthorityRefusal) as excinfo:
            gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_b], _handler)
        assert excinfo.value.args[0] == "COMPANY_SCOPE_REFUSED"
        assert scope_mod.bound_note(handle) is None
        gate_mod.open_transaction(handle)
        handle.rollback()
    finally:
        handle.close()


def test_multi_target_payment_actions_stay_closed_at_active(two_companies, monkeypatch):
    """Multi-row handlers stay unreachable without a full declaration; not qualification."""
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    comp_a = bundle["env_a"]["company_id"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    for action, argv in [
        ("allocate-payment", ["--action", "allocate-payment", "--db-path", path]),
        ("apply-advance-to-invoice", ["--action", "apply-advance-to-invoice", "--db-path", path]),
        ("create-payment-ledger-entry", ["--action", "create-payment-ledger-entry", "--db-path", path]),
    ]:
        before = jgate._snapshot(path)
        code, payload = pgate._direct(argv)
        assert code == 1
        assert payload.get("message") == "IMPACT_UNDECLARED"
        assert jgate._snapshot(path) == before



def test_active_unbind_failure_propagates_after_success(two_companies, monkeypatch):
    """Unbind failure replaces success at ACTIVE; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    monkeypatch.setattr(scope_mod, "unbind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unbind")))

    def _handler(inner):
        return {"status": "ok"}

    handle = get_connection(path)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert excinfo.value.args[0] == "unbind"
    finally:
        handle.close()


def test_active_unbind_failure_propagates_after_an_ok_exit(two_companies, monkeypatch):
    """SystemExit zero counts as success for unbind re-raise; not qualification."""
    import json as _json
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    monkeypatch.setattr(scope_mod, "unbind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unbind")))

    def _handler(inner):
        print(_json.dumps({"status": "ok"}))
        raise SystemExit(0)

    handle = get_connection(path)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert excinfo.value.args[0] == "unbind"
    finally:
        handle.close()


def test_active_handler_error_wins_over_unbind_failure(two_companies, monkeypatch):
    """Handler error survives a later unbind failure; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    monkeypatch.setattr(scope_mod, "unbind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unbind")))

    def _handler(inner):
        raise ValueError("handler")

    handle = get_connection(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert excinfo.value.args[0] == "handler"
    finally:
        handle.close()


def test_staged_unbind_failure_is_swallowed(two_companies, monkeypatch):
    """Staged unbind failures never escape; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    monkeypatch.setattr(scope_mod, "unbind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unbind")))

    def _handler(inner):
        return {"status": "ok"}

    handle = get_connection(path)
    try:
        out = gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert out == {"status": "ok"}
    finally:
        handle.close()



def test_envelope_branch_unbind_failure_propagates_at_active(two_companies, monkeypatch):
    """Envelope unbind failure replaces success at ACTIVE; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    import payments_helpers as helpers
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    comp_a = bundle["env_a"]["company_id"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    _rights(path, comp_a, pe_a, "cancel-payment")
    auth_id = pgate._issue(path, "cancel-payment", ["--action", "cancel-payment", "--db-path", path, "--payment-entry-id", pe_a])
    monkeypatch.setattr(scope_mod, "unbind_note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unbind")))
    mod = helpers.load_db_query()

    def _handler(proxy):
        return mod.cancel_payment(proxy, helpers.ns(payment_entry_id=pe_a))

    handle = get_connection(path)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            gate_mod.run(handle, "cancel-payment", ["--payment-entry-id", pe_a, "--authorization-id", auth_id], _handler, option_strings=["--payment-entry-id", "--authorization-id"], repeatable_options=())
        assert excinfo.value.args[0] == "unbind"
    finally:
        handle.close()


def test_staged_stale_note_is_not_recorded(two_companies, monkeypatch):
    """Staged clears a stale note before recording; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    comp_b = bundle["env_b"]["company_id"]
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    handle = get_connection(path)
    try:
        stale = scope_mod.Note((comp_b,), "out_of_scope")
        scope_mod.bind_note(handle, stale)

        def _handler(inner):
            audit_mod.audit(inner, "erpclaw-setup", "add-uom", "uom", "u1")
            inner.commit()
            return {"status": "ok"}

        out = gate_mod.run(handle, "add-uom", [], _handler)
        assert out == {"status": "ok"}
        try:
            handle.commit()
        except Exception:
            pass
        rows = jgate._read_all(path, "audit_log", ["action", "entity_id", "scope_status", "scope_company_ids"])
        found = [r for r in rows if r["action"] == "add-uom" and r["entity_id"] == "u1"]
        assert found
        assert (found[-1]["scope_status"], found[-1]["scope_company_ids"]) == ("not_applicable", None)
        assert scope_mod.bound_note(handle) is None
    finally:
        handle.close()


def test_active_pre_bound_note_still_refuses(two_companies, monkeypatch):
    """Active never clears a pre-bound note; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    bundle = two_companies
    path = bundle["path"]
    pe_a = bundle["pe_a"]
    comp_a = bundle["env_a"]["company_id"]
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    handle = get_connection(path)
    try:
        pre = scope_mod.Note((comp_a,), "in_scope")
        scope_mod.bind_note(handle, pre)
        seen = {}

        def _handler(inner):
            seen["called"] = True
            return {"status": "ok"}

        with pytest.raises(gate_mod.AuthorityRefusal) as excinfo:
            gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
        assert excinfo.value.args[0] == "COMPANY_SCOPE_REFUSED"
        assert "called" not in seen
        assert scope_mod.bound_note(handle) == pre
    finally:
        handle.close()


def test_clear_note_is_called_only_by_the_gate():
    """Only the gate and the scope module mention the private clearer."""
    import os as _os
    root = _os.path.abspath(_os.path.join(_HERE, "..", "..", "..", ".."))
    hits = []
    for dirpath, dirnames, filenames in _os.walk(root):
        parts = set(dirpath.split(_os.sep))
        if "tests" in parts:
            continue
        # skip any directory named tests below root
        if _os.sep + "tests" + _os.sep in dirpath or dirpath.endswith(_os.sep + "tests"):
            continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = _os.path.join(dirpath, name)
            # skip files inside a tests directory
            if _os.sep + "tests" + _os.sep in full:
                continue
            with open(full, "r", encoding="utf-8") as stream:
                text = stream.read()
            if "_clear_note" in text:
                hits.append(full)
    names = sorted([_os.path.basename(p) for p in hits])
    assert names == ["authority_gate.py", "company_scope.py"]

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _pg_seed_pair():
    from erpclaw_lib.db import get_connection
    import authority_fixtures as fx
    handle = get_connection(None)
    try:
        out = []
        for idx in (0, 1):
            cid = str(uuid.uuid4())
            fx._insert_row(handle, "company", {
                "id": cid, "name": "Pg Scope Co %s" % cid[:6],
                "abbr": "PS%s%d" % (cid[:4], idx),
                "default_currency": "USD", "country": "United States",
                "fiscal_year_start_month": 1})
            fx._insert_row(handle, "fiscal_year", {
                "id": str(uuid.uuid4()), "name": "FY-%s" % cid[:6],
                "start_date": "2026-01-01", "end_date": "2026-12-31",
                "company_id": cid})
            fx._insert_row(handle, "naming_series", {
                "id": str(uuid.uuid4()), "entity_type": "payment_entry",
                "prefix": "PAY-", "current_value": 0,
                "company_id": cid})
            fx._insert_row(handle, "cost_center", {
                "id": str(uuid.uuid4()), "name": "Main CC",
                "company_id": cid, "is_group": 0})
            bank = str(uuid.uuid4())
            fx._insert_row(handle, "account", {
                "id": bank, "name": "Bank", "account_number": "10%d0" % idx,
                "root_type": "asset", "account_type": "bank",
                "balance_direction": "debit_normal", "company_id": cid,
                "depth": 0})
            cash = str(uuid.uuid4())
            fx._insert_row(handle, "account", {
                "id": cash, "name": "Cash", "account_number": "10%d1" % idx,
                "root_type": "asset", "account_type": "cash",
                "balance_direction": "debit_normal", "company_id": cid,
                "depth": 0})
            cust = str(uuid.uuid4())
            fx._insert_row(handle, "customer", {
                "id": cust, "name": "Pg Customer %s" % cid[:6],
                "customer_type": "company", "status": "active",
                "company_id": cid})
            pe = str(uuid.uuid4())
            fx._insert_row(handle, "payment_entry", {
                "id": pe, "payment_type": "receive",
                "posting_date": DATE, "party_type": "customer",
                "party_id": cust, "paid_from_account": cash,
                "paid_to_account": bank, "paid_amount": "100.00",
                "received_amount": "100.00", "payment_currency": "USD",
                "exchange_rate": "1", "status": "draft",
                "unallocated_amount": "100.00", "company_id": cid})
            handle.commit()
            out.append((cid, pe))
        return out
    finally:
        handle.close()


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres leg mirrors the staged and active flows; not qualification."""
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import company_scope as scope_mod
    import authority_fixtures as fx
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    _RIGHTS_CAPS.clear()
    jgate._SEEDED.clear()
    jgate._SEED_CAPS.clear()
    pgate._SEEDED.clear()
    pgate._SEED_CAPS.clear()
    from erpclaw_lib import seam as _seam
    _seam.dispose_engines()
    try:
        jgate._pg_reset_schema()
        import setup_helpers as setup
        setup.init_all_tables(None)
        pairs = _pg_seed_pair()
        comp_a, pe_a = pairs[0]
        comp_b, pe_b = pairs[1]
        fx.seed_authority(None, comp_a)
        monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
        code, payload = pgate._direct(["--action", "get-payment", "--payment-entry-id", pe_a])
        assert code == 0, payload
        _rights(None, comp_a, pe_a, "submit-payment")
        auth_id = pgate._issue(None, "submit-payment", ["--action", "submit-payment", "--payment-entry-id", pe_a])
        code, payload = pgate._direct(["--action", "submit-payment", "--payment-entry-id", pe_a, "--authorization-id", auth_id])
        assert code == 0, payload
        entry = jgate._read_one(None, "payment_entry", ["id", "status"], pe_a)
        assert entry["status"] == "submitted"
        rows = jgate._read_all(None, "audit_log", ["action", "entity_id", "scope_status", "scope_company_ids"])
        found = [r for r in rows if r["action"] == "submit-payment" and r["entity_id"] == pe_a]
        assert found
        assert (found[-1]["scope_status"], found[-1]["scope_company_ids"]) == ("in_scope", comp_a)
        fx.make_active(None)
        monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
        monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
        code, payload = pgate._direct(["--action", "get-payment", "--payment-entry-id", pe_a])
        assert code == 0, payload
        before = jgate._snapshot(None)
        code, payload = pgate._direct(["--action", "get-payment", "--payment-entry-id", pe_b])
        assert code == 1
        assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
        assert jgate._snapshot(None) == before

        def _handler(inner):
            gate_mod.open_transaction(inner)
            note = scope_mod.bound_note(inner)
            assert note is not None
            assert note.status == "in_scope"
            inner.rollback()
            return {"status": "ok"}

        handle = get_connection(None)
        try:
            out = gate_mod.run(handle, "get-payment", ["--payment-entry-id", pe_a], _handler)
            assert out == {"status": "ok"}
            assert scope_mod.bound_note(handle) is None
        finally:
            handle.close()
    finally:
        _RIGHTS_CAPS.clear()
        jgate._SEEDED.clear()
        jgate._SEED_CAPS.clear()
        pgate._SEEDED.clear()
        pgate._SEED_CAPS.clear()
        _seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
