"""Plugin migration branches."""
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Column, String, Table, inspect, text

from conftest import drop_everything
from erpclaw_core import plugins
from erpclaw_core.common import Refusal
from erpclaw_core.db import create_all, metadata
from erpclaw_core.migrate import CORE_TABLES, CORE_VERSIONS, config, core_head, upgrade
from erpclaw_core.models import CORE_TABLES as CORE_MARKED
from erpclaw_core.plugins import (REGISTRY, BooksRefused, check_installed, install_into, install_plugin,
                                  uninstall_plugin)
from erpclaw_core.setup import create_company
from test_plugins import EXTRA_BRANCH, fill, plug, refused


def sales_head() -> str:
    """The sales branch's head, read from the plugin's own revisions (never a literal, so a new revision needs no
    edit here)."""
    from erpclaw_core.migrate import branch_scripts
    from erpclaw_core.plugins import REGISTRY, load_plugins
    load_plugins()
    return next(sc.revision for sc in branch_scripts(REGISTRY.plugins["sales"].migrations) if sc.is_head)


def _recorded(engine) -> set[str]:
    """The plugins these books have a record of, installed or uninstalled: the ones whose branch they applied."""
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT name FROM plugin_install")).scalars())


def plugin_heads(engine) -> list[str]:
    """The branch head of every plugin these books applied, read from its own revisions (never a literal, so a new
    plugin or a new revision needs no edit here); a plugin loaded here but never installed in them has none. A plugin that owns tables has exactly one head, or this raises: deriving the list
    never hides a missing branch. A plugin with no tables has no branch."""
    from erpclaw_core.migrate import branch_scripts
    from erpclaw_core.plugins import REGISTRY, load_plugins
    load_plugins()
    heads, recorded = [], _recorded(engine)
    for name in REGISTRY.order:
        p = REGISTRY.plugins[name]
        if name == "core" or name not in recorded or (not p.manifest.owned_tables and not p.migrations):
            continue
        found = [sc.revision for sc in branch_scripts(p.migrations) if sc.is_head] if p.migrations else []
        if len(found) != 1:
            raise AssertionError(f"plugin {name} owns tables but has {len(found)} branch heads")
        heads.append(found[0])
    return heads

def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _versions(engine) -> list[str]:
    with engine.connect() as conn:
        return sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def test_empty_books_get_core_and_sales_by_migration_and_create_all_makes_no_plugin_table(engine):
    """The invoice path end to end on books initialized from empty is every path test: the `engine`
    fixture starts from an empty database and initializes it through the engine's own setup."""
    drop_everything(engine)
    create_all(engine)
    assert _tables(engine) == set(CORE_TABLES) | {"alembic_version"}  # core's only: no plugin table
    assert _versions(engine) == [core_head()]
    create_company(engine, "Fresh Co")  # installs the sales plugin: its branch, in the install transaction
    recorded = _recorded(engine)  # core's tables and those of the plugins new books install, and no other
    assert _tables(engine) == set(CORE_TABLES) | {t for n in recorded for t in REGISTRY.plugins[n].manifest.owned_tables} \
        | {"alembic_version"}
    assert _versions(engine) == sorted([core_head(), *plugin_heads(engine)])  # one version row per head


def test_every_table_on_the_metadata_is_created_by_exactly_one_revision(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn)  # core's branch alone
    core = _tables(engine) - {"alembic_version"}
    assert core == set(CORE_TABLES) == set(CORE_MARKED)
    made = set(core)
    for name in REGISTRY.order:  # each plugin's branch, alone, makes exactly the tables it owns
        p = REGISTRY.plugins[name]
        if not p.migrations:
            continue
        with engine.begin() as conn:
            command.upgrade(config(conn), f"{name}@head")
        branch = _tables(engine) - {"alembic_version"} - made
        assert branch == set(p.manifest.owned_tables), name
        made |= branch
    assert made == set(metadata.tables)


def test_a_later_core_revision_moves_the_core_row_and_leaves_the_sales_row(engine, tmp_path):
    (tmp_path / "core_9999.py").write_text(
        f'"""a test core revision after the head"""\nrevision = "9999"\ndown_revision = "{core_head()}"\n'
        "branch_labels = None\ndepends_on = None\n\n\ndef upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n")
    branches = [REGISTRY.plugins[n].migrations for n in REGISTRY.order if REGISTRY.plugins[n].migrations]
    with engine.begin() as conn:
        command.upgrade(config(conn, locations=[CORE_VERSIONS, *branches, tmp_path]), "core@head")
    assert _versions(engine) == sorted(["9999", *plugin_heads(engine)])


@pytest.fixture
def extra(monkeypatch):
    """A planted plugin with its own branch and one table (tests/fixtures/extra_branch)."""
    t = Table("extra_thing", metadata, Column("id", String(36), primary_key=True), info={"owner": "extra"})
    monkeypatch.setitem(REGISTRY.plugins, "extra", plug("extra", owned_tables=["extra_thing"],
                                                        migrations=EXTRA_BRANCH, after_core_revision="0004"))
    yield REGISTRY.plugins["extra"]
    metadata.remove(t)


def _installed(engine, name):
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM plugin_install WHERE name = :n"), {"n": name}).scalar_one()


@pytest.mark.parametrize("where", ["after its DDL", "after the grants"])
def test_an_install_that_fails_leaves_no_table_version_row_or_record(engine, extra, monkeypatch, where):
    def boom(*a, **k):
        raise RuntimeError("forced")

    if where == "after its DDL":
        monkeypatch.setattr(plugins, "_record", boom)
        role = None
    else:  # the grants ran (as far as these books have any), then the step after them fails
        import erpclaw_core.grants as grants

        def grants_then_fail(conn, role):
            conn.exec_driver_sql("SELECT 1 FROM extra_thing")  # the table existed inside the transaction
            raise RuntimeError("forced")

        monkeypatch.setattr(grants, "apply_grants", grants_then_fail)
        role = "runtime"
    with pytest.raises(RuntimeError, match="forced"):
        with engine.begin() as conn:
            install_into(conn, extra, role)
    assert "extra_thing" not in _tables(engine)
    assert "extra_0001" not in _versions(engine)
    assert _installed(engine, "extra") == 0


def test_uninstall_keeps_the_sales_tables_and_version_row_and_a_reinstall_changes_nothing(engine):
    drop_everything(engine)
    create_company(engine, "Fresh Co")  # no postings yet, so sales may be uninstalled
    with engine.connect() as conn:
        rows = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                for t in REGISTRY.plugins["sales"].manifest.owned_tables}
    with engine.begin() as conn:
        uninstall_plugin(conn, "sales")
    assert set(REGISTRY.plugins["sales"].manifest.owned_tables) <= _tables(engine)
    assert sales_head() in _versions(engine) and _installed(engine, "sales") == 0
    install_plugin(engine, "sales")
    assert _versions(engine) == sorted([core_head(), *plugin_heads(engine)]) and _installed(engine, "sales") == 1
    with engine.connect() as conn:
        assert rows == {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in rows}


def test_an_install_into_books_before_the_plugins_core_revision_is_refused(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0003")
    with pytest.raises(Refusal, match="needs these books at core revision 0013 or later; they are at 0003"):
        with engine.begin() as conn:
            install_into(conn, REGISTRY.plugins["sales"])
    assert not {"sales_invoice"} & _tables(engine)


def _branch(tmp_path, name, body):
    d = tmp_path / name
    d.mkdir()
    (d / f"{name}_rev.py").write_text(body)
    return d


def _rev(rev="x_0001", down="None", labels="('extra',)", creates="('extra_thing',)", depends="None"):
    return (f'"""t"""\nrevision = "{rev}"\ndown_revision = {down}\nbranch_labels = {labels}\ndepends_on = {depends}\n'
            f"creates = {creates}\n\n\ndef upgrade():\n    pass\n")


@pytest.mark.filterwarnings("ignore:Revision 0004 referenced from")  # the refused branch names a parent in core
def test_the_loader_refuses_a_branch_that_does_not_create_exactly_the_plugins_tables(tmp_path):
    t = Table("extra_thing", metadata, Column("id", String(36), primary_key=True), info={"owner": "extra"})
    try:
        assert fill(plug(owned_tables=["extra_thing"], migrations=EXTRA_BRANCH))
        assert "owns tables but ships no migrations" in refused(plug(owned_tables=["extra_thing"]))
        none = _branch(tmp_path, "none", _rev(creates="()"))
        assert "owns table extra_thing, which none of its revisions creates" in refused(
            plug(owned_tables=["extra_thing"], migrations=none))
        more = _branch(tmp_path, "more", _rev(creates="('extra_thing', 'party')"))
        assert "creates table party, which it does not own" in refused(
            plug(owned_tables=["extra_thing"], migrations=more))
        unlabelled = _branch(tmp_path, "unlabelled", _rev(labels="None"))
        assert "exactly one base, labelled" in refused(plug(owned_tables=["extra_thing"], migrations=unlabelled))
        for i, rev in enumerate((_rev(depends="'0004'"), _rev(down="'0004'"))):  # a dependency, or a parent, in core
            reaching = _branch(tmp_path, f"reaching{i}", rev)
            assert "reach" in refused(plug(owned_tables=["extra_thing"], migrations=reaching))
        undeclared = _branch(tmp_path, "undeclared", _rev().replace("creates = ('extra_thing',)\n", ""))
        assert "does not declare the tables it creates" in refused(
            plug(owned_tables=["extra_thing"], migrations=undeclared))
    finally:
        metadata.remove(t)


def test_missing_sales_history_refusal_does_not_invent_cause_or_unknown_version(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version WHERE version_num = '"+sales_head()+"'")
    with pytest.raises(BooksRefused) as caught:
        check_installed(engine)
    assert str(caught.value) == ("These books have tables for plugin sales, but their migration history for that "
                                 "plugin is missing. An update for existing books is not available yet. "
                                 "Nothing was changed.")
