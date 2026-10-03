"""Selling submit and cancel behind single-use authorization."""
import io
import json
import os
import sys
import uuid

import pytest

_SI_TESTS = os.path.dirname(os.path.abspath(__file__))
_SI_MODULE = os.path.dirname(_SI_TESTS)
_SI_SETUP_TESTS = os.path.join(os.path.dirname(_SI_MODULE), "erpclaw-setup", "tests")
_SI_PAYMENTS_TESTS = os.path.join(
    os.path.dirname(_SI_MODULE), "erpclaw-payments", "tests")
if _SI_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _SI_SETUP_TESTS)
if _SI_PAYMENTS_TESTS not in sys.path:
    sys.path.insert(0, _SI_PAYMENTS_TESTS)
if _SI_TESTS not in sys.path:
    sys.path.insert(0, _SI_TESTS)

_ROUTER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(_SI_MODULE), "db_query.py"))

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


class _Forwarded(Exception):
    def __init__(self, argv):
        super().__init__("forwarded")
        self.argv = list(argv)


def _helpers():
    import selling_helpers as helpers
    return helpers


def _seam():
    from erpclaw_lib import seam
    return seam


def _open(path):
    from erpclaw_lib.db import get_connection
    return get_connection(path)


def _direct(argv):
    helpers = _helpers()
    mod = helpers.load_db_query()
    buf = io.StringIO()
    code = 0
    payload = {}
    from unittest.mock import patch
    with patch.object(sys, "argv", ["db_query.py"] + list(argv)):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit as done:
                code = done.code if isinstance(done.code, int) else 0
    text = buf.getvalue().strip()
    if text:
        payload = json.loads(text)
    return (code, payload)


def _router_mod(tmp_path, monkeypatch):
    import importlib.util
    home = str(tmp_path / "rhome")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("ERPCLAW_HOME", home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("ERPCLAW_TEST_SESSION", raising=False)
    monkeypatch.delenv("ERPCLAW_ACTOR_CONTEXT", raising=False)
    lib = os.path.join(os.path.dirname(_SI_MODULE), "erpclaw-setup", "lib")
    lib = os.path.abspath(lib)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    name = "erpclaw_router_seg_%s" % abs(hash(home))
    spec = importlib.util.spec_from_file_location(name, _ROUTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _via_router(argv, tmp_path, monkeypatch):
    from unittest.mock import patch
    router = _router_mod(tmp_path, monkeypatch)

    def _fake_execvp(exe, args):
        raise _Forwarded(list(args))

    buf = io.StringIO()
    with patch.object(sys, "argv", ["db_query.py"] + list(argv)):
        with patch("sys.stdout", buf):
            with patch.object(router.os, "execvp", _fake_execvp):
                try:
                    router.main()
                except _Forwarded as moved:
                    forwarded = moved.argv
                    rest = forwarded[2:]
                    return _direct(rest)
                except SystemExit as done:
                    code = done.code if isinstance(done.code, int) else 0
                    text = buf.getvalue().strip()
                    payload = json.loads(text) if text else {}
                    return (code, payload)
    text = buf.getvalue().strip()
    payload = json.loads(text) if text else {}
    return (0, payload)


def _update_item(path, item_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("sales_invoice_item")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(item_id)
        handle.execute(query.get_sql(), params)
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


def _patch_ready(monkeypatch):
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)


def _patch_actor(monkeypatch):
    import authority_fixtures as fx
    from erpclaw_lib import actor
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(
        None, None, fx.SERVICE, (), actor.ATTESTED))


def _gl_rows(path, si_id):
    import test_payment_envelope_gate as pgate
    cols = _seam().column_names("gl_entry", path)
    rows = pgate._read_all(path, "gl_entry", cols)
    return (cols, sorted((row for row in rows if row["voucher_id"] == si_id),
                         key=lambda row: row["id"]))


def _check_cancelled(path, si_id, before):
    _cols, after = _gl_rows(path, si_id)
    before_ids = {row["id"] for row in before}
    assert len(after) == 2 * len(before)
    fresh = [row for row in after if row["id"] not in before_ids]
    assert len(fresh) == len(before)
    after_by_id = {row["id"]: row for row in after}
    for row in before:
        kept = dict(after_by_id[row["id"]])
        assert kept.pop("is_cancelled") == 1
        wanted = dict(row)
        assert wanted.pop("is_cancelled") == 0
        assert kept == wanted
    for row in fresh:
        assert row["is_cancelled"] == 1
    assert sorted((row["debit"], row["credit"]) for row in fresh) == sorted(
        (row["credit"], row["debit"]) for row in before)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam = _seam()
    seam.dispose_engines()


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    import test_payment_envelope_gate as pgate
    path = pgate._fresh_db(tmp_path, monkeypatch, "selling-gate")
    handle = _open(path)
    try:
        env = _helpers().build_selling_env(handle)
    finally:
        handle.close()
    return (path, env)


def test_staged_flows_unchanged(db_env, tmp_path, monkeypatch):
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    code, payload = _via_router(
        ["--action", "submit-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si], tmp_path, monkeypatch)
    assert code == 2
    assert payload.get("error") == "user_confirmation_required"
    code, payload = _via_router(
        ["--action", "submit-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si, "--user-confirmed"],
        tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    from decimal import Decimal as _D
    legs = [row for row in pgate._read_all(
        path, "gl_entry", ["voucher_id", "account_id", "debit", "credit"])
        if row["voucher_id"] == si]
    assert len(legs) >= 2
    assert sum((_D(row["debit"]) for row in legs),
               _D("0")) == sum((_D(row["credit"]) for row in legs), _D("0"))
    assert _D("100.00") in [_D(row["debit"]) for row in legs
                            if row["account_id"] == env["ar"]]
    assert pgate._read_all(path, "operation_authorization_result",
                           ["authorization_id"]) == []
    audits = pgate._read_all(path, "audit_log", ["authorization_id"])
    assert [row for row in audits if row["authorization_id"]] == []
    code, payload = _direct(
        ["--action", "get-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_active_without_envelope_refuses(db_env, tmp_path, monkeypatch, via):
    """Refusal without envelope at ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    import authority_fixtures as fx
    path, env = db_env
    si = sproj._si(path, env)
    fx.make_active(path)
    _patch_ready(monkeypatch)
    if via == "direct":
        argv = sproj._std("submit-sales-invoice", path, si)
        before = pgate._snapshot(path)
        code, payload = _direct(argv)
    else:
        argv = sproj._std("submit-sales-invoice", path, si,
                          ["--user-confirmed"])
        before = pgate._snapshot(path)
        code, payload = _via_router(argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REQUIRED"
    assert "issue-authorization" in payload.get("suggestion", "")
    assert pgate._snapshot(path) == before
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = pgate._snapshot(path)
    if via == "direct":
        code, payload = _direct(sproj._std("submit-sales-invoice", path, si))
    else:
        code, payload = _via_router(
            sproj._std("submit-sales-invoice", path, si,
                       ["--user-confirmed"]),
            tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "list-sales-invoices", "--db-path", path,
         "--company", "No Such Company"])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert pgate._snapshot(path) == before
    _patch_ready(monkeypatch)
    from erpclaw_lib import actor as _absent_actor
    monkeypatch.setattr(_absent_actor, "current", lambda: _absent_actor.ActorContext(None, None, None, (), _absent_actor.ABSENT))
    _absent_before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "get-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si])
    assert code == 1, payload
    assert payload.get("message") == "COMPANY_SCOPE_REFUSED"
    assert pgate._snapshot(path) == _absent_before
    fx.seed_authority(path, env["company_id"])
    sproj._SEEDED.add(path)
    _patch_actor(monkeypatch)
    code, payload = _direct(
        ["--action", "get-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si])
    assert code == 0, payload
    import json as _json
    items = _json.dumps([
        {"item_id": env["item1"], "qty": "1", "rate": "10.00",
         "warehouse_id": env["warehouse"]},
    ])
    code, payload = _direct(
        ["--action", "create-sales-invoice", "--db-path", path,
         "--company-id", env["company_id"], "--customer-id", env["customer"],
         "--posting-date", "2026-06-20", "--items", items])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_valid_envelope_consumes(db_env, tmp_path, monkeypatch, via):
    """Consume one envelope at ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued = pgate._issue_full(path, "submit-sales-invoice",
                               sproj._std("submit-sales-invoice", path, si))
    assert issued["issued_route"] == "delegation"
    auth_id = issued["authorization_id"]
    if via == "direct":
        code, payload = _direct(
            sproj._std("submit-sales-invoice", path, si,
                       ["--authorization-id", auth_id]))
    else:
        code, payload = _via_router(
            sproj._std("submit-sales-invoice", path, si,
                       ["--user-confirmed", "--authorization-id", auth_id]),
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry = pgate._read_one(path, "sales_invoice", ["id", "status"], si)
    assert entry["status"] == "submitted"
    legs = [row for row in pgate._read_all(
        path, "gl_entry", ["voucher_id"]) if row["voucher_id"] == si]
    assert len(legs) >= 2
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is not None
    result = pgate._result_row(path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("sales-invoice", si, "submitted")
    audits = [row for row in pgate._read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == auth_id]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    usage = pgate._read_all(path, "authority_delegation_usage", ["used"])
    assert "100.00" in [row["used"] for row in usage]


def test_valid_envelope_readiness_unpatched(db_env, monkeypatch):
    """Unready ACTIVE refuses before spend; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-sales-invoice",
                           sproj._std("submit-sales-invoice", path, si))
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = pgate._snapshot(path)
    code, payload = _direct(
        sproj._std("submit-sales-invoice", path, si,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert pgate._snapshot(path) == before


def test_install_id_mismatch(db_env, monkeypatch):
    """Foreign install refuses; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-sales-invoice",
                           sproj._std("submit-sales-invoice", path, si))
    from erpclaw_lib import authority_gate
    monkeypatch.setattr(authority_gate, "install_phase",
                        lambda conn: ("ACTIVE", "other-install"))
    before = pgate._snapshot(path)
    code, payload = _direct(
        sproj._std("submit-sales-invoice", path, si,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert pgate._snapshot(path) == before


def test_state_digest_binds_the_invoice(db_env):
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    auth_id = pgate._issue(path, "submit-sales-invoice",
                           sproj._std("submit-sales-invoice", path, si))
    base = sproj._std("submit-sales-invoice", path, si,
                      ["--authorization-id", auth_id])
    head = pgate._read_one(path, "sales_invoice",
                           ["id", "grand_total", "posting_date", "due_date",
                            "update_stock", "dimensions_json"], si)
    item_rows = sorted(
        (row for row in pgate._read_all(
            path, "sales_invoice_item",
            ["id", "sales_invoice_id", "quantity", "rate", "project_id"])
         if row["sales_invoice_id"] == si),
        key=lambda row: row["id"])
    assert len(item_rows) == 2
    first = dict(item_rows[0])
    second = dict(item_rows[1])

    def _try(argv):
        before = pgate._snapshot(path)
        code, payload = _direct(argv)
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REFUSED"
        auth = pgate._read_one(path, "operation_authorization",
                               ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is None
        assert pgate._snapshot(path) == before

    sproj._update_si(path, si, {"grand_total": "90.00"})
    _try(base)
    sproj._update_si(path, si, {"grand_total": head["grand_total"]})
    sproj._update_si(path, si, {"posting_date": "2026-07-01"})
    _try(base)
    sproj._update_si(path, si, {"posting_date": head["posting_date"]})
    sproj._update_si(path, si, {"due_date": "2026-08-15"})
    _try(base)
    sproj._update_si(path, si, {"due_date": head["due_date"]})
    flipped = 0 if str(head["update_stock"]) == "1" else 1
    sproj._update_si(path, si, {"update_stock": flipped})
    _try(base)
    sproj._update_si(path, si, {"update_stock": head["update_stock"]})
    sproj._update_si(path, si, {"dimensions_json": '{"zone": "north"}'})
    _try(base)
    sproj._update_si(path, si, {"dimensions_json": head["dimensions_json"]})
    _update_item(path, first["id"], {"quantity": "99"})
    _try(base)
    _update_item(path, first["id"], {"quantity": first["quantity"]})
    _update_item(path, first["id"], {"rate": "9.00"})
    _try(base)
    _update_item(path, first["id"], {"rate": first["rate"]})
    _update_item(path, second["id"], {"project_id": str(uuid.uuid4())})
    _try(base)
    _update_item(path, second["id"], {"project_id": second["project_id"]})
    _try(base + ["--due-date", "2026-08-01"])
    code, payload = _direct(base)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def test_cancel_through_the_gate(db_env, monkeypatch):
    """Cancel consumes at STAGED and ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    code, payload = _direct(sproj._std("submit-sales-invoice", path, si))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    _cols, before = _gl_rows(path, si)
    assert len(before) >= 2
    sproj._grant(path, env["company_id"], si, "cancel-sales-invoice")
    cancel_id = pgate._issue(path, "cancel-sales-invoice",
                             sproj._std("cancel-sales-invoice", path, si))
    code, payload = _direct(
        sproj._std("cancel-sales-invoice", path, si,
                   ["--authorization-id", cancel_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = pgate._result_row(path, cancel_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("sales-invoice", si, "cancelled")
    entry = pgate._read_one(path, "sales_invoice", ["id", "status"], si)
    assert entry["status"] == "cancelled"
    _check_cancelled(path, si, before)
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    other = sproj._si(path, env)
    sproj._grant(path, env["company_id"], other, "submit-sales-invoice")
    submit_id = pgate._issue(path, "submit-sales-invoice",
                             sproj._std("submit-sales-invoice", path, other))
    code, payload = _direct(
        sproj._std("submit-sales-invoice", path, other,
                   ["--authorization-id", submit_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before_snap = pgate._snapshot(path)
    code, payload = _direct(sproj._std("cancel-sales-invoice", path, other))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REQUIRED"
    assert pgate._snapshot(path) == before_snap


def test_replay_after_submit(db_env, monkeypatch):
    """Replay returns stored result; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-sales-invoice",
                           sproj._std("submit-sales-invoice", path, si))
    argv = sproj._std("submit-sales-invoice", path, si,
                      ["--authorization-id", auth_id])
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before = pgate._snapshot(path)
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("replayed") is True
    assert payload.get("authorization_id") == auth_id
    assert payload.get("result_kind") == "sales-invoice"
    assert payload.get("result_id") == si
    assert payload.get("result_status") == "submitted"
    assert pgate._snapshot(path) == before


def test_unprojected_and_non_envelope_actions(db_env, monkeypatch):
    """Unprojected refuses with or without id; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    si = sproj._si(path, env)
    sproj._grant(path, env["company_id"], si, "submit-sales-invoice")
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="delete-sales-invoice",
                argv=sproj._std("delete-sales-invoice", path, si),
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    assert "IMPACT_UNDECLARED" in excinfo.value.args
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    before = pgate._snapshot(path)
    code, payload = _direct(sproj._std("delete-sales-invoice", path, si))
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "submit-sales-order", "--db-path", path,
         "--sales-order-id", "no-such"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "generate-recurring-invoices", "--db-path", path,
         "--company-id", env["company_id"], "--as-of-date", "2026-06-20"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    submit_id = pgate._issue(path, "submit-sales-invoice",
                             sproj._std("submit-sales-invoice", path, si))
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "get-sales-invoice", "--db-path", path,
         "--sales-invoice-id", si, "--authorization-id", submit_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], submit_id)
    assert auth["consumed_at"] is None
    assert pgate._snapshot(path) == before


@pytest.mark.parametrize("via", ["direct", "router"])
def test_argv_spelling_and_forms(db_env, tmp_path, monkeypatch, via):
    """Option spellings gate before any read; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    path, env = db_env
    entry_s = sproj._si(path, env)
    if via == "direct":
        code, payload = _direct(
            ["--action", "submit-sales-invoice", "--db-path", path,
             "--sales-invoice", entry_s])
    else:
        code, payload = _via_router(
            ["--action", "submit-sales-invoice", "--db-path", path,
             "--sales-invoice", entry_s, "--user-confirmed"],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_a = sproj._si(path, env)
    entry_b = sproj._si(path, env)
    sproj._grant(path, env["company_id"], entry_a, "submit-sales-invoice")
    sproj._grant(path, env["company_id"], entry_b, "submit-sales-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued_argv = ["--action", "submit-sales-invoice", "--db-path", path,
                   "--sales-invoice-id", entry_a,
                   "--sales-invoice", entry_b]
    auth_id = pgate._issue(path, "submit-sales-invoice", issued_argv)
    from erpclaw_lib import authority_gate as _gate_abbrev
    _real_phase = _gate_abbrev.install_phase

    def _must_not_read(conn):
        raise AssertionError("refusal happens before any read")

    monkeypatch.setattr(_gate_abbrev, "install_phase", _must_not_read)
    consume_argv = ["--action", "submit-sales-invoice", "--db-path", path,
                    "--sales-invoice-id", entry_a,
                    "--sales-invoice", entry_b,
                    "--authorization-id", auth_id]
    before = pgate._snapshot(path)
    if via == "direct":
        code, payload = _direct(consume_argv)
    else:
        router_argv = ["--action", "submit-sales-invoice", "--db-path", path,
                       "--sales-invoice-id", entry_a,
                       "--sales-invoice", entry_b,
                       "--user-confirmed", "--authorization-id", auth_id]
        code, payload = _via_router(router_argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._read_one(
        path, "sales_invoice", ["id", "status"],
        entry_a)["status"] == "draft"
    assert pgate._read_one(
        path, "sales_invoice", ["id", "status"],
        entry_b)["status"] == "draft"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           auth_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    monkeypatch.setattr(_gate_abbrev, "install_phase", _real_phase)
    entry_c = sproj._si(path, env)
    sproj._grant(path, env["company_id"], entry_c, "submit-sales-invoice")
    company = env["company_id"]
    issued_dup = ["--action", "submit-sales-invoice", "--db-path", path,
                  "--sales-invoice-id", entry_c,
                  "--company-id", company, "--company-id", company]
    dup_id = pgate._issue(path, "submit-sales-invoice", issued_dup)
    before = pgate._snapshot(path)
    code, payload = _direct(issued_dup + ["--authorization-id", dup_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._read_one(
        path, "sales_invoice", ["id", "status"],
        entry_c)["status"] == "draft"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           dup_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    entry_d = sproj._si(path, env)
    sproj._grant(path, env["company_id"], entry_d, "submit-sales-invoice")
    dim_argv = ["--action", "submit-sales-invoice", "--db-path", path,
                "--sales-invoice-id", entry_d, "--dimension-key", "a",
                "--dimension-key", "b", "--dimension-value", "x",
                "--dimension-value", "y"]
    dim_id = pgate._issue(path, "submit-sales-invoice", dim_argv)
    if via == "direct":
        code, payload = _direct(dim_argv + ["--authorization-id", dim_id])
    else:
        code, payload = _via_router(
            dim_argv + ["--user-confirmed", "--authorization-id", dim_id],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_e = sproj._si(path, env)
    sproj._grant(path, env["company_id"], entry_e, "submit-sales-invoice")
    eq_id = pgate._issue(path, "submit-sales-invoice",
                         sproj._std("submit-sales-invoice", path, entry_e))
    argv = ["--action", "submit-sales-invoice", "--db-path", path,
            "--sales-invoice-id=" + entry_e,
            "--authorization-id", eq_id]
    if via == "direct":
        code, payload = _direct(argv)
    else:
        code, payload = _via_router(
            ["--action", "submit-sales-invoice", "--db-path", path,
             "--sales-invoice-id=" + entry_e,
             "--user-confirmed", "--authorization-id", eq_id],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_f = sproj._si(path, env)
    before = pgate._snapshot(path)
    code, payload = _direct(
        sproj._std("submit-sales-invoice", path, entry_f,
                   ["--authorization-id", "a-1", "--authorization-id", "b-2"]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._snapshot(path) == before


def test_cross_company_cancel_at_active(tmp_path, monkeypatch):
    """Cross scope cancel stays closed while same scope passes; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    import authority_fixtures as fx
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    path = pgate._fresh_db(tmp_path, monkeypatch, "selling-scope")
    handle = get_connection(path)
    try:
        env_a = _helpers().build_selling_env(handle)
        env_b = _helpers().build_selling_env(handle)
    finally:
        handle.close()
    comp_a = env_a["company_id"]
    comp_b = env_b["company_id"]
    handle = get_connection(path)
    try:
        mod = _helpers().load_db_query()
        import json as _json

        def _draft(env):
            items = _json.dumps([
                {"item_id": env["item1"], "qty": "10", "rate": "6.00",
                 "warehouse_id": env["warehouse"]},
                {"item_id": env["item2"], "qty": "2", "rate": "20.00",
                 "warehouse_id": env["warehouse"]},
            ])
            out = _helpers().call_action(
                mod.create_sales_invoice, handle,
                _helpers().ns(sales_order_id=None, delivery_note_id=None,
                              customer_id=env["customer"],
                              company_id=env["company_id"],
                              posting_date="2026-06-20",
                              due_date="2026-07-20", items=items,
                              tax_template_id=None, payment_terms_id=None))
            assert out.get("status") == "ok", out
            return out["sales_invoice_id"]

        def _submit(si):
            out = _helpers().call_action(
                mod.submit_sales_invoice, handle,
                _helpers().ns(sales_invoice_id=si))
            assert out.get("status") == "ok", out
            return out

        draft_a = _draft(env_a)
        draft_b = _draft(env_b)
        _submit(draft_a)
        _submit(draft_b)
    finally:
        handle.close()
    from erpclaw_lib import seam as _seam
    _seam.dispose_engines()
    sproj._grant(path, comp_a, draft_a, "cancel-sales-invoice")
    sproj._grant(path, comp_b, draft_b, "cancel-sales-invoice")
    _allow_member(path, fx.SERVICE, comp_b, "allow")
    _allow_member(path, fx.OWNER, comp_b, "allow")
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    auth_b = pgate._issue(path, "cancel-sales-invoice",
                          ["--action", "cancel-sales-invoice",
                           "--db-path", path,
                           "--sales-invoice-id", draft_b])
    _allow_member(path, fx.SERVICE, comp_b, "deny")
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "cancel-sales-invoice", "--db-path", path,
         "--sales-invoice-id", draft_b, "--authorization-id", auth_b])
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    assert pgate._snapshot(path) == before
    entry_b = pgate._read_one(path, "sales_invoice", ["id", "status"], draft_b)
    assert entry_b["status"] == "submitted"
    auth_row = pgate._read_one(path, "operation_authorization",
                               ["id", "consumed_at"], auth_b)
    assert auth_row["consumed_at"] is None
    auth_a = pgate._issue(path, "cancel-sales-invoice",
                          ["--action", "cancel-sales-invoice",
                           "--db-path", path,
                           "--sales-invoice-id", draft_a])
    code, payload = _direct(
        ["--action", "cancel-sales-invoice", "--db-path", path,
         "--sales-invoice-id", draft_a, "--authorization-id", auth_a])
    assert code == 0, payload
    entry_a = pgate._read_one(path, "sales_invoice", ["id", "status"], draft_a)
    assert entry_a["status"] == "cancelled"
    rows = pgate._read_all(path, "audit_log",
                           ["action", "entity_id", "scope_status",
                            "scope_company_ids"])
    found = [row for row in rows if row["action"] == "cancel-sales-invoice"
             and row["entity_id"] == draft_a]
    assert len(found) >= 1
    last = found[-1]
    assert last["scope_status"] == "in_scope"
    assert last["scope_company_ids"] == comp_a


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres leg mirrors the envelope flow; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_sales_invoice_projection as sproj
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    sproj._SEEDED.clear()
    sproj._SEED_CAPS.clear()
    seam = _seam()
    seam.dispose_engines()
    try:
        pgate._pg_reset_schema()
        import setup_helpers as setup
        setup.init_all_tables(None)
        handle = _open(None)
        try:
            env = _helpers().build_selling_env(handle)
        finally:
            handle.close()
        sproj._SEEDED.clear()
        sproj._SEED_CAPS.clear()
        import authority_fixtures as fx
        company = env["company_id"]
        si = sproj._si(None, env)
        sproj._grant(None, company, si, "submit-sales-invoice")
        fx.make_active(None)
        _patch_ready(monkeypatch)
        _patch_actor(monkeypatch)
        issued = pgate._issue_full(None, "submit-sales-invoice",
                                   ["--action", "submit-sales-invoice",
                                    "--sales-invoice-id", si])
        assert issued["issued_route"] == "delegation"
        auth_id = issued["authorization_id"]
        code, payload = _direct(
            ["--action", "submit-sales-invoice",
             "--sales-invoice-id", si,
             "--authorization-id", auth_id])
        assert code == 0, payload
        assert payload.get("document_status") == "submitted"
        entry = pgate._read_one(None, "sales_invoice", ["id", "status"], si)
        assert entry["status"] == "submitted"
        auth = pgate._read_one(None, "operation_authorization",
                               ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is not None
        result = pgate._result_row(None, auth_id)
        assert (result["result_kind"], result["result_id"],
                result["result_status"]) == ("sales-invoice", si,
                                             "submitted")
        other = sproj._si(None, env)
        sproj._grant(None, company, other, "submit-sales-invoice")
        before = pgate._snapshot(None)
        code, payload = _direct(
            ["--action", "submit-sales-invoice",
             "--sales-invoice-id", other])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REQUIRED"
        assert pgate._snapshot(None) == before
    finally:
        sproj._SEEDED.clear()
        sproj._SEED_CAPS.clear()
        seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
