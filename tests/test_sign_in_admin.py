"""Owner administration of sign-in codes and session history on PostgreSQL."""
import base64
import hashlib
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from erpclaw_core import administration as admin, models as m, principals as pr
from erpclaw_core.common import Refusal, now


def digest(value, domain="sign-in-code"):
    return hashlib.sha256(f"erpclaw:{domain}:v1\0".encode() + value.encode("utf-8")).hexdigest()


@pytest.fixture
def people(engine):
    with engine.begin() as conn:
        for name in ("alice", "bob"):
            admin.add_principal(conn, name, [], [])
        return dict(conn.execute(select(m.principal.c.name, m.principal.c.id)).all())


def add_code(conn, pid, label, *, expired=False, used=False):
    at = now() - timedelta(hours=1) if expired else now()
    row = dict(code_sha256=digest(label), principal_id=pid, created_at=at,
               expires_at=at + timedelta(minutes=10), used_at=at if used else None,
               created_by="owner", run_id=None)
    conn.execute(m.principal_sign_in.insert().values(**row))
    return row


def add_session(conn, pid, label, *, ended=False):
    at = now() - timedelta(minutes=2)
    row = dict(id=label, secret_sha256=digest(label + " secret", "session-secret"), principal_id=pid,
               created_at=at, last_seen_at=at + timedelta(minutes=1), idle_expires_at=at + timedelta(hours=12),
               expires_at=at + timedelta(days=7), ended_at=at if ended else None,
               ended_why="sign_out" if ended else None, user_agent=label + " browser")
    conn.execute(m.principal_session.insert().values(**row))
    return row


def audits(conn):
    """Administration's additions, separate from the demo fixture's business history."""
    return [dict(row) for row in conn.execute(select(m.audit_log).where(m.audit_log.c.actor != "seed")
                                            .order_by(m.audit_log.c.at, m.audit_log.c.id)).mappings()]


def assert_audit(row, name, pid, actor):
    assert row["actor"] == actor
    assert row["document_type"] == "principal" and row["document_id"] == pid
    assert json.loads(row["detail"])["principal"] == name


@pytest.mark.parametrize("helper", ["code_digest", "session_digest"])
def test_digests_use_separate_domains_and_document_hash_lookup(engine, helper):
    fn = getattr(pr, helper)
    for value in ("", "a sign-in value", "café 🔑"):
        assert fn(value) == digest(value, "sign-in-code" if helper == "code_digest" else "session-secret")
        assert pr.code_digest(value) != pr.session_digest(value)
        assert re.fullmatch(r"[0-9a-f]{64}", fn(value))
    assert "hash lookup" in fn.__doc__ and "timing oracle" in fn.__doc__


@pytest.mark.parametrize("run_id", [None, "a" * 32])
def test_mint_format_hash_storage_expiry_actor_and_prior_codes(engine, people, run_id):
    with engine.begin() as conn:
        before = now()
        codes = [admin.mint_sign_in_code(conn, "alice", "bookkeeper", run_id) for _ in range(2)]
        after = now()
        assert codes[0] != codes[1]
        for code in codes:
            assert re.fullmatch(r"[A-Z2-7]{26}", code)
            row = conn.execute(select(m.principal_sign_in).where(
                m.principal_sign_in.c.code_sha256 == digest(code))).mappings().one()
            assert row["principal_id"] == people["alice"]
            assert before <= row["created_at"] <= after
            assert row["expires_at"] - row["created_at"] == timedelta(minutes=10)
            assert row["used_at"] is None
            assert row["created_by"] == "bookkeeper" and row["run_id"] == run_id
            assert not conn.execute(text(
                "SELECT 1 FROM principal_sign_in s WHERE row_to_json(s)::text LIKE :value"),
                {"value": "%" + code + "%"}).first()
        rows = audits(conn)
        assert len(rows) == 2
        for row in rows:
            assert_audit(row, "alice", people["alice"], "bookkeeper")
        audit_text = conn.execute(text("SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a")).scalar_one()
        assert all(value not in audit_text for code in codes for value in (code, digest(code)))
        assert not re.search(r"\b[0-9a-f]{64}\b", audit_text)


def test_mint_draws_exactly_sixteen_secure_random_bytes(engine, people, monkeypatch):
    calls = []

    def random_bytes(size):
        calls.append(size)
        return bytes(range(size))

    monkeypatch.setattr(admin.secrets, "token_bytes", random_bytes)
    with engine.begin() as conn:
        code = admin.mint_sign_in_code(conn, "alice", "owner")
        assert calls == [16]
        assert code == base64.b32encode(bytes(range(16))).decode("ascii").rstrip("=")


@pytest.mark.parametrize("name,reason,words", [
    ("alice", "forbidden", "disabled"), ("missing", "no_principal", "no principal")])
def test_mint_refuses_disabled_or_unknown_without_writes(engine, people, name, reason, words):
    with engine.begin() as conn:
        conn.execute(m.principal.update().where(m.principal.c.id == people["alice"]).values(disabled=True))
        before = audits(conn)
        with pytest.raises(Refusal) as refused:
            admin.mint_sign_in_code(conn, name, "owner")
        assert refused.value.code == reason and words in refused.value.message.lower()
        assert not conn.execute(select(m.principal_sign_in)).first()
        assert audits(conn) == before


def test_cancel_uses_only_this_principals_unused_unexpired_codes_and_audits_changes(engine, people):
    with engine.begin() as conn:
        originals = [add_code(conn, people[who], label, **options) for who, label, options in [
            ("alice", "first", {}), ("alice", "second", {}),
            ("alice", "expired", {"expired": True}), ("alice", "used", {"used": True}),
            ("bob", "other", {})]]
        before = now()
        assert admin.cancel_sign_in_codes(conn, "alice", "operator") == 2
        after = now()
        rows = {r["code_sha256"]: dict(r) for r in conn.execute(select(m.principal_sign_in)).mappings()}
        for original in originals[:2]:
            row = rows[original["code_sha256"]]
            assert before <= row["used_at"] <= after
            assert dict(row, used_at=None) == original
        for original in originals[2:]:
            assert rows[original["code_sha256"]] == original
        logged = audits(conn)
        assert len(logged) == 1
        assert_audit(logged[0], "alice", people["alice"], "operator")
        assert admin.cancel_sign_in_codes(conn, "alice", "operator") == 0
        assert audits(conn) == logged


@pytest.mark.parametrize("why", ["sign_out", "expired", "disabled", "restored", "engine_start", "sign_out_all"])
def test_end_sessions_scopes_open_rows_preserves_history_and_audits_only_changes(engine, people, why):
    with engine.begin() as conn:
        original = {label: add_session(conn, people[who], label, ended=ended)
                    for who, label, ended in [("alice", "first", False), ("alice", "second", False),
                                               ("alice", "closed", True), ("bob", "other", False)]}
        before = now()
        assert admin.end_sessions(conn, people["alice"], why, "operator") == 2
        after = now()
        rows = {r["id"]: dict(r) for r in conn.execute(select(m.principal_session)).mappings()}
        for label in ("first", "second"):
            assert before <= rows[label]["ended_at"] <= after
            assert rows[label]["ended_why"] == why
            assert dict(rows[label], ended_at=None, ended_why=None) == original[label]
        assert rows["closed"] == original["closed"] and rows["other"] == original["other"]
        logged = audits(conn)
        assert len(logged) == 1
        assert_audit(logged[0], "alice", people["alice"], "operator")
        assert json.loads(logged[0]["detail"])["why"] == why
        assert admin.end_sessions(conn, people["alice"], why, "operator") == 0
        assert audits(conn) == logged


@pytest.mark.parametrize("why", ["unknown", "", None])
def test_invalid_end_reason_is_a_plain_refusal_before_any_write(engine, people, why):
    with engine.begin() as conn:
        original = add_session(conn, people["alice"], "first")
        with pytest.raises(Refusal, match="reason"):
            admin.end_sessions(conn, people["alice"], why, "operator")
        assert dict(conn.execute(select(m.principal_session)).mappings().one()) == original
        assert audits(conn) == []


def test_sign_out_uses_sign_out_all_and_leaks_no_authentication_values_to_audit(engine, people):
    with engine.begin() as conn:
        session = add_session(conn, people["alice"], "first")
        other = add_session(conn, people["bob"], "other")
        code = admin.mint_sign_in_code(conn, "alice", "operator")
        assert admin.sign_out_principal(conn, "alice", "operator") == 1
        assert admin.sign_out_principal(conn, "alice", "operator") == 0
        row = conn.execute(select(m.principal_session).where(m.principal_session.c.id == "first")).mappings().one()
        assert row["ended_why"] == "sign_out_all"
        assert dict(conn.execute(select(m.principal_session).where(
            m.principal_session.c.id == "other")).mappings().one()) == other
        assert admin.cancel_sign_in_codes(conn, "alice", "operator") == 1
        logged = audits(conn)
        assert len(logged) == 3
        for row in logged:
            assert_audit(row, "alice", people["alice"], "operator")
        audit_text = conn.execute(text("SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a")).scalar_one()
        for value in (code, digest(code), "first secret", session["secret_sha256"], other["secret_sha256"]):
            assert value not in audit_text
        assert not re.search(r"\b[0-9a-f]{64}\b", audit_text)


def test_list_sessions_returns_only_public_columns_of_open_sessions(engine, people):
    with engine.begin() as conn:
        first = add_session(conn, people["alice"], "first")
        other = add_session(conn, people["bob"], "other")
        add_session(conn, people["alice"], "closed", ended=True)
        expected = [{"principal": who, **{key: row[key] for key in
                    ("id", "created_at", "last_seen_at", "user_agent")}}
                    for who, row in [("alice", first), ("bob", other)]]
        assert sorted(admin.list_sessions(conn), key=lambda r: r["id"]) == expected
        assert admin.list_sessions(conn, "alice") == expected[:1]
        assert admin.list_sessions(conn, "owner") == []


def test_disable_ends_sessions_uses_all_unused_codes_and_rolls_back_atomically(engine, people):
    with engine.begin() as conn:
        sessions = [add_session(conn, people[who], label, ended=ended) for who, label, ended in
                    [("alice", "first", False), ("alice", "closed", True), ("bob", "other", False)]]
        codes = [add_code(conn, people[who], label, **options) for who, label, options in
                 [("alice", "live", {}), ("alice", "expired", {"expired": True}),
                  ("alice", "used", {"used": True}), ("bob", "other", {})]]
        principal = dict(conn.execute(select(m.principal).where(m.principal.c.id == people["alice"])).mappings().one())
    with engine.connect() as conn:
        transaction = conn.begin()
        before = now()
        assert admin.disable(conn, "alice") == principal["grants_version"] + 1
        row = conn.execute(select(m.principal_session).where(m.principal_session.c.id == "first")).mappings().one()
        assert row["ended_why"] == "disabled" and before <= row["ended_at"] <= now()
        rows = {r["code_sha256"]: dict(r) for r in conn.execute(select(m.principal_sign_in)).mappings()}
        for original in codes[:2]:
            assert before <= rows[original["code_sha256"]]["used_at"] <= now()
        assert rows[codes[2]["code_sha256"]] == codes[2]
        assert rows[codes[3]["code_sha256"]] == codes[3]
        logged = audits(conn)
        assert len(logged) == 2
        for row in logged:
            assert_audit(row, "alice", people["alice"], "owner")
        with pytest.raises(Refusal, match="disabled"):
            admin.mint_sign_in_code(conn, "alice", "owner")
        transaction.rollback()
    with engine.begin() as conn:
        assert dict(conn.execute(select(m.principal).where(m.principal.c.id == people["alice"])).mappings().one()) == principal
        assert {r["id"]: dict(r) for r in conn.execute(select(m.principal_session)).mappings()} == {r["id"]: r for r in sessions}
        assert {r["code_sha256"]: dict(r) for r in conn.execute(select(m.principal_sign_in)).mappings()} == {r["code_sha256"]: r for r in codes}
        assert audits(conn) == []
        admin.disable(conn, "alice")
    with engine.connect() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(select(m.principal_session)).mappings()}
        assert rows["first"]["ended_why"] == "disabled"
        assert rows["closed"] == sessions[1] and rows["other"] == sessions[2]


@pytest.mark.parametrize("operation", ["mint", "cancel", "end", "sign_out"])
def test_mutations_and_audits_belong_to_the_callers_transaction(engine, people, operation):
    with engine.begin() as conn:
        code = add_code(conn, people["alice"], "original")
        session = add_session(conn, people["alice"], "first")
    with engine.connect() as conn:
        transaction = conn.begin()
        if operation == "mint":
            assert admin.mint_sign_in_code(conn, "alice", "owner")
        elif operation == "cancel":
            assert admin.cancel_sign_in_codes(conn, "alice", "owner") == 1
        elif operation == "end":
            assert admin.end_sessions(conn, people["alice"], "sign_out", "owner") == 1
        else:
            assert admin.sign_out_principal(conn, "alice", "owner") == 1
        assert len(audits(conn)) == 1
        transaction.rollback()
    with engine.connect() as conn:
        assert dict(conn.execute(select(m.principal_sign_in)).mappings().one()) == code
        assert dict(conn.execute(select(m.principal_session)).mappings().one()) == session
        assert audits(conn) == []


def test_global_administration_works_with_multiple_companies_and_grants_keep_sessions(engine, people):
    with engine.begin() as conn:
        company = dict(conn.execute(select(m.company)).mappings().one())
        conn.execute(m.company.insert().values(**dict(company, id="second-company", name="Second company")))
        session = add_session(conn, people["alice"], "first")
        code = admin.mint_sign_in_code(conn, "alice", "operator")
        admin.grant(conn, "alice", ["view"], company["id"])
        admin.revoke(conn, "alice", ["view"], company["id"])
        assert dict(conn.execute(select(m.principal_session)).mappings().one()) == session
        assert conn.execute(select(m.principal_sign_in.c.used_at).where(
            m.principal_sign_in.c.code_sha256 == digest(code))).scalar_one() is None
        assert admin.cancel_sign_in_codes(conn, "alice", "operator") == 1
        assert admin.sign_out_principal(conn, "alice", "operator") == 1
        assert len(audits(conn)) == 3
