"""The owner's code and session commands against the real books."""
import re

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from conftest import cli_urls
from erpclaw_cli import main
from erpclaw_core import administration as admin, models as m
from test_sign_in_admin import add_code, add_session, assert_audit, audits, digest


@pytest.fixture
def cli(engine, runtime_login, monkeypatch):
    cli_urls(monkeypatch, engine, runtime_login)
    monkeypatch.setattr(main, "con", main.Console(width=240))
    with engine.begin() as conn:
        for name in ("alice", "bob"):
            admin.add_principal(conn, name, [], [])
    return lambda *args: CliRunner().invoke(main.app, ["users", *args])


def test_sign_in_prints_code_once_with_lifetime_and_audits_owner_without_code(engine, cli):
    out = cli("sign-in", "alice")
    assert out.exit_code == 0, out.output
    codes = re.findall(r"\b[A-Z2-7]{26}\b", out.output)
    assert len(codes) == 1 and out.output.count(codes[0]) == 1
    assert "10 minutes" in out.output and "once" in out.output
    with engine.connect() as conn:
        row = conn.execute(select(m.principal_sign_in)).mappings().one()
        assert row["code_sha256"] == digest(codes[0]) and row["created_by"] == "owner"
        logged = audits(conn)
        assert len(logged) == 1
        assert_audit(logged[0], "alice", row["principal_id"], "owner")
        audit_text = conn.execute(text("SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a")).scalar_one()
        assert codes[0] not in audit_text and digest(codes[0]) not in audit_text
        assert not re.search(r"\b[0-9a-f]{64}\b", audit_text)


@pytest.mark.parametrize("name,words", [("alice", "disabled"), ("missing", "no principal")])
def test_sign_in_refuses_disabled_and_unknown_in_plain_words(engine, cli, name, words):
    with engine.begin() as conn:
        admin.disable(conn, "alice")
    out = cli("sign-in", name)
    assert out.exit_code == 1 and words in out.output.lower(), out.output
    assert "Traceback" not in out.output and not re.search(r"\b[A-Z2-7]{26}\b", out.output)
    with engine.connect() as conn:
        assert not conn.execute(select(m.principal_sign_in)).first()


def test_sign_in_cancel_reports_count_preserves_others_and_audits_owner(engine, cli):
    with engine.begin() as conn:
        people = dict(conn.execute(select(m.principal.c.name, m.principal.c.id)).all())
        add_code(conn, people["alice"], "first")
        add_code(conn, people["alice"], "second")
        other = add_code(conn, people["bob"], "other")
    for count in (2, 0):
        out = cli("sign-in", "alice", "--cancel")
        assert out.exit_code == 0 and str(count) in out.output and "cancel" in out.output.lower(), out.output
        assert not re.search(r"\b[A-Z2-7]{26}\b", out.output)
    with engine.connect() as conn:
        rows = list(conn.execute(select(m.principal_sign_in).where(
            m.principal_sign_in.c.principal_id == people["alice"])).mappings())
        assert len(rows) == 2 and all(row["used_at"] for row in rows)
        assert dict(conn.execute(select(m.principal_sign_in).where(
            m.principal_sign_in.c.principal_id == people["bob"])).mappings().one()) == other
        logged = audits(conn)
        assert len(logged) == 1
        assert_audit(logged[0], "alice", people["alice"], "owner")


def test_sign_out_reports_count_ends_only_target_and_audits_owner(engine, cli):
    with engine.begin() as conn:
        people = dict(conn.execute(select(m.principal.c.name, m.principal.c.id)).all())
        add_session(conn, people["alice"], "first")
        add_session(conn, people["alice"], "second")
        other = add_session(conn, people["bob"], "other")
    for count in (2, 0):
        out = cli("sign-out", "alice")
        assert out.exit_code == 0 and str(count) in out.output and "session" in out.output.lower(), out.output
    with engine.connect() as conn:
        rows = list(conn.execute(select(m.principal_session).where(
            m.principal_session.c.principal_id == people["alice"])).mappings())
        assert len(rows) == 2 and all(row["ended_at"] and row["ended_why"] == "sign_out_all" for row in rows)
        assert dict(conn.execute(select(m.principal_session).where(
            m.principal_session.c.id == "other")).mappings().one()) == other
        logged = audits(conn)
        assert len(logged) == 1
        assert_audit(logged[0], "alice", people["alice"], "owner")
        assert "first secret" not in str(logged) and digest("first secret") not in str(logged)


@pytest.mark.parametrize("name", [None, "alice"])
def test_sessions_lists_open_public_details_without_hashes_or_secrets(engine, cli, name):
    with engine.begin() as conn:
        people = dict(conn.execute(select(m.principal.c.name, m.principal.c.id)).all())
        rows = [add_session(conn, people[who], label, ended=ended) for who, label, ended in
                [("alice", "first", False), ("alice", "closed", True), ("bob", "other", False)]]
    out = cli("sessions", *([name] if name else []))
    assert out.exit_code == 0, out.output
    assert "alice" in out.output and "first browser" in out.output and "closed browser" not in out.output
    assert ("bob" in out.output and "other browser" in out.output) == (name is None)
    assert "created" in out.output.lower() and "last seen" in out.output.lower()
    assert "user agent" in out.output.lower()
    assert rows[0]["created_at"].isoformat() in out.output
    assert rows[0]["last_seen_at"].isoformat() in out.output
    for row in rows:
        assert row["secret_sha256"] not in out.output and row["id"] + " secret" not in out.output
    assert "sha256" not in out.output and "hash" not in out.output.lower()
    assert not re.search(r"\b[0-9a-f]{64}\b", out.output)
    with engine.connect() as conn:
        assert {row["id"]: dict(row) for row in conn.execute(select(m.principal_session)).mappings()} == {row["id"]: row for row in rows}
        assert audits(conn) == []
