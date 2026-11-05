"""The PostgreSQL runtime role: the policy is derived
from the code, applied and verified as the owner, and the runtime opens books
only as a genuine runtime login holding exactly that policy."""
from __future__ import annotations

import ast
import importlib
import re
import secrets
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Table, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from erpclaw_core import approvals, grants, integration as ik, ledger, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.db import APPEND, READ, RoleRefused, Runtime, make_engine, metadata
from erpclaw_core.plugins import REGISTRY, disable_plugin, load_plugins
from erpclaw_core.setup import create_company

from conftest import PG_URL, cli_urls, drop_everything

ENGINE = Path(__file__).resolve().parents[1]
# core and every plugin folder on disk, registered or not: a grants scanner errs towards reading more
SOURCES = [ENGINE / "packages/erpclaw-core/src", *sorted(d / "src" for d in (ENGINE / "plugins").iterdir()
                                                       if (d / "src").is_dir())]
CHAINED = ("where", "returning", "execution_options", "inline")
RAW_WRITE = re.compile(r"\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b|\bFOR\s+UPDATE\b|\bTRUNCATE\s+(TABLE\s+)?\w")  # statements, not privilege names

# --- the policy, derived from the code ---------------------------------------

def _runtime_files():
    for root in SOURCES:
        for f in sorted(root.rglob("*.py")):
            if "migrations" in f.parts or "__pycache__" in f.parts:  # migrations run as the owner
                continue
            if re.search(r"^OWNER_ONLY = True", f.read_text(), re.M):  # owner commands: under the runtime role
                continue                                                # they fail with permission denied
            yield root, f


def _walk(scope):
    """The nodes of one scope, not descending into nested functions (their own scope)."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))


class Scope:
    """Names in one function (or the module): assignments, and imports made inside it."""

    def __init__(self, scope, glb, package, parent=None):
        self.glb, self.assigned, self.objects, self.parent = glb, {}, {}, parent
        for n in _walk(scope):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    pairs = (zip(t.elts, n.value.elts) if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple)
                             else [(t, n.value)])
                    for target, value in pairs:
                        if isinstance(target, ast.Name):
                            self.assigned[target.id] = value
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    parts = package.split(".")
                    target = ".".join(parts[:len(parts) - n.level + 1]) + (f".{n.module}" if n.module else "")
                else:
                    target = n.module
                pkg = importlib.import_module(target)
                for a in n.names:
                    obj = getattr(pkg, a.name, None)
                    if obj is None:
                        obj = importlib.import_module(f"{pkg.__name__}.{a.name}")
                    self.objects[a.asname or a.name] = obj

    def name(self, ident):
        if ident in self.objects:
            return self.objects[ident]
        if ident in self.assigned:
            return self.resolve(self.assigned[ident])
        return self.parent.name(ident) if self.parent else self.glb.get(ident)

    def resolve(self, node):
        if isinstance(node, ast.Name):
            return self.name(node.id)
        if isinstance(node, ast.Attribute):
            return getattr(self.resolve(node.value), node.attr, None)
        return None

    def table(self, node):
        obj = self.resolve(node)
        return obj if isinstance(obj, Table) else getattr(obj, "table", None)

    def expr(self, node):
        """A Name's assigned expression (a query built before it is locked), else the node; a name whose assignment
        leads back to itself (`q = q.where(...)`) is left as the name, so it cannot be traced and is never followed
        round in a loop."""
        seen = set()
        while isinstance(node, ast.Name) and node.id in self.assigned and node.id not in seen:
            seen.add(node.id)
            node = self.assigned[node.id]
        return node


def _docstrings(tree):
    return {id(b[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            for b in [n.body] if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant)}


def engine_writes() -> tuple[set, list[str]]:
    """Every (update, table, column), (delete, table) and (lock, table) the runtime
    code issues, and anything it could not classify (which fails the test)."""
    load_plugins()
    found, unknown = set(), []
    for root, f in _runtime_files():
        modname = ".".join(f.relative_to(root).with_suffix("").parts)
        module = importlib.import_module(modname)
        package = module.__package__ or modname.rpartition(".")[0]
        tree = ast.parse(f.read_text())
        docs = _docstrings(tree)
        top = Scope(tree, vars(module), package)
        scopes = [(tree, top)] + [(n, Scope(n, vars(module), package, top)) for n in ast.walk(tree)
                                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope, names in scopes:
            for n in _walk(scope):
                where = f"{f.relative_to(root)}:{getattr(n, 'lineno', '?')}"
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs \
                        and RAW_WRITE.search(n.value):
                    unknown.append(f"{where} raw SQL write: {n.value[:60]!r}")
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func
                if isinstance(fn, ast.Attribute) and fn.attr == "values":
                    base = names.expr(fn.value)  # a statement built into a name first is read through it
                    visited = set()  # each node once: a name assigned through itself cannot loop
                    while isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute) \
                            and base.func.attr in CHAINED and id(base) not in visited:
                        visited.add(id(base))
                        base = names.expr(base.func.value)
                    if id(base) in visited:
                        base = None  # it led back to itself: not traceable
                    if isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute) and base.func.attr == "update":
                        tbl = names.table(base.func.value)
                    elif isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "update":
                        tbl = names.table(base.args[0])
                    elif isinstance(base, ast.Call) and (getattr(base.func, "attr", None) == "insert"
                                                         or getattr(base.func, "id", None) in ("insert", "pg_insert")):
                        continue  # an insert's values
                    elif not n.args and not n.keywords:
                        continue  # a mapping's values(), not a statement
                    else:  # a statement the scanner cannot trace is not taken for an insert
                        unknown.append(f"{where} values() whose statement cannot be read")
                        continue
                    if tbl is None or any(k.arg is None for k in n.keywords) or n.args:
                        unknown.append(f"{where} update whose table or columns cannot be read")
                        continue
                    found |= {("update", tbl.name, k.arg) for k in n.keywords}
                elif isinstance(fn, ast.Attribute) and fn.attr == "delete" and not n.args:
                    tbl = names.table(fn.value)
                    if tbl is None:
                        unknown.append(f"{where} delete whose table cannot be read")
                    else:
                        found.add(("delete", tbl.name))
                elif isinstance(fn, ast.Name) and fn.id in ("update", "delete") and n.args:
                    if fn.id == "delete":
                        tbl = names.table(n.args[0])
                        found.add(("delete", tbl.name)) if tbl is not None else unknown.append(f"{where} delete()")
                elif (isinstance(fn, ast.Name) and fn.id in ("_for_update", "_for_share")) or \
                        (isinstance(fn, ast.Attribute) and fn.attr == "for_update"):  # ledger.for_update(ctx, q)
                    sel = [c for c in ast.walk(names.expr(n.args[1])) if isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Name) and c.func.id == "select"]
                    tbl = names.table(sel[0].args[0]) if sel and sel[0].args else None
                    if tbl is None and f.read_text().splitlines()[n.lineno - 1].rstrip().endswith("# read-set lock"):
                        continue  # the card commit's document locks: test_the_commit_locks_every_read_set_table
                    if tbl is None:
                        unknown.append(f"{where} lock whose table cannot be read")
                    else:
                        found.add(("lock", tbl.name))
                elif isinstance(fn, ast.Attribute) and fn.attr == "with_for_update":
                    if not (isinstance(scope, ast.FunctionDef) and scope.name in ("_for_update", "_for_share")):
                        unknown.append(f"{where} a lock outside ledger._for_update")
    return found, unknown


def policy(name: str) -> Runtime:
    return metadata.tables[name].info["runtime"]


def test_every_table_declares_a_runtime_policy():
    load_plugins()
    assert all(isinstance(t.info.get("runtime"), Runtime) for t in metadata.tables.values()), \
        [t.name for t in metadata.tables.values() if not isinstance(t.info.get("runtime"), Runtime)]


def test_the_policy_grants_exactly_what_the_code_updates_deletes_and_locks():
    """My first two policies each missed a column, so the list is checked by code."""
    found, unknown = engine_writes()
    assert unknown == []
    # Anti-vacuity: the scanner sees the writes found by review.
    assert {("update", "sales_invoice", "cancel_operation_key"), ("update", "posting_chain_head", "last_hash"),
            ("lock", "fiscal_period"), ("lock", "fiscal_year"), ("delete", "system_of_record")} <= found
    for kind, name, *col in sorted(found):
        p = policy(name)
        if kind == "update":
            assert col[0] in p.update, f"the code updates {name}.{col[0]} but the policy does not grant it"
        elif kind == "delete":
            assert p.delete, f"the code deletes from {name} but the policy does not grant DELETE"
        else:
            assert p.update or p.lock, f"the code locks {name} but the policy grants no UPDATE on it"
    for name, t in metadata.tables.items():  # and nothing more than the code needs
        p = t.info["runtime"]
        written = {x[2] for x in found if x[0] == "update" and x[1] == name}
        assert written == set(p.update), name
        assert not written & set(p.lock), f"{name}: a lock column is only for locking; the code writes it"
        assert not p.lock or ("lock", name) in found, f"{name}: a lock column with no lock in the code"
        assert p.delete == (("delete", name) in found), name


def test_the_scanner_reads_every_plugin_folder_and_every_registered_plugin():
    """SOURCES is derived from the folders on disk, so derivation must not hide a gap: every plugins/* folder is
    scanned, every registered plugin's code lies in one of them, and the stock head's update is seen."""
    load_plugins()
    folders = {d.name for d in (ENGINE / "plugins").iterdir() if d.is_dir() and not d.name.startswith(".")}
    assert {f.parent.name for f in SOURCES[1:]} == folders
    for name in REGISTRY.order:
        if name == "core":
            continue
        dist = REGISTRY.plugins[name].manifest.distribution
        assert dist in folders, f"plugin {name}'s distribution {dist} is not a folder the scanner reads"
    found, _ = engine_writes()
    assert {("update", "stock_head", c) for c in ("last_seq", "qty", "value", "last_date")} <= found


def test_the_scanner_finds_a_planted_update_it_was_not_told_about(tmp_path, monkeypatch):
    mod = tmp_path / "planted_t14.py"
    mod.write_text("from erpclaw_core import models as m\n\n"
                   "def f(conn):\n    pc = m.posting\n    conn.execute(pc.update().where(pc.c.id == 'x').values(memo='y'))\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "SOURCES", [tmp_path])
    found, unknown = engine_writes()
    assert found == {("update", "posting", "memo")} and unknown == []



def test_the_scanner_follows_an_update_held_in_a_name_and_fails_closed_when_it_cannot(tmp_path, monkeypatch):
    """`.values()` on a name: an update built into a variable first is read through it; one the scanner cannot
    trace (a parameter, a call's result) is unknown, not taken for an insert. A dict's `.values()` takes no
    arguments and is not a write."""
    mod = tmp_path / "planted_names.py"
    mod.write_text("from erpclaw_core import models as m\n\n"
                   "def f(conn, d):\n    pc = m.posting\n    q = pc.update().where(pc.c.id == 'x')\n"
                   "    conn.execute(q.values(memo='y'))\n    return list(d.values())\n\n"
                   "def g(conn, stmt):\n    conn.execute(stmt.values(memo='z'))\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "SOURCES", [tmp_path])
    found, unknown = engine_writes()
    assert found == {("update", "posting", "memo")}
    assert len(unknown) == 1 and "planted_names.py:10" in unknown[0], unknown


def test_a_statement_reassigned_through_itself_is_unknown_and_the_scanner_ends(tmp_path, monkeypatch):
    """`q = q.where(...)` keeps only the last assignment, which names itself: the scanner cannot trace it, says so, and
    never follows the name round in a loop."""
    mod = tmp_path / "planted_cycle.py"
    mod.write_text("from erpclaw_core import models as m\n\n"
                   "def f(conn, q):\n    q = q.where(m.posting.c.id == 'x')\n    conn.execute(q.values(memo='y'))\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "SOURCES", [tmp_path])
    found, unknown = engine_writes()
    assert found == set() and len(unknown) == 1 and "planted_cycle.py:5" in unknown[0], unknown

# --- the two URLs ---------------------------------------------------------------

def test_both_urls_name_postgresql_and_the_runtime_url_names_its_role(monkeypatch):
    from erpclaw_core.db import NotPostgreSQL, migration_url, runtime_role

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "sqlite:///b.sqlite")
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "postgresql+psycopg://o@127.0.0.1/x_test")
    with pytest.raises(NotPostgreSQL):
        migration_url()
    with pytest.raises(NotPostgreSQL):
        runtime_role()
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "postgresql+psycopg://rt@127.0.0.1/x_test")
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "sqlite:///x_test")
    with pytest.raises(RoleRefused, match="same database"):
        migration_url()
    assert runtime_role() == "rt"
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "postgresql+psycopg://127.0.0.1/x_test")
    with pytest.raises(RoleRefused, match="must name the runtime role"):
        runtime_role()


def test_the_two_urls_must_name_one_database_with_two_roles(monkeypatch):
    from erpclaw_core.db import migration_url

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "postgresql+psycopg://rt@127.0.0.1/books_test")
    with pytest.raises(RoleRefused, match="set ERPCLAW_MIGRATION_URL"):
        migration_url()
    for mig, why in (("postgresql+psycopg://o@127.0.0.1/other_test", "same database"),
                     ("postgresql+psycopg://rt@127.0.0.1/books_test", "different roles")):
        monkeypatch.setenv("ERPCLAW_MIGRATION_URL", mig)
        with pytest.raises(RoleRefused, match=why):
            migration_url()


@pytest.mark.parametrize("command", [["serve"], ["chat"]])
def test_serve_and_chat_refuse_the_owner_credential(monkeypatch, command):
    from typer.testing import CliRunner

    from erpclaw_cli.main import app

    def started(*a, **k):  # if the refusal is gone, fail instead of serving or chatting forever
        raise AssertionError("started with the owner credential")

    monkeypatch.setattr("uvicorn.run", started)
    monkeypatch.setattr("erpclaw_cli.main._client", started)
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "postgresql+psycopg://o@127.0.0.1/x_test")
    out = CliRunner().invoke(app, command)
    assert out.exit_code == 1 and "owner credential" in out.output


# --- PostgreSQL: a genuine runtime login ----------------------------------------

@pytest.fixture
def rt(engine, runtime_login):
    """An engine connected as the runtime login."""
    eng = make_engine(runtime_login)
    yield eng
    eng.dispose()


def _role(url: str) -> str:
    return make_url(url).username


def _approve(eng, contract, **args):
    card = approvals.propose(eng, REGISTRY.contracts[contract], args)
    out = approvals.approve(eng, card.approval_id, card.digest)
    assert out.status == "committed", out.text
    assert approvals.read_back(eng, out.operation_key).status == "committed"
    return out


def test_the_runtime_login_drafts_submits_and_reads_back_but_cannot_change_a_fixed_figure(engine, rt, runtime_login):
    """The invoice path under a genuine runtime login, and the
    database refusing what its policy or the sales triggers forbid."""
    from erpclaw_sales import tables as t

    d = _approve(rt, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}], submit=False)
    with rt.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == d.operation_key)).mappings().one()
    sub = _approve(rt, "submit_invoice", invoice=inv["number"])
    assert inv["number"] in approvals.read_back(rt, sub.operation_key).text
    for sql, why in [("UPDATE sales_invoice SET total = '1.00' WHERE id = :i", "permission denied"),
                     ("UPDATE sales_invoice_line SET amount = '1.00' WHERE invoice_id = :i", "permission denied"),
                     ("UPDATE sales_invoice SET status = 'draft', outstanding = '0.00' WHERE id = :i",
                      "status only moves forward"),
                     ("INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, rate, "
                      "amount) SELECT 'rt-line', company_id, invoice_id, currency, item_id, '1', rate, rate "
                      "FROM sales_invoice_line WHERE invoice_id = :i LIMIT 1", "only into a draft")]:
        with pytest.raises(DBAPIError, match=why):
            with rt.begin() as conn:
                conn.execute(text(sql), {"i": inv["id"]})


def test_a_temporary_table_cannot_stand_in_for_the_invoice_in_the_line_trigger(engine, rt, runtime_login):
    """PostgreSQL searches pg_temp first unless it is named: the trigger functions pin the books' schema and
    put pg_temp last, so a session's temporary sales_invoice saying 'draft' changes nothing."""
    from erpclaw_sales import tables as t

    out = _approve(rt, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}])
    with rt.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == out.operation_key)).mappings().one()
    with pytest.raises(DBAPIError) as e:
        with rt.begin() as conn:
            conn.exec_driver_sql("CREATE TEMP TABLE sales_invoice AS SELECT * FROM public.sales_invoice")
            conn.exec_driver_sql("UPDATE pg_temp.sales_invoice SET status = 'draft'")
            conn.execute(text("INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, "
                              "rate, amount) SELECT 'tmp-line', company_id, invoice_id, currency, item_id, '1', rate, "
                              "rate FROM public.sales_invoice_line WHERE invoice_id = :i LIMIT 1"), {"i": inv["id"]})
    assert "only into a draft" in str(e.value)  # the runtime role may create temporary tables; it changed nothing


def test_the_runtime_login_does_the_engines_work(engine, rt, runtime_login):
    from erpclaw_sales import tables as t

    assert grants.validate_runtime_session(rt) is None  # no escape: the real checks pass
    inv = _approve(rt, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    _approve(rt, "record_payment", customer="Pied Piper", amount="100.00", oldest_first=True, method="check")
    with rt.connect() as conn:
        number = conn.execute(select(t.invoice.c.number).where(t.invoice.c.operation_key == inv.operation_key)).scalar()
    _approve(rt, "cancel_invoice", invoice=number)  # with its reversal
    with rt.begin() as conn:
        ctx = company_ctx(conn)
        bank, sales = (ledger.account_by_number(ctx, n)["id"] for n in ("1100", "4100"))
        posted = ledger.post(ctx, date(2026, 10, 2), "journal", new_id(),
                             [ledger.Line(bank, debit=Decimal("5.00")), ledger.Line(sales, credit=Decimal("5.00"))])
        ledger.reverse(ctx, posted.posting_id, date(2026, 10, 3))
        ledger.lock_through(ctx, date(2025, 12, 31))
        fy = conn.execute(select(m.fiscal_year.c.id).where(m.fiscal_year.c.company_id == ctx.company_id,
                                                           m.fiscal_year.c.label == 2025)).scalar()
        ledger.close_year(ctx, fy)
        disable_plugin(conn, "email_folder")
    create_company(engine, "External Co")  # an external company, set up by the owner
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="External Co")
        ik.set_system_of_record(ctx, "accounting", "external", "QuickBooks")
        for n in ("1100", "4100"):
            ik.map_account(ctx, "QuickBooks", ledger.account_by_number(ctx, n)["id"], f"QB-{n}")
    with rt.begin() as conn:  # the runtime posts to its sub-ledger and stages the journal
        ctx = company_ctx(conn, company_name="External Co")
        bank, sales = (ledger.account_by_number(ctx, n)["id"] for n in ("1100", "4100"))
        ledger.post(ctx, date(2026, 10, 2), "journal", new_id(),
                    [ledger.Line(bank, debit=Decimal("7.00")), ledger.Line(sales, credit=Decimal("7.00"))])
    with engine.connect() as conn:
        assert conn.execute(select(m.sync_journal.c.id).where(m.sync_journal.c.direction == "out")).first()


REFUSED = [
    "UPDATE posting SET memo = 'x'", "DELETE FROM posting", "UPDATE ledger_entry SET debit = '0.00'",
    "DELETE FROM ledger_entry", "TRUNCATE posting", "TRUNCATE ledger_entry", "TRUNCATE posting CASCADE",
    "TRUNCATE ledger_entry CASCADE", "ALTER TABLE posting DISABLE TRIGGER ALL", "ALTER TABLE ledger_entry DISABLE TRIGGER ALL",
    "DROP TABLE ledger_entry", "CREATE TABLE planted (id int)", "UPDATE fiscal_period SET start_date = '2020-01-01'",
    "UPDATE sales_invoice SET total = '1.00'", "UPDATE posting_chain_head SET company_id = company_id",
    "INSERT INTO plugin_install SELECT * FROM plugin_install",
]


def _snapshot(engine):
    with engine.connect() as conn:
        return {name: conn.execute(text(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {name} t")).scalar()
                for name in ("posting", "ledger_entry", "fiscal_period", "sales_invoice", "posting_chain_head")}


@pytest.mark.parametrize("sql", REFUSED)
def test_privilege_refuses_every_mutation_and_schema_change(engine, rt, sql):
    before = _snapshot(engine)
    with pytest.raises(DBAPIError, match="permission denied|must be owner"):
        with rt.begin() as conn:
            conn.exec_driver_sql(sql)
    assert _snapshot(engine) == before


def _refused_open(url, match):
    eng = make_engine(url)
    try:
        with pytest.raises(RoleRefused, match=match):
            grants.validate_runtime_session(eng)
    finally:
        eng.dispose()


def _owner(engine) -> str:
    with engine.connect() as conn:
        return conn.exec_driver_sql("SELECT tableowner FROM pg_tables WHERE tablename = 'posting'").scalar()


def test_an_owner_session_that_switched_role_is_refused(engine, rt, runtime_login):
    from sqlalchemy import event

    def switch(dbapi, _):  # committed, so the driver's rollback on checkout keeps it
        dbapi.cursor().execute(f"SET ROLE {_role(runtime_login)}")
        dbapi.commit()

    eng = make_engine(PG_URL)
    event.listen(eng, "connect", switch)
    eng.dispose()  # make_engine's own check already pooled a connection without the switch
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("SELECT current_user").scalar() == _role(runtime_login)
        with pytest.raises(RoleRefused, match="SET ROLE"):
            grants.validate_runtime_session(eng)
    finally:
        eng.dispose()


@pytest.mark.parametrize("via", ["direct", "intermediate"])
def test_a_runtime_login_belonging_to_the_owner_is_refused(engine, rt, runtime_login, via):
    role, owner = _role(runtime_login), _owner(engine)
    middle = f"erpclaw_mid_{secrets.token_hex(3)}"
    with engine.begin() as conn:
        if via == "direct":
            conn.exec_driver_sql(f"GRANT {owner} TO {role}")
        else:
            conn.exec_driver_sql(f"CREATE ROLE {middle} NOLOGIN")
            conn.exec_driver_sql(f"GRANT {owner} TO {middle}")
            conn.exec_driver_sql(f"GRANT {middle} TO {role}")
    try:
        _refused_open(runtime_login, "member of")
    finally:
        with engine.begin() as conn:
            if via == "direct":
                conn.exec_driver_sql(f"REVOKE {owner} FROM {role}")
            else:
                conn.exec_driver_sql(f"DROP ROLE {middle}")


def test_a_superuser_login_is_refused(engine, rt, monkeypatch):
    _refused_open(PG_URL, "superuser")  # the test database's owner is a superuser


@pytest.mark.parametrize("via", ["public", "another role"])
def test_an_extra_privilege_from_public_or_another_role_is_refused(engine, rt, runtime_login, via):
    role, other = _role(runtime_login), f"erpclaw_x_{secrets.token_hex(3)}"
    with engine.begin() as conn:
        if via == "public":
            conn.exec_driver_sql("GRANT UPDATE ON posting TO PUBLIC")
        else:
            conn.exec_driver_sql(f"CREATE ROLE {other} NOLOGIN")
            conn.exec_driver_sql(f"GRANT DELETE ON ledger_entry TO {other}")
            conn.exec_driver_sql(f"GRANT {other} TO {role}")
    try:
        _refused_open(runtime_login, "has UPDATE" if via == "public" else f"belongs to {other}")
    finally:
        with engine.begin() as conn:
            if via == "public":
                conn.exec_driver_sql("REVOKE UPDATE ON posting FROM PUBLIC")
            else:
                conn.exec_driver_sql(f"REVOKE DELETE ON ledger_entry FROM {other}")
                conn.exec_driver_sql(f"DROP ROLE {other}")


@pytest.mark.parametrize("via", ["direct", "through another role"])
def test_a_membership_without_inheritance_is_refused(engine, rt, runtime_login, via):
    """Exact direct grants are not enough: a role whose privileges are not inherited
    can still be taken on with SET ROLE."""
    role, strong, mid = _role(runtime_login), f"erpclaw_s_{secrets.token_hex(3)}", f"erpclaw_m_{secrets.token_hex(3)}"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE ROLE {strong} NOLOGIN")
        conn.exec_driver_sql(f"GRANT SELECT, UPDATE ON sales_invoice TO {strong}")
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {strong}")  # apply_grants took it from PUBLIC
        if via == "direct":
            conn.exec_driver_sql(f"GRANT {strong} TO {role} WITH INHERIT FALSE")
        else:
            conn.exec_driver_sql(f"CREATE ROLE {mid} NOLOGIN")
            conn.exec_driver_sql(f"GRANT {strong} TO {mid} WITH INHERIT FALSE")
            conn.exec_driver_sql(f"GRANT {mid} TO {role} WITH INHERIT FALSE")
    try:
        with engine.connect() as conn:  # the effective privileges alone look exact
            assert grants.privilege_differences(conn, role) == []
        with rt.connect() as conn:  # yet the authority is reachable
            conn.exec_driver_sql(f"SET ROLE {strong}")
            assert conn.exec_driver_sql("UPDATE sales_invoice SET total = total").rowcount > 0  # beyond the policy
            conn.rollback()
        _refused_open(runtime_login, "belongs to")
    finally:
        rt.dispose()
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DROP ROLE IF EXISTS {mid}")
            conn.exec_driver_sql(f"REVOKE SELECT, UPDATE ON sales_invoice FROM {strong}")
            conn.exec_driver_sql(f"REVOKE USAGE ON SCHEMA public FROM {strong}")
            conn.exec_driver_sql(f"DROP ROLE {strong}")


@pytest.mark.parametrize("grant, undo, why", [
    ("ALTER DATABASE {db} OWNER TO {role}", "ALTER DATABASE {db} OWNER TO {owner}", "owns the database"),
    ("GRANT CREATE ON DATABASE {db} TO {role}", "REVOKE CREATE ON DATABASE {db} FROM {role}",
     "has CREATE on the database"),
    ("ALTER ROLE {role} CREATEROLE", "ALTER ROLE {role} NOCREATEROLE", "has CREATEROLE"),
    ("ALTER ROLE {role} REPLICATION", "ALTER ROLE {role} NOREPLICATION", "has REPLICATION"),
], ids=["database-owner", "database-create", "createrole", "replication"])
def test_authority_outside_the_table_privileges_is_refused(engine, rt, runtime_login, grant, undo, why):
    """A database owner is an implicit member of
    pg_database_owner, which no membership list shows; the schema is moved to
    the tables' owner so that only the database ownership is left to catch it."""
    with engine.connect() as conn:
        db, schema_owner = conn.exec_driver_sql(
            "SELECT current_database(), pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'").one()
    names = {"db": db, "role": _role(runtime_login), "owner": _owner(engine)}
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER SCHEMA public OWNER TO {names['owner']}")
        conn.exec_driver_sql(grant.format(**names))
    try:
        _refused_open(runtime_login, why)
        with pytest.raises(grants.GrantsRefused, match=why):
            with engine.begin() as conn:
                grants.apply_grants(conn, names["role"])
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(undo.format(**names))
            conn.exec_driver_sql(f"ALTER SCHEMA public OWNER TO {schema_owner}")


TRIGGERS = "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'posting'::regclass AND NOT tgisinternal"


def test_a_runtime_role_that_owns_the_trigger_function_is_refused(engine, rt, runtime_login):
    """A function's owner can drop the ledger's
    triggers with CASCADE, with no table privilege at all."""
    role = _role(runtime_login)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER FUNCTION erpclaw_refuse_change() OWNER TO {role}")
    try:
        with rt.connect() as conn:  # the authority is real: the triggers go
            conn.exec_driver_sql("DROP FUNCTION erpclaw_refuse_change() CASCADE")
            assert conn.exec_driver_sql(TRIGGERS).scalar() == 0
            conn.rollback()
        _refused_open(runtime_login, "owns 1 object")
        with pytest.raises(grants.GrantsRefused, match="owns 1 object"):
            with engine.begin() as conn:
                grants.apply_grants(conn, role)
    finally:
        rt.dispose()
        with engine.begin() as conn:
            conn.exec_driver_sql(f"ALTER FUNCTION erpclaw_refuse_change() OWNER TO {_owner(engine)}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql(TRIGGERS).scalar() > 0


@pytest.mark.parametrize("grantee", ["role", "PUBLIC"])
def test_a_parameter_privilege_is_refused(engine, rt, runtime_login, grantee):
    """SET on session_replication_role turns the
    ledger's triggers and foreign keys off for the session."""
    role = _role(runtime_login)
    to = role if grantee == "role" else "PUBLIC"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"GRANT SET ON PARAMETER session_replication_role TO {to}")
    try:
        with rt.connect() as conn:  # the authority is real
            conn.exec_driver_sql("SET session_replication_role = replica")
            conn.rollback()
        _refused_open(runtime_login, "session_replication_role")
        with pytest.raises(grants.GrantsRefused, match="session_replication_role"):
            with engine.begin() as conn:
                grants.apply_grants(conn, role)
    finally:
        rt.dispose()
        with engine.begin() as conn:
            conn.exec_driver_sql(f"REVOKE SET ON PARAMETER session_replication_role FROM {to}")


def test_the_development_escape_needs_a_test_database(engine, rt, monkeypatch):
    name = f"erpclaw_escape_{secrets.token_hex(3)}"
    admin = make_engine(PG_URL).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE {name}")
    other = make_engine(make_url(PG_URL).set(database=name).render_as_string(hide_password=False))
    try:
        monkeypatch.setenv("ERPCLAW_ALLOW_OWNER_RUNTIME", "1")
        with pytest.raises(RoleRefused, match="only for databases named"):
            grants.validate_runtime_session(other)
    finally:
        other.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


# --- applying grants ------------------------------------------------------------

def _grant_rows(engine, role):
    with engine.connect() as conn:
        return sorted(conn.execute(text(
            "SELECT table_name, privilege_type, '' FROM information_schema.role_table_grants WHERE grantee IN (:r, 'PUBLIC') "
            "UNION ALL SELECT table_name, privilege_type, column_name FROM information_schema.column_privileges "
            "WHERE grantee IN (:r, 'PUBLIC') AND table_schema = current_schema()"), {"r": role}).all())


def test_apply_grants_is_idempotent_and_removes_privileges_added_by_hand(engine, rt, runtime_login):
    role = _role(runtime_login)
    once = _grant_rows(engine, role)
    with engine.begin() as conn:
        grants.apply_grants(conn, role)
    assert _grant_rows(engine, role) == once
    with engine.begin() as conn:
        conn.exec_driver_sql(f"GRANT TRUNCATE ON posting TO {role}")
        conn.exec_driver_sql("GRANT DELETE ON ledger_entry TO PUBLIC")
        conn.exec_driver_sql(f"GRANT UPDATE (memo) ON posting TO {role}")
    with engine.connect() as conn:
        diffs = grants.privilege_differences(conn, role)
    assert {"posting: has TRUNCATE", "ledger_entry: has DELETE", "posting.memo: has UPDATE"} <= set(diffs)
    with engine.begin() as conn:
        grants.apply_grants(conn, role)
    assert _grant_rows(engine, role) == once
    assert grants.validate_runtime_session(rt) is None


def test_apply_grants_refuses_a_privilege_it_cannot_revoke(engine, rt, runtime_login):
    """A privilege held through another role is not the owner's to revoke: applying
    the grants verifies effective privileges and fails, rolling back."""
    role, other = _role(runtime_login), f"erpclaw_y_{secrets.token_hex(3)}"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE ROLE {other} NOLOGIN")
        conn.exec_driver_sql(f"GRANT DELETE ON ledger_entry TO {other}")
        conn.exec_driver_sql(f"GRANT {other} TO {role}")
    try:
        with pytest.raises(grants.GrantsRefused, match=f"belongs to {other}"):
            with engine.begin() as conn:
                grants.apply_grants(conn, role)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"REVOKE DELETE ON ledger_entry FROM {other}")
            conn.exec_driver_sql(f"DROP ROLE {other}")


def test_a_table_without_a_policy_refuses_naming_it(engine, rt, runtime_login):
    with pytest.raises(grants.GrantsRefused, match="table stray_thing has no runtime policy"):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE stray_thing (id int)")
            grants.apply_grants(conn, _role(runtime_login))


# --- lifecycle: each command that creates tables applies the grants ---------------

def _exact(engine, runtime_login):
    with engine.connect() as conn:
        assert grants.privilege_differences(conn, _role(runtime_login)) == []
        names = set(conn.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").scalars())
    assert "alembic_version" in names


def test_init_applies_the_grants(engine, rt, runtime_login, monkeypatch):
    from typer.testing import CliRunner

    from erpclaw_cli.main import app

    rt.dispose()
    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init"])
    assert out.exit_code == 0, out.output
    _exact(engine, runtime_login)
    with rt.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    _approve(rt, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}])


def test_plugins_install_applies_the_grants_to_its_own_table(engine, rt, runtime_login, monkeypatch):
    from sqlalchemy import Column, String
    from typer.testing import CliRunner

    from erpclaw_cli.main import app
    from test_plugins import EXTRA_BRANCH, plug

    extra = Table("extra_thing", metadata, Column("id", String(36), primary_key=True),
                  info={"owner": "extra", "runtime": APPEND})
    monkeypatch.setitem(REGISTRY.plugins, "extra", plug("extra", owned_tables=["extra_thing"], migrations=EXTRA_BRANCH))
    try:
        cli_urls(monkeypatch, engine, runtime_login)
        out = CliRunner().invoke(app, ["plugins", "install", "extra", "--yes"])
        assert out.exit_code == 0, out.output
        with engine.connect() as conn:  # its revision ran in the install transaction
            assert "extra_0001" in conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
        _exact(engine, runtime_login)
        with rt.begin() as conn:
            conn.exec_driver_sql("INSERT INTO extra_thing VALUES ('a')")
        with pytest.raises(DBAPIError, match="permission denied"):
            with rt.begin() as conn:
                conn.exec_driver_sql("DELETE FROM extra_thing")
    finally:
        metadata.remove(extra)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS extra_thing")
            conn.exec_driver_sql("DELETE FROM alembic_version WHERE version_num = 'extra_0001'")


def test_the_grants_command_repairs_the_privileges(engine, rt, runtime_login, monkeypatch):
    from typer.testing import CliRunner

    from erpclaw_cli.main import app

    with engine.begin() as conn:
        conn.exec_driver_sql(f"REVOKE INSERT ON posting FROM {_role(runtime_login)}")
    with pytest.raises(grants.GrantsRefused, match="posting: lacks INSERT"):
        grants.validate_runtime_session(rt)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["grants"])
    assert out.exit_code == 0 and "holds exactly its policy" in out.output
    assert grants.validate_runtime_session(rt) is None


def test_the_api_and_the_cli_open_books_only_as_the_runtime_role(engine, rt, runtime_login, monkeypatch):
    from typer.testing import CliRunner

    from erpclaw_api.app import create_app
    from erpclaw_cli.main import app

    owner = make_engine(PG_URL)
    try:
        with pytest.raises(RoleRefused, match="superuser"):
            create_app(owner, model=None)
    finally:
        owner.dispose()
    assert create_app(rt, model=None)  # the genuine runtime login opens
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", PG_URL)  # the owner as runtime
    out = CliRunner().invoke(app, ["plugins", "disable", "email_folder"])
    assert out.exit_code == 1 and "superuser" in out.output



def test_a_fresh_process_knows_every_loaded_plugins_tables_before_anything_imports_them(engine):
    """`erpclaw demo chat` refused "table sales_invoice has no runtime policy": the runtime check ran before the
    plugins loaded, in a process that had not imported the sales plugin. Tests in one process hid it."""
    import subprocess
    import sys

    url = engine.url.render_as_string(hide_password=False)
    code = ("import sys; from sqlalchemy import create_engine; from erpclaw_core import grants; "
            "e = create_engine(sys.argv[1]); c = e.connect(); p = grants._policies(c); "
            "print('sales_invoice' in p and 'sales_invoice_line' in p)")
    out = subprocess.run([sys.executable, "-c", code, url], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "True", out.stderr[-2000:]
