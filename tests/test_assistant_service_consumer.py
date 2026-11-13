"""The assistant declares its own service use without becoming an installable plugin."""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import service_plugins as sp
from bookdiff import assert_changes, snapshot
from erpclaw_assistant import resolve
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import core_contracts, models as m, plugins
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.manifest import parse_use
from erpclaw_stock import service as stock

WAREHOUSES = "stock.list_warehouses"


@pytest.fixture
def discovered_plugin(monkeypatch):
    def discover(name):
        plugin = plugins.Plugin(manifest=sp.manifest(name, distribution="erpclaw-sales"),
                                kind="business-module", artifact_digest="0" * 64)
        entries = list(plugins.entry_points(group=plugins.GROUP))
        entries.append(SimpleNamespace(name=name, dist=SimpleNamespace(name="erpclaw-sales"),
                                       module="erpclaw_assistant", load=lambda: plugin))
        monkeypatch.setattr(plugins, "entry_points", lambda group: entries)
        monkeypatch.setattr(plugins, "REGISTRY", plugins.Registry())
        monkeypatch.setattr(plugins, "_loaded", False)
        return plugin

    return discover


def test_loader_refuses_a_plugin_named_for_a_service_consumer(discovered_plugin):
    discovered_plugin("assistant")
    with pytest.raises(plugins.PluginRefused) as caught:
        plugins.load_plugins()
    assert str(caught.value) == ("plugin assistant: its name is reserved for a built-in service consumer. "
                                 "Rename the plugin.")
    assert not plugins._loaded
    assert "assistant" not in plugins.REGISTRY.plugins


def test_loader_accepts_a_plugin_with_an_unreserved_name(discovered_plugin):
    plugin = discovered_plugin("warehouse_reader")
    assert plugins.load_plugins().plugins[plugin.name] is plugin


@pytest.mark.parametrize("action", ["install_plugin", "install_preview", "install_into", "install_record"])
def test_install_refuses_a_service_consumer_name_without_writes(engine, monkeypatch, action):
    from erpclaw_core import migrate

    plugin = plugins.Plugin(manifest=sp.manifest("assistant"), kind="business-module", artifact_digest="0" * 64,
                            migrations=Path(__file__).parent / "fixtures" / "extra_branch")
    # Plant an entry directly to exercise install even if loader validation was bypassed.
    monkeypatch.setitem(plugins.REGISTRY.plugins, plugin.name, plugin)
    migrated = []
    monkeypatch.setattr(migrate, "upgrade_plugin", lambda *args: migrated.append(args))
    before = snapshot(engine)
    with engine.connect() as conn:
        revisions = migrate.applied_revisions(conn)
    with engine.begin() as conn:
        with pytest.raises(plugins.PluginRefused) as caught:
            if action == "install_plugin":
                plugins.install_plugin(engine, plugin.name)
            elif action == "install_preview":
                plugins.install_preview(plugin)
            else:
                getattr(plugins, action)(conn, plugin)
    assert str(caught.value) == ("plugin assistant: its name is reserved for a built-in service consumer. "
                                 "Rename the plugin.")
    assert_changes(before, snapshot(engine), {})
    assert migrated == []
    with engine.connect() as conn:
        assert migrate.applied_revisions(conn) == revisions


def test_assistant_service_declarations_come_only_from_code(monkeypatch):
    extra = "stock.preview_create_warehouse"
    plugin = plugins.Plugin(manifest=sp.manifest("assistant", uses=[extra + "?"]),
                            kind="business-module", artifact_digest="0" * 64)
    monkeypatch.setitem(plugins.REGISTRY.plugins, plugin.name, plugin)
    assert plugins.declares_service("assistant", WAREHOUSES)
    assert not plugins.declares_service("assistant", extra)
    monkeypatch.setitem(plugins.SERVICE_CONSUMERS, "assistant", ())
    assert not plugins.declares_service("assistant", WAREHOUSES)
    assert not plugins.declares_service("assistant", extra)


@pytest.fixture
def warehouse_books(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        assert "stock" in plugins.enabled_plugins(conn)
        stock.create_warehouse(ctx, "Main Store", default=True)
    return engine


def recorded_warehouses(ctx):
    session = resolve.Session(owner_messages=[{"text": "Main Store", "field": "warehouse",
                                              "question_id": "warehouse-choice", "answer_ref": "Main Store"}])
    resolve.record_message_names(ctx, session, [], [])
    return [span for span in session.owner_messages[0]["spans"] if span["kind"] == "warehouse"]


def warehouse_lookup_reply(engine):
    invoice = call("create_invoice", customer="Acme Corporation",
                   items=[{"item": "Circuit Board", "quantity": 5, "warehouse": "Main Store"}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice])]))
    events = assistant.turn("warehouse-lookup", "invoice Acme Corporation for 5 Circuit Board from Main Store")
    # With no stock service, the unknown source does not turn a service item into warehouse stock.
    assert [event["type"] for event in events] == ["card"], events
    card = events[0]
    assert not any(label == "From" for label, value in card["card"]["lines"])
    with engine.connect() as conn:
        args = json.loads(conn.execute(select(m.approval.c.args_json).where(
            m.approval.c.id == card["approval_id"])).scalar_one())
    assert [(line["item"], line["quantity"], line.get("warehouse")) for line in args["items"]] == [
        ("Circuit Board", "5", None)]


def test_assistant_service_callers_are_honest(warehouse_books):
    facade = Path(resolve.__file__)
    paths = [facade, *sorted(facade.parent.glob("resolve_*.py"))]
    calls = [node for path in paths for node in ast.walk(ast.parse(path.read_text()))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "call_service"]
    # The third caller retrieves stored warehouse spellings for ambiguity.
    # It honestly calls as the assistant using its declared optional service.
    assert len(calls) == 3
    assert [(ast.literal_eval(call.args[1]), ast.literal_eval(call.args[2]), ast.literal_eval(call.args[3]))
            for call in calls] == [("assistant", WAREHOUSES, {})] * 3
    with warehouse_books.connect() as conn:
        ctx = company_ctx(conn)
        assert resolve._company_warehouse_names(ctx) == {"main store"}
        assert [span["name"] for span in recorded_warehouses(ctx)] == ["Main Store"]
    assert plugins.declares_service("assistant", WAREHOUSES)


def test_assistant_declared_service_returns_the_providers_output(warehouse_books):
    with warehouse_books.connect() as conn:
        out = plugins.call_service(company_ctx(conn), "assistant", WAREHOUSES, {})
    assert plugins.declares_service("assistant", WAREHOUSES)
    assert plugins.REGISTRY.services[WAREHOUSES].output_model.model_validate(out).model_dump(mode="json") == out
    assert len(out["warehouses"]) == 1
    assert out["warehouses"][0]["name"] == "Main Store"


def test_core_declares_exactly_the_stock_services_it_calls():
    tree = ast.parse(Path(core_contracts.__file__).read_text())
    calls = {"stock." + ast.literal_eval(node.args[1]) for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_stock_setup_call"}
    uses = {parse_use(use)[0] for use in core_contracts.MANIFEST.uses}
    assert uses == calls
    assert WAREHOUSES in calls


def test_undeclared_assistant_and_plugin_calls_have_the_same_refusal(warehouse_books, monkeypatch, caplog):
    caller = plugins.Plugin(manifest=sp.manifest("warehouse_reader", uses=[WAREHOUSES + "?"]),
                            kind="business-module", artifact_digest="0" * 64)
    sp.register(monkeypatch, caller)
    name = "stock.preview_create_warehouse"
    assert name in plugins.REGISTRY.services
    ran = []
    service = plugins.REGISTRY.services[name]
    original = service.fn

    def probe(ctx, inp):
        ran.append(True)
        return original(ctx, inp)

    monkeypatch.setattr(service, "fn", probe)
    before = snapshot(warehouse_books)
    for identity in (caller.name, "assistant"):
        caplog.clear()
        with caplog.at_level("WARNING", logger="erpclaw_core.plugins"), warehouse_books.begin() as conn:
            with pytest.raises(Refusal) as caught:
                plugins.call_service(company_ctx(conn), identity, name, {"name": "Another Store"})
        assert caught.value.code == "undeclared_service"
        assert str(caught.value) == "Something went wrong inside your books' software, so nothing was changed."
        assert caplog.record_tuples == [("erpclaw_core.plugins", 30,
            f"Service {name} refused (undeclared_service): caller={identity}, owner=None")]
        assert "Another Store" not in caplog.text
        assert "Another Store" not in repr(caplog.records[0].__dict__)
        assert caplog.records[0].exc_info is None
        assert snapshot(warehouse_books) == before
    assert ran == []


@pytest.mark.parametrize("provider", ["disabled", "absent"])
def test_unavailable_provider_keeps_the_refusal_and_both_degrades(warehouse_books, monkeypatch, provider):
    if provider == "disabled":
        with warehouse_books.begin() as conn:
            plugins.disable_plugin(conn, "stock")
    else:
        monkeypatch.delitem(plugins.REGISTRY.plugins, "stock")
        for name in list(plugins.REGISTRY.services):
            if name.startswith("stock."):
                monkeypatch.delitem(plugins.REGISTRY.services, name)
    with warehouse_books.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as caught:
            plugins.call_service(ctx, "assistant", WAREHOUSES, {})
        assert caught.value.code == "plugin_not_enabled"
        assert str(caught.value) == "The stock plugin is not enabled in these books."
        assert resolve._company_warehouse_names(ctx) == set()
        assert recorded_warehouses(ctx) == []
        assert resolve.warehouse_words("from Main Store", [], [], ctx) == []
    warehouse_lookup_reply(warehouse_books)


def test_warehouse_lookup_degrades_when_service_refuses(warehouse_books, monkeypatch, caplog):
    ran = []

    def refuse(ctx, inp):
        ran.append(True)
        raise Refusal("The stock plugin is not enabled in these books.", "plugin_not_enabled")

    monkeypatch.setattr(plugins.REGISTRY.services[WAREHOUSES], "fn", refuse)
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        warehouse_lookup_reply(warehouse_books)
    assert ran
    assert "Main Store" not in caplog.text
    assert all("Main Store" not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize("path", ["company_names", "message_names", "warehouse_phrases"])
def test_other_service_refusals_propagate_unchanged(warehouse_books, monkeypatch, caplog, path):
    refusal = Refusal("Which warehouse?", "warehouse_ambiguous", ["Main Store"], field="warehouse")

    def refuse(ctx, inp):
        raise refusal

    monkeypatch.setattr(plugins.REGISTRY.services[WAREHOUSES], "fn", refuse)
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"), warehouse_books.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as caught:
            if path == "company_names":
                resolve._company_warehouse_names(ctx)
            elif path == "message_names":
                recorded_warehouses(ctx)
            else:
                resolve.warehouse_words("from Main Store", [], [], ctx)
        assert caught.value is refusal
    assert "Main Store" not in caplog.text
    assert all("Main Store" not in repr(record.__dict__) for record in caplog.records)


def test_assistant_has_no_plugin_installation_lifecycle(warehouse_books, monkeypatch):
    registry = plugins.Registry()
    monkeypatch.setattr(plugins, "REGISTRY", registry)
    monkeypatch.setattr(plugins, "_loaded", False)
    plugins.load_plugins()
    for _ in range(2):
        registry.fill([registry.plugins[name] for name in registry.order])
        assert plugins.SERVICE_CONSUMERS == {"assistant": (WAREHOUSES + "?",)}
        assert registry.plugins.get("assistant") is None
        assert plugins.declares_service("assistant", WAREHOUSES)
        assert not plugins.declares_service("assistant", "stock.preview_create_warehouse")
    before = snapshot(warehouse_books)
    with pytest.raises(Refusal) as caught:
        plugins.install_plugin(warehouse_books, "assistant")
    assert caught.value.code == "no_plugin"
    assert str(caught.value) == "This engine has no plugin called assistant."
    with warehouse_books.begin() as conn:
        assert "stock" in plugins.enabled_plugins(conn)
        assert "assistant" not in plugins.enabled_plugins(conn)
        assert conn.execute(select(m.plugin_install).where(m.plugin_install.c.name == "assistant")).first() is None
        with pytest.raises(Refusal) as disabled:
            plugins.disable_plugin(conn, "assistant")
        assert disabled.value.code == "plugin_not_enabled"
        with pytest.raises(Refusal) as uninstalled:
            plugins.uninstall_plugin(conn, "assistant")
        assert uninstalled.value.code == "plugin_not_installed"
    assert snapshot(warehouse_books) == before


def test_loader_names_assistant_when_loaded_stock_omits_its_optional_service():
    loaded = [plugins.REGISTRY.plugins[name] for name in plugins.REGISTRY.order]
    isolated = []
    for plugin in loaded:
        if plugin.name == "core":
            # Core also uses this service; remove its overlap only in this fixture.
            uses = [use for use in plugin.manifest.uses if parse_use(use)[0] != WAREHOUSES]
            plugin = replace(plugin, manifest=plugin.manifest.model_copy(update={"uses": uses}))
        elif plugin.name == "stock":
            services = [service for service in plugin.services if service.name != "list_warehouses"]
            declared = [service for service in plugin.manifest.services if service.name != "list_warehouses"]
            plugin = replace(plugin, services=services,
                             manifest=plugin.manifest.model_copy(update={"services": declared}))
        isolated.append(plugin)
    with pytest.raises(plugins.PluginRefused) as caught:
        plugins.Registry().fill(isolated)
    assert str(caught.value) == ("plugin assistant: optionally uses stock.list_warehouses, which stock does not "
                                 "declare. Use a service stock declares.")


def test_assistant_optional_use_allows_an_unloaded_provider(monkeypatch):
    # The real metadata remains loaded, so isolate the table-owner check from this loader view.
    loaded = [plugin for plugin in plugins.REGISTRY.plugins.values() if plugin.name != "stock"]
    monkeypatch.setattr(plugins, "_check_tables", lambda by_name: None)
    registry = plugins.Registry()
    registry.fill(loaded)
    monkeypatch.setattr(plugins, "REGISTRY", registry)
    assert plugins.declares_service("assistant", WAREHOUSES)
