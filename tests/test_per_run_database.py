"""Per-run PostgreSQL databases are isolated, inherited, and removed by their owner."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import conftest


CONFTEST = Path(conftest.__file__).resolve()
PG_URL = conftest.PG_URL
BASE_NAME = make_url(PG_URL).database
IMPORT = f"""
import importlib.util
import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
spec = importlib.util.spec_from_file_location('run_conftest', {str(CONFTEST)!r})
conf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conf)
"""


def _environment(flag="1"):
    env = dict(os.environ)
    for key in ("ERPCLAW_LOCKS_HELD", "GITHUB_ACTIONS", "ERPCLAW_TEST_DB_PRIVATE", "ERPCLAW_TEST_DB_PER_RUN"):
        env.pop(key, None)
    env["ERPCLAW_PG_TEST_URL"] = PG_URL
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if flag is not None:
        env["ERPCLAW_TEST_DB_PER_RUN"] = flag
    return env


@pytest.fixture
def admin():
    engine = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


def _exists(admin, name):
    with admin.connect() as conn:
        return bool(conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(name)s",
                                         {"name": name}).scalar())


def _wait_for(path, processes):
    deadline = time.monotonic() + 45
    while not path.exists():
        for process in processes:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Child exited before its signal: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for {path.name}")
        time.sleep(0.02)


def test_concurrent_pytest_runs_are_isolated_and_drop_only_their_own_database(tmp_path, admin):
    processes, ready, releases, names = [], [], [], []
    try:
        for label in ("first", "second"):
            folder = tmp_path / label
            folder.mkdir()
            signal, release = folder / "ready.json", folder / "release"
            ready.append(signal)
            releases.append(release)
            (folder / "conftest.py").write_text(CONFTEST.read_text())
            (folder / "test_child.py").write_text(f'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from conftest import PG_URL

def test_private_database():
    assert PG_URL == os.environ["ERPCLAW_PG_TEST_URL"]
    name = make_url(PG_URL).database
    engine = create_engine(PG_URL, poolclass=NullPool)
    with engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT current_database()").scalar() == name
        conn.exec_driver_sql("CREATE TABLE {label}_only (value int)")
    inherited = subprocess.run([sys.executable, "-c", "import os; from sqlalchemy import create_engine; "
        "engine = create_engine(os.environ['ERPCLAW_PG_TEST_URL']); "
        "conn = engine.connect(); print(conn.exec_driver_sql('SELECT current_database()').scalar())"],
        capture_output=True, text=True, check=True, timeout=20)
    assert inherited.stdout.strip() == name
    temporary = Path({str(signal)!r}).with_suffix(".tmp")
    temporary.write_text(json.dumps({{"name": name, "pid": os.getpid()}}))
    temporary.replace({str(signal)!r})
    deadline = time.monotonic() + 60
    while not Path({str(release)!r}).exists():
        assert time.monotonic() < deadline, "parent did not release child"
        time.sleep(0.02)
    with engine.connect() as conn:
        other = "second_only" if {label!r} == "first" else "first_only"
        assert conn.exec_driver_sql("SELECT to_regclass(%(table)s)", {{"table": other}}).scalar() is None
    engine.dispose()
''')
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider", str(folder)],
                cwd=folder, env=_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

        for signal in ready:
            _wait_for(signal, processes)
            info = json.loads(signal.read_text())
            name = info["name"]
            names.append(name)
            assert re.search(rf"_r{info['pid']}_[0-9a-f]{{8}}_test$", name)
            assert len(name.encode()) <= 63
        assert len(set(names)) == 2
        assert all(_exists(admin, name) for name in [BASE_NAME, *names])
        for index, label in enumerate(("first", "second")):
            engine = create_engine(make_url(PG_URL).set(database=names[index]), poolclass=NullPool)
            try:
                with engine.connect() as conn:
                    tables = set(conn.exec_driver_sql(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").scalars())
                assert tables == {f"{label}_only"}
            finally:
                engine.dispose()

        releases[0].touch()
        stdout, stderr = processes[0].communicate(timeout=45)
        assert processes[0].returncode == 0, stdout + stderr
        assert "1 passed" in stdout
        assert not _exists(admin, names[0])
        assert _exists(admin, names[1]) and _exists(admin, BASE_NAME)
        releases[1].touch()
        stdout, stderr = processes[1].communicate(timeout=45)
        assert processes[1].returncode == 0, stdout + stderr
        assert "1 passed" in stdout
        assert not _exists(admin, names[1])
        assert _exists(admin, BASE_NAME)
    finally:
        for release in releases:
            release.touch()
        for process in processes:
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        # If a child was killed, remove only names it reported as its own.
        for signal in ready:
            if signal.exists():
                name = json.loads(signal.read_text())["name"]
                with admin.connect() as conn:
                    quoted = conn.dialect.identifier_preparer.quote_identifier(name)
                    conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")


@pytest.mark.parametrize("base", ["build_test", "a" * 100 + "_test", "é" * 100 + "_test",
                                 '1 space"quote_test', "_test"])
def test_database_name_keeps_the_suffix_and_fits_an_identifier(base, monkeypatch):
    monkeypatch.setattr(conftest.os, "getpid", lambda: 123456)
    monkeypatch.setattr(conftest.secrets, "token_hex", lambda size: "abcdef01")
    name = conftest._per_run_database_name(base)
    assert name.endswith("_r123456_abcdef01_test")
    assert len(name.encode()) <= 63
    assert re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name)
    if base == "build_test":
        assert name == "build_r123456_abcdef01_test"
    if len(base) > 63:
        assert len(name.encode()) == 63


@pytest.mark.parametrize("flag", [None, "0"])
def test_without_the_flag_the_url_is_unchanged(flag):
    env = _environment(flag)
    env["ERPCLAW_TEST_DB_PRIVATE"] = "1"
    result = subprocess.run([sys.executable, "-c", IMPORT + '''
assert conf.PG_URL == os.environ["ERPCLAW_PG_TEST_URL"] == os.environ["EXPECTED_URL"]
assert conf._PER_RUN_DATABASE is None
'''], env={**env, "EXPECTED_URL": PG_URL}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid", ["production", "overrides"])
def test_per_run_creation_still_requires_a_safe_base_url(invalid):
    url = make_url(PG_URL)
    url = url.set(database="production") if invalid == "production" else url.update_query_dict({"options": "x"})
    result = subprocess.run([sys.executable, "-c", IMPORT],
                            env={**_environment(), "ERPCLAW_PG_TEST_URL": url.render_as_string(hide_password=False)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "must name a *_test database and carry no connection overrides" in result.stderr


def test_import_without_pytest_drops_its_database_at_exit(admin):
    result = subprocess.run([sys.executable, "-c", IMPORT + '''
name = make_url(conf.PG_URL).database
assert conf.PG_URL == os.environ["ERPCLAW_PG_TEST_URL"]
engine = create_engine(conf.PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
conn = engine.connect()
assert conn.exec_driver_sql("SELECT current_database()").scalar() == name
print(name, flush=True)
'''], env=_environment(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    name = result.stdout.strip()
    assert re.search(r"_r\d+_[0-9a-f]{8}_test$", name)
    assert not _exists(admin, name)
    assert _exists(admin, BASE_NAME)


def test_cleanup_ignores_changed_urls_and_warns_without_failing(admin):
    result = subprocess.run([sys.executable, "-c", IMPORT + '''
import contextlib
import io
from unittest.mock import patch
name = make_url(conf.PG_URL).database
engine = create_engine(conf.PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
conn = engine.connect()
assert conn.exec_driver_sql("SELECT current_database()").scalar() == name
conf.PG_URL = os.environ["EXISTING_URL"]
os.environ["ERPCLAW_PG_TEST_URL"] = conf.PG_URL
warning = io.StringIO()
with patch("sqlalchemy.create_engine", side_effect=RuntimeError("cannot connect")):
    with contextlib.redirect_stderr(warning):
        conf._cleanup_per_run_database()
assert name in warning.getvalue() and "Warning" in warning.getvalue()
assert conf._PER_RUN_DATABASE == name
conf._cleanup_per_run_database()
conf._cleanup_per_run_database()
assert conf._PER_RUN_DATABASE is None
print(name, flush=True)
'''], env={**_environment(), "EXISTING_URL": PG_URL}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _exists(admin, result.stdout.strip())
    assert _exists(admin, BASE_NAME)
