"""Payment postings behind single-use authorization."""
import io
import json
import os
import sys
import uuid
import pytest
_PE_TESTS = os.path.dirname(os.path.abspath(__file__))
_PE_MODULE = os.path.dirname(_PE_TESTS)
_PE_SETUP_TESTS = os.path.join(os.path.dirname(_PE_MODULE), "erpclaw-setup", "tests")
if _PE_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _PE_SETUP_TESTS)
if _PE_TESTS not in sys.path:
    sys.path.insert(0, _PE_TESTS)

DATE = "2026-06-15"
_SEEDED = set()
_SEED_CAPS = set()

_ROUTER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(_PE_MODULE), "db_query.py"))


class _Forwarded(Exception):
    def __init__(self, argv):
        super().__init__("forwarded")
        self.argv = list(argv)


def _helpers():
    import payments_helpers as helpers
    return helpers


def _seam():
    from erpclaw_lib import seam
    return seam


def _fresh_db(tmp_path, monkeypatch, tag):
    import setup_helpers as setup
    path = str(tmp_path / ("envelope-%s.sqlite" % tag))
    setup.init_all_tables(path)
    monkeypatch.setenv("ERPCLAW_DB_PATH", path)
    return path


def _open(path):
    from erpclaw_lib.db import get_connection
    return get_connection(path)


def _env(handle):
    return _helpers().build_ar_env(handle)


def _std(action, path, pe_id, extra=None):
    argv = ["--action", action, "--db-path", path, "--payment-entry-id", pe_id]
    if extra:
        argv = argv + list(extra)
    return argv


def _pe(path, env, amount="100.00", allocations=None, deductions=None,
       payment_currency="USD", exchange_rate="1"):
    helpers = _helpers()
    handle = _open(path)
    try:
        result = helpers.call_action(
            helpers.load_db_query().add_payment, handle,
            helpers.ns(company_id=env["company_id"], payment_type="receive",
                       posting_date=DATE, party_type="customer",
                       party_id=env["customer"],
                       paid_from_account=env["ar"],
                       paid_to_account=env["bank"], paid_amount=amount,
                       payment_currency=payment_currency,
                       exchange_rate=exchange_rate, reference_number=None,
                       reference_date=None, allocations=allocations,
                       deductions=deductions, dimensions=None,
                       dimension_key=None, dimension_value=None))
    finally:
        handle.close()
    assert result.get("status") == "ok", result
    assert result.get("document_status") == "created", result
    return result["payment_entry_id"]


def _grant(path, company_id, pe_id, action):
    import authority_fixtures as fx
    from erpclaw_lib import authority_clock
    if path not in _SEEDED:
        fx.seed_authority(path, company_id)
        _SEEDED.add(path)
    handle = _open(path)
    try:
        install_id = fx._install_id(handle)
        fx._insert_row(handle, "authority_right", {
            "install_id": install_id, "principal_id": fx.SERVICE,
            "company_id": company_id, "resource_kind": "payment-entry",
            "resource_id": pe_id, "action": action, "effect": "allow"})
        fx._insert_row(handle, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company_id, "resource_kind": "payment-entry",
            "resource_id": pe_id, "action": action})
        if (path, action) not in _SEED_CAPS:
            now = authority_clock.now_ms()
            fx._insert_row(handle, "authority_delegation_cap", {
                "install_id": install_id, "delegation_id": fx.DELEGATION,
                "action": action, "currency": "USD", "scale": 2,
                "per_operation": "500.00", "aggregate_limit": "1000.00",
                "window_start": now - fx.DAY_MS,
                "window_end": now + 10 * fx.DAY_MS})
            _SEED_CAPS.add((path, action))
        handle.commit()
    finally:
        handle.close()


def _issue(path, action, argv):
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import authority_fixtures as fx
        out = authorization_issuance.issue_envelope(
            handle, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=action, argv=list(argv),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    return out["authorization_id"]


def _issue_full(path, action, argv):
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import authority_fixtures as fx
        out = authorization_issuance.issue_envelope(
            handle, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=action, argv=list(argv),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    return out


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
    lib = os.path.join(os.path.dirname(_PE_MODULE), "erpclaw-setup", "lib")
    lib = os.path.abspath(lib)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    name = "erpclaw_router_peg_%s" % abs(hash(home))
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


def _snapshot(path):
    import setup_helpers as setup
    handle = _open(path)
    try:
        names = _seam().table_names(path)
        return setup.freeze_snapshot(handle, path, names)
    finally:
        handle.close()


def _read_all(path, table, columns):
    import setup_helpers as setup
    handle = _open(path)
    try:
        return setup.read_all(handle, table, columns)
    finally:
        handle.close()


def _read_one(path, table, columns, row_id):
    import setup_helpers as setup
    handle = _open(path)
    try:
        return setup.read_one(handle, table, columns, row_id)
    finally:
        handle.close()


def _result_row(path, auth_id):
    rows = [row for row in _read_all(
        path, "operation_authorization_result",
        ["authorization_id", "consumed_txn", "result_kind", "result_id",
         "result_status"]) if row["authorization_id"] == auth_id]
    assert len(rows) == 1
    return rows[0]


def _patch_ready(monkeypatch):
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)


def _patch_actor(monkeypatch):
    import authority_fixtures as fx
    from erpclaw_lib import actor
    monkeypatch.setattr(actor, "current", lambda: actor.ActorContext(
        None, None, fx.SERVICE, (), actor.ATTESTED))


def _update_pe(path, pe_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("payment_entry")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(pe_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _update_alloc(path, alloc_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("payment_allocation")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(alloc_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _update_ded(path, ded_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("payment_deduction")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(ded_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _alloc_ids(path, pe_id):
    rows = _read_all(path, "payment_allocation", ["id", "payment_entry_id"])
    return sorted(row["id"] for row in rows
                  if row["payment_entry_id"] == pe_id)


def _ded_ids(path, pe_id):
    rows = _read_all(path, "payment_deduction", ["id", "payment_entry_id"])
    return sorted(row["id"] for row in rows
                  if row["payment_entry_id"] == pe_id)


def _gl_rows(path, pe_id):
    cols = _seam().column_names("gl_entry", path)
    rows = _read_all(path, "gl_entry", cols)
    return (cols, sorted((row for row in rows if row["voucher_id"] == pe_id),
                         key=lambda row: row["id"]))


def _check_cancelled(path, pe_id, before):
    _cols, after = _gl_rows(path, pe_id)
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
    path = _fresh_db(tmp_path, monkeypatch, "main")
    handle = _open(path)
    try:
        env = _env(handle)
    finally:
        handle.close()
    return (path, env)


def test_staged_flows_unchanged(db_env, tmp_path, monkeypatch):
    path, env = db_env
    pe = _pe(path, env)
    code, payload = _via_router(
        ["--action", "submit-payment", "--db-path", path,
         "--payment-entry-id", pe], tmp_path, monkeypatch)
    assert code == 2
    assert payload.get("error") == "user_confirmation_required"
    code, payload = _via_router(
        ["--action", "submit-payment", "--db-path", path,
         "--payment-entry-id", pe, "--user-confirmed"],
        tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    from decimal import Decimal as _D
    legs = [row for row in _read_all(
        path, "gl_entry", ["voucher_id", "debit", "credit"])
        if row["voucher_id"] == pe]
    assert len(legs) == 2
    assert sum((_D(row["debit"]) for row in legs), _D("0")) == _D("100.00")
    assert sum((_D(row["credit"]) for row in legs), _D("0")) == _D("100.00")
    assert _read_all(path, "operation_authorization_result",
                      ["authorization_id"]) == []
    audits = _read_all(path, "audit_log", ["authorization_id"])
    assert [row for row in audits if row["authorization_id"]] == []
    code, payload = _direct(
        ["--action", "get-payment", "--db-path", path,
         "--payment-entry-id", pe])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_active_without_envelope_refuses(db_env, tmp_path, monkeypatch, via):
    """Refusal without envelope at ACTIVE; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    if via == "direct":
        argv = _std("submit-payment", path, pe)
        before = _snapshot(path)
        code, payload = _direct(argv)
    else:
        argv = _std("submit-payment", path, pe, ["--user-confirmed"])
        before = _snapshot(path)
        code, payload = _via_router(argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REQUIRED"
    assert "issue-authorization" in payload.get("suggestion", "")
    after = _snapshot(path)
    assert before == after
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = _snapshot(path)
    if via == "direct":
        code, payload = _direct(_std("submit-payment", path, pe))
    else:
        code, payload = _via_router(
            _std("submit-payment", path, pe, ["--user-confirmed"]),
            tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert _snapshot(path) == before
    _patch_ready(monkeypatch)
    from erpclaw_lib import actor as _absent_actor
    monkeypatch.setattr(_absent_actor, "current", lambda: _absent_actor.ActorContext(None, None, None, (), _absent_actor.ABSENT))
    _absent_before = _snapshot(path)
    code, payload = _direct(
        ["--action", "get-payment", "--db-path", path,
         "--payment-entry-id", pe])
    assert code == 1, payload
    assert payload.get("message") == "COMPANY_SCOPE_REFUSED"
    assert _snapshot(path) == _absent_before
    fx.seed_authority(path, env["company_id"])
    _SEEDED.add(path)
    _patch_actor(monkeypatch)
    code, payload = _direct(
        ["--action", "get-payment", "--db-path", path,
         "--payment-entry-id", pe])
    assert code == 0, payload
    code, payload = _direct(
        ["--action", "add-payment", "--db-path", path,
         "--company-id", env["company_id"], "--payment-type", "receive",
         "--posting-date", DATE, "--party-type", "customer",
         "--party-id", env["customer"],
         "--paid-from-account", env["ar"],
         "--paid-to-account", env["bank"], "--paid-amount", "10.00"])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_valid_envelope_consumes(db_env, tmp_path, monkeypatch, via):
    """Consume one envelope at ACTIVE; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued = _issue_full(path, "submit-payment",
                         _std("submit-payment", path, pe))
    assert issued["issued_route"] == "delegation"
    auth_id = issued["authorization_id"]
    if via == "direct":
        code, payload = _direct(
            _std("submit-payment", path, pe,
                 ["--authorization-id", auth_id]))
    else:
        code, payload = _via_router(
            _std("submit-payment", path, pe,
                 ["--user-confirmed", "--authorization-id", auth_id]),
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry = _read_one(path, "payment_entry", ["id", "status"], pe)
    assert entry["status"] == "submitted"
    legs = [row for row in _read_all(
        path, "gl_entry", ["voucher_id"]) if row["voucher_id"] == pe]
    assert len(legs) == 2
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is not None
    result = _result_row(path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("payment-entry", pe, "submitted")
    audits = [row for row in _read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == auth_id]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    usage = _read_all(path, "authority_delegation_usage", ["used"])
    assert "100.00" in [row["used"] for row in usage]


def test_valid_envelope_readiness_unpatched(db_env, monkeypatch):
    """Unready ACTIVE refuses before spend; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-payment",
                     _std("submit-payment", path, pe))
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-payment", path, pe,
             ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before


def test_install_id_mismatch(db_env, monkeypatch):
    """Foreign install refuses; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-payment",
                     _std("submit-payment", path, pe))
    from erpclaw_lib import authority_gate
    monkeypatch.setattr(authority_gate, "install_phase",
                        lambda conn: ("ACTIVE", "other-install"))
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-payment", path, pe,
             ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before


def test_state_digest_binds_the_payment(db_env):
    path, env = db_env
    handle = _open(path)
    try:
        si = _helpers().seed_sales_invoice(handle, env, "100.00")
    finally:
        handle.close()
    pe = _pe(path, env,
             allocations=json.dumps(
                 [{"voucher_type": "sales_invoice", "voucher_id": si,
                   "allocated_amount": "60.00"}]),
             deductions=json.dumps(
                 [{"account_id": env["commission"], "amount": "10.00",
                   "type": "commission"}]))
    _grant(path, env["company_id"], pe, "submit-payment")
    auth_id = _issue(path, "submit-payment",
                     _std("submit-payment", path, pe))
    base = _std("submit-payment", path, pe,
                ["--authorization-id", auth_id])
    alloc_id = _alloc_ids(path, pe)[0]
    ded_id = _ded_ids(path, pe)[0]
    pe_before = _read_one(path, "payment_entry",
                          ["id", "paid_amount", "posting_date",
                           "exchange_rate", "advance_account_id",
                           "dimensions_json"], pe)
    alloc_before = _read_one(path, "payment_allocation",
                             ["id", "allocated_amount", "delinked"],
                             alloc_id)
    ded_before = _read_one(path, "payment_deduction", ["id", "amount"],
                           ded_id)

    def _try(argv):
        before = _snapshot(path)
        code, payload = _direct(argv)
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REFUSED"
        auth = _read_one(path, "operation_authorization",
                         ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is None
        assert _snapshot(path) == before

    _update_pe(path, pe, {"paid_amount": "90.00"})
    _try(base)
    _update_pe(path, pe, {"paid_amount": pe_before["paid_amount"]})
    _update_pe(path, pe, {"posting_date": "2026-07-01"})
    _try(base)
    _update_pe(path, pe, {"posting_date": pe_before["posting_date"]})
    _update_pe(path, pe, {"exchange_rate": "1.20"})
    _try(base)
    _update_pe(path, pe, {"exchange_rate": pe_before["exchange_rate"]})
    _update_pe(path, pe, {"advance_account_id": env["ar"]})
    _try(base)
    _update_pe(path, pe,
               {"advance_account_id": pe_before["advance_account_id"]})
    _update_pe(path, pe, {"dimensions_json": '{"zone": "north"}'})
    _try(base)
    _update_pe(path, pe,
               {"dimensions_json": pe_before["dimensions_json"]})
    _update_alloc(path, alloc_id, {"allocated_amount": "50.00"})
    _try(base)
    _update_alloc(path, alloc_id,
                  {"allocated_amount": alloc_before["allocated_amount"]})
    _update_alloc(path, alloc_id, {"delinked": 1})
    _try(base)
    _update_alloc(path, alloc_id, {"delinked": alloc_before["delinked"]})
    _update_ded(path, ded_id, {"amount": "5.00"})
    _try(base)
    _update_ded(path, ded_id, {"amount": ded_before["amount"]})
    _try(base + ["--reference-number", "x"])
    code, payload = _direct(base)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def test_projection_reads_the_database(db_env):
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    _update_pe(path, pe, {"paid_amount": "100"})
    from erpclaw_lib import authority_gate
    handle = _open(path)
    try:
        derived = authority_gate.ENVELOPE_ACTIONS[
            "submit-payment"]["derive"](
            handle, "submit-payment", [["payment-entry-id", pe]])
    finally:
        handle.close()
    entry = _read_one(path, "payment_entry",
                      ["id", "company_id", "payment_currency"], pe)
    assert derived["company_ids"] == [entry["company_id"]]
    assert len(derived["targets"]) == 1
    assert derived["targets"][0]["kind"] == "payment-entry"
    assert derived["targets"][0]["id"] == pe
    assert derived["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    full = _read_one(path, "payment_entry",
                     ["id", "company_id", "naming_series", "payment_type",
                      "posting_date", "party_type", "party_id",
                      "paid_from_account", "paid_to_account", "paid_amount",
                      "received_amount", "payment_currency", "exchange_rate",
                      "reference_number", "reference_date", "status",
                      "unallocated_amount", "payment_method",
                      "advance_account_id", "dimensions_json"], pe)
    assert full["company_id"] == env["company_id"]
    assert full["payment_type"] == "receive"
    assert full["posting_date"] == DATE
    assert full["party_type"] == "customer"
    assert full["party_id"] == env["customer"]
    assert full["paid_from_account"] == env["ar"]
    assert full["paid_to_account"] == env["bank"]
    assert full["paid_amount"] == "100"
    assert full["received_amount"] == "100.00"
    assert full["payment_currency"] == "USD"
    assert full["exchange_rate"] == "1"
    assert full["reference_number"] is None
    assert full["reference_date"] is None
    assert full["status"] == "draft"
    assert full["unallocated_amount"] == "100.00"
    assert full["payment_method"] == ""
    assert full["advance_account_id"] is None
    assert full["dimensions_json"] == "{}"
    alloc_rows = _read_all(path, "payment_allocation",
                           ["id", "payment_entry_id", "voucher_type",
                            "voucher_id", "allocated_amount",
                            "exchange_gain_loss", "delinked"])
    own_allocs = sorted(
        ({"id": row["id"], "voucher_type": row["voucher_type"],
          "voucher_id": row["voucher_id"],
          "allocated_amount": row["allocated_amount"],
          "exchange_gain_loss": row["exchange_gain_loss"],
          "delinked": row["delinked"]} for row in alloc_rows
         if row["payment_entry_id"] == pe),
        key=lambda row: row["id"])
    assert own_allocs == []
    ded_rows = _read_all(path, "payment_deduction",
                         ["id", "payment_entry_id", "account_id", "amount",
                          "type", "description"])
    own_deds = sorted(
        ({"id": row["id"], "account_id": row["account_id"],
          "amount": row["amount"], "type": row["type"],
          "description": row["description"]} for row in ded_rows
         if row["payment_entry_id"] == pe),
        key=lambda row: row["id"])
    assert own_deds == []
    want_entry = {"id": pe, "company_id": env["company_id"],
                  "naming_series": full["naming_series"],
                  "payment_type": "receive", "posting_date": DATE,
                  "party_type": "customer", "party_id": env["customer"],
                  "paid_from_account": env["ar"],
                  "paid_to_account": env["bank"], "paid_amount": "100",
                  "received_amount": "100.00", "payment_currency": "USD",
                  "exchange_rate": "1", "reference_number": None,
                  "reference_date": None, "status": "draft",
                  "unallocated_amount": "100.00", "payment_method": "",
                  "advance_account_id": None, "dimensions_json": "{}"}
    import hashlib as _hl
    import json as _js
    want_text = _js.dumps(
        {"entry": want_entry, "allocations": own_allocs,
         "deductions": own_deds},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False)
    want_digest = _hl.sha256(want_text.encode("utf-8")).hexdigest()
    assert derived["targets"][0]["state_digest"] == want_digest
    _update_pe(path, pe,
               {"paid_amount": "100.125", "received_amount": "100.125"})
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-payment"]["derive"](
                handle, "submit-payment", [["payment-entry-id", pe]])
    finally:
        handle.close()
    _update_pe(path, pe,
               {"paid_amount": "100.00", "received_amount": "100.00"})
    handle = _open(path)
    try:
        shaped = authority_gate.ENVELOPE_ACTIONS[
            "submit-payment"]["derive"](
            handle, "submit-payment", [["payment-entry-id", pe]])
    finally:
        handle.close()
    assert shaped["amounts"] == [{"currency": "USD", "value": "100.00",
                                  "scale": 2}]
    import uuid as _uuid
    handle = _open(path)
    try:
        import authority_fixtures as fx
        second_company = str(_uuid.uuid4())
        fx._insert_row(handle, "company", {
            "id": second_company, "name": "Other %s" % second_company[:6],
            "abbr": "OT%s" % second_company[:4],
            "default_currency": "USD", "country": "United States",
            "fiscal_year_start_month": 1})
        handle.commit()
    finally:
        handle.close()
    handle = _open(path)
    try:
        import pytest as _pt2
        with _pt2.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-payment"]["derive"](
                handle, "submit-payment",
                [["payment-entry-id", pe],
                 ["company-id", second_company]])
    finally:
        handle.close()
    import pytest as _pt3
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        with _pt3.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="submit-payment",
                argv=["--payment-entry-id", pe, "--company-id",
                      second_company],
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(_uuid.uuid4()))
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_ISSUANCE_REFUSED",)
    from erpclaw_lib import action_impact
    product = {key for key in authority_gate.ENVELOPE_ACTIONS
               if key in action_impact.IMPACT}
    assert {"submit-journal-entry", "cancel-journal-entry",
            "amend-journal-entry"} <= set(authority_gate.ENVELOPE_ACTIONS)
    assert product == {"submit-journal-entry", "cancel-journal-entry",
                       "amend-journal-entry", "submit-payment",
                       "cancel-payment", "submit-sales-invoice",
                       "cancel-sales-invoice", "submit-purchase-invoice",
                       "cancel-purchase-invoice"}


def test_cancel_through_the_gate(db_env, monkeypatch):
    """Cancel consumes at STAGED and ACTIVE; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    code, payload = _direct(_std("submit-payment", path, pe))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    _cols, before = _gl_rows(path, pe)
    assert len(before) == 2
    _grant(path, env["company_id"], pe, "cancel-payment")
    cancel_id = _issue(path, "cancel-payment",
                       _std("cancel-payment", path, pe))
    code, payload = _direct(
        _std("cancel-payment", path, pe,
             ["--authorization-id", cancel_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = _result_row(path, cancel_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("payment-entry", pe, "cancelled")
    _check_cancelled(path, pe, before)
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    other = _pe(path, env)
    _grant(path, env["company_id"], other, "submit-payment")
    submit_id = _issue(path, "submit-payment",
                       _std("submit-payment", path, other))
    code, payload = _direct(
        _std("submit-payment", path, other,
             ["--authorization-id", submit_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    _cols, before_other = _gl_rows(path, other)
    assert len(before_other) == 2
    _grant(path, env["company_id"], other, "cancel-payment")
    other_cancel = _issue(path, "cancel-payment",
                          _std("cancel-payment", path, other))
    code, payload = _direct(
        _std("cancel-payment", path, other,
             ["--authorization-id", other_cancel]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = _result_row(path, other_cancel)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("payment-entry", other, "cancelled")
    _check_cancelled(path, other, before_other)
    third = _pe(path, env)
    _grant(path, env["company_id"], third, "submit-payment")
    third_submit = _issue(path, "submit-payment",
                          _std("submit-payment", path, third))
    code, payload = _direct(
        _std("submit-payment", path, third,
             ["--authorization-id", third_submit]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before_snap = _snapshot(path)
    code, payload = _direct(_std("cancel-payment", path, third))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REQUIRED"
    assert _snapshot(path) == before_snap


def test_replay_after_submit(db_env, monkeypatch):
    """Replay returns stored result; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-payment",
                     _std("submit-payment", path, pe))
    argv = _std("submit-payment", path, pe,
                ["--authorization-id", auth_id])
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before = _snapshot(path)
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("replayed") is True
    assert payload.get("authorization_id") == auth_id
    assert payload.get("result_kind") == "payment-entry"
    assert payload.get("result_id") == pe
    assert payload.get("result_status") == "submitted"
    assert _snapshot(path) == before


def test_unprojected_and_non_envelope_actions(db_env, monkeypatch):
    """Unprojected refuses with or without id; not qualification."""
    path, env = db_env
    pe = _pe(path, env)
    _grant(path, env["company_id"], pe, "submit-payment")
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="delete-payment",
                argv=["--payment-entry-id", pe],
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    assert "IMPACT_UNDECLARED" in excinfo.value.args
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    before = _snapshot(path)
    code, payload = _direct(_std("delete-payment", path, pe))
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert _snapshot(path) == before
    before = _snapshot(path)
    code, payload = _direct(
        ["--action", "allocate-payment", "--db-path", path,
         "--payment-entry-id", pe, "--voucher-type", "sales_invoice",
         "--voucher-id", "no-such", "--allocated-amount", "10.00"])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert _snapshot(path) == before
    before = _snapshot(path)
    code, payload = _direct(
        ["--action", "reconcile-payments", "--db-path", path,
         "--company-id", env["company_id"]])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert _snapshot(path) == before
    submit_id = _issue(path, "submit-payment",
                       _std("submit-payment", path, pe))
    before = _snapshot(path)
    code, payload = _direct(
        ["--action", "get-payment", "--db-path", path,
         "--payment-entry-id", pe, "--authorization-id", submit_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], submit_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before


@pytest.mark.parametrize("via", ["direct", "router"])
def test_argv_spelling_and_forms(db_env, tmp_path, monkeypatch, via):
    """Option spellings gate before any read; not qualification."""
    path, env = db_env
    entry_s = _pe(path, env)
    code, payload = _direct(
        ["--action", "submit-payment", "--db-path", path,
         "--payment-entry", entry_s])
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_a = _pe(path, env)
    entry_b = _pe(path, env)
    _grant(path, env["company_id"], entry_a, "submit-payment")
    _grant(path, env["company_id"], entry_b, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued_argv = ["--action", "submit-payment", "--db-path", path,
                   "--payment-entry-id", entry_a,
                   "--payment-entry", entry_b]
    auth_id = _issue(path, "submit-payment", issued_argv)
    from erpclaw_lib import authority_gate as _gate_abbrev
    _real_phase = _gate_abbrev.install_phase

    def _must_not_read(conn):
        raise AssertionError("refusal happens before any read")

    monkeypatch.setattr(_gate_abbrev, "install_phase", _must_not_read)
    consume_argv = ["--action", "submit-payment", "--db-path", path,
                    "--payment-entry-id", entry_a,
                    "--payment-entry", entry_b,
                    "--authorization-id", auth_id]
    before = _snapshot(path)
    if via == "direct":
        code, payload = _direct(consume_argv)
    else:
        router_argv = ["--action", "submit-payment", "--db-path", path,
                       "--payment-entry-id", entry_a,
                       "--payment-entry", entry_b,
                       "--user-confirmed", "--authorization-id", auth_id]
        code, payload = _via_router(router_argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _read_one(
        path, "payment_entry", ["id", "status"],
        entry_a)["status"] == "draft"
    assert _read_one(
        path, "payment_entry", ["id", "status"],
        entry_b)["status"] == "draft"
    assert _read_one(path, "operation_authorization",
                     ["id", "consumed_at"],
                     auth_id)["consumed_at"] is None
    assert _snapshot(path) == before
    monkeypatch.setattr(_gate_abbrev, "install_phase", _real_phase)
    entry_c = _pe(path, env)
    _grant(path, env["company_id"], entry_c, "submit-payment")
    company = env["company_id"]
    issued_dup = ["--action", "submit-payment", "--db-path", path,
                  "--payment-entry-id", entry_c,
                  "--company-id", company, "--company-id", company]
    dup_id = _issue(path, "submit-payment", issued_dup)
    before = _snapshot(path)
    code, payload = _direct(issued_dup + ["--authorization-id", dup_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _read_one(
        path, "payment_entry", ["id", "status"],
        entry_c)["status"] == "draft"
    assert _read_one(path, "operation_authorization",
                     ["id", "consumed_at"],
                     dup_id)["consumed_at"] is None
    assert _snapshot(path) == before
    entry_d = _pe(path, env)
    _grant(path, env["company_id"], entry_d, "submit-payment")
    dim_argv = ["--action", "submit-payment", "--db-path", path,
                "--payment-entry-id", entry_d, "--dimension-key", "a",
                "--dimension-key", "b", "--dimension-value", "x",
                "--dimension-value", "y"]
    dim_id = _issue(path, "submit-payment", dim_argv)
    code, payload = _direct(dim_argv + ["--authorization-id", dim_id])
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_e = _pe(path, env)
    _grant(path, env["company_id"], entry_e, "submit-payment")
    eq_id = _issue(path, "submit-payment",
                   _std("submit-payment", path, entry_e))
    argv = ["--action", "submit-payment", "--db-path", path,
            "--payment-entry-id=" + entry_e,
            "--authorization-id", eq_id]
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry_f = _pe(path, env)
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-payment", path, entry_f,
             ["--authorization-id", "a-1", "--authorization-id", "b-2"]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _snapshot(path) == before


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _pg_reset_schema():
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url
    from erpclaw_lib.db import get_connection as _gc
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(None)
    handle = _gc()
    try:
        resolved_db = handle.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError("pg host/port mismatch")
        handle.execute("DROP SCHEMA IF EXISTS public CASCADE")
        handle.execute("CREATE SCHEMA public")
        handle.commit()
    finally:
        handle.close()


def _pg_seed(env_target):
    import uuid as _uuid
    import authority_fixtures as fx
    from erpclaw_lib.db import get_connection as _gc
    handle = _gc()
    try:
        company_id = str(_uuid.uuid4())
        fx._insert_row(handle, "company", {
            "id": company_id, "name": "Pg Co %s" % company_id[:6],
            "abbr": "PG%s" % company_id[:4],
            "default_currency": "USD", "country": "United States",
            "fiscal_year_start_month": 1})
        fiscal_id = str(_uuid.uuid4())
        fx._insert_row(handle, "fiscal_year", {
            "id": fiscal_id, "name": "FY-%s" % fiscal_id[:6],
            "start_date": "2026-01-01", "end_date": "2026-12-31",
            "company_id": company_id})
        fx._insert_row(handle, "naming_series", {
            "id": str(_uuid.uuid4()), "entity_type": "payment_entry",
            "prefix": "PAY-", "current_value": 0,
            "company_id": company_id})
        cc_id = str(_uuid.uuid4())
        fx._insert_row(handle, "cost_center", {
            "id": cc_id, "name": "Main CC", "company_id": company_id,
            "is_group": 0})
        bank = str(_uuid.uuid4())
        fx._insert_row(handle, "account", {
            "id": bank, "name": "Bank", "account_number": "1000",
            "root_type": "asset", "account_type": "bank",
            "balance_direction": "debit_normal", "company_id": company_id,
            "depth": 0})
        ar = str(_uuid.uuid4())
        fx._insert_row(handle, "account", {
            "id": ar, "name": "Debtors", "account_number": "1100",
            "root_type": "asset", "account_type": None,
            "balance_direction": "debit_normal", "company_id": company_id,
            "depth": 0})
        cust = str(_uuid.uuid4())
        fx._insert_row(handle, "customer", {
            "id": cust, "name": "Pg Customer",
            "customer_type": "company", "status": "active",
            "company_id": company_id})
        handle.commit()
    finally:
        handle.close()
    return {"company_id": company_id, "cc": cc_id, "bank": bank, "ar": ar,
            "customer": cust}


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres leg mirrors the envelope flow; not qualification."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    _SEEDED.clear()
    _SEED_CAPS.clear()
    seam = _seam()
    seam.dispose_engines()
    try:
        _pg_reset_schema()
        import setup_helpers as setup
        setup.init_all_tables(None)
        env = _pg_seed(None)
        import authority_fixtures as fx
        pe = _pe(None, env)
        _grant(None, env["company_id"], pe, "submit-payment")
        fx.make_active(None)
        _patch_ready(monkeypatch)
        _patch_actor(monkeypatch)
        auth_id = _issue(None, "submit-payment",
                         ["--action", "submit-payment",
                          "--payment-entry-id", pe])
        code, payload = _direct(
            ["--action", "submit-payment", "--payment-entry-id", pe,
             "--authorization-id", auth_id])
        assert code == 0, payload
        assert payload.get("document_status") == "submitted"
        entry = _read_one(None, "payment_entry", ["id", "status"], pe)
        assert entry["status"] == "submitted"
        auth = _read_one(None, "operation_authorization",
                         ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is not None
        result = _result_row(None, auth_id)
        assert (result["result_kind"], result["result_id"],
                result["result_status"]) == ("payment-entry", pe,
                                             "submitted")
        other = _pe(None, env)
        _grant(None, env["company_id"], other, "submit-payment")
        before = _snapshot(None)
        code, payload = _direct(
            ["--action", "submit-payment", "--payment-entry-id", other])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REQUIRED"
        assert _snapshot(None) == before
    finally:
        _SEEDED.clear()
        _SEED_CAPS.clear()
        seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")


def test_foreign_currency_payment_needs_its_own_cap(db_env, monkeypatch):
    """Foreign currency needs rights in its currency; not qualification."""
    path, env = db_env
    pe = _pe(path, env, payment_currency="EUR", exchange_rate="1.10")
    _grant(path, env["company_id"], pe, "submit-payment")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    from erpclaw_lib import authority_clock
    handle = _open(path)
    try:
        install_id = fx._install_id(handle)
        now = authority_clock.now_ms()
        win_start = now - fx.DAY_MS
        win_end = now + 10 * fx.DAY_MS
        fx._insert_row(handle, "authority_delegation_cap", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "action": "submit-payment", "currency": "EUR", "scale": 2,
            "per_operation": "500.00", "aggregate_limit": "1000.00",
            "window_start": win_start, "window_end": win_end})
        handle.commit()
    finally:
        handle.close()
    auth_id = _issue(path, "submit-payment",
                     _std("submit-payment", path, pe))
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        usage_table = Table("authority_delegation_usage")
        usage_query = Q.from_(usage_table).delete().where(
            Field("install_id") == P()).where(
            Field("delegation_id") == P()).where(
            Field("action") == P()).where(Field("currency") == P())
        handle.execute(usage_query.get_sql(),
                       (install_id, fx.DELEGATION, "submit-payment", "EUR"))
        cap_table = Table("authority_delegation_cap")
        cap_query = Q.from_(cap_table).delete().where(
            Field("install_id") == P()).where(
            Field("delegation_id") == P()).where(
            Field("action") == P()).where(Field("currency") == P())
        handle.execute(cap_query.get_sql(),
                       (install_id, fx.DELEGATION, "submit-payment", "EUR"))
        handle.commit()
    finally:
        handle.close()
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-payment", path, pe,
             ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before
    handle = _open(path)
    try:
        fx._insert_row(handle, "authority_delegation_cap", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "action": "submit-payment", "currency": "EUR", "scale": 2,
            "per_operation": "500.00", "aggregate_limit": "1000.00",
            "window_start": win_start, "window_end": win_end})
        fx._insert_row(handle, "authority_delegation_usage", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "action": "submit-payment", "currency": "EUR",
            "window_start": win_start, "window_end": win_end,
            "used": "0.00"})
        handle.commit()
    finally:
        handle.close()
    code, payload = _direct(
        _std("submit-payment", path, pe,
             ["--authorization-id", auth_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    rows = _read_all(path, "authority_delegation_usage",
                     ["currency", "used"])
    eur = [row for row in rows if row["currency"] == "EUR"]
    assert "100.00" in [row["used"] for row in eur]
