"""m842: every foundation migration reaches the database on a URL target.

Two migrations once treated the runner's URL target as a file path and
returned success without opening anything. This probe fails if any
foundation migration can return normally on a URL target without touching
the database or asking its catalog: every seam entry point and the driver
entry point raise a private signal, so a normal return proves a skip.
"""
import ast
import importlib.util
import io
import os
import sys
import types
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_RUNNER_PATH = os.path.join(_SETUP_DIR, "migration_runner.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import setup_helpers  # noqa: F401  (binds erpclaw_lib to this tree)

import erpclaw_lib.db as _db
import erpclaw_lib.seam as _seam


class _Reached(BaseException):
    pass


_PROBE_URL = "postgresql://probe@127.0.0.1:1/m842_probe"


def _load_runner():
    spec = importlib.util.spec_from_file_location("migration_runner_m842", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_runner = _load_runner()
_DISCOVERED = [(stem, path) for stem, path in _runner.discover()]
_IDS = [stem for stem, _ in _DISCOVERED]


def _raise(name):
    def _fn(*args, **kwargs):
        raise _Reached(name)
    return _fn


def _install_probes(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PROBE_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setattr(_db, "get_connection", _raise("db.get_connection"))
    for attr in ("get_engine", "table_exists", "table_names", "column_names",
                 "index_names", "describe_table", "provision",
                 "provision_authority_core", "provision_authority_envelope"):
        monkeypatch.setattr(_seam, attr, _raise("seam." + attr))
    pg = types.ModuleType("psycopg2")
    pg.__path__ = []

    def _connect(*args, **kwargs):
        raise _Reached("psycopg2.connect")

    pg.connect = _connect
    extras = types.ModuleType("psycopg2.extras")

    class _DictCursor:  # never used; present so the import shape holds
        pass

    extras.DictCursor = _DictCursor
    extensions = types.ModuleType("psycopg2.extensions")
    monkeypatch.setitem(sys.modules, "psycopg2", pg)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    monkeypatch.setitem(sys.modules, "psycopg2.extensions", extensions)


def _has_reached(exc):
    seen = exc
    while seen is not None:
        if isinstance(seen, _Reached):
            return True
        seen = getattr(seen, "__cause__", None) or getattr(seen, "__context__", None)
        if seen is None:
            break
    return False


@pytest.mark.parametrize("stem,path", _DISCOVERED, ids=_IDS)
def test_migration_reaches_postgresql(monkeypatch, stem, path):
    _install_probes(monkeypatch)
    spec = importlib.util.spec_from_file_location("m842_probe_" + stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mod.run_migration(_PROBE_URL)
    except _Reached:
        return
    except BaseException as exc:
        if _has_reached(exc):
            return
        raise
    raise AssertionError(
        "%s returned on a URL target without opening the database "
        "or asking its catalog\ncaptured stdout:\n%s" % (stem, buf.getvalue()))


def _contains_path_exists(node, _root=False):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and not _root:
        return False
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "exists"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"):
            return True
    for child in ast.iter_child_nodes(node):
        if _contains_path_exists(child):
            return True
    return False


def _is_pg_guard(stmt):
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    if not isinstance(test, ast.Compare):
        return False
    sides = [test.left] + list(test.comparators)
    if not any(isinstance(side, ast.Constant) and side.value == "postgresql" for side in sides):
        return False
    if not stmt.body:
        return False
    return isinstance(stmt.body[-1], (ast.Return, ast.Raise))


def test_postgres_branch_comes_first():
    bad = []
    for stem, path in _DISCOVERED:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        func = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "run_migration":
                func = node
                break
        if func is None:
            continue
        for index, stmt in enumerate(func.body):
            if _contains_path_exists(stmt, _root=True):
                if not any(_is_pg_guard(prev) for prev in func.body[:index]):
                    bad.append(stem)
                break
    assert not bad, "run_migration checks the file path before the backend first: %s" % bad
