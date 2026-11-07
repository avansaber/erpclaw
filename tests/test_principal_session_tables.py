"""Browser authentication rows are hash-only history under both database roles."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from conftest import drop_everything
from erpclaw_core import models as m
from erpclaw_core.db import Runtime, metadata
from erpclaw_core.grants import GrantsRefused, apply_grants
from erpclaw_core.migrate import upgrade

AT = datetime(2026, 11, 1, tzinfo=timezone.utc)
CODE = hashlib.sha256(b"test sign in code").hexdigest()
SECRET = hashlib.sha256(b"test session secret").hexdigest()
COLUMNS = {
    "principal_sign_in": {"code_sha256": (64, False), "principal_id": (36, False),
                          "created_at": (None, False), "expires_at": (None, False),
                          "used_at": (None, True), "created_by": (100, False), "run_id": (32, True)},
    "principal_session": {"id": (36, False), "secret_sha256": (64, False), "principal_id": (36, False),
                          "created_at": (None, False), "last_seen_at": (None, False),
                          "idle_expires_at": (None, False), "expires_at": (None, False),
                          "ended_at": (None, True), "ended_why": (20, True), "user_agent": (200, True)},
}
ALLOWED = {"principal_sign_in": ("used_at",),
           "principal_session": ("last_seen_at", "idle_expires_at", "ended_at", "ended_why")}


def rows(pid):
    return {
        "principal_sign_in": dict(code_sha256=CODE, principal_id=pid, created_at=AT,
                                  expires_at=AT + timedelta(minutes=10), used_at=None,
                                  created_by="owner", run_id="a" * 32),
        "principal_session": dict(id="session", secret_sha256=SECRET, principal_id=pid, created_at=AT,
                                  last_seen_at=AT, idle_expires_at=AT + timedelta(hours=12),
                                  expires_at=AT + timedelta(days=7), ended_at=None, ended_why=None,
                                  user_agent="test browser"),
    }


@pytest.fixture
def auth_rows(engine):
    # Assert existence before referring to the models, so the baseline failure identifies the missing tables.
    with engine.begin() as conn:
        assert set(COLUMNS) <= set(inspect(conn).get_table_names())
        pid = conn.execute(select(m.principal.c.id).limit(1)).scalar_one()
        data = rows(pid)
        for name, row in data.items():
            conn.execute(metadata.tables[name].insert().values(**row))
    return data


def db_refusal(conn, statement, state):
    with conn.begin_nested():
        with pytest.raises(DBAPIError) as refused:
            with conn.begin_nested():
                conn.execute(statement)
        assert refused.value.orig.sqlstate == state
        return str(refused.value.orig)


def safe_refusal(conn, statement, state):
    message = db_refusal(conn, statement, state)
    assert CODE not in message and SECRET not in message
    return message


def test_migration_documents_native_keys_and_history_triggers(engine):
    migration = importlib.import_module("erpclaw_core.migrations.versions.0016_principal_sessions")
    assert "Primary and unique keys are ordinary, non-deferrable constraints." in migration.__doc__
    assert "Only used_at changes on a code" in migration.__doc__
    assert "only last_seen_at, idle_expires_at, ended_at and ended_why change on a session." in \
        " ".join(migration.__doc__.split())
    assert "DELETE and TRUNCATE are refused for both" in migration.__doc__
    with engine.connect() as conn:
        keys = conn.exec_driver_sql(
            "SELECT conname, condeferrable, condeferred FROM pg_constraint "
            "WHERE conrelid IN ('principal_sign_in'::regclass, 'principal_session'::regclass) "
            "AND contype IN ('p', 'u')").all()
        assert set(keys) == {
            ("pk_principal_sign_in", False, False), ("pk_principal_session", False, False),
            ("uq_principal_session_secret", False, False)}
        triggers = conn.exec_driver_sql(
            "SELECT c.relname, t.tgname, p.proname, t.tgtype, t.tgenabled, "
            "encode(t.tgargs, 'escape') FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid IN ('principal_sign_in'::regclass, 'principal_session'::regclass) "
            "AND NOT t.tgisinternal").all()
        expected = set()
        # pg_trigger flags: ROW=1, BEFORE=2, DELETE=8, UPDATE=16, TRUNCATE=32.
        for name, columns in ALLOWED.items():
            expected.add((name, f"{name}_columns", "erpclaw_only_columns", 19, "O",
                          "".join(column + r"\000" for column in columns)))
            expected.add((name, f"{name}_no_delete", "erpclaw_refuse_change", 11, "O", ""))
            expected.add((name, f"{name}_no_truncate", "erpclaw_refuse_change", 34, "O", ""))
        # the forward-only triggers: BEFORE UPDATE ROW, no arguments
        expected.add(("principal_sign_in", "principal_sign_in_stays_used", "erpclaw_sign_in_stays_used", 19, "O", ""))
        expected.add(("principal_session", "principal_session_stays_ended", "erpclaw_session_stays_ended", 19, "O", ""))
        assert set(triggers) == expected
        assert not conn.exec_driver_sql(
            "SELECT 1 FROM pg_proc WHERE pronamespace = current_schema()::regnamespace "
            "AND proname IN ('erpclaw_auth_row_checks', 'erpclaw_auth_keys')").first()


def test_schema_and_exact_runtime_policy(engine, runtime_login, auth_rows):
    for name, columns in COLUMNS.items():
        table = metadata.tables[name]
        assert set(table.c.keys()) == set(columns)
        for col, (length, nullable) in columns.items():
            assert table.c[col].nullable == nullable
            if length:
                assert table.c[col].type.length == length
            else:
                assert table.c[col].type.timezone
        assert {fk.target_fullname for fk in table.c.principal_id.foreign_keys} == {"principal.id"}
        assert table.info["owner"] == "core"
        assert table.info["runtime"] == Runtime(insert=name == "principal_session", update=ALLOWED[name])
    assert list(m.principal_sign_in.primary_key.columns.keys()) == ["code_sha256"]
    assert list(m.principal_session.primary_key.columns.keys()) == ["id"]
    with engine.connect() as conn:
        schema = inspect(conn)
        assert {c["name"] for c in schema.get_check_constraints("principal_sign_in")} == {
            "ck_principal_sign_in_expiry"}
        assert {c["name"] for c in schema.get_check_constraints("principal_session")} == {
            "ck_principal_session_end_reason", "ck_principal_session_end_pair", "ck_principal_session_idle"}
        assert {tuple(c["column_names"]) for c in schema.get_unique_constraints("principal_session")} == {
            ("secret_sha256",)}
        for name, primary in (("principal_sign_in", "code_sha256"), ("principal_session", "id")):
            assert schema.get_pk_constraint(name)["constrained_columns"] == [primary]
    assert make_url(runtime_login).password
    runtime = create_engine(runtime_login)
    try:
        with runtime.begin() as conn:
            # SELECT is acceptable for authentication because these tables store hashes, never bearer secrets.
            for name, data in auth_rows.items():
                assert dict(conn.execute(select(metadata.tables[name])).mappings().one()) == data
    finally:
        runtime.dispose()


@pytest.mark.parametrize("name,column", [(n, c) for n, cols in COLUMNS.items() for c in cols
                                         if c not in ALLOWED[n]])
@pytest.mark.parametrize("who", ["runtime", "owner"])
def test_each_immutable_column_is_refused(engine, runtime_login, auth_rows, name, column, who):
    table = metadata.tables[name]
    value = auth_rows[name][column]
    changed = value + timedelta(seconds=1) if isinstance(value, datetime) else (
        "b" * 64 if column.endswith("sha256") else "different")
    db = create_engine(runtime_login) if who == "runtime" else engine
    try:
        with db.begin() as conn:
            message = safe_refusal(conn, table.update().values({column: changed}),
                                   "42501" if who == "runtime" else "P0001")
            assert ("permission denied" if who == "runtime" else "only") in message
            if column.endswith("sha256"):
                assert changed not in message
    finally:
        if db is not engine:
            db.dispose()


@pytest.mark.parametrize("who", ["runtime", "owner"])
def test_allowed_updates_and_insert_authority(engine, runtime_login, auth_rows, who):
    db = create_engine(runtime_login) if who == "runtime" else engine
    try:
        with db.begin() as conn:
            t = m.principal_sign_in
            conn.execute(t.update().values(used_at=AT + timedelta(minutes=1)))
            assert conn.execute(select(t.c.used_at)).scalar_one() == AT + timedelta(minutes=1)
            code = dict(auth_rows[t.name], code_sha256="b" * 64)
            if who == "runtime":
                safe_refusal(conn, t.insert().values(**code), "42501")
            else:
                conn.execute(t.insert().values(**code))
            t = m.principal_session
            conn.execute(t.insert().values(**dict(auth_rows[t.name], id="second", secret_sha256="c" * 64)))
            conn.execute(t.update().values(last_seen_at=AT + timedelta(minutes=1),
                                          idle_expires_at=AT + timedelta(hours=13)))
            conn.execute(t.update().values(ended_at=AT, ended_why="sign_out"))
            for row in conn.execute(select(t)).mappings():
                assert row["last_seen_at"] == AT + timedelta(minutes=1)
                assert row["idle_expires_at"] == AT + timedelta(hours=13)
                assert row["ended_at"] == AT and row["ended_why"] == "sign_out"
    finally:
        if db is not engine:
            db.dispose()


@pytest.mark.parametrize("name", list(COLUMNS))
@pytest.mark.parametrize("operation", ["DELETE", "TRUNCATE"])
@pytest.mark.parametrize("who", ["runtime", "owner"])
def test_history_cannot_be_removed(engine, runtime_login, auth_rows, name, operation, who):
    db = create_engine(runtime_login) if who == "runtime" else engine
    try:
        with db.begin() as conn:
            from sqlalchemy import text
            safe_refusal(conn, text(f"{operation} {'FROM ' if operation == 'DELETE' else ''}{name}"),
                         "42501" if who == "runtime" else "P0001")
    finally:
        if db is not engine:
            db.dispose()


BAD_ROWS = [
    ("principal_sign_in", {"expires_at": AT - timedelta(seconds=1)}, "23514"),
    ("principal_sign_in", {"expires_at": AT}, "23514"),
    ("principal_session", {"ended_at": AT}, "23514"),
    ("principal_session", {"ended_why": "expired"}, "23514"),
    ("principal_session", {"ended_at": AT, "ended_why": "unknown"}, "23514"),
    ("principal_session", {"idle_expires_at": AT + timedelta(days=8)}, "23514"),
    ("principal_sign_in", {"principal_id": "missing"}, "23503"),
    ("principal_session", {"principal_id": "missing"}, "23503"),
]
BAD_ROWS += [(name, {column: None}, "23502") for name, columns in COLUMNS.items()
             for column, (_, nullable) in columns.items() if not nullable]


@pytest.mark.parametrize("name,changes,state", BAD_ROWS)
def test_checks_refuse_bad_rows(engine, auth_rows, name, changes, state):
    row = dict(auth_rows[name])
    # A new identity lets the intended constraint run without a duplicate-key conflict.
    row.update({"code_sha256": "d" * 64} if name == "principal_sign_in"
               else {"id": "bad", "secret_sha256": "e" * 64})
    row.update(changes)
    with engine.begin() as conn:
        db_refusal(conn, metadata.tables[name].insert().values(**row), state)


def test_session_end_reasons_and_idle_boundary(engine, auth_rows):
    with engine.begin() as conn:
        for number, reason in enumerate(("sign_out", "expired", "disabled", "restored", "engine_start", "sign_out_all")):
            conn.execute(m.principal_session.insert().values(**dict(
                auth_rows["principal_session"], id=f"reason{number}", secret_sha256=f"{number:064d}")))
            conn.execute(m.principal_session.update().where(m.principal_session.c.id == f"reason{number}").values(
                ended_at=AT, ended_why=reason, idle_expires_at=AT + timedelta(days=7)))


@pytest.mark.parametrize("name", list(COLUMNS))
def test_missing_policy_refuses_grants(engine, runtime_login, auth_rows, monkeypatch, name):
    with engine.begin() as conn:
        monkeypatch.delitem(metadata.tables[name].info, "runtime")
        with pytest.raises(GrantsRefused, match="no runtime policy"):
            apply_grants(conn, make_url(runtime_login).username)


def test_upgrade_preserves_existing_principals(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0015")
        conn.execute(m.principal.insert().values(id="existing", name="existing", kind="person", disabled=False,
                                                grants_version=1, token_sha256=None, created_at=AT))
    with engine.begin() as conn:
        upgrade(conn)
        assert set(COLUMNS) <= set(inspect(conn).get_table_names())
        assert conn.execute(select(m.principal.c.name)).scalar_one() == "existing"
        for name, row in rows("existing").items():
            conn.execute(metadata.tables[name].insert().values(**row))


@pytest.mark.parametrize("name", list(COLUMNS))
def test_keys_refuse_duplicates(engine, auth_rows, name):
    with engine.begin() as conn:
        row = dict(auth_rows[name])
        if name == "principal_session":
            row["id"] = "duplicate-secret"
        db_refusal(conn, metadata.tables[name].insert().values(**row), "23505")
        if name == "principal_session":
            row.update(id="session", secret_sha256="1" * 64)
            db_refusal(conn, metadata.tables[name].insert().values(**row), "23505")


def _reopening(conn):
    """The two statements a runtime login must never be able to commit."""
    from sqlalchemy import text
    return [
        (text("UPDATE principal_sign_in SET used_at = NULL WHERE used_at IS NOT NULL"), "a used code stays used"),
        (text("UPDATE principal_session SET ended_at = NULL, ended_why = NULL WHERE ended_why IS NOT NULL"),
         "an ended session stays ended"),
    ]


@pytest.mark.parametrize("who", ["runtime", "owner"])
def test_a_used_code_and_an_ended_session_cannot_be_reopened_or_changed(engine, runtime_login, auth_rows, who):
    t = m.principal_sign_in
    s = m.principal_session
    with engine.begin() as conn:
        conn.execute(t.update().values(used_at=AT + timedelta(minutes=1)))
        conn.execute(s.update().values(ended_at=AT + timedelta(minutes=2), ended_why="restored"))
    db = create_engine(runtime_login) if who == "runtime" else engine
    try:
        with db.begin() as conn:
            for statement, reason in _reopening(conn):
                assert reason in db_refusal(conn, statement, "P0001")
            # nor can the consumption or the end be rewritten to something else
            db_refusal(conn, t.update().values(used_at=AT + timedelta(minutes=9)), "P0001")
            db_refusal(conn, s.update().values(ended_at=AT + timedelta(minutes=9)), "P0001")
            db_refusal(conn, s.update().values(ended_why="sign_out"), "P0001")
            db_refusal(conn, s.update().values(ended_at=None, ended_why=None), "P0001")
        with engine.connect() as conn:  # nothing changed
            assert conn.execute(select(t.c.used_at)).scalar_one() == AT + timedelta(minutes=1)
            row = conn.execute(select(s.c.ended_at, s.c.ended_why)).one()
            assert (row.ended_at, row.ended_why) == (AT + timedelta(minutes=2), "restored")
    finally:
        if db is not engine:
            db.dispose()


@pytest.mark.parametrize("who", ["runtime", "owner"])
def test_ending_and_using_still_go_forward_and_repeating_is_harmless(engine, runtime_login, auth_rows, who):
    t = m.principal_sign_in
    s = m.principal_session
    db = create_engine(runtime_login) if who == "runtime" else engine
    try:
        with db.begin() as conn:
            conn.execute(t.update().where(t.c.used_at.is_(None)).values(used_at=AT + timedelta(minutes=1)))
            conn.execute(s.update().where(s.c.ended_at.is_(None)).values(ended_at=AT + timedelta(minutes=2), ended_why="sign_out"))
            # the same values written again (a retried sign-out) change nothing and are not refused
            conn.execute(t.update().values(used_at=AT + timedelta(minutes=1)))
            conn.execute(s.update().values(ended_at=AT + timedelta(minutes=2), ended_why="sign_out"))
            # activity columns of an ENDED session are not part of the irreversible markers
            conn.execute(s.update().values(last_seen_at=AT + timedelta(minutes=3)))
    finally:
        if db is not engine:
            db.dispose()
