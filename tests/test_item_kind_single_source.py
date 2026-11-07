"""Stocked-item routing and the kind question follow the same registry state."""
import pytest

from bookdiff import assert_changes, snapshot
from erpclaw_core import catalogue as cat, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.core_contracts import AddItem, preview_add_item
from erpclaw_core.plugins import REGISTRY, disable_plugin, enabled_plugins


REQUEST = "Add an item I keep in stock called Bronze Tube"


def assert_stock_routing(engine, on):
    before = snapshot(engine)
    with engine.connect() as conn:
        with pytest.raises(Refusal) as caught:
            preview_add_item(company_ctx(conn), AddItem(name="Bronze Tube", unit="meter", price="12"))
        question = caught.value
        assert question.code == "item_kind_missing"
        choices = [choice["ref"] for choice in question.data["choices"]]
        entries = {entry.id: entry for entry in cat.catalogue(conn)}
        instead = cat.planned_instead(conn, "add_item", REQUEST)
        found = cat.find_capability(conn, REQUEST)
        assert choices == (["service", "stock"] if on else ["service"])
        assert ("planned.stock_item" in entries) is not on, (
            f"kind choices {choices} disagree with catalogue: "
            f"planned.stock_item {'still listed' if 'planned.stock_item' in entries else 'absent'}"
        )
        if on:
            assert instead is None
            assert next(entry for entry in found if entry.id == "op.add_item").state == "installed"
            assert cat.decide(found, "op.add_item").outcome == "a"
            for request in ("stock item", "stocked item", "goods I keep in stock"):
                assert cat.find_capability(conn, request)[0].id == "op.add_item"
        else:
            entry = entries["planned.stock_item"]
            assert entry in found
            reply = cat.decide(found, entry.id)
            assert reply == instead
            assert reply.outcome == "c" and reply.capability == entry.id
            assert "Adding an item you keep in stock isn't available yet" in reply.text
    assert_changes(before, snapshot(engine), {})


def test_disabling_stock_keeps_the_kind_question_and_planned_reply_in_agreement(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "stock")
    assert_stock_routing(engine, False)


@pytest.mark.parametrize("switch", ["plugin", "contract", "service"])
def test_stock_kind_and_catalogue_agree_when_registry_switches_off_and_back(engine, monkeypatch, switch):
    with engine.connect() as conn:
        assert "stock" in enabled_plugins(conn)
    assert "stock.make_stock_item" in REGISTRY.services
    assert REGISTRY.contracts["add_item"].planned is None
    assert_stock_routing(engine, True)

    with monkeypatch.context() as off:
        if switch == "plugin":
            with engine.begin() as conn:
                disable_plugin(conn, "stock")
        elif switch == "contract":
            off.setattr(REGISTRY.contracts["add_item"], "planned", "Core")
        else:
            off.delitem(REGISTRY.services, "stock.make_stock_item")
        assert_stock_routing(engine, False)

    if switch == "plugin":
        with engine.begin() as conn:
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "stock")
                         .values(status="installed"))
    assert_stock_routing(engine, True)
