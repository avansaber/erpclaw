"""The plugin interface, version plugin/1: every fixture in tests/contracts/plugin/v1/ run against the engine.
A failing fixture means the interface changed; a change that breaks a plugin is a new version, served beside this
one (the fixtures' README)."""
from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import Column, String, Table, func, select

import service_plugins as sp  # tests/ is on the path (pytest rootdir)
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, ledger, models as m, plugins
from erpclaw_core.common import Ctx, Refusal, company_ctx
from erpclaw_core.contracts import Contract, Input, Result, Service, ServiceModel
from erpclaw_core.db import metadata
from erpclaw_core.manifest import EFFECTS, PUBLISHER, SERVICE_EFFECTS, Manifest
from erpclaw_core.metrics import Metric
from erpclaw_core.plugins import REGISTRY, Plugin, PluginRefused, Registry, call_service

HERE = Path(__file__).parent / "contracts" / "plugin" / "v1"
FIXTURES = sorted(HERE.glob("*.json"))
EXTRA_BRANCH = Path(__file__).parent / "fixtures" / "extra_branch"
PARTS = {"manifest", "loader", "branch", "contract", "service", "check", "question"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_every_fixture_names_the_contract_its_part_and_its_file():
    assert len(FIXTURES) >= 100
    for path in FIXTURES:
        fx = _load(path)
        assert fx["contract"] == "plugin/1" and fx["part"] in PARTS and fx["name"] == path.stem, path.name
        assert fx["expect"] and fx["note"], path.name
    assert {_load(p)["part"] for p in FIXTURES} == PARTS


# --- building planted plugins from a fixture's description --------------------------

class _In(Input):
    pass


class Closed(ServiceModel):
    value: str = ""


class OpenModel(BaseModel):
    value: str = ""


class NestedOpen(ServiceModel):
    items: list[OpenModel] = []


MODELS = {"closed": Closed, "open": OpenModel, "nested_open": NestedOpen, "not_a_model": dict}
HOOKS = {"run": lambda ctx, inp: Result("ok"), "preview": lambda ctx, inp: None,
         "commit": lambda ctx, inp, key: Result("ok"), "find_by_operation_key": lambda ctx, key: None,
         "read_set": lambda ctx, inp: []}


def build(spec: dict, migrations: Path | None = None) -> Plugin:
    """A plugin from a fixture's description: its contracts, metrics, slots and services, and a manifest derived
    from them unless the description's `manifest` overrides a field."""
    name = spec["name"]
    contracts = [Contract(c["name"], c.get("plugin", name), c["effect"], "a planted contract", _In,
                          **{h: HOOKS[h] for h in c.get("hooks", [])}) for c in spec.get("contracts", [])]
    metrics = [Metric(n, n, "a planted metric", lambda *a: None) for n in spec.get("metrics", [])]
    slots = {s: (lambda r: "x") for s in spec.get("slots", [])}
    services = [Service(s["name"], s["effect"], MODELS[s.get("input", "closed")], MODELS[s.get("output", "closed")],
                        (lambda ctx, inp: Closed()) if s.get("fn", True) else None) for s in spec.get("services", [])]
    fields = dict(name=name, version="1.0.0", publisher=PUBLISHER, distribution=f"erpclaw-{name}",
                  licence="GPL-3.0-only", source={"repository": "workspace", "commit": "workspace"},
                  engine=">=0.0.1", after_core_revision="0001", depends={}, conflicts=[], owned_tables=[],
                  contracts={c.name: c.effect for c in contracts}, calls=[], events={"emits": [], "consumes": []},
                  slots={s: {"cardinality": plugins.SLOTS.get(s, "one"), "selection": "only"} for s in slots},
                  voucher_types=[], capabilities={"network": [], "files": []}, metrics=[mt.name for mt in metrics],
                  services=[{"name": s.name, "effect": s.effect} for s in services], uses=[])
    fields.update(spec.get("manifest", {}))
    if spec.get("migrations") == "extra_branch":
        migrations = EXTRA_BRANCH
    return Plugin(manifest=Manifest(**fields), kind="business-module", contracts=contracts, metrics=metrics,
                  slots=slots, artifact_digest="0" * 64, migrations=migrations, services=services)


def _table(env, name: str, owner: str | None, existing: bool = False) -> None:
    if existing:
        env.monkeypatch.setitem(metadata.tables[name].info, "owner", owner)
        return
    t = Table(name, metadata, Column("id", String(36), primary_key=True), info={"owner": owner} if owner else {})
    env.request.addfinalizer(lambda: metadata.remove(t))


def _fill(extra: list[Plugin]) -> None:
    Registry().fill([REGISTRY.plugins[n] for n in REGISTRY.order] + extra)


# --- the runners, one per part ------------------------------------------------------

def run_manifest(fx: dict, env) -> None:
    exp = fx["expect"]
    if "invalid" in exp:
        with pytest.raises(ValidationError) as e:
            Manifest(**fx["manifest"])
        assert exp["invalid"] in {str(err["loc"][0]) for err in e.value.errors()}, e.value
        return
    digest = Manifest(**fx["manifest"]).digest()
    assert digest == exp["digest"]
    if "differs_from" in exp:
        assert digest != _load(HERE / f"{exp['differs_from']}.json")["expect"]["digest"]
    if "differs_from_manifest" in exp:
        assert digest != Manifest(**exp["differs_from_manifest"]).digest()


def run_loader(fx: dict, env) -> None:
    exp = fx["expect"]
    for spec in fx["plugins"]:
        for t in spec.get("tables", []):
            _table(env, t["name"], t["owner"], t.get("existing", False))
    if "entry_point" in fx:
        ep = fx["entry_point"]

        def load():
            if ep["object"] == "raises":
                raise ImportError("planted")
            return build(fx["plugins"][0]) if ep["object"] == "plugin" else object()

        point = SimpleNamespace(name="planted", dist=SimpleNamespace(name=ep["dist"]) if ep["dist"] else None,
                                module="erpclaw_sales", load=load)
        env.monkeypatch.setattr(plugins, "REGISTRY", Registry())
        env.monkeypatch.setattr(plugins, "_loaded", False)
        env.monkeypatch.setattr(plugins, "entry_points", lambda group: [point])
        go = plugins.load_plugins
    else:
        built = [build(spec) for spec in fx["plugins"]]

        def go():
            _fill(built)
    if exp.get("loads"):
        go()
        return
    with pytest.raises(PluginRefused) as e:
        go()
    assert exp["refused"] in str(e.value), str(e.value)


def run_branch(fx: dict, env) -> None:
    for name in fx["owned_tables"]:
        _table(env, name, "extra")
    versions = None
    if fx["revisions"] is not None:
        versions = env.tmp_path / "versions"
        versions.mkdir()
        for i, source in enumerate(fx["revisions"], 1):
            (versions / f"x_{i:04d}.py").write_text(source)
    p = build({"name": "extra", "manifest": {"owned_tables": fx["owned_tables"]}}, migrations=versions)
    if fx["expect"].get("loads"):
        _fill([p])
        return
    with pytest.raises(PluginRefused) as e:
        _fill([p])
    assert fx["expect"]["refused"] in str(e.value), str(e.value)


def _installed(env, uses: list[str] | None = None, commit=None):
    engine = env.request.getfixturevalue("engine")
    sp.RAN.clear()
    consumer = sp.consumer(uses)
    if commit is not None:
        consumer.contracts = [dataclasses.replace(c, commit=commit) if c.name == "svc_post" else c
                              for c in consumer.contracts]
    sp.install(env.monkeypatch, engine, sp.provider(), consumer)
    return engine


def run_contract(fx: dict, env) -> None:
    exp = fx["expect"]
    if fx["name"] == "contract_interface":
        assert plugins.PLUGIN_CONTRACT == exp["PLUGIN_CONTRACT"]
        assert [f.name for f in dataclasses.fields(Contract)] == exp["contract_fields"]
        assert list(EFFECTS) == exp["contract_effects"]
        assert [f.name for f in dataclasses.fields(Service)] == exp["service_fields"]
        assert list(SERVICE_EFFECTS) == exp["service_effects"]
        assert [f.name for f in dataclasses.fields(Plugin)] == exp["plugin_fields"]
        assert [f.name for f in dataclasses.fields(Ctx)] == exp["ctx_fields"]
        assert list(Manifest.model_fields) == exp["manifest_fields"]
        assert [n for n, f in Manifest.model_fields.items() if not f.is_required()] == exp["manifest_optional"]
        assert list(inspect.signature(call_service).parameters) == exp["call_service"]
        assert list(inspect.signature(Ctx.register_check).parameters)[1:] == exp["register_check"]
        return
    engine = _installed(env)
    contract = REGISTRY.contracts[fx["operation"]]
    if fx.get("planned"):
        env.monkeypatch.setattr(contract, "planned", fx["planned"])
    if "refusal" in exp:
        with pytest.raises(Refusal) as e:
            approvals.propose(engine, contract, fx["args"])
        assert e.value.code == exp["refusal"]["code"]
        return
    if contract.effect == "read":
        with engine.connect() as conn:
            res = contract.run(company_ctx(conn), contract.input_model.model_validate(fx["args"]))
        assert res.text == exp["text"] and res.data == exp["data"]
        return
    card = approvals.propose(engine, contract, fx["args"])
    assert card.card["title"] == exp["card"]["title"] and card.card["lines"] == exp["card"]["lines"]
    with engine.connect() as conn:
        stored = json.loads(conn.execute(select(m.approval.c.read_set_json)
                                         .where(m.approval.c.id == card.approval_id)).scalar_one())
    assert [t for t, _key, _digest in stored] == exp["read_set_tables"]
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == exp["status"] and out.text == exp["text"], out.text


def _symbols(value, ctx: Ctx):
    """A fixture's value with each @acct:<number> replaced by that account's id in these books."""
    if isinstance(value, str) and value.startswith("@acct:"):
        return ledger.account_by_number(ctx, value.split(":", 1)[1])["id"]
    if isinstance(value, list):
        return [_symbols(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _symbols(v, ctx) for k, v in value.items()}
    return value


def run_service(fx: dict, env) -> None:
    exp = fx["expect"]

    def invoke(ctx):
        if "refusal" in exp:
            with pytest.raises(Refusal) as e:
                call_service(ctx, fx["caller"], fx["service"], fx["input"])
            r = exp["refusal"]
            assert e.value.code == r["code"], e.value.message
            for key in ("message", "options", "data"):
                if key in r:
                    assert getattr(e.value, key) == r[key]
            return e.value
        out = call_service(ctx, fx["caller"], fx["service"], fx["input"])
        assert type(out) is dict and out == _symbols(exp["output"], ctx)
        return out

    called = []
    def approved_commit(ctx, inp, key):
        # The frozen fixture specifies an approved context, not a forged flag.
        value = invoke(ctx)
        called.append(value)
        if isinstance(value, Refusal):
            raise value  # preserve the real approve rollback/decision path
        return Result("Checked the service response.")

    in_commit = fx["on"] == "commit"
    engine = _installed(env, fx.get("uses"), approved_commit if in_commit else None)
    if fx.get("disable"):
        with engine.begin() as conn:
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == fx["disable"])
                         .values(status="disabled"))
    if in_commit:
        card = approvals.propose(engine, REGISTRY.contracts["svc_post"], {"amount": "1"})
        outcome = approvals.approve(engine, card.approval_id, card.digest)
        assert len(called) == 1
        assert outcome.status == ("refused" if "refusal" in exp else "committed"), outcome.text
    else:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                invoke(company_ctx(conn))
            finally:
                tx.rollback()


def _posted(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.posting)
                            .where(m.posting.c.voucher_type == "planted_sale")).scalar_one()


def run_check(fx: dict, env) -> None:
    exp = fx["expect"]
    engine = _installed(env)
    card = approvals.propose(engine, REGISTRY.contracts[fx["operation"]], fx["args"])
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == exp["status"], out.text
    if "code" in exp:
        assert out.data["code"] == exp["code"]
    assert sp.RAN == exp["ran"]
    assert (_posted(engine) > 0) == exp["posted"]


def run_question(fx: dict, env) -> None:
    exp = fx["expect"]
    engine = _installed(env)
    if fx["where"] == "proposal":
        def ask(ctx, inp):
            q = sp.QUESTION
            raise Refusal(q["message"], q["code"], q["options"], **q["data"])

        env.monkeypatch.setattr(REGISTRY.services[f"{sp.PROVIDER}.quote"], "fn", ask)
    client = TestClient(create_app(engine, ScriptedModel([Reply("", [call(fx["operation"], **fx["args"])])]),
                                   embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    events = client.post(f"/v1/conversations/{cid}/messages", json={"text": "record it"}).json()["events"]
    if fx["where"] == "effect":
        [card] = events
        events = client.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    [event] = events
    assert {k: event.get(k) for k in exp["event"]} == exp["event"], event
    if "posted" in exp:
        assert (_posted(engine) > 0) == exp["posted"]


RUNNERS = {"manifest": run_manifest, "loader": run_loader, "branch": run_branch, "contract": run_contract,
           "service": run_service, "check": run_check, "question": run_question}


@pytest.mark.filterwarnings("ignore:Revision 0004 referenced from")  # a refused branch names a parent in core
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_fixture_holds(path, request, monkeypatch, tmp_path):
    fx = _load(path)
    RUNNERS[fx["part"]](fx, SimpleNamespace(request=request, monkeypatch=monkeypatch, tmp_path=tmp_path))
