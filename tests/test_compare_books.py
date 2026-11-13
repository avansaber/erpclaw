"""Compare real PostgreSQL snapshots, without exposing data or changing books."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from conftest import PG_URL

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'compare_books.py'
spec = importlib.util.spec_from_file_location('compare_books', SCRIPT)
compare_books = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_books)


@pytest.fixture
def pair(other_database):
    from conftest import drop_everything

    left = create_engine(PG_URL)
    drop_everything(left)
    right = create_engine(other_database)
    for engine in (left, right):
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE "odd\"\"table" (id integer PRIMARY KEY, secret text)')
            conn.exec_driver_sql("INSERT INTO \"odd\"\"table\" VALUES (1, 'private row value')")
            conn.exec_driver_sql('CREATE TABLE plugin_install (name text PRIMARY KEY, version text)')
            conn.exec_driver_sql("INSERT INTO plugin_install VALUES ('sales', 'old')")
            conn.exec_driver_sql('CREATE TABLE no_key (value text)')
            conn.exec_driver_sql("INSERT INTO no_key VALUES ('same')")
    try:
        yield left, right, PG_URL, other_database
    finally:
        drop_everything(left)
        left.dispose()
        right.dispose()


def run(left, right, *args):
    env = dict(os.environ, OLD_DATABASE_URL=left, NEW_DATABASE_URL=right)
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_identical_books_and_changed_row(pair):
    left, right, url1, url2 = pair
    result = run(url1, url2)
    assert result.returncode == 0, result.stderr
    assert 'odd"table: same (left 1, right 1)' in result.stdout
    assert 'no_key: same' in result.stdout and 'depends on the server collation' in result.stdout
    with right.begin() as conn:
        conn.exec_driver_sql("UPDATE \"odd\"\"table\" SET secret = 'different private row'")
    result = run(url1, url2)
    assert result.returncode == 1 and 'odd"table: differs' in result.stdout
    for secret in ('private row', url1, url2, make_url(url1).password):
        assert secret not in result.stdout + result.stderr


def test_expected_differences_are_repeatable_and_scoped(pair):
    left, right, url1, url2 = pair
    with right.begin() as conn:
        conn.exec_driver_sql("UPDATE plugin_install SET version = 'new'")
    assert run(url1, url2).returncode == 1
    assert run(url1, url2, '--expect-differ', 'plugin_install').returncode == 0
    with right.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE no_key ADD COLUMN extra text')
    result = run(url1, url2, '--expect-differ', 'plugin_install')
    assert result.returncode == 1
    assert 'no_key: differs' in result.stdout and 'column extra only on right' in result.stdout
    assert run(url1, url2, '--expect-differ', 'plugin_install', '--expect-differ', 'no_key').returncode == 0


def test_missing_tables_and_columns_are_reported(pair):
    left, right, url1, url2 = pair
    with right.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE only_right (id integer)')
        conn.exec_driver_sql('ALTER TABLE plugin_install ADD COLUMN only_right text')
    result = run(url1, url2)
    assert result.returncode == 1
    assert 'only_right: differs (table only on right)' in result.stdout
    assert 'plugin_install: column only_right only on right' in result.stdout
    assert run(url1, url2, '--expect-differ', 'only_right', '--expect-differ', 'plugin_install').returncode == 0


def test_connections_are_read_only_repeatable_read_and_reject_writes(pair, monkeypatch, capsys):
    left, right, url1, url2 = pair
    opened = []
    open_original = compare_books._open

    def inspect_connection(url):
        conn = open_original(url)
        opened.append(conn)
        assert conn.execute('SHOW transaction_read_only').fetchone() == ('on',)
        assert conn.execute('SHOW transaction_isolation').fetchone() == ('repeatable read',)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn.transaction():
                conn.execute("UPDATE plugin_install SET version = 'should not write'")
        return conn

    monkeypatch.setattr(compare_books, '_open', inspect_connection)
    assert compare_books.compare(url1, url2)
    assert len(opened) == 2 and all(conn.closed for conn in opened)
    for engine in (left, right):
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT version FROM plugin_install').scalar_one() == 'old'
    capsys.readouterr()


def test_connection_errors_never_print_credentials(pair):
    _, _, url1, url2 = pair
    result = run(url1, url2 + '?invalid_secret_option=private-value')
    assert result.returncode == 2
    assert 'private-value' not in result.stderr and make_url(url1).password not in result.stderr
