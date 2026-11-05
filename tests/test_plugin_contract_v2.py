"""The plugin interface, version plugin/2: plugin/1 plus the two message slots. Every fixture
in tests/contracts/plugin/v2/ runs against the engine; every plugin/1 fixture still passes unchanged
(test_plugin_contract_v1.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from erpclaw_core import models as m, outbox, plugins
from erpclaw_core.common import company_ctx
from erpclaw_core.outbox import SendOutcome, SenderSnapshot, deliver_pending, enqueue, owner_text
from erpclaw_core.plugins import REGISTRY, PluginRefused, Registry, disable_plugin, install_record

from slot_support import loaded_without_mailers  # every plugin kept, its tables owned
from test_plugins import plug

HERE = Path(__file__).parent / "contracts" / "plugin" / "v2"
FIXTURES = sorted(HERE.glob("*.json"))
PARTS = {"slots", "loader", "send"}
SENDER = SenderSnapshot(from_address="books@acme.example", from_name="Acme Books", reply_to=None, read_entry=None)



def test_every_fixture_names_the_contract_its_part_and_its_file():
    assert FIXTURES
    for path in FIXTURES:
        fx = json.loads(path.read_text())
        assert fx["contract"] == "plugin/2" and fx["part"] in PARTS and fx["name"] == path.stem, path


def run_slots(fx, engine):
    for slot, card in fx["expect"].items():
        assert plugins.SLOTS[slot] == card


def run_loader(fx, engine):
    extra = [plug(p["name"], slots={s: (lambda *a: None) for s in p["slots"]}) for p in fx["plugins"]]
    load = lambda: Registry().fill(loaded_without_mailers() + extra)  # noqa: E731
    if "refused" in fx["expect"]:
        with pytest.raises(PluginRefused, match=fx["expect"]["refused"]):
            load()
    else:
        load()


def run_send(fx, engine, monkeypatch):
    plan = fx["connector"]

    def connector(settings, msg, mark_data):
        if plan.get("mark"):
            assert mark_data() is True
        if plan.get("raise"):
            raise RuntimeError("planted")
        return SendOutcome(**plan["outcome"])

    p = plug("mailer", slots={"send_message": connector})
    r = Registry()
    r.fill(loaded_without_mailers() + [p])
    monkeypatch.setattr(plugins, "REGISTRY", r)
    monkeypatch.setattr(outbox, "REGISTRY", r)
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
        install_record(conn, p)
        oid = enqueue(company_ctx(conn), "deliver_message", "ar@acme.example", "Hi", "Body", None, sender=SENDER)
    deliver_pending(engine)
    with engine.connect() as conn:
        row = dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())
    assert row["status"] == fx["expect"]["status"]
    assert owner_text(row).startswith(fx["expect"]["owner_text_starts"]), owner_text(row)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_fixture_holds(path, engine, monkeypatch):
    fx = json.loads(path.read_text())
    if fx["part"] == "send":
        run_send(fx, engine, monkeypatch)
    else:
        {"slots": run_slots, "loader": run_loader}[fx["part"]](fx, engine)
