"""Routed Constitution article reads leave a fresh installation unchanged."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[2]
LIB = SCRIPTS / "erpclaw-setup" / "lib"


@pytest.fixture
def installed_database(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    database = install / "data.sqlite"
    monkeypatch.setenv("ERPCLAW_HOME", str(install))
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_DIALECT", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_READONLY", raising=False)
    sys.path.insert(0, str(LIB))
    spec = importlib.util.spec_from_file_location(
        "articles_action_init_schema", SCRIPTS / "erpclaw-setup" / "init_schema.py"
    )
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    schema.init_db(str(database))
    return install


def _snapshot(install):
    return {
        str(path.relative_to(install)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in install.rglob("*") if path.is_file()
    }


def _call(install, article_type=None):
    env = {
        **os.environ,
        "ERPCLAW_HOME": str(install),
        "PYTHONPATH": str(LIB),
        "ERPCLAW_DB_READONLY": "1",
    }
    env.pop("ERPCLAW_ACTOR_CONTEXT", None)
    env.pop("ERPCLAW_TEST_SESSION", None)
    command = [sys.executable, str(SCRIPTS / "db_query.py"),
               "--action", "list-articles"]
    if article_type is not None:
        command.extend(["--article-type", article_type])
    return subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize("article_type, numbers", [
    (None, set(range(1, 22))),
    ("all", set(range(1, 22))),
    ("static", set(range(1, 9)) | {10, 11, 12, 19, 20, 21}),
    ("runtime", {9, 13, 14, 15, 16, 17, 18}),
])
def test_list_articles_routes_exact_selection_without_writes(
    installed_database, article_type, numbers
):
    before = _snapshot(installed_database)
    result = _call(installed_database, article_type)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    articles = payload["articles"]
    assert payload["count"] == len(numbers) == len(articles)
    assert {article["number"] for article in articles} == numbers
    assert [article["number"] for article in articles] == sorted(numbers)
    by_number = {article["number"]: article for article in articles}
    if article_type in ("static", "runtime"):
        assert {article["enforcement"] for article in articles} == {article_type}
    if 2 in by_number:
        assert by_number[2]["name"] == "Money is TEXT"
        assert by_number[2]["severity"] == "critical"
        assert by_number[2]["bypass_policy"] == "never"
        assert "Python Decimal" in by_number[2]["description"]
    if 9 in by_number:
        assert by_number[9]["name"] == "Tests Pass"
        assert by_number[9]["enforcement"] == "runtime"
    assert _snapshot(installed_database) == before


def test_list_articles_refuses_unknown_filter_without_writes(installed_database):
    before = _snapshot(installed_database)
    result = _call(installed_database, "unknown-category")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "invalid choice" in payload["message"]
    assert "unknown-category" in payload["message"]
    assert "articles" not in payload
    assert _snapshot(installed_database) == before
