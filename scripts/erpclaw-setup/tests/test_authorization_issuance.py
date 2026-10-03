"""Issuance, revocation and reading of single-use authorization envelopes."""
import ast
import json
import os
import sys
import time

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402
import authority_fixtures as fx  # noqa: E402
from setup_helpers import (  # noqa: E402
    call_action, freeze_snapshot, load_db_query, ns, open_reader, read_all)
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402

AUTH_COLS = ["id", "install_id", "principal_id", "action",
             "binding_digest", "delegation_id", "issued_at",
             "expires_at", "revoked_at", "consumed_at", "consumed_txn"]
SIDE_COLS = ["authorization_id", "install_id", "principal_id",
             "envelope_version", "args_digest", "issuer_id",
             "issued_route", "reason_code", "reason_text",
             "idempotency_key", "call_id", "envelope_digest"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


@pytest.fixture
def seeded(db_path):
    company_id = fx.seed_company(db_path)
    info = fx.seed_authority(db_path, company_id)
    return info


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


def _standard_issue(db_path, seeded, monkeypatch, amount="60.00",
                    name="Crate", key="key-1", **over):
    mod = _issuance()
    _register(monkeypatch)
    _fix_clock(monkeypatch, seeded["now"])
    params = {
        "principal_id": fx.SERVICE,
        "delegation_id": fx.DELEGATION,
        "action": fx.ACTION,
        "argv": fx.standard_argv(
            seeded["company_id"], amount=amount, name=name),
        "reason_code": "ops-need",
        "reason_text": "need units",
        "idempotency_key": key,
    }
    params.update(over)
    conn = _open(db_path)
    try:
        return mod.issue_envelope(conn, **params)
    finally:
        conn.close()


def _counts(db_path):
    auth = fx.read_rows(db_path, "operation_authorization", ["id"])
    side = fx.read_rows(
        db_path, "operation_authorization_envelope", ["authorization_id"])
    return len(auth), len(side)


def test_clock_and_readiness(db_path, seeded):
    clock = _clock()
    before = time.time_ns() // 1_000_000
    value = clock.now_ms()
    after = time.time_ns() // 1_000_000
    assert type(value) is int
    assert before <= value <= after
    assert clock.EXACT_APPROVAL_DEFAULT_MS == 600_000
    assert clock.EXACT_APPROVAL_MAX_MS == 3_600_000
    assert clock.ROUTINE_MAX_MS == 86_400_000
    from erpclaw_lib import authority_readiness
    conn = _open(db_path)
    try:
        assert authority_readiness.is_ready(conn) is False
    finally:
        conn.close()


def test_routine_issue_at_staged(db_path, seeded, monkeypatch):
    out = _standard_issue(db_path, seeded, monkeypatch)
    assert out["issued_route"] == "staged_unattested"
    assert out["expires_at"] == seeded["now"] + 86_400_000
    assert out["idempotent"] is False
    assert _counts(db_path) == (1, 1)
    side = fx.read_rows(db_path, "operation_authorization_envelope",
                        SIDE_COLS)
    assert side[0]["issuer_id"] == fx.OWNER
    assert side[0]["envelope_version"] == 2
    assert side[0]["envelope_digest"] == out["envelope_digest"]
    usage = fx.read_rows(db_path, "authority_delegation_usage",
                         ["used"])
    assert [row["used"] for row in usage] == ["0.00"]
    fx.set_delegation_expiry(
        db_path, fx.DELEGATION, seeded["now"] + 5_000)
    out2 = _standard_issue(
        db_path, seeded, monkeypatch, name="Box", key="key-2")
    assert out2["expires_at"] == seeded["now"] + 5_000


def test_idempotent_issue(db_path, seeded, monkeypatch):
    first = _standard_issue(db_path, seeded, monkeypatch)
    second = _standard_issue(db_path, seeded, monkeypatch)
    assert second["authorization_id"] == first["authorization_id"]
    assert second["idempotent"] is True
    assert _counts(db_path) == (1, 1)
    mod = _issuance()
    _fix_clock(monkeypatch, seeded["now"])
    conn = _open(db_path)
    try:
        with pytest.raises(mod.AuthorityRefusal if hasattr(
                mod, "AuthorityRefusal") else Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(
                    seeded["company_id"], amount="61.00",
                    name="Crate"),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="key-1")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.IDEMPOTENCY_CONFLICT,)


@pytest.mark.parametrize("case", [
    "revoked", "expired", "service-issuer", "delegation-right",
    "principal-right", "over-per-op", "headroom", "sensitive",
    "eur-cap",
])
def test_issue_refusals(db_path, seeded, monkeypatch, case):
    mod = _issuance()
    gate = _register(
        monkeypatch,
        **({"action_class": "sensitive"} if case == "sensitive" else {}))
    if case == "eur-cap":
        monkeypatch.setitem(
            gate.ENVELOPE_ACTIONS, "add-uom",
            fx.make_declaration(currency="EUR"))
    _fix_clock(monkeypatch, seeded["now"])
    if case == "revoked":
        fx.set_delegation_revoked(
            db_path, fx.DELEGATION, seeded["now"])
    elif case == "expired":
        fx.set_delegation_expiry(
            db_path, fx.DELEGATION, seeded["now"] - 1_000)
    elif case == "service-issuer":
        fx.set_delegation_issuer(
            db_path, fx.DELEGATION, fx.OTHER_SERVICE)
    elif case == "delegation-right":
        fx.delete_delegation_rights(db_path, fx.DELEGATION)
    elif case == "principal-right":
        fx.delete_principal_rights(db_path, fx.SERVICE)
    elif case == "headroom":
        fx.insert_usage(
            db_path, seeded["install_id"], fx.DELEGATION,
            fx.ACTION, fx.CURRENCY, seeded["window_start"],
            seeded["window_end"], "50.00")
    amount = "80.00" if case == "over-per-op" else "60.00"
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(
                    seeded["company_id"], amount=amount,
                    name="Crate"),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="key-%s" % case)
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    assert _counts(db_path) == (0, 0)


def test_undeclared_and_malformed(db_path, seeded, monkeypatch):
    mod = _issuance()
    _register(monkeypatch)
    _fix_clock(monkeypatch, seeded["now"])

    def _try(argv=None, action=fx.ACTION, lifetime=None, key="k"):
        conn = _open(db_path)
        try:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=action,
                argv=argv if argv is not None else fx.standard_argv(
                    seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key=key, lifetime_ms=lifetime)
        finally:
            conn.close()

    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action="nope-action",
                argv=fx.standard_argv(seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="k-undeclared")
    finally:
        conn.close()
    gate = _gate()
    assert excinfo.value.args == (gate.IMPACT_UNDECLARED,)
    with pytest.raises(ValueError) as excinfo:
        _try(argv=["--amount", "60.00", "--name", "Crate"], key="k-bound")
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    with pytest.raises(ValueError) as excinfo:
        _try(argv=fx.standard_argv(
            seeded["company_id"], amount="1E+2"), key="k-money")
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    with pytest.raises(ValueError) as excinfo:
        _try(lifetime=0, key="k-life0")
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    with pytest.raises(ValueError) as excinfo:
        _try(lifetime=86_400_001, key="k-life1")
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)


def test_exact_approval_lifetimes(db_path, seeded, monkeypatch):
    mod = _issuance()
    _register(monkeypatch, action_class="sensitive")
    _fix_clock(monkeypatch, seeded["now"])

    def _exact(lifetime=None, key="exact-1", issuer=fx.OWNER,
               principal=fx.SERVICE):
        conn = _open(db_path)
        try:
            return mod.issue_exact_approval(
                conn, issuer_id=issuer, principal_id=principal,
                action=fx.ACTION,
                argv=fx.standard_argv(seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key=key, lifetime_ms=lifetime)
        finally:
            conn.close()

    out = _exact()
    assert out["issued_route"] == "staged_unattested"
    assert out["idempotent"] is False
    auth = fx.read_rows(db_path, "operation_authorization",
                        ["delegation_id", "issued_at", "expires_at"])
    assert auth[0]["delegation_id"] is None
    assert (auth[0]["expires_at"] - auth[0]["issued_at"]) == 600_000
    out2 = _exact(lifetime=3_600_000, key="exact-2")
    assert (out2["expires_at"] - seeded["now"]) == 3_600_000
    with pytest.raises(ValueError) as excinfo:
        _exact(lifetime=3_600_001, key="exact-3")
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    with pytest.raises(Exception) as excinfo:
        _exact(key="exact-4", issuer=fx.OTHER_SERVICE)
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)


def test_active_refusals(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    mod = _issuance()
    gate = _gate()
    _register(monkeypatch)
    _fix_clock(monkeypatch, seeded["now"])
    fx.make_active(db_path)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="key-active")
    finally:
        conn.close()
    assert excinfo.value.args == (gate.AUTHORITY_NOT_READY,)
    from erpclaw_lib import authority_readiness
    from erpclaw_lib import actor
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(
        actor, "current",
        lambda: actor.ActorContext(
            None, None, None, (), actor.ABSENT))
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_envelope(
                conn, principal_id=fx.SERVICE,
                delegation_id=fx.DELEGATION, action=fx.ACTION,
                argv=fx.standard_argv(seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="key-active2")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUANCE_REFUSED,)
    conn = _open(db_path)
    try:
        with pytest.raises(Exception) as excinfo:
            mod.issue_exact_approval(
                conn, issuer_id=fx.OWNER, principal_id=fx.SERVICE,
                action=fx.ACTION,
                argv=fx.standard_argv(seeded["company_id"]),
                reason_code="ops-need", reason_text="need units",
                idempotency_key="key-active3")
    finally:
        conn.close()
    assert excinfo.value.args == (mod.AUTHORIZATION_ISSUER_UNAVAILABLE,)


def _action_args(seeded, argv=None, key="cli-1", envelope=None):
    return ns(
        principal_id=fx.SERVICE, delegation_id=fx.DELEGATION,
        authorized_action=fx.ACTION,
        authorized_args=json.dumps(
            argv if argv is not None else fx.standard_argv(
                seeded["company_id"])),
        reason_code="ops-need", reason_text="need units",
        authorization_key=key, call_id=None, lifetime_ms=None,
        envelope_id=envelope)


def test_setup_actions_at_staged(db_path, seeded, monkeypatch):
    mod = load_db_query()
    _register(monkeypatch)
    _fix_clock(monkeypatch, seeded["now"])
    conn = _open(db_path)
    try:
        gate = _gate()
        monkeypatch.delitem(
            gate.ENVELOPE_ACTIONS, "add-uom", raising=False)
        missing = call_action(
            mod.issue_authorization, conn, _action_args(seeded))
        assert missing["status"] == "error"
        assert missing["message"] == gate.IMPACT_UNDECLARED
        monkeypatch.setitem(
            gate.ENVELOPE_ACTIONS, "add-uom",
            fx.make_declaration())
        bad = call_action(
            mod.issue_authorization, conn,
            _action_args(seeded, argv={"a": 1}, key="cli-bad"))
        assert bad["status"] == "error"
        assert bad["message"] == "AUTHORIZATION_INPUT_INVALID"
        ok_out = call_action(
            mod.issue_authorization, conn, _action_args(seeded))
        assert ok_out["status"] == "ok"
        assert ok_out["issued_route"] == "staged_unattested"
        auth_id = ok_out["authorization_id"]
        got = call_action(
            mod.get_authorization, conn,
            ns(envelope_id=auth_id))
        assert got["status"] == "ok"
        assert got["authorization_id"] == auth_id
        assert "reason_text" not in got
        assert got["issued_route"] == "staged_unattested"
        revoked = call_action(
            mod.revoke_authorization, conn,
            ns(envelope_id=auth_id))
        assert revoked["status"] == "ok"
        assert revoked["revoked"] is True
        reader = open_reader(db_path)
        try:
            rows = [row for row in read_all(
                reader, "audit_log",
                ["action", "authorization_id"])
                if row["authorization_id"] == auth_id]
        finally:
            reader.close()
        assert len(rows) == 1
        again = call_action(
            mod.revoke_authorization, conn,
            ns(envelope_id=auth_id))
        assert again["status"] == "error"
        assert again["message"] == "AUTHORIZATION_REFUSED"
    finally:
        conn.close()


def test_setup_actions_refuse_at_active(db_path, seeded, monkeypatch):
    mod = load_db_query()
    _register(monkeypatch)
    _fix_clock(monkeypatch, seeded["now"])
    fx.make_active(db_path)
    conn = _open(db_path)
    try:
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(
                conn, db_path, seam.table_names(db_path))
        finally:
            reader.close()
        for fn, args in (
                (mod.issue_authorization, _action_args(seeded)),
                (mod.revoke_authorization, ns(envelope_id="x")),
                (mod.get_authorization, ns(envelope_id="x"))):
            out = call_action(fn, conn, args)
            assert out["status"] == "error"
            assert out["message"] == "AUTHORIZATION_ISSUER_UNAVAILABLE"
        after = freeze_snapshot(conn, db_path, seam.table_names(db_path))
        assert before == after
    finally:
        conn.close()


def test_active_row_is_active_whatever_the_inspector_says(
        db_path, seeded, monkeypatch):
    """not qualification: inspector patches below prove nothing."""
    mod = load_db_query()
    gate = _gate()
    _register(monkeypatch)
    fx.make_active(db_path)
    for mode in ("raise", "mismatch"):
        if mode == "raise":
            def _boom(*args, **kwargs):
                raise RuntimeError("boom")
            monkeypatch.setattr(
                seam, "inspect_authority_core", _boom)
        else:
            monkeypatch.setattr(
                seam, "inspect_authority_core",
                lambda *args, **kwargs: {
                    "profile": "p", "status": "MISMATCH",
                    "phase": None, "reason": "x"})
        conn = _open(db_path)
        try:
            with pytest.raises(Exception) as excinfo:
                gate.verify_and_consume(
                    conn, authorization_id="auth-1",
                    action=fx.ACTION,
                    argv=fx.standard_argv(seeded["company_id"]),
                    handler=lambda proxy: None)
        finally:
            conn.close()
        assert excinfo.value.args == (gate.AUTHORITY_NOT_READY,)
        conn = _open(db_path)
        try:
            reader = open_reader(db_path)
            try:
                before = freeze_snapshot(
                    conn, db_path, seam.table_names(db_path))
            finally:
                reader.close()
            for fn, args in (
                    (mod.issue_authorization, _action_args(seeded)),
                    (mod.revoke_authorization, ns(envelope_id="x")),
                    (mod.get_authorization, ns(envelope_id="x"))):
                out = call_action(fn, conn, args)
                assert out["status"] == "error"
                assert (out["message"]
                        == "AUTHORIZATION_ISSUER_UNAVAILABLE")
            after = freeze_snapshot(
                conn, db_path, seam.table_names(db_path))
            assert before == after
        finally:
            conn.close()


ISSUE = "issue-authorization"
REVOKE_AUTH = "revoke-authorization"
GET_AUTH = "get-authorization"


def test_setup_cli_parser_builds(monkeypatch, capsys):
    mod = load_db_query()
    monkeypatch.setattr(sys, "argv", ["db_query.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        mod.main()
    assert excinfo.value.code == 0
    assert "--action" in capsys.readouterr().out


def test_authorization_actions_routed_and_carved_out():
    repo = os.path.abspath(os.path.join(_TESTS_DIR, os.pardir, os.pardir,
                                        os.pardir, os.pardir, os.pardir))
    router_path = os.path.join(repo, "source", "erpclaw", "scripts",
                               "db_query.py")
    with open(router_path, encoding="utf-8") as handle:
        router_tree = ast.parse(handle.read())
    action_map = {}
    dangerous = set()
    for node in router_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_MAP":
                    if isinstance(node.value, ast.Dict):
                        for key, value in zip(node.value.keys,
                                              node.value.values):
                            if (isinstance(key, ast.Constant)
                                    and isinstance(value, ast.Constant)):
                                action_map[key.value] = value.value
                if isinstance(target, ast.Name) and target.id == "DANGEROUS_ACTIONS":
                    call = node.value
                    if isinstance(call, ast.Call) and call.args:
                        first = call.args[0]
                        if isinstance(first, (ast.Set, ast.List, ast.Tuple)):
                            dangerous = {elt.value for elt in first.elts
                                         if isinstance(elt, ast.Constant)}
    for name in (ISSUE, REVOKE_AUTH, GET_AUTH):
        assert action_map.get(name) == "erpclaw-setup", name
    for name in (ISSUE, REVOKE_AUTH):
        assert name in dangerous, name
    assert GET_AUTH not in dangerous
    skill_path = os.path.join(repo, "source", "erpclaw", "SKILL.md")
    with open(skill_path, encoding="utf-8") as handle:
        skill_text = handle.read()
    assert len(skill_text.splitlines()) <= 300
    for name in (ISSUE, REVOKE_AUTH, GET_AUTH):
        assert ("`" + name + "`") in skill_text, name
    confirm_path = os.path.join(repo, "source", "erpclaw", "mcp",
                                "confirm.py")
    with open(confirm_path, encoding="utf-8") as handle:
        confirm_tree = ast.parse(handle.read())
    carved = set()
    for node in confirm_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CREDENTIAL_CARVE_OUT":
                    call = node.value
                    if isinstance(call, ast.Call) and call.args:
                        first = call.args[0]
                        if isinstance(first, (ast.Set, ast.List, ast.Tuple)):
                            carved = {elt.value for elt in first.elts
                                      if isinstance(elt, ast.Constant)}
    for name in (ISSUE, REVOKE_AUTH):
        assert name in carved, name
    assert GET_AUTH not in carved
