"""`erpclaw demo up`, `chat` and `reset`: the demo makes and removes only its own database, login and folder.

Every refusal is checked against what its guard protects, compared before and after: the databases and roles on the
server, the demo login's password verifier and grants, another database's grants, and a digest of the books in use.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from conftest import PG_URL
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli import demo, main
from erpclaw_cli.evals.run import snapshot
from erpclaw_core.db import DEMO_MARK, OWNER_MARK, create_all
from erpclaw_sales import tables as t


class Local(ScriptedModel):
    """A scripted model that answers like the local route: ready, and described."""

    def check_ready(self):
        return None

    def describe(self):
        return {"provider": "openai-compatible", "model": "scripted", "base_url": "local", "thinking": False}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """The test server as the demo's server, a demo name of the test's own, the demo home in the test's folder;
    everything the test made on the server is removed afterwards."""
    name = f"t{secrets.token_hex(4)}_demo"
    monkeypatch.setenv(demo.SERVER_VAR, PG_URL)
    monkeypatch.setenv(demo.HOME_VAR, str(tmp_path / "demo-home"))
    for var in ("ERPCLAW_HOME", "ERPCLAW_MAIL_DIR", "ERPCLAW_ALLOW_OWNER_RUNTIME"):  # restored after the chat
        monkeypatch.setenv(var, os.environ.get(var, ""))
    adm = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    yield name, adm
    with adm.connect() as c:
        for db in [d for (d,) in c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                                     {"p": f"{name.removesuffix('_demo')}%"})]:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        if c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %(r)s", {"r": demo.login_of(name)}).first():
            c.exec_driver_sql(f'DROP OWNED BY "{demo.login_of(name)}"')
            c.exec_driver_sql(f'DROP ROLE "{demo.login_of(name)}"')
        for r in [r for (r,) in c.exec_driver_sql("SELECT rolname FROM pg_roles WHERE rolname LIKE %(p)s",
                                                    {"p": f"{name}_other%"})]:
            c.exec_driver_sql(f'DROP ROLE "{r}"')
    adm.dispose()


def cli(*args, typed=(), model=None, monkeypatch=None):
    if model is not None:
        monkeypatch.setattr(demo, "_model", lambda: model)
    return CliRunner().invoke(main.app, ["demo", *args], input="\n".join([*typed, "quit"]) + "\n")


def said(out) -> str:
    """The command's output as one line: the window wraps long messages."""
    return " ".join(out.output.split())


def on(name: str):
    return create_engine(make_url(PG_URL).set(database=name), poolclass=NullPool)


def server_state(adm, name: str) -> dict:
    """What the guards protect: every database and role, the demo login's verifier, attributes, comment and
    memberships, and every database's ACL."""
    login = demo.login_of(name)
    with adm.connect() as c:
        return {
            "databases": sorted(c.exec_driver_sql("SELECT datname, datacl::text FROM pg_database").all()),
            "roles": sorted(r for (r,) in c.exec_driver_sql("SELECT rolname FROM pg_roles")),
            "login": c.exec_driver_sql(
                "SELECT rolpassword, rolsuper, rolcanlogin, shobj_description(oid, 'pg_authid') FROM pg_authid "
                "WHERE rolname = %(r)s", {"r": login}).first(),
            "members": sorted(c.exec_driver_sql("SELECT roleid::int, member::int FROM pg_auth_members").all()),
        }


def books_digest(engine) -> str:
    rows = {name: sorted(map(repr, table.items())) for name, table in snapshot(engine).items()}
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def invoices(name: str) -> int:
    eng = on(name)
    try:
        with eng.connect() as c:
            return c.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    finally:
        eng.dispose()


INVOICE = call("create_invoice", customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5}])


# --- up, chat, reset ------------------------------------------------------------------------------------------

def test_up_makes_the_demo_books_their_login_and_folder(server):
    name, adm = server
    out = cli("up", "--name", name)
    assert out.exit_code == 0, out.output
    assert f"/{name}" in said(out) and "Stark Manufacturing" in said(out) and "erpclaw demo chat" in said(out)
    eng = on(name)
    with eng.connect() as c:
        assert {OWNER_MARK, DEMO_MARK} <= {m for (m,) in c.exec_driver_sql("SELECT mark FROM erpclaw_books")}
    eng.dispose()
    with adm.connect() as c:
        assert c.exec_driver_sql("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %(r)s",
                                 {"r": demo.login_of(name)}).scalar() == demo.login_comment(name)
    folder = demo.folder_of(name)
    assert json.loads((folder / "DEMO.json").read_text()) == {"database": name, "login": demo.login_of(name)}
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


def test_chat_saves_an_invoice_as_the_demo_login_and_keeps_its_files_in_the_demo_folder(server, monkeypatch):
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    n = invoices(name)
    import erpclaw_core.db as core_db

    seen, real = [], core_db.make_engine

    def watched(url=None):
        eng = real(url)
        with eng.connect() as conn:
            seen.append(conn.exec_driver_sql("SELECT current_user").scalar())
        return eng

    monkeypatch.setattr(core_db, "make_engine", watched)
    model = Local([Reply("", [INVOICE]),
                   Reply("", [call("find_capability", request="run payroll")]),
                   Reply("", [call("choose_capability", pick="planned.payroll")]),
                   Reply("", [call("find_capability", request="fly me to the moon")]),
                   Reply("", [call("choose_capability", pick="none")])])
    out = cli("chat", "--name", name, typed=["invoice Acme Corporation for 5 copper wire", "a", "run payroll",
                                              "fly me to the moon"], model=model, monkeypatch=monkeypatch)
    assert out.exit_code == 0, out.output
    assert "Saved and submitted invoice INV-2026-" in said(out)
    assert invoices(name) == n + 1
    assert seen == [demo.login_of(name)]  # the runtime login, never the server role
    store = demo.folder_of(name) / "assistant.sqlite"
    rows = sqlite3.connect(store).execute("SELECT outcome FROM capability_demand ORDER BY rowid").fetchall()
    assert rows == [("c",), ("d",)]


def test_reset_makes_the_demo_again_and_down_removes_everything(server, monkeypatch):
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    seeded = invoices(name)
    model = Local([Reply("", [INVOICE])])
    assert cli("chat", "--name", name, typed=["invoice Acme Corporation for 5 copper wire", "a"], model=model,
               monkeypatch=monkeypatch).exit_code == 0
    assert invoices(name) == seeded + 1
    out = cli("reset", "--name", name)
    assert out.exit_code == 0, out.output
    assert invoices(name) == seeded
    out = cli("reset", "--name", name, "--down")
    assert out.exit_code == 0, out.output
    assert demo.database_oid(adm, name) is None
    with adm.connect() as c:
        assert not c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %(r)s",
                                     {"r": demo.login_of(name)}).first()
    assert not os.path.lexists(demo.folder_of(name))


# --- the login is the demo's alone ----------------------------------------------------------------------------

def test_a_login_of_that_name_already_there_refuses_up(server):
    name, adm = server
    with adm.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{demo.login_of(name)}" LOGIN PASSWORD \'x{secrets.token_hex(8)}\'')
    before = server_state(adm, name)
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and "the demo makes its own" in said(out)
    assert server_state(adm, name) == before and not os.path.lexists(demo.folder_of(name))


def _used_elsewhere(adm, name, how, other_db):
    login = demo.login_of(name)
    if how == "connect":
        with adm.connect() as c:
            c.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{other_db}" TO "{login}"')
    elif how == "owns":
        eng = on(other_db)
        with eng.begin() as c:
            c.exec_driver_sql("CREATE TABLE elsewhere (x int)")
            c.exec_driver_sql(f'ALTER TABLE elsewhere OWNER TO "{login}"')
        eng.dispose()
    elif how == "member":
        with adm.connect() as c:
            c.exec_driver_sql(f'CREATE ROLE "{name}_other"')
            c.exec_driver_sql(f'GRANT "{name}_other" TO "{login}"')
    elif how == "has_member":
        with adm.connect() as c:
            c.exec_driver_sql(f'CREATE ROLE "{name}_other"')
            c.exec_driver_sql(f'GRANT "{login}" TO "{name}_other"')
    elif how == "no_comment":
        with adm.connect() as c:
            c.exec_driver_sql(f'COMMENT ON ROLE "{login}" IS NULL')
    elif how == "createdb":
        with adm.connect() as c:
            c.exec_driver_sql(f'ALTER ROLE "{login}" CREATEDB')


@pytest.mark.parametrize("how", ["connect", "owns", "member", "has_member", "no_comment", "createdb"])
@pytest.mark.parametrize("command", ["chat", "reset"])
def test_a_login_used_outside_the_demo_refuses_chat_and_reset_and_changes_nothing(server, monkeypatch, engine,
                                                                                    how, command):
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    other = f"{name.removesuffix('_demo')}_other"
    with adm.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{other}"')
    _used_elsewhere(adm, name, how, other)
    before, books, demo_books = server_state(adm, name), books_digest(engine), invoices(name)
    out = cli(command, "--name", name, typed=["invoice Acme Corporation for 5 copper wire", "a"],
              model=Local([Reply("", [INVOICE])]), monkeypatch=monkeypatch)
    assert out.exit_code == 1, out.output
    assert "nothing was changed" in said(out)
    assert server_state(adm, name) == before  # the verifier, the grants, the memberships, every database
    assert books_digest(engine) == books and invoices(name) == demo_books
    assert (demo.folder_of(name) / "DEMO.json").exists()


def test_the_login_is_accepted_when_only_the_demo_uses_it(server):
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    assert demo.check_login(adm, name, demo.database_oid(adm, name)) is True


# --- refusals, each beside an accepted case -------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["erpclaw", "books", "Erpclaw_demo", "1x_demo", "x-y_demo", "_demo", "x_demo; drop",
                                 "a" * 56 + "_demo"])
def test_a_name_that_is_not_a_demo_name_is_refused_before_anything(server, bad):
    name, adm = server
    before = server_state(adm, name)
    out = cli("up", "--name", bad)
    assert out.exit_code == 1 and "is not a demo name" in said(out)
    assert server_state(adm, name) == before


def test_the_name_bound_is_60_bytes_and_the_login_is_never_truncated(server):
    name, adm = server
    long = name.removesuffix("_demo") + "x" * (55 - len(name.removesuffix("_demo"))) + "_demo"
    assert len(long) == 60
    try:
        assert cli("up", "--name", long).exit_code == 0
        with adm.connect() as c:
            assert c.exec_driver_sql("SELECT rolname FROM pg_roles WHERE rolname = %(r)s",
                                     {"r": long + "_rt"}).scalar() == long + "_rt"
    finally:
        demo.reset(long, down=True)
    assert cli("up", "--name", "x" + long).exit_code == 1


def test_a_server_url_that_names_no_database_is_refused(server, monkeypatch):
    name, adm = server
    monkeypatch.setenv(demo.SERVER_VAR, PG_URL.rsplit("/", 1)[0] if "?" not in PG_URL else PG_URL)
    if "?" in PG_URL:
        pytest.skip("the test server URL carries options")
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and "must name a database" in said(out)
    assert demo.database_oid(adm, name) is None


def test_your_books_database_is_refused(server, monkeypatch):
    name, adm = server
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", str(make_url(PG_URL).set(database=name)))
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and "your books' database" in said(out)
    assert demo.database_oid(adm, name) is None
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", str(make_url(PG_URL).set(database="elsewhere_test")))
    assert cli("up", "--name", name).exit_code == 0


@pytest.mark.parametrize("how", ["dbname", "service", "pgdatabase", "pguser"])
@pytest.mark.parametrize("command", ["up", "chat", "reset"])
def test_your_books_named_any_other_way_are_refused_and_nothing_changes(server, monkeypatch, how, command):
    name, adm = server
    if command != "up":
        assert cli("up", "--name", name).exit_code == 0
    base = make_url(PG_URL).set(database="elsewhere_test")
    if how in ("pgdatabase", "pguser"):  # PostgreSQL would open the database these name: the URL names none
        monkeypatch.setenv("PGDATABASE" if how == "pgdatabase" else "PGUSER", name)
        # no database in the URL (URL.set cannot remove one), and for PGUSER no user either; host and port as the
        # server's, absent ones left absent (a socket server has neither), and no query, so only the name is tested
        user = {} if how == "pguser" else {"username": base.username, "password": "x"}
        url = URL.create(base.drivername, host=base.host, port=base.port, **user).render_as_string(hide_password=False)
        assert make_url(url).database is None and not make_url(url).query
        assert (make_url(url).username is None) == (how == "pguser")
    else:
        url = f"{base}{'&' if '?' in str(base) else '?'}{how}={name}"
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", url)
    before, demo_books = server_state(adm, name), (invoices(name) if command != "up" else None)
    out = cli(command, "--name", name, model=Local([]), monkeypatch=monkeypatch)
    assert out.exit_code == 1, said(out)
    assert "cannot rule out" in said(out) and "nothing was changed" in said(out)
    if how in ("pgdatabase", "pguser"):
        assert "names no database" in said(out)  # the refusal of the URL itself, not of an option it carries
    assert server_state(adm, name) == before
    if command != "up":
        assert invoices(name) == demo_books


def test_a_refused_login_is_reported_without_its_password(server, monkeypatch):
    name, adm = server
    weak = f"{name}_other_weak"
    secret = f"x{secrets.token_hex(12)}"
    with adm.connect() as c:  # a server role that may make databases but not roles
        c.exec_driver_sql(f'CREATE ROLE "{weak}" LOGIN CREATEDB PASSWORD \'{secret}\'')
    made = []
    monkeypatch.setattr(demo, "_password", lambda: made.append(secrets.token_hex(16)) or made[-1])
    monkeypatch.setenv(demo.SERVER_VAR, make_url(PG_URL).set(username=weak, password=secret)
                       .render_as_string(hide_password=False))
    before = server_state(adm, name)
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and said(out) == (
        "PostgreSQL refused the demo's login: the server role lacks a privilege the demo needs (it must create "
        "databases and roles) (SQLSTATE 42501)."), said(out)
    assert made and made[0] not in out.output and "Traceback" not in out.output
    assert out.exception is None or isinstance(out.exception, SystemExit)
    assert server_state(adm, name) == before and not os.path.lexists(demo.folder_of(name))


@pytest.mark.parametrize("marks", [[OWNER_MARK], [DEMO_MARK], []])
def test_a_demo_named_database_without_both_marks_is_refused(server, marks):
    name, adm = server
    with adm.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    if marks == [OWNER_MARK]:
        eng = on(name)
        create_all(eng)  # the ownership mark only: the demo mark is the demo seed's
        eng.dispose()
    elif marks:
        eng = on(name)
        with eng.begin() as c:
            c.exec_driver_sql("CREATE TABLE erpclaw_books (mark varchar(64) PRIMARY KEY)")
            c.exec_driver_sql("INSERT INTO erpclaw_books VALUES (%(m)s)", {"m": DEMO_MARK})
        eng.dispose()
    with adm.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{demo.login_of(name)}" LOGIN')
        c.exec_driver_sql(f"COMMENT ON ROLE \"{demo.login_of(name)}\" IS '{demo.login_comment(name)}'")
    before = server_state(adm, name)
    for command in ("chat", "reset"):
        out = cli(command, "--name", name)
        assert out.exit_code == 1 and "is not a demo the engine made" in said(out)
    assert server_state(adm, name) == before


def test_up_when_the_demo_is_up_and_chat_or_reset_when_it_is_not(server):
    name, adm = server
    out = cli("chat", "--name", name)
    assert out.exit_code == 1 and "There is no demo" in said(out)
    assert cli("up", "--name", name).exit_code == 0
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and "already up" in said(out)


@pytest.mark.parametrize("query", ["dbname=other", "options=-csearch_path%3Dx", "host=elsewhere"])
def test_a_server_url_with_more_than_transport_options_is_refused(server, monkeypatch, query):
    name, adm = server
    monkeypatch.setenv(demo.SERVER_VAR, f"{PG_URL}{'&' if '?' in PG_URL else '?'}{query}")
    out = cli("up", "--name", name)
    assert out.exit_code == 1 and "may carry only" in said(out)
    assert demo.database_oid(adm, name) is None


def test_reset_leaves_a_folder_that_is_not_the_demos_alone(server, tmp_path):
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    folder = demo.folder_of(name)
    (folder / "DEMO.json").write_text(json.dumps({"database": "someone_else_demo"}))
    out = cli("reset", "--name", name, "--down")
    assert out.exit_code == 1 and "is not the demo's own folder" in said(out)
    assert demo.database_oid(adm, name) is not None and folder.exists()
    (folder / "DEMO.json").unlink()
    folder.rmdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "DEMO.json").write_text(json.dumps({"database": name}))
    folder.symlink_to(target)
    out = cli("reset", "--name", name, "--down")
    assert out.exit_code == 1 and target.exists() and demo.database_oid(adm, name) is not None


# --- nothing else touched, no password kept -------------------------------------------------------------------

OPENED: list[str] | None = None


def _audit(event, args):
    if OPENED is not None and event in ("open", "os.open", "sqlite3.connect") and args and \
            isinstance(args[0], (str, bytes, os.PathLike)):
        OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def test_every_command_opens_files_only_in_the_demo_folder_and_the_installed_engine(server, monkeypatch, tmp_path):
    global OPENED
    name, adm = server
    home = tmp_path / "home"
    (home / ".openclaw" / "erpclaw").mkdir(parents=True)
    (home / ".openclaw" / "erpclaw" / "books.sqlite").write_bytes(b"sentinel")
    mine = tmp_path / "my-erpclaw-home"
    (mine / "mail").mkdir(parents=True)
    sqlite3.connect(mine / "assistant.sqlite").execute("CREATE TABLE kept (x)").connection.commit()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(mine))
    monkeypatch.setenv("ERPCLAW_MAIL_DIR", str(mine / "mail"))

    def digest(root: Path) -> dict:
        return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    kept = digest(home), digest(mine)
    model = Local([Reply("", [call("find_capability", request="run payroll")]),
                   Reply("", [call("choose_capability", pick="planned.payroll")]),
                   Reply("", [call("find_capability", request="fly me to the moon")]),
                   Reply("", [call("choose_capability", pick="none")])])
    OPENED = []
    try:
        assert cli("up", "--name", name).exit_code == 0
        assert cli("chat", "--name", name, typed=["run payroll", "fly me to the moon"], model=model,
                   monkeypatch=monkeypatch).exit_code == 0
        assert cli("reset", "--name", name).exit_code == 0
        assert cli("reset", "--name", name, "--down").exit_code == 0
        opened = list(OPENED)
    finally:
        OPENED = None
    assert (digest(home), digest(mine)) == kept
    assert str(demo.folder_of(name) / "assistant.sqlite") in opened  # the hook saw the chat's store being opened
    allowed = [str(demo.folder_of(name)), sys.prefix, sys.base_prefix, str(Path(demo.__file__).parents[4]), "/dev/"]
    allowed = [os.path.realpath(a) for a in allowed] + allowed
    stray = sorted({p for p in opened if not any(os.path.realpath(os.path.abspath(p)).startswith(a) or
                                                 os.path.abspath(p).startswith(a) for a in allowed)})
    assert stray == []


def test_no_password_is_kept_or_shown(server, monkeypatch, tmp_path):
    name, adm = server
    made = []

    def password():
        made.append(secrets.token_hex(16))
        return made[-1]

    monkeypatch.setattr(demo, "_password", password)
    outs = [cli("up", "--name", name).output,
            cli("chat", "--name", name, typed=[], model=Local([]), monkeypatch=monkeypatch).output]
    assert len(made) == 2
    files = [p for root in (demo.folder_of(name), tmp_path) for p in root.rglob("*") if p.is_file()]
    for secret in made:
        assert not any(secret in o for o in outs)  # compared, never printed
        assert not any(secret.encode() in p.read_bytes() for p in files)


# --- a server the demo cannot reach or log in to: one line, exit 1, no traceback and no password -----------------

def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


AT = demo.where(make_url(PG_URL))  # the test server as the lines name it: host and port, or the local socket


def one_line_refusal(out, *never: str) -> str:
    """A refusal as the commands give it: exit 1, no traceback, and none of the secrets or whole URLs given."""
    assert out.exit_code == 1, out.output
    assert out.exception is None or isinstance(out.exception, SystemExit), repr(out.exception)
    assert "Traceback" not in out.output and "OperationalError" not in out.output, out.output
    for s in [p for p in (make_url(PG_URL).password,) if p] + list(never):
        assert s not in out.output and s not in said(out)
    return said(out)


def enforces_passwords(adm, role: str) -> bool:
    """Whether the test server refuses role with a wrong password (not so under trust or peer authentication)."""
    eng = create_engine(make_url(PG_URL).set(username=role, password=f"x{secrets.token_hex(8)}"), poolclass=NullPool)
    try:
        with eng.connect():
            return False
    except Exception:  # noqa: BLE001  (refused: the server checks passwords)
        return True
    finally:
        eng.dispose()


@pytest.fixture
def wrong_password(server):
    """A server role of the test's own with a password, and a URL for it with the wrong one; skipped where the
    server does not check passwords."""
    name, adm = server
    role, secret = f"{name}_other_pw", f"x{secrets.token_hex(12)}"
    with adm.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{secret}\'')
    if not enforces_passwords(adm, role):
        pytest.skip("the test server does not check passwords")
    wrong = f"wrong-{secrets.token_hex(12)}"
    return role, wrong, make_url(PG_URL).set(username=role, password=wrong).render_as_string(hide_password=False)


def login_refused(user: str) -> str:
    return (f"Couldn't log in to PostgreSQL at {AT} as {user}. Check the password in {demo.SERVER_VAR}, and that "
            "this is the server you meant (another PostgreSQL may be using that port).")


@pytest.mark.parametrize("command", ["up", "chat", "reset"])
def test_a_wrong_server_password_is_one_line_naming_host_port_and_user(server, wrong_password, monkeypatch, command):
    name, adm = server
    role, wrong, url = wrong_password
    monkeypatch.setenv(demo.SERVER_VAR, url)
    before = server_state(adm, name)
    line = one_line_refusal(cli(command, "--name", name, model=Local([]), monkeypatch=monkeypatch), wrong, url)
    assert line == login_refused(role), line
    assert server_state(adm, name) == before and not os.path.lexists(demo.folder_of(name))


@pytest.mark.parametrize("command", ["up", "chat", "reset"])
def test_nothing_listening_is_one_line(server, monkeypatch, command):
    name, _ = server
    port = free_port()
    url = make_url(PG_URL).set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    monkeypatch.setenv(demo.SERVER_VAR, url)
    line = one_line_refusal(cli(command, "--name", name, model=Local([]), monkeypatch=monkeypatch), url)
    assert line == (f"No PostgreSQL is answering at 127.0.0.1:{port}. Start the demo's server (the README's \"Try "
                    f"it\"), or set {demo.SERVER_VAR} to the one you meant."), line


def test_a_server_database_that_is_not_there_is_one_line(server, monkeypatch):
    name, _ = server
    url = make_url(PG_URL).set(database=f"{name}_none").render_as_string(hide_password=False)
    monkeypatch.setenv(demo.SERVER_VAR, url)
    line = one_line_refusal(cli("up", "--name", name), url)
    assert line.startswith(f"PostgreSQL at {AT} has no database {name}_none:"), line


def test_the_demo_login_refused_after_its_password_is_set_is_one_line(server, monkeypatch):
    """Another server answering for the demo's login: the line names the demo's login, never its password."""
    name, adm = server
    assert cli("up", "--name", name).exit_code == 0
    if not enforces_passwords(adm, demo.login_of(name)):
        pytest.skip("the test server does not check passwords")
    made = []
    monkeypatch.setattr(demo, "_password", lambda: made.append(secrets.token_hex(16)) or made[-1])
    monkeypatch.setattr(demo, "_secret", lambda adm, sql: None)  # the password is never set, so the login fails
    line = one_line_refusal(cli("chat", "--name", name, model=Local([]), monkeypatch=monkeypatch), *made)
    assert made and line.startswith(f"Couldn't log in to PostgreSQL at {AT} as {demo.login_of(name)}, the demo's "
                                    "login"), line


def test_the_server_failing_during_the_chat_is_one_line(server, monkeypatch):
    import psycopg
    from sqlalchemy.exc import OperationalError

    name, _ = server
    assert cli("up", "--name", name).exit_code == 0

    def gone(c):
        raise OperationalError("SELECT 1", {}, psycopg.OperationalError("server closed the connection unexpectedly"))

    monkeypatch.setattr(main, "chat_loop", gone)
    line = one_line_refusal(cli("chat", "--name", name, model=Local([]), monkeypatch=monkeypatch))
    assert line.endswith(f"No PostgreSQL is answering at {AT}. Start the demo's server (the README's \"Try it\"), "
                         f"or set {demo.SERVER_VAR} to the one you meant."), line


def test_connection_refusal_lines_name_no_secret():
    import psycopg
    from sqlalchemy.exc import OperationalError, ProgrammingError

    pw = f"pw-{secrets.token_hex(8)}"
    url = URL.create("postgresql+psycopg", username="owner", password=pw, host="db.example", port=6543,
                     database="postgres")
    host = OperationalError("x", {}, psycopg.OperationalError("failed to resolve host 'db.example': [Errno 8]"))
    other = OperationalError("x", {}, psycopg.OperationalError(f"SSL error: {pw}"))
    denied = ProgrammingError("x", {}, psycopg.errors.InsufficientPrivilege(f"permission denied\nDETAIL: {pw}"))
    assert demo.connection_refusal(host, url) == "There is no host db.example to reach; check ERPCLAW_DEMO_PG_URL."
    assert demo.connection_refusal(other, url) == ("Couldn't connect to PostgreSQL at db.example:6543 as owner; "
                                                   "check ERPCLAW_DEMO_PG_URL.")
    assert demo.connection_refusal(denied, url) == (  # the demo's words by SQLSTATE, never the server's text
        "PostgreSQL at db.example:6543 refused the demo: the server role lacks a privilege the demo needs (it must "
        "create databases and roles) (SQLSTATE 42501).")
    odd = ProgrammingError("x", {}, psycopg.errors.SyntaxError("line one\nline two"))
    assert demo.connection_refusal(odd, url) == ("PostgreSQL at db.example:6543 refused the demo: a refusal the demo "
                                                 "does not name (SQLSTATE 42601).")
    assert not any(pw in demo.connection_refusal(e, url) for e in (host, other, denied))
    assert demo.connection_refusal(OperationalError("x", {}, psycopg.OperationalError("x")),
                                   make_url("postgresql+psycopg://owner@/postgres")).startswith(
        "Couldn't connect to PostgreSQL at the local socket as owner")


# --- the commands as a person runs them: fresh processes of the installed `erpclaw`, nothing patched -------------

@pytest.fixture
def model_server():
    """A stand-in for the local model server that answers only the readiness check (the model list)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"object": "list", "data": [{"id": "stand-in", "object": "model", "created": 0,
                                                           "owned_by": "test"}]}).encode()
            self.send_response(200 if self.path.rstrip("/") == "/v1/models" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Models)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def erpclaw(*args, server_url: str, home, model_url: str, typed: str = ""):
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("ERPCLAW_")}
    env.update({demo.SERVER_VAR: server_url, demo.HOME_VAR: str(home), "ERPCLAW_MODEL_BASE_URL": model_url,
                "ERPCLAW_MODEL": "stand-in"})
    exe = Path(sys.executable).parent / "erpclaw"
    p = subprocess.run([str(exe), "demo", *args], input=typed, env=env, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def test_up_then_chat_reach_the_prompt_as_fresh_processes(server, model_server, tmp_path):
    """What the in-process tests cannot see: what a fresh `erpclaw` imports, and in which order."""
    name, _ = server
    code, out = erpclaw("up", "--name", name, server_url=PG_URL, home=tmp_path / "demo-home", model_url=model_server)
    assert code == 0 and "Traceback" not in out and "Demo books ready" in out, out
    code, out = erpclaw("chat", "--name", name, server_url=PG_URL, home=tmp_path / "demo-home",
                        model_url=model_server, typed="quit\n")
    assert code == 0 and "Traceback" not in out and "Type 'quit' to leave." in out, out
    code, out = erpclaw("reset", "--down", "--name", name, server_url=PG_URL, home=tmp_path / "demo-home",
                        model_url=model_server)
    assert code == 0 and "Traceback" not in out, out


@pytest.mark.parametrize("command", ["up", "chat", "reset"])
def test_a_wrong_password_is_one_line_in_a_fresh_process(server, wrong_password, model_server, tmp_path, command):
    name, _ = server
    role, wrong, url = wrong_password
    code, out = erpclaw(command, "--name", name, server_url=url, home=tmp_path / "demo-home", model_url=model_server)
    assert code == 1 and "Traceback" not in out and wrong not in out, out
    assert not (make_url(PG_URL).password and make_url(PG_URL).password in out), out
    assert " ".join(out.split()) == login_refused(role), out
