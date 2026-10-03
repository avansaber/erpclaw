"""Journal postings behind single-use authorization."""
import io
import json
import os
import sys
import uuid
import pytest
_JG_TESTS = os.path.dirname(os.path.abspath(__file__))
_JG_MODULE = os.path.dirname(_JG_TESTS)
_JG_SETUP_TESTS = os.path.join(os.path.dirname(_JG_MODULE), "erpclaw-setup", "tests")
if _JG_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _JG_SETUP_TESTS)
if _JG_TESTS not in sys.path:
    sys.path.insert(0, _JG_TESTS)

DATE = "2026-06-20"
_SEEDED = set()
_SEED_CAPS = set()

_ROUTER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(_JG_MODULE), "db_query.py"))


class _Forwarded(Exception):
    def __init__(self, argv):
        super().__init__("forwarded")
        self.argv = list(argv)


def _helpers():
    import journals_helpers as helpers
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
    return _helpers().build_journals_env(handle)


def _std(action, path, je_id, extra=None):
    argv = ["--action", action, "--db-path", path, "--journal-entry-id", je_id]
    if extra:
        argv = argv + list(extra)
    return argv


def _je(path, env, amount="100.00"):
    helpers = _helpers()
    handle = _open(path)
    try:
        lines = json.dumps([
            {"account_id": env["expense"], "debit": amount, "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": amount,
             "cost_center_id": env["cc"]},
        ])
        result = helpers.call_action(
            helpers.load_db_query().add_journal_entry, handle,
            helpers.ns(company_id=env["company_id"], posting_date=DATE,
                       entry_type="journal", remark=None, lines=lines,
                       cwip_asset_id=None, dimensions=None,
                       dimension_key=None, dimension_value=None))
    finally:
        handle.close()
    assert result.get("status") == "ok", result
    return result["journal_entry_id"]


def _grant(path, company_id, je_id, action):
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
            "company_id": company_id, "resource_kind": "journal-entry",
            "resource_id": je_id, "action": action, "effect": "allow"})
        fx._insert_row(handle, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company_id, "resource_kind": "journal-entry",
            "resource_id": je_id, "action": action})
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
    lib = os.path.join(os.path.dirname(_JG_MODULE), "erpclaw-setup", "lib")
    lib = os.path.abspath(lib)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    name = "erpclaw_router_jeg_%s" % abs(hash(home))
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


def _update_entry(path, je_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("journal_entry")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(je_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _update_lines(path, je_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("journal_entry_line")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("journal_entry_id") == P())
        params.append(je_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _update_one_line(path, line_id, values):
    from erpclaw_lib.query import Field, P, Q, Table
    handle = _open(path)
    try:
        table = Table("journal_entry_line")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(line_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _line_ids(path, je_id):
    rows = _read_all(path, "journal_entry_line", ["id", "journal_entry_id"])
    return sorted(row["id"] for row in rows if row["journal_entry_id"] == je_id)


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
    je = _je(path, env)
    code, payload = _via_router(
        ["--action", "submit-journal-entry", "--db-path", path,
         "--journal-entry-id", je], tmp_path, monkeypatch)
    assert code == 2
    assert payload.get("error") == "user_confirmation_required"
    code, payload = _via_router(
        ["--action", "submit-journal-entry", "--db-path", path,
         "--journal-entry-id", je, "--user-confirmed"],
        tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    gl_rows = [row for row in _read_all(
        path, "gl_entry", ["id", "voucher_id"]) if row["voucher_id"] == je]
    assert len(gl_rows) == 2
    assert _read_all(path, "operation_authorization_result",
                      ["authorization_id"]) == []
    audits = _read_all(path, "audit_log", ["authorization_id"])
    assert [row for row in audits if row["authorization_id"]] == []
    code, payload = _direct(
        ["--action", "get-journal-entry", "--db-path", path,
         "--journal-entry-id", je])
    assert code == 0, payload
    assert payload.get("status") == "ok"


@pytest.mark.parametrize("via", ["direct", "router"])
def test_active_without_envelope_refuses(db_env, tmp_path, monkeypatch, via):
    """Refusal without envelope at ACTIVE; not qualification."""
    path, env = db_env
    je = _je(path, env)
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    if via == "direct":
        argv = _std("submit-journal-entry", path, je)
        before = _snapshot(path)
        code, payload = _direct(argv)
    else:
        argv = _std("submit-journal-entry", path, je,
                    ["--user-confirmed"])
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
        code, payload = _direct(_std("submit-journal-entry", path, je))
    else:
        code, payload = _via_router(
            _std("submit-journal-entry", path, je, ["--user-confirmed"]),
            tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORITY_NOT_READY"
    assert _snapshot(path) == before
    _patch_ready(monkeypatch)
    from erpclaw_lib import actor as _absent_actor
    monkeypatch.setattr(_absent_actor, "current", lambda: _absent_actor.ActorContext(None, None, None, (), _absent_actor.ABSENT))
    _absent_before = _snapshot(path)
    code, payload = _direct(
        ["--action", "get-journal-entry", "--db-path", path,
         "--journal-entry-id", je])
    assert code == 1, payload
    assert payload.get("message") == "COMPANY_SCOPE_REFUSED"
    assert _snapshot(path) == _absent_before
    fx.seed_authority(path, env["company_id"])
    _SEEDED.add(path)
    _patch_actor(monkeypatch)
    code, payload = _direct(
        ["--action", "get-journal-entry", "--db-path", path,
         "--journal-entry-id", je])
    assert code == 0, payload
    code, payload = _direct(
        ["--action", "add-journal-entry", "--db-path", path,
         "--company-id", env["company_id"], "--posting-date", DATE,
         "--lines", json.dumps([
             {"account_id": env["expense"], "debit": "10.00",
              "credit": "0", "cost_center_id": env["cc"]},
             {"account_id": env["cash"], "debit": "0",
              "credit": "10.00", "cost_center_id": env["cc"]},
         ])])
    assert code == 0, payload


@pytest.mark.parametrize("via", ["direct", "router"])
def test_valid_envelope_consumes(db_env, tmp_path, monkeypatch, via):
    """Consume one envelope at ACTIVE; not qualification."""
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued = _issue_full(path, "submit-journal-entry",
                         _std("submit-journal-entry", path, je))
    assert issued["issued_route"] == "delegation"
    auth_id = issued["authorization_id"]
    if via == "direct":
        code, payload = _direct(
            _std("submit-journal-entry", path, je,
                 ["--authorization-id", auth_id]))
    else:
        code, payload = _via_router(
            _std("submit-journal-entry", path, je,
                 ["--user-confirmed", "--authorization-id", auth_id]),
            tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    entry = _read_one(path, "journal_entry", ["id", "status"], je)
    assert entry["status"] == "submitted"
    gl_rows = [row for row in _read_all(
        path, "gl_entry", ["id", "voucher_id"]) if row["voucher_id"] == je]
    assert len(gl_rows) == 2
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is not None
    result = _result_row(path, auth_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("journal-entry", je, "submitted")
    audits = [row for row in _read_all(
        path, "audit_log",
        ["authorization_id", "authorization_status"])
        if row["authorization_id"] == auth_id]
    assert len(audits) == 1
    assert audits[0]["authorization_status"] == "verified"
    usage = _read_all(path, "authority_delegation_usage", ["used"])
    assert "100.00" in [row["used"] for row in usage]
    second = _je(path, env, amount="50.00")
    _grant(path, env["company_id"], second, "submit-journal-entry")
    _slids = _line_ids(path, second)
    from decimal import Decimal as _D
    for _lid in _slids:
        _row = _read_one(path, "journal_entry_line",
                         ["id", "debit", "credit"], _lid)
        if _D(_row["debit"]) != 0:
            _update_one_line(path, _lid, {"debit": "40.00"})
        else:
            _update_one_line(path, _lid, {"credit": "40.00"})
    _update_entry(path, second,
                  {"total_debit": "40.00", "total_credit": "40.00"})
    second_issued = _issue_full(path, "submit-journal-entry",
                                _std("submit-journal-entry", path, second))
    second_id = second_issued["authorization_id"]
    lids = _line_ids(path, second)
    _update_one_line(path, lids[0], {"debit": "50.00"})
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-journal-entry", path, second,
             ["--authorization-id", second_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth2 = _read_one(path, "operation_authorization",
                      ["id", "consumed_at"], second_id)
    assert auth2["consumed_at"] is None
    assert _snapshot(path) == before


def test_valid_envelope_readiness_unpatched(db_env, monkeypatch):
    """Unready ACTIVE refuses before spend; not qualification."""
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-journal-entry",
                     _std("submit-journal-entry", path, je))
    from erpclaw_lib import authority_readiness
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-journal-entry", path, je,
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
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-journal-entry",
                     _std("submit-journal-entry", path, je))
    from erpclaw_lib import authority_gate
    monkeypatch.setattr(authority_gate, "install_phase",
                        lambda conn: ("ACTIVE", "other-install"))
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-journal-entry", path, je,
             ["--authorization-id", auth_id]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], auth_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before


def test_state_digest_binds_the_entry(db_env):
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    auth_id = _issue(path, "submit-journal-entry",
                     _std("submit-journal-entry", path, je))
    base = _std("submit-journal-entry", path, je,
                ["--authorization-id", auth_id])
    entry_before = _read_one(
        path, "journal_entry",
        ["id", "posting_date", "total_debit", "total_credit"], je)
    lids = _line_ids(path, je)
    line_before = _read_one(
        path, "journal_entry_line",
        ["id", "debit", "credit", "dimensions_json"], lids[0])
    line2_before = _read_one(
        path, "journal_entry_line",
        ["id", "debit", "credit", "dimensions_json"], lids[1])

    def _try(argv):
        before = _snapshot(path)
        code, payload = _direct(argv)
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REFUSED"
        auth = _read_one(path, "operation_authorization",
                         ["id", "consumed_at"], auth_id)
        assert auth["consumed_at"] is None
        assert _snapshot(path) == before

    from decimal import Decimal as _D2
    for _lid in lids:
        _row = _read_one(path, "journal_entry_line",
                         ["id", "debit", "credit"], _lid)
        if _D2(_row["debit"]) != 0:
            _update_one_line(path, _lid, {"debit": "90.00"})
        else:
            _update_one_line(path, _lid, {"credit": "90.00"})
    _update_entry(path, je, {"total_debit": "90.00", "total_credit": "90.00"})
    _try(base)
    _update_one_line(path, lids[0], {"debit": line_before["debit"],
                                     "credit": line_before["credit"]})
    _update_one_line(path, lids[1], {"debit": line2_before["debit"],
                                     "credit": line2_before["credit"]})
    _update_entry(path, je, {"total_debit": entry_before["total_debit"],
                             "total_credit": entry_before["total_credit"]})
    _update_entry(path, je, {"posting_date": "2026-07-01"})
    _try(base)
    _update_entry(path, je, {"posting_date": entry_before["posting_date"]})
    _update_one_line(path, lids[0], {"dimensions_json": '{"zone": "north"}'})
    _try(base)
    _update_one_line(
        path, lids[0], {"dimensions_json": line_before["dimensions_json"]})
    _try(base + ["--remark", "x"])
    code, payload = _direct(base)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def test_projection_reads_the_database(db_env):
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    from erpclaw_lib import authority_gate
    handle = _open(path)
    try:
        derived = authority_gate.ENVELOPE_ACTIONS[
            "submit-journal-entry"]["derive"](
            handle, "submit-journal-entry",
            [["journal-entry-id", je]])
    finally:
        handle.close()
    entry = _read_one(path, "journal_entry",
                      ["id", "company_id", "currency"], je)
    assert derived["company_ids"] == [entry["company_id"]]
    assert len(derived["targets"]) == 1
    assert derived["targets"][0]["kind"] == "journal-entry"
    assert derived["targets"][0]["id"] == je
    full = _read_one(path, "journal_entry",
                     ["id", "company_id", "naming_series", "posting_date",
                      "entry_type", "total_debit", "total_credit", "currency",
                      "exchange_rate", "remark", "status", "amended_from",
                      "cwip_asset_id", "dimensions_json"], je)
    assert full["company_id"] == env["company_id"]
    assert full["posting_date"] == DATE
    assert full["entry_type"] == "journal"
    assert full["total_debit"] == "100.00"
    assert full["total_credit"] == "100.00"
    assert full["currency"] == "USD"
    assert full["exchange_rate"] == "1"
    assert full["remark"] is None
    assert full["status"] == "draft"
    assert full["amended_from"] is None
    assert full["cwip_asset_id"] is None
    assert full["dimensions_json"] == "{}"
    rows = _read_all(path, "journal_entry_line",
                     ["id", "journal_entry_id", "account_id", "party_type",
                      "party_id", "debit", "credit", "cost_center_id",
                      "project_id", "remark", "dimensions_json"])
    own = sorted([r for r in rows if r["journal_entry_id"] == je],
                 key=lambda r: r["id"])
    assert len(own) == 2
    by_acct = {r["account_id"]: r for r in own}
    assert set(by_acct) == {env["expense"], env["cash"]}
    exp_row = by_acct[env["expense"]]
    cash_row = by_acct[env["cash"]]
    assert exp_row["debit"] == "100.00"
    assert exp_row["credit"] == "0.00"
    assert exp_row["cost_center_id"] == env["cc"]
    assert cash_row["debit"] == "0.00"
    assert cash_row["credit"] == "100.00"
    assert cash_row["cost_center_id"] == env["cc"]
    want_entry = {"amended_from": None, "company_id": env["company_id"],
                  "currency": "USD", "cwip_asset_id": None,
                  "dimensions_json": "{}", "entry_type": "journal",
                  "exchange_rate": "1", "id": je,
                  "naming_series": full["naming_series"],
                  "posting_date": DATE, "remark": None, "status": "draft",
                  "total_credit": "100.00", "total_debit": "100.00"}
    want_lines = sorted([{"account_id": env["expense"],
                          "cost_center_id": env["cc"], "credit": "0.00",
                          "debit": "100.00", "dimensions_json": "{}",
                          "id": exp_row["id"], "party_id": None,
                          "party_type": None, "project_id": None,
                          "remark": None},
                         {"account_id": env["cash"],
                          "cost_center_id": env["cc"], "credit": "100.00",
                          "debit": "0.00", "dimensions_json": "{}",
                          "id": cash_row["id"], "party_id": None,
                          "party_type": None, "project_id": None,
                          "remark": None}],
                        key=lambda r: r["id"])
    import hashlib as _hl
    import json as _js
    want_text = _js.dumps({"entry": want_entry, "lines": want_lines},
                          sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    want_digest = _hl.sha256(want_text.encode("utf-8")).hexdigest()
    assert derived["targets"][0]["state_digest"] == want_digest
    assert derived["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    _update_entry(path, je, {"total_debit": "100", "total_credit": "100"})
    handle = _open(path)
    try:
        shaped = authority_gate.ENVELOPE_ACTIONS[
            "submit-journal-entry"]["derive"](
            handle, "submit-journal-entry",
            [["journal-entry-id", je]])
    finally:
        handle.close()
    assert shaped["amounts"] == [{"currency": "USD", "value": "100.00",
                                  "scale": 2}]
    _update_entry(path, je,
                  {"total_debit": "100.125", "total_credit": "100.125"})
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-journal-entry"]["derive"](
                handle, "submit-journal-entry",
                [["journal-entry-id", je]])
    finally:
        handle.close()
    _update_entry(path, je,
                  {"total_debit": "100.00", "total_credit": "100.00"})
    _update_entry(path, je,
                  {"total_debit": "90.00", "total_credit": "90.00"})
    handle = _open(path)
    try:
        import pytest as _pt_sum
        with _pt_sum.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-journal-entry"]["derive"](
                handle, "submit-journal-entry",
                [["journal-entry-id", je]])
    finally:
        handle.close()
    _update_entry(path, je,
                  {"total_debit": "100.00", "total_credit": "100.00"})
    other = _read_all(path, "company", ["id"])
    second_company = None
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
                "submit-journal-entry"]["derive"](
                handle, "submit-journal-entry",
                [["journal-entry-id", je],
                 ["company-id", second_company]])
    finally:
        handle.close()
    import pytest as _pt3
    from erpclaw_lib import authorization_issuance
    from erpclaw_lib import authority_gate as _gate
    handle = _open(path)
    try:
        with _pt3.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="submit-journal-entry",
                argv=["--journal-entry-id", je, "--company-id",
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


def test_cancel_and_amend(db_env):
    path, env = db_env
    je = _je(path, env)
    code, payload = _direct(_std("submit-journal-entry", path, je))
    assert code == 0, payload
    _gl_cols = _seam().column_names("gl_entry", path)
    before_rows = _read_all(path, "gl_entry", _gl_cols)
    before_two = sorted(
        (row for row in before_rows if row["voucher_id"] == je),
        key=lambda row: row["id"])
    assert len(before_two) == 2
    _grant(path, env["company_id"], je, "cancel-journal-entry")
    cancel_id = _issue(path, "cancel-journal-entry",
                       _std("cancel-journal-entry", path, je))
    code, payload = _direct(
        _std("cancel-journal-entry", path, je,
             ["--authorization-id", cancel_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "cancelled"
    result = _result_row(path, cancel_id)
    assert (result["result_kind"], result["result_id"],
            result["result_status"]) == ("journal-entry", je, "cancelled")
    after_rows = _read_all(path, "gl_entry", _gl_cols)
    after_two = sorted(
        (row for row in after_rows if row["voucher_id"] == je),
        key=lambda row: row["id"])
    assert len(after_two) > len(before_two)
    after_by_id = {row["id"]: row for row in after_two}
    for row in before_two:
        kept = dict(after_by_id[row["id"]])
        assert kept.pop("is_cancelled") == 1
        wanted = dict(row)
        assert wanted.pop("is_cancelled") == 0
        assert kept == wanted
    other = _je(path, env, amount="75.00")
    code, payload = _direct(_std("submit-journal-entry", path, other))
    assert code == 0, payload
    _grant(path, env["company_id"], other, "amend-journal-entry")
    amend_id = _issue(path, "amend-journal-entry",
                      _std("amend-journal-entry", path, other))
    code, payload = _direct(
        _std("amend-journal-entry", path, other,
             ["--authorization-id", amend_id]))
    assert code == 0, payload
    assert payload.get("document_status") == "created"
    assert payload.get("new_journal_entry_id")
    amend_result = _result_row(path, amend_id)
    assert amend_result["result_kind"] == "journal-entry"
    assert amend_result["result_id"] == payload["new_journal_entry_id"]
    assert amend_result["result_status"] == "created"


def test_replay_after_submit(db_env, monkeypatch):
    """Replay returns stored result; not qualification."""
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-journal-entry",
                     _std("submit-journal-entry", path, je))
    argv = _std("submit-journal-entry", path, je,
                ["--authorization-id", auth_id])
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"
    before = _snapshot(path)
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("replayed") is True
    assert payload.get("authorization_id") == auth_id
    assert payload.get("result_kind") == "journal-entry"
    assert payload.get("result_id") == je
    assert payload.get("result_status") == "submitted"
    assert _snapshot(path) == before


def test_undeclared_action(db_env, monkeypatch, caplog):
    """Missing declaration warns then refuses; not qualification."""
    import logging
    path, env = db_env
    je = _je(path, env)
    from erpclaw_lib import action_impact
    monkeypatch.delitem(action_impact.IMPACT, "get-journal-entry")
    audits_before = _read_all(path, "audit_log", ["id"])
    with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
        code, payload = _direct(
            ["--action", "get-journal-entry", "--db-path", path,
             "--journal-entry-id", je])
    assert code == 0, payload
    assert any("impact" in rec.message and "undeclared" in rec.message
               for rec in caplog.records
               if rec.levelname == "WARNING")
    audits_after = _read_all(path, "audit_log", ["id"])
    assert len(audits_after) == len(audits_before)
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    before = _snapshot(path)
    code, payload = _direct(
        ["--action", "get-journal-entry", "--db-path", path,
         "--journal-entry-id", je])
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert _snapshot(path) == before


def test_unprojected_and_non_envelope_actions(db_env, monkeypatch):
    """Unprojected refuses with or without id; not qualification."""
    path, env = db_env
    je = _je(path, env)
    _grant(path, env["company_id"], je, "submit-journal-entry")
    from erpclaw_lib import authorization_issuance
    handle = _open(path)
    try:
        import pytest as _pt
        with _pt.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="delete-journal-entry",
                argv=["--journal-entry-id", je],
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
    code, payload = _direct(_std("delete-journal-entry", path, je))
    assert code == 1, payload
    assert payload.get("message") == "IMPACT_UNDECLARED"
    assert _snapshot(path) == before
    submit_id = _issue(path, "submit-journal-entry",
                       _std("submit-journal-entry", path, je))
    before = _snapshot(path)
    code, payload = _direct(
        ["--action", "get-journal-entry", "--db-path", path,
         "--journal-entry-id", je, "--authorization-id", submit_id])
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_REFUSED"
    auth = _read_one(path, "operation_authorization",
                     ["id", "consumed_at"], submit_id)
    assert auth["consumed_at"] is None
    assert _snapshot(path) == before


def test_authorization_id_forms(db_env):
    from erpclaw_lib import authority_gate
    rest, found = authority_gate.split_authorization_id(
        ["--action", "submit-journal-entry", "--authorization-id", "abc-1"])
    assert found == "abc-1"
    assert rest == ["--action", "submit-journal-entry"]
    rest, found = authority_gate.split_authorization_id(
        ["--action", "submit-journal-entry", "--authorization-id=abc-1"])
    assert found == "abc-1"
    assert rest == ["--action", "submit-journal-entry"]
    import pytest as _pt
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    with _pt.raises(ValueError) as exc:
        authority_gate.split_authorization_id(
            ["--authorization-id", "a-1", "--authorization-id", "b-2"])
    assert exc.value.args == (INPUT_INVALID,)
    with _pt.raises(ValueError) as exc:
        authority_gate.split_authorization_id(["--authorization-id"])
    assert exc.value.args == (INPUT_INVALID,)
    with _pt.raises(ValueError) as exc:
        authority_gate.split_authorization_id(["--authorization-id", "-x"])
    assert exc.value.args == (INPUT_INVALID,)
    with _pt.raises(ValueError) as exc:
        authority_gate.split_authorization_id(["--authorization-id", "a b"])
    assert exc.value.args == (INPUT_INVALID,)
    path, env = db_env
    je = _je(path, env)
    before = _snapshot(path)
    code, payload = _direct(
        _std("submit-journal-entry", path, je,
             ["--authorization-id", "a-1", "--authorization-id", "b-2"]))
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _snapshot(path) == before


@pytest.mark.parametrize("via", ["direct", "router"])
def test_abbreviated_option_cannot_switch_target(db_env, tmp_path, monkeypatch, via):
    path, env = db_env
    entry_a = _je(path, env)
    entry_b = _je(path, env)
    _grant(path, env["company_id"], entry_a, "submit-journal-entry")
    _grant(path, env["company_id"], entry_b, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    issued_argv = ["--action", "submit-journal-entry", "--db-path", path,
                   "--journal-entry-id", entry_a, "--journal-entry", entry_b]
    auth_id = _issue(path, "submit-journal-entry", issued_argv)
    from erpclaw_lib import authority_gate as _gate_abbrev
    def _must_not_read(conn):
        raise AssertionError("refusal happens before any read")
    monkeypatch.setattr(_gate_abbrev, "install_phase", _must_not_read)
    consume_argv = ["--action", "submit-journal-entry", "--db-path", path,
                    "--journal-entry-id", entry_a, "--journal-entry", entry_b,
                    "--authorization-id", auth_id]
    before = _snapshot(path)
    if via == "direct":
        code, payload = _direct(consume_argv)
    else:
        router_argv = ["--action", "submit-journal-entry", "--db-path", path,
                       "--journal-entry-id", entry_a, "--journal-entry", entry_b,
                       "--user-confirmed", "--authorization-id", auth_id]
        code, payload = _via_router(router_argv, tmp_path, monkeypatch)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _read_one(path, "journal_entry", ["id", "status"], entry_a)["status"] == "draft"
    assert _read_one(path, "journal_entry", ["id", "status"], entry_b)["status"] == "draft"
    assert _read_one(path, "operation_authorization", ["id", "consumed_at"], auth_id)["consumed_at"] is None
    assert _snapshot(path) == before


def test_duplicate_option_refuses(db_env):
    path, env = db_env
    entry_a = _je(path, env)
    _grant(path, env["company_id"], entry_a, "submit-journal-entry")
    company = env["company_id"]
    issued_argv = ["--action", "submit-journal-entry", "--db-path", path,
                   "--journal-entry-id", entry_a, "--company-id", company,
                   "--company-id", company]
    auth_id = _issue(path, "submit-journal-entry", issued_argv)
    consume_argv = issued_argv + ["--authorization-id", auth_id]
    before = _snapshot(path)
    code, payload = _direct(consume_argv)
    assert code == 1, payload
    assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
    assert _read_one(path, "journal_entry", ["id", "status"], entry_a)["status"] == "draft"
    assert _read_one(path, "operation_authorization", ["id", "consumed_at"], auth_id)["consumed_at"] is None
    assert _snapshot(path) == before
    entry_d = _je(path, env)
    _grant(path, env["company_id"], entry_d, "submit-journal-entry")
    dim_argv = ["--action", "submit-journal-entry", "--db-path", path,
                "--journal-entry-id", entry_d, "--dimension-key", "zone",
                "--dimension-value", "north", "--dimension-key", "zone2",
                "--dimension-value", "south"]
    dim_id = _issue(path, "submit-journal-entry", dim_argv)
    code, payload = _direct(dim_argv + ["--authorization-id", dim_id])
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def test_missing_option_list_refuses_before_any_read(db_env, monkeypatch):
    path, env = db_env
    entry_a = _je(path, env)
    _grant(path, env["company_id"], entry_a, "submit-journal-entry")
    auth_id = _issue(path, "submit-journal-entry", _std("submit-journal-entry", path, entry_a))
    from erpclaw_lib import authority_gate as _gate_missing
    from erpclaw_lib.authorization_consumption import INPUT_INVALID as _INVALID
    def _must_not_read(conn):
        raise AssertionError("refusal happens before any read")
    monkeypatch.setattr(_gate_missing, "install_phase", _must_not_read)
    called = []
    def _handler(conn):
        called.append(True)
        return {"status": "ok"}
    argv = _std("submit-journal-entry", path, entry_a, ["--authorization-id", auth_id])
    handle = _open(path)
    try:
        before = _snapshot(path)
        import pytest as _pt_missing
        with _pt_missing.raises(ValueError) as exc:
            _gate_missing.run(handle, "submit-journal-entry", argv, _handler)
        assert exc.value.args == (_INVALID,)
        assert called == []
        assert _snapshot(path) == before
        assert _read_one(path, "operation_authorization", ["id", "consumed_at"], auth_id)["consumed_at"] is None
    finally:
        handle.close()


def test_equals_form_of_exact_option_consumes(db_env, monkeypatch):
    path, env = db_env
    entry_a = _je(path, env)
    _grant(path, env["company_id"], entry_a, "submit-journal-entry")
    import authority_fixtures as fx
    fx.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    auth_id = _issue(path, "submit-journal-entry", _std("submit-journal-entry", path, entry_a))
    argv = ["--action", "submit-journal-entry", "--db-path", path,
            "--journal-entry-id=" + entry_a, "--authorization-id", auth_id]
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


def test_no_id_abbreviation_unchanged(db_env):
    path, env = db_env
    entry_a = _je(path, env)
    argv = ["--action", "submit-journal-entry", "--db-path", path,
            "--journal-entry", entry_a]
    code, payload = _direct(argv)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted"


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
            "id": str(_uuid.uuid4()), "entity_type": "journal_entry",
            "prefix": "JE-", "current_value": 0,
            "company_id": company_id})
        cc_id = str(_uuid.uuid4())
        fx._insert_row(handle, "cost_center", {
            "id": cc_id, "name": "Main CC", "company_id": company_id,
            "is_group": 0})
        cash = str(_uuid.uuid4())
        fx._insert_row(handle, "account", {
            "id": cash, "name": "Cash", "account_number": "1000",
            "root_type": "asset", "account_type": "cash",
            "balance_direction": "debit_normal", "company_id": company_id,
            "depth": 0})
        expense = str(_uuid.uuid4())
        fx._insert_row(handle, "account", {
            "id": expense, "name": "Purchases",
            "account_number": "5000", "root_type": "expense",
            "account_type": "expense",
            "balance_direction": "debit_normal",
            "company_id": company_id, "depth": 0})
        handle.commit()
    finally:
        handle.close()
    return {"company_id": company_id, "cash": cash, "expense": expense,
            "cc": cc_id}


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_leg(tmp_path, monkeypatch):
    """Postgres leg mirrors the envelope flow; not qualification."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam = _seam()
    seam.dispose_engines()
    try:
        _pg_reset_schema()
        import setup_helpers as setup
        setup.init_all_tables(None)
        env = _pg_seed(None)
        import authority_fixtures as fx
        from erpclaw_lib.db import get_connection as _gc
        handle = _gc()
        try:
            je_id = _je(None, env)
        finally:
            pass
        company_id = env["company_id"]
        _grant(None, company_id, je_id, "submit-journal-entry")
        fx.make_active(None)
        _patch_ready(monkeypatch)
        _patch_actor(monkeypatch)
        auth_id = _issue(None, "submit-journal-entry",
                         ["--action", "submit-journal-entry",
                          "--journal-entry-id", je_id])
        code, payload = _direct(
            ["--action", "submit-journal-entry",
             "--journal-entry-id", je_id,
             "--authorization-id", auth_id])
        assert code == 0, payload
        assert payload.get("document_status") == "submitted"
        other = _je(None, env, amount="20.00")
        _grant(None, company_id, other, "submit-journal-entry")
        before = _snapshot(None)
        code, payload = _direct(
            ["--action", "submit-journal-entry",
             "--journal-entry-id", other])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_REQUIRED"
        assert _snapshot(None) == before
        entry_a = _je(None, env, amount="30.00")
        entry_b = _je(None, env, amount="30.00")
        _grant(None, company_id, entry_a, "submit-journal-entry")
        _grant(None, company_id, entry_b, "submit-journal-entry")
        abbrev_issue = ["--action", "submit-journal-entry",
                        "--journal-entry-id", entry_a,
                        "--journal-entry", entry_b]
        abbrev_id = _issue(None, "submit-journal-entry", abbrev_issue)
        before = _snapshot(None)
        code, payload = _direct(
            ["--action", "submit-journal-entry",
             "--journal-entry-id", entry_a, "--journal-entry", entry_b,
             "--authorization-id", abbrev_id])
        assert code == 1, payload
        assert payload.get("message") == "AUTHORIZATION_INPUT_INVALID"
        assert _read_one(None, "journal_entry", ["id", "status"], entry_a)["status"] == "draft"
        assert _read_one(None, "journal_entry", ["id", "status"], entry_b)["status"] == "draft"
        assert _read_one(None, "operation_authorization", ["id", "consumed_at"], abbrev_id)["consumed_at"] is None
        assert _snapshot(None) == before
    finally:
        seam.dispose_engines()
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
