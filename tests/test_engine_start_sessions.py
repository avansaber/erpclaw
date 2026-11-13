"""Starting an engine retires authentication history in one transaction only."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select

from bookdiff import changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_api.pipe import ListenerApp
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import administration as admin, models as m, principals as pr
from erpclaw_core.common import now

AUTH_TABLES = {"principal_session", "principal_sign_in"}
RUN = "a" * 32


@pytest.fixture
def access(engine, runtime_login):
    with engine.begin() as conn:
        for name in ("alice", "bob"):
            admin.add_principal(conn, name, [], [])
        people = dict(conn.execute(select(m.principal.c.name, m.principal.c.id)).all())
    runtime = create_engine(runtime_login)
    app = create_app(runtime, ScriptedModel([]), run_id=RUN)
    try:
        with TestClient(ListenerApp(app, "host", port=8740), base_url="http://127.0.0.1:8740") as client:
            yield runtime, client, people
    finally:
        runtime.dispose()


def add_session(conn, pid, label, *, expired=False, ended=False):
    at = now() - (timedelta(days=8) if expired else timedelta(0))
    conn.execute(m.principal_session.insert().values(
        id=label, principal_id=pid, secret_sha256=pr.session_digest(label),
        created_at=at, last_seen_at=at, idle_expires_at=at + timedelta(hours=12),
        expires_at=at + timedelta(days=7), ended_at=at if ended else None,
        ended_why="sign_out" if ended else None, user_agent=label))


def add_code(conn, pid, code, *, expired=False, used=False, run_id=None):
    at = now() - (timedelta(hours=1) if expired else timedelta(0))
    conn.execute(m.principal_sign_in.insert().values(
        code_sha256=pr.code_digest(code), principal_id=pid, created_at=at,
        expires_at=at + timedelta(minutes=10), used_at=at if used else None,
        created_by="owner", run_id=run_id))


def exchange(client, code):
    return client.post("/v1/session", json={"sign_in_code": code})


def me(client, secret):
    return client.get("/v1/me", headers={"Authorization": f"Session {secret}"})


def test_start_ends_all_open_sessions_voids_valid_codes_and_preserves_history(engine, access):
    runtime, client, people = access
    with engine.begin() as conn:
        for name in ("alice", "bob"):
            add_session(conn, people[name], name)
        add_session(conn, people["alice"], "expired", expired=True)
        add_session(conn, people["alice"], "closed", ended=True)
        add_code(conn, people["alice"], "A" * 26)
        add_code(conn, people["bob"], "B" * 26, run_id=RUN)
        add_code(conn, people["alice"], "C" * 26, used=True)
        add_code(conn, people["alice"], "D" * 26, expired=True)
    assert me(client, "alice").status_code == me(client, "bob").status_code == 200
    before = snapshot(engine)
    started = now()

    admin.end_all_sessions_at_start(runtime)

    finished = now()
    after = snapshot(engine)
    delta = changes(before, after)
    assert set(delta) == AUTH_TABLES  # includes audit and every business table in the comparison
    assert len(delta["principal_session"]["changed"]) == 3
    assert len(delta["principal_sign_in"]["changed"]) == 2
    for table in AUTH_TABLES:
        assert delta[table]["added"] == delta[table]["removed"] == []
    with engine.connect() as conn:
        sessions = {r["id"]: dict(r) for r in conn.execute(select(m.principal_session)).mappings()}
        codes = {r["code_sha256"]: dict(r) for r in conn.execute(select(m.principal_sign_in)).mappings()}
    at = sessions["alice"]["ended_at"]
    assert started <= at <= finished
    for label in ("alice", "bob", "expired"):
        assert sessions[label]["ended_at"] == at
        assert sessions[label]["ended_why"] == "engine_start"
    for code in ("A" * 26, "B" * 26):
        assert codes[pr.code_digest(code)]["used_at"] == at
    assert after["principal_session"][("closed",)] == before["principal_session"][("closed",)]
    for code in ("C" * 26, "D" * 26):
        key = (pr.code_digest(code),)
        assert after["principal_sign_in"][key] == before["principal_sign_in"][key]
    for table in AUTH_TABLES:
        allowed = {"ended_at", "ended_why"} if table == "principal_session" else {"used_at"}
        for old, new in delta[table]["changed"]:
            assert {k for k in old if old[k] != new[k]} == allowed
    for secret in ("alice", "bob"):
        assert me(client, secret).status_code == 401
    for code in ("A" * 26, "B" * 26, "C" * 26):
        response = exchange(client, code)
        assert response.status_code == 401
        assert response.json()["code"] == "sign_in_code_invalid"

    admin.end_all_sessions_at_start(runtime)
    assert snapshot(engine) == after

    # Sessions and codes created after startup remain usable.
    with engine.begin() as conn:
        code = admin.mint_sign_in_code(conn, "alice", "owner", RUN)
        add_session(conn, people["bob"], "after-start")
    assert me(client, "after-start").status_code == 200
    response = exchange(client, code)
    assert response.status_code == 200, response.text
    assert me(client, response.json()["session_secret"]).status_code == 200


def test_start_is_safe_and_repeatable_with_empty_authentication_tables(engine):
    before = snapshot(engine)
    assert before["principal_session"] == before["principal_sign_in"] == {}
    admin.end_all_sessions_at_start(engine)
    admin.end_all_sessions_at_start(engine)
    assert snapshot(engine) == before


def test_start_does_not_sweep_up_credentials_created_between_its_updates(engine, access):
    runtime, client, people = access
    with engine.begin() as conn:
        add_session(conn, people["alice"], "before-start")
        add_code(conn, people["alice"], "A" * 26)
    inserted = []

    def create_later_credentials(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if (not inserted and compiled is not None and compiled.statement.is_update
                and compiled.statement.table.name in AUTH_TABLES):
            with engine.begin() as other:
                add_session(other, people["bob"], "during-start")
                add_code(other, people["bob"], "B" * 26)
            inserted.append(True)

    event.listen(runtime, "after_cursor_execute", create_later_credentials)
    try:
        admin.end_all_sessions_at_start(runtime)
    finally:
        event.remove(runtime, "after_cursor_execute", create_later_credentials)
    assert inserted == [True]
    assert me(client, "before-start").status_code == 401
    assert exchange(client, "A" * 26).status_code == 401
    assert me(client, "during-start").status_code == 200
    assert exchange(client, "B" * 26).status_code == 200


def test_start_rolls_back_both_tables_if_either_update_fails(engine, access):
    runtime, _, people = access
    with engine.begin() as conn:
        add_session(conn, people["alice"], "session")
        add_code(conn, people["alice"], "A" * 26)
    before = snapshot(engine)
    connections = []

    def fail_second_update(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and compiled.statement.is_update and compiled.statement.table.name in AUTH_TABLES:
            connections.append(conn)
            assert conn.in_transaction()
            if len(connections) == 2:
                assert connections[0] is conn
                assert snapshot(engine) == before  # the first update is still uncommitted
                raise RuntimeError("startup update failed")

    event.listen(runtime, "before_cursor_execute", fail_second_update)
    try:
        with pytest.raises(RuntimeError, match="startup update failed"):
            admin.end_all_sessions_at_start(runtime)
    finally:
        event.remove(runtime, "before_cursor_execute", fail_second_update)
    assert len(connections) == 2
    assert snapshot(engine) == before
