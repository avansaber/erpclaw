"""The gate binds one ledger context per consumed envelope."""
import ast
import json
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402
import authority_fixtures as fx  # noqa: E402
from setup_helpers import freeze_snapshot, init_all_tables, read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import P, Q, Table, ValueWrapper  # noqa: E402

AUTH_COLS = ["id", "install_id", "principal_id", "action",
             "binding_digest", "delegation_id", "issued_at",
             "expires_at", "revoked_at", "consumed_at", "consumed_txn"]
RESULT_COLS = ["authorization_id", "consumed_txn", "result_kind",
               "result_id", "result_status", "recorded_at"]
_CHAIN_COLS = ("company_id", "last_sequence", "last_checksum", "updated_at")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


def _setup(monkeypatch, fixed):
    from erpclaw_lib import authority_gate
    monkeypatch.setitem(
        authority_gate.ENVELOPE_ACTIONS, "add-uom", fx.make_declaration())
    if fixed is not None:
        from erpclaw_lib import authority_clock
        monkeypatch.setattr(
            authority_clock, "now_ms", lambda: fixed)
    return authority_gate


def _fresh(tmp_path, monkeypatch, tag):
    path = str(tmp_path / ("gate-context-%s.sqlite" % tag))
    init_all_tables(path)
    monkeypatch.setenv("ERPCLAW_DB_PATH", path)
    company_id = fx.seed_company(path)
    return path, fx.seed_authority(path, company_id)


def _issue(target, info, key="key-1", amount="60.00", name="Crate"):
    from erpclaw_lib import authorization_issuance
    conn = get_connection(target)
    try:
        return authorization_issuance.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action="add-uom",
            argv=fx.standard_argv(
                info["company_id"], amount=amount, name=name),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=key)
    finally:
        conn.close()


def _snapshot(target):
    handle = get_connection(target)
    try:
        return freeze_snapshot(handle, target, seam.table_names(target))
    finally:
        handle.close()


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


def _auth_row(target, auth_id):
    rows = fx.read_rows(target, "operation_authorization", AUTH_COLS)
    return next(row for row in rows if row["id"] == auth_id)


def _result_row(target, auth_id):
    found = fx.read_rows(target, "operation_authorization_result", RESULT_COLS)
    return next(
        (row for row in found if row["authorization_id"] == auth_id), None)


def _uom_names(target):
    return sorted(
        row["name"] for row in fx.read_rows(target, "uom", ["id", "name"]))


def _insert_chain(handle, company_id):
    table = Table("gl_chain_head")
    query = Q.into(table).columns(*_CHAIN_COLS).insert(
        *[P() for _ in _CHAIN_COLS]).get_sql()
    handle.execute(query, (company_id, 0, "9" * 64, "2026-01-01"))


def _chain_rows(target, company_id):
    handle = get_connection(target)
    try:
        rows = read_all(handle, "gl_chain_head",
                        ["company_id", "last_sequence"])
    finally:
        handle.close()
    return [row for row in rows if row["company_id"] == company_id]


def test_context_bound_only_during_handler(tmp_path, monkeypatch):
    target, info = _fresh(tmp_path, monkeypatch, "bound")
    gate = _setup(monkeypatch, info["now"])
    out = _issue(target, info, key="bound-1")
    auth_id = out["authorization_id"]
    from erpclaw_lib import authority_sink
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    unbind_calls = _spy_calls(monkeypatch, authority_sink, "unbind")
    seen = []

    def _handler(proxy):
        real = object.__getattribute__(proxy, "_target")
        seen.append(authority_sink.current(real))
        fx.make_handler()(proxy)

    conn = get_connection(target)
    try:
        payload = gate.verify_and_consume(
            conn, authorization_id=auth_id, action="add-uom",
            argv=fx.standard_argv(info["company_id"]),
            handler=_handler)
        assert payload["status"] == "ok"
        assert authority_sink.current(conn) is None
    finally:
        conn.close()
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.authorization_id == auth_id
    assert ctx.action == "add-uom"
    assert ctx.label == "staged_unattested"
    fresh = get_connection(target)
    try:
        assert ctx.install_id == fx._install_id(fresh)
    finally:
        fresh.close()
    stored = _auth_row(target, auth_id)
    assert stored["consumed_txn"] == ctx.txn_id
    assert len(bind_calls) == 1
    assert len(unbind_calls) == 1
    assert bind_calls[0][2] == unbind_calls[0][0][1]


def test_gate_rollback_clears_poison_and_context(tmp_path, monkeypatch):
    from erpclaw_lib import authority_sink
    target, info = _fresh(tmp_path, monkeypatch, "rollback-catch")
    gate = _setup(monkeypatch, info["now"])
    out = _issue(target, info, key="rollback-1")
    auth_id = out["authorization_id"]
    before = _snapshot(target)
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    clear_calls = _spy_calls(monkeypatch, authority_sink, "clear")
    real_phase = gate.install_phase

    def _boom(conn_arg):
        raise gate.AuthorityRefusal(gate.AUTHORITY_NOT_READY)

    company_id = info["company_id"]

    def _handler(proxy):
        monkeypatch.setattr(gate, "install_phase", _boom)
        try:
            _insert_chain(proxy, company_id)
        except authority_sink.LedgerWriteRefused:
            pass
        fx.make_handler()(proxy)

    conn = get_connection(target)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(company_id), handler=_handler)
        assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
        assert authority_sink.current(conn) is None
        _select_one(conn)
    finally:
        conn.close()
    assert _auth_row(target, auth_id)["consumed_at"] is None
    assert _uom_names(target) == []
    assert _chain_rows(target, company_id) == []
    assert _snapshot(target) == before
    assert len(bind_calls) == 1
    assert len(clear_calls) == 1
    assert clear_calls[0][0][1] == bind_calls[0][2]
    monkeypatch.setattr(gate, "install_phase", real_phase)

    target2, info2 = _fresh(tmp_path, monkeypatch, "rollback-bare")
    gate2 = _setup(monkeypatch, info2["now"])
    out2 = _issue(target2, info2, key="rollback-2")
    auth2 = out2["authorization_id"]
    before2 = _snapshot(target2)
    bind2 = _spy_calls(monkeypatch, authority_sink, "bind")
    clear2 = _spy_calls(monkeypatch, authority_sink, "clear")
    company2 = info2["company_id"]

    def _handler2(proxy):
        monkeypatch.setattr(gate2, "install_phase", _boom)
        _insert_chain(proxy, company2)
        fx.make_handler()(proxy)

    conn2 = get_connection(target2)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
            gate2.verify_and_consume(
                conn2, authorization_id=auth2, action="add-uom",
                argv=fx.standard_argv(company2), handler=_handler2)
        assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
        assert authority_sink.current(conn2) is None
        _select_one(conn2)
    finally:
        conn2.close()
    assert _auth_row(target2, auth2)["consumed_at"] is None
    assert _uom_names(target2) == []
    assert _chain_rows(target2, company2) == []
    assert _snapshot(target2) == before2
    assert len(bind2) == 1
    assert len(clear2) == 1
    assert clear2[0][0][1] == bind2[0][2]
    monkeypatch.setattr(gate2, "install_phase", real_phase)

    target3, info3 = _fresh(tmp_path, monkeypatch, "rollback-control")
    gate3 = _setup(monkeypatch, info3["now"])
    out3 = _issue(target3, info3, key="rollback-3")
    company3 = info3["company_id"]

    def _handler3(proxy):
        _insert_chain(proxy, company3)
        fx.make_handler()(proxy)

    conn3 = get_connection(target3)
    try:
        payload3 = gate3.verify_and_consume(
            conn3, authorization_id=out3["authorization_id"],
            action="add-uom", argv=fx.standard_argv(company3),
            handler=_handler3)
    finally:
        conn3.close()
    assert payload3["status"] == "ok"
    assert _uom_names(target3) == ["Crate"]
    assert len(_chain_rows(target3, company3)) == 1


def test_handler_error_clears_context(tmp_path, monkeypatch):
    target, info = _fresh(tmp_path, monkeypatch, "handler-error")
    gate = _setup(monkeypatch, info["now"])
    out = _issue(target, info, key="handler-error-1")
    auth_id = out["authorization_id"]
    from erpclaw_lib import authority_sink
    bind_calls = _spy_calls(monkeypatch, authority_sink, "bind")
    unbind_calls = _spy_calls(monkeypatch, authority_sink, "unbind")
    clear_calls = _spy_calls(monkeypatch, authority_sink, "clear")
    uid = str(uuid.uuid4())

    def _handler(proxy):
        table = Table("uom")
        query = Q.into(table).columns(
            "id", "name", "must_be_whole_number").insert(
            P(), P(), P()).get_sql()
        proxy.execute(query, (uid, "Crate", 0))
        print(json.dumps({"status": "ok", "uom_id": uid}))
        proxy.commit()
        proxy.commit()

    conn = get_connection(target)
    try:
        with pytest.raises(gate.AuthorityRefusal) as excinfo:
            gate.verify_and_consume(
                conn, authorization_id=auth_id, action="add-uom",
                argv=fx.standard_argv(info["company_id"]),
                handler=_handler)
        assert excinfo.value.args == (gate.HANDLER_COMMIT_INVALID,)
        assert authority_sink.current(conn) is None
        _select_one(conn)
    finally:
        conn.close()
    assert _auth_row(target, auth_id)["consumed_at"] is None
    assert _uom_names(target) == []
    assert len(bind_calls) == 1
    assert len(unbind_calls) == 1
    assert len(clear_calls) == 1
    assert unbind_calls[0][0][1] == bind_calls[0][2]
    assert clear_calls[0][0][1] == bind_calls[0][2]


def test_consume_has_one_product_caller():
    here = os.path.dirname(os.path.abspath(__file__))
    root = here
    while True:
        if os.path.isdir(os.path.join(root, "source")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            raise AssertionError("repository root not found")
        root = parent
    base = os.path.join(root, "source")
    skipped = ("tests", "test", "vendor", "__pycache__")
    calls = []
    imports = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.func = None

        def visit_FunctionDef(self, node):
            outer = self.func
            self.func = node.name
            self.generic_visit(node)
            self.func = outer

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "consume":
                calls.append((current_path, self.func, node.lineno))
            elif isinstance(func, ast.Attribute) and func.attr == "consume":
                calls.append((current_path, self.func, node.lineno))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            for alias in node.names:
                if alias.name == "consume":
                    imports.append((current_path, self.func, node.lineno))

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            item for item in dirnames if item not in skipped)
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if name == "conftest.py":
                continue
            if name.startswith("test_"):
                continue
            current_path = os.path.join(dirpath, name)
            with open(current_path, "r", encoding="utf-8") as handle:
                text = handle.read()
            try:
                tree = ast.parse(text, filename=current_path)
            except SyntaxError:
                pytest.fail("unparseable file: " + current_path)
            _Visitor().visit(tree)
    assert len(calls) == 1, calls
    assert calls[0][0].endswith(
        os.path.join("erpclaw_lib", "authority_gate.py")), calls
    assert calls[0][1] == "verify_and_consume", calls
    assert len(imports) == 1, imports
    assert imports[0][0].endswith(
        os.path.join("erpclaw_lib", "authority_gate.py")), imports


def test_result_stored_in_consuming_transaction(tmp_path, monkeypatch):
    target, info = _fresh(tmp_path, monkeypatch, "ordering")
    gate = _setup(monkeypatch, info["now"])
    out = _issue(target, info, key="ordering-1")
    auth_id = out["authorization_id"]
    events = []
    real_consume = gate.consume

    def _spy_consume(conn_arg, **kwargs):
        outcome = real_consume(conn_arg, **kwargs)
        events.append(("consume", id(conn_arg), kwargs.get("txn_id")))
        return outcome

    monkeypatch.setattr(gate, "consume", _spy_consume)
    real_result = gate.record_result

    def _spy_result(conn_arg, **kwargs):
        outcome = real_result(conn_arg, **kwargs)
        events.append(("result", id(conn_arg), kwargs.get("consumed_txn")))
        return outcome

    monkeypatch.setattr(gate, "record_result", _spy_result)
    probe = get_connection(target)
    real_commit = type(probe).commit
    probe.close()

    def _spy_commit(self):
        events.append(("commit", id(self)))
        return real_commit(self)

    monkeypatch.setattr(type(probe), "commit", _spy_commit)
    handle = get_connection(target)
    try:
        payload = gate.verify_and_consume(
            handle, authorization_id=auth_id, action="add-uom",
            argv=fx.standard_argv(info["company_id"]),
            handler=fx.make_handler())
    finally:
        handle.close()
    assert payload["status"] == "ok"
    mine = [entry for entry in events if entry[1] == id(handle)]
    assert [entry[0] for entry in mine] == ["consume", "result", "commit"]
    assert mine[0][2] == mine[1][2]
    auth = _auth_row(target, auth_id)
    result = _result_row(target, auth_id)
    assert result is not None
    assert result["consumed_txn"] == auth["consumed_txn"] == mine[0][2]
