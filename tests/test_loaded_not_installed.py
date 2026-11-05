"""A plugin loaded in this process but not installed in a set of books has no tables, no version row and no
operations there: every reader of a set of books' tables reads that set's applied branches, and a call to one of its
operations is the plugin_not_enabled refusal before anything is resolved, previewed or read."""
from __future__ import annotations

import ast
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, String, Table, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import bookdiff
import slot_support
from conftest import PG_URL
from erpclaw_api.app import create_app
from erpclaw_assistant import graph
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli.evals import run as eval_run
from erpclaw_core import backup as bk
from erpclaw_core.db import READ, make_engine, metadata
from erpclaw_core.grants import apply_grants
from erpclaw_core.plugins import REGISTRY, disable_plugin, install_into
from test_catalogue import books as catalogue_books
from test_plugins import EXTRA_BRANCH, plug

ENGINE = Path(__file__).resolve().parents[1]
INVOICE = call("create_invoice", customer="Wayne Enterprises", items=[{"item": "circuit board", "quantity": 2}])
SAID = "invoice Wayne Enterprises for 2 circuit boards"


@pytest.fixture
def extra(monkeypatch):
    """A planted plugin with its own branch and one table, loaded here; new books do not install it (it is not in the
    registry's install order)."""
    t = Table("extra_thing", metadata, Column("id", String(36), primary_key=True),
              info={"owner": "extra", "runtime": READ})  # a policy, so grants can be applied where it is installed
    monkeypatch.setitem(REGISTRY.plugins, "extra", plug("extra", owned_tables=["extra_thing"],
                                                        migrations=EXTRA_BRANCH, after_core_revision="0004"))
    yield REGISTRY.plugins["extra"]
    metadata.remove(t)


def _names(tables) -> set[str]:
    return {t.name for t in tables}


def events(engine, replies, said):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]


# --- the books' tables -------------------------------------------------------------------------------------------

def test_books_tables_lists_a_plugins_tables_only_in_the_books_that_installed_it(engine, extra):
    with engine.connect() as conn:
        names = _names(bk.books_tables(conn))
    assert "extra_thing" not in names and {"company", "posting"} <= names
    assert "extra_thing" in _names(bk.engine_tables())
    with engine.begin() as conn:
        install_into(conn, extra)
    with engine.connect() as conn:
        assert "extra_thing" in _names(bk.books_tables(conn))


def test_the_table_list_has_no_engine_wide_default():
    with pytest.raises(TypeError):
        bk._tables()  # noqa: the engine-wide form is engine_tables(), by name


def test_the_suite_and_eval_snapshots_read_only_the_books_tables(engine, extra):
    assert "extra_thing" not in bookdiff.snapshot(engine)
    assert "extra_thing" not in eval_run.snapshot(engine)
    assert catalogue_books(engine)


def test_backup_restore_and_book_diff_of_books_that_never_installed_a_loaded_plugin(engine, extra, tmp_path):
    manifest = bk.backup(engine, tmp_path / "b")
    assert "extra_thing" not in manifest["tables"]
    name = f"restored_{secrets.token_hex(4)}_test"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        target = bk.NewPostgresDatabase(PG_URL, name, None)
        bk.restore(tmp_path / "b", target)
        restored = make_engine(make_url(PG_URL).set(database=name).render_as_string(hide_password=False))
        try:
            assert bk.book_diff(engine, restored) == []
        finally:
            restored.dispose()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def test_two_sets_of_books_in_one_process_each_read_their_own_tables(engine, extra, other_database, tmp_path,
                                                                     monkeypatch):
    """Books A install the planted plugin, books B (the demo books) do not, in one process with it loaded."""
    from erpclaw_sales.demo import seed_demo

    a = make_engine(other_database)
    try:
        seed_demo(a)
        with a.begin() as conn:
            install_into(conn, extra)
            conn.exec_driver_sql("INSERT INTO extra_thing VALUES ('kept')")
        assert "extra_thing" in bookdiff.snapshot(a) and "extra_thing" not in bookdiff.snapshot(engine)
        assert bk.backup(a, tmp_path / "a")["tables"]["extra_thing"]["rows"] == 1
        assert "extra_thing" not in bk.backup(engine, tmp_path / "b")["tables"]
        role = f"erpclaw_g_{secrets.token_hex(4)}"
        with engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        try:
            for books in (a, engine):
                with books.begin() as conn:
                    apply_grants(conn, role)
        finally:
            for books in (a, engine):
                with books.begin() as conn:
                    conn.exec_driver_sql(f"DROP OWNED BY {role}")
                    conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO PUBLIC")
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP ROLE {role}")
        # an operation of the planted plugin: runs where it is installed, refused where it is not
        monkeypatch.setitem(REGISTRY.contract_plugin, "list_invoices", "extra")
        [refused] = events(engine, [Reply("", [call("list_invoices")])], "list my invoices")
        assert refused["type"] == "refused" and refused["code"] == "plugin_not_enabled", refused
        ran = events(a, [Reply("", [call("list_invoices")]), Reply("Here they are.", [])], "list my invoices")
        assert ran and ran[0]["type"] == "answer", ran
    finally:
        a.dispose()


# --- an operation of a plugin these books have not enabled -------------------------------------------------------

def _never(*a, **k):
    raise AssertionError("resolved or previewed before the enabled check")


def test_an_unoffered_write_is_refused_before_any_resolution_or_preview(engine, monkeypatch):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    monkeypatch.setattr(graph, "resolve_args", _never)
    monkeypatch.setattr(graph, "write_scopes", _never)
    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "preview", _never)
    [e] = events(engine, [Reply("", [INVOICE])], SAID)
    assert e["type"] == "refused" and e["code"] == "plugin_not_enabled", e
    assert e["capability"] == "available.op.create_invoice"


def test_an_unoffered_write_whose_preview_reads_a_table_these_books_lack_never_reaches_it(engine, extra,
                                                                                          monkeypatch):
    def preview(ctx, inp):
        ctx.conn.execute(text("SELECT count(*) FROM extra_thing"))
        raise AssertionError("previewed")

    monkeypatch.setitem(REGISTRY.contract_plugin, "create_invoice", "extra")
    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "preview", preview)
    touched = []

    def seen(conn, cursor, statement, *a):
        if "extra_thing" in statement:
            touched.append(statement)

    event.listen(engine, "before_cursor_execute", seen)
    try:
        [e] = events(engine, [Reply("", [INVOICE])], SAID)
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert e["type"] == "refused" and e["code"] == "plugin_not_enabled", e
    assert touched == []


def test_an_unoffered_read_is_refused_before_its_arguments_are_checked(engine):
    """A read of a plugin these books lack, called with arguments its contract refuses: the refusal, not a request to
    the model to correct the call."""
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    [e] = events(engine, [Reply("", [call("list_invoices", no_such_field=1)])], "list my invoices")
    assert e["type"] == "refused" and e["code"] == "plugin_not_enabled", e
    assert e["capability"] == "available.op.list_invoices"


# --- no books reader reaches the engine-wide form ----------------------------------------------------------------

READERS = [ENGINE / "packages/erpclaw-core/src/erpclaw_core/backup.py",
           ENGINE / "packages/erpclaw-cli/src/erpclaw_cli/evals/run.py",
           ENGINE / "tests/bookdiff.py", ENGINE / "tests/test_catalogue.py", ENGINE / "tests/test_backup.py"]
ENGINE_WIDE = ("engine_tables", "_tables")  # the two definitions in backup.py: the forms themselves


def engine_wide_reads(path: Path) -> list[str]:
    """Each place a books reader lists every table the engine knows: a call of engine_tables(), or metadata's whole
    table collection (sorted_tables, or tables other than looked up by one name)."""
    tree = ast.parse(path.read_text())
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    skip = {id(d) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ENGINE_WIDE
            for d in ast.walk(n)} if path.name == "backup.py" else set()
    out = []
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        where = f"{path.name}:{getattr(n, 'lineno', '?')}"
        if isinstance(n, ast.Call) and (getattr(n.func, "attr", None) == "engine_tables"
                                        or getattr(n.func, "id", None) == "engine_tables"):
            out.append(f"{where} engine_tables()")
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "metadata":
            p = parent.get(n)
            by_name = (n.attr == "tables" and (isinstance(p, ast.Subscript) and p.value is n
                                               or isinstance(p, ast.Attribute) and p.attr == "get"))
            if n.attr in ("tables", "sorted_tables") and not by_name:
                out.append(f"{where} metadata.{n.attr}")
    return out


def test_no_books_reader_lists_every_table_the_engine_knows():
    assert [r for f in READERS for r in engine_wide_reads(f)] == []


@pytest.mark.parametrize("body", ["def snap(conn):\n    return bk.engine_tables()\n",
                                  "def snap(conn):\n    return list(metadata.sorted_tables)\n",
                                  "def snap(conn):\n    return [t for t in metadata.tables.values()]\n"])
def test_the_reader_scan_finds_a_planted_engine_wide_read(tmp_path, body):
    planted = tmp_path / "planted_reader.py"
    planted.write_text(body)
    assert engine_wide_reads(planted)


def test_the_reader_scan_lets_a_lookup_by_name_through(tmp_path):
    planted = tmp_path / "planted_lookup.py"
    planted.write_text("def one(name):\n    return metadata.tables[name], metadata.tables.get(name)\n")
    assert engine_wide_reads(planted) == []


# --- a test's own slot filler --------------------------------------------------------------------------------------

def test_a_replacement_filler_must_take_the_registered_fillers_parameters(monkeypatch):
    with pytest.raises(TypeError, match="mark_data"):
        slot_support.replace_filler(monkeypatch, "send_message", lambda settings, msg: None)
    slot_support.replace_filler(monkeypatch, "send_message", lambda settings, msg, mark_data: None)


def test_the_real_send_message_filler_runs_on_its_real_path():
    """No replacement: the registered email plugin's own callable is what the slot resolves to."""
    import erpclaw_email

    assert REGISTRY.slot_filler("send_message", {"email"}) == ("email", erpclaw_email.send_message)


def test_loaded_without_mailers_keeps_every_plugin_and_its_tables():
    kept = slot_support.loaded_without_mailers()
    assert [p.name for p in kept] == list(REGISTRY.order)
    assert all(not {"send_message", "message_sender"} & set(p.slots) for p in kept)
    assert "deliver_message" in kept[[p.name for p in kept].index("email_folder")].slots  # the folder connector stays
    assert [p.manifest.owned_tables for p in kept] == [REGISTRY.plugins[n].manifest.owned_tables
                                                       for n in REGISTRY.order]
