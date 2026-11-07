"""The PostgreSQL runtime role: the owner role creates and
migrates the schema; the runtime role gets exactly each table's policy, so
privilege refuses TRUNCATE, UPDATE, DELETE and schema changes on the ledger
whatever the statement, and the triggers are the second layer.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from .db import READ, Runtime, RoleRefused, metadata

log = logging.getLogger(__name__)

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
VERSION_TABLE = "alembic_version"  # Alembic's, not on the metadata: read only


class GrantsRefused(RoleRefused):
    """The runtime role's privileges differ from the policy, or a table has none."""


def _policies(conn: Connection) -> dict[str, Runtime]:
    """Every table in the books' schema with its policy; a table without one refuses. The plugins are loaded
    first, so a plugin's tables have their policies even in a process that has not imported the plugin yet."""
    from . import models  # noqa: F401  (core's tables and their policies)
    from .plugins import load_plugins

    load_plugins()

    out: dict[str, Runtime] = {}
    for name in sorted(inspect(conn).get_table_names()):
        if name == VERSION_TABLE:
            out[name] = READ
            continue
        table = metadata.tables.get(name)
        policy = table.info.get("runtime") if table is not None else None
        if not isinstance(policy, Runtime):
            raise GrantsRefused(f"table {name} has no runtime policy, so no privileges can be granted on it. "
                                "Every table declares its policy (Table.info['runtime']).")
        out[name] = policy
    return out


def _quote(conn: Connection, *names: str) -> str:
    q = conn.dialect.identifier_preparer.quote
    return ".".join(q(n) for n in names)


def expected(policy: Runtime, column: str, privilege: str) -> bool:
    if privilege == "SELECT":
        return True
    if privilege == "INSERT":
        return policy.insert
    if privilege == "UPDATE":
        return column in policy.update or column in policy.lock
    return False  # REFERENCES


def apply_grants(conn: Connection, role: str) -> None:
    """As the owner, inside the command's transaction: revoke everything from
    PUBLIC and the runtime role on every table and the schema, grant exactly the
    policy, then verify the role's effective privileges."""
    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    who = f"PUBLIC, {_quote(conn, role)}"
    rq = _quote(conn, role)
    for name, policy in _policies(conn).items():
        t = _quote(conn, schema, name)
        conn.exec_driver_sql(f"REVOKE ALL ON TABLE {t} FROM {who}")
        grants = ["SELECT"] + (["INSERT"] if policy.insert else []) + (["DELETE"] if policy.delete else [])
        if policy.update or policy.lock:
            grants.append(f"UPDATE ({', '.join(_quote(conn, c) for c in policy.update + policy.lock)})")
        conn.exec_driver_sql(f"GRANT {', '.join(grants)} ON TABLE {t} TO {rq}")
    s = _quote(conn, schema)
    conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA {s} FROM {who}")
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA {s} TO {rq}")
    verify_grants(conn, role)


def privilege_differences(conn: Connection, role: str) -> list[str]:
    """The role's effective privileges, from every source (direct, PUBLIC, any
    role it belongs to), against the policy: one line per difference."""
    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    diffs: list[str] = []
    for name, policy in _policies(conn).items():
        t = _quote(conn, schema, name)
        held = dict(conn.exec_driver_sql(
            "SELECT p, has_table_privilege(%(r)s, %(t)s, p) FROM unnest(%(ps)s::text[]) AS p",
            {"r": role, "t": t, "ps": list(TABLE_PRIVILEGES)}).all())
        want = {"SELECT": True, "INSERT": policy.insert, "DELETE": policy.delete}
        for p in TABLE_PRIVILEGES:
            if held[p] != want.get(p, False):
                diffs.append(f"{name}: {'has' if held[p] else 'lacks'} {p}")
        cols = conn.exec_driver_sql(
            "SELECT a.attname, p, has_column_privilege(%(r)s, %(t)s, a.attname, p) "
            "FROM pg_attribute a CROSS JOIN unnest(%(ps)s::text[]) AS p "
            "WHERE a.attrelid = %(t)s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
            {"r": role, "t": t, "ps": list(COLUMN_PRIVILEGES)}).all()
        for col, p, has in cols:
            if has != expected(policy, col, p):
                diffs.append(f"{name}.{col}: {'has' if has else 'lacks'} {p}")
    s = _quote(conn, schema)
    for p, want in (("USAGE", True), ("CREATE", False)):
        has = conn.exec_driver_sql("SELECT has_schema_privilege(%(r)s, %(s)s, %(p)s)",
                                   {"r": role, "s": s, "p": p}).scalar()
        if has != want:
            diffs.append(f"schema {schema}: {'has' if has else 'lacks'} {p}")
    return diffs


def memberships(conn: Connection, role: str) -> list[str]:
    """The roles this role belongs to, inherited or not. Any membership is
    authority beyond the policy: a role whose privileges are not inherited can
    still be taken on with SET ROLE. So the
    runtime role belongs to no role, the predefined ones included."""
    return list(conn.exec_driver_sql(
        "SELECT r.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid "
        "WHERE a.member = (SELECT oid FROM pg_roles WHERE rolname = %(r)s) ORDER BY 1", {"r": role}).scalars())


def authority_beyond_policy(conn: Connection, role: str) -> list[str]:
    """Authority the runtime never needs and the table privileges do not show:
    owning any object (a function owner can drop the ledger's triggers with
    CASCADE), owning the database (an implicit member of pg_database_owner,
    which pg_auth_members does not list), CREATE on the database, and the role
    attributes that administer roles or replicate."""
    out = []
    owned = conn.exec_driver_sql(  # every object it owns, of any kind: the role owns nothing
        "SELECT count(*) FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass AND deptype = 'o' "
        "AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = %(r)s) "
        "AND dbid IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))", {"r": role}).scalar()
    if owned:
        out.append(f"owns {owned} object{'s' if owned > 1 else ''} (a table, function, type, schema or the like)")
    params = list(conn.exec_driver_sql(  # e.g. SET on session_replication_role turns triggers and foreign keys off
        "SELECT DISTINCT p.parname FROM pg_parameter_acl p, aclexplode(p.paracl) a "
        "WHERE a.grantee IN (0, (SELECT oid FROM pg_roles WHERE rolname = %(r)s)) ORDER BY 1", {"r": role}).scalars())
    if params:
        out.append(f"may set or change server parameters ({', '.join(params)})")
    owns_db, create_db = conn.exec_driver_sql(
        "SELECT d.datdba = r.oid, has_database_privilege(r.oid, d.oid, 'CREATE') FROM pg_database d, pg_roles r "
        "WHERE d.datname = current_database() AND r.rolname = %(r)s", {"r": role}).one()
    if owns_db:
        out.append("owns the database")
    if create_db:
        out.append("has CREATE on the database")
    createrole, replication = conn.exec_driver_sql(
        "SELECT rolcreaterole, rolreplication FROM pg_roles WHERE rolname = %(r)s", {"r": role}).one()
    if createrole:
        out.append("has CREATEROLE")
    if replication:
        out.append("has REPLICATION")
    return out


def verify_grants(conn: Connection, role: str) -> None:
    belongs = memberships(conn, role)
    if belongs:
        raise GrantsRefused(f"the runtime role {role} belongs to {', '.join(belongs)}; it must belong to no role, "
                            "since any membership can be taken on with SET ROLE. Revoke the membership.")
    beyond = authority_beyond_policy(conn, role)
    if beyond:
        raise GrantsRefused(f"the runtime role {role} {' and '.join(beyond)}; the runtime needs none of that. "
                            "Make another role the database's owner and remove the rest.")
    diffs = privilege_differences(conn, role)
    if diffs:
        raise GrantsRefused(
            f"the runtime role {role} does not hold exactly its policy: {'; '.join(diffs[:10])}"
            f"{f' (and {len(diffs) - 10} more)' if len(diffs) > 10 else ''}. Run `erpclaw grants` as the owner; "
            "privileges granted to PUBLIC or to roles it belongs to must be removed by their grantor.")


def validate_runtime_session(engine: Engine) -> str | None:
    """Before the runtime opens PostgreSQL books, on its own
    connection: the session is the runtime role itself, with no SET ROLE, not a
    superuser, no member of any owner, and holding exactly the policy. Returns a
    note to report when the development escape was used."""
    with engine.connect() as conn:
        db = conn.exec_driver_sql("SELECT current_database()").scalar()
        if os.environ.get("ERPCLAW_ALLOW_OWNER_RUNTIME") == "1":
            if not db.endswith("_test"):
                raise RoleRefused("ERPCLAW_ALLOW_OWNER_RUNTIME is only for databases named *_test.")
            note = f"ERPCLAW_ALLOW_OWNER_RUNTIME: the runtime role checks are off for {db} (development only)"
            log.warning(note)
            return note
        session, current, role_setting = conn.exec_driver_sql(
            "SELECT session_user, current_user, current_setting('role')").one()
        if session != current or role_setting != "none":
            raise RoleRefused("The runtime session must not have switched role (SET ROLE): "
                              "connect as the runtime role itself.")
        su, bypass = conn.exec_driver_sql(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = session_user").one()
        if su or bypass:
            raise RoleRefused(f"The runtime role {session} is a superuser or bypasses row security; "
                              "the runtime must run as a role without owner authority.")
        schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
        owners = {o for (o,) in conn.exec_driver_sql(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %(s)s AND c.relkind IN ('r', 'p') "
            "UNION SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %(s)s", {"s": schema})}
        for o in sorted(owners):
            if conn.exec_driver_sql("SELECT pg_has_role(session_user, %(o)s, 'MEMBER')", {"o": o}).scalar():
                raise RoleRefused(f"The runtime role {session} is, or is a member of, {o}, which owns these books' "
                                  "tables or schema; the runtime must hold no owner authority.")
        verify_grants(conn, session)
    return None
