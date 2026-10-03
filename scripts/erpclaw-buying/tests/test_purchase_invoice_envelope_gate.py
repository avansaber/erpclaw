"""Buying submit and cancel behind single-use authorization."""
import io
import json
import os
import sys
import uuid

import pytest

_PI_TESTS = os.path.dirname(os.path.abspath(__file__))
_PI_MODULE = os.path.dirname(_PI_TESTS)
_PI_SETUP_TESTS = os.path.join(os.path.dirname(_PI_MODULE), "erpclaw-setup", "tests")
_PI_PAYMENTS_TESTS = os.path.join(
    os.path.dirname(_PI_MODULE), "erpclaw-payments", "tests")
if _PI_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _PI_SETUP_TESTS)
if _PI_PAYMENTS_TESTS not in sys.path:
    sys.path.insert(0, _PI_PAYMENTS_TESTS)
if _PI_TESTS not in sys.path:
    sys.path.insert(0, _PI_TESTS)

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


class _Forwarded(Exception):
    def __init__(self, argv):
        super().__init__("forwarded")
        self.argv = list(argv)


def _helpers():
    import buying_helpers as helpers
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


def _via_router(argv, tmp_path, monkeypatch):
    import test_payment_envelope_gate as pgate
    from unittest.mock import patch
    router = pgate._router_mod(tmp_path, monkeypatch)

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


def _gl_rows(path, pi_id):
    import test_payment_envelope_gate as pgate
    cols = _seam().column_names("gl_entry", path)
    rows = pgate._read_all(path, "gl_entry", cols)
    return (cols, sorted((row for row in rows if row["voucher_id"] == pi_id),
                         key=lambda row: row["id"]))


def _check_cancelled(path, pi_id, before):
    _cols, after = _gl_rows(path, pi_id)
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
    path = pgate._fresh_db(tmp_path, monkeypatch, "buying-gate")
    handle = _open(path)
    try:
        env = _helpers().build_buying_env(handle)
    finally:
        handle.close()
    return (path, env)


def test_staged_flows_unchanged(db_env, tmp_path, monkeypatch):
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    code, payload = _via_router(
        ["--action", "submit-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi], tmp_path, monkeypatch)
    assert code == 2
    assert payload.get("error") == "user_confirmation_required"
    code, payload = _via_router(
        ["--action", "submit-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi, "--user-confirmed"],
        tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    from decimal import Decimal as _D
    legs = [row for row in pgate._read_all(
        path, "gl_entry",
        ["voucher_id", "voucher_type", "account_id", "debit", "credit"])
        if row["voucher_id"] == pi]
    assert len(legs) >= 2
    assert all(row["voucher_type"] == "purchase_invoice" for row in legs)
    assert sum((_D(row["debit"]) for row in legs),
               _D("0")) == sum((_D(row["credit"]) for row in legs), _D("0"))
    ap_legs = [row for row in legs if row["account_id"] == env["ap"]]
    assert len(ap_legs) == 1
    assert _D(ap_legs[0]["credit"]) == _D("100.00")
    assert pgate._read_all(path, "operation_authorization_result",
                           ["authorization_id"]) == []
    audits = pgate._read_all(path, "audit_log", ["authorization_id"])
    assert [row for row in audits if row["authorization_id"]] == []
    code, payload = _direct(
        ["--action", "get-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_active_without_envelope_refuses(db_env, tmp_path, monkeypatch, via):
    """Refusal without envelope at ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    import authority_fixtures as fx
    path, env = db_env
    pi = pproj._pi(path, env)
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    if via == "direct":
        argv = pproj._std("submit-purchase-invoice", path, pi)
        before = pgate._snapshot(path)
        code, payload = _direct(argv)
    else:
        argv = pproj._std("submit-purchase-invoice", path, pi,
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
        code, payload = _direct(
            pproj._std("submit-purchase-invoice", path, pi))
    else:
        code, payload = _via_router(
            pproj._std("submit-purchase-invoice", path, pi,
                       ["--user-confirmed"]),
            tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "list-purchase-invoices", "--db-path", path,
         "--company", "No Such Company"])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert pgate._snapshot(path) == before
    pgate._patch_ready(monkeypatch)
    from erpclaw_lib import actor as _absent_actor
    monkeypatch.setattr(_absent_actor, "current", lambda: _absent_actor.ActorContext(None, None, None, (), _absent_actor.ABSENT))
    _absent_before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "get-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi])
    assert code == 1, payload
    assert payload.get("message") == "COMPANY_SCOPE_REFUSED"
    assert pgate._snapshot(path) == _absent_before
    fx.seed_authority(path, env["company_id"])
    pproj._SEEDED.add(path)
    pgate._patch_actor(monkeypatch)
    code, payload = _direct(
        ["--action", "get-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi])
    assert code == 0, payload
    import json as _json
    items = _json.dumps([
        {"item_id": env["item1"], "qty": "1", "rate": "10.00",
         "warehouse_id": env["warehouse"]},
    ])
    code, payload = _direct(
        ["--action", "create-purchase-invoice", "--db-path", path,
         "--company-id", env["company_id"], "--supplier-id", env["supplier"],
         "--posting-date", "2026-06-20", "--items", items])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_valid_envelope_consumes(db_env, tmp_path, monkeypatch, via):
    """Consume one envelope at ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    issued = pgate._issue_full(path, "submit-purchase-invoice",
                               pproj._std("submit-purchase-invoice", path, pi))
    assert issued["issued_route"] == "delegation"
    auth_id = issued["authorization_id"]
    if via == "direct":
        code, payload = _direct(
            pproj._std("submit-purchase-invoice", path, pi,
                       ["--authorization-id", auth_id]))
    else:
        code, payload = _via_router(
            pproj._std("submit-purchase-invoice", path, pi,
                       ["--user-confirmed", "--authorization-id", auth_id]),
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry = pgate._read_one(path, "purchase_invoice", ["id", "status"], pi)
    assert entry["status"] == "submitted"
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is not None
    result = pgate._result_row(path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", pi, "submitted")
    audits = [row for row in pgate._read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == auth_id]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    usage = [row for row in pgate._read_all(
        path, "authority_delegation_usage",
        ["delegation_id", "action", "currency", "used"])
        if row["delegation_id"] == fx.DELEGATION
        and row["action"] == "submit-purchase-invoice"
        and row["currency"] == "USD"]
    assert len(usage) == 1
    assert usage[0]["used"] == "100.00"


def test_valid_envelope_readiness_unpatched(db_env, monkeypatch):
    """Unready ACTIVE refuses before spend; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           pproj._std("submit-purchase-invoice", path, pi))
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = pgate._snapshot(path)
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, pi,
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
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           pproj._std("submit-purchase-invoice", path, pi))
    from erpclaw_lib import authority_gate
    monkeypatch.setattr(authority_gate, "install_phase",
                        lambda conn: ("ACTIVE", "other-install"))
    before = pgate._snapshot(path)
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, pi,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert pgate._snapshot(path) == before


def test_state_digest_binds_the_invoice(db_env):
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    other = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           pproj._std("submit-purchase-invoice", path, pi))
    base = pproj._std("submit-purchase-invoice", path, pi,
                      ["--authorization-id", auth_id])

    def _try(argv):
        before = pgate._snapshot(path)
        code, payload = _direct(argv)
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REFUSED"
        auth = pgate._read_one(path, "operation_authorization",
                               ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is None
        assert pgate._snapshot(path) == before

    before = pgate._snapshot(path)
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, other,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"], pi)["status"] == "draft"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        other)["status"] == "draft"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           auth_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    head = pgate._read_one(path, "purchase_invoice",
                           ["id", "grand_total", "posting_date", "due_date",
                            "update_stock", "dimensions_json"], pi)
    item_rows = sorted(
        (row for row in pgate._read_all(
            path, "purchase_invoice_item",
            ["id", "purchase_invoice_id", "quantity", "rate",
             "discount_amount", "expense_account_id"])
         if row["purchase_invoice_id"] == pi),
        key=lambda row: row["id"])
    assert len(item_rows) == 2
    first = dict(item_rows[0])
    second = dict(item_rows[1])
    pproj._update_pi(path, pi, {"grand_total": "90.00"})
    _try(base)
    pproj._update_pi(path, pi, {"grand_total": head["grand_total"]})
    pproj._update_pi(path, pi, {"posting_date": "2026-07-01"})
    _try(base)
    pproj._update_pi(path, pi, {"posting_date": head["posting_date"]})
    pproj._update_pi(path, pi, {"due_date": "2026-08-15"})
    _try(base)
    pproj._update_pi(path, pi, {"due_date": head["due_date"]})
    pproj._update_pi(path, pi, {"update_stock": 0})
    _try(base)
    pproj._update_pi(path, pi, {"update_stock": head["update_stock"]})
    pproj._update_pi(path, pi, {"dimensions_json": '{"zone": "north"}'})
    _try(base)
    pproj._update_pi(path, pi, {"dimensions_json": head["dimensions_json"]})
    pproj._update_item(path, first["id"], {"quantity": "99"})
    _try(base)
    pproj._update_item(path, first["id"], {"quantity": first["quantity"]})
    pproj._update_item(path, first["id"], {"rate": "9.00"})
    _try(base)
    pproj._update_item(path, first["id"], {"rate": first["rate"]})
    pproj._update_item(path, first["id"], {"discount_amount": "5.00"})
    _try(base)
    pproj._update_item(
        path, first["id"], {"discount_amount": first["discount_amount"]})
    pproj._update_item(
        path, second["id"], {"expense_account_id": env["cogs"]})
    _try(base)
    pproj._update_item(
        path, second["id"],
        {"expense_account_id": second["expense_account_id"]})
    _try(base + ["--due-date", "2026-08-01"])
    code, payload = _direct(base)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def _disc_po_items(env):
    return json.dumps([{"item_id": env["item1"], "qty": "3", "rate": "10.00",
                        "discount_amount": "1.00",
                        "warehouse_id": env["warehouse"]}])


def _confirmed_po_id(mod, helpers, handle, env):
    po = helpers.call_action(
        mod.add_purchase_order, handle,
        helpers.ns(supplier_id=env["supplier"],
                   company_id=env["company_id"],
                   posting_date="2026-06-15",
                   items=_disc_po_items(env), tax_template_id=None,
                   name=None))
    assert po.get("status") == "ok", po
    submit = helpers.call_action(
        mod.submit_purchase_order, handle,
        helpers.ns(purchase_order_id=po["purchase_order_id"]))
    assert submit.get("status") == "ok", submit
    return po["purchase_order_id"]


def _receipt_id(mod, helpers, handle, env, po_id, qty="3"):
    from erpclaw_lib.query import Field, P, Q, Table
    table = Table("purchase_order_item")
    query = Q.from_(table).select(table.id).where(
        table.purchase_order_id == P())
    row = handle.execute(query.get_sql(), (po_id,)).fetchone()
    assert row is not None
    poi_id = dict(row)["id"]
    items = json.dumps([{"purchase_order_item_id": poi_id, "qty": qty}])
    res = helpers.call_action(
        mod.create_purchase_receipt, handle,
        helpers.ns(purchase_order_id=po_id, company_id=env["company_id"],
                   posting_date="2026-06-20", items=items,
                   purchase_receipt_id=None))
    assert res.get("status") == "ok", res
    sub = helpers.call_action(
        mod.submit_purchase_receipt, handle,
        helpers.ns(purchase_receipt_id=res["purchase_receipt_id"]))
    assert sub.get("status") == "ok", sub
    return res["purchase_receipt_id"]


def _bill_from_order_id(mod, helpers, handle, env, po_id):
    res = helpers.call_action(
        mod.create_purchase_invoice, handle,
        helpers.ns(purchase_order_id=po_id, purchase_receipt_id=None,
                   supplier_id=None, company_id=env["company_id"],
                   posting_date="2026-06-21", due_date=None, items=None,
                   tax_template_id=None))
    assert res.get("status") == "ok", res
    return res["purchase_invoice_id"]


def test_order_linked_bill_with_stale_discount(db_env):
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    import authority_fixtures as fx
    from erpclaw_lib.db import get_connection
    path, env = db_env
    helpers = _helpers()
    mod = helpers.load_db_query()
    handle = get_connection(path)
    try:
        po_id = _confirmed_po_id(mod, helpers, handle, env)
        _receipt_id(mod, helpers, handle, env, po_id, "3")
        bill = _bill_from_order_id(mod, helpers, handle, env, po_id)
    finally:
        handle.close()
    lines = [row for row in pgate._read_all(
        path, "purchase_invoice_item",
        ["id", "purchase_invoice_id", "discount_amount"])
        if row["purchase_invoice_id"] == bill]
    assert len(lines) == 1
    assert lines[0]["discount_amount"] == "1.00"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "grand_total"],
        bill)["grand_total"] == "29.00"
    line_id = lines[0]["id"]
    pproj._update_item(path, line_id, {"discount_amount": "0.50"})
    pproj._update_pi(path, bill, {"total_amount": "29.50",
                                 "grand_total": "29.50",
                                 "outstanding_amount": "29.50"})
    pproj._grant(path, env["company_id"], bill, "submit-purchase-invoice")
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           pproj._std("submit-purchase-invoice", path, bill))
    before = pgate._snapshot(path)
    usage_before = pgate._read_all(
        path, "authority_delegation_usage",
        ["delegation_id", "action", "currency", "used"])
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, bill,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           auth_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    stale_lines = [row for row in pgate._read_all(
        path, "purchase_invoice_item",
        ["id", "purchase_invoice_id", "discount_amount"])
        if row["purchase_invoice_id"] == bill]
    assert len(stale_lines) == 1
    assert stale_lines[0]["discount_amount"] == "0.50"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "grand_total"],
        bill)["grand_total"] == "29.50"
    assert pgate._read_all(
        path, "authority_delegation_usage",
        ["delegation_id", "action", "currency", "used"]) == usage_before
    handle = get_connection(path)
    try:
        po2 = _confirmed_po_id(mod, helpers, handle, env)
        _receipt_id(mod, helpers, handle, env, po2, "3")
        bill2 = _bill_from_order_id(mod, helpers, handle, env, po2)
    finally:
        handle.close()
    lines2 = [row for row in pgate._read_all(
        path, "purchase_invoice_item",
        ["id", "purchase_invoice_id", "discount_amount"])
        if row["purchase_invoice_id"] == bill2]
    assert len(lines2) == 1
    assert lines2[0]["discount_amount"] == "1.00"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "grand_total"],
        bill2)["grand_total"] == "29.00"
    pproj._grant(path, env["company_id"], bill2, "submit-purchase-invoice")
    auth2 = pgate._issue(path, "submit-purchase-invoice",
                         pproj._std("submit-purchase-invoice", path, bill2))
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, bill2,
                   ["--authorization-id", auth2]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    result = pgate._result_row(path, auth2)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", bill2,
                                         "submitted")
    usage = [row for row in pgate._read_all(
        path, "authority_delegation_usage",
        ["delegation_id", "action", "currency", "used"])
        if row["delegation_id"] == fx.DELEGATION
        and row["action"] == "submit-purchase-invoice"
        and row["currency"] == "USD"]
    assert len(usage) == 1
    assert usage[0]["used"] == "29.00"


def test_cancel_through_the_gate(db_env, monkeypatch):
    """Cancel consumes at STAGED and ACTIVE; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    import authority_fixtures as fx
    from decimal import Decimal as _D
    from erpclaw_lib.db import get_connection
    path, env = db_env
    helpers = _helpers()
    mod = helpers.load_db_query()

    def _used(action):
        rows = [row for row in pgate._read_all(
            path, "authority_delegation_usage",
            ["delegation_id", "action", "currency", "used"])
            if row["delegation_id"] == fx.DELEGATION
            and row["action"] == action and row["currency"] == "USD"]
        assert len(rows) == 1
        return rows[0]["used"]

    pi = pproj._pi(path, env)
    code, payload = _direct(pproj._std("submit-purchase-invoice", path, pi))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    _cols, before = _gl_rows(path, pi)
    assert len(before) >= 2
    pproj._grant(path, env["company_id"], pi, "cancel-purchase-invoice")
    cancel_id = pgate._issue(path, "cancel-purchase-invoice",
                             pproj._std("cancel-purchase-invoice", path, pi))
    code, payload = _direct(
        pproj._std("cancel-purchase-invoice", path, pi,
                   ["--authorization-id", cancel_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = pgate._result_row(path, cancel_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", pi, "cancelled")
    entry = pgate._read_one(path, "purchase_invoice", ["id", "status"], pi)
    assert entry["status"] == "cancelled"
    _check_cancelled(path, pi, before)
    assert _used("cancel-purchase-invoice") == "100.00"
    pi2 = pproj._pi(path, env)
    code, payload = _direct(pproj._std("submit-purchase-invoice", path, pi2))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    handle = get_connection(path)
    try:
        dn_res = helpers.call_action(
            mod.create_debit_note, handle,
            helpers.ns(against_invoice_id=pi2,
                       items=json.dumps([{"item_id": env["item2"],
                                          "qty": "1"}]),
                       posting_date="2026-06-25", reason="returned"))
    finally:
        handle.close()
    assert dn_res.get("status") == "ok", dn_res
    dn = dn_res["debit_note_id"]
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "grand_total"],
        dn)["grand_total"] == "-20.00"
    submit_rows = [row for row in pgate._read_all(
        path, "authority_delegation_usage",
        ["delegation_id", "action", "currency", "used"])
        if row["delegation_id"] == fx.DELEGATION
        and row["action"] == "submit-purchase-invoice"
        and row["currency"] == "USD"]
    submit_before = _D(submit_rows[0]["used"]) if submit_rows else _D("0.00")
    pproj._grant(path, env["company_id"], dn, "submit-purchase-invoice")
    sub_id = pgate._issue(path, "submit-purchase-invoice",
                          pproj._std("submit-purchase-invoice", path, dn))
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, dn,
                   ["--authorization-id", sub_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    result = pgate._result_row(path, sub_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", dn, "submitted")
    assert _D(_used("submit-purchase-invoice")) - submit_before == _D("20.00")
    assert _used("submit-purchase-invoice") == str(
        (submit_before + _D("20.00")).quantize(_D("0.00")))
    cancel_before = _used("cancel-purchase-invoice")
    assert cancel_before == "100.00"
    _cols, dn_before = _gl_rows(path, dn)
    assert len(dn_before) >= 2
    pproj._grant(path, env["company_id"], dn, "cancel-purchase-invoice")
    dn_cancel = pgate._issue(path, "cancel-purchase-invoice",
                             pproj._std("cancel-purchase-invoice", path, dn))
    code, payload = _direct(
        pproj._std("cancel-purchase-invoice", path, dn,
                   ["--authorization-id", dn_cancel]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = pgate._result_row(path, dn_cancel)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", dn, "cancelled")
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"], dn)["status"] == "cancelled"
    _cols, dn_after = _gl_rows(path, dn)
    dn_before_ids = {row["id"] for row in dn_before}
    assert len(dn_after) == 2 * len(dn_before)
    dn_fresh = [row for row in dn_after if row["id"] not in dn_before_ids]
    assert len(dn_fresh) == len(dn_before)
    dn_by_id = {row["id"]: row for row in dn_after}
    for row in dn_before:
        kept = dict(dn_by_id[row["id"]])
        assert kept.pop("is_cancelled") == 1
        wanted = dict(row)
        assert wanted.pop("is_cancelled") == 0
        assert kept == wanted
    for row in dn_fresh:
        assert row["voucher_type"] == "debit_note"
        assert row["is_cancelled"] == 1
    assert sorted((row["debit"], row["credit"]) for row in dn_fresh) == sorted(
        (row["credit"], row["debit"]) for row in dn_before)
    assert _D(_used("cancel-purchase-invoice")) - _D(cancel_before) == _D("20.00")
    assert _used("cancel-purchase-invoice") == "120.00"
    pi3 = pproj._pi(path, env)
    code, payload = _direct(pproj._std("submit-purchase-invoice", path, pi3))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    import payments_helpers as pay_helpers
    pay_mod = pay_helpers.load_db_query()
    handle = get_connection(path)
    try:
        allocs = json.dumps([{"voucher_type": "purchase_invoice",
                              "voucher_id": pi3,
                              "allocated_amount": "40.00"}])
        padd = pay_helpers.call_action(
            pay_mod.add_payment, handle,
            pay_helpers.ns(company_id=env["company_id"],
                           payment_type="pay", posting_date="2026-06-22",
                           party_type="supplier", party_id=env["supplier"],
                           paid_from_account=env["cash"],
                           paid_to_account=env["ap"], paid_amount="40.00",
                           exchange_rate=None, payment_currency=None,
                           reference_number=None, reference_date=None,
                           allocations=allocs, deductions=None))
        assert padd.get("payment_entry_id"), padd
        pay_id = padd["payment_entry_id"]
        psub = pay_helpers.call_action(
            pay_mod.submit_payment, handle,
            pay_helpers.ns(payment_entry_id=pay_id))
        assert psub.get("status") == "ok", psub
    finally:
        handle.close()
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"], pi3)["status"] == "partially_paid"
    assert _D(pgate._read_one(
        path, "purchase_invoice", ["id", "outstanding_amount"],
        pi3)["outstanding_amount"]) == _D("60.00")
    assert _D(pgate._read_one(
        path, "payment_entry", ["id", "unallocated_amount"],
        pay_id)["unallocated_amount"]) == _D("0")
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    pproj._grant(path, env["company_id"], pi3, "cancel-purchase-invoice")
    pi3_cancel = pgate._issue(path, "cancel-purchase-invoice",
                              pproj._std("cancel-purchase-invoice", path, pi3))
    code, payload = _direct(
        pproj._std("cancel-purchase-invoice", path, pi3,
                   ["--authorization-id", pi3_cancel]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = pgate._result_row(path, pi3_cancel)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("purchase-invoice", pi3, "cancelled")
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        pi3)["status"] == "cancelled"
    assert _D(pgate._read_one(
        path, "payment_entry", ["id", "unallocated_amount"],
        pay_id)["unallocated_amount"]) == _D("40.00")
    audits = [row for row in pgate._read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == pi3_cancel]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    before_snap = pgate._snapshot(path)
    code, payload = _direct(pproj._std("cancel-purchase-invoice", path, pi3))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REQUIRED"
    assert pgate._snapshot(path) == before_snap


def test_replay_after_submit(db_env, monkeypatch):
    """Replay returns stored result; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    other = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           pproj._std("submit-purchase-invoice", path, pi))
    argv = pproj._std("submit-purchase-invoice", path, pi,
                      ["--authorization-id", auth_id])
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before = pgate._snapshot(path)
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("replayed") is True
    assert payload.get("authorization_id") == auth_id
    assert payload.get("result_kind") == "purchase-invoice"
    assert payload.get("result_id") == pi
    assert payload.get("result_status") == "submitted"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, other,
                   ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        other)["status"] == "draft"
    assert pgate._snapshot(path) == before


def test_unprojected_and_non_envelope_actions(db_env, monkeypatch):
    """Unprojected refuses with or without id; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    pi = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], pi, "submit-purchase-invoice")
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="submit-purchase-order",
                argv=["--action", "submit-purchase-order", "--db-path", path,
                      "--purchase-order-id", "no-such"],
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    assert "IMPACT_UNDECLARED" in excinfo.value.args
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "submit-purchase-order", "--db-path", path,
         "--purchase-order-id", "no-such"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "submit-purchase-receipt", "--db-path", path,
         "--purchase-receipt-id", "no-such"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "generate-recurring-bills", "--db-path", path,
         "--company-id", env["company_id"], "--as-of-date", "2026-06-30"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert pgate._snapshot(path) == before
    submit_id = pgate._issue(path, "submit-purchase-invoice",
                             pproj._std("submit-purchase-invoice", path, pi))
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "get-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", pi, "--authorization-id", submit_id])
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
    import test_purchase_invoice_projection as pproj
    path, env = db_env
    entry_s = pproj._pi(path, env)
    if via == "direct":
        code, payload = _direct(
            ["--action", "submit-purchase-invoice", "--db-path", path,
             "--purchase-invoice", entry_s])
    else:
        code, payload = _via_router(
            ["--action", "submit-purchase-invoice", "--db-path", path,
             "--purchase-invoice", entry_s, "--user-confirmed"],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_a = pproj._pi(path, env)
    entry_b = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], entry_a, "submit-purchase-invoice")
    pproj._grant(path, env["company_id"], entry_b, "submit-purchase-invoice")
    import authority_fixtures as fx
    fx.make_active(path)
    pgate._patch_ready(monkeypatch)
    pgate._patch_actor(monkeypatch)
    issued_argv = ["--action", "submit-purchase-invoice", "--db-path", path,
                   "--purchase-invoice-id", entry_a,
                   "--purchase-invoice", entry_b]
    auth_id = pgate._issue(path, "submit-purchase-invoice", issued_argv)
    from erpclaw_lib import authority_gate as _gate_abbrev
    _real_phase = _gate_abbrev.install_phase

    def _must_not_read(conn):
        raise AssertionError("refusal happens before any read")

    monkeypatch.setattr(_gate_abbrev, "install_phase", _must_not_read)
    consume_argv = ["--action", "submit-purchase-invoice", "--db-path", path,
                    "--purchase-invoice-id", entry_a,
                    "--purchase-invoice", entry_b,
                    "--authorization-id", auth_id]
    before = pgate._snapshot(path)
    if via == "direct":
        code, payload = _direct(consume_argv)
    else:
        router_argv = ["--action", "submit-purchase-invoice", "--db-path", path,
                       "--purchase-invoice-id", entry_a,
                       "--purchase-invoice", entry_b,
                       "--user-confirmed", "--authorization-id", auth_id]
        code, payload = _via_router(router_argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        entry_a)["status"] == "draft"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        entry_b)["status"] == "draft"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           auth_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    monkeypatch.setattr(_gate_abbrev, "install_phase", _real_phase)
    entry_c = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], entry_c, "submit-purchase-invoice")
    company = env["company_id"]
    issued_dup = ["--action", "submit-purchase-invoice", "--db-path", path,
                  "--purchase-invoice-id", entry_c,
                  "--company-id", company, "--company-id", company]
    dup_id = pgate._issue(path, "submit-purchase-invoice", issued_dup)
    before = pgate._snapshot(path)
    code, payload = _direct(issued_dup + ["--authorization-id", dup_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._read_one(
        path, "purchase_invoice", ["id", "status"],
        entry_c)["status"] == "draft"
    assert pgate._read_one(path, "operation_authorization",
                           ["id", "consumed_at"],
                           dup_id)["consumed_at"] is None
    assert pgate._snapshot(path) == before
    entry_d = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], entry_d, "submit-purchase-invoice")
    dim_argv = ["--action", "submit-purchase-invoice", "--db-path", path,
                "--purchase-invoice-id", entry_d, "--dimension-key", "a",
                "--dimension-key", "b", "--dimension-value", "x",
                "--dimension-value", "y"]
    dim_id = pgate._issue(path, "submit-purchase-invoice", dim_argv)
    if via == "direct":
        code, payload = _direct(dim_argv + ["--authorization-id", dim_id])
    else:
        code, payload = _via_router(
            dim_argv + ["--user-confirmed", "--authorization-id", dim_id],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_e = pproj._pi(path, env)
    pproj._grant(path, env["company_id"], entry_e, "submit-purchase-invoice")
    eq_id = pgate._issue(path, "submit-purchase-invoice",
                         pproj._std("submit-purchase-invoice", path, entry_e))
    argv = ["--action", "submit-purchase-invoice", "--db-path", path,
            "--purchase-invoice-id=" + entry_e,
            "--authorization-id", eq_id]
    if via == "direct":
        code, payload = _direct(argv)
    else:
        code, payload = _via_router(
            ["--action", "submit-purchase-invoice", "--db-path", path,
             "--purchase-invoice-id=" + entry_e,
             "--user-confirmed", "--authorization-id", eq_id],
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_f = pproj._pi(path, env)
    before = pgate._snapshot(path)
    code, payload = _direct(
        pproj._std("submit-purchase-invoice", path, entry_f,
                   ["--authorization-id", "a-1", "--authorization-id", "b-2"]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert pgate._snapshot(path) == before


def test_cross_company_cancel_at_active(tmp_path, monkeypatch):
    """Cross scope cancel stays closed while same scope passes; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    import authority_fixtures as fx
    from erpclaw_lib.db import get_connection
    from erpclaw_lib import actor
    from erpclaw_lib import authority_readiness
    path = pgate._fresh_db(tmp_path, monkeypatch, "buying-scope")
    handle = get_connection(path)
    try:
        env_a = _helpers().build_buying_env(handle)
        env_b = _helpers().build_buying_env(handle)
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
                mod.create_purchase_invoice, handle,
                _helpers().ns(purchase_order_id=None,
                              purchase_receipt_id=None,
                              supplier_id=env["supplier"],
                              company_id=env["company_id"],
                              posting_date="2026-06-20",
                              due_date="2026-07-20", items=items,
                              tax_template_id=None))
            assert out.get("status") == "ok", out
            return out["purchase_invoice_id"]

        def _submit(pi):
            out = _helpers().call_action(
                mod.submit_purchase_invoice, handle,
                _helpers().ns(purchase_invoice_id=pi))
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
    pproj._grant(path, comp_a, draft_a, "cancel-purchase-invoice")
    pproj._grant(path, comp_b, draft_b, "cancel-purchase-invoice")
    _allow_member(path, fx.SERVICE, comp_b, "allow")
    _allow_member(path, fx.OWNER, comp_b, "allow")
    fx.make_active(path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(None, None, fx.SERVICE, (), actor.ATTESTED))
    auth_b = pgate._issue(path, "cancel-purchase-invoice",
                          ["--action", "cancel-purchase-invoice",
                           "--db-path", path,
                           "--purchase-invoice-id", draft_b])
    _allow_member(path, fx.SERVICE, comp_b, "deny")
    before = pgate._snapshot(path)
    code, payload = _direct(
        ["--action", "cancel-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", draft_b, "--authorization-id", auth_b])
    assert code == 1
    assert payload == {"status": "error", "message": "COMPANY_SCOPE_REFUSED"}
    assert pgate._snapshot(path) == before
    entry_b = pgate._read_one(path, "purchase_invoice", ["id", "status"], draft_b)
    assert entry_b["status"] == "submitted"
    auth_row = pgate._read_one(path, "operation_authorization",
                               ["id", "consumed_at"], auth_b)
    assert auth_row["consumed_at"] is None
    auth_a = pgate._issue(path, "cancel-purchase-invoice",
                          ["--action", "cancel-purchase-invoice",
                           "--db-path", path,
                           "--purchase-invoice-id", draft_a])
    code, payload = _direct(
        ["--action", "cancel-purchase-invoice", "--db-path", path,
         "--purchase-invoice-id", draft_a, "--authorization-id", auth_a])
    assert code == 0, payload
    entry_a = pgate._read_one(path, "purchase_invoice", ["id", "status"], draft_a)
    assert entry_a["status"] == "cancelled"
    rows = pgate._read_all(path, "audit_log",
                           ["action", "entity_id", "scope_status",
                            "scope_company_ids"])
    found = [row for row in rows if row["action"] == "cancel-purchase-invoice"
             and row["entity_id"] == draft_a]
    assert len(found) >= 1
    last = found[-1]
    assert last["scope_status"] == "in_scope"
    assert last["scope_company_ids"] == comp_a


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres leg mirrors the envelope flow; not qualification."""
    import test_payment_envelope_gate as pgate
    import test_purchase_invoice_projection as pproj
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    pproj._SEEDED.clear()
    pproj._SEED_CAPS.clear()
    seam = _seam()
    seam.dispose_engines()
    try:
        pgate._pg_reset_schema()
        import setup_helpers as setup
        setup.init_all_tables(None)
        handle = _open(None)
        try:
            env = _helpers().build_buying_env(handle)
        finally:
            handle.close()
        pproj._SEEDED.clear()
        pproj._SEED_CAPS.clear()
        import authority_fixtures as fx
        company = env["company_id"]
        pi = pproj._pi(None, env)
        pproj._grant(None, company, pi, "submit-purchase-invoice")
        fx.make_active(None)
        pgate._patch_ready(monkeypatch)
        pgate._patch_actor(monkeypatch)
        issued = pgate._issue_full(None, "submit-purchase-invoice",
                                   ["--action", "submit-purchase-invoice",
                                    "--purchase-invoice-id", pi])
        assert issued["issued_route"] == "delegation"
        auth_id = issued["authorization_id"]
        code, payload = _direct(
            ["--action", "submit-purchase-invoice",
             "--purchase-invoice-id", pi,
             "--authorization-id", auth_id])
        assert code == 0, payload
        assert payload.get("document_status") == "submitted"
        entry = pgate._read_one(None, "purchase_invoice", ["id", "status"], pi)
        assert entry["status"] == "submitted"
        auth = pgate._read_one(None, "operation_authorization",
                               ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is not None
        result = pgate._result_row(None, auth_id)
        assert (result["result_kind"], result["result_id"],
                result["result_status"]) == ("purchase-invoice", pi,
                                             "submitted")
        audits = [row for row in pgate._read_all(
            None, "audit_log",
            ["authorization_id", "authorization_status"])
            if row["authorization_id"] == auth_id]
        assert len(audits) == 1
        assert audits[0]["authorization_status"] == "verified"
        usage = [row for row in pgate._read_all(
            None, "authority_delegation_usage",
            ["delegation_id", "action", "currency", "used"])
            if row["delegation_id"] == fx.DELEGATION
            and row["action"] == "submit-purchase-invoice"
            and row["currency"] == "USD"]
        assert len(usage) == 1
        assert usage[0]["used"] == "100.00"
        other = pproj._pi(None, env)
        pproj._grant(None, company, other, "submit-purchase-invoice")
        before = pgate._snapshot(None)
        code, payload = _direct(
            ["--action", "submit-purchase-invoice",
             "--purchase-invoice-id", other])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REQUIRED"
        assert pgate._snapshot(None) == before
        from erpclaw_lib import authority_readiness
        monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
        before = pgate._snapshot(None)
        code, payload = _direct(
            ["--action", "submit-purchase-invoice",
             "--purchase-invoice-id", other])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORITY_NOT_READY"
        assert pgate._snapshot(None) == before
        before = pgate._snapshot(None)
        code, payload = _direct(
            ["--action", "list-purchase-invoices",
             "--company", "No Such Company"])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORITY_NOT_READY"
        assert pgate._snapshot(None) == before
        pgate._patch_ready(monkeypatch)
        from erpclaw_lib import actor as _absent_actor
        monkeypatch.setattr(_absent_actor, "current", lambda: _absent_actor.ActorContext(None, None, None, (), _absent_actor.ABSENT))
        before = pgate._snapshot(None)
        code, payload = _direct(
            ["--action", "get-purchase-invoice",
             "--purchase-invoice-id", other])
        assert code == 1, payload
        assert payload.get("message") == "COMPANY_SCOPE_REFUSED"
        assert pgate._snapshot(None) == before
        pgate._patch_actor(monkeypatch)
        code, payload = _direct(
            ["--action", "get-purchase-invoice",
             "--purchase-invoice-id", other])
        assert code == 0, payload
        import json as _json
        items = _json.dumps([
            {"item_id": env["item1"], "qty": "1", "rate": "10.00",
             "warehouse_id": env["warehouse"]},
        ])
        code, payload = _direct(
            ["--action", "create-purchase-invoice",
             "--company-id", company, "--supplier-id", env["supplier"],
             "--posting-date", "2026-06-20", "--items", items])
        assert code == 0, payload
    finally:
        pproj._SEEDED.clear()
        pproj._SEED_CAPS.clear()
        seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
