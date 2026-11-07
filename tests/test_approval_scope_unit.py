"""Pure approval state and registered-call orchestration; no database qualification."""
import asyncio
import copy
import contextvars
import threading
from dataclasses import FrozenInstanceError, fields
from datetime import date
from types import SimpleNamespace

import pytest

from erpclaw_core import approval_scope as scope, approvals, plugins
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Contract, Input, Service, ServiceModel
from erpclaw_core.metrics import Metric


class Transaction:
    def __init__(self, conn, previous=None):
        self.connection = conn
        self.is_active = True
        self._previous_nested = previous


class Connection:
    def __init__(self):
        self.closed = False
        self.invalidated = False
        self.root = Transaction(self)
        self.nested = Transaction(self)
    def get_transaction(self):
        return self.root
    def get_nested_transaction(self):
        return self.nested
    def execute(self, *args, **kwargs):
        raise AssertionError("SQL forbidden in pure units")


@pytest.fixture
def ctx():
    return Ctx(Connection(), "co", "Company", "USD", date(2026, 11, 5), extra={"commit": "card"})


def run_approved(ctx, fn, *, key="op", owner="sales", contract="invoice"):
    wrapped = scope._wrap_contract(fn, owner, contract, "commit")
    with scope._issue(ctx, "card", contract, key, owner, ctx.conn.nested):
        return wrapped(ctx, None, key)


def test_public_query_without_core_issuer_never_trusts_flag(ctx):
    assert approvals.committing(ctx) is None
    ctx.extra.clear()
    assert approvals.committing(ctx) is None


def test_unframed_issuer_is_ineligible(ctx):
    with scope._issue(ctx, "card", "invoice", "op", "sales", ctx.conn.nested):
        assert approvals.committing(ctx) is None


def test_exact_registered_commit_identity_and_non_authority_return(ctx):
    def commit(actual, inp, key):
        assert actual is ctx and key == "op"
        assert approvals.committing(actual) == "card"
        actual.extra["commit"] = "forged"
        assert approvals.committing(actual) == "card"
        identity = scope._current().identity
        assert (identity.approval_id, identity.company_id, identity.contract,
                identity.operation_key, identity.owner) == ("card", "co", "invoice", "op", "sales")
        with pytest.raises(FrozenInstanceError):
            identity.company_id = "other"
        return "legacy result"
    assert run_approved(ctx, commit) == "legacy result"
    assert approvals.committing(ctx) is None


@pytest.mark.parametrize("copy_kind", ["shallow", "new"])
def test_copied_ctx_does_not_inherit_scope(ctx, copy_kind):
    def commit(actual, *unused):
        other = copy.copy(actual) if copy_kind == "shallow" else Ctx(
            actual.conn, actual.company_id, actual.company_name, actual.currency, actual.today,
            extra=actual.extra, checks=actual.checks)
        assert approvals.committing(other) is None
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


@pytest.mark.parametrize("change", ["company", "connection", "root", "effect", "closed", "invalidated", "root_inactive", "effect_inactive", "missing_link", "cycle"])
def test_mismatched_or_lost_transaction_fails_closed(ctx, change):
    def commit(actual, *unused):
        conn = actual.conn
        if change == "company": actual.company_id = "other"
        elif change == "connection": actual.conn = Connection()
        elif change == "root": conn.root = Transaction(conn)
        elif change == "effect": conn.nested = Transaction(conn)
        elif change == "closed": conn.closed = True
        elif change == "invalidated": conn.invalidated = True
        elif change == "root_inactive": conn.root.is_active = False
        elif change == "effect_inactive": conn.nested.is_active = False
        elif change == "missing_link": conn.nested = SimpleNamespace(connection=conn, is_active=True)
        else:
            conn.nested = Transaction(conn)
            conn.nested._previous_nested = conn.nested
        assert approvals.committing(actual) is None
    run_approved(ctx, commit)


def test_valid_descendant_savepoint_keeps_original_effect(ctx):
    def commit(actual, *unused):
        original = actual.conn.nested
        actual.conn.nested = Transaction(actual.conn, Transaction(actual.conn, original))
        assert approvals.committing(actual) == "card"
        actual.conn.nested = original
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_missing_transaction_api_is_ineligible(ctx):
    ctx.conn = object()
    with scope._issue(ctx, "card", "invoice", "op", "sales", object()):
        assert scope._wrap_contract(lambda c, *a: approvals.committing(c), "sales", "invoice", "commit")(ctx, None, "op") is None


def test_scope_exception_revokes_saved_context(ctx):
    saved = []
    def commit(actual, *unused):
        saved.append(contextvars.copy_context())
        assert approvals.committing(actual) == "card"
        raise RuntimeError("original failure")
    with pytest.raises(RuntimeError, match="original failure"):
        run_approved(ctx, commit)
    assert saved[0].run(approvals.committing, ctx) is None
    assert not scope._live


@pytest.mark.parametrize("kind", ["run", "preview", "read_set", "find_by_operation_key"])
def test_all_registered_read_callbacks_are_read_only(ctx, kind):
    def commit(actual, *unused):
        read = scope._wrap_contract(lambda c, *a: approvals.committing(c), "reports", "read", kind)
        assert read(actual, None) is None
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_saved_parent_cannot_hide_metric_and_child_cannot_replay_after_exit(ctx):
    captured = []
    def commit(actual, *unused):
        parent = contextvars.copy_context()
        def metric(c, *args):
            captured.append(contextvars.copy_context())
            assert approvals.committing(c) is None
            assert parent.run(approvals.committing, c) is None
            # A registered service entered through the replay still cannot borrow authority.
            with scope._service(c, "sales", "stock", "write"):
                assert parent.run(approvals.committing, c) is None
                assert approvals.committing(c) is None
            return "metric result"
        assert scope._wrap_metric(metric, "reports")(actual) == "metric result"
        assert approvals.committing(actual) == "card"
        assert captured[0].run(approvals.committing, actual) is None
    run_approved(ctx, commit)
    assert captured[0].run(approvals.committing, ctx) is None


def test_same_task_service_ancestor_copy_cannot_spend_parent_owner(ctx):
    def commit(actual, *unused):
        parent = contextvars.copy_context()
        with scope._service(actual, "sales", "stock", "write"):
            assert scope._current().cursor.owner == "stock"
            assert approvals.committing(actual) == "card"
            assert parent.run(approvals.committing, actual) is None
            def forged():
                with scope._service(actual, "sales", "other", "write"):
                    assert approvals.committing(actual) is None
            parent.run(forged)
            assert approvals.committing(actual) == "card"
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_thread_copy_cannot_transfer_observation(ctx):
    def commit(actual, *unused):
        copied = contextvars.copy_context(); result = []
        thread = threading.Thread(target=lambda: result.append(copied.run(approvals.committing, actual)))
        thread.start(); thread.join(timeout=2)
        assert not thread.is_alive() and result == [None]
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_async_child_task_cannot_transfer_observation(ctx):
    async def main():
        with scope._issue(ctx, "card", "invoice", "op", "sales", ctx.conn.nested):
            with scope._frame(ctx, "sales", "commit", readonly=False, root_contract="invoice", operation_key="op"):
                assert approvals.committing(ctx) == "card"
                async def child(): return approvals.committing(ctx)
                assert await asyncio.create_task(child()) is None
                assert approvals.committing(ctx) == "card"
    asyncio.run(main())


def test_process_identity_mismatch_is_ineligible(ctx, monkeypatch):
    def commit(actual, *unused):
        original = scope.os.getpid()
        with monkeypatch.context() as change:
            change.setattr(scope.os, "getpid", lambda: original + 1)
            assert approvals.committing(actual) is None
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_wrong_contract_key_or_owner_never_observes_issuer(ctx):
    with scope._issue(ctx, "card", "invoice", "op", "sales", ctx.conn.nested):
        for owner, name, key in [("other", "invoice", "op"), ("sales", "other", "op"), ("sales", "invoice", "other")]:
            assert scope._wrap_contract(lambda c, *a: approvals.committing(c), owner, name, "commit")(ctx, None, key) is None


def test_check_registration_snapshot_dedup_owner_and_legacy_refusal(ctx):
    calls = []
    def late(c): calls.append("late")
    def check(c):
        calls.append("first")
        assert approvals.committing(c) is None
        assert scope._current().cursor.owner == "stock"
        c.register_check("late", late)  # preserve legacy acceptance; no new execution this snapshot
    def commit(actual, *unused):
        with scope._service(actual, "sales", "stock", "write"):
            actual.register_check("first", check)
            actual.register_check("first", lambda c: calls.append("duplicate"))
        assert actual.checks["first"] is check
    with scope._issue(ctx, "card", "invoice", "op", "sales", ctx.conn.nested):
        scope._wrap_contract(commit, "sales", "invoice", "commit")(ctx, None, "op")
        for key, fn in list(ctx.checks.items()):
            with scope._check(ctx, key, fn): fn(ctx)
    assert calls == ["first"] and list(ctx.checks) == ["first", "late"]
    ctx.extra.clear()
    with pytest.raises(Refusal) as exc: ctx.register_check("outside", check)
    assert exc.value.code == "service_outside_commit"


def test_nested_approval_observation_cannot_borrow_outer(ctx):
    def commit(actual, *unused):
        with scope._issue(actual, "second", "invoice", "op2", "sales", actual.conn.nested):
            assert approvals.committing(actual) is None
        assert approvals.committing(actual) == "card"
    run_approved(ctx, commit)


def test_registry_wraps_actual_callbacks_without_fields_or_repeat_nesting(ctx, monkeypatch):
    observed = []
    def record(c, *a): observed.append(approvals.committing(c)); return "result"
    contract = Contract("invoice", "sales", "write", "example", Input,
                        run=record, preview=record, commit=record, read_set=record,
                        find_by_operation_key=record)
    metric = Metric("metric", "Metric", "definition", record)
    plugin = SimpleNamespace(name="sales", contracts=[contract], metrics=[metric], slots={}, services=[])
    monkeypatch.setattr(plugins, "_check_plugin", lambda p: None)
    monkeypatch.setattr(plugins, "_check_set", lambda ps: {p.name: p for p in ps})
    monkeypatch.setattr(plugins, "_dependency_order", lambda ps: list(ps))
    registry = plugins.Registry()
    before = [f.name for f in fields(contract)]
    for _ in range(2): registry.fill([plugin])
    assert [f.name for f in fields(contract)] == before
    assert contract.commit.__wrapped__ is record and contract.run.__wrapped__ is record
    with scope._issue(ctx, "card", "invoice", "op", "sales", ctx.conn.nested):
        assert registry.contracts["invoice"].commit(ctx, None, "op") == "result"
        for name in ["run", "preview", "read_set", "find_by_operation_key"]:
            assert getattr(registry.contracts["invoice"], name)(ctx, None) == "result"
        assert registry.metrics["metric"].run(ctx) == "result"
    assert observed == ["card", None, None, None, None, None]


class Empty(ServiceModel): pass
class Reply(ServiceModel): approval: str | None


@pytest.fixture
def service_registry(monkeypatch):
    registry = plugins.Registry()
    registry.plugins = {n: SimpleNamespace(manifest=SimpleNamespace(uses=uses)) for n, uses in {
        "sales": ["stock.issue", "reports.metric"], "stock": ["reports.metric"]}.items()}
    registry.services["stock.issue"] = Service("issue", "write", Empty, Reply, lambda c, i: {"approval": approvals.committing(c)})
    registry.services["reports.metric"] = Service("metric", "read", Empty, Reply, lambda c, i: {"approval": approvals.committing(c)})
    monkeypatch.setattr(plugins, "REGISTRY", registry)
    monkeypatch.setattr(plugins, "ensure_enabled", lambda *a: None)
    monkeypatch.setattr(plugins, "_known_currencies", lambda *a: None)
    return registry


def test_real_call_service_requires_scope_and_preserves_results(ctx, service_registry):
    with pytest.raises(Refusal) as err: plugins.call_service(ctx, "sales", "stock.issue", {})
    assert err.value.code == "service_outside_commit"
    def commit(actual, *unused):
        assert plugins.call_service(actual, "sales", "stock.issue", {}) == {"approval": "card"}
        assert plugins.call_service(actual, "sales", "reports.metric", {}) == {"approval": None}
        # A current provider cannot impersonate its caller.
        with scope._service(actual, "sales", "stock", "write"):
            with pytest.raises(Refusal) as err: plugins.call_service(actual, "sales", "stock.issue", {})
            assert err.value.code == "undeclared_service"
            assert plugins.call_service(actual, "stock", "reports.metric", {}) == {"approval": None}
    run_approved(ctx, commit)


def test_legacy_service_refusal_precedence_and_passthrough(ctx, service_registry):
    ctx.extra.clear()
    with pytest.raises(Refusal) as err: plugins.call_service(ctx, "unknown", "stock.issue", {})
    assert err.value.code == "undeclared_service"
    with pytest.raises(Refusal) as err: plugins.call_service(ctx, "sales", "stock.issue", {})
    assert err.value.code == "service_outside_commit"
    ctx.extra["commit"] = "card"
    refusal = Refusal("original", "stale", marker="preserved")
    def refused(*args): raise refusal
    service_registry.services["stock.issue"].fn = refused
    with pytest.raises(Refusal) as err:
        run_approved(ctx, lambda c, *a: plugins.call_service(c, "sales", "stock.issue", {}))
    assert err.value is refusal and err.value.data == {"marker": "preserved"}
