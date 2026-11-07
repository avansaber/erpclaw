"""Restore invalidates authentication history before publishing the books."""
import hashlib
import json
import secrets
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from conftest import PG_URL
from erpclaw_core import backup as bk, models as m
from erpclaw_core.db import metadata
from test_principal_session_tables import AT, CODE, COLUMNS, auth_rows  # noqa: F401


@pytest.fixture
def target(runtime_login):
    name = f"restored_auth_{secrets.token_hex(4)}_test"
    result = bk.NewPostgresDatabase(PG_URL, name, make_url(runtime_login).username)
    yield result
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


@pytest.fixture
def saved(engine, auth_rows, runtime_login, tmp_path):
    with engine.begin() as conn:
        conn.execute(m.principal_sign_in.insert().values(**dict(auth_rows["principal_sign_in"],
                     code_sha256="f" * 64, used_at=AT + timedelta(minutes=2))))
        conn.execute(m.principal_session.insert().values(**dict(auth_rows["principal_session"],
                     id="closed", secret_sha256="a" * 64, ended_at=AT + timedelta(hours=1), ended_why="disabled")))
        conn.execute(m.principal_sign_in.insert().values(**dict(auth_rows["principal_sign_in"], code_sha256="3" * 64)))
        conn.execute(m.principal_session.insert().values(**dict(auth_rows["principal_session"],
                     id="another", secret_sha256="2" * 64)))
    path = tmp_path / "backup"
    runtime = create_engine(runtime_login)
    try:
        manifest = bk.backup(runtime, path)
        assert set(COLUMNS) <= set(manifest["tables"])
    finally:
        runtime.dispose()
    return path


def opened(target):
    return create_engine(make_url(target.server_url).set(database=target.name), poolclass=NullPool)


def assert_not_published(target):
    admin = create_engine(make_url(PG_URL).set(database="postgres"), poolclass=NullPool)
    try:
        with admin.connect() as conn:
            assert not conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(name)s",
                                            {"name": target.name}).first()
            assert not conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname LIKE 'erpclaw_restore_%%'").first()
    finally:
        admin.dispose()


def rewrite(path, name, rows, rehash=True):
    lines = [json.dumps(r, separators=(",", ":")) for r in rows]
    (path / "tables" / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    if rehash:
        manifest = json.loads((path / "MANIFEST.json").read_text())
        manifest["tables"][name].update(rows=len(lines), sha256=hashlib.sha256("\n".join(lines).encode()).hexdigest())
        (path / "MANIFEST.json").write_text(json.dumps(manifest))


def test_restore_invalidates_in_staging_and_preserves_closed_history(engine, saved, target, monkeypatch, tmp_path):
    original = bk._integrity
    observed = []

    def integrity(conn):
        # This is restore's uncommitted staging connection: invalidation must precede its integrity proof.
        assert conn.in_transaction()
        codes = conn.execute(select(m.principal_sign_in)).mappings().all()
        sessions = conn.execute(select(m.principal_session)).mappings().all()
        assert all(r["used_at"] is not None for r in codes)
        assert all(r["ended_at"] is not None for r in sessions)
        observed.append(True)
        original(conn)

    monkeypatch.setattr(bk, "_integrity", integrity)
    with engine.connect() as conn:
        before_restore = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    bk.restore(saved, target)
    assert observed == [True]
    db = opened(target)
    try:
        with db.connect() as conn:
            after_restore = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
            sessions = {r["id"]: r for r in conn.execute(select(m.principal_session)).mappings()}
            assert sessions["session"]["ended_why"] == "restored"
            assert before_restore <= sessions["session"]["ended_at"] <= after_restore
            assert sessions["another"]["ended_why"] == "restored"
            assert sessions["another"]["ended_at"] == sessions["session"]["ended_at"]
            assert sessions["closed"]["ended_why"] == "disabled"
            assert sessions["closed"]["ended_at"] == AT + timedelta(hours=1)
            used = dict(conn.execute(select(m.principal_sign_in.c.code_sha256, m.principal_sign_in.c.used_at)).all())
            assert used["f" * 64] == AT + timedelta(minutes=2)
            assert used[CODE] == used["3" * 64] == sessions["session"]["ended_at"]
        assert bk.verify(saved, target) == []
        assert bk.book_diff(engine, db) == []
        bk.backup(db, tmp_path / "again")
        assert bk.verify(tmp_path / "again", target) == []
    finally:
        db.dispose()


# Every column, and both row-count directions, must be covered by the shared predicate.
MUTATIONS = [(name, column) for name, columns in COLUMNS.items() for column in columns]
MUTATIONS += [(name, change) for name in COLUMNS for change in ("missing", "extra", "revived")]
MUTATIONS += [("principal_session", "wrong_reason")]


def tampered(conn, name, change):
    table = metadata.tables[name]
    conn.exec_driver_sql(f'ALTER TABLE "{name}" DISABLE TRIGGER USER')
    row = dict(conn.execute(select(table).limit(1)).mappings().one())
    if change == "missing":
        conn.execute(table.delete())
    elif change == "extra":
        row.update({"code_sha256": "0" * 64} if name == "principal_sign_in"
                   else {"id": "extra", "secret_sha256": "0" * 64})
        conn.execute(table.insert().values(**row))
    elif change in ("revived", "wrong_reason"):
        key = table.c.code_sha256 == CODE if name == "principal_sign_in" else table.c.id == "session"
        values = {"ended_why": "sign_out"} if change == "wrong_reason" else (
            {"used_at": None} if name == "principal_sign_in" else {"ended_at": None, "ended_why": None})
        conn.execute(table.update().where(key).values(**values))
    else:
        value = row[change]
        if change == "principal_id":
            conn.execute(m.principal.insert().values(id="other", name="other", kind="person", disabled=False,
                                                    grants_version=1, token_sha256=None, created_at=AT))
            value = "other"
        elif change in ("used_at", "ended_at", "ended_why"):
            # Change already-used/already-ended history, which restore must preserve exactly.
            value = "sign_out" if change == "ended_why" else AT + timedelta(days=1)
        elif hasattr(value, "tzinfo"):
            value += timedelta(seconds=1)
        else:
            value = "b" * 64 if change.endswith("sha256") else "changed"
        if change in ("used_at", "ended_at", "ended_why"):
            key = table.c.code_sha256 == "f" * 64 if name == "principal_sign_in" else table.c.id == "closed"
            conn.execute(table.update().where(key).values({change: value}))
        else:
            key = table.c.code_sha256 == row["code_sha256"] if name == "principal_sign_in" else table.c.id == row["id"]
            conn.execute(table.update().where(key).values({change: value}))
    conn.exec_driver_sql(f'ALTER TABLE "{name}" ENABLE TRIGGER USER')


@pytest.mark.parametrize("name,change", MUTATIONS)
def test_verify_rejects_every_other_change(engine, saved, target, name, change):
    bk.restore(saved, target)
    db = opened(target)
    try:
        with db.begin() as conn:
            tampered(conn, name, change)
        assert f"{name} differs from the backup" in bk.verify(saved, target)
        assert any(line.startswith(f"{name}:") for line in bk.book_diff(engine, db))
    finally:
        db.dispose()


@pytest.mark.parametrize("name,change", [(name, change) for name in COLUMNS
                                         for change in ("created_at", "missing", "extra", "revived")]
                         + [("principal_session", "wrong_reason")])
def test_restore_proof_rejects_changes_before_publication(saved, target, monkeypatch, name, change):
    def hook(step, stage=None, **kw):
        if step == "committed":
            db = create_engine(make_url(PG_URL).set(database=stage.name), poolclass=NullPool)
            try:
                with db.begin() as conn:
                    tampered(conn, name, change)
            finally:
                db.dispose()
    monkeypatch.setattr(bk, "_HOOK", hook)
    with pytest.raises(bk.BackupRefused, match=f"committed {name} differs"):
        bk.restore(saved, target)
    assert_not_published(target)


@pytest.mark.parametrize("name", list(COLUMNS))
@pytest.mark.parametrize("rehash", [True, False])
def test_tampered_backup_is_refused(saved, target, name, rehash):
    rows = [json.loads(line) for line in (saved / "tables" / f"{name}.jsonl").read_text().splitlines()]
    if name == "principal_sign_in":
        rows[0]["expires_at"] = rows[0]["created_at"]
    else:
        rows[0]["ended_at"], rows[0]["ended_why"] = None, "restored"
    rewrite(saved, name, rows, rehash)
    with pytest.raises(bk.BackupRefused, match="break the database's rules|damaged") as refused:
        bk.restore(saved, target)
    for row in rows:
        assert row.get("code_sha256", row.get("secret_sha256")) not in str(refused.value)
    assert_not_published(target)


def test_failure_before_commit_leaves_nothing_published(saved, target, monkeypatch):
    def fail(conn):
        assert not conn.execute(select(m.principal_session).where(m.principal_session.c.ended_at.is_(None))).first()
        assert not conn.execute(select(m.principal_sign_in).where(m.principal_sign_in.c.used_at.is_(None))).first()
        raise bk.BackupRefused("staging integrity refused")
    monkeypatch.setattr(bk, "_integrity", fail)
    with pytest.raises(bk.BackupRefused, match="staging integrity refused"):
        bk.restore(saved, target)
    assert_not_published(target)


def test_after_a_restore_the_runtime_login_cannot_reopen_a_consumed_code_or_a_restored_session(engine, saved, target, runtime_login):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    bk.restore(saved, target)
    runtime = create_engine(make_url(runtime_login).set(database=target.name), poolclass=NullPool)
    try:
        for statement in ("UPDATE principal_sign_in SET used_at = NULL WHERE used_at IS NOT NULL",
                          "UPDATE principal_session SET ended_at = NULL, ended_why = NULL WHERE ended_why = 'restored'"):
            with pytest.raises(DBAPIError) as refused:
                with runtime.begin() as conn:
                    conn.execute(text(statement))
            assert refused.value.orig.sqlstate == "P0001"
        with runtime.connect() as conn:  # still consumed and still ended
            assert conn.execute(select(m.principal_sign_in).where(m.principal_sign_in.c.used_at.is_(None))).first() is None
            assert conn.execute(select(m.principal_session).where(m.principal_session.c.ended_at.is_(None))).first() is None
    finally:
        runtime.dispose()
