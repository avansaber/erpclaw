"""Safety checks for the documented update workflow."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from conftest import PG_URL
from erpclaw_cli.main import app
from erpclaw_core.backup import BackupRefused, _q
from test_principal_session_tables import auth_rows  # noqa: F401
from test_restore_sessions import saved, target  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/compare_books.py'
spec = importlib.util.spec_from_file_location('update_compare', SCRIPT)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_comparison_allows_named_additions_but_checks_book_rows(other_database):
    left = create_engine(PG_URL)
    right = create_engine(other_database)
    from conftest import drop_everything
    drop_everything(left)
    try:
        for engine in (left, right):
            with engine.begin() as conn:
                conn.exec_driver_sql('CREATE TABLE book (id integer PRIMARY KEY, value text)')
                conn.exec_driver_sql("INSERT INTO book VALUES (1, 'original')")
                conn.exec_driver_sql('CREATE TABLE alembic_version (version_num text)')
                conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('old')")
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE alembic_version SET version_num = 'new'")
            conn.exec_driver_sql('ALTER TABLE book ADD COLUMN added text')
            conn.exec_driver_sql('CREATE TABLE added_table (id integer)')
        options = dict(expected=['public.alembic_version'], added_tables=['public.added_table'],
                       added_columns=['public.book.added'])
        assert comparison.compare(PG_URL, other_database, **options)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE book SET value = 'changed'")
        assert not comparison.compare(PG_URL, other_database, **options)
        with right.begin() as conn:
            conn.exec_driver_sql('DELETE FROM book')
        assert not comparison.compare(PG_URL, other_database, **options)
    finally:
        drop_everything(left)
        left.dispose()
        right.dispose()


def test_unqualified_expectation_does_not_exempt_other_schema(other_database):
    left = create_engine(PG_URL)
    right = create_engine(other_database)
    from conftest import drop_everything
    drop_everything(left)
    try:
        for engine in (left, right):
            with engine.begin() as conn:
                conn.exec_driver_sql('CREATE SCHEMA private')
                conn.exec_driver_sql('CREATE TABLE private.plugin_install (value text)')
        with right.begin() as conn:
            conn.exec_driver_sql("INSERT INTO private.plugin_install VALUES ('changed')")
        assert not comparison.compare(PG_URL, other_database, ['plugin_install'])
    finally:
        for engine in (left, right):
            with engine.begin() as conn:
                conn.exec_driver_sql('DROP SCHEMA private CASCADE')
        drop_everything(left)
        left.dispose()
        right.dispose()


def test_restore_invalid_name_redacts_url(monkeypatch, tmp_path):
    secret = 'hidden-password'
    url = f'postgresql://owner:{secret}@host/books'
    with pytest.raises(BackupRefused) as exc:
        _q(url)
    assert url not in str(exc.value) and secret not in str(exc.value)


def test_comparison_uses_environment_without_url_arguments(other_database):
    env = dict(os.environ, OLD_DATABASE_URL=PG_URL, NEW_DATABASE_URL=other_database)
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode != 2, result.stderr
    rejected = subprocess.run([sys.executable, str(SCRIPT), 'unexpected-argument'], env=env, capture_output=True, text=True)
    assert rejected.returncode == 2 and 'unexpected-argument' not in rejected.stderr


def test_runbook_connection_is_libpq_safe():
    doc = (ROOT / 'docs/UPDATING.md').read_text()
    assert 'psql "$ERPCLAW_MIGRATION_URL"' not in doc
    assert 'PGPASSFILE' in doc and 'psql -w' in doc


def test_runbook_uses_absolute_new_home():
    doc = (ROOT / 'docs/UPDATING.md').read_text()
    assert 'export NEW_HOME=/' in doc
    assert 'export ERPCLAW_HOME="$NEW_HOME"' in doc
    assert 'mkdir NEW_HOME' not in doc


def test_runbook_unsets_owner_before_chat_and_restores_for_maintenance():
    doc = (ROOT / 'docs/UPDATING.md').read_text()
    assert doc.count('unset ERPCLAW_MIGRATION_URL') >= 3
    assert 'set ERPCLAW_MIGRATION_URL again' in doc


def test_runbook_resolves_links_on_copy():
    doc = (ROOT / 'docs/UPDATING.md').read_text()
    assert 'cp -aL ' in doc and 'symbolic links' in doc


def test_real_migration_preserves_existing_rows(other_database):
    from conftest import drop_everything
    from erpclaw_core.migrate import upgrade

    left = create_engine(PG_URL)
    right = create_engine(other_database)
    drop_everything(left)
    try:
        for engine in (left, right):
            with engine.begin() as conn:
                upgrade(conn, '0017')
        with right.begin() as conn:
            upgrade(conn, '0018')
        options = dict(expected=['public.alembic_version'], added_columns=[
            'public.supplier_payable.' + name for name in (
                'posting_id', 'control_line_no', 'source_voucher_id', 'opening_sequence', 'old_reference', 'due_date')])
        assert comparison.compare(PG_URL, other_database, **options)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE currency SET name = 'changed' WHERE code = 'USD'")
        assert not comparison.compare(PG_URL, other_database, **options)
        with right.begin() as conn:
            conn.exec_driver_sql("DELETE FROM currency WHERE code = 'USD'")
        assert not comparison.compare(PG_URL, other_database, **options)
    finally:
        drop_everything(left)
        left.dispose()
        right.dispose()


def test_restore_cli_redacts_invalid_target(monkeypatch, tmp_path):
    from erpclaw_core import backup
    secret = 'hidden-password'
    url = f'postgresql://owner:{secret}@host/books'
    monkeypatch.setenv('ERPCLAW_MIGRATION_URL', PG_URL)
    def refuse(path, target):
        backup._q(target.name)
    monkeypatch.setattr(backup, 'restore', refuse)
    result = CliRunner().invoke(app, ['restore', str(tmp_path), '--to-database', url])
    assert result.exit_code != 0
    assert url not in result.output and 'hidden-password' not in result.output


def test_comparison_accepts_only_restore_authentication_changes(other_database):
    from conftest import drop_everything
    from erpclaw_core.restore_upgrade import _void_authentication

    left = create_engine(PG_URL)
    right = create_engine(other_database)
    drop_everything(left)
    try:
        for engine in (left, right):
            with engine.begin() as conn:
                conn.exec_driver_sql('CREATE TABLE principal_session (id text PRIMARY KEY, ended_at timestamptz, ended_why text, note text)')
                conn.exec_driver_sql('CREATE TABLE principal_sign_in (code_sha256 text PRIMARY KEY, used_at timestamptz, note text)')
                conn.exec_driver_sql("INSERT INTO principal_session VALUES ('one', NULL, NULL, 'original')")
                conn.exec_driver_sql("INSERT INTO principal_sign_in VALUES ('one', NULL, 'original')")
        with right.begin() as conn:
            _void_authentication(conn)
        assert comparison.compare(PG_URL, other_database)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE principal_session SET ended_why = 'other'")
        assert not comparison.compare(PG_URL, other_database)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE principal_session SET ended_why = 'restored'")
            conn.exec_driver_sql("UPDATE principal_sign_in SET note = 'changed'")
        assert not comparison.compare(PG_URL, other_database)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE principal_sign_in SET note = 'original'")
            conn.exec_driver_sql("INSERT INTO principal_sign_in VALUES ('extra', now(), 'original')")
        assert not comparison.compare(PG_URL, other_database)
        with right.begin() as conn:
            conn.exec_driver_sql("DELETE FROM principal_sign_in WHERE code_sha256 = 'extra'")
            conn.exec_driver_sql("UPDATE principal_session SET note = 'changed'")
        assert not comparison.compare(PG_URL, other_database)
        with right.begin() as conn:
            conn.exec_driver_sql("UPDATE principal_session SET note = 'original'")
            conn.exec_driver_sql('DELETE FROM principal_session')
        assert not comparison.compare(PG_URL, other_database)
    finally:
        drop_everything(left)
        left.dispose()
        right.dispose()


def test_upgrade_with_live_authentication_compares(engine, saved, target):
    from sqlalchemy.engine import make_url
    from erpclaw_core.restore_upgrade import restore

    restore(saved, target)
    restored_url = make_url(target.server_url).set(database=target.name).render_as_string(hide_password=False)
    assert comparison.compare(PG_URL, restored_url, expected=['plugin_install', 'alembic_version'])
