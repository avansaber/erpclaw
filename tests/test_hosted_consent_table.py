"""Hosted consent belongs to PostgreSQL books and is read without changing them."""
from datetime import datetime, timezone
import secrets
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from conftest import PG_URL, drop_everything
from erpclaw_core import backup as bk, backup_reflect, migrate, models as m, restore_upgrade
from erpclaw_core.db import OWNER_MARK, READ, books_identity, owner
from erpclaw_core.grants import TABLE_PRIVILEGES, privilege_differences
from erpclaw_core.hosted_consent import live_consent
from erpclaw_core.plugins import REGISTRY, install_into

AT = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
COLUMNS = {
    "company_id": 36, "profile_name": 100, "provider": 60, "model_id": 200,
    "endpoint_host": 253, "route_digest": 64, "route_version": 40,
    "retention_version": 40, "wording_version": 40, "consented_at": None,
    "consented_by": 100, "status": 10, "approval_id": 36, "operation_key": 36,
}


def consent(company_id, key="consent", status="live"):
    return dict(company_id=company_id, profile_name="hosted", provider="provider",
                model_id="model", endpoint_host="models.example.invalid", route_digest="a" * 64,
                route_version="1", retention_version="1", wording_version="1", consented_at=AT,
                consented_by="owner", status=status, approval_id=f"approval-{key}", operation_key=key)


@pytest.fixture
def company_id(engine):
    with engine.connect() as conn:
        return conn.execute(select(m.company.c.id)).scalars().first()


@pytest.fixture
def target():
    name = f"restored_consent_{secrets.token_hex(4)}_test"
    yield bk.NewPostgresDatabase(PG_URL, name)
    admin = create_engine(make_url(PG_URL).set(database="postgres"),
                          isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


def test_migration_chains_on_current_predecessor_and_plugin_install_preserves_one_core_head(engine):
    """Mutation: branch from an older revision or introduce a second core head."""
    scripts = ScriptDirectory.from_config(migrate.config(plugins=False))
    assert scripts.get_heads() == ["0018"]
    assert scripts.get_revision("0018").down_revision == "0017"
    assert "0009" in {r.revision for r in scripts.walk_revisions()}
    drop_everything(engine)
    with engine.begin() as conn:
        migrate.upgrade(conn, "0016")
        assert not inspect(conn).has_table("hosted_books_consent")
        migrate.upgrade(conn)
        assert inspect(conn).has_table("hosted_books_consent")
        assert migrate.applied_revisions(conn) == {"0018"}
        install_into(conn, REGISTRY.plugins["sales"])
        core_rows = migrate.applied_revisions(conn) & set(migrate.core_revisions())
        assert core_rows == {"0018"}
        assert ScriptDirectory.from_config(migrate.config(plugins=False)).get_heads() == ["0018"]


def test_schema_keeps_required_types_company_foreign_key_and_core_owner(engine):
    """Mutation: omit a field, allow NULL, change its type, or omit core ownership."""
    table = m.hosted_books_consent
    assert set(table.c.keys()) == set(COLUMNS)
    assert table.info == {"owner": "core", "runtime": READ}
    assert {fk.target_fullname for fk in table.c.company_id.foreign_keys} == {"company.id"}
    with engine.connect() as conn:
        schema = inspect(conn)
        columns = {c["name"]: c for c in schema.get_columns(table.name)}
        assert set(columns) == set(COLUMNS)
        for name, length in COLUMNS.items():
            assert columns[name]["nullable"] is False
            assert table.c[name].nullable is False
            if length is None:
                assert columns[name]["type"].timezone and table.c[name].type.timezone
            else:
                assert columns[name]["type"].length == table.c[name].type.length == length
        assert schema.get_pk_constraint(table.name)["constrained_columns"] == ["company_id", "operation_key"]
        assert any(f["constrained_columns"] == ["company_id"] and f["referred_table"] == "company"
                   and f["referred_columns"] == ["id"] for f in schema.get_foreign_keys(table.name))


def test_second_live_insert_fails_on_partial_unique_index_but_two_withdrawn_inserts_succeed(engine, company_id):
    """Mutation: omit the live unique index or make withdrawn history unique too."""
    table = m.hosted_books_consent
    with engine.begin() as conn:
        for key in ("old-one", "old-two"):
            conn.execute(table.insert().values(**consent(company_id, key, "withdrawn")))
        conn.execute(table.insert().values(**consent(company_id)))
        with pytest.raises(DBAPIError) as raised, conn.begin_nested():
            conn.execute(table.insert().values(**consent(company_id, "second")))
        assert raised.value.orig.sqlstate == "23505"
        assert raised.value.orig.diag.constraint_name == "uq_hosted_books_consent_live"
        assert len(conn.execute(select(table)).all()) == 3


@pytest.mark.parametrize("change,state", [({"status": "unknown"}, "23514"),
                                         ({"company_id": "missing"}, "23503")]
                         + [({name: None}, "23502") for name in COLUMNS])
def test_invalid_status_company_or_null_insert_is_refused(engine, company_id, change, state):
    """Mutation: remove the status check, company foreign key, or a NOT NULL constraint."""
    with engine.begin() as conn:
        with pytest.raises(DBAPIError) as raised, conn.begin_nested():
            conn.execute(m.hosted_books_consent.insert().values(**(consent(company_id) | change)))
        assert raised.value.orig.sqlstate == state


def test_lookup_ignores_withdrawn_rows_returns_live_row_and_writes_nothing(engine, company_id):
    """Mutation: fabricate absent consent, treat withdrawn history as live, omit fields, or write during lookup."""
    with engine.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert live_consent(conn, company_id) is None
    with engine.begin() as conn:
        conn.execute(m.hosted_books_consent.insert().values(**consent(company_id, "old", "withdrawn")))
    with engine.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert live_consent(conn, company_id) is None
    row = consent(company_id)
    with engine.begin() as conn:
        conn.execute(m.hosted_books_consent.insert().values(**row))
    with engine.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert dict(live_consent(conn, company_id)) == row


def test_lookup_never_returns_another_companys_live_row(engine, company_id):
    """Mutation: omit the company predicate, even when both companies have live consent."""
    with engine.begin() as conn:
        conn.execute(m.company.insert().values(id="other", name="Other consent books", functional_currency="USD",
                                              fiscal_year_start_month=1, created_at=AT))
        conn.execute(m.hosted_books_consent.insert().values(**consent("other", "other-consent")))
        assert live_consent(conn, company_id) is None
        conn.execute(m.hosted_books_consent.insert().values(**consent(company_id)))
        assert live_consent(conn, company_id)["company_id"] == company_id
        assert live_consent(conn, "other")["operation_key"] == "other-consent"


def test_runtime_grants_allow_only_select_through_the_books_policy(engine, company_id, runtime_login):
    """Mutation: omit the grants policy or grant runtime writes or schema authority."""
    row = consent(company_id)
    with engine.begin() as conn:
        conn.execute(m.hosted_books_consent.insert().values(**row))
    role = make_url(runtime_login).username
    with engine.connect() as conn:
        assert privilege_differences(conn, role) == []
        held = dict(conn.exec_driver_sql(
            "SELECT p, has_table_privilege(%(role)s, 'hosted_books_consent', p) "
            "FROM unnest(%(privileges)s::text[]) p", {"role": role, "privileges": list(TABLE_PRIVILEGES)}).all())
        assert held == {p: p == "SELECT" for p in TABLE_PRIVILEGES}
        reference = dict(conn.exec_driver_sql(
            "SELECT p, has_table_privilege(%(role)s, 'books_identity', p) "
            "FROM unnest(%(privileges)s::text[]) p", {"role": role, "privileges": list(TABLE_PRIVILEGES)}).all())
        assert held == reference
    runtime = create_engine(runtime_login)
    try:
        with runtime.begin() as conn:
            assert dict(live_consent(conn, company_id)) == row
            for statement in (m.hosted_books_consent.insert().values(**consent(company_id, "new")),
                              m.hosted_books_consent.update().values(status="withdrawn"),
                              m.hosted_books_consent.delete(), text("TRUNCATE hosted_books_consent")):
                with pytest.raises(DBAPIError) as raised, conn.begin_nested():
                    conn.execute(statement)
                assert raised.value.orig.sqlstate == "42501"
    finally:
        runtime.dispose()


@pytest.mark.parametrize("live", [True, False], ids=["live-and-withdrawn", "only-withdrawn"])
def test_backup_restore_preserves_live_and_withdrawn_rows(engine, company_id, target, tmp_path, live):
    """Mutation: omit consent from backups, alter route evidence, or revive withdrawn history."""
    rows = ([consent(company_id)] if live else []) + [
        consent(company_id, "old-one", "withdrawn"), consent(company_id, "old-two", "withdrawn")]
    with engine.begin() as conn:
        conn.execute(m.hosted_books_consent.insert(), rows)
    path = tmp_path / "backup"
    manifest = bk.backup(engine, path)
    assert manifest["tables"]["hosted_books_consent"]["owner"] == "core"
    assert manifest["tables"]["hosted_books_consent"]["rows"] == len(rows)
    reflected = backup_reflect.backup(engine, tmp_path / "reflected")
    assert reflected["tables"]["hosted_books_consent"] == manifest["tables"]["hosted_books_consent"]
    bk.restore(path, target)
    restored = create_engine(make_url(target.server_url).set(database=target.name))
    try:
        with restored.connect() as conn:
            found = live_consent(conn, company_id)
            assert (dict(found) if found is not None else None) == (rows[0] if live else None)
            actual = list(conn.execute(select(m.hosted_books_consent).order_by(
                m.hosted_books_consent.c.operation_key)).mappings())
            assert [dict(r) for r in actual] == rows
        assert bk.verify(path, target) == []
    finally:
        restored.dispose()


def test_restore_of_predecessor_without_table_never_fabricates_consent(engine, target, tmp_path):
    """Mutation: fail open on a missing table or seed live consent while restoring older books."""
    drop_everything(engine)
    with engine.begin() as conn:
        migrate.upgrade(conn, "0016")
        conn.execute(owner.insert().values(mark=OWNER_MARK))
        conn.execute(books_identity.insert().values(id=1, books_id=str(uuid.uuid4())))
        conn.execute(m.company.insert().values(id="older", name="Older books", functional_currency="USD",
                                              fiscal_year_start_month=1, created_at=AT))
        conn.execute(m.posting_chain_head.insert().values(company_id="older", last_seq=0, last_hash="0" * 64))
        assert live_consent(conn, "older") is None
        assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1
    path = tmp_path / "older"
    manifest = backup_reflect.backup(engine, path)
    assert "hosted_books_consent" not in manifest["tables"]
    restore_upgrade.restore(path, target)
    restored = create_engine(make_url(target.server_url).set(database=target.name))
    try:
        with restored.connect() as conn:
            assert inspect(conn).has_table("hosted_books_consent")
            assert live_consent(conn, "older") is None
            assert not conn.execute(select(m.hosted_books_consent)).first()
    finally:
        restored.dispose()


def test_restored_books_with_table_or_live_row_removed_have_no_consent(engine, company_id):
    """Mutation: cache live consent after its row or table disappears."""
    with engine.begin() as conn:
        conn.execute(m.hosted_books_consent.insert().values(**consent(company_id)))
        assert live_consent(conn, company_id) is not None
        conn.execute(m.hosted_books_consent.delete())
        assert live_consent(conn, company_id) is None
        conn.exec_driver_sql("DROP TABLE hosted_books_consent")
        assert live_consent(conn, company_id) is None
        assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1
