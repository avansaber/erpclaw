"""The plugin interface, version plugin/4: plugins 1 to 3 plus WriteContract, a write that says whether it moves money
and why it takes no date when it takes none. Every fixture in tests/contracts/plugin/v4/ runs against the engine; every
plugin/1 to plugin/3 fixture still passes unchanged."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from erpclaw_core.contracts import Contract, WriteContract, moves_money
from erpclaw_core.plugins import PluginRefused
from test_write_contract import _Dated, _Dateless, _line, _load, _write

HERE = Path(__file__).parent / "contracts" / "plugin" / "v4"
FIXTURES = sorted(HERE.glob("*.json"))
PARTS = {"contract", "loader", "card"}
V1_CONTRACT_FIELDS = json.loads((HERE.parent / "v1" / "contract_interface.json").read_text())["expect"]["contract_fields"]


def test_every_fixture_names_the_contract_its_part_and_its_file():
    assert FIXTURES
    for path in FIXTURES:
        fx = json.loads(path.read_text())
        assert fx["contract"] == "plugin/4" and fx["part"] in PARTS and fx["name"] == path.stem, path


def _built(w: dict) -> Contract:
    cls = WriteContract if w["cls"] == "WriteContract" else Contract
    return _write(reason=w["reason"], money=w["moves_money"], inp=_Dateless if w["dateless"] else _Dated, cls=cls)


def run_contract(fx, engine):
    exp = fx["expect"]
    assert [f.name for f in dataclasses.fields(Contract)] == V1_CONTRACT_FIELDS
    added = [f for f in dataclasses.fields(WriteContract) if f.name not in V1_CONTRACT_FIELDS]
    assert [f.name for f in added] == exp["write_contract_adds"] and all(f.kw_only for f in added)
    assert dataclasses.fields(WriteContract)[len(V1_CONTRACT_FIELDS)].default is dataclasses.MISSING


def run_loader(fx, engine):
    c, exp = _built(fx["write"]), fx["expect"]
    if "refused" in exp:
        with pytest.raises(PluginRefused, match=exp["refused"]):
            _load(c)
    else:
        _load(c)
        assert moves_money(c) is exp["moves_money"]


def run_card(fx, engine):
    w = fx["write"]
    assert _line(engine, _built(w), _Dateless() if w["dateless"] else _Dated()) == fx["expect"]["line"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_fixture_holds(path, engine):
    fx = json.loads(path.read_text())
    {"contract": run_contract, "loader": run_loader, "card": run_card}[fx["part"]](fx, engine)
