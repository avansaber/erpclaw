"""The plugin interface: manifest, loader (fails closed), table ownership,
per-books installs and slots."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, String, Table, select, text

from erpclaw_core import approvals, models as m, plugins
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import Contract, Input, Result
from erpclaw_core.db import metadata
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, SlotDecl, Source
from erpclaw_core.metrics import Metric
from erpclaw_core.outbox import deliver_pending, enqueue
from erpclaw_core.plugins import (REGISTRY, BooksRefused, Plugin, PluginRefused, Registry, check_installed,
                                  disable_plugin, enabled_plugins, install_plugin, install_record, uninstall_plugin)

from plugin_writes import other_plugin_imports, table_names, violations  # tests/ is on the path (pytest rootdir)

ENGINE_SRC = Path(__file__).resolve().parents[1]


def manifest(name="extra", **over) -> Manifest:
    base = dict(name=name, version="1.0.0", publisher=PUBLISHER, distribution=f"erpclaw-{name}",
                licence="GPL-3.0-only", source=Source(repository="workspace", commit="workspace"),
                engine=">=0.0.1", after_core_revision="0001", depends={}, conflicts=[], owned_tables=[],
                contracts={}, calls=[], events=Events(emits=[], consumes=[]), slots={}, voucher_types=[],
                capabilities=Capabilities(network=[], files=[]), metrics=[])
    base.update(over)
    return Manifest(**base)


class _In(Input):
    pass


def _read(name: str, plugin: str) -> Contract:
    return Contract(name, plugin, "read", "a read", _In, run=lambda ctx, inp: Result("ok"))


EXTRA_BRANCH = Path(__file__).parent / "fixtures" / "extra_branch"  # the planted plugin's branch: extra_thing


def plug(name="extra", contracts=(), metrics=(), slots=None, declare=None, migrations=None, **over) -> Plugin:
    """A plugin whose manifest matches its objects, unless `declare` overrides the manifest."""
    over.setdefault("contracts", {c.name: c.effect for c in contracts})
    over.setdefault("metrics", [mt.name for mt in metrics])
    over.setdefault("slots", {s: SlotDecl(cardinality="one", selection="only") for s in (slots or {})})
    over.update(declare or {})
    return Plugin(manifest=manifest(name, **over), kind="business-module", contracts=list(contracts),
                  metrics=list(metrics), slots=dict(slots or {}), artifact_digest="0" * 64, migrations=migrations)


def fill(*extra: Plugin) -> Registry:
    """The real loaded set plus `extra`, validated as the loader would."""
    r = Registry()
    r.fill([REGISTRY.plugins[n] for n in REGISTRY.order] + list(extra))
    return r


def refused(*extra: Plugin) -> str:
    with pytest.raises(PluginRefused) as e:
        fill(*extra)
    return str(e.value)


@pytest.fixture(autouse=True)
def _loaded():
    plugins.load_plugins()


# --- the manifest ------------------------------------------------------------

def test_manifest_refuses_an_unknown_or_missing_field():
    with pytest.raises(ValidationError):
        manifest(unexpected=1)
    good = manifest().model_dump()
    del good["voucher_types"]
    with pytest.raises(ValidationError):
        Manifest(**good)


@pytest.mark.parametrize("field,value", [("licence", "AGPL-3.0-only"), ("version", "one"), ("engine", "~~1"),
                                         ("name", "Bad-Name"), ("depends", {"sales": "not a spec"})])
def test_manifest_refuses_bad_values(field, value):
    with pytest.raises(ValidationError):
        manifest(**{field: value})


# --- the loader --------------------------------------------------------------

def _ep(name, dist, obj, module="erpclaw_sales"):
    return SimpleNamespace(name=name, dist=SimpleNamespace(name=dist) if dist else None, module=module,
                           load=lambda: obj)


def _load_with(monkeypatch, *eps):
    monkeypatch.setattr(plugins, "REGISTRY", Registry())
    monkeypatch.setattr(plugins, "_loaded", False)
    monkeypatch.setattr(plugins, "entry_points", lambda group: list(eps))
    return plugins.load_plugins()


def test_loader_refuses_a_distribution_not_on_the_first_party_list(monkeypatch):
    with pytest.raises(PluginRefused, match="not a first-party distribution"):
        _load_with(monkeypatch, _ep("evil", "evil-plugin", plug("evil")))


def test_loader_refuses_a_manifest_naming_another_distribution(monkeypatch):
    with pytest.raises(PluginRefused, match="names distribution erpclaw-extra"):
        _load_with(monkeypatch, _ep("extra", "erpclaw-sales", plug("extra")))


def test_loader_refuses_an_object_that_is_not_a_plugin(monkeypatch):
    with pytest.raises(PluginRefused, match="not a Plugin with a manifest"):
        _load_with(monkeypatch, _ep("x", "erpclaw-sales", object()))


def test_loader_refuses_the_reserved_name_and_another_publisher(monkeypatch):
    with pytest.raises(PluginRefused, match="reserved name core"):
        _load_with(monkeypatch, _ep("core", "erpclaw-sales", plug("core", distribution="erpclaw-sales")))
    with pytest.raises(PluginRefused, match="publisher"):
        _load_with(monkeypatch, _ep("xx", "erpclaw-sales", plug("xx", distribution="erpclaw-sales", publisher="Other")))


def test_loader_loads_the_real_plugins_with_their_code_digest():
    sales = REGISTRY.plugins["sales"]
    assert REGISTRY.order[0] == "core" and {"sales", "email_folder"} <= set(REGISTRY.order)
    assert sales.artifact_digest and len(sales.artifact_digest) == 64


@pytest.mark.parametrize("effect", ["mutate", None])
def test_an_unclassified_effect_is_refused(effect):
    c = Contract("op", "extra", effect, "x", _In, run=lambda ctx, inp: Result("ok"))
    p = Plugin(manifest=manifest(contracts={}), kind="business-module", contracts=[c], artifact_digest="0" * 64)
    assert f"has effect {effect!r}" in refused(p)


def test_contracts_must_equal_the_manifest():
    c = _read("extra_list", "extra")
    assert "contracts differ" in refused(plug(contracts=[c], declare={"contracts": {"extra_list": "write"}}))
    assert "contracts differ" in refused(plug(contracts=[c], declare={"contracts": {}}))


def test_write_contracts_need_preview_commit_and_read_back():
    c = Contract("op", "extra", "write", "x", _In, preview=lambda *a: None)
    assert "needs preview, commit" in refused(plug(contracts=[c]))


def test_metrics_and_slots_must_equal_the_manifest():
    mt = Metric("extra_metric", "Extra", "a metric", lambda *a: None)
    assert "metrics differ" in refused(plug(metrics=[mt], declare={"metrics": []}))
    assert "slot callbacks differ" in refused(plug(slots={"deliver_message": lambda r: "x"}, declare={"slots": {}}))


def test_slot_cardinality_must_be_cores_and_a_one_slot_filled_once():
    many = {"deliver_message": SlotDecl(cardinality="many", selection="any")}
    assert "core defines it as 'one'" in refused(plug(slots={"deliver_message": lambda r: "x"}, declare={"slots": many}))
    assert "already filled by email_folder" in refused(plug(slots={"deliver_message": lambda r: "x"}))
    assert "unknown slot" in refused(plug(slots={"teleport": lambda r: "x"},
                                          declare={"slots": {"teleport": SlotDecl(cardinality="one", selection="x")}}))


def test_dependencies_must_be_loaded_in_range_and_without_conflicts():
    assert "depends on missing, which is not loaded" in refused(plug(depends={"missing": ">=1"}))
    assert "needs sales >=9" in refused(plug(depends={"sales": ">=9"}))
    assert "conflicts with sales" in refused(plug(conflicts=["sales"]))


def test_a_dependency_cycle_is_refused():
    a, b, c = plug("a_one", depends={"b_two": ">=1"}), plug("b_two", depends={"c_three": ">=1"}), \
        plug("c_three", depends={"a_one": ">=1"})
    msg = refused(a, b, c)
    assert "dependency cycle" in msg and "a_one" in msg and "b_two" in msg and "c_three" in msg


def test_calls_and_consumed_events_resolve_inside_declared_dependencies():
    assert "calls create_invoice" in refused(plug(calls=["create_invoice"]))
    assert fill(plug(calls=["create_invoice"], depends={"sales": ">=0.0.1"}))
    assert "consumes event invoice_posted" in refused(plug(events=Events(emits=[], consumes=["invoice_posted"])))


def test_voucher_types_and_metrics_have_one_owner():
    assert "voucher type sales_invoice is already declared by sales" in refused(plug(voucher_types=["sales_invoice"]))
    mt = Metric("sales", "Sales", "again", lambda *a: None)
    assert "metric sales is already declared" in refused(plug(metrics=[mt]))


def test_engine_version_and_core_revision_must_match():
    assert "needs engine >=99" in refused(plug(engine=">=99"))
    assert "core revision 9999" in refused(plug(after_core_revision="9999"))


@pytest.fixture
def extra_table():
    t = Table("extra_thing", metadata, Column("id", String(36), primary_key=True))
    yield t
    metadata.remove(t)


def test_tables_need_an_owner_mark_even_when_registered_before_loading(extra_table):
    assert "has no owner mark" in refused(plug())


def test_tables_marked_for_a_plugin_must_equal_its_declaration(extra_table):
    extra_table.info["owner"] = "extra"
    assert "defines ['extra_thing']" in refused(plug())  # undeclared
    assert fill(plug(owned_tables=["extra_thing"], migrations=EXTRA_BRANCH))
    assert "declares tables" in refused(plug(owned_tables=["extra_thing", "ghost"]))  # declared, not defined


def test_a_table_for_an_unloaded_plugin_and_a_claimed_core_table_are_refused(extra_table):
    extra_table.info["owner"] = "nobody"
    assert "plugin nobody, which is not loaded" in refused()
    extra_table.info["owner"] = "extra"
    assert "claims core table party" in refused(plug(owned_tables=["extra_thing", "party"]))
    assert "table sales_invoice is already declared by sales" in refused(plug(owned_tables=["sales_invoice"]))


# --- per set of books --------------------------------------------------------

def test_books_record_versions_and_both_digests(engine):
    with engine.connect() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute(select(m.plugin_install)).mappings()}
    sends = {n for n in REGISTRY.order if "send_message" in REGISTRY.plugins[n].slots}  # the owner installs those
    assert set(rows) == set(REGISTRY.order) - {"core"} - sends  # every other registered plugin; core is not one
    s = REGISTRY.plugins["sales"]
    assert rows["sales"]["manifest_digest"] == s.manifest.digest()
    assert rows["sales"]["artifact_digest"] == s.artifact_digest and rows["sales"]["status"] == "installed"
    check_installed(engine)  # opens


@pytest.mark.parametrize("column,what", [("version", "version"), ("manifest_digest", "manifest"),
                                         ("artifact_digest", "code")])
def test_books_do_not_open_when_a_plugin_changed(engine, column, what):
    with engine.begin() as conn:
        conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "sales").values({column: "0" * 20}))
    with pytest.raises(BooksRefused, match=f"different {what}"):
        check_installed(engine)


def test_a_code_change_with_the_same_version_and_manifest_refuses_the_books(engine, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    from erpclaw_core.manifest import artifact_digest

    before = artifact_digest(pkg)
    (pkg / "a.py").write_text("x = 2\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "a.pyc").write_bytes(b"ignored")
    assert artifact_digest(pkg) != before
    sales = REGISTRY.plugins["sales"]
    original = sales.artifact_digest
    try:
        sales.artifact_digest = "f" * 64
        with pytest.raises(BooksRefused, match="different code"):
            check_installed(engine)
    finally:
        sales.artifact_digest = original


def test_a_disabled_plugin_stops_its_operations_and_keeps_its_history(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    with engine.connect() as conn:
        assert "sales" not in enabled_plugins(conn)
        assert conn.execute(select(text("count(*)")).select_from(text("sales_invoice"))).scalar_one() > 0
    with pytest.raises(Refusal, match="not enabled in these books"):
        approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                          {"customer": "Acme", "items": [{"item": "copper wire", "quantity": 1}]})
    check_installed(engine)  # disabled books still open


def test_install_needs_dependencies_enabled_in_the_same_books(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    needy = plug("needy", depends={"sales": ">=0.0.1"})
    with engine.begin() as conn:
        with pytest.raises(Refusal, match="needs sales installed and enabled"):
            install_record(conn, needy)


def test_disable_and_uninstall_are_refused_while_a_dependent_is_installed(engine, monkeypatch):
    needy = plug("needy", depends={"email_folder": ">=0.0.1"})
    monkeypatch.setitem(REGISTRY.plugins, "needy", needy)
    with engine.begin() as conn:
        install_record(conn, needy)
    with engine.begin() as conn:
        with pytest.raises(Refusal, match="needy depends on it"):
            disable_plugin(conn, "email_folder")
        with pytest.raises(Refusal, match="needy depends on it"):
            uninstall_plugin(conn, "email_folder")


def test_uninstall_is_refused_while_postings_depend_on_the_plugin(engine):
    with engine.begin() as conn:
        with pytest.raises(Refusal, match="posted documents in these books depend on it"):
            uninstall_plugin(conn, "sales")


def test_uninstall_keeps_tables_and_install_restores_it(engine):
    with engine.begin() as conn:
        uninstall_plugin(conn, "email_folder")
    with engine.connect() as conn:
        assert "email_folder" not in enabled_plugins(conn)
    install_plugin(engine, "email_folder")
    with engine.connect() as conn:
        assert "email_folder" in enabled_plugins(conn)


def _queue(engine) -> str:
    from erpclaw_core.common import company_ctx

    with engine.begin() as conn:
        return enqueue(company_ctx(conn), "deliver_message", "a@b.example", "Hi", "Body", None)


def test_a_message_queued_before_its_connector_is_disabled_stays_queued(engine):
    oid = _queue(engine)
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    out = {d["id"]: d for d in deliver_pending(engine)}
    assert out[oid]["status"] == "queued" and "No plugin delivers messages in these books" in out[oid]["detail"]
    with engine.connect() as conn:
        assert conn.execute(select(m.outbox.c.status).where(m.outbox.c.id == oid)).scalar_one() == "queued"


def test_two_books_deliver_through_their_own_connector_installs(engine, other_database):
    from erpclaw_core.db import make_engine
    from erpclaw_sales.demo import seed_demo

    other = make_engine(other_database)
    seed_demo(other)
    with other.begin() as conn:
        uninstall_plugin(conn, "email_folder")
    mine, theirs = _queue(engine), _queue(other)
    assert {d["id"]: d["status"] for d in deliver_pending(engine)}[mine] == "accepted"
    assert {d["id"]: d["status"] for d in deliver_pending(other)}[theirs] == "queued"
    other.dispose()


# --- the static ownership check ----------------------------------------------

def _tables() -> dict:
    return table_names(["erpclaw_core.models", "erpclaw_core.db", "erpclaw_sales"])


def test_first_party_plugins_write_only_their_own_tables():
    for owner, pkg in (("sales", "erpclaw-sales/src/erpclaw_sales"),
                       ("email_folder", "erpclaw-email-folder/src/erpclaw_email_folder")):
        assert violations(ENGINE_SRC / "plugins" / pkg, owner, _tables()) == []


def test_the_ownership_check_finds_planted_writes_and_allows_reads_and_core_services():
    found = violations(Path(__file__).parent / "fixtures" / "planted_writes", "sales", _tables())
    assert any("writes party" in v for v in found)
    assert any("writes ledger_entry" in v for v in found)
    assert any("raw SQL write" in v for v in found)
    assert len(found) == 3  # the read and the core-service call are not flagged


def test_install_shows_the_preview_and_installs_nothing_without_yes(engine, runtime_login, monkeypatch):
    from typer.testing import CliRunner

    from conftest import cli_urls
    from erpclaw_cli.main import app

    cli_urls(monkeypatch, engine, runtime_login)
    with engine.begin() as conn:
        uninstall_plugin(conn, "email_folder")
    out = CliRunner().invoke(app, ["plugins", "install", "email_folder"])
    assert out.exit_code == 0 and "slots: deliver_message" in out.output and "Nothing was installed" in out.output
    with engine.connect() as conn:
        assert "email_folder" not in enabled_plugins(conn)
    out = CliRunner().invoke(app, ["plugins", "install", "email_folder", "--yes"])
    assert out.exit_code == 0 and "email_folder installed" in out.output
    with engine.connect() as conn:
        assert "email_folder" in enabled_plugins(conn)


def test_no_plugin_imports_another_plugins_package():
    """A plugin reaches another plugin only through core (call_service), never by importing its package, its
    declared dependencies included."""
    packages = sorted(d for d in (ENGINE_SRC / "plugins").glob("*/src/*") if (d / "__init__.py").is_file())
    assert {d.name for d in packages} >= {"erpclaw_sales", "erpclaw_email_folder"}
    for pkg in packages:
        assert other_plugin_imports(pkg, pkg.name) == [], pkg.name


def test_the_import_check_finds_planted_imports_of_another_plugin():
    found = other_plugin_imports(Path(__file__).parent / "fixtures" / "planted_imports", "erpclaw_planted")
    assert found == ["bad.py:2 imports erpclaw_sales", "bad.py:3 imports erpclaw_sales.service",
                     "bad.py:5 imports erpclaw_email_folder"]


def test_the_cli_exits_with_the_loaders_message(monkeypatch, capsys):
    import erpclaw_cli.main as cli

    def boom():
        raise PluginRefused("plugin extra: is in a dependency cycle: a -> b -> a. Break the cycle.")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as e:
        cli.run()
    assert e.value.code == 1 and "dependency cycle" in capsys.readouterr().out


# --- round 1 of the build review --------------------------------------------

def test_core_tables_are_a_fixed_set_not_a_mark_anyone_can_set(extra_table):
    """A table marked core that is not one of core's tables is refused (an
    unmarked table registered before core's models used to become core)."""
    extra_table.info["owner"] = "core"
    assert "is not one of core's tables" in refused()


def test_a_plugin_cannot_claim_a_core_table_even_if_its_mark_changes(monkeypatch):
    monkeypatch.setitem(m.posting.info, "owner", "extra")  # a changed mark is itself refused
    assert "core table posting is marked for plugin extra" in refused(plug(owned_tables=["posting"]))


@pytest.mark.parametrize("command", [["plugins", "uninstall", "email_folder"], ["plugins", "disable", "email_folder"],
                                     ["plugins", "install", "email_folder", "--yes"]])
def test_lifecycle_commands_refuse_books_whose_installs_do_not_match(engine, runtime_login, monkeypatch, command):
    from typer.testing import CliRunner

    from conftest import cli_urls
    from erpclaw_cli.main import app

    cli_urls(monkeypatch, engine, runtime_login)
    with engine.begin() as conn:
        conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email_folder")
                     .values(artifact_digest="0" * 64))
    out = CliRunner().invoke(app, command)
    assert out.exit_code == 1 and "different code" in out.output
    with engine.connect() as conn:  # nothing changed: the stale record is still there, still installed
        row = conn.execute(select(m.plugin_install).where(m.plugin_install.c.name == "email_folder")).mappings().one()
    assert row["artifact_digest"] == "0" * 64 and row["status"] == "installed"


def test_lifecycle_functions_check_the_books_first(engine):
    with engine.begin() as conn:
        conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == "email_folder")
                     .values(artifact_digest="0" * 64))
    for fn in (disable_plugin, uninstall_plugin):
        with engine.begin() as conn:
            with pytest.raises(BooksRefused, match="different code"):
                fn(conn, "email_folder")
    with pytest.raises(BooksRefused, match="different code"):
        install_plugin(engine, "email_folder")
