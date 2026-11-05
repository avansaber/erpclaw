"""A demo to try the engine: `erpclaw demo up`, `chat` and `reset`.

The demo books are a database of their own on the PostgreSQL server named by ERPCLAW_DEMO_PG_URL (a role that can
create databases and roles), with a runtime login made for them, and a folder of their own for the chat's local
files. Before anything is written or dropped, every command checks that its target is the demo the engine made and
nothing else: the name, the books the engine's own URLs name, both of the engine's marks in the database, a login
that nothing outside the demo depends on, and the folder's marker. A check that fails stops the command with
nothing changed.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

import typer
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

from erpclaw_core.db import DEMO_MARK, OWNER_MARK, OWNER_TABLE, RoleRefused, require_postgresql, safe_url

SERVER_VAR = "ERPCLAW_DEMO_PG_URL"
HOME_VAR = "ERPCLAW_DEMO_HOME"
DEFAULT = "erpclaw_demo"
# at most 60 bytes, so the login's name (the database's and "_rt") is never truncated at PostgreSQL's 63
NAME = re.compile(r"[a-z][a-z0-9_]{0,54}_demo")
MARKER = "DEMO.json"
COMPANY = "Stark Manufacturing"

demo_app = typer.Typer(help="Try ERPClaw on demo books of their own: up, chat, reset")


class DemoRefused(RuntimeError):
    """The demo's target is not the demo the engine made; nothing was changed."""


def login_of(name: str) -> str:
    return f"{name}_rt"


def login_comment(name: str) -> str:
    return f"erpclaw-demo-login:{name}"


def folder_of(name: str) -> Path:
    """The demo's own folder: ERPCLAW_DEMO_HOME/<database>, by default ~/.erpclaw-demo/<database>."""
    home = os.environ.get(HOME_VAR)
    return (Path(home) if home else Path.home() / ".erpclaw-demo") / name


def _password() -> str:
    """A fresh password, held by this process only (hex, so it is safe inside the statement that sets it)."""
    return secrets.token_hex(16)


def _model():
    """The local model at the settings the qualification runs use (the OpenAI-compatible route)."""
    from erpclaw_assistant.muse import LocalModel

    return LocalModel()


# --- the checks ---------------------------------------------------------------------------------------------

def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise DemoRefused(f"{name!r} is not a demo name: a lowercase name ending in _demo, at most 60 characters "
                          "(letters, digits and underscores).")


def server() -> URL:
    """The demo's server URL, carrying transport options only."""
    from erpclaw_cli.evals.run import SAFE_OPTIONS

    raw = os.environ.get(SERVER_VAR)
    if not raw:
        raise DemoRefused(f"Set {SERVER_VAR} to your local PostgreSQL server's URL, as a role that can create "
                          "databases and roles (for the Docker server, its superuser).")
    try:
        url = make_url(require_postgresql(raw))
    except RoleRefused as e:
        raise DemoRefused(str(e)) from None
    unsafe = sorted(set(url.query) - SAFE_OPTIONS)
    if unsafe:
        raise DemoRefused(f"{SERVER_VAR} may carry only {', '.join(sorted(SAFE_OPTIONS))}; "
                          f"remove {', '.join(unsafe)}.")
    return url


def check_not_your_books(srv: URL, name: str) -> None:
    """The demo never touches the books the engine's own URLs name, nor the server URL's own database."""
    from erpclaw_cli.evals.run import SAFE_OPTIONS

    if not srv.database:
        raise DemoRefused(f"{SERVER_VAR} must name a database of the server (for example its postgres database); "
                          "nothing was changed.")
    if srv.database == name:
        raise DemoRefused(f"{SERVER_VAR} names {name} itself; point it at the server's own database.")
    for var in ("ERPCLAW_DATABASE_URL", "ERPCLAW_MIGRATION_URL"):
        raw = os.environ.get(var)
        if not raw:
            continue
        try:
            url = make_url(raw)
        except Exception:  # noqa: BLE001  (a URL the demo cannot read is refused, never guessed at)
            raise DemoRefused(f"{var} cannot be read; the demo will not run beside books it cannot rule out.")
        extra = sorted(set(url.query) - SAFE_OPTIONS)
        if extra:  # dbname, service, options, host ... can name other books than the URL's path does
            raise DemoRefused(f"{var} carries {', '.join(extra)}, so the demo cannot rule out which books it names; "
                              "nothing was changed.")
        if not url.database:  # PostgreSQL would choose it from PGDATABASE, PGUSER, a service file ...: never guessed
            raise DemoRefused(f"{var} names no database, so the demo cannot rule out which books it opens; "
                              "nothing was changed.")
        if url.database == name:
            raise DemoRefused(f"{name} is your books' database ({var}); the demo never touches it.")


# A refusal in the demo's own words, by its SQLSTATE (the code, else its class), never PostgreSQL's text.
REFUSED = {
    "42501": "the server role lacks a privilege the demo needs (it must create databases and roles)",
    "42710": "a role of that name exists", "42P04": "a database of that name exists",
    "08": "the connection was lost", "28": "the login was refused", "3D": "the database does not exist",
    "53": "the server is out of resources", "57": "the server stopped or cancelled the statement",
}


def why_refused(orig: BaseException | None) -> str:
    code = str(getattr(orig, "sqlstate", None) or "")
    if not re.fullmatch(r"[0-9A-Z]{5}", code):
        return "a refusal without a code"
    return f"{REFUSED.get(code) or REFUSED.get(code[:2]) or 'a refusal the demo does not name'} (SQLSTATE {code})"


def _secret(adm: Engine, sql: str) -> None:
    """Run a statement that carries a password; PostgreSQL's refusal is reported without the statement, its
    traceback, its locals or its text."""
    from sqlalchemy.exc import DBAPIError

    try:
        with adm.connect() as c:
            c.exec_driver_sql(sql)
    except DBAPIError as e:
        raise DemoRefused(f"PostgreSQL refused the demo's login: {why_refused(e.orig)}.") from None


def where(url: URL) -> str:
    return f"{url.host}:{url.port or 5432}" if url.host else "the local socket"


def connection_refusal(e: Exception, url: URL, login: str | None = None) -> str:
    """One line for a failure to reach or use the server at url: its host, port, user and database only, never the
    password, the URL or PostgreSQL's own text (sorted by that text, as a connection failure carries no code)."""
    from sqlalchemy.exc import OperationalError

    orig = getattr(e, "orig", None) or e
    text = str(orig)
    at = where(url)
    if isinstance(e, OperationalError) or type(orig).__name__ == "OperationalError":
        if any(t in text for t in ("authentication failed", "no password supplied", "pg_hba.conf")):
            if login:
                return (f"Couldn't log in to PostgreSQL at {at} as {login}, the demo's login, just after setting its "
                        "password: check that this is the server you meant (another PostgreSQL may be using "
                        "that port).")
            return (f"Couldn't log in to PostgreSQL at {at} as {url.username}. Check the password in {SERVER_VAR}, "
                    "and that this is the server you meant (another PostgreSQL may be using that port).")
        if "failed to resolve host" in text or "could not translate host name" in text:
            return f"There is no host {url.host} to reach; check {SERVER_VAR}."
        if "does not exist" in text and "database" in text:
            return (f"PostgreSQL at {at} has no database {url.database}: point {SERVER_VAR} at one of its "
                    "databases (for example postgres), on the server you meant.")
        if any(t in text for t in ("Connection refused", "No such file or directory", "timeout expired",
                                   "could not receive data", "server closed the connection", "Connection reset")):
            return (f"No PostgreSQL is answering at {at}. Start the demo's server (the README's \"Try it\"), or set "
                    f"{SERVER_VAR} to the one you meant.")
        return f"Couldn't connect to PostgreSQL at {at} as {url.username}; check {SERVER_VAR}."
    return f"PostgreSQL at {at} refused the demo: {why_refused(orig)}."


def admin(srv: URL) -> Engine:
    return create_engine(srv, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def database_oid(adm: Engine, name: str) -> int | None:
    with adm.connect() as c:
        return c.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = %(n)s", {"n": name}).scalar()


def check_marks(srv: URL, name: str) -> None:
    """The database is the one the server says it is, and holds both the engine's ownership mark and its demo
    mark; read in a read-only transaction."""
    eng = create_engine(srv.set(database=name), poolclass=NullPool)
    try:
        with eng.connect() as c:
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            got = c.exec_driver_sql("SELECT current_database()").scalar()
            if got != name:
                raise DemoRefused(f"connected to {got}, not {name}; nothing was changed.")
            marks = set()
            if c.exec_driver_sql("SELECT to_regclass(%(t)s)", {"t": f"public.{OWNER_TABLE}"}).scalar():
                marks = {m for (m,) in c.exec_driver_sql(f"SELECT mark FROM public.{OWNER_TABLE}")}
            c.rollback()
    finally:
        eng.dispose()
    if not {OWNER_MARK, DEMO_MARK} <= marks:
        raise DemoRefused(f"{name} is not a demo the engine made; nothing was changed.")


def check_login(adm: Engine, name: str, db_oid: int | None) -> bool:
    """Whether the demo's login exists; when it does, that it is the demo's alone: its comment names this demo, it
    holds no special attribute, belongs to no role and has no member, and nothing outside the demo database depends
    on it (in pg_shdepend, only rows inside the demo database and its CONNECT on it)."""
    login = login_of(name)
    with adm.connect() as c:
        row = c.exec_driver_sql(
            "SELECT oid, shobj_description(oid, 'pg_authid'), rolsuper OR rolcreatedb OR rolcreaterole "
            "OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname = %(r)s", {"r": login}).first()
        if row is None:
            return False
        oid, comment, special = row
        used = special or comment != login_comment(name)
        # the server's own role may administer the login it created (PostgreSQL 16 grants that to a creator
        # that is not a superuser); any other membership, either way, is use outside the demo
        used = used or bool(c.exec_driver_sql(
            "SELECT 1 FROM pg_auth_members WHERE (roleid = %(o)s AND member <> "
            "(SELECT oid FROM pg_roles WHERE rolname = current_user)) OR member = %(o)s LIMIT 1",
            {"o": oid}).first())
        used = used or bool(c.exec_driver_sql(
            "SELECT 1 FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass AND refobjid = %(o)s "
            "AND NOT (dbid = %(d)s AND %(d)s <> 0) "
            "AND NOT (dbid = 0 AND classid = 'pg_database'::regclass AND objid = %(d)s AND deptype = 'a') LIMIT 1",
            {"o": oid, "d": db_oid or 0}).first())
    if used:
        raise DemoRefused(f"the demo's login {login} is used outside the demo, or was not made by it; "
                          "nothing was changed.")
    return True


def check_folder(name: str) -> bool:
    """Whether the demo's folder exists; when it does, that it is the one the demo made: a real directory, not a
    link, whose marker names this demo."""
    path = folder_of(name)
    if not os.path.lexists(path):
        return False
    try:
        ok = path.is_dir() and not path.is_symlink() and \
            json.loads((path / MARKER).read_text()).get("database") == name
    except (OSError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise DemoRefused(f"{path} is not the demo's own folder; it was left alone, and nothing was changed.")
    return True


# --- the commands -------------------------------------------------------------------------------------------

def _drop(adm: Engine, name: str, db: bool, login: bool) -> None:
    with adm.connect() as c:
        if db:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if login:
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{login_of(name)}"')


def up(name: str) -> None:
    from erpclaw_core.db import make_engine
    from erpclaw_core.grants import apply_grants
    from erpclaw_sales.demo import seed_demo

    check_name(name)
    srv = server()
    check_not_your_books(srv, name)
    adm = admin(srv)
    try:
        if database_oid(adm, name) is not None:
            raise DemoRefused(f"The demo {name} is already up: `erpclaw demo chat`, or `erpclaw demo reset` to "
                              "start again.")
        with adm.connect() as c:
            if c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %(r)s", {"r": login_of(name)}).first():
                raise DemoRefused(f"A login named {login_of(name)} exists; the demo makes its own. Nothing was "
                                  "changed.")
        if check_folder(name):
            raise DemoRefused(f"{folder_of(name)} is left from an earlier demo; `erpclaw demo reset --down` "
                              "removes it.")
        made_login = made_db = made_folder = False
        try:
            _secret(adm, f'CREATE ROLE "{login_of(name)}" LOGIN PASSWORD \'{_password()}\'')
            made_login = True
            with adm.connect() as c:
                c.exec_driver_sql(f"COMMENT ON ROLE \"{login_of(name)}\" IS '{login_comment(name)}'")
                c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
                made_db = True
            eng = make_engine(srv.set(database=name).render_as_string(hide_password=False))
            try:
                seed_demo(eng)
                with eng.begin() as conn:
                    apply_grants(conn, login_of(name))
            finally:
                eng.dispose()
            folder = folder_of(name)
            folder.mkdir(parents=True, mode=0o700)
            made_folder = True
            (folder / MARKER).write_text(json.dumps({"database": name, "login": login_of(name)}) + "\n")
        except BaseException:
            _drop(adm, name, made_db, made_login)  # only what this command made
            if made_folder:
                shutil.rmtree(folder_of(name))
            raise
    finally:
        adm.dispose()


def reset(name: str, down: bool) -> None:
    check_name(name)
    srv = server()
    check_not_your_books(srv, name)
    adm = admin(srv)
    try:
        oid = database_oid(adm, name)
        if oid is not None:
            check_marks(srv, name)
        login = check_login(adm, name, oid)
        folder = check_folder(name)
        _drop(adm, name, oid is not None, login)  # every check above passed before anything is dropped
        if folder:
            shutil.rmtree(folder_of(name))
    finally:
        adm.dispose()
    if not down:
        up(name)


def open_chat(name: str):
    """The demo books opened for chat as the demo's login, with this process's files in the demo's folder: the
    API client, or a refusal."""
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import DBAPIError

    from erpclaw_api.app import create_app
    from erpclaw_core.db import make_engine
    from erpclaw_core.grants import validate_runtime_session

    check_name(name)
    srv = server()
    check_not_your_books(srv, name)
    adm = admin(srv)
    try:
        oid = database_oid(adm, name)
        if oid is None:
            raise DemoRefused(f"There is no demo {name}: `erpclaw demo up` makes it.")
        check_marks(srv, name)
        if not check_login(adm, name, oid):
            raise DemoRefused(f"The demo's login {login_of(name)} is missing: `erpclaw demo reset` makes the "
                              "demo again.")
        if not check_folder(name):
            raise DemoRefused(f"The demo's folder {folder_of(name)} is missing: `erpclaw demo reset` makes the "
                              "demo again.")
        model = _model()
        problem = model.check_ready()
        if problem:
            raise DemoRefused(problem)
        password = _password()
        _secret(adm, f'ALTER ROLE "{login_of(name)}" PASSWORD \'{password}\'')
    finally:
        adm.dispose()
    # From here this process reads and writes files only in the demo's folder, and cannot reach your books' URLs.
    os.environ["ERPCLAW_HOME"] = str(folder_of(name))
    os.environ["ERPCLAW_MAIL_DIR"] = str(folder_of(name) / "mail")
    for var in ("ERPCLAW_DATABASE_URL", "ERPCLAW_MIGRATION_URL", "ERPCLAW_ALLOW_OWNER_RUNTIME"):
        os.environ.pop(var, None)
    url = srv.set(database=name, username=login_of(name), password=password)
    eng = None
    try:
        eng = make_engine(url.render_as_string(hide_password=False))
        with eng.connect() as c:
            got = tuple(c.exec_driver_sql("SELECT current_database(), current_user").one())
        if got != (name, login_of(name)):
            raise DemoRefused(f"connected to {got[0]} as {got[1]}, not the demo; nothing was changed.")
        validate_runtime_session(eng)
    except (DemoRefused, RoleRefused, DBAPIError) as e:
        if eng is not None:
            eng.dispose()
        if isinstance(e, DBAPIError):
            raise DemoRefused(connection_refusal(e, url, login=login_of(name))) from None
        raise
    return TestClient(create_app(eng, model, embedded_owner=True)), model


def _run(fn):
    """A command's work, every refusal and every PostgreSQL failure as one line (never a traceback, a password or
    a whole URL), exit 1."""
    import psycopg
    from sqlalchemy.exc import DBAPIError

    from erpclaw_cli.main import con

    try:
        return fn()
    except (DemoRefused, RoleRefused) as e:
        why = str(e)
    except (DBAPIError, psycopg.Error) as e:
        why = connection_refusal(e, server())
    con.print(why, style="red", markup=False, highlight=False)
    raise typer.Exit(1)


NAME_OPTION = typer.Option(DEFAULT, "--name", help="The demo database: a lowercase name ending in _demo")


@demo_app.command("up")
def up_cmd(name: str = NAME_OPTION):
    """Create the demo books on your local PostgreSQL server, with their own login and folder."""
    from erpclaw_cli.main import con

    _run(lambda: up(name))
    con.print(f"[green]Demo books ready:[/green] {safe_url(server().set(database=name).render_as_string())}, "
              f"the demo company {COMPANY}.")
    con.print("next: `erpclaw demo chat`")


@demo_app.command("chat")
def chat_cmd(name: str = NAME_OPTION):
    """Chat with the demo books, on the local model."""
    from erpclaw_cli.main import chat_loop, con

    c, model = _run(lambda: open_chat(name))
    d = model.describe()
    con.print(f"[dim]Demo books {name} ({COMPANY}), model {d['model']} at {d['base_url']}.[/dim]")
    _run(lambda: chat_loop(c))


@demo_app.command("reset")
def reset_cmd(name: str = NAME_OPTION,
              down: bool = typer.Option(False, "--down", help="Remove the demo without making it again")):
    """Remove the demo books, their login and folder, and make them again (or not, with --down)."""
    from erpclaw_cli.main import con

    _run(lambda: reset(name, down))
    if down:
        con.print(f"[green]The demo {name} is removed.[/green]")
    else:
        con.print(f"[green]The demo {name} is fresh.[/green] next: `erpclaw demo chat`")
