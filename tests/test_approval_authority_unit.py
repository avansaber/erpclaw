"""Live authority units with recorded transactions; no SQL, install or runtime qualification."""
import asyncio
import copy
import contextvars
import threading
import tomllib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.version import Version

from erpclaw_core import approval_scope as scope, approvals, core_contracts, ledger, payables, plugins
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Service, ServiceModel
from erpclaw_sales import MANIFEST as SALES
from erpclaw_stock import MANIFEST as STOCK, service as stock, goods_received, reverse_goods_received
from test_approval_scope_unit import Connection, run_approved


class Rows:
    def __init__(self, value): self.value = value
    def first(self): return self.value
    def scalar_one(self): return self.value
    def scalar_one_or_none(self): return self.value


class Recording(Connection):
    def __init__(self):
        super().__init__()
        self.queries = []
        self.contract = "invoice"
    def execute(self, stmt):
        self.queries.append(stmt)
        names = [c.name for c in stmt.selected_columns]
        if names == ["read_set_json"]: return Rows("[]")
        if names == ["contract"]: return Rows(SimpleNamespace(contract=self.contract))
        assert names == ["contract", "company_id"]
        return Rows(SimpleNamespace(contract=self.contract, company_id="co"))


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(plugins.REGISTRY, "contract_plugin", {"invoice": "stock"})
    return Ctx(Recording(), "co", "Company", "USD", date(2026, 11, 5), extra={"commit": "approval"})


@pytest.mark.parametrize("guard", [lambda c: stock.authorize(c, None, "issue"), payables._authorize,
                                 goods_received._authorize, reverse_goods_received._authorize,
                                 lambda c: stock.check_fresh(c, [])])
def test_flag_and_metadata_alone_never_authorize(ctx, guard):
    with pytest.raises(Refusal) as err: guard(ctx)
    assert err.value.code == "not_approved" and ctx.conn.queries == []


def test_live_scope_ignores_cleared_or_forged_observation_flag(ctx):
    def commit(c, *args):
        for flag in [None, "different-card"]:
            c.extra.clear()
            if flag: c.extra["commit"] = flag
            stock.authorize(c, None, "issue")
            payables._authorize(c)
            stock.check_fresh(c, [])
        assert all(q.compile().params["id_1"] == "card" for q in c.conn.queries)
        assert all(q.compile().params["company_id_1"] == "co" for q in c.conn.queries)
        assert all("status" not in [x.name for x in q.selected_columns] for q in c.conn.queries)
    run_approved(ctx, commit, owner="stock")
    assert approvals.committing(ctx) is None


def test_trusted_bootstrap_is_explicit_and_preserved(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(ledger, "lock_chain", lambda c: calls.append(c))
    ctx.extra.clear()
    stock.authorize(ctx, stock.Trusted("unit bootstrap"), "issue")
    assert calls == [ctx] and ctx.conn.queries == []
    ctx.extra["commit"] = "forged"
    with pytest.raises(ValueError): stock.authorize(ctx, stock.Trusted("unit bootstrap"), "issue")
    assert calls == [ctx]


@pytest.mark.parametrize("form", ["original", "copied_ctx", "read_only", "copied_context", "foreign_thread", "foreign_task", "revoked"])
def test_trusted_cannot_mix_with_related_issued_scope_even_without_flag(ctx, monkeypatch, form):
    calls = []
    monkeypatch.setattr(ledger, "lock_chain", lambda c: calls.append(c))
    def deny(c):
        assert not c.extra.get("commit")
        with pytest.raises(ValueError): stock.authorize(c, stock.Trusted("unit bootstrap"), "issue")
    def commit(c, *args):
        c.extra.clear()
        if form == "copied_ctx": deny(copy.copy(c))
        elif form == "read_only": scope._wrap_metric(lambda actual: deny(actual), "reports")(c)
        elif form == "copied_context": contextvars.copy_context().run(deny, c)
        elif form == "foreign_thread":
            errors = []
            def foreign():
                try: deny(copy.copy(c))
                except BaseException as exc: errors.append(exc)
            worker = threading.Thread(target=foreign)
            worker.start(); worker.join(timeout=2)
            assert not worker.is_alive() and errors == []
        elif form == "foreign_task":
            async def foreign(): deny(c)
            asyncio.run(foreign())
        elif form == "revoked":
            c.conn.invalidated = True
            assert approvals.committing(c) is None
            deny(c)
        else: deny(c)
    run_approved(ctx, commit, owner="stock")
    assert calls == [] and not scope._live


class Empty(ServiceModel): pass
class Reply(ServiceModel): ok: bool


@pytest.fixture
def services(monkeypatch):
    registry = plugins.Registry()
    registry.plugins = {n: SimpleNamespace(manifest=SimpleNamespace(uses=uses)) for n,uses in {
        "sales": ["stock.issue", "reports.read", "?optional.read"],
        "stock": ["reports.read"], "reports": ["stock.issue"]}.items()}
    registry.services = {"stock.issue": Service("issue", "write", Empty, Reply, lambda c,i:{"ok":True}),
                         "reports.read": Service("read", "read", Empty, Reply, lambda c,i:{"ok":True})}
    monkeypatch.setattr(plugins, "REGISTRY", registry)
    monkeypatch.setattr(plugins, "_known_currencies", lambda *args: None)
    enabled = []
    monkeypatch.setattr(plugins, "ensure_enabled", lambda *args: enabled.append(args))
    return registry, enabled


def test_caller_identity_precedes_enable_and_write_gate(ctx, services):
    registry, enabled = services
    def commit(c, *args):
        with scope._service(c, "sales", "stock", "write"):
            with pytest.raises(Refusal) as err: plugins.call_service(c, "sales", "stock.issue", {})
            assert err.value.code == "undeclared_service" and enabled == []
            assert plugins.call_service(c, "stock", "reports.read", {}) == {"ok":True}
    run_approved(ctx, commit)


def test_read_only_caller_is_bound_and_cannot_write(ctx, services):
    registry, enabled = services
    def read(c):
        with pytest.raises(Refusal) as err: plugins.call_service(c, "sales", "stock.issue", {})
        assert err.value.code == "undeclared_service"
        with pytest.raises(Refusal) as err: plugins.call_service(c, "reports", "stock.issue", {})
        assert err.value.code == "service_outside_commit"
        with pytest.raises(Refusal) as err: c.register_check("metric", lambda c: None)
        assert err.value.code == "service_outside_commit"
    run_approved(ctx, lambda c,*a: scope._wrap_metric(read, "reports")(c))
    assert len(enabled) == 1


def test_declared_disabled_refusal_still_precedes_scope_gate(ctx, services, monkeypatch):
    refused = Refusal("disabled", "plugin_disabled")
    def disabled(*args): raise refused
    monkeypatch.setattr(plugins, "ensure_enabled", disabled)
    with pytest.raises(Refusal) as err: plugins.call_service(ctx, "sales", "stock.issue", {})
    assert err.value is refused
    with pytest.raises(Refusal) as err: plugins.call_service(ctx, "unknown", "stock.issue", {})
    assert err.value.code == "undeclared_service"


def test_unframed_flag_cannot_register_check(ctx):
    with pytest.raises(Refusal) as err: ctx.register_check("forged", lambda c: None)
    assert err.value.code == "service_outside_commit" and ctx.checks == {}


def test_unknown_check_cannot_register_and_copied_ancestor_is_ineligible(ctx):
    def commit(c,*args):
        parent = contextvars.copy_context()
        def read(actual):
            with pytest.raises(Refusal): parent.run(actual.register_check, "copied", lambda c: None)
        scope._wrap_metric(read, "reports")(c)
    run_approved(ctx, commit)
    with scope._issue(ctx,"card","invoice","op","sales",ctx.conn.nested):
        with scope._check(ctx,"unregistered",lambda c:None):
            with pytest.raises(Refusal): ctx.register_check("unknown", lambda c: None)
    assert ctx.checks == {}


def test_reserved_source_versions_are_consistent_without_installation_claim():
    root = Path(__file__).resolve().parents[1]
    packages = [("packages/erpclaw-core", "erpclaw-core", "0.0.2", core_contracts.MANIFEST),
                ("plugins/erpclaw-sales", "erpclaw-sales", "0.0.2", SALES),
                ("plugins/erpclaw-stock", "erpclaw-stock", "0.1.1", STOCK)]
    lock = {p["name"]:p["version"] for p in tomllib.loads((root/"uv.lock").read_text())["package"]}
    for path, name, version, manifest in packages:
        project = tomllib.loads((root/path/"pyproject.toml").read_text())["project"]
        assert project["version"] == manifest.version == lock[name] == version
        assert manifest.engine == ">=0.0.2"
    # This is source metadata only. The reused interpreter is not a candidate install.
