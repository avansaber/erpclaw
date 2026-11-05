"""The plugin interface, version plugin/3: plugins 1 and 2 plus optional uses, an entry of `uses` ending in `?`. Every
fixture in tests/contracts/plugin/v3/ runs against the engine; every plugin/1 and plugin/2 fixture still passes
unchanged (test_plugin_contract_v1.py, test_plugin_contract_v2.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import service_plugins as sp  # tests/ is on the path (pytest rootdir)
from erpclaw_core.plugins import PluginRefused, Registry, disable_plugin
from test_optional_uses import QUOTE_IN, call, loaded, optional_consumer, refused

HERE = Path(__file__).parent / "contracts" / "plugin" / "v3"
FIXTURES = sorted(HERE.glob("*.json"))
PARTS = {"loader", "manifest", "call"}


def test_every_fixture_names_the_contract_its_part_and_its_file():
    assert FIXTURES
    for path in FIXTURES:
        fx = json.loads(path.read_text())
        assert fx["contract"] == "plugin/3" and fx["part"] in PARTS and fx["name"] == path.stem, path


def _consumer(fx):
    return optional_consumer(uses=[f"{sp.PROVIDER}.{u}" for u in fx["uses"]],
                             depends={sp.PROVIDER: ">=1"} if fx.get("depends") else None)


def run_loader(fx, engine, monkeypatch):
    plugins = ([sp.provider()] if fx["provider"] else []) + [_consumer(fx)]
    if "refused" in fx["expect"]:
        with pytest.raises(PluginRefused, match=fx["expect"]["refused"]):
            Registry().fill(loaded() + plugins)
    else:
        Registry().fill(loaded() + plugins)


def run_manifest(fx, engine, monkeypatch):
    for uses in fx["invalid_uses"]:
        with pytest.raises(ValidationError):
            sp.manifest("shapes", depends={sp.PROVIDER: ">=1"}, uses=uses)


def run_call(fx, engine, monkeypatch):
    sp.install(monkeypatch, engine, *(([sp.provider()] if fx["provider"] else []) + [_consumer(fx)]))
    if fx.get("disabled"):
        with engine.begin() as conn:
            disable_plugin(conn, sp.PROVIDER)
    name = f"{sp.PROVIDER}.quote"
    exp = fx["expect"]
    if exp.get("runs"):
        assert call(engine, name, QUOTE_IN)
        return
    e = refused(engine, name, QUOTE_IN)
    assert e.code == exp["code"] and "?" not in str(e)
    if "text" in exp:
        assert str(e) == exp["text"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_fixture_holds(path, engine, monkeypatch):
    fx = json.loads(path.read_text())
    {"loader": run_loader, "manifest": run_manifest, "call": run_call}[fx["part"]](fx, engine, monkeypatch)
